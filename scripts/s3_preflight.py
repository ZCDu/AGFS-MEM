"""
Validates that an S3 bucket is actually usable by this service BEFORE you
point the server at it.

    python scripts/s3_preflight.py --bucket my-bucket --prefix memory

Checks, in dependency order, the six things that break real deployments:

  1. Credentials resolve at all.
  2. The bucket exists and is reachable from this region.
  3. s3:ListBucket — the big one. Without it S3 answers 403 instead of 404
     for a key that does not exist, and this store asks "does this object
     exist?" on nearly every path, so a readable+writable bucket still
     fails on the first upsert with an opaque AccessDenied.
  4. PutObject / GetObject / DeleteObject round-trip.
  5. Conditional writes (If-None-Match and If-Match). Every write in this
     store is a conditional write — this is the compatibility-critical
     feature and the one non-AWS S3 clones most often lack.
  6. Bucket versioning, which interacts badly with a write-heavy workload
     unless you have a lifecycle rule to expire noncurrent versions.

Exits non-zero if anything fatal fails. Cleans up its own probe objects.
"""

from __future__ import annotations

import argparse
import sys
import uuid

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

_failed = False


def ok(msg: str) -> None:
    print(f"  {GREEN}PASS{RESET}  {msg}")


def warn(msg: str, detail: str = "") -> None:
    print(f"  {YELLOW}WARN{RESET}  {msg}")
    if detail:
        print(f"        {DIM}{detail}{RESET}")


