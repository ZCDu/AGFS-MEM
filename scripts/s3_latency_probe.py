"""
Time each S3 primitive separately, to find which one is slow.

    python scripts/s3_latency_probe.py
    python scripts/s3_latency_probe.py --samples 5
    python scripts/s3_latency_probe.py --both-path-styles

Why this exists: diagnose_mirage.py proves the connection WORKS, and
bench.ps1 shows the app is slow, but neither says which operation is
responsible. A single upsert_entity is 5 mirage ops (2 read, 3 write) plus a
directory listing on the first call. If one of those types costs seconds and
the others are fast, only a per-primitive breakdown will show it.

The listing is the prime suspect on non-AWS S3 gateways. GET and PUT address
one key; LIST scans a prefix, is often implemented very differently, and on
some gateways is orders of magnitude slower. It is also the operation
MIRAGE_INDEX_TTL_SECONDS=0 stops us from caching.

--both-path-styles matters for S3-compatible services specifically.
Virtual-hosted addressing (path_style=false) puts the bucket in the
hostname, which needs wildcard DNS for *.endpoint. When that resolves
slowly, or fails and falls back, every request pays a fixed multi-second
penalty that looks exactly like a slow network. Path-style addressing keeps
the bucket in the URL path and avoids the question entirely.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True))
    load_dotenv()
except ImportError:
    pass

from app.storage.mirage_backend import MirageBackend  # noqa: E402

PREFIX = "_latency_probe"


def timed(fn, samples: int) -> tuple[float, float, float]:
    """Returns (min, median, max) in ms."""
    out = []
    for i in range(samples):
        t0 = time.perf_counter()
        fn(i)
        out.append((time.perf_counter() - t0) * 1000)
    return min(out), statistics.median(out), max(out)


def probe(path_style: bool, samples: int) -> None:
    bucket = os.environ.get("MIRAGE_S3_BUCKET")
    if not bucket:
        print("MIRAGE_S3_BUCKET is not set — nothing to probe.")
        sys.exit(1)

    print(f"\n  path_style = {path_style}")
    print(f"  endpoint   = {os.environ.get('MIRAGE_S3_ENDPOINT_URL')}")

    t0 = time.perf_counter()
    backend = MirageBackend.from_s3_config(
        bucket=bucket,
        region=os.environ.get("MIRAGE_S3_REGION"),
        endpoint_url=os.environ.get("MIRAGE_S3_ENDPOINT_URL"),
        aws_access_key_id=os.environ.get("MIRAGE_S3_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("MIRAGE_S3_SECRET_ACCESS_KEY"),
        aws_session_token=os.environ.get("MIRAGE_S3_SESSION_TOKEN"),
        aws_profile=os.environ.get("MIRAGE_S3_PROFILE"),
        path_style=path_style,
        key_prefix=os.environ.get("MIRAGE_S3_KEY_PREFIX"),
    )
    print(f"  construct  : {(time.perf_counter() - t0) * 1000:,.0f} ms")

    payload = b"x" * 512
    try:
        rows = []

        rows.append(("PUT  (new key)", timed(
            lambda i: backend.put_bytes(f"{PREFIX}/k{i}.txt", payload), samples)))

        rows.append(("GET  (existing)", timed(
            lambda i: backend.get_bytes(f"{PREFIX}/k{i}.txt"), samples)))

        rows.append(("GET  (missing)", timed(
            lambda i: backend.get_bytes(f"{PREFIX}/absent{i}.txt"), samples)))

        # The conditional write is what upsert_entity actually uses. On
        # mirage it costs a full extra read, because there is no
        # compare-and-swap primitive to lean on.
        rows.append(("PUT  (if_match)", timed(
            lambda i: backend.put_bytes(
                f"{PREFIX}/k{i}.txt", payload,
                if_match=backend.get_bytes(f"{PREFIX}/k{i}.txt").etag), samples)))

        rows.append(("LIST (populated)", timed(
            lambda i: backend.list_keys(f"{PREFIX}/"), samples)))

        rows.append(("LIST (empty path)", timed(
            lambda i: backend.list_keys(f"{PREFIX}/nothing-here-{i}/"), samples)))

        rows.append(("DELETE", timed(
            lambda i: backend.delete(f"{PREFIX}/k{i}.txt"), samples)))

        print()
        print(f"  {'OPERATION':<20}{'min ms':>10}{'median ms':>12}{'max ms':>10}")
        print("  " + "-" * 52)
        for name, (lo, mid, hi) in rows:
            flag = "   <-- SLOW" if mid > 1000 else ""
            print(f"  {name:<20}{lo:>10,.0f}{mid:>12,.0f}{hi:>10,.0f}{flag}")

        medians = {n: mid for n, (_, mid, _) in rows}
        gets = [v for k, v in medians.items() if k.startswith("GET")]
        puts = [v for k, v in medians.items() if k.startswith("PUT")]
        base = min(gets) if gets else 0

        print()
        if base and base < 150:
            # A healthy profile: point reads near one RTT, writes a small
            # multiple of it. Flag the ratio rather than the absolute number.
            print(f"  Baseline RTT looks healthy (~{base:,.0f} ms per round-trip).")
            if puts and base and (min(puts) / base) > 2.5:
                print(f"  Writes cost {min(puts) / base:.1f}x reads, which is normal for object")
                print("  storage — durability confirmation. Reduce the NUMBER of writes")
                print("  rather than trying to make each one faster:")
                print("    FLUSH_INTERVAL_SECONDS=30   (amortise manifest + ops-log writes)")
                print("    FLUSH_MAX_PENDING=200")
            print("  If throughput is still short, the remaining lever is round-trip")
            print("  COUNT per request, not per-op latency.")
            return

        slow = [n for n, (_, mid, _) in rows if mid > 1000]
        if slow:
            print(f"  Dominant cost: {', '.join(slow)}")
            if any("LIST" in s for s in slow):
                print("  LIST is slow. This gateway handles prefix scans far worse than")
                print("  point reads. Mitigations, in order:")
                print("    1. Raise MIRAGE_INDEX_TTL_SECONDS so listings are cached — but")
                print("       read the correctness warning in the README first; it is only")
                print("       safe with a single writer process.")
                print("    2. Reduce listing frequency: raise COMPACT_MAX_DELTAS and")
                print("       COMPACT_RATIO in app/graph/manifest.py so compaction (which")
                print("       lists) runs less often.")
        else:
            print("  No single primitive dominates; cost is spread evenly across all")
            print("  operation types regardless of payload. That is the signature of")
            print("  per-request connection setup (DNS + TCP + TLS), not of any one")
            print("  operation. Check MIRAGE_REUSE_CONNECTIONS is not disabled.")

    finally:
        for i in range(samples):
            try:
                backend.delete(f"{PREFIX}/k{i}.txt")
            except Exception:
                pass
        backend.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--both-path-styles", action="store_true",
                    help="probe with path_style false AND true, to compare")
    args = ap.parse_args()

    if args.both_path_styles:
        for ps in (False, True):
            probe(ps, args.samples)
        print("\n  If path_style=True is dramatically faster, set")
        print("  MIRAGE_S3_PATH_STYLE=true in .env. Virtual-hosted addressing")
        print("  needs wildcard DNS for the bucket subdomain, which many")
        print("  S3-compatible gateways do not provide cleanly.\n")
    else:
        current = os.environ.get("MIRAGE_S3_PATH_STYLE", "false").lower() in ("1", "true", "yes")
        probe(current, args.samples)


if __name__ == "__main__":
    main()
