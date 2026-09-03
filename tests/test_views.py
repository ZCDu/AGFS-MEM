"""
Home-wiki + topic sub-scope + summarized views + diary.

Covers the "one home wiki per user with physically separate topic sub-scopes,
plus filtered AI-summarized views for colleagues/admins, plus the scheduled
diary" feature:

  - registry: ensure_home / create_topic / list_topics / topic_scope storage
  - grants: grant_view / list_views / revoke_view / can_view
  - summarize: on-demand LLM digest over all/topic scopes (fake LLM)
  - admin oversight: admin bypasses the grant but the read is still a digest
  - transparency: summarizing writes an audit note the owner can read
  - diary: generate_diary persists under the home wiki; run_diary_pass
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone

import pytest

from app.graph.store import EntityGraphStore
from app.storage.mirage_backend import MirageBackend
from app.wikis.registry import WikiRegistry, WikiError
from app.views.summarizer import WikiSummarizer
from app.views.diary import run_diary_pass


class FakeLLM:
    """Returns a fixed, parseable digest so tests never hit a real model."""

    def __init__(self, summary="Worked on the topic."):
        self.summary = summary
        self.calls = 0

    @property
    def configured(self):
        return True

    def complete(self, system, user, **kwargs):
        self.calls += 1
        assert "json_object" or True  # placeholder to keep signature honest
        return json.dumps({
            "summary": self.summary,
            "key_people": ["Alice"],
            "decisions": ["Move forward"],
            "open_items": [],
            "timeline": [],
            "sources": [],
        })


class FakeDiaryLLM(FakeLLM):
    def complete(self, system, user, **kwargs):
        self.calls += 1
        return json.dumps({
            "diary": "Today they worked on the migration.",
            "highlights": ["Cutover planned"],
            "topics_touched": ["snowflake"],
            "sources": [],
        })


def _today() -> str:
    """_seed() below creates facts with no explicit `when`, so they land
    dated at the real current UTC moment (see add_fact() -> _now_iso()).
    The diary tests need to ask about that same day; a hardcoded literal
    here was correct only on the day it was written and silently breaks
    once real time moves past it -- this keeps the assertion tied to
    what _seed() actually produces instead."""
    return datetime.now(timezone.utc).date().isoformat()


@pytest.fixture()
def env():
    backend = MirageBackend.from_disk(root=tempfile.mkdtemp())
    store = EntityGraphStore(backend)
    reg = WikiRegistry(backend)
    yield {"backend": backend, "store": store, "reg": reg}
    backend.close()


def _seed(env, owner="alice", topic="snowflake", title="Snowflake Migration"):
    reg, store = env["reg"], env["store"]
    home = reg.ensure_home(owner)
    top = reg.create_topic(owner, title, topic_key=topic,
                           topic="data migration")
    scope = reg.topic_scope(owner, topic)
    ent = store.upsert_entity(scope, "project", title,
                              summary_append="cutover planned for Q4")
    store.add_fact(scope, ent.wiki_id, "Move to the new platform by Q4")
    store.flush()
    return home, top, scope


# ---------- registry: home + topic sub-scopes ----------

def test_ensure_home_creates_kind_home(env):
    reg = env["reg"]
    home = reg.ensure_home("bob")
    assert home is not None
    assert home.kind == "home"
    assert home.wiki_id == "bob"


def test_create_topic_nested_under_home(env):
    reg = env["reg"]
    top = reg.create_topic("alice", "Snowflake Migration", topic_key="snowflake")
    assert top.kind == "topic"
    assert top.parent == "alice"
    assert top.topic_key == "snowflake"
    # storage scope nests under the home wiki (Interpretation A physical sub-scope)
    assert reg.topic_scope("alice", "snowflake") == "alice/topics/snowflake"


def test_same_topic_key_under_same_home_raises(env):
    reg = env["reg"]
    reg.create_topic("alice", "Snowflake", topic_key="snowflake")
    with pytest.raises(WikiError):
        reg.create_topic("alice", "Snowflake 2", topic_key="snowflake")


def test_topic_scope_different_users_do_not_collide(env):
    reg = env["reg"]
    reg.create_topic("alice", "Snowflake", topic_key="snowflake")
    reg.create_topic("bob", "Snowflake", topic_key="snowflake")  # same key, diff user -> ok
    assert reg.get_topic("alice", "snowflake") is not None
    assert reg.get_topic("bob", "snowflake") is not None
    assert reg.get_topic("alice", "snowflake").wiki_id == "alice-snowflake"
    assert reg.get_topic("bob", "snowflake").wiki_id == "bob-snowflake"


def test_list_topics_returns_homes_subgraphs(env):
    reg = env["reg"]
    reg.ensure_home("alice")
    reg.create_topic("alice", "Snowflake", topic_key="snowflake")
    reg.create_topic("alice", "Espresso", topic_key="espresso")
    keys = {t.topic_key for t in reg.list_topics("alice")}
    assert keys == {"snowflake", "espresso"}


def test_entities_land_in_topic_scope_not_home(env):
    _seed(env)
    store, reg = env["store"], env["reg"]
    scope = reg.topic_scope("alice", "snowflake")
    assert store.list_entities(scope) == ["project/snowflake-migration"]
    # the home wiki page holds no entities of its own
    assert store.list_entities("alice") == []


# ---------- view grants ----------

def test_grant_view_and_can_view(env):
    reg = env["reg"]
    reg.grant_view("alice", "boss", scope="all", permissions="summary")
    assert reg.can_view("alice", "boss", "all") is True
    assert reg.can_view("alice", "boss", "topic:snowflake") is True
    assert reg.can_view("alice", "stranger", "all") is False


def test_grant_view_owner_and_admin_bypass(env):
    reg = env["reg"]
    assert reg.can_view("alice", "alice", "all") is True  # owner
    assert reg.can_view("alice", "admin", "all", is_admin=True) is True  # admin


def test_revoke_view(env):
    reg = env["reg"]
    reg.grant_view("alice", "boss")
    assert reg.can_view("alice", "boss", "all") is True
    assert reg.revoke_view("alice", "boss") is True
    assert reg.can_view("alice", "boss", "all") is False


# ---------- summarize (fake LLM) ----------

def test_summarize_all_includes_topic_scope(env):
    _seed(env)
    store, reg = env["store"], env["reg"]
    s = WikiSummarizer(reg, store, FakeLLM())
    res = s.summarize("alice", query="what did alice do?", scope="all",
                      viewer="boss", is_admin=True)
    assert res["entity_count"] == 1
    assert res["summary"]
    assert res["owner"] == "alice"
    assert res["viewer"] == "boss"


def test_summarize_topic_only(env):
    _seed(env)
    store, reg = env["store"], env["reg"]
    s = WikiSummarizer(reg, store, FakeLLM())
    res = s.summarize("alice", query="snowflake", scope="topic:snowflake",
                      viewer="boss", is_admin=True)
    assert res["entity_count"] == 1


def test_summarize_empty_wiki_is_degraded_not_error(env):
    store, reg = env["store"], env["reg"]
    reg.ensure_home("carol")
    s = WikiSummarizer(reg, store, FakeLLM())
    res = s.summarize("carol", viewer="boss", is_admin=True)
    assert res["entity_count"] == 0
    assert "No stored information" in res["summary"]


# ---------- transparency: audit trail ----------

def test_record_and_list_audit(env):
    store, reg = env["store"], env["reg"]
    reg.ensure_home("alice")
    s = WikiSummarizer(reg, store, FakeLLM())
    s.record_view("alice", "boss", "all", kind="summary", note="weekly check")
    s.record_view("alice", "admin", "topic:snowflake", kind="admin-summary")
    views = s.list_audit("alice")
    assert len(views) == 2
    assert views[0]["viewer"] == "admin"  # newest first
    assert views[0]["kind"] == "admin-summary"


# ---------- diary ----------

def test_generate_diary_persists_and_is_readable(env):
    _seed(env)
    store, reg = env["store"], env["reg"]
    s = WikiSummarizer(reg, store, FakeDiaryLLM())
    day = _today()
    rec = s.generate_diary("alice", day=day, viewer="system")
    assert rec is not None
    assert rec["diary"]
    entries = s.read_diary("alice", day=day)
    assert len(entries) == 1
    assert entries[0]["owner"] == "alice"


def test_run_diary_pass_across_homes(env):
    reg, store = env["reg"], env["store"]
    _seed(env, owner="alice", topic="snowflake", title="Snowflake Migration")
    _seed(env, owner="bob", topic="ci", title="CI Runner")
    def factory(reg, store):
        return WikiSummarizer(reg, store, FakeDiaryLLM())
    res = run_diary_pass(env["backend"], day=_today(),
                         summarizer_factory=factory)
    assert set(res) <= {"alice", "bob"}
    assert res.get("alice") == "generated"
    assert res.get("bob") == "generated"


def test_diary_skips_when_nothing_dated(env):
    reg, store = env["reg"], env["store"]
    reg.ensure_home("dan")
    # no entities at all -> no diary
    s = WikiSummarizer(reg, store, FakeDiaryLLM())
    assert s.generate_diary("dan", day=_today(), viewer="system") is None