def fail(msg: str, detail: str = "") -> None:
    global _failed
    _failed = True
    print(f"  {RED}FAIL{RESET}  {msg}")
    if detail:
        print(f"        {DIM}{detail}{RESET}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--prefix", default="")
    ap.add_argument("--region", default=None)
    ap.add_argument("--endpoint-url", default=None,
                    help="For S3-compatible stores (MinIO, R2, Ceph).")
    args = ap.parse_args()

    try:
        import boto3
        from botocore.exceptions import ClientError, NoCredentialsError
    except ImportError:
        print(f"{RED}boto3 is not installed. Run: pip install boto3{RESET}")
        return 2

    kw = {}
    if args.region:
        kw["region_name"] = args.region
    if args.endpoint_url:
        kw["endpoint_url"] = args.endpoint_url
    s3 = boto3.client("s3", **kw)

    prefix = args.prefix.rstrip("/")
    probe = f"{prefix + '/' if prefix else ''}_preflight/{uuid.uuid4().hex}.txt"
    absent = f"{prefix + '/' if prefix else ''}_preflight/definitely-absent-{uuid.uuid4().hex}"

    print(f"\nBucket: s3://{args.bucket}/{prefix or ''}")
    if args.endpoint_url:
        print(f"Endpoint: {args.endpoint_url}")
    print()

    # 1. credentials
    try:
        ident = boto3.client("sts", **kw).get_caller_identity()
        ok(f"credentials resolve ({ident['Arn']})")
    except NoCredentialsError:
        fail("no AWS credentials found",
             "Set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, run `aws configure`, or attach an IAM role.")
        return 1
    except Exception as e:
        warn(f"could not call STS ({type(e).__name__})", "Not fatal — some S3-compatible stores lack STS.")

    # 2. bucket reachable
    try:
        s3.head_bucket(Bucket=args.bucket)
        ok("bucket exists and is reachable")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("404", "NoSuchBucket"):
            fail(f"bucket {args.bucket} does not exist")
        elif code in ("301", "PermanentRedirect"):
            fail("bucket is in a different region", "Pass --region / set AWS_DEFAULT_REGION to match.")
        else:
            fail(f"head_bucket failed: {code}")
        return 1

    # 3. ListBucket — the one that silently breaks everything
    try:
        s3.list_objects_v2(Bucket=args.bucket, Prefix=prefix, MaxKeys=1)
        ok("s3:ListBucket granted")
    except ClientError:
        fail("s3:ListBucket DENIED",
             "This is fatal for this service, even though reads and writes may work. "
             "Without ListBucket, S3 returns 403 (not 404) for keys that don't exist, so "
             "every existence check raises instead of returning None. Grant s3:ListBucket "
             "on the BUCKET arn, not just the object arn.")

    # 4. write / read / delete
    try:
        s3.put_object(Bucket=args.bucket, Key=probe, Body=b"preflight")
        ok("s3:PutObject granted")
    except ClientError as e:
        fail(f"s3:PutObject denied ({e.response['Error']['Code']})")
        return 1

    try:
        body = s3.get_object(Bucket=args.bucket, Key=probe)["Body"].read()
        ok("s3:GetObject granted" if body == b"preflight" else "GetObject returned unexpected bytes")
    except ClientError as e:
        fail(f"s3:GetObject denied ({e.response['Error']['Code']})")

    # 4b. absent key must be 404, not 403 — the concrete symptom of check 3
    try:
        s3.get_object(Bucket=args.bucket, Key=absent)
        warn("a key that should not exist returned data")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("NoSuchKey", "404"):
            ok("absent key returns 404 (existence checks will work)")
        else:
            fail(f"absent key returns {code}, expected NoSuchKey",
                 "This is the ListBucket problem above, in its concrete form. "
                 "The store cannot distinguish 'missing' from 'forbidden', so upserts will fail.")

    # 5. conditional writes — every write here is one
    try:
        s3.put_object(Bucket=args.bucket, Key=probe, Body=b"x", IfNoneMatch="*")
        fail("If-None-Match:* did NOT reject an overwrite of an existing key",
             "Conditional writes appear unsupported or ignored. Optimistic concurrency "
             "will silently lose writes on this endpoint.")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("PreconditionFailed", "ConditionalRequestConflict"):
            ok("conditional write via If-None-Match works")
        elif code in ("NotImplemented", "InvalidRequest"):
            fail("endpoint does not implement If-None-Match",
                 "Real AWS S3 has supported this since Aug 2024. Some S3-compatible "
                 "stores do not. This backend cannot run safely without it.")
        else:
            warn(f"unexpected code from If-None-Match probe: {code}")

    try:
        etag = s3.head_object(Bucket=args.bucket, Key=probe)["ETag"].strip('"')
        try:
            s3.put_object(Bucket=args.bucket, Key=probe, Body=b"y", IfMatch="0" * 32)
            fail("If-Match did NOT reject a stale ETag",
                 "Lost-update protection is not working on this endpoint.")
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("PreconditionFailed", "ConditionalRequestConflict"):
                ok("conditional write via If-Match works")
            elif code in ("NotImplemented", "InvalidRequest"):
                fail("endpoint does not implement If-Match on PutObject",
                     "AWS added this in Nov 2024. Required by this backend.")
            else:
                warn(f"unexpected code from If-Match probe: {code}")
        s3.put_object(Bucket=args.bucket, Key=probe, Body=b"z", IfMatch=etag)
        ok("If-Match accepts the current ETag")
    except ClientError as e:
        warn(f"could not complete If-Match probe: {e.response['Error']['Code']}")

    # 6. versioning interacts badly with a write-heavy workload
    try:
        status = s3.get_bucket_versioning(Bucket=args.bucket).get("Status")
        if status == "Enabled":
            warn("bucket versioning is ENABLED",
                 "This service rewrites the manifest frequently. Every rewrite keeps a "
                 "noncurrent version and you pay storage for all of them. Add a lifecycle "
                 "rule expiring noncurrent versions after N days, or use a separate bucket.")
        else:
            ok("bucket versioning is not enabled")
    except ClientError:
        warn("could not read versioning config (s3:GetBucketVersioning not granted)")

    # cleanup
    try:
        s3.delete_object(Bucket=args.bucket, Key=probe)
        ok("s3:DeleteObject granted (probe cleaned up)")
    except ClientError as e:
        warn(f"could not delete probe object {probe} ({e.response['Error']['Code']})",
             "Delete it manually. Cascade deletes will also fail without s3:DeleteObject.")

    print()
    if _failed:
        print(f"{RED}Preflight FAILED — fix the items above before starting the server.{RESET}\n")
        return 1
    print(f"{GREEN}Preflight passed. Safe to run with STORAGE_BACKEND=s3.{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
