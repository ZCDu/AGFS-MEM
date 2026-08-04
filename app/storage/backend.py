"""
Storage backend abstraction.

Two implementations:
- LocalFSBackend: a directory on disk that mimics an S3 bucket (key -> file path,
  "/" in keys becomes subdirectories). Zero setup, no credentials, no network.
  This is what quickstart.py uses so you can run the demo immediately after
  downloading, with no AWS account required.
- S3Backend: the real thing, via boto3. Swap in when you're ready to point at
  an actual bucket.

Both backends implement optimistic concurrency via ETags: put_bytes(if_match=...)
raises ConflictError if the object changed since you read it. This is what
protects entity markdown files from lost-update races when two writers touch
the same entity concurrently (see get_and_put_with_retry in entity_store.py).
"""

from __future__ import annotations

import hashlib
import os
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass


class ConflictError(Exception):
    """Raised when a conditional write's if_match ETag doesn't match current state."""


class NotFoundError(Exception):
    """Raised when get_bytes is called on a key that doesn't exist and required=True."""


@dataclass
class GetResult:
    data: bytes
    etag: str


class StorageBackend(ABC):
    @abstractmethod
    def get_bytes(self, key: str) -> GetResult | None:
        """Returns None if the key doesn't exist."""

    @abstractmethod
    def put_bytes(self, key: str, data: bytes, if_match: str | None = None) -> str:
        """
        Writes data to key. If if_match is provided, the write only succeeds if
        the object's current ETag equals if_match (optimistic concurrency).
        If if_match is provided and the key does NOT currently exist, the write fails
        (use if_match="" to mean "must not exist yet").
        Returns the new ETag.
        """

    @abstractmethod
    def delete(self, key: str) -> None:
        ...

    @abstractmethod
    def list_keys(self, prefix: str) -> list[str]:
        ...

    def get_many(self, keys: list[str]) -> dict[str, "GetResult | None"]:
        """Fetch several keys at once, returning {key: GetResult|None}.

        The default is a sequential loop, which is correct but is exactly the
        latency problem this exists to solve: over S3 every GET costs a full
        round-trip, so reading N objects one after another costs N * RTT of
        pure waiting. Backends that can issue requests concurrently should
        override this — MirageBackend does, via asyncio.gather on its
        existing event loop.

        Callers that know their reads are independent (BFS levels, whole-
        graph scans) should prefer this over calling get_bytes in a loop.
        """
        return {key: self.get_bytes(key) for key in keys}

    def exists(self, key: str) -> bool:
        return self.get_bytes(key) is not None


