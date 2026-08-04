"""
CRUD test script with timing, run against a LIVE server (start uvicorn first).

Usage:
    python scripts/crud_timing_test.py
    python scripts/crud_timing_test.py --url http://localhost:8000 --repeat 5

No extra dependencies — uses only the Python standard library (urllib).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field


@dataclass
class Timing:
    name: str
    samples_ms: list[float] = field(default_factory=list)
    ok: bool = True
    detail: str = ""

    @property
    def avg(self) -> float:
        return sum(self.samples_ms) / len(self.samples_ms) if self.samples_ms else 0.0

    @property
    def min(self) -> float:
        return min(self.samples_ms) if self.samples_ms else 0.0

    @property
    def max(self) -> float:
        return max(self.samples_ms) if self.samples_ms else 0.0


class ApiClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        # Force NO proxy for these requests, regardless of Windows system proxy
        # settings or HTTP_PROXY/HTTPS_PROXY env vars. urllib.request honors
        # system proxy config by default and — unlike most HTTP clients — does
        # NOT automatically bypass it for 127.0.0.1/localhost. On a machine with
        # any proxy configured (corporate VPN, security software, etc.), that
        # can mean requests to your own local server get silently routed through
        # the proxy and hang, even though the exact same request via
        # Invoke-RestMethod or curl works instantly. This opener sidesteps that
        # entirely, since this script should never need a proxy to reach a
        # server you passed in directly via --url.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, path: str, json_body: dict | None = None):
        url = f"{self.base_url}{path}"
        data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")

        start = time.perf_counter()
        try:
            # 120s, not 30s — a slow storage backend (e.g. mirage over a
            # high-latency network) can take longer than 30s per write; this
            # was previously throwing a spurious timeout on writes that were
            # actually still succeeding server-side, just slowly.
            with self._opener.open(req, timeout=120) as resp:
                status = resp.status
                body = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            body = e.read()
        elapsed_ms = (time.perf_counter() - start) * 1000

        parsed = None
        if body:
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = body.decode("utf-8", errors="replace")
        return status, parsed, elapsed_ms


def time_operation(timings: list[Timing], name: str, repeat: int, fn):
    print(f"  running: {name} ...", flush=True)
    t = Timing(name=name)
    result = None
    for _ in range(repeat):
        start = time.perf_counter()
        try:
            result = fn()
        except AssertionError as e:
            t.ok = False
            t.detail = str(e)
            elapsed_ms = (time.perf_counter() - start) * 1000
            t.samples_ms.append(elapsed_ms)
            timings.append(t)
            return None
        elapsed_ms = (time.perf_counter() - start) * 1000
        t.samples_ms.append(elapsed_ms)
    timings.append(t)
    return result


def run_crud_suite(base_url: str, repeat: int) -> list[Timing]:
    client = ApiClient(base_url)
    user_id = "u_crud_timing_test"
    timings: list[Timing] = []
    p = lambda s: urllib.parse.quote(s)  # noqa: E731 — path-segment escaping (e.g. spaces)

    def create_alice():
        status, body, ms = client.request(
            "PUT", f"/v1/users/{user_id}/wiki", {"type": "person", "title": "Alice"}
        )
        assert status == 200, f"expected 200, got {status}: {body}"
        assert body.get("wiki_id") == "person/alice", f"unexpected body: {body}"
        return status, body

    def create_orion():
        status, body, ms = client.request(
            "PUT", f"/v1/users/{user_id}/wiki", {"type": "project", "title": "Project Orion"}
        )
        assert status == 200, f"expected 200, got {status}: {body}"
        return status, body

    def read_alice():
        status, body, ms = client.request("GET", f"/v1/users/{user_id}/wiki/person/{p('Alice')}")
        assert status == 200, f"expected 200, got {status}: {body}"
        assert body.get("wiki_id") == "person/alice", f"unexpected body: {body}"
        return status, body

    def update_alice():
        status, body, ms = client.request(
            "PUT", f"/v1/users/{user_id}/wiki",
            {"type": "person", "title": "Alice", "summary_append": "Works remotely."},
        )
        assert status == 200, f"expected 200, got {status}: {body}"
        return status, body

    def add_fact():
        status, body, ms = client.request(
            "POST", f"/v1/users/{user_id}/wiki/person/{p('Alice')}/facts",
            {"text": "Works on Project Orion.", "confidence": 0.9},
        )
        assert status == 200, f"expected 200, got {status}: {body}"
        return status, body

    def update_fact():
        status, body, ms = client.request(
            "PATCH", f"/v1/users/{user_id}/wiki/person/{p('Alice')}/facts/fact_0001",
            {"text": "Works on Project Orion (updated).", "confidence": 0.99},
        )
        assert status == 200, f"expected 200, got {status}: {body}"
        return status, body

    def link_alice_orion():
        status, body, ms = client.request(
            "POST", f"/v1/users/{user_id}/wiki/person/{p('Alice')}/relations",
            {"target_wiki_id": "project/project-orion", "category": "related_to",
             "label": "works_on", "reason": "works on"},
        )
        assert status == 200, f"expected 200, got {status}: {body}"
        return status, body

    def update_relation():
        status, body, ms = client.request(
            "PATCH", f"/v1/users/{user_id}/wiki/person/{p('Alice')}/relations/rel_0001",
            {"label": "leads", "weight": 1.0},
        )
        assert status == 200, f"expected 200, got {status}: {body}"
        return status, body

    def read_relations():
        status, body, ms = client.request(
            "GET", f"/v1/users/{user_id}/wiki/person/{p('Alice')}/relations"
        )
        assert status == 200, f"expected 200, got {status}: {body}"
        assert isinstance(body, list) and len(body) >= 1, f"expected relations, got {body}"
        return status, body

    def traverse():
        status, body, ms = client.request(
            "POST", f"/v1/users/{user_id}/wiki/traverse",
            {"entry_wiki_ids": ["person/alice"], "max_depth": 2},
        )
        assert status == 200, f"expected 200, got {status}: {body}"
        assert "person/alice" in body.get("wiki_ids", []), f"unexpected body: {body}"
        return status, body

    def list_entities():
        status, body, ms = client.request("GET", f"/v1/users/{user_id}/wiki")
        assert status == 200, f"expected 200, got {status}: {body}"
        return status, body

    def graph_stats():
        status, body, ms = client.request("GET", f"/v1/users/{user_id}/wiki/_stats")
        assert status == 200, f"expected 200, got {status}: {body}"
        return status, body

    def append_raw_fact():
        status, body, ms = client.request(
            "POST", f"/v1/users/{user_id}/raw-facts",
            {"facts": [{"fact": "Alice joined Orion"}]},
        )
        assert status == 201, f"expected 201, got {status}: {body}"
        return status, body

    def read_raw_facts():
        import datetime
        today = datetime.date.today().isoformat()
        status, body, ms = client.request("GET", f"/v1/users/{user_id}/raw-facts?on={today}")
        assert status == 200, f"expected 200, got {status}: {body}"
        assert body.get("count", 0) >= 1, f"expected at least 1 record, got {body}"
        return status, body

    def remove_relation():
        status, body, ms = client.request(
            "DELETE", f"/v1/users/{user_id}/wiki/person/{p('Alice')}/relations/rel_0001"
        )
        assert status == 200, f"expected 200, got {status}: {body}"
        return status, body

    def remove_fact():
        status, body, ms = client.request(
            "DELETE", f"/v1/users/{user_id}/wiki/person/{p('Alice')}/facts/fact_0001"
        )
        assert status == 200, f"expected 200, got {status}: {body}"
        return status, body

    def delete_orion():
        status, body, ms = client.request(
            "DELETE", f"/v1/users/{user_id}/wiki/project/{p('Project Orion')}"
        )
        assert status == 204, f"expected 204, got {status}: {body}"
        return status, body

    def delete_alice():
        status, body, ms = client.request("DELETE", f"/v1/users/{user_id}/wiki/person/{p('Alice')}")
        assert status == 204, f"expected 204, got {status}: {body}"
        return status, body

    def delete_alice_again_should_404():
        status, body, ms = client.request("DELETE", f"/v1/users/{user_id}/wiki/person/{p('Alice')}")
        assert status == 404, f"expected 404 on double-delete, got {status}: {body}"
        return status, body

    time_operation(timings, "CREATE entity (person/Alice)", 1, create_alice)
    time_operation(timings, "CREATE entity (project/Orion)", 1, create_orion)
    time_operation(timings, "READ entity (Alice)", repeat, read_alice)
    time_operation(timings, "UPDATE entity (Alice summary)", repeat, update_alice)
    time_operation(timings, "ADD fact (Alice)", 1, add_fact)
    time_operation(timings, "UPDATE fact (Alice)", repeat, update_fact)
    time_operation(timings, "CREATE relation (Alice->Orion)", 1, link_alice_orion)
    time_operation(timings, "UPDATE relation (Alice->Orion)", repeat, update_relation)
    time_operation(timings, "READ relations (Alice)", repeat, read_relations)
    time_operation(timings, "TRAVERSE graph (from Alice)", repeat, traverse)
    time_operation(timings, "LIST entities (manifest)", repeat, list_entities)
    time_operation(timings, "READ graph stats", repeat, graph_stats)
    time_operation(timings, "CREATE raw fact", 1, append_raw_fact)
    time_operation(timings, "READ raw facts (today)", repeat, read_raw_facts)
    time_operation(timings, "DELETE relation (Alice->Orion)", 1, remove_relation)
    time_operation(timings, "DELETE fact (Alice)", 1, remove_fact)
    time_operation(timings, "DELETE entity (Orion)", 1, delete_orion)
    time_operation(timings, "DELETE entity (Alice)", 1, delete_alice)
    time_operation(timings, "DELETE entity again -> 404", 1, delete_alice_again_should_404)

    return timings


def print_report(timings: list[Timing]) -> bool:
    name_w = max(len(t.name) for t in timings) + 2
    header = f"{'OPERATION':<{name_w}}{'STATUS':<8}{'AVG ms':>10}{'MIN ms':>10}{'MAX ms':>10}{'N':>5}"
    print(header)
    print("-" * len(header))

    all_ok = True
    for t in timings:
        status_str = "PASS" if t.ok else "FAIL"
        if not t.ok:
            all_ok = False
        print(
            f"{t.name:<{name_w}}{status_str:<8}{t.avg:>10.2f}{t.min:>10.2f}{t.max:>10.2f}{len(t.samples_ms):>5}"
        )
        if not t.ok:
            print(f"  -> {t.detail}")

    total_ms = sum(t.avg * len(t.samples_ms) for t in timings)
    print("-" * len(header))
    print(f"Total time across all requests: {total_ms:.2f} ms")
    print(f"Result: {'ALL PASSED' if all_ok else 'SOME FAILED'}")
    return all_ok


def main():
    parser = argparse.ArgumentParser(description="CRUD + timing test against a live memory_backend server")
    parser.add_argument("--url", default="http://127.0.0.1:8000",
                         help="Base URL of the running server. Prefer 127.0.0.1 over "
                              "localhost on Windows — localhost can trigger a slow "
                              "IPv6-then-IPv4 fallback that adds ~2s per request.")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()

    print(f"Running CRUD timing test against {args.url} (repeat={args.repeat})\n")
    try:
        timings = run_crud_suite(args.url, args.repeat)
    except urllib.error.URLError as e:
        print(f"Could not reach server at {args.url}: {e}")
        print("Is uvicorn running? e.g. `uvicorn app.main:app --reload --reload-dir app --host 0.0.0.0 --port 8000`")
        sys.exit(2)

    ok = print_report(timings)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
