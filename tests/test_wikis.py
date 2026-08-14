"""
Multi-wiki registry and routing.

Two properties carry the design:

  - Access is decided in one place, and routing never reveals a wiki the
    caller cannot already reach.
  - Auto-creation cannot fragment a graph. A fragmented graph is worse than a
    large one, because the connections that make it a graph fall across the
    split.
"""

from __future__ import annotations

import tempfile

import pytest

from app.graph.store import EntityGraphStore
from app.storage.mirage_backend import MirageBackend
from app.wikis.registry import (ROLE_READ, ROLE_WRITE, WikiAccessDenied,
                                WikiError, WikiNameCollision, WikiNotFound,
                                WikiRegistry)
from app.wikis.router import WikiRouter


@pytest.fixture()
def env():
    backend = MirageBackend.from_disk(root=tempfile.mkdtemp())
    registry = WikiRegistry(backend)
    store = EntityGraphStore(backend)
    yield registry, store, WikiRouter(registry, store)
    backend.close()


# ---------- access ----------

def test_a_user_only_sees_wikis_they_are_granted(env):
    """Storage was `{user_id}/wiki/...`, so a credential could never reach
    another user's graph. For an organisation that is wrong — the point is
    that a team shares one wiki — so access became an explicit grant."""
    registry, _, _ = env
    registry.create("Sales", "alice")
    registry.create("Engineering", "bob")

    assert [w.wiki_id for w in registry.list_for("alice")] == ["sales"]
    assert [w.wiki_id for w in registry.list_for("bob")] == ["engineering"]

    registry.grant("sales", "bob", ROLE_READ)
    assert {w.wiki_id for w in registry.list_for("bob")} == {"engineering", "sales"}


def test_require_enforces_role_not_just_presence(env):
    registry, _, _ = env
    registry.create("Sales", "alice")
    registry.grant("sales", "bob", ROLE_READ)

    registry.require("sales", "bob", ROLE_READ)
    with pytest.raises(WikiAccessDenied):
        registry.require("sales", "bob", ROLE_WRITE)
    # The creator is admin, which outranks both.
    registry.require("sales", "alice", ROLE_WRITE)


def test_public_read_never_implies_write(env):
    """An open-read wiki is useful; an open-write one is vandalism waiting."""
    registry, _, _ = env
    registry.create("Handbook", "alice", public_read=True)

    registry.require("handbook", "stranger", ROLE_READ)
    with pytest.raises(WikiAccessDenied):
        registry.require("handbook", "stranger", ROLE_WRITE)


def test_platform_admin_bypasses_grants(env):
    registry, _, _ = env
    registry.create("Sales", "alice")
    registry.require("sales", "nobody", ROLE_WRITE, is_admin=True)


def test_missing_and_barred_wikis_are_indistinguishable_to_outsiders(env):
    """Which wikis exist is not something an unauthorised caller should be
    able to enumerate."""
    registry, _, _ = env
    registry.create("Secret Project", "alice")

    with pytest.raises(WikiNotFound):
        registry.require("does-not-exist", "bob")
    with pytest.raises(WikiAccessDenied):
        registry.require("secret-project", "bob")


def test_routing_never_offers_an_inaccessible_wiki(env):
    registry, store, router = env
    registry.create("Sales", "alice", description="Pipeline accounts quotas")
    store.upsert_entity("sales", "organization", "Acme Corp",
                        summary_append="Enterprise customer.")
    store.flush()
    registry.refresh_stats("sales", 1, ["Acme Corp"])

    assert router.score("alice", "the Acme Corp renewal")
    assert router.score("bob", "the Acme Corp renewal") == [], \
        "routing must not reveal a wiki bob cannot reach"


# ---------- anti-fragmentation ----------

def test_near_duplicate_names_are_refused_with_the_existing_id(env):
    """Sprawl is the failure mode of auto-creation: "Sales", "Sales Team",
    "The Sales Wiki". The error carries the existing id so the router can use
    it instead of splitting the graph."""
    registry, _, _ = env
    registry.create("Sales", "alice")

    for name in ("Sales Team", "The Sales Wiki", "Sales Department"):
        with pytest.raises(WikiNameCollision) as exc:
            registry.create(name, "alice")
        assert exc.value.existing_id == "sales"


