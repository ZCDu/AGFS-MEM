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


def test_thin_match_with_new_names_proposes_a_new_wiki(env):
    """The core of "smarter determination": when the best existing match is
    only a weak word-overlap (no specific named entity matches the text) AND
    the text introduces new named entities, the router must propose a NEW
    independent wiki instead of forcing unrelated content into the existing
    one. This is the "unrelated nodes forcibly stored in a wiki page" failure
    the change exists to stop."""
    registry, store, router = env
    # A wiki whose metadata shares generic buzzwords but has NO entity that
    # the new text is really about. "Incidents" appears in both, so it clears
    # MIN_SCORE via _metadata_score (thin), but Nimbus/Runbook are new names.
    registry.create("SRE On-Call", "alice",
                    description="Incidents on-call rotation alerts paging")
    store.upsert_entity("sre-on-call", "artifact", "Opsgenie",
                        summary_append="Paging and on-call scheduling tool.")
    store.flush()
    registry.refresh_stats("sre-on-call", 1, ["Opsgenie"])

    decision = router.route(
        "alice",
        "Nimbus Runbook automation is being retired next quarter.",
        allow_create=True)
    # Must NOT be forced into sre-on-call: that wiki matched on buzzwords, not
    # on Nimbus/Runbook.
    assert decision.action == "create", decision
    assert len(registry.list_all()) == 1, "route() must still not create"


def test_thin_match_with_no_new_names_keeps_the_home(env):
    """The empty-home guard: when the text introduces no new named entities,
    a thin match should still route to the existing wiki rather than spin up a
    new one. Without this, a user pasting a first message about a topic into
    their one (empty) wiki would generate "create another wiki" instead of
    using the one they just made."""
    registry, store, router = env
    registry.create("Kestrel Prize", "alice",
                    description="Annual literary award ceremony and shortlists")

    decision = router.route(
        "alice", "who is on the Kestrel Prize shortlist?", allow_create=True)
    # Kestrel Prize is present in the wiki's metadata (no new names), so even
    # though the match is metadata-thin, we use the existing wiki.
    assert decision.action == "use"
    assert decision.wiki_id == "kestrel-prize", decision


def test_name_only_match_on_a_different_topic_queries_not_uses(env):
    """The "unrelated nodes forced into a wiki page" failure in its subtlest
    form: the text cites entities a wiki KNOWS (so it scores as a strong,
    relevance 1.0 match) but its actual subject is a different topic. "Alice
    Chen wants a new espresso machine" names person/alice-chen but is not
    about Alice's work. Routing such text straight into that wiki (action
    "use") is what dumped unrelated memory into the demo wiki. It must
    surface as "query" — cite the wiki but ask the LLM whether this is a new
    topic — rather than auto-committing the write."""
    registry, store, router = env
    registry.create("Kestrel Prize", "alice",
                    description="Annual literary award ceremonies and shortlists")
    store.upsert_entity("kestrel-prize", "person", "Alice Chen",
                        summary_append="Literary editor for the Kestrel shortlist.")
    store.upsert_entity("kestrel-prize", "organization", "Acme Corp",
                        summary_append="Sponsor of the Kestrel Prize.")
    store.flush()
    registry.refresh_stats("kestrel-prize", 2, ["Alice Chen", "Acme Corp"])

    # Alice Chen and Acme Corp are entities this wiki KNOWS, so the espresso
    # text scores high on names — but the shared content vocabulary is nil
    # (the break-room subject shares no words with what the wiki knows).
    decision = router.route(
        "alice",
        "Alice Chen from Acme Corp wants to revamp the espresso machine "
        "in the break room; she's also looking at a second pour-over station.",
        allow_create=True)
    assert decision.action == "query", decision
    assert decision.wiki_id == "kestrel-prize", decision