def _etag_for(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


class LocalFSBackend(StorageBackend):
    """
    TEST DOUBLE ONLY — not reachable from the running service.

    Storage for this service always goes through a mirage Workspace (see
    app/deps.py). app/config.py's "disk" mode mounts a mirage DiskResource
    for offline work, so even local development exercises MirageBackend.
    This class survives purely as a harness for tests that need to count
    bytes or inject faults at the storage layer, which is awkward to do
    through a Workspace. Do not wire it into deps.py.

    Simulates an S3 bucket using a local directory. Good enough to exercise the
    full read-modify-write + conditional-write code paths without any cloud
    dependency, which is the point: you can run quickstart.py the moment you
    download this, no AWS credentials, no network calls.
    """

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self._lock = threading.Lock()  # coarse-grained; fine for a demo backend

    def _path(self, key: str) -> str:
        # keys look like "graph/entities/alice-chen.md" -> nested dirs, like S3 prefixes
        safe = key.lstrip("/")
        return os.path.join(self.root, safe)

    def get_bytes(self, key: str) -> GetResult | None:
        path = self._path(key)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            data = f.read()
        return GetResult(data=data, etag=_etag_for(data))

    def put_bytes(self, key: str, data: bytes, if_match: str | None = None) -> str:
        path = self._path(key)
        with self._lock:
            current = self.get_bytes(key)
            if if_match is not None:
                current_etag = current.etag if current else ""
                if current_etag != if_match:
                    raise ConflictError(
                        f"ETag mismatch for {key}: expected {if_match!r}, found {current_etag!r}"
                    )
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = path + ".tmp"
            with open(tmp_path, "wb") as f:
                f.write(data)
            os.replace(tmp_path, path)  # atomic on POSIX
            return _etag_for(data)

    def delete(self, key: str) -> None:
        path = self._path(key)
        if os.path.exists(path):
            os.remove(path)

    def list_keys(self, prefix: str) -> list[str]:
        prefix_path = self._path(prefix)
        base_dir = prefix_path if os.path.isdir(prefix_path) else os.path.dirname(prefix_path)
        if not os.path.isdir(base_dir):
            return []
        out = []
        for dirpath, _dirnames, filenames in os.walk(base_dir):
            for fname in filenames:
                if fname.endswith(".tmp"):
                    continue
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                if rel.startswith(prefix.lstrip("/")):
                    out.append(rel)
        return sorted(out)


class S3Backend(StorageBackend):
    """
    Real S3 backend via boto3. Requires `pip install boto3` and either env vars
    (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION) or an
    attached IAM role.

    Conditional writes use S3's native If-Match support on PutObject. This
    requires a reasonably recent boto3/botocore (AWS added conditional writes
    to S3 in 2024) — check your version if put_bytes(if_match=...) raises
    an unexpected botocore.exceptions.ParamValidationError, and upgrade with
    `pip install -U boto3` if so.
    """

    def __init__(self, bucket: str, prefix: str = "", client=None):
        try:
            import boto3  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "S3Backend requires boto3. Install it with: pip install boto3"
            ) from e
        import boto3

        self.bucket = bucket
        self.prefix = prefix.rstrip("/")
        self.client = client or boto3.client("s3")

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def get_bytes(self, key: str) -> GetResult | None:
        from botocore.exceptions import ClientError

        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("NoSuchKey", "404"):
                return None
            if code in ("AccessDenied", "403"):
                # S3 returns 403 rather than 404 for a MISSING key when the
                # caller lacks s3:ListBucket, so as not to leak key existence.
                # This store asks "does this object exist?" on nearly every
                # code path, so without ListBucket a perfectly readable and
                # writable bucket fails on the very first upsert with an
                # opaque AccessDenied. Surfacing the cause here saves a long
                # debugging session.
                raise PermissionError(
                    f"S3 denied GetObject on s3://{self.bucket}/{self._key(key)}. "
                    "If the object genuinely does not exist, this is almost always a "
                    "missing s3:ListBucket permission on the BUCKET arn (not just "
                    "s3:GetObject on the object arn) — without it S3 returns 403 "
                    "instead of 404 for absent keys. See the IAM policy in README.md."
                ) from e
            raise
        data = resp["Body"].read()
        etag = resp["ETag"].strip('"')
        return GetResult(data=data, etag=etag)

    def put_bytes(self, key: str, data: bytes, if_match: str | None = None) -> str:
        from botocore.exceptions import ClientError

        kwargs = {"Bucket": self.bucket, "Key": self._key(key), "Body": data}
        if if_match is not None:
            # "" means "must not already exist" -> use IfNoneMatch: "*"
            if if_match == "":
                kwargs["IfNoneMatch"] = "*"
            else:
                kwargs["IfMatch"] = if_match
        try:
            resp = self.client.put_object(**kwargs)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("PreconditionFailed", "ConditionalRequestConflict"):
                raise ConflictError(str(e)) from e
            if code in ("NotImplemented", "InvalidRequest") and if_match is not None:
                # Some S3-compatible stores (older MinIO/Ceph builds, various
                # gateways) accept the request but reject the conditional
                # header. Every write in this store is a conditional write, so
                # this is fatal rather than degraded — say so plainly instead
                # of surfacing a bare NotImplemented.
                raise RuntimeError(
                    f"Storage endpoint rejected a conditional write (If-Match/If-None-Match) "
                    f"on s3://{self.bucket}/{self._key(key)}: {e}. This backend relies on "
                    "conditional writes for optimistic concurrency; real AWS S3 has supported "
                    "them since Nov 2024. Upgrade the endpoint or use a store that supports them."
                ) from e
            raise
        return resp["ETag"].strip('"')

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))

    def list_keys(self, prefix: str) -> list[str]:
        full_prefix = self._key(prefix)
        keys = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                k = obj["Key"]
                keys.append(k[len(self.prefix) + 1:] if self.prefix else k)
        return sorted(keys)