def test_genuinely_distinct_names_are_allowed(env):
    """The collision check must not be so loose that it blocks real wikis."""
    registry, _, _ = env
    registry.create("Sales", "alice")
    registry.create("Engineering", "alice")
    registry.create("Legal", "alice")
    assert len(registry.list_all()) == 3


def test_similar_names_can_be_forced_but_only_by_a_human(env):
    """allow_similar exists for when two similarly named worlds really are
    distinct. The router never passes it — a machine should not be what
    decides that."""
    registry, _, _ = env
    registry.create("Sales", "alice")
    registry.create("Sales EMEA", "alice", wiki_id="sales-emea", allow_similar=True)
    assert len(registry.list_all()) == 2


def test_auto_creation_reuses_rather_than_fragmenting(env):
    """The whole point of the guard: a second conversation on the same subject
    must join the wiki the first one created."""
    registry, store, router = env

    first, _ = router.route_or_existing(
        "alice", "The Huaifeng independence war lasted six years against Sanrii.")
    assert first.action == "use" and first.wiki_id

    store.upsert_entity(first.wiki_id, "organization", "Huaifeng",
                        summary_append="Newly unified nation.")
    store.flush()
    registry.refresh_stats(first.wiki_id, 1, ["Huaifeng"])

    second, _ = router.route_or_existing(
        "alice", "Huaifeng and Sanrii signed the independence agreement.")
    assert second.wiki_id == first.wiki_id
    assert len(registry.list_all()) == 1, "must not have created a second wiki"


# ---------- routing decisions ----------

def _two_wikis(registry, store):
    registry.create("Sales", "alice", description="Pipeline accounts quotas revenue")
    registry.create("Engineering", "alice",
                    description="Services incidents architecture latency")
    store.upsert_entity("sales", "organization", "Acme Corp",
                        summary_append="Enterprise customer, largest by ARR.")
    store.upsert_entity("engineering", "project", "Orion",
                        summary_append="Search platform.")
    store.flush()
    registry.refresh_stats("sales", 1, ["Acme Corp"])
    registry.refresh_stats("engineering", 1, ["Orion"])


def test_a_clear_match_routes_without_asking(env):
    registry, store, router = env
    _two_wikis(registry, store)

    assert router.route("alice", "what is the status of the Acme Corp renewal?").wiki_id == "sales"
    assert router.route("alice", "Orion latency is above target").wiki_id == "engineering"


def test_a_tie_is_reported_rather_than_guessed(env):
    """Picking arbitrarily between two close candidates is worse than saying
    so: a silently wrong route is invisible."""
    registry, store, router = env
    _two_wikis(registry, store)

    decision = router.route("alice", "we need to discuss quotas and incidents")
    assert decision.action == "ambiguous"
    assert decision.wiki_id is None
    assert len(decision.candidates) >= 2


def test_no_match_proposes_creation_but_does_not_create(env):
    """route() decides; the caller performs. That is what allows a proposal to
    be reviewed before a graph is created."""
    registry, store, router = env
    _two_wikis(registry, store)

    decision = router.route("alice", "the weather is nice today",
                            allow_create=True)
    assert decision.action == "create"
    assert decision.proposed_title
    assert len(registry.list_all()) == 2, "route() must not have created anything"


def test_no_match_without_allow_create_says_so(env):
    registry, store, router = env
    _two_wikis(registry, store)
    assert router.route("alice", "the weather is nice today").action == "none"


def test_a_brand_new_wiki_can_still_win_a_route(env):
    """An empty wiki has no entities to match, so without a metadata fallback
    it could never attract a conversation — and so could never acquire any."""
    registry, store, router = env
    registry.create("Kestrel Prize", "alice",
                    description="Annual literary award ceremony and shortlists")

    decision = router.route("alice", "who is on the Kestrel Prize shortlist?")
    assert decision.wiki_id == "kestrel-prize"


# ---------- title proposal ----------

