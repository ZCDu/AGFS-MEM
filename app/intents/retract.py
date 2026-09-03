"""Deterministic retraction/correction detection + intent completion.

WHY THIS EXISTS
    The LLM reliably emits CRUD intents for EXPLICIT mutations ("forget X",
    "delete X", "remember X"). It is genuinely unreliable for INDIRECT ones:
    "Sarah no longer leads X", "the deadline is wrong, it's October", "I
    shouldn't have said that", "take that back". In live tests the model
    replies "Got it, I'll keep that in mind" and emits NOTHING -- memory is
    silently left wrong, which is the worst failure mode.

    Relying on the prompt to force this is fragile (it did not help). So we
    add a DETERMINISTIC layer: pattern-detect the retraction/correction
    phrasings, and when present, run a focused follow-up LLM call whose ONLY
    job is to produce the precise update/delete intent. The prose reply is
    untouched; only the missing intent is computed with certainty.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("memory_backend.retract")

# Phrases that unambiguously signal a memory RETRACTION or CORRECTION -- the
# user is telling us something already stored is no longer true or was wrong.
_RETRACT_PATTERNS = [
    re.compile(r"\bno longer\b", re.I),
    re.compile(r"\b(take|scratch)\s+that\s+back\b", re.I),
    re.compile(r"\b(i )?was wrong\b", re.I),
    re.compile(r"\b(i )?shouldn'?t have (said|told|mentioned)\b", re.I),
    re.compile(r"\b(ignore|forget|erase) (the )?(last|that|previous)\b", re.I),
    re.compile(r"\bnot? (true|correct|right|accurate)\b", re.I),
    re.compile(r"\b(actually|correction[:,\s]|correct that)\b", re.I),
    re.compile(r"\btake\s+(it|that)\s+back\b", re.I),
    re.compile(r"\b(no|not)\s+(longer|anymore|any more)\b", re.I),
]

# These signal a plain new fact or question (NOT a retraction). A correction
# like "actually it's Y not X" has BOTH a correction marker AND a replacement,
# so we require a correction marker to fire, and below we still let the model
# decide the exact op when the follow-up runs.
_STRONG_RETRACT = re.compile(
    r"(no longer|was wrong|\bwrong\b|shouldn'?t have|take it back|take that back|"
    r"take back|not true|not correct|erase the|forget the|forgot the|forget about|scratch that|"
    r"isn'?t true|last thing i said|\bwrong,|\bnot.*true\b|"
    r"remove the claim that|remove the fact that|delete the claim that|delete the fact that)", re.I)


def is_retraction(text: str) -> bool:
    """True when the user's message clearly retracts or corrects stored info."""
    if not text:
        return False
    # A strong signal anywhere decides it.
    if _STRONG_RETRACT.search(text):
        return True
    # Otherwise require at least two weaker signals to avoid false positives
    # on ordinary talk ("that's not right" in a non-memory context).
    hits = sum(1 for p in _RETRACT_PATTERNS if p.search(text))
    return hits >= 2


# A bare destructive request: "delete/forget/remove X from memory", "erase X".
# Must NOT fire on generic objects ("delete the file", "remove the post") -
# those are not memory deletions and this route must not intercept them.
_DELETE_VERB = re.compile(
    r"\b(?:delete|remove|forget|drop|erase)\s+(?:the\s+)?"
    r"(?:entity|record|person|node|entry|memory|everything\s+about|all\s+(?:info|information|data)\s+on|info|information|data\s+on)?"
    r"\s*[A-Z][A-Za-z' .-]{1,40}"
    r"(?:\s+(?:from\s+(?:memory|the\s+memory|the\s+graph|my\s+memory)))?",
    re.I)


def is_delete_request(text: str) -> bool:
    """True for a bare destructive instruction ("delete sam whitfield",
    "forget about the marina project") rather than a correction/retraction.
    Distinct from is_retraction so a plain delete never rides the
    new-topic/proposal path and never fabricates a junk wiki."""
    if not text:
        return False
    if not _DELETE_VERB.search(text):
        return False
    # A delete verb pointing at a generic artifact is NOT a memory delete.
    low = text.lower().strip()
    for gen in ("file", "attachment", "post", "message", "photo", "image",
                "account", "email", "task", "row", "entry row"):
        if re.search(rf"\b(?:delete|remove|forget|drop|erase)\s+(?:the\s+)?{gen}\b", low):
            return False
    return True


# Affirmative/negative replies to a staged-deletion prompt. These are checked
# only after the assistant has asked "Forget is pending your confirmation: X".
_CONFIRM_YES = re.compile(
    r"\b(?:yes|yeah|yep|yup|sure|ok(?:ay)?|go ahead|do it|confirm|proceed|"
    r"delete|remove|forget|yeah delete|yes delete)\b",
    re.I)
_CONFIRM_NO = re.compile(
    r"\b(?:no|nope|nah|cancel|stop|abort|never mind|keep|keep it|"
    r"don't|dont|do not|leave|don't delete|do not delete|leave it)\b",
    re.I)


def confirmations_resolved(user_id: str, text: str, store,
                           max_pending: int = 5) -> dict | None:
    """Resolve a follow-up reply into a confirm/cancel action for a staged
    deletion, or None when the reply is not a clear decision.

    Looks only at pending deletions the user actually has. A message must be
    unambiguously yes (deletion) or no (keep it) to count; otherwise None so
    the caller falls through to the normal reply.
    Returns {"pending_id", "confirm": bool, "entity": str, "entity_label": str}
    or None.
    """
    if not text or not text.strip():
        return None
    low = text.strip().lower()
    yes = bool(_CONFIRM_YES.search(low))
    no = bool(_CONFIRM_NO.search(low))
    # Ambiguous (both keywords, or neither) -> not a clear decision.
    if yes == no:
        return None
    try:
        from app.intents.semantica_crud import list_pending_deletions
        pending = list_pending_deletions(store.backend, user_id)
    except Exception:
        return None
    if not pending:
        return None
    # Prefer a pending record whose entity name the user mentions; else the
    # most recent (last staged) one.
    target = None
    for p in pending[:max_pending]:
        label = (p.get("entity_title") or "").lower()
        eid = (p.get("entity") or "").lower()
        if label and label in low or (eid and eid in low):
            target = p
            break
    if target is None:
        target = pending[0]
    return {
        "pending_id": target.get("pending_id"),
        "confirm": yes,
        "entity": target.get("entity"),
        "entity_label": target.get("entity_title") or target.get("entity"),
    }


RETRACT_PROMPT = """\
(removed -- retraction resolution now handled by semantica_crud)
"""


def completion_intent_from_reply(message: str, context: str, llm) -> list[dict]:
    """DEPRECATED. Retraction intents are now produced by semantica_crud
    (structure_claim + apply_semantica_conflict). Kept only so old imports
    do not hard-fail; returns [].
    """
    return []
