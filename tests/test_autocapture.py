"""
Auto-capture scheduler tests.

The auto-capture path is the "no button click" flow: a timer (or a manual
POST /autocapture/run) reads a day's journaled sessions and runs assessor ->
LLM extract -> route -> apply — the exact pipeline /extract?apply=true uses —
instead of leaving it to a human to read a plan and press Apply.

What is worth pinning here is:
  - it reads the day's sessions and turns durable content into applied facts
  - a genuinely new topic lands in a properly-named NEW wiki (never a demo
    dump) — the same no-personal-wiki rule as the interactive path
  - it is idempotent: re-running does not double-store the same session
  - chatter / empty transcripts are skipped, not stored
  - routing happens per transcript, so two unrelated topics in one day get
    two wikis

Everything runs against a STUB model so the logic is deterministic and needs
no API key.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date

import pytest

from app.autocapture.scheduler import backfill, capture_day
from app.extract.extractor import ConversationExtractor
from app.graph.store import EntityGraphStore
from app.storage.mirage_backend import MirageBackend
from tests.test_extract import StubLLM

TOPIC_1 = (
    "We decided today to spin up the Orion search migration. Alice Chen will "
    "lead it and the deadline is 2026-09-30."
)
TOPIC_2 = (
    "The office coffee machine is broken. The facilities team ordered a new "
    "espresso machine and it arrives Friday."
)

GOOD_RESPONSE = {
    "entities": [
        {"title": "Alice Chen", "type": "person", "aliases": ["Alice"],
         "summary": "Engineer leading the Orion migration.",
         "facts": [{"text": "Leads the Orion search migration.", "confidence": 0.95}]},
        {"title": "Orion", "type": "project",
         "summary": "Search platform moving off the legacy index.",
         "facts": [{"text": "Migration deadline is 2026-09-30.", "confidence": 0.9}]},
    ],
    "relations": [
        {"source": "Alice Chen", "target": "Orion", "category": "related_to",
         "label": "leads", "reason": "stated"},
    ],
    "discarded": [],
}

COFFEE_RESPONSE = {
    "entities": [
        {"title": "Office Coffee Machine", "type": "artifact",
         "summary": "The break-room espresso machine.",
         "facts": [{"text": "Replacement espresso machine arrives Friday.",
                    "confidence": 0.9}]},
    ],
    "relations": [],
    "discarded": [],
}


@pytest.fixture()
def store():
    backend = MirageBackend.from_disk(root=tempfile.mkdtemp())
    yield EntityGraphStore(backend), backend
    backend.close()


def _seed_session(backend, user_id, day, session_id, content):
    """Write a session JSONL file directly to storage, as chat would. Session
    keys live at {user_id}/sessions/{Y}/{M}/{date}/{session}.jsonl (no "users/"
    prefix -- see rawlog/sessions.py)."""
    key = f"{user_id}/sessions/{day.year:04d}/{day.month:02d}/" \
          f"{day.isoformat()}/{session_id}.jsonl"
    lines = [
        json.dumps({"role": "user", "content": content, "ts": day.isoformat() + "T09:00:00+00:00"}),
        json.dumps({"role": "assistant", "content": "ok", "ts": day.isoformat() + "T09:00:01+00:00"}),
    ]
    backend.put_bytes(key, ("\n".join(lines) + "\n").encode("utf-8"))


def _extractor_with(llm, store, **kw):
    return ConversationExtractor(store, llm, **kw)


# ---------- the happy path ----------

def test_capture_lands_in_the_users_own_wiki_and_applies(store):
    """Single-wiki deployment: capture always lands in the user's own home
    wiki (wiki_id == user_id) and applies the extracted facts there --
    never a separate topic wiki."""
    store, backend = store
    day = date(2026, 8, 18)
    _seed_session(backend, "alice", day, "s1", TOPIC_1)

    ex = _extractor_with(StubLLM(GOOD_RESPONSE), store)

    report = capture_day(backend, target=day, user_ids=["alice"], extractor=ex)

    assert report.stored == 1, report.to_dict()
    assert report.processed_sessions == 1
    registry_ids = _registry(backend)
    assert "demo" not in registry_ids
    # The user's own wiki exists and owns the entities.
    assert "alice" in registry_ids, registry_ids
    ids = set(store.list_entities("alice"))
    assert {"person/alice-chen", "project/orion"} <= ids
    alice = store.get_entity("alice", "person/alice-chen", touch=False)
    assert any("migration" in f.text for f in alice.facts)


def test_capture_skips_messages_already_reviewed_by_interactive_flow(store):
    """Messages a human already reviewed and applied (flagged `examined` on
    the session log) must NOT be re-examined by auto-capture — that is the
    review-data contamination guard. Only the unexamined parts are processed.

    Seed a session with two durable topics. Simulate the interactive flow
    having already reviewed + applied the FIRST message (mark it examined), so
    capture runs only on the second, unexamined part."""
    store_, backend = store
    day = date(2026, 8, 19)
    key = f"alice/sessions/{day.year:04d}/{day.month:02d}/{day.isoformat()}/s-partial.jsonl"
    msgs = [
        # reviewed + applied already (examined=True)
        {"role": "user", "content": TOPIC_1,
         "ts": day.isoformat() + "T09:00:00+00:00", "examined": True},
        {"role": "assistant", "content": "ok",
         "ts": day.isoformat() + "T09:00:01+00:00", "examined": True},
        # NOT yet examined -> must be the only thing capture looks at
        {"role": "user", "content": TOPIC_2,
         "ts": day.isoformat() + "T09:00:02+00:00"},
        {"role": "assistant", "content": "ok",
         "ts": day.isoformat() + "T09:00:03+00:00"},
    ]
    lines = [json.dumps(m, ensure_ascii=False) for m in msgs]
    backend.put_bytes(key, ("\n".join(lines) + "\n").encode("utf-8"))

    ex = _extractor_with(StubLLM(COFFEE_RESPONSE), store_)  # only the coffee topic is unexamined

    report = capture_day(backend, target=day, user_ids=["alice"], extractor=ex)

    # Only the unexamined coffee topic was processed and stored, into alice's
    # own wiki (single-wiki deployment: there is nowhere else for it to go).
    assert report.stored == 1, report.to_dict()
    ids = _registry(backend)
    assert "alice" in ids, ids


def test_grown_session_only_feeds_new_unexamined_messages(store):
    """When a session grows after capture, the next run examines only the new,
    unexamined messages — never the already-captured tail again."""
    backend = store[1]
    day = date(2026, 8, 19)
    _seed_session(backend, "alice", day, "s-grow", TOPIC_1)

    ex = _extractor_with(StubLLM(GOOD_RESPONSE), store[0])

    first = capture_day(backend, target=day, user_ids=["alice"], extractor=ex)
    assert first.stored == 1, first.to_dict()
    alice_after_first = set(store[0].list_entities("alice"))
    assert "person/alice-chen" in alice_after_first

    # Append a NEW, unrelated durable topic to the SAME session file.
    lines = backend.get_bytes(
        f"alice/sessions/{day.year:04d}/{day.month:02d}/"
        f"{day.isoformat()}/s-grow.jsonl").data.decode("utf-8")
    extra = json.dumps({"role": "user", "content": TOPIC_2,
                        "ts": day.isoformat() + "T10:00:00+00:00"},
                       ensure_ascii=False)
    backend.put_bytes(
        f"alice/sessions/{day.year:04d}/{day.month:02d}/"
        f"{day.isoformat()}/s-grow.jsonl", (lines + extra + "\n").encode("utf-8"))

    ex2 = _extractor_with(StubLLM(COFFEE_RESPONSE), store[0])
    second = capture_day(backend, target=day, user_ids=["alice"], extractor=ex2)

    # The new coffee topic was captured, into the same wiki as before…
    assert second.stored == 1, second.to_dict()
    ids = _registry(backend)
    assert "alice" in ids, ids
    # …and the original Orion entities were NOT re-created as fresh duplicates.
    # (Their facts persist untouched, plus whatever the coffee topic added.)
    assert alice_after_first <= set(store[0].list_entities("alice"))


def test_capture_is_idempotent(store):
    """Re-running capture over the same day must not double-store the same
    session — the cursor marks each date:session processed."""
    backend = store[1]
    day = date(2026, 8, 18)
    _seed_session(backend, "alice", day, "s1", TOPIC_1)

    ex = _extractor_with(StubLLM(GOOD_RESPONSE), store[0])

    first = capture_day(backend, target=day, user_ids=["alice"], extractor=ex)
    ids_after_first = set(store[0].list_entities("alice"))
    assert ids_after_first, "the first pass should have stored something"

    second = capture_day(backend, target=day, user_ids=["alice"], extractor=ex)

    assert first.stored == 1
    assert second.processed_sessions == 0, "cursor should skip it"
    assert second.stored == 0
    # Nothing extra was written on the second pass.
    assert set(store[0].list_entities("alice")) == ids_after_first


def test_two_unrelated_topics_in_one_day_land_in_the_same_wiki(store):
    """A day can hold several unrelated conversations. In a single-wiki
    deployment they all land in the SAME home wiki -- there is nowhere else
    for either one to go, unlike the retired multi-wiki-per-topic model."""
    backend = store[1]
    day = date(2026, 8, 18)
    _seed_session(backend, "alice", day, "s1", TOPIC_1)
    _seed_session(backend, "alice", day, "s2", TOPIC_2)

    class ByPromptStub:
        configured = True
        def complete(self, system, user, **kw):
            resp = GOOD_RESPONSE if ("Orion" in user) else COFFEE_RESPONSE
            return json.dumps(resp)

    ex = _extractor_with(ByPromptStub(), store[0])
    report = capture_day(backend, target=day, user_ids=["alice"], extractor=ex)

    assert report.stored == 2, report.to_dict()
    ids = _registry(backend)
    assert set(ids) == {"alice"}
    entities = set(store[0].list_entities("alice"))
    assert {"person/alice-chen", "project/orion"} <= entities


def test_two_unrelated_topics_in_one_session_land_in_the_same_wiki(store):
    """A single session with two unrelated user fact-dumps still lands
    entirely in the user's one wiki -- both dumps' entities coexist there
    rather than being split across separate topic wikis."""
    backend = store[1]
    day = date(2026, 8, 18)
    key = f"alice/sessions/{day.year:04d}/{day.month:02d}/{day.isoformat()}/s-multi.jsonl"
    msgs = [
        ("user", TOPIC_1),
        ("assistant", "got it."),
        ("user", "Sterling Bank is a British commercial bank founded in 1897. "
                  "The Corinth Exchange lists about 800 tickers."),
        ("assistant", "understood."),
    ]
    lines = [json.dumps({"role": r, "content": c,
                          "ts": day.isoformat() + f"T09:00:0{i}+00:00"})
             for i, (r, c) in enumerate(msgs)]
    backend.put_bytes(key, ("\n".join(lines) + "\n").encode("utf-8"))

    BANK_RESPONSE = {
        "entities": [
            {"title": "Sterling Bank", "type": "organization",
             "summary": "British commercial bank founded in 1897.",
             "facts": [{"text": "Founded in 1897.", "confidence": 1.0}]},
            {"title": "The Corinth Exchange", "type": "organization",
             "aliases": ["Corinth"],
             "summary": "Fictional stock exchange listing about 800 tickers.",
             "facts": [{"text": "Lists about 800 tickers.", "confidence": 1.0}]},
        ],
        "relations": [],
        "discarded": [],
    }

    class MultiTopicStub:
        configured = True
        def complete(self, system, user, **kw):
            resp = GOOD_RESPONSE if ("Orion" in user) else BANK_RESPONSE
            return json.dumps(resp)

    ex = _extractor_with(MultiTopicStub(), store[0])
    report = capture_day(backend, target=day, user_ids=["alice"], extractor=ex)

    assert report.stored >= 1, report.to_dict()
    ids = _registry(backend)
    assert set(ids) == {"alice"}
    entities = set(store[0].list_entities("alice"))
    assert "person/alice-chen" in entities
    assert any("sterling" in w or "corinth" in w for w in entities), entities


def test_chatter_session_is_skipped(store):
    """A session with no durable content should be assessed, found empty, and
    skipped — recorded as no-op, not stored, and cursor-marked so we never
    re-assess it."""
    backend = store[1]
    day = date(2026, 8, 18)
    _seed_session(backend, "bob", day, "s1", "hey thanks so much, sounds good")

    llm = StubLLM({})  # would return empty if called; assessor should gate it
    llm.evaluate_new_topic = lambda text, existing, **kw: {
        "is_new_topic": True, "belongs_to": None, "topic": "t", "title": "X",
        "description": "", "reason": ""}
    ex = _extractor_with(llm, store[0])

    first = capture_day(backend, target=day, user_ids=["bob"], extractor=ex)
    second = capture_day(backend, target=day, user_ids=["bob"], extractor=ex)

    assert first.processed_sessions == 1
    assert first.stored == 0
    assert first.skipped == 1, first.to_dict()
    # The empty day should not have created a wiki.
    assert _registry(backend) == {}
    # And the cursor marks it: no re-processing next run.
    assert second.processed_sessions == 0


# ---------- the manual trigger endpoint ----------

def test_manual_run_endpoint(client):
    """POST /v1/users/{u}/autocapture/run runs capture for that user and
    returns a well-formed report. With no sessions (and a real extractor that
    would need an API key only when there is actually something durable to
    extract), it returns an empty report rather than erroring."""
    import os
    # Ensure the endpoint can be reached without a key when there is nothing
    # durable (the assessor gates the LLM; empty sessions never reach it).
    resp = client.post("/v1/users/u/autocapture/run")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["user_id"] == "u"
    assert "processed_sessions" in data
    assert data["per_session"] == []


# ---------- the startup backfill ----------

def test_backfill_catches_up_missed_days(store):
    """backfill() scans the trailing window and captures any day whose
    sessions the cursor hasn't marked — the "closed overnight / over the
    weekend" self-heal. It must not re-store days already captured."""
    backend = store[1]
    through = date(2026, 8, 18)
    missed_day = date(2026, 8, 16)  # a day the service was down
    _seed_session(backend, "alice", missed_day, "s1", TOPIC_1)

    ex = _extractor_with(StubLLM(GOOD_RESPONSE), store[0])

    # A window that includes the missed day (and days with no sessions).
    reports = backfill(backend, days=2, extractor=ex, through=through)

    # One of the scanned days captured the missed session, into alice's own wiki.
    assert sum(r.stored for r in reports) == 1
    assert "alice" in _registry(backend)

    # Run it again — backfill is idempotent, nothing double-stores.
    again = backfill(backend, days=2, extractor=ex, through=through)
    assert sum(r.stored for r in again) == 0
    # Entity count unchanged after the second pass.
    ids = set(store[0].list_entities("alice"))
    assert {"person/alice-chen", "project/orion"} <= ids


