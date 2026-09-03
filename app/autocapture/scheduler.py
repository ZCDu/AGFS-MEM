"""Automated daily memory capture.

This is the "instead of a button click" path. Where the UI's review-extraction
flow requires someone to read a plan and press Apply, this scheduler runs the
SAME pipeline on a timer: it reads a day's journaled conversations (the
session transcripts), and for each one runs assessor -> LLM extract -> route ->
apply, exactly as POST /extract?apply=true would.

The routing step is the same one that respects the "no personal/demo dump
wiki" rule: genuinely new topics get a properly-named topic wiki, and
continuations land back in the wiki they belong to. Nothing here bypasses the
routing/apply logic; it only supplies the trigger the button used to supply.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time as _time
from dataclasses import dataclass, field

from datetime import date, datetime, timedelta, timezone

from app.rawlog.sessions import SessionLog
from app.storage.backend import ConflictError, StorageBackend
from app.wikis.registry import (ROLE_WRITE, WikiError, WikiRegistry,
                                slugify_wiki)
from app.wikis.router import WikiRouter

logger = logging.getLogger("memory_backend.autocapture")

# Where the cursor (which date:session has already been processed) lives.
_CURSOR_KEY = "autocapture/cursor.json"

# Cross-process lock so two overlapping capture runs can never both extract
# the same session at once. `uvicorn --reload` can leave an old worker alive
# for a moment while spawning a new one; both run AutoCaptureTimer's startup
# backfill, and AUTO_CAPTURE_BACKFILL_DAYS covers "today" by design. Without
# a lock, two concurrent runs each read "which facts already exist" before
# the other's write lands, so both add the same fact -- this is exactly how
# one claim ended up stored two or three times in production (2026-09-02).
# The dedup logic inside a single run is correct; nothing protected across
# two runs racing on the same entity, which is what this closes.
_LOCK_KEY = "autocapture/lock.json"
_LOCK_TTL_SECONDS = 1800  # generous: comfortably longer than any real run


def _acquire_lock(backend: StorageBackend) -> str | None:
    """Take the lock via a conditional write (if_match), which is atomic even
    across processes/hosts. Returns a token to release with, or None if
    another run currently holds a live (non-expired) lock."""
    now = _time.time()
    token = secrets.token_hex(8)
    payload = json.dumps({"token": token, "expires_at": now + _LOCK_TTL_SECONDS,
                          "pid": os.getpid()}).encode("utf-8")
    try:
        backend.put_bytes(_LOCK_KEY, payload, if_match="")
        return token
    except ConflictError:
        pass

    current = backend.get_bytes(_LOCK_KEY)
    if current is None:
        try:
            backend.put_bytes(_LOCK_KEY, payload, if_match="")
            return token
        except ConflictError:
            return None
    try:
        holder = json.loads(current.data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        holder = {}
    if float(holder.get("expires_at", 0)) > now:
        return None  # someone else is actively capturing; skip this pass
    try:
        # Lock expired (holder crashed mid-run) -- steal it.
        backend.put_bytes(_LOCK_KEY, payload, if_match=current.etag)
        return token
    except ConflictError:
        return None  # lost the race to steal it; the winner runs, we skip


def _release_lock(backend: StorageBackend, token: str) -> None:
    current = backend.get_bytes(_LOCK_KEY)
    if current is None:
        return
    try:
        holder = json.loads(current.data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return
    if holder.get("token") == token:
        backend.delete(_LOCK_KEY)


class Cursor:
    """Tracks which sessions have been captured so a run never stores the
    same transcript twice.

    Records, per session marker, the MESSAGE COUNT that was seen when it was
    captured. A session whose transcript has since GROWN (new messages
    appended) has a stale count, so it is re-scanned on the next run — but
    only its unexamined tail is processed, thanks to the per-message
    `examined` tracking. An unchanged session is skipped wholesale.

    In-memory set, persisted to the object store at the start of each run so
    a process restart does not re-process everything: the in-memory cache is
    seeded from the stored file, and any new run's additions are written back
    before the run ends.
    """

    def __init__(self, backend: StorageBackend):
        self._backend = backend
        self._counts: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        raw = self._backend.get_bytes(_CURSOR_KEY)
        if raw is None:
            return
        try:
            data = json.loads(raw.data.decode("utf-8"))
            processed = data.get("processed", [])
            if isinstance(processed, list):
                # Legacy cursor: a bare list of markers, counts unknown (-1
                # means "seen but count unknown"). Preserve it so already-
                # captured sessions stay skipped.
                self._counts = {k: -1 for k in processed}
            else:
                self._counts = {str(k): int(v) for k, v in processed.items()}
        except (json.JSONDecodeError, TypeError):
            logger.warning("autocapture cursor at %s is unreadable; starting empty",
                           _CURSOR_KEY)

    def _save(self) -> None:
        payload = json.dumps({"version": 2, "processed": dict(sorted(self._counts.items()))},
                             ensure_ascii=False).encode("utf-8")
        self._backend.put_bytes(_CURSOR_KEY, payload)

    def done_with(self, key: str, message_count: int) -> bool:
        """True when this session was captured before with the SAME (or more)
        messages. A session that has grown beyond the recorded count is NOT
        done: its new messages must be examined."""
        seen = self._counts.get(key)
        if seen is None:
            return False
        if seen == -1:
            return True  # legacy: count unknown, keep it skipped
        return seen >= message_count

    def record(self, key: str, message_count: int) -> None:
        self._counts[key] = message_count

    def count(self, key: str) -> int | None:
        return self._counts.get(key)

    def persist(self) -> None:
        self._save()


@dataclass
class CaptureReport:
    """Result of a capture run, for the manual trigger endpoint and tests."""
    processed_sessions: int = 0
    stored: int = 0
    skipped: int = 0
    failed: int = 0
    per_session: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "processed_sessions": self.processed_sessions,
            "stored": self.stored,
            "skipped": self.skipped,
            "failed": self.failed,
            "per_session": self.per_session,
        }


def _users_with_sessions(backend: StorageBackend) -> list[str]:
    """User ids that appear as session-log owners.

    We cannot enumerate "every account" reliably when AUTH_MODE=off (there is
    no account store), but we can find every user that has journalled
    sessions by listing the sessions prefix. A user with no sessions has
    nothing to capture anyway.
    """
    user_ids: set[str] = set()
    for key in backend.list_keys(""):
        # Session keys are {user_id}/sessions/... (see rawlog/sessions.py).
        if "/sessions/" not in key:
            continue
        parts = key.split("/")
        if len(parts) >= 3 and parts[1] == "sessions" and parts[0]:
            user_ids.add(parts[0])
    return sorted(user_ids)


def _session_marker(user_id: str, day: date, session_id: str) -> str:
    return f"{user_id}|{day.isoformat()}|{session_id}"


def capture_day(backend: StorageBackend, target: date | None = None,
                user_ids: list[str] | None = None,
                extractor=None) -> CaptureReport:
    """Extract and apply memory for a day's journal across all (or given) users.

    `target` defaults to today. When `user_ids` is given, only those users are
    scanned (used by the manual endpoint and tests); otherwise every user with
    session logs is scanned. Idempotent: a given date:session is captured at
    most once.

    `extractor` is an optional ConversationExtractor override (tests inject a
    stub-backed one without an API key); it defaults to the process singleton
    built from settings.

    Locked (see `_acquire_lock`): if another process is already running a
    capture, this call returns an empty report immediately rather than racing
    it. A skipped pass here is not lost -- the cursor makes the next call
    (the next reload, the next scheduled fire) pick up whatever the other run
    did not already cover.
    """
    from app.deps import get_extractor
    from app.extract.extractor import ConversationExtractor
    from app.graph.store import EntityGraphStore

    lock_token = _acquire_lock(backend)
    if lock_token is None:
        logger.info("autocapture: another run holds the lock; skipping this pass")
        return CaptureReport()

    try:
        target = target or datetime.now(timezone.utc).date()
        cursor = Cursor(backend)
        if extractor is None:
            extractor = get_extractor()
        store: EntityGraphStore = extractor.store
        registry = WikiRegistry(backend)
        router = WikiRouter(registry, store)
        session_log = SessionLog(backend)

        if user_ids is None:
            user_ids = _users_with_sessions(backend)
        elif isinstance(user_ids, str):
            user_ids = [user_ids]

        report = CaptureReport()
        for user_id in user_ids:
            try:
                summaries = session_log.list_day(user_id, target)
            except Exception as e:
                logger.warning("autocapture could not list sessions for %s: %s",
                               user_id, e)
                report.failed += 1
                continue

            for s in sorted(summaries, key=lambda x: x.get("started_at", "")):
                session_id = s.get("session_id")
                message_count = s.get("messages", 0)
                if not session_id or message_count == 0:
                    continue
                marker = _session_marker(user_id, target, session_id)
                if cursor.done_with(marker, message_count):
                    report.skipped += 1
                    continue

                report.processed_sessions += 1
                rec = _capture_one(extractor, router, registry, session_log,
                                   store, user_id, target, session_id)
                report.per_session.append(rec)
                status = rec.get("status")
                if status == "stored":
                    report.stored += 1
                elif status == "failed":
                    report.failed += 1
                else:  # no-op: nothing durable, or already-known
                    report.skipped += 1
                # Remember how many messages this session had when we examined
                # it. If the session later grows, the stale count triggers a
                # rescan of ONLY the new, unexamined messages.
                cursor.record(marker, message_count)

        cursor.persist()
        return report
    finally:
        _release_lock(backend, lock_token)


def backfill(backend: StorageBackend, days: int = 7, extractor=None,
             through=None) -> list[CaptureReport]:
    """Capture every day in the trailing window (oldest -> newest) once.

    Used on startup so days the service was down get caught up automatically.
    `days` is how many trailing days to scan (default 7); `through` defaults to
    today. Each day is captured via capture_day(), which is idempotent: the
    cursor skips anything already processed, so backfill can never store a
    transcript twice, even across restarts. Returns one report per processed
    day (oldest first) for logging/tests.
    """
    through = through or datetime.now(timezone.utc).date()
    reports: list[CaptureReport] = []
    for n in range(days, -1, -1):
        day = through - timedelta(days=n)
        try:
            report = capture_day(backend, target=day, extractor=extractor)
        except Exception as e:
            logger.warning("autocapture backfill failed for %s: %s", day, e)
            report = CaptureReport()
            report.failed += 1
        reports.append(report)
    return reports


def _capture_one(extractor, router, registry, session_log, store,
                 user_id: str, day: date, session_id: str) -> dict:
    """Assess + route + extract + apply the UNEXAMINED parts of ONE session.

    Mirrors what POST /extract?apply=true does: the assessor gates the LLM,
    the route targets a (possibly new, properly-named) topic wiki, and apply
    writes the operations. Returns a per-session record for the report.

    Only the messages NOT yet flagged ``examined`` are examined here, so the
    timer never re-processes parts of a conversation a human already reviewed
    and applied (or that an earlier capture already handled). After each run
    is attempted -- stored, no-op, or failed -- its messages are marked
    examined so the next run starts from where this one left off.

    A single session can genuinely span several DISJOINT topics (someone
    pastes two unrelated fact dumps in one conversation). Routing the whole
    concatenated transcript as one unit lets a long dominant topic absorb a
    short disjoint one that follows -- both dumps land in the first dump's
    wiki. Before committing to one routing decision we cheaply segment the
    session by per-message routing and capture each topic-run separately.
    """
    try:
        unexamined = session_log.read_unexamined(user_id, session_id, day=day)
        if not unexamined:
            return {"session_id": session_id, "status": "no-op",
                    "detail": "nothing unexamined"}
        if not any(m.get("role") == "user" for _, m in unexamined):
            return {"session_id": session_id, "status": "no-op",
                    "detail": "empty transcript"}

        runs = _topic_runs(router, store, user_id, unexamined)
        if len(runs) > 1:
            # Session spans multiple topics: capture each run independently so
            # one dominant topic cannot absorb another. Each attempted run's
            # messages are marked examined as it completes.
            outcomes = []
            for indexes, sub in runs:
                text = _render(sub)
                outcomes.append(_capture_blob(
                    extractor, router, registry, store, user_id, day,
                    session_id, text))
                session_log.mark_examined(user_id, session_id, indexes,
                                          day=day, by="autocapture")
            return _merge_run_outcomes(session_id, outcomes)

        # Single topic (the common case): capture the whole unexamined stretch
        # as one, then mark it examined.
        indexes, messages = runs[0] if runs else (
            [i for i, _ in unexamined], [m for _, m in unexamined])
        text = _render(messages)
        outcome = _capture_blob(extractor, router, registry, store, user_id,
                                day, session_id, text)
        session_log.mark_examined(user_id, session_id, indexes,
                                  day=day, by="autocapture")
        return outcome
    except Exception as e:
        logger.warning("autocapture failed for session %s (%s): %s",
                       session_id, user_id, e)
        return {"session_id": session_id, "status": "failed", "detail": str(e)}


def _render(messages: list[dict]) -> str:
    """Session messages to the transcript shape the extractor reads --
    USER messages only.

    The assistant's own reply used to be included too, on the theory that
    it might restate something worth capturing. In practice this backfired
    two ways, both traced back to feeding it into the SAME extraction pass
    as the user's original: (1) when the assistant restates a claim from
    the user's message, the LLM's extraction can emit it twice -- once from
    each speaker -- producing duplicate facts; (2) when the user pastes a
    large, detailed document and the assistant replies with its own
    condensed summary of it, the extractor has been observed gravitating
    toward the shorter, already-distilled assistant text instead of doing
    its own extraction from the richer original -- a 7,000-character
    Chinese source with ~25 named people and 15 decisions produced only the
    ~6 bullets of the assistant's English recap, in English, with the rest
    of the source's content simply never reaching the graph.

    The assistant never introduces information the user did not already
    provide -- it is a response TO the session, not a source of new
    real-world facts -- so dropping it here has no capture cost and
    resolves both failure modes at once. (Message INDEXES for
    mark_examined bookkeeping still cover every message, assistant
    included, so nothing gets scanned twice; only the rendered TEXT
    changes.)"""
    lines = []
    for m in messages:
        if m.get("role") != "user":
            continue
        content = str(m.get("content", ""))
        if content.strip():
            lines.append(f"User: {content}")
    return "\n\n".join(lines)


def _topic_runs(router, store, user_id: str,
               indexed: list[tuple[int, dict]]) -> list[tuple[list[int], list[dict]]]:
    """Split a session's UNEXAMINED messages into contiguous runs that each
    belong to one topic (one destination wiki). Each return value is
    ``(indexes, messages)`` where ``indexes`` are the messages' positions in
    the session file (used to mark them examined after processing).

    Cheap and deterministic: user messages are grouped by whether they talk
    about the SAME named subjects. A user message that names a set of people /
    projects / orgs entirely disjoint from everything before it starts a new
    run. This catches a real multi-topic session (two unrelated fact dumps ->
    two runs -> two wikis) while leaving a normal single-topic conversation as
    one run.

    Names come from the assessor's proper-noun detector (no LLM). Because the
    detector over-captures, a message that merely echoes a prior name ("when
    does Corinth's launch start?") shares a name with its run and stays put;
    a fragment with no new names ("and what about the deadline?") is glued to
    the run in progress rather than splitting it.

    The routing destination is used as a secondary signal: when a message
    already routes to an EXISTING wiki that differs from the current run's
    home, it is a different topic even if the cheap name test is ambiguous.
    """
    from app.verify.assessor import _proper_noun_phrases

    # Calendar and generic temporal words that the proper-noun detector
    # over-captures but that are NOT entities: "September", "Tuesday",
    # "October", "Monday". Two unrelated dumps both mentioning "September"
    # must NOT be treated as the same subject. Only multi-word proper nouns
    # (real names like "Marcus Hale", "Iron Foundation") and non-calendar
    # singletons count as topic identity.
    _CALENDAR = frozenset(
        """january february march april may june july august september october
        november december monday tuesday wednesday thursday friday saturday
        sunday spring summer autumn fall winter q1 q2 q3 q4 today tomorrow
        yesterday weekend weekday morning afternoon evening tonight""".split())

    def names(text: str) -> set[str]:
        """Real topic-identity nouns in a message: multi-word proper nouns and
        non-calendar single words. Calendar/temporal singletons are dropped so
        they cannot glue unrelated topics together."""
        out = set()
        for p in _proper_noun_phrases(text or ""):
            parts = [w.lower() for w in p.split()]
            if len(parts) > 1:
                out.add(p.lower())  # a real multi-word name
            elif parts and parts[0] not in _CALENDAR:
                out.add(parts[0])  # a real single-word name (org/person)
        return out

    def dest(text: str) -> str | None:
        """The existing wiki this message would route to, if any."""
        text = (text or "").strip()
        if not text:
            return None
        try:
            d = router.route(user_id, text, role="write", allow_create=False)
        except Exception:
            return None
        if d.action == "use" and d.wiki_id:
            return d.wiki_id
        if d.action == "query" and d.wiki_id:
            return d.wiki_id
        return None

    runs: list[tuple[list[int], list[dict]]] = []
    current: list[tuple[int, dict]] = []  # (index, message)
    current_names: set[str] = set()
    current_dest: str | None = None

    def close():
        nonlocal current, current_names, current_dest
        if current:
            runs.append(([i for i, _ in current], [m for _, m in current]))
        current, current_names, current_dest = [], set(), None

    for idx, m in indexed:
        if m.get("role") != "user":
            if current:
                current.append((idx, m))
            continue
        text = str(m.get("content", "")).strip()
        nm = names(text)
        d = dest(text)

        if not current:
            current = [(idx, m)]
            current_names = nm
            current_dest = d
            continue

        # A clear different-destination signal overrides the name test.
        if (current_dest and d and d != current_dest):
            close()
            current = [(idx, m)]
            current_names = nm
            current_dest = d
            continue

        # No serious names (conversational filler) -> stay with the run.
        real = {w for w in nm if len(w) > 2}
        if real and current_names and not (real & current_names):
            # Genuinely disjoint named subjects -> new topic.
            close()
            current = [(idx, m)]
            current_names = nm
            current_dest = d
            continue

        # Same subjects (or no new subjects): continuation.
        current.append((idx, m))
        current_names |= nm
        if d:
            current_dest = d

    close()
    if runs:
        return runs
    # Nothing user-bearing (defensive): treat the whole indexed input as one run.
    return [([i for i, _ in indexed], [m for _, m in indexed])]


def _capture_blob(extractor, router, registry, store, user_id, day, session_id,
                  text: str) -> dict:
    """Route + plan + materialise + apply one topic blob (a whole single-topic
    session, or one run of a split multi-topic session). Mirrors the interactive
    /extract?apply=true path."""
    try:
        if not text.strip():
            return {"session_id": session_id, "status": "no-op",
                    "detail": "empty transcript"}
        # CONTENT GATE: run the cheap assessor BEFORE routing. A blob of
        # conversational filler (a bare "yes, delete it", "thanks", an
        # affirmation/confirmation echoing a staged deletion, a single
        # calendar word) has no durable memory content. If we routed first
        # with allow_create=True, such a message would spin up a junk wiki
        # ("deletion request", "affirmation request", ...) that holds
        # nothing. The interactive /extract path gates on the assessor
        # before the model; autocapture must gate before the ROUTER, which is
        # what fabricates the empty wiki. Only when the assessor judges this
        # to be durable/review-worthy content do we bother routing.
        try:
            _gate = extractor.assessor.assess(user_id, text)
            _allowed = {"store"} if extractor.min_decision == "store" else {"store", "review"}
            if extractor.min_decision != "store" and _gate.decision not in _allowed:
                return {"session_id": session_id, "status": "no-op",
                        "detail": _gate.decision or "nothing durable",
                        "assessor": _gate.decision,
                        "reasons": _gate.reasons[:3]}
        except Exception:
            pass  # gate is best-effort; a failure must not block capture
        target, reason, pending = _resolve_target_for(
            router, registry, store, user_id, text)
        plan = extractor.plan(target, text,
                              evidence=f"session:{day.isoformat()}:{session_id}",
                              pending_new_wiki=pending)
        if plan.target_wiki:
            target = plan.target_wiki

        if not plan.operations:
            return {"session_id": session_id, "status": "no-op",
                    "detail": plan.decision or "nothing durable",
                    "assessor": plan.assessment.get("decision")}

        if pending is not None and plan.new_wiki_proposal is not None:
            made = _materialize(registry, user_id, plan.new_wiki_proposal)
            if made:
                # The new-topic wiki was created (possibly under a disambiguated
                # slug); apply must target THAT wiki, not the provisional slug.
                target = made

        store.set_actor(user_id)
        plan = extractor.apply(target, plan)
        applied = sum(1 for o in plan.operations if o.status == "applied")
        _refresh_wiki_stats(registry, store, target)
        # OPTION A IDENTITY: on a wiki's first populated write, lock its
        # identity to the entities it now holds. From then on "continue" is
        # only allowed when content touches one of these; other content starts
        # a new wiki. This is authoritative, set once, never re-expanded.
        if applied:
            try:
                wiki = registry.get(target)
                if wiki is not None and not wiki.identity_set:
                    registry.set_identity(target, store.list_entities(target))
                # Backfill curated tags for any new wiki that reached this
                # write without them (see routes_extract._ensure_wiki_tags).
                if (wiki is not None and not wiki.tags
                        and (wiki.topic or wiki.title)):
                    from app.verify.assessor import _content_tokens
                    tags = sorted(_content_tokens(
                        f"{wiki.title} {wiki.topic}"))[:8]
                    if tags:
                        try:
                            registry.set_tags(target, tags)
                        except Exception:
                            logger.warning("could not set tags for %s", target,
                                           exc_info=True)
            except Exception:
                logger.warning("could not set identity for %s", target,
                               exc_info=True)
        failed = [o.to_dict() for o in plan.operations if o.status == "failed"]
        pending = [o.to_dict() for o in plan.operations
                  if o.status == "needs_confirmation"]
        # A retraction/correction found in an autocaptured transcript can
        # stage a destructive deletion for confirmation (see
        # ConversationExtractor.apply()) instead of applying it outright --
        # autocapture runs unattended, so nothing here can answer a
        # confirmation prompt. If that staging is the ONLY thing this session
        # produced, report it plainly rather than as "stored" with nothing
        # actually stored.
        status = "stored" if applied else ("needs_confirmation" if pending else "stored")
        return {
            "session_id": session_id,
            "status": status,
            "target_wiki": target,
            "reason": reason,
            "applied": applied,
            "new_wiki": (plan.new_wiki_proposal or {}).get("title"),
            "failed": failed,
            "pending_deletions": pending,
        }
    except Exception as e:
        logger.warning("autocapture capture failed for session %s: %s",
                       session_id, e)
        return {"session_id": session_id, "status": "failed", "detail": str(e)}


def _merge_run_outcomes(session_id: str, outcomes: list[dict]) -> dict:
    """Collapse per-run capture outcomes into one per-session record."""
    stored = [o for o in outcomes if o.get("status") == "stored"]
    failed = [o for o in outcomes if o.get("status") == "failed"]
    noops = [o for o in outcomes if o.get("status") == "no-op"]
    if stored:
        first = stored[0]
        return {
            "session_id": session_id,
            "status": "stored",
            "target_wiki": first.get("target_wiki"),
            "reason": "; ".join(i.get("reason") for i in stored),
            "applied": sum(i.get("applied", 0) for i in stored),
            "runs": len(outcomes),
            "new_wiki": first.get("new_wiki"),
            "failed": [f for i in outcomes for f in i.get("failed", [])],
        }
    if failed and not noops:
        return {"session_id": session_id, "status": "failed", "detail":
                "; ".join(f.get("detail") for f in failed)}
    return {"session_id": session_id, "status": "no-op", "detail":
            "; ".join(o.get("detail", o.get("status", "")) for o in outcomes)}


def _resolve_target_for(router: WikiRouter, registry: WikiRegistry,
                        store, user_id: str, text: str):
    """Which wiki a captured session writes to.

    Single-wiki deployment: always the user's own home wiki (wiki_id ==
    user_id), created on first use if needed. Mirrors
    routes_extract._resolve_target -- no topic routing, no candidate wikis,
    nothing proposed as a separate page. `router` is accepted for signature
    compatibility with callers but no longer consulted.
    """
    home = registry.ensure_home(user_id)
    if home is None:
        raise _AmbiguousTarget("none", f"Could not create the home wiki for {user_id!r}.")
    return home.wiki_id, "single wiki: user's own wiki", None


class _AmbiguousTarget(Exception):
    def __init__(self, action: str, reason: str):
        self.action = action
        self.reason = reason
        super().__init__(f"autocapture could not decide a wiki (action={action}): {reason}")


def _refresh_wiki_stats(registry, store, wiki_id: str) -> None:
    """Keep the registry's cached entity_count/sample_entities current after
    an autocapture apply writes entities. Best-effort: a stale hint only
    routes/summarises worse; it must never fail the capture.
    """
    try:
        ids = store.list_entities(wiki_id)
        registry.refresh_stats(
            wiki_id, len(ids),
            [e.title for e in store.manifest.list_entries(wiki_id)[:25]])
    except Exception:
        logger.warning("could not refresh wiki stats for %s", wiki_id,
                       exc_info=True)


def _materialize(registry, user_id: str, proposal: dict) -> None:
    """Create the properly-named NEW wiki on apply. Mirrors
    _materialize_new_wiki in routes_extract.py (kept local so the scheduler
    does not import the FastAPI route module's private helpers for side
    effects)."""
    title = (proposal.get("title") or "").strip() \
        or (proposal.get("provisional_title") or "").strip() or "New Wiki"
    slug = slugify_wiki(title)
    try:
        registry.create(title, created_by=user_id,
                        description=proposal.get("description") or "",
                        wiki_id=slug,
                        topic=proposal.get("topic") or "",
                        tags=proposal.get("tags") or None)
        logger.info("autocapture materialised new wiki %r", title)
        return slug
    except WikiError as e:
        # The slug already names an existing wiki. Reuse it ONLY if that wiki
        # is NOT archived (it is the same topic, so merging is correct); an
        # archived wiki must never receive new content. If archived, give the
        # new topic its own distinct wiki by disambiguating the slug.
        existing = registry.get(slug)
        if existing is not None and not existing.archived:
            logger.info("autocapture wiki %r already exists (active); reusing", slug)
            return slug
        # Archived or otherwise blocked: create a distinct, non-colliding wiki.
        disamb = slug
        n = 2
        while registry.get(disamb) is not None:
            disamb = f"{slug}-{n}"
            n += 1
        logger.info("autocapture wiki %r is archived/blocked; creating %r",
                    slug, disamb)
        registry.create(title, created_by=user_id,
                        description=proposal.get("description") or "",
                        wiki_id=disamb, topic=proposal.get("topic") or "",
                        tags=proposal.get("tags") or None,
                        allow_similar=True)
        return disamb
