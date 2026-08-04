"""
Measures the object-store write cost of the graph store, before/after the
write-behind changes.

Run:  python scripts/cost_benchmark.py [--n 400]

Counts calls the STORE layer makes to the backend. It deliberately does not
count LocalFSBackend.put_bytes()'s internal ETag read, because real S3 does
that check server-side in the same round-trip — counting it would overstate
GETs by ~2x and make the baseline look worse than it is.

The `sync` column is the original WRITE MODES, not the original code: two
other fixes apply in both columns, so sync already reads better than the
code this replaced. Specifically, upsert no longer does a redundant pre-read
(original was 4 GET + 3 PUT, sync here shows 2 GET + 3 PUT), and the ops log
uses segment writes even in sync mode instead of rewriting the whole day
file. The honest read of this table is therefore "what buffering alone
buys", and the true saving against the original is larger.

The bytes-written row is where the original design loses badly: it was
quadratic in entity count, so its disadvantage compounds with scale.
"""

from __future__ import annotations

import argparse
import collections
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.config as cfg  # noqa: E402
from app.graph.store import EntityGraphStore  # noqa: E402
from app.storage.backend import LocalFSBackend  # noqa: E402

# S3 Standard, us-east-1. A PUT costs 12.5x a GET, which is why PUT count
# dominates the bill even though GETs dominate the call count.
GET_COST = 0.0004 / 1000
PUT_COST = 0.005 / 1000


class CountingBackend(LocalFSBackend):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.ops = collections.Counter()
        self.bytes_out = 0
        self._inside_put = False

    def get_bytes(self, key):
        if not self._inside_put:
            self.ops["GET"] += 1
        return super().get_bytes(key)

    def put_bytes(self, key, data, if_match=None):
        self.ops["PUT"] += 1
        self.bytes_out += len(data)
        self._inside_put = True
        try:
            return super().put_bytes(key, data, if_match)
        finally:
            self._inside_put = False

    def delete(self, key):
        self.ops["DELETE"] += 1
        return super().delete(key)

    def list_keys(self, prefix):
        self.ops["LIST"] += 1
        return super().list_keys(prefix)


def build(tmp: str, mode: str):
    import os
    for var, val in {
        "MANIFEST_WRITE_MODE": mode,
        "OPS_LOG_WRITE_MODE": mode,
        "FLUSH_INTERVAL_SECONDS": "3600",
        "FLUSH_MAX_PENDING": "100",
    }.items():
        os.environ[var] = val
    cfg._settings = None
    backend = CountingBackend(root=tempfile.mkdtemp(dir=tmp))
    return backend, EntityGraphStore(backend)


def run(tmp: str, mode: str, n: int) -> dict:
    backend, store = build(tmp, mode)

    for i in range(n):
        store.upsert_entity("u", "concept", f"C{i}")
    store.flush()
    create = dict(backend.ops)
    create_bytes = backend.bytes_out

    backend.ops.clear()
    for i in range(n):
        store.get_entity("u", f"concept/c{i}", touch=True)
    store.flush()
    read = dict(backend.ops)

    backend.ops.clear()
    for i in range(n):
        store.add_fact("u", f"concept/c{i}", "a fact")
    store.flush()
    fact = dict(backend.ops)

    return {
        "create": create, "create_bytes": create_bytes,
        "read": read, "fact": fact,
    }


def per_op(d: dict, n: int) -> str:
    return f"{d.get('GET', 0)/n:.2f} GET + {d.get('PUT', 0)/n:.2f} PUT"


def bill(d: dict, n: int, ops: float) -> float:
    return (d.get("GET", 0) / n * ops * GET_COST) + (d.get("PUT", 0) / n * ops * PUT_COST)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=400, help="entities to exercise")
    args = ap.parse_args()
    n = args.n

    tmp = tempfile.mkdtemp()
    try:
        sync = run(tmp, "sync", n)
        buf = run(tmp, "buffered", n)

        print(f"\n  n = {n} entities\n")
        print(f"  {'operation':<22}{'sync (original)':<26}{'buffered (default)':<26}")
        print(f"  {'-'*72}")
        for label, key in (("upsert_entity", "create"), ("get_entity(touch)", "read"),
                            ("add_fact", "fact")):
            print(f"  {label:<22}{per_op(sync[key], n):<26}{per_op(buf[key], n):<26}")

        print(f"\n  {'bytes written (creates)':<22}{sync['create_bytes']/1e6:>10.2f} MB"
              f"{buf['create_bytes']/1e6:>22.2f} MB"
              f"   {sync['create_bytes']/max(buf['create_bytes'],1):>6.1f}x less")

        print("\n  S3 request bill, 1M upserts + 5M reads + 2M add_fact:")
        s = bill(sync["create"], n, 1e6) + bill(sync["read"], n, 5e6) + bill(sync["fact"], n, 2e6)
        b = bill(buf["create"], n, 1e6) + bill(buf["read"], n, 5e6) + bill(buf["fact"], n, 2e6)
        print(f"    sync     ${s:>10,.2f}")
        print(f"    buffered ${b:>10,.2f}   ({s/max(b, 1e-9):.1f}x cheaper)\n")

        print("  Note: bytes-written reduction GROWS with n, because the original")
        print("  cost was quadratic and the new one is linear. Re-run with a larger")
        print("  --n to see the gap widen.\n")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