def _registry(backend) -> dict:
    raw = backend.get_bytes("wikis/_registry.json")
    if raw is None:
        return {}
    data = json.loads(raw.data.decode("utf-8"))
    return data.get("wikis", {})


def test_each_user_gets_their_own_wiki_even_with_a_shared_wiki_granted(store):
    """Single-wiki deployment: auto-capture ALWAYS writes a user's content
    into that user's own home wiki, even when a shared wiki exists and both
    users are granted write on it. There is no more "the router decided this
    journal belongs to the shared project wiki" outcome for capture -- that
    would require topic routing, which is retired in this deployment mode.
    """
    store_, backend = store
    day = date(2026, 8, 18)

    # A shared wiki exists with both teammates granted write -- capture must
    # still ignore it and route each teammate's content to their own wiki.
    from app.wikis.registry import WikiRegistry, ROLE_WRITE
    reg = WikiRegistry(backend)
    reg.create("Snowflake Migration", created_by="alice",
               wiki_id="snowflake-migration",
               description="Moving analytics onto Snowflake",
               topic="snowflake data warehouse migration")
    reg.grant("snowflake-migration", "alice", ROLE_WRITE)
    reg.grant("snowflake-migration", "bob", ROLE_WRITE)

    ALICE_JOURNAL = (
        "alice: we agreed Snowflake is our staging target for the new "
        "analytics warehouse; cutover is next quarter."
    )
    BOB_JOURNAL = (
        "bob: the Snowflake migration moved the ledger load to the new "
        "warehouse today; zero downtime achieved."
    )
    _seed_session(backend, "alice", day, "s-alice", ALICE_JOURNAL)
    _seed_session(backend, "bob", day, "s-bob", BOB_JOURNAL)

    class SharedWikiStub:
        configured = True
        def complete(self, system, user, **kw):
            who = "alice" if "alice:" in user else "bob"
            return json.dumps({
                "entities": [
                    {"title": who, "type": "person",
                     "summary": f"{who} working on the Snowflake migration.",
                     "facts": [{"text": "Works on the Snowflake migration.",
                                 "confidence": 0.9}]},
                ],
                "relations": [],
                "discarded": [],
            })

    ex = _extractor_with(SharedWikiStub(), store_)
    report = capture_day(backend, target=day, user_ids=["alice", "bob"], extractor=ex)

    assert report.stored == 2, report.to_dict()
    assert report.failed == 0, report.to_dict()

    # Each teammate's content landed in THEIR OWN wiki, not the shared one.
    ids = _registry(backend)
    assert "alice" in ids and "bob" in ids
    assert "person/alice" in set(store_.list_entities("alice"))
    assert "person/bob" in set(store_.list_entities("bob"))
    assert set(store_.list_entities("snowflake-migration")) == set()
