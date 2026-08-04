"""
Exercise every CRUD operation against a running server and write a markdown
report of what was created, updated, read and deleted.

    python scripts/crud_report.py
    python scripts/crud_report.py --user crudtest --out report.md
    python scripts/crud_report.py --url http://127.0.0.1:8000 --keep

Uses only the standard library, so it needs no dependencies beyond Python
itself, and it talks to the API over HTTP rather than importing the app —
meaning it tests what a real client would actually get, including routing,
serialisation and the buffered-write timing.

Writes to a throwaway user (default "crudreport") and cleans up afterwards
unless --keep is passed, so it will not disturb real data.

The report records the FULL STATE of each memory at each step, not just the
status codes, because "the request returned 200" and "the memory now says
what I expected" are different claims and only the second one matters.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

STEPS: list[dict] = []

# Bearer token, set from --token. Module-level because call() is used from many
# helpers and threading it through every signature would be noise.
TOKEN: str | None = None


def call(base: str, method: str, path: str, body: dict | None = None):
    """Returns (status, parsed_json_or_text, elapsed_ms)."""
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"} if data else {}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    except Exception as e:
        return -1, {"error": f"{type(e).__name__}: {e}"}, (time.perf_counter() - t0) * 1000
    elapsed = (time.perf_counter() - t0) * 1000
    try:
        return status, (json.loads(raw) if raw else None), elapsed
    except json.JSONDecodeError:
        return status, raw.decode("utf-8", "replace"), elapsed


def step(base: str, phase: str, label: str, method: str, path: str,
         body: dict | None = None, expect: tuple[int, ...] = (200, 201, 204)):
    status, payload, ms = call(base, method, path, body)
    STEPS.append({
        "phase": phase, "label": label, "method": method, "path": path,
        "request": body, "status": status, "response": payload,
        "ms": round(ms, 1), "ok": status in expect,
    })
    return payload


def entity_block(e: dict | None) -> str:
    """Render an entity the way a human would want to verify it."""
    if not isinstance(e, dict) or "wiki_id" not in e:
        return "```\n(no entity returned)\n```"
    lines = [
        f"- **wiki_id**: `{e['wiki_id']}`",
        f"- **title**: {e.get('title')}",
        f"- **type**: {e.get('type')}",
    ]
    if e.get("aliases"):
        lines.append(f"- **aliases**: {', '.join(e['aliases'])}")
    if e.get("summary"):
        lines.append(f"- **summary**: {e['summary']}")
    md = e.get("metadata") or {}
    if md:
        lines.append(f"- **significance**: {md.get('significance')} · "
                     f"**decay**: {round(e.get('decay_score', 0), 4)}")
    if e.get("facts"):
        lines.append("- **facts**:")
        for f in e["facts"]:
            lines.append(f"    - `{f['fact_id']}` {f['text']} "
                         f"_(confidence {f['confidence']})_")
    else:
        lines.append("- **facts**: none")
    if e.get("relations"):
        lines.append("- **relations**:")
        for r in e["relations"]:
            lines.append(f"    - `{r['relation_id']}` --{r['label'] or r['category']}--> "
                         f"`{r['target']}` _(category {r['category']}, weight {r['weight']})_")
    else:
        lines.append("- **relations**: none")
    return "\n".join(lines)


def run(base: str, user: str, keep: bool) -> None:
    root = f"{base}/v1/users/{user}"

    # ---------- CREATE ----------
    alice = step(base, "CREATE", "Create entity: person", "PUT", f"/v1/users/{user}/wiki",
                 {"type": "person", "title": "Rosa Marchetti", "aliases": ["Rosa"],
                  "summary_append": "Marine biologist studying coral bleaching."})
    step(base, "CREATE", "Create entity: project", "PUT", f"/v1/users/{user}/wiki",
         {"type": "project", "title": "Reef Atlas",
          "summary_append": "Mapping bleaching events across the Coral Triangle."})
    step(base, "CREATE", "Create entity: organization", "PUT", f"/v1/users/{user}/wiki",
         {"type": "organization", "title": "Blue Horizon Institute",
          "summary_append": "Independent marine research institute."})

    with_fact = step(base, "CREATE", "Add fact to person", "POST",
                     f"/v1/users/{user}/wiki/person/rosa-marchetti/facts",
                     {"text": "Leads the Reef Atlas survey team.", "confidence": 0.95,
                      "evidence": ["field-notes-2026"]})
    fact_id = (with_fact or {}).get("facts", [{}])[0].get("fact_id")

    with_rel = step(base, "CREATE", "Link person to project", "POST",
                    f"/v1/users/{user}/wiki/person/rosa-marchetti/relations",
                    {"target_wiki_id": "project/reef-atlas", "category": "related_to",
                     "label": "leads", "weight": 1.0})
    rel_id = (with_rel or {}).get("relations", [{}])[0].get("relation_id")

    step(base, "CREATE", "Link project to organization", "POST",
         f"/v1/users/{user}/wiki/project/reef-atlas/relations",
         {"target_wiki_id": "organization/blue-horizon-institute",
          "category": "related_to", "label": "funded_by"})

    step(base, "CREATE", "Append raw conversation fact", "POST",
         f"/v1/users/{user}/raw-facts",
         {"facts": [{"text": "Rosa mentioned a new bleaching event at Tubbataha.",
                     "source": "standup"}]}, expect=(201,))

    # ---------- READ (before updates) ----------
    step(base, "READ", "Read entity before update", "GET",
         f"/v1/users/{user}/wiki/person/rosa-marchetti")

    # ---------- UPDATE ----------
    step(base, "UPDATE", "Append to summary, raise significance", "PUT",
         f"/v1/users/{user}/wiki",
         {"type": "person", "title": "Rosa Marchetti",
          "summary_append": "Promoted to survey lead in 2026.", "significance": 0.92})
    if fact_id:
        step(base, "UPDATE", "Revise fact text and confidence", "PATCH",
             f"/v1/users/{user}/wiki/person/rosa-marchetti/facts/{fact_id}",
             {"text": "Leads the Reef Atlas survey team across three sites.",
              "confidence": 0.99})
    if rel_id:
        step(base, "UPDATE", "Relabel and reweight relation", "PATCH",
             f"/v1/users/{user}/wiki/person/rosa-marchetti/relations/{rel_id}",
             {"label": "principal_investigator", "weight": 0.95,
              "reason": "Formalised after promotion."})

    # ---------- READ (after updates) ----------
    step(base, "READ", "Read entity after update", "GET",
         f"/v1/users/{user}/wiki/person/rosa-marchetti")
    step(base, "READ", "List all entities", "GET", f"/v1/users/{user}/wiki")
    step(base, "READ", "List only persons", "GET", f"/v1/users/{user}/wiki?type=person")
    step(base, "READ", "Read relations of person", "GET",
         f"/v1/users/{user}/wiki/person/rosa-marchetti/relations")
    step(base, "READ", "Traverse depth 1", "POST", f"/v1/users/{user}/wiki/traverse",
         {"entry_wiki_ids": ["person/rosa-marchetti"], "max_depth": 1})
    step(base, "READ", "Traverse depth 2", "POST", f"/v1/users/{user}/wiki/traverse",
         {"entry_wiki_ids": ["person/rosa-marchetti"], "max_depth": 2})
    step(base, "READ", "Graph stats", "GET", f"/v1/users/{user}/wiki/_stats")
    step(base, "READ", "Assess a redundant statement", "POST", f"/v1/users/{user}/assess",
         {"text": "Rosa Marchetti is a marine biologist studying coral bleaching."})
    step(base, "READ", "Assess a novel statement", "POST", f"/v1/users/{user}/assess",
         {"text": "We decided to extend Reef Atlas to Tubbataha after the 2026 bleaching "
                  "event. Rosa will lead the survey and the deadline is 2026-11-30."})

    # ---------- DELETE ----------
    if fact_id:
        step(base, "DELETE", "Remove fact", "DELETE",
             f"/v1/users/{user}/wiki/person/rosa-marchetti/facts/{fact_id}")
    if rel_id:
        step(base, "DELETE", "Remove relation", "DELETE",
             f"/v1/users/{user}/wiki/person/rosa-marchetti/relations/{rel_id}")
    step(base, "DELETE", "Soft delete organization (tombstone)", "DELETE",
         f"/v1/users/{user}/wiki/organization/blue-horizon-institute?hard_delete=false",
         expect=(204,))
    step(base, "DELETE", "Confirm tombstone is unreadable", "GET",
         f"/v1/users/{user}/wiki/organization/blue-horizon-institute", expect=(404,))
    if not keep:
        step(base, "DELETE", "Hard delete project (cascade)", "DELETE",
             f"/v1/users/{user}/wiki/project/reef-atlas?hard_delete=true&cascade=true",
             expect=(204,))
        step(base, "DELETE", "Hard delete person (cascade)", "DELETE",
             f"/v1/users/{user}/wiki/person/rosa-marchetti?hard_delete=true&cascade=true",
             expect=(204,))

    # Buffered index writes need a moment to land before the final read.
    time.sleep(3)
    step(base, "READ", "Final graph stats", "GET", f"/v1/users/{user}/wiki/_stats")
    step(base, "READ", "Final entity list", "GET", f"/v1/users/{user}/wiki")


def report(base: str, user: str, health) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    passed = sum(1 for s in STEPS if s["ok"])
    failed = [s for s in STEPS if not s["ok"]]
    total_ms = sum(s["ms"] for s in STEPS)

    out = [
        "# CRUD Test Report",
        "",
        f"- **Run at**: {now}",
        f"- **Server**: `{base}`",
        f"- **User**: `{user}`",
        f"- **Storage backend**: `{(health or {}).get('storage_backend', 'unknown')}`",
        f"- **Operations**: {len(STEPS)} — **{passed} passed**, "
        f"**{len(failed)} failed**",
        f"- **Total time**: {total_ms:,.0f} ms "
        f"({total_ms / max(len(STEPS), 1):,.0f} ms average)",
        "",
    ]

    if failed:
        out += ["## Failures", ""]
        for s in failed:
            out.append(f"- **{s['label']}** — `{s['method']} {s['path']}` "
                       f"returned {s['status']}")
            out.append(f"  ```json\n  {json.dumps(s['response'])[:400]}\n  ```")
        out.append("")

    # Phase-by-phase detail
    for phase, heading, note in (
        ("CREATE", "Memories written", "New entities, facts and relations."),
        ("UPDATE", "Memories updated", "Note that PUT appends to the summary rather "
                                      "than replacing it, and omitted fields are left alone."),
        ("READ", "Memories read", "Includes graph traversal and non-LLM assessment."),
        ("DELETE", "Memories deleted", "Soft delete leaves a tombstone that reads as 404; "
                                      "hard delete removes the file and cascades to "
                                      "inbound relations."),
    ):
        rows = [s for s in STEPS if s["phase"] == phase]
        if not rows:
            continue
        out += [f"## {heading}", "", f"_{note}_", ""]
        for s in rows:
            mark = "OK" if s["ok"] else "**FAILED**"
            out += [f"### {s['label']}", "",
                    f"`{s['method']} {s['path']}` → {s['status']} {mark} · {s['ms']} ms", ""]
            if s["request"]:
                out += ["Request:", "", "```json",
                        json.dumps(s["request"], indent=2), "```", ""]
            resp = s["response"]
            if isinstance(resp, dict) and "wiki_id" in resp:
                out += ["Resulting memory:", "", entity_block(resp), ""]
            elif resp is None:
                out += ["_No response body (204)._", ""]
            else:
                text = json.dumps(resp, indent=2)
                if len(text) > 1400:
                    text = text[:1400] + "\n  ... truncated ..."
                out += ["Response:", "", "```json", text, "```", ""]

    out += ["## Operation log", "",
            "| # | Phase | Operation | Method | Status | ms |",
            "|---|---|---|---|---|---|"]
    for i, s in enumerate(STEPS, 1):
        out.append(f"| {i} | {s['phase']} | {s['label']} | `{s['method']}` | "
                   f"{s['status']} | {s['ms']} |")
    out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--user", default="crudreport")
    ap.add_argument("--out", default="crud_report.md")
    ap.add_argument("--token", default=None,
                    help="bearer token, if AUTH_MODE=token. Must be scoped to --user, "
                         "or be the admin token.")
    ap.add_argument("--keep", action="store_true",
                    help="leave the test entities in place instead of deleting them")
    args = ap.parse_args()
    global TOKEN
    TOKEN = args.token
    base = args.url.rstrip("/")

    hstatus, health, _ = call(base, "GET", "/healthz")
    if hstatus != 200:
        print(f"Server not reachable at {base} (healthz returned {hstatus}).")
        print("Start it with: python -m uvicorn app.main:app --host 127.0.0.1 --port 8000")
        raise SystemExit(1)

    run(base, args.user, args.keep)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report(base, args.user, health))

    passed = sum(1 for s in STEPS if s["ok"])
    failed = len(STEPS) - passed
    print(f"  {len(STEPS)} operations: {passed} passed, {failed} failed")
    print(f"  report written to {args.out}")
    if failed:
        for s in STEPS:
            if not s["ok"]:
                print(f"    FAILED {s['method']} {s['path']} -> {s['status']}")


if __name__ == "__main__":
    main()