def test_question_citing_a_known_entity_queries_not_uses(env):
    """READ-side counterpart: a QUESTION that cites a known entity's name but
    shares no topical vocabulary is a lookup ABOUT that entity, not a claim of
    a new topic. It must surface as "query" carrying the cited wiki -- so the
    chat read-path can retrieve from the cited wiki -- and NOT be declared a
    new topic. (A read that lands on the cited wiki and finds nothing is a
    safely-wrong answer; a write to the wrong graph is corruption, which is
    why writes gate behind the LLM but reads short-circuit to the cited wiki.)"""
    registry, store, router = env
    registry.create("Kestrel Prize", "alice",
                    description="Annual literary award ceremonies and shortlists")
    store.upsert_entity("kestrel-prize", "person", "Alice Chen",
                        summary_append="Literary editor for the Kestrel shortlist.")
    store.flush()
    registry.refresh_stats("kestrel-prize", 1, ["Alice Chen"])

    decision = router.route("alice", "Who is Alice Chen?")
    assert decision.action == "query", decision
    assert decision.wiki_id == "kestrel-prize", decision


def test_shared_entity_tie_across_wikis_queries_not_ambiguous(env):
    """The same entity known to TWO unrelated wikis (e.g. person/priya in
    both a database-drills wiki and an office-plants wiki) used to make the
    router report a false TIE -> 409 Conflict for any text naming that
    person, even when the text's real subject is neither wiki's topic ("Priya
    is planning the team offsite"). A shared name is a reference, not a
    membership conflict: the text must surface as "query" (let the LLM decide
    new-topic vs continuation) so an unrelated subject gets its own wiki
    instead of deadlocking on a name coincidence."""
    registry, store, router = env
    registry.create("Database Failover Drills", "alice",
                    description="On-call runbook and failover exercises")
    registry.create("Office Plants", "alice",
                    description="Facilities and lobby greenery")
    store.upsert_entity("db-drills", "person", "Priya",
                        summary_append="Runs the on-call failover drills.")
    store.upsert_entity("office-plants", "person", "Priya",
                        summary_append="Tracks facilities plant requests.")
    store.flush()
    registry.refresh_stats("db-drills", 1, ["Priya"])
    registry.refresh_stats("office-plants", 1, ["Priya"])

    text = ("Priya is helping plan the Q3 team offsite in the mountains, "
            "team-building games plus a roadmap retrospective.")
    decision = router.route("alice", text, allow_create=True)
    assert decision.action == "query", decision
    assert decision.wiki_id in ("db-drills", "office-plants"), decision
    assert decision.action != "ambiguous"  # shared name is NOT a disqualifying tie


def test_name_match_with_real_topic_overlap_still_uses(env):
    """A name match that ALSO shares content vocabulary with an entity's
    summary is a genuine continuation and must keep routing home (no LLM
    round-trip). This is the counterpart to the espresso case: the topical
    tier proves the text is about what the wiki knows, so "use" is safe."""
    registry, store, router = env
    registry.create("Engineering", "alice",
                    description="Services incidents architecture latency")
    store.upsert_entity("engineering", "project", "Orion",
                        summary_append="Search platform; latency incidents recur "
                                       "when above target.")
    store.flush()
    registry.refresh_stats("engineering", 1, ["Orion"])

    # "latency above target" shares two domain words (latency, target) with
    # Orion's summary -> summary-tier topical match, so straight use with no
    # LLM round-trip. This is a genuine continuation.
    decision = router.route("alice", "Orion latency is above target")
    assert decision.action == "use" and decision.wiki_id == "engineering", decision


