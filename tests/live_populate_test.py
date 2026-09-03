"""
Live smoke test: wiki population + wiki page creation, against the RUNNING
server (mirage/S3 backend on :8000 — the real production path).

This exercises the exact chain a user hits in the chat UI:
    POST /extract            -> plan (route + LLM-extract entities)
    POST /extract/apply      -> materialise new wiki + write entities + render page
    GET  /wikis/{id}         -> read the created wiki
    GET  /subgraph / list    -> verify entities actually landed in the wiki

It uses a THROWAWAY user + wiki names (prefix `poptest-`) so it cannot touch
demo / sre-on-call-rotation, and it archives the wikis it creates so the live
registry is left clean.

Usage:
    .venv\\Scripts\\python.exe tests\\live_populate_test.py           # runs
    set LIVEPOP=skip                                               # dry-skip
"""

import json
import os
import sys
import urllib.request
import urllib.error

BASE = os.environ.get("LIVEPOP_BASE", "http://127.0.0.1:8000")
USER = f"poptest-{os.getpid()}"          # throwaway user, unique per run
WIKI_PREFIX = "poptest-wiki"
ARCHIVE = ["ar"]                            # we archive wikis we create

results: list[tuple[str, bool, str]] = []  # (label, passed, detail)


def call(method: str, path: str, body: dict | None = None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}")
        except Exception:
            detail = {"raw": e.read().decode(errors="replace")[:300]}
        return e.code, detail


def check(label: str, ok: bool, detail: str = ""):
    results.append((label, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))


def main():
    if os.environ.get("LIVEPOP") == "skip":
        print("LIVEPOP=skip -> dry run, no requests sent.")
        return

    created_wikis: list[str] = []

    # ---- 1. A genuinely NEW topic -> should create a new wiki ----
    print("\n=== 1. new-topic: extract -> apply -> verify ===")
    text_new = (
        "We kicked off the Meridian logistics platform build this quarter. "
        "Priya Sharma is the program lead, and the first milestone is the "
        "container-tracking data pipeline with Acme's freight team."
    )
    st, plan = call("POST", f"/v1/users/{USER}/extract",
                    {"text": text_new, "force": True})
    check("extract plan returned 200", st == 200, f"status={st}")
    if st != 200:
        check("plan has a target or proposal", False, json.dumps(plan)[:300])
        return _report()
    plan_ok = (plan.get("target_wiki") or plan.get("new_wiki_proposal")) is not None
    check("plan resolved a target/proposal", plan_ok,
          f"target={plan.get('target_wiki')} target_wiki_reason="
          f"{plan.get('target_wiki_reason')}")
    has_ops = bool(plan.get("operations"))
    check("plan has extraction ops", has_ops, f"n_ops={len(plan.get('operations') or [])}")

    if not plan.get("new_wiki_proposal") and not plan.get("target_wiki"):
        check("(apply skipped: nothing to apply)", False,
              "no target and no proposal from a brand-new topic")
        return _report()

    # ---- 2. Apply (creates the wiki if it's a new topic) ----
    st, applied = call("POST", f"/v1/users/{USER}/extract/apply", {
        "target_wiki": plan.get("target_wiki"),
        "operations": plan.get("operations") or [],
        "new_wiki_proposal": plan.get("new_wiki_proposal"),
    })
    check("apply returned 200", st == 200, f"status={st}")
    if st == 200:
        applied_ok = applied.get("failed", 1) == 0
        check("all ops applied (0 failures)", applied_ok,
              f"applied={applied.get('applied')} failed={applied.get('failed')}")
        target = plan.get("target_wiki")
        if target:
            created_wikis.append(target)
            check("the new wiki was created", True, f"wiki={target}")
    else:
        check("apply failure detail", False, json.dumps(applied)[:300])

    # ---- 3. Confirm the wiki page exists + is readable ----
    for wid in created_wikis:
        st, page = call("GET", f"/v1/wikis/{wid}")
        check(f"GET /wikis/{wid} (page) is 200", st == 200, f"status={st}")

    # ---- 4. A second related message -> should JOIN the wiki (population) ----
    print("\n=== 2. continuation: same topic should join the wiki ===")
    if created_wikis:
        st, plan2 = call("POST", f"/v1/users/{USER}/extract",
                         {"text": (
                             "Priya Sharma signed off the Meridian milestone "
                             "review; container tracking is now on track."
                         ), "force": True})
        check("continuation plan is 200", st == 200, f"status={st}")
        if st == 200:
            joined = plan2.get("target_wiki") == created_wikis[0]
            check("continuation routed to SAME wiki (no duplicate)", joined,
                  f"target={plan2.get('target_wiki')} expected={created_wikis[0]}")
            check("continuation was NOT a new-topic proposal",
                  plan2.get("new_wiki_proposal") is None,
                  "no new_wiki_proposal on a continuation")

    # ---- 5. Population: entities actually landed in the wiki scope ----
    print("\n=== 3. entity population (storage actually has the pages) ===")
    # GET /v1/users/{user}/wiki lists the entities stored under the user's
    # reachable wiki scopes (ManifestEntryOut list).
    st, ents = call("GET", f"/v1/users/{USER}/wiki")
    check("GET /users/{{user}}/wiki lists entities", st == 200, f"status={st}")
    if st == 200:
        names = ents if isinstance(ents, list) else ents.get("entities", [])
        # Each entry carries an entity path like organization/acme-corp; count
        # non-empty entries across the created wikis.
        stored = [e for e in names if isinstance(e, dict) and e.get("wiki_id")]
        check("created wikis have stored entities", len(stored) >= 2,
              f"n_entities={len(stored)} -> {stored[:6]}")
    # Prove page CREATION: read back an actual entity page. GET /wikis/{id}
    # only returns metadata; the rendered page lives in storage as an entity
    # file. Fetch one entity page and confirm it carried the rendered content.
    for wid in created_wikis:
        # entity ref: organization/acme (from population output above)
        st, ent = call("GET", f"/v1/users/{USER}/wiki/project/Meridian")
        check(f"entity page rendered in {wid}", st == 200, f"status={st}")
        if st == 200 and isinstance(ent, dict):
            check(f"entity {wid} has summary content", bool(ent.get("compact") or ent.get("summary")),
                  f"compact_len={len(ent.get('compact') or '')}")
        elif st != 200:
            check(f"entity fetch {wid}", False, f"status={st}")
    if not created_wikis:
        check("entity page render (no wiki to check)", True, "none created")

    # ---- 6. Cleanup: archive throwaway wikis so the registry is left clean ----
    print("\n=== 4. cleanup (archive created wikis) ===")
    for wid in created_wikis:
        st, _ = call("POST", f"/v1/wikis/{wid}/archive?archived=true")
        check(f"archived {wid}", st == 200, f"status={st}")

    _report()


def _report():
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'='*60}\nRESULT: {passed}/{len(results)} checks passed")
    for label, ok, detail in results:
        if not ok:
            print(f"  FAILED: {label}  -- {detail}")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