def test_proposed_title_ignores_speaker_labels_and_sentence_capitals(env):
    """A bare "hello?" transcript must not propose a wiki named after its
    speaker labels or sentence-initial capitals."""
    _, _, router = env
    text = "User: hello?\n\nAssistant: Hello! How can I help you today?"
    decision = router.route("alice", text, allow_create=True)
    assert decision.action == "create"
    assert decision.proposed_title == "New Wiki"


def test_proposed_title_keeps_real_proper_nouns(env):
    _, _, router = env
    decision = router.route("alice", "We signed a deal with Acme Corp yesterday.",
                            allow_create=True)
    assert "Acme Corp" in decision.proposed_title


def test_proposed_title_ignores_contractions_and_dedups(env):
    """"I'm" is not a proper noun, and a name said twice must not double."""
    _, _, router = env
    decision = router.route("alice", "So I'm working on Orion, and Orion needs a new index.",
                            allow_create=True)
    assert decision.proposed_title == "Orion"


def test_proposed_title_ignores_curly_apostrophe_contractions(env):
    """Smart-quote keyboards emit U+2019/U+2018 instead of ASCII ', and the
    contraction stripper only matched ASCII, so "I\u2019ll" was proposed as a
    proper noun. Curly apostrophes must be normalised before stripping."""
    _, _, router = env
    for apostrophe in ("\u2019", "\u2018"):
        text = f"Acme Corp renewal is at risk, I{apostrophe}ll handle it."
        decision = router.route("alice", text, allow_create=True)
        assert decision.proposed_title == "Acme Corp", (
            f"apostrophe {apostrophe!r} leaked into title: "
            f"{decision.proposed_title!r}")


# ---------- validation ----------

def test_wiki_ids_cannot_escape_their_prefix(env):
    registry, _, _ = env
    for bad in ("../../etc", "a/b", "UPPER", "with space", ""):
        with pytest.raises(WikiError):
            registry.create("Valid Title", "alice", wiki_id=bad)


def test_archiving_hides_from_routing_without_deleting(env):
    """An automatic process must never be able to destroy a graph."""
    registry, store, router = env
    _two_wikis(registry, store)
    registry.set_archived("sales", True)

    assert "sales" not in [w.wiki_id for w in registry.list_for("alice")]
    assert router.route("alice", "the Acme Corp renewal").wiki_id != "sales"
    assert registry.get("sales") is not None, "archiving must not delete"


# ---------- storage layout ----------

def test_wiki_keys_all_go_through_one_helper():
    """The prefix was built in twelve places across four modules. A missed
    site does not fail loudly — it silently reads and writes the wrong
    prefix — so nothing may construct these keys inline."""
    import pathlib
    import re

    # Only f-strings and literals that would actually become a key — prose in
    # docstrings mentioning the old layout is documentation, not a call site.
    key_expr = re.compile(r'(f"|f\')[^"\']*\{user_id\}/wiki')
    offenders = []
    for path in pathlib.Path("app/graph").glob("*.py"):
        if path.name == "keys.py":
            continue
        for line in path.read_text().splitlines():
            if key_expr.search(line) and "wiki_key" not in line:
                offenders.append(f"{path.name}: {line.strip()[:70]}")
    assert not offenders, "wiki keys built inline:\n" + "\n".join(offenders)


def test_entities_are_stored_under_the_wikis_prefix(env):
    registry, store, _ = env
    store.upsert_entity("sales", "person", "Alice Chen", summary_append="Engineer.")
    store.flush()

    keys = store.backend.list_keys("wikis/sales/")
    assert any(k.endswith("person/alice-chen.okf.md") for k in keys)
    assert not store.backend.list_keys("sales/wiki/"), "old layout must not be written"


def test_two_wikis_do_not_share_storage(env):
    """The isolation the whole change exists for."""
    registry, store, _ = env
    store.upsert_entity("sales", "organization", "Acme Corp")
    store.upsert_entity("engineering", "project", "Orion")
    store.flush()

    assert store.list_entities("sales") == ["organization/acme-corp"]
    assert store.list_entities("engineering") == ["project/orion"]
    assert store.get_entity("sales", "project/orion", touch=False) is None