def test_contaminated_wiki_no_longer_absorbs_unrelated_topic(env):
    """Regression: an unrelated entity previously written into a wiki (whose
    summary now carries a different topic) must not keep absorbing future
    messages about that topic -- the self-reinforcing contamination trap.

    A coffee/office message routed to a 'Q3 Azure Migration' wiki at 0.99
    because 'Maria'/'Facilities' entities with coffee summaries lived there.
    The fix: when best.matched is strong+topical but INCOHERENT with the
    wiki's own identity (title/description/topic), route to create (write)
    instead of use. A genuine continuation into a wiki that IS about that
    topic stays coherent and still routes home.
    """
    registry, store, router = env
    registry.create("Q3 Azure Migration", "alice",
                    description="Planning the Q3 Azure migration",
                    topic="Q3 Azure migration planning")
    # A proper Azure entity (coherent with the wiki identity).
    store.upsert_entity("q3-azure-migration", "project", "q3-azure-migration",
                        summary_append="Moving the customer portal to Azure in Q3.")
    # Contamination: coffee entities whose summaries carry a DIFFERENT topic,
    # simulating a prior bad write that landed coffee content in the Azure wiki.
    store.upsert_entity("q3-azure-migration", "person", "Maria",
                        summary_append="Researching coffee machine models for the office.")
    store.upsert_entity("q3-azure-migration", "organization", "Facilities",
                        summary_append="Wants to budget $300 for a replacement coffee machine.")
    store.flush()
    registry.refresh_stats("q3-azure-migration", 3,
                           ["q3-azure-migration", "Maria", "Facilities"])

    coffee = ("The office Keurig broke again. Facilities wants to budget $300 "
              "for a replacement, Maria researching models toward the Ninja.")
    decision = router.route("alice", coffee, allow_create=True)
    # Must NOT be absorbed into the Azure wiki despite the strong entity match.
    assert decision.action == "create", decision
    assert decision.wiki_id is None, decision

    # A genuine continuation into a wiki that IS about the coffee topic stays
    # coherent and still routes home -- the fix demotes only the misfit.
    registry.create("Office Coffee Station", "alice",
                    description="Office coffee and break-room equipment",
                    topic="Office coffee")
    store.upsert_entity("office-coffee-station", "person", "Maria",
                        summary_append="Researching coffee machine models for the office.")
    store.upsert_entity("office-coffee-station", "artifact", "Keurig",
                        summary_append="The office coffee machine that keeps breaking.")
    store.flush()
    registry.refresh_stats("office-coffee-station", 2, ["Maria", "Keurig"])

    coffee_followup = "Maria ordered the replacement Keurig, it arrives Friday."
    cont = router.route("alice", coffee_followup, allow_create=True)
    # Must route to the coffee wiki (use, or a query that cites it) -- never
    # the contaminated Azure wiki, and never a new-wiki void.
    assert cont.wiki_id == "office-coffee-station", cont
    assert cont.action in ("use", "query"), cont


def test_overcaptured_proper_noun_does_not_trigger_new_topic(env):
    """Regression: a genuine continuation must not route to a NEW wiki just
    because the proper-noun detector over-captures a phrase. "The Q3 Azure
    migration is on track..." is detected as proper-noun phrase "The Q3 Azure",
    which does NOT exactly match the known entity name "q3 azure migration"
    -- so an exact-phrase discovery check wrongly flags it as a new topic.
    The discovery check must compare CONTENT TOKENS (azure) against known
    names, not the whole over-captured phrase."""
    registry, store, router = env
    registry.create("Q3 Azure Migration", "alice",
                    description="Tracking the Q3 Azure migration",
                    topic="Q3 Azure migration planning")
    store.upsert_entity("q3-azure-migration", "project", "q3-azure-migration",
                        summary_append="Moving the customer portal to Azure in Q3.")
    store.upsert_entity("q3-azure-migration", "person", "Sarah Kim",
                        summary_append="Leads the Q3 Azure migration.")
    store.flush()
    registry.refresh_stats("q3-azure-migration", 2, ["q3-azure-migration", "Sarah Kim"])

    cont = "The Q3 Azure migration is on track, Sarah updated the portal timeline."
    decision = router.route("alice", cont, allow_create=True)
    assert decision.action == "use", decision
    assert decision.wiki_id == "q3-azure-migration", decision


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


def test_entities_are_stored_under_the_scope_wiki_prefix(env):
    registry, store, _ = env
    store.upsert_entity("sales", "person", "Alice Chen", summary_append="Engineer.")
    store.flush()

    keys = store.backend.list_keys("sales/wiki/")
    assert any(k.endswith("person/alice-chen.okf.md") for k in keys)
    assert not store.backend.list_keys("wikis/sales/"), "the retired layout must not be written"


def test_two_wikis_do_not_share_storage(env):
    """The isolation the whole change exists for."""
    registry, store, _ = env
    store.upsert_entity("sales", "organization", "Acme Corp")
    store.upsert_entity("engineering", "project", "Orion")
    store.flush()

    assert store.list_entities("sales") == ["organization/acme-corp"]
    assert store.list_entities("engineering") == ["project/orion"]
    assert store.get_entity("sales", "project/orion", touch=False) is None


