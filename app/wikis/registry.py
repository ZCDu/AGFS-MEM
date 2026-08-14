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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from app.storage.backend import StorageBackend

logger = logging.getLogger("memory_backend.wikis")

REGISTRY_KEY = "wikis/_registry.json"

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
    return slug[:64] or ""


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
    # A few representative entity titles, refreshed on write. Lets the router
    # score a wiki without loading its manifest.
    sample_entities: list[str] = field(default_factory=list)
    # user_id -> role. The creator is admin; everyone else is granted.
    access: dict[str, str] = field(default_factory=dict)
    # Any authenticated user may read. Write still needs an explicit grant —
    # an open-read wiki is useful, an open-write one is vandalism waiting.
    public_read: bool = False
    archived: bool = False

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
        raw = self.backend.get_bytes(REGISTRY_KEY)
        if raw is None:
            return {}
        try:
            return json.loads(raw.data.decode("utf-8")).get("wikis", {})
        except json.JSONDecodeError:
            logger.error("wiki registry at %s is not valid JSON", REGISTRY_KEY)
            raise

    def _write(self, wikis: dict[str, dict]) -> None:
        payload = json.dumps({"version": 1, "wikis": wikis},
                             ensure_ascii=False, indent=2).encode("utf-8")
        self.backend.put_bytes(REGISTRY_KEY, payload)

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
        it into existence.
        """
        if wiki_id != user_id:
            return None
        existing = self.get(wiki_id)
        if existing is not None:
            return existing
        try:
            return self.create(wiki_id, created_by=user_id,
                               description="Personal wiki.", wiki_id=wiki_id,
                               allow_similar=True)
        except WikiError:
            return self.get(wiki_id)

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
               allow_similar: bool = False) -> WikiMeta:
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
                        public_read=public_read,
                        access={created_by: ROLE_ADMIN} if created_by else {})
        wikis[slug] = meta.to_dict()
        self._write(wikis)
        logger.info("created wiki %s (%r) for %s", slug, title, created_by)
        return meta

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
