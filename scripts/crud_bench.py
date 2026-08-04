"""
Run a full CRUD cycle N times and report where the time actually goes.

    python scripts/crud_bench.py --repeat 20
    python scripts/crud_bench.py --repeat 50 --user bench --url http://127.0.0.1:8000

Reports three numbers per operation, and they answer different questions:

  wall    Round-trip measured by the client. What a caller experiences.
  server  Taken from the X-Process-Time-Ms header the app sets in its
          timing middleware. Time spent inside FastAPI, including all
          storage I/O.
  overhead  wall - server. Network/loopback plus client-side cost.

Splitting them matters because the fixes are opposite. High `server` means
storage round-trips — the thing the write-behind and log-structured work
targets. High `overhead` on a loopback connection means the client is the
bottleneck, not the service, and tuning the backend will achieve nothing.

Also reports p50/p95, because averages hide the tail that the PLAN.md §13
latency targets are actually written against (p50 50ms, p95 200ms,
p99 500ms).

Note the DELETE step uses hard_delete=true so each iteration starts clean;
otherwise iteration 2 would be updating a tombstone rather than creating.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request

OPS = ["CREATE", "READ", "UPDATE (add fact)", "LIST", "TRAVERSE", "DELETE"]


def call(method: str, url: str, body=None) -> tuple[float, float, int]:
    """Return (wall_ms, server_ms, status)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"content-type": "application/json"} if data else {},
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
            status = resp.status
            server = float(resp.headers.get("X-Process-Time-Ms", "nan"))
    except urllib.error.HTTPError as e:
        e.read()
        status = e.code
        server = float(e.headers.get("X-Process-Time-Ms", "nan"))
    wall = (time.perf_counter() - start) * 1000
    return wall, server, status


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000",
                    help="Prefer 127.0.0.1 over localhost on Windows — localhost can "
                         "trigger a slow IPv6-then-IPv4 fallback that dwarfs the real timing.")
    ap.add_argument("--user", default="bench")
    ap.add_argument("--repeat", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=2,
                    help="Discarded iterations. The first calls pay for lazy imports, "
                         "connection setup and a cold manifest cache, which would "
                         "otherwise skew the mean badly at low --repeat.")
    args = ap.parse_args()

    base = f"{args.url.rstrip('/')}/v1/users/{args.user}"
    wall: dict[str, list[float]] = {o: [] for o in OPS}
    server: dict[str, list[float]] = {o: [] for o in OPS}
    failures = 0

    total_start = time.perf_counter()

    for i in range(args.repeat + args.warmup):
        record = i >= args.warmup
        title = f"Bench Subject {i}"
        slug = f"person/bench-subject-{i}"

        steps = [
            ("CREATE", "PUT", f"{base}/wiki",
             {"type": "person", "title": title, "summary_append": "Benchmark entity."}),
            ("READ", "GET", f"{base}/{'wiki/' + slug}", None),
            ("UPDATE (add fact)", "POST", f"{base}/wiki/{slug}/facts",
             {"text": "A benchmark fact.", "confidence": 0.9}),
            ("LIST", "GET", f"{base}/wiki", None),
            ("TRAVERSE", "POST", f"{base}/wiki/traverse",
             {"entry_wiki_ids": [slug], "max_depth": 2}),
            ("DELETE", "DELETE", f"{base}/wiki/{slug}?hard_delete=true&cascade=true", None),
        ]

        for name, method, url, body in steps:
            w, s, status = call(method, url, body)
            if status >= 400:
                failures += 1
            if record:
                wall[name].append(w)
                server[name].append(s)

    total_elapsed = (time.perf_counter() - total_start) * 1000

    print(f"\n  {args.repeat} iterations x {len(OPS)} operations = "
          f"{args.repeat * len(OPS)} requests   ({args.warmup} warmup discarded)")
    print(f"  target: {base}")
    if failures:
        print(f"  WARNING: {failures} request(s) returned >=400")

    hdr = f"  {'operation':<20}{'wall avg':>10}{'server avg':>12}{'overhead':>10}{'p50':>9}{'p95':>9}"
    print("\n" + hdr)
    print("  " + "-" * (len(hdr) - 2))

    sum_wall = sum_server = 0.0
    for op in OPS:
        w, s = wall[op], server[op]
        if not w:
            continue
        aw, asv = statistics.mean(w), statistics.mean(s)
        sum_wall += aw
        sum_server += asv
        p50 = statistics.median(w)
        p95 = sorted(w)[max(0, int(len(w) * 0.95) - 1)]
        print(f"  {op:<20}{aw:>9.1f}ms{asv:>11.1f}ms{aw - asv:>9.1f}ms{p50:>8.1f}ms{p95:>8.1f}ms")

    print("  " + "-" * (len(hdr) - 2))
    print(f"  {'FULL CYCLE':<20}{sum_wall:>9.1f}ms{sum_server:>11.1f}ms{sum_wall - sum_server:>9.1f}ms")
    print(f"\n  total wall clock for the whole run: {total_elapsed:.0f}ms")
    print(f"  average per full CRUD cycle:        {sum_wall:.1f}ms")
    print(f"  server share:                       {sum_server / sum_wall * 100:.0f}%\n")


if __name__ == "__main__":
    main()