# ---------- tag gate (option a + b: curated tags + existing topic signals) ----------

def test_tag_gate_skips_a_mismatched_winner_for_a_tag_aligned_wiki(env):
    """When the top-scoring wiki matched on an entity NAME and is even
    topically overlapping, but is TAGGED for a different topic, the router must
    SKIP it and pick the tag-aligned wiki (the 'search other wikis' behaviour).
    This is the contamination case where curated tags catch what fuzzy summaries
    miss: the Search wiki's Alice entity over-captures generic coffee vocabulary,
    so the topological guards pass — but its curated tags [search, migration]
    clearly say coffee does not belong here."""
    registry, store, router = env
    # Search-migration wiki: tagged for search. Its Alice entity's summary
    # happens to mention coffee (over-captured), so a coffee text matches it
    # topically by accident.
    registry.create("Search Migration", "alice", wiki_id="search-migration",
                    topic="search platform migration",
                    tags=["search", "migration", "platform"])
    store.upsert_entity("search-migration", "person", "Alice Chen",
                        summary_append="Engineer who wants to replace the "
                                       "office espresso machine.")
    # Coffee wiki: tagged for coffee, holds the espresso machine.
    registry.create("Office Coffee", "alice", wiki_id="office-coffee",
                    topic="office coffee machines",
                    tags=["coffee", "espresso", "breakroom"])
    store.upsert_entity("office-coffee", "artifact", "Espresso Machine",
                        summary_append="The breakroom espresso machine.")
    store.flush()
    registry.refresh_stats("search-migration", 1, ["Alice Chen"])
    registry.refresh_stats("office-coffee", 1, ["Espresso Machine"])

    text = "Alice Chen wants a new espresso machine."
    scores = router.score("alice", text)
    # The search wiki matches Alice's name AND is topical (espresso summary),
    # but is TAGGED for search -> tags_match False. The coffee wiki is tag-
    # aligned.
    search = next(c for c in scores if c.wiki_id == "search-migration")
    assert search.tags_match is False, search.to_dict()
    coffee = next(c for c in scores if c.wiki_id == "office-coffee")
    assert coffee.tags_match is True, coffee.to_dict()

    # The router must NOT absorb this into the tagged-wrong search wiki; it
    # skips it and uses the tag-aligned coffee wiki.
    decision = router.route("alice", text, role=ROLE_WRITE, allow_create=True)
    assert decision.action == "use", decision.reason
    assert decision.wiki_id == "office-coffee", decision.reason


def test_tag_gate_with_no_tag_aligned_home_proposes_new_creation(env):
    """When the only matching wiki is tagged for a different topic and no other
    wiki matches the text's tags, the text is a DIFFERENT topic — the write
    must not absorb into a tagged-wrong wiki (route create, or query for review
    rather than a blind use)."""
    registry, store, router = env
    registry.create("Search Migration", "alice", wiki_id="search-migration",
                    topic="search platform migration",
                    tags=["search", "migration"])
    store.upsert_entity("search-migration", "person", "Alice Chen",
                        summary_append="Engineer who wants to replace the "
                                       "office espresso machine.")
    store.flush()
    registry.refresh_stats("search-migration", 1, ["Alice Chen"])

    text = "Alice Chen wants a new espresso machine."
    decision = router.route("alice", text, role=ROLE_WRITE, allow_create=True)
    assert decision.action in ("create", "query"), decision.reason
    assert decision.wiki_id != "search-migration", decision.to_dict()


def test_wiki_without_tags_is_tag_neutral_and_not_skipped(env):
    """Back-compat: a wiki that has not yet been tagged must still route — the
    tag gate only bites when a wiki has tags that DISAGREE."""
    registry, store, router = env
    registry.create("Search Migration", "alice", wiki_id="search-migration",
                    topic="search platform migration")  # no tags yet
    store.upsert_entity("search-migration", "project", "Orion",
                        summary_append="Search platform migration project.")
    store.flush()
    registry.refresh_stats("search-migration", 1, ["Orion"])

    decision = router.route("alice", "the Orion search migration is behind",
                            role=ROLE_WRITE, allow_create=True)
    assert decision.action == "use", decision.reason
    assert decision.wiki_id == "search-migration"
