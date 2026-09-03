"""
The wiki registry: which knowledge graphs exist, and who may touch them.

WHY WIKIS ARE NOT NESTED UNDER A USER
    Storage was `{user_id}/wiki/...`, so a credential bound to one user could
    never reach anything else. That is right for a personal tool and wrong for
    an organisation, where the point is that the whole Sales team reads one
    Sales graph.

    Wikis now live at `wikis/{wiki_id}/...` with an explicit access list, and
    authentication answers "who is this?" rather than "which prefix may they
    have?". Sharing becomes a grant instead of an accident of naming.

WHY AUTO-CREATION NEEDS GUARDRAILS
    The system creates a wiki when the router decides none fits. Left alone
    that produces sprawl — "Sales", "Sales Team", "sales-2026" — and a
    fragmented graph is worse than a large one, because the connections that
    make it a graph fall across the split.

    So creation goes through the same normalisation the entity slugs use, and
    a proposed name that collides with an existing wiki under a looser
    comparison is REFUSED with the existing one returned instead. The router
    can then use it rather than making a near-duplicate.

WHAT THE REGISTRY IS NOT
    Not a search index over wiki contents. It holds one small record per wiki
    — title, description, counts, a handful of representative entities — so
    routing can score every wiki from a single object read rather than loading
    each wiki's manifest.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from app.storage.backend import StorageBackend

logger = logging.getLogger("memory_backend.wikis")

REGISTRY_KEY = "wikis/_registry.json"

# ---- short-term (in-memory) registry cache ----
#
# Every request builds a fresh WikiRegistry, and _read() used to fetch the
# whole registry from S3 every time — a ~1s qiniu GET on a service that needs
# sub-ms reads. That defeated the STM/LTM split: the registry (small, hot,
# read constantly for routing and listing) should never go to the object
# store on every request.
#
# The cache lives ON THE BACKEND (like the store's manifest/ops-log buffers),
# so its lifetime tracks the backend, not the per-request WikiRegistry. In the
# running service there is one backend singleton, so one shared cache; in
# tests each backend is isolated, so caches cannot leak across tests.
#
# Design: a small TTL-bounded snapshot cache. Reads serve from memory when the
# snapshot is younger than the TTL; writes invalidate immediately so a
# mutation in this process is visible on the next read. A short TTL (2s)
# bounds cross-process staleness for the (documented) multi-writer case.
import threading

_REGISTRY_CACHE_ATTR = "_registry_cache"
_REGISTRY_CACHE_LOCK = threading.Lock()
_REGISTRY_CACHE_TTL = 2.0  # seconds


def _registry_cache(backend: StorageBackend) -> dict:
    """Get-or-create the per-backend cache slot: (cached_wikis, fetched_when)."""
    existing = getattr(backend, _REGISTRY_CACHE_ATTR, None)
    if existing is None:
        with _REGISTRY_CACHE_LOCK:
            existing = getattr(backend, _REGISTRY_CACHE_ATTR, None)
            if existing is None:
                existing = {"snapshot": None}  # -> (wikis: dict, when: float)
                setattr(backend, _REGISTRY_CACHE_ATTR, existing)
    return existing


# Same shape as entity slugs, for the same reason: this goes straight into an
# object key.
_WIKI_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

ROLE_READ = "read"
ROLE_WRITE = "write"
ROLE_ADMIN = "admin"
_ROLE_RANK = {ROLE_READ: 1, ROLE_WRITE: 2, ROLE_ADMIN: 3}


class WikiError(ValueError):
    pass


class WikiNotFound(WikiError):
    pass


class WikiAccessDenied(WikiError):
    pass


class WikiNameCollision(WikiError):
    """A proposed name is too close to an existing wiki.

    Carries the existing id so the caller — usually the router deciding
    whether to auto-create — can use it instead of making a near-duplicate.
    """

    def __init__(self, proposed: str, existing_id: str, existing_title: str):
        self.proposed = proposed
        self.existing_id = existing_id
        self.existing_title = existing_title
        super().__init__(
            f"{proposed!r} is too close to the existing wiki {existing_title!r} "
            f"({existing_id}). Use that one, or choose a clearly distinct name.")


def slugify_wiki(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    if slug:
        return slug[:64]
    # CJK / non-Latin-only title (e.g. Chinese, Japanese, Korean): collapsing
    # the script to "" made every Chinese-authored wiki attempt an empty id
    # -> "Invalid wiki id ''". No transliterator is installed, so derive a
    # deterministic, readable-enough, valid latin slug from the codepoints of
    # the first few non-ASCII characters. Stable across calls (same text ->
    # same id) and 1-64 chars, so it passes the wiki-id validator.
    cjk = [c for c in title.strip() if ord(c) > 127 and not c.isspace()]
    if cjk:
        head = cjk[:4]
        code = "-".join(f"{ord(c):x}" for c in head)
        import hashlib as _hl
        h = _hl.sha1(title.encode("utf-8")).hexdigest()[:8]
        raw = f"cjk-{code}-{h}"
        return re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")[:64] or "cjk"
    return ""


def _match_key(title: str) -> str:
    """Loose comparison form for collision detection.

    Drops words that carry no distinguishing meaning in an organisation, so
    "Sales", "Sales Team" and "The Sales Wiki" collapse together. Deliberately
    looser than the slug: the cost of a false collision is one clarifying
    question, while the cost of a missed one is a permanently split graph.
    """
    noise = {"the", "a", "an", "team", "group", "dept", "department", "wiki",
             "notes", "docs", "project", "org", "division", "unit"}
    words = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in noise]
    return " ".join(sorted(words))


@dataclass
class WikiMeta:
    wiki_id: str
    title: str
    description: str = ""
    created_at: str = ""
    created_by: str = ""
    updated_at: str = ""
    entity_count: int = 0
    # Graph-kind model. None = a standalone org/shared wiki. "home" = a
    # user's personal home wiki (wiki_id == user_id). "topic" = a topic
    # sub-graph physically nested under a user's home wiki (a 
    # The LLM-summarized abstract topic of this wiki's content, e.g. "SRE
    # on-call rotation planning". Hidden from the user-facing list; used by
    # the topic system to decide whether a new message is close enough to
    # belong here or different enough to warrant its own wiki.
    topic: str = ""
    # Explicit, curated topic labels for this wiki, e.g. ["search",
    # "migration", "infra"]. POPULATED BY THE LLM when the wiki is created /
    # first written (alongside `topic`), refreshed opportunistically. The
    # router uses them as a deterministic TAG GATE: if the incoming text's
    # key points share no tag with a candidate, the candidate is ``tags_match``
    # False and the router skips it in favour of other wikis (or a new topic).
    # Unlike the fuzzy summary-vocabulary guards, tags are a curated,
    # debuggable statement of what the wiki is ABOUT.
    tags: list[str] = field(default_factory=list)
    # A few representative entity titles, refreshed on write. Lets the router
    # score a wiki without loading its manifest.
    sample_entities: list[str] = field(default_factory=list)
    # Option A identity: the entity slugs created when this wiki was first
    # populated. "Continue" is ONLY allowed when incoming content mentions at
    # least one of these; content touching none of them starts a NEW wiki.
    # identity_set distinguishes "never populated" (False) from "populated
    # but empty identity" (True) so the router knows whether to require a
    # match before continuing.
    identity_entities: list[str] = field(default_factory=list)
    identity_set: bool = False
    # Graph-kind model for the user-home + topic-subscope feature.
    #   None     -> standalone org/shared wiki (original model)
    #   "home"   -> a user's personal home wiki; wiki_id == user_id
    #   "topic"  -> a topic sub-graph nested under a home wiki
    # When kind == "topic", `parent` is the home wiki's id (the user) and the
    # topic graph is physically stored at wikis/{parent}/topics/{topic_key}/.
    # A "topic" wiki is not a sibling -- it belongs to a home and inherits its
    # owner's grant model. `topic_key` is the slug that names the sub-graph's
    # storage scope within the home.
    kind: str | None = None
    parent: str = ""
    topic_key: str = ""
    # user_id -> role. The creator is admin; everyone else is granted.
    access: dict[str, str] = field(default_factory=dict)
    # Any authenticated user may read. Write still needs an explicit grant —
    # an open-read wiki is useful, an open-write one is vandalism waiting.
    public_read: bool = False
    archived: bool = False
    # passcode -> invite record. A member generates an invite (target role,
    # optional use-limit and expiry) and shares the passcode; anyone who
    # redeems it is granted that role. Keys are the plaintext passcodes.
    invites: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "WikiMeta":
        base = asdict(WikiMeta("", ""))
        return WikiMeta(**{k: d.get(k, v) for k, v in base.items()})


class WikiRegistry:
    def __init__(self, backend: StorageBackend):
        self.backend = backend

    # ---------- persistence ----------

    def _read(self) -> dict[str, dict]:
        # Short-term memory: serve the hot registry snapshot from the
        # in-process cache instead of a ~1s S3 GET on every request.
        cache = _registry_cache(self.backend)
        now = time.time()
        hit = cache.get("snapshot")
        if hit is not None and now - hit[1] < _REGISTRY_CACHE_TTL:
            return hit[0]
        raw = self.backend.get_bytes(REGISTRY_KEY)
        if raw is None:
            wikis: dict[str, dict] = {}
        else:
            try:
                wikis = json.loads(raw.data.decode("utf-8")).get("wikis", {})
            except json.JSONDecodeError:
                logger.error("wiki registry at %s is not valid JSON", REGISTRY_KEY)
                raise
        with _REGISTRY_CACHE_LOCK:
            cache["snapshot"] = (wikis, now)
        return wikis

    def _write(self, wikis: dict[str, dict]) -> None:
        payload = json.dumps({"version": 1, "wikis": wikis},
                             ensure_ascii=False, indent=2).encode("utf-8")
        self.backend.put_bytes(REGISTRY_KEY, payload)
        # Invalidate immediately: a mutation in this process must be visible
        # on the next read (no staleness window for our own writes).
        with _REGISTRY_CACHE_LOCK:
            _registry_cache(self.backend)["snapshot"] = (wikis, time.time())

    # ---------- reading ----------

    def get(self, wiki_id: str) -> WikiMeta | None:
        raw = self._read().get(wiki_id)
        return WikiMeta.from_dict(raw) if raw else None

    def list_all(self, include_archived: bool = False) -> list[WikiMeta]:
        out = [WikiMeta.from_dict(v) for v in self._read().values()]
        if not include_archived:
            out = [w for w in out if not w.archived]
        return sorted(out, key=lambda w: w.title.lower())

    def list_for(self, user_id: str, role: str = ROLE_READ,
                 include_archived: bool = False) -> list[WikiMeta]:
        """Only the wikis this user may reach at `role` or above.

        The router scores against this list rather than every wiki, so a user
        is never told a wiki exists by having it offered as a destination.
        """
        return [w for w in self.list_all(include_archived)
                if self._permits(w, user_id, role)]

    @staticmethod
    def _permits(wiki: WikiMeta, user_id: str, role: str) -> bool:
        if wiki.public_read and role == ROLE_READ:
            return True
        held = wiki.access.get(user_id)
        if held is None:
            return False
        return _ROLE_RANK.get(held, 0) >= _ROLE_RANK.get(role, 99)

    def ensure_personal(self, wiki_id: str, user_id: str) -> WikiMeta | None:
        """Register a user's own wiki on first use.

        Before the multi-wiki change every user had exactly one graph named
        after them. Requiring an explicit registry entry would lock every
        existing deployment out of its own data until someone ran a migration,
        so a user reaching for the wiki that shares their name gets it created
        with themselves as admin.

        Only ever for `wiki_id == user_id`. Any other name must be created
        deliberately, or auto-creation would mean anyone naming a wiki brings
        it into existence. The resulting wiki is marked kind="home" so the
        API/UI know it is a personal container (which may hold topic
        sub-scopes), not a standalone shared wiki.
        """
        if wiki_id != user_id:
            return None
        existing = self.get(wiki_id)
        if existing is not None:
            if not existing.kind:
                self._set_kind(wiki_id, "home", "", "")
            return self.get(wiki_id)
        try:
            meta = self.create(wiki_id, created_by=user_id,
                               description="Personal wiki.", wiki_id=wiki_id,
                               allow_similar=True, kind="home")
            return meta
        except WikiError:
            return self.get(wiki_id)

    # ---------- user home + topic sub-scopes ----------

    def ensure_home(self, user_id: str) -> WikiMeta | None:
        """Create (or fetch) a user's home wiki: kind="home", wiki_id == user_id.

        This is the container that owns the user's topic sub-graphs. Returns
        the home wiki, or None if it could not be created.
        """
        return self.ensure_personal(user_id, user_id)

    def list_topics(self, user_id: str, include_archived: bool = False) -> list[WikiMeta]:
        """The topic sub-scopes inside a user's home wiki."""
        return [w for w in self.list_all(include_archived)
                if w.kind == "topic" and w.parent == user_id]

    def get_topic(self, user_id: str, topic_key: str) -> WikiMeta | None:
        """Find the topic sub-scope `topic_key` under user_id's home wiki."""
        for w in self.list_topics(user_id):
            if w.topic_key == topic_key:
                return w
        return None

    def create_topic(self, user_id: str, title: str, description: str = "",
                     topic: str = "", tags: list[str] | None = None,
                     topic_key: str | None = None) -> WikiMeta:
        """Create a topic sub-scope inside user_id's home wiki.

        Ensures the home wiki exists first. The sub-scope is registered with
        kind="topic", parent=user_id, topic_key=<slug>. Its entities are
        stored physically under `wikis/{user_id}/topics/{topic_key}/` (the
        home wiki's own scope) rather than as a sibling top-level wiki -- this
        is what makes the topic graphs "separate within the user's wiki".

        A topic_key that already exists under this home RAISES WikiError
        (a topic is namespace-scoped, not global) rather than silently
        colliding with another user's same-named topic.
        """
        home = self.ensure_home(user_id)
        if home is None:
            raise WikiError(f"Could not create a home wiki for {user_id!r}.")
        key = topic_key or slugify_wiki(title) or "topic"
        if not _WIKI_ID.match(key):
            raise WikiError(
                f"Invalid topic key {key!r}: lowercase letters, digits and "
                f"dashes, 1-64 characters.")
        if self.get_topic(user_id, key) is not None:
            raise WikiError(
                f"Topic {key!r} already exists in {user_id!r}'s home wiki.")
        wikis = self._read()
        # The registry entry id must be globally unique, so namespace the
        # sub-scope's id under the user: "<user>-<key>".
        wid = f"{user_id}-{key}"
        now = datetime.now(timezone.utc).isoformat()
        meta = WikiMeta(
            wiki_id=wid, title=title, description=description,
            created_at=now, created_by=user_id, updated_at=now,
            topic=topic, tags=list(tags or []), kind="topic",
            parent=user_id, topic_key=key,
            access={user_id: ROLE_ADMIN})
        wikis[wid] = meta.to_dict()
        self._write(wikis)
        logger.info("created topic sub-scope %s (%r) under home %s",
                    key, title, user_id)
        return meta

    def home_of(self, wiki_id: str) -> WikiMeta | None:
        """The home wiki that owns a topic sub-scope (None for standalone)."""
        w = self.get(wiki_id)
        if w is None or w.kind != "topic" or not w.parent:
            return None
        return self.get(w.parent)

    def _set_kind(self, wiki_id: str, kind: str, parent: str, topic_key: str) -> None:
        try:
            wikis = self._read()
            if wiki_id not in wikis:
                return
            wikis[wiki_id]["kind"] = kind
            wikis[wiki_id]["parent"] = parent
            wikis[wiki_id]["topic_key"] = topic_key
            self._write(wikis)
        except Exception:
            logger.debug("could not set kind on %s", wiki_id, exc_info=True)

    def topic_scope(self, user_id: str, topic_key: str) -> str:
        """The composite STORAGE scope for a topic sub-scope: `{user}/topics/{key}`.

        wiki_key() joins this under `wikis/`, so entities of a topic graph are
        stored at `wikis/{user}/topics/{key}/{type}/{slug}.okf.md` -- nested
        inside the home wiki's namespace, physically separate from both the
        home page and other topic graphs.
        """
        return f"{user_id}/topics/{topic_key}"

    # ---------- view grants (summarized reads by other users / admins) ----------

    def grant_view(self, owner: str, viewer: str, scope: str = "all",
                   permissions: str = "summary") -> dict:
        """Record that `viewer` may ask for a SUMMARIZED view of `owner`'s wiki.

        `scope`: "all" | a topic_key | "{topic}:{topic_key}" -- what part of
        the owner's wiki the viewer may summarise. `permissions`: "summary"
        (digest only, no raw entities) or "read+summary" (also raw read).
        Stored on the owner's home wiki record under `_views`, so the owner
        can list/revoke their own grants. Returns the grant record.
        """
        home = self.ensure_home(owner)
        if home is None:
            raise WikiError(f"Could not establish a home wiki for {owner!r}.")
        wikis = self._read()
        views = wikis.get(home.wiki_id, {}).setdefault("_views", {})
        views[viewer] = {"scope": scope, "permissions": permissions,
                         "granted_at": datetime.now(timezone.utc).isoformat()}
        self._write(wikis)
        return views[viewer]

    def list_views(self, owner: str) -> dict:
        """Who may summarise `owner`'s wiki, and how."""
        raw = self._read().get(owner)
        if raw is None:
            return {}
        return dict(raw.get("_views") or {})

    def revoke_view(self, owner: str, viewer: str) -> bool:
        """Remove `viewer`'s summarised-view grant on `owner`'s wiki."""
        wikis = self._read()
        if owner not in wikis:
            return False
        views = wikis[owner].get("_views", {})
        removed = views.pop(viewer, None) is not None
        if removed:
            self._write(wikis)
        return removed

    def can_view(self, owner: str, viewer: str, scope: str = "all",
                 is_admin: bool = False) -> bool:
        """May `viewer` summarise `owner`'s wiki (or that topic)? Admins bypass
        the grant (oversight) -- but can_view True does not itself reveal raw
        entities; the summarizer still returns a digest."""
        if is_admin:
            return True
        if viewer == owner:
            return True
        rec = self.list_views(owner).get(viewer)
        if rec is None:
            return False
        grant_scope = rec.get("scope", "all")
        if grant_scope == "all" or grant_scope == scope:
            return True
        # grant on the whole wiki covers any topic sub-scope of it
        if scope and scope.startswith("topic:") and grant_scope == "all":
            return True
        return False

    def view_permissions(self, owner: str, viewer: str,
                         is_admin: bool = False) -> str:
        """The view permission for a viewer ("summary" | "read+summary" | "")."""
        if is_admin or viewer == owner:
            return "read+summary"
        rec = self.list_views(owner).get(viewer)
        return (rec or {}).get("permissions", "")

    def require(self, wiki_id: str, user_id: str, role: str = ROLE_READ,
                is_admin: bool = False) -> WikiMeta:
        """Fetch a wiki, or raise. The single place access is decided.

        `is_admin` is the platform admin token, which bypasses per-wiki
        grants — that credential already reaches every user.
        """
        wiki = self.get(wiki_id) or self.ensure_personal(wiki_id, user_id)
        if wiki is None:
            raise WikiNotFound(f"No wiki {wiki_id!r}.")
        if is_admin or self._permits(wiki, user_id, role):
            return wiki
        # Same message whether the wiki exists but is barred or the user
        # simply has no grant: which wikis exist is not something an
        # unauthorised caller should be able to enumerate.
        raise WikiAccessDenied(
            f"You do not have {role} access to {wiki_id!r}.")

    # ---------- writing ----------

    def create(self, title: str, created_by: str, description: str = "",
               wiki_id: str | None = None, public_read: bool = False,
               allow_similar: bool = False, topic: str = "",
               tags: list[str] | None = None, kind: str | None = None) -> WikiMeta:
        """Create a wiki. Raises WikiNameCollision on a near-duplicate name.

        `allow_similar` is the explicit override for when two similarly named
        wikis really are distinct. The router never passes it — a machine
        should not be the thing that decides two worlds are different.
        """
        title = " ".join(title.split())
        if not title:
            raise WikiError("A wiki needs a title.")

        # `None` means "derive one"; an empty string means the caller supplied
        # a wiki_id and it was blank. Treating both as "derive" would silently
        # accept a bug in the caller.
        slug = slugify_wiki(title) if wiki_id is None else wiki_id
        if not _WIKI_ID.match(slug):
            raise WikiError(
                f"Invalid wiki id {slug!r}: lowercase letters, digits and "
                f"dashes, 1-64 characters.")

        wikis = self._read()
        if slug in wikis:
            raise WikiError(f"Wiki {slug!r} already exists.")

        if not allow_similar:
            key = _match_key(title)
            if key:
                for existing in wikis.values():
                    if _match_key(existing.get("title", "")) == key:
                        raise WikiNameCollision(title, existing["wiki_id"],
                                                existing["title"])

        now = datetime.now(timezone.utc).isoformat()
        meta = WikiMeta(wiki_id=slug, title=title, description=description,
                        created_at=now, created_by=created_by, updated_at=now,
                        topic=topic, tags=list(tags or []),
                        public_read=public_read, kind=kind,
                        access={created_by: ROLE_ADMIN} if created_by else {})
        wikis[slug] = meta.to_dict()
        self._write(wikis)
        logger.info("created wiki %s (%r) for %s", slug, title, created_by)
        return meta

    def set_tags(self, wiki_id: str, tags: list[str]) -> WikiMeta:
        """Replace a wiki's curated tags. Used at first-write to lock the
        topic labels once the wiki's real content is known. Best-effort: a
        missing wiki is a no-op raising WikiNotFound."""
        wikis = self._read()
        if wiki_id not in wikis:
            raise WikiNotFound(f"No wiki {wiki_id!r}.")
        clean = [t.strip() for t in (tags or []) if t and t.strip()]
        wikis[wiki_id]["tags"] = clean
        wikis[wiki_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write(wikis)
        return WikiMeta.from_dict(wikis[wiki_id])

    def grant(self, wiki_id: str, user_id: str, role: str) -> WikiMeta:
        if role not in _ROLE_RANK:
            raise WikiError(f"Unknown role {role!r}; use read, write or admin.")
        wikis = self._read()
        if wiki_id not in wikis:
            raise WikiNotFound(f"No wiki {wiki_id!r}.")
        wikis[wiki_id].setdefault("access", {})[user_id] = role
        wikis[wiki_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write(wikis)
        return WikiMeta.from_dict(wikis[wiki_id])

    def set_identity(self, wiki_id: str, entity_ids: list[str]) -> WikiMeta:
        """Lock the wiki's identity to `entity_ids` (first-write entities).

        Option A: called once, when a wiki is first populated. After this, the
        router allows "continue" only when incoming content mentions at least
        one of these entities; content touching none starts a new wiki. Does
        nothing if the identity is already set (first write wins).
        """
        wikis = self._read()
        if wiki_id not in wikis:
            raise WikiNotFound(f"No wiki {wiki_id!r}.")
        meta = WikiMeta.from_dict(wikis[wiki_id])
        if meta.identity_set:
            return meta  # already locked; first write is authoritative
        wikis[wiki_id]["identity_entities"] = list(entity_ids or [])
        wikis[wiki_id]["identity_set"] = True
        wikis[wiki_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write(wikis)
        return WikiMeta.from_dict(wikis[wiki_id])

    def revoke(self, wiki_id: str, user_id: str) -> WikiMeta:
        wikis = self._read()
        if wiki_id not in wikis:
            raise WikiNotFound(f"No wiki {wiki_id!r}.")
        wikis[wiki_id].get("access", {}).pop(user_id, None)
        self._write(wikis)
        return WikiMeta.from_dict(wikis[wiki_id])

    def set_archived(self, wiki_id: str, archived: bool) -> WikiMeta:
        """Archiving hides a wiki from routing without deleting anything.

        Deliberately not deletion: a wiki auto-created in error should stop
        attracting conversations, but its contents may still be wanted, and an
        automatic process must never be able to destroy a graph.
        """
        wikis = self._read()
        if wiki_id not in wikis:
            raise WikiNotFound(f"No wiki {wiki_id!r}.")
        wikis[wiki_id]["archived"] = archived
        self._write(wikis)
        return WikiMeta.from_dict(wikis[wiki_id])

    def refresh_stats(self, wiki_id: str, entity_count: int,
                      sample_entities: list[str]) -> None:
        """Keep the routing summary current.

        Called after writes. Failure here is logged, not raised: a stale
        routing hint is a worse route, while a failed write is lost work.
        """
        try:
            wikis = self._read()
            if wiki_id not in wikis:
                return
            wikis[wiki_id]["entity_count"] = entity_count
            wikis[wiki_id]["sample_entities"] = sample_entities[:25]
            wikis[wiki_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._write(wikis)
        except Exception:
            logger.warning("could not refresh stats for wiki %s", wiki_id,
                           exc_info=True)

    # ---------- passcode invites ----------

    @staticmethod
    def _new_passcode(length: int = 8) -> str:
        """Random passcode from an unambiguous alphabet (no 0/O, 1/I/l)."""
        alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def create_invite(self, wiki_id: str, created_by: str, role: str = ROLE_WRITE,
                      uses_left: int | None = None,
                      expires_in: int | None = None,
                      passcode: str | None = None) -> dict:
        """Register a passcode on a wiki. Returns the invite record incl. the
        passcode (the caller shows it ONCE; keys are plaintext passcodes).

        `uses_left=None` means unlimited. `expires_in` is seconds from now
        (None = never expires). `created_by` is recorded but NOT authorised
        here — the route enforces admin on the wiki.
        """
        if role not in _ROLE_RANK:
            raise WikiError(f"Unknown role {role!r}; use read, write or admin.")
        wikis = self._read()
        if wiki_id not in wikis:
            raise WikiNotFound(f"No wiki {wiki_id!r}.")
        code = passcode or self._new_passcode()
        now = int(time.time())
        record = {
            "role": role,
            "created_by": created_by,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "uses_left": uses_left,
            "expires_at": (now + expires_in) if expires_in else None,
            "redeemed": 0,
        }
        wikis[wiki_id].setdefault("invites", {})[code] = record
        wikis[wiki_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write(wikis)
        return {"passcode": code, "record": record}

    def redeem_invite(self, wiki_id: str, passcode: str,
                      user_id: str) -> WikiMeta:
        """Grant `user_id` the role an invite carries, if the passcode is
        valid. Invalid, expired, or exhausted invites raise `WikiError`.
        Redeeming the same passcode again by a user who already holds >= the
        invite's role is a no-op success."""
        wikis = self._read()
        if wiki_id not in wikis:
            raise WikiNotFound(f"No wiki {wiki_id!r}.")
        invites = wikis[wiki_id].get("invites", {})
        rec = invites.get(passcode)
        if rec is None:
            raise WikiError("Invalid invite passcode.")
        now = int(time.time())
        if rec.get("expires_at") is not None and now > rec["expires_at"]:
            # Drop it so it fails fast next time.
            invites.pop(passcode, None)
            self._write(wikis)
            raise WikiError("That invite has expired.")
        if rec.get("uses_left") == 0:
            raise WikiError("That invite has no uses left.")
        role = rec.get("role", ROLE_READ)
        held = wikis[wiki_id].get("access", {}).get(user_id)
        # No-op if they already hold at least the invite's role.
        if held is None or _ROLE_RANK.get(held, 0) < _ROLE_RANK.get(role, 0):
            wikis[wiki_id].setdefault("access", {})[user_id] = role
            rec["redeemed"] = rec.get("redeemed", 0) + 1
            if rec.get("uses_left") is not None:
                rec["uses_left"] -= 1
        wikis[wiki_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write(wikis)
        return WikiMeta.from_dict(wikis[wiki_id])

    def list_invites(self, wiki_id: str) -> dict:
        wikis = self._read()
        if wiki_id not in wikis:
            raise WikiNotFound(f"No wiki {wiki_id!r}.")
        now = int(time.time())
        out = {}
        for code, rec in wikis[wiki_id].get("invites", {}).items():
            expired = rec.get("expires_at") is not None and now > rec["expires_at"]
            if expired or rec.get("uses_left") == 0:
                continue
            out[code] = dict(rec)
        return out

    def revoke_invite(self, wiki_id: str, passcode: str) -> bool:
        wikis = self._read()
        if wiki_id not in wikis:
            raise WikiNotFound(f"No wiki {wiki_id!r}.")
        removed = wikis[wiki_id].get("invites", {}).pop(passcode, None) is not None
        if removed:
            wikis[wiki_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._write(wikis)
        return removed

    def clear_invites(self, wiki_id: str) -> int:
        wikis = self._read()
        if wiki_id not in wikis:
            raise WikiNotFound(f"No wiki {wiki_id!r}.")
        n = len(wikis[wiki_id].get("invites", {}))
        if n:
            wikis[wiki_id]["invites"] = {}
            wikis[wiki_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._write(wikis)
        return n
