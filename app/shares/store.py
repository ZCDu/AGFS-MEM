"""
Share links: {share_id -> owner_user_id, entry_wiki_id}.

WHY A FLAT, GLOBAL KEY (not under the owner's wiki prefix)
    A guest opening a link has ONLY the share_id -- they don't know, and must
    never need to know, the owner's user_id to resolve it. Nesting the record
    under `{owner}/wiki/_shares/{id}.json` (the WikiInbox pattern) would mean
    the record can't be found without already knowing the owner, which is
    exactly backwards for a capability URL. So this lives at a flat top-level
    prefix (`_shares/{share_id}.json`), independent of any wiki's namespace --
    the share_id itself is the only lookup key a request ever has.

THE TOKEN IS THE CREDENTIAL
    share_id is generated with secrets.token_urlsafe (not a sequential or
    guessable id) because holding it is what grants access -- see
    app/auth.py's require_share(). There is no separate password or grant
    list to check; anyone who has the string can use it until it's revoked
    or expires. Treat it exactly like a capability URL (a Google Docs "anyone
    with the link" share), not like a username.

SCOPE IS NOT STORED HERE
    A record remembers WHERE the share points (entry_wiki_id) but not WHICH
    entities are currently in scope -- that's the live connected component
    containing entry_wiki_id, recomputed on every request by
    app/shares/scope.py. Storing a frozen member list would go stale the
    moment the owner or a guest adds something new to the shared topic.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from app.storage.backend import StorageBackend

_PREFIX = "_shares/"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


@dataclass
class ShareLink:
    share_id: str
    owner_user_id: str
    entry_wiki_id: str
    label: str = ""
    created_at: str = ""
    expires_at: str | None = None
    revoked_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ShareLink":
        return ShareLink(
            share_id=d["share_id"], owner_user_id=d["owner_user_id"],
            entry_wiki_id=d["entry_wiki_id"], label=d.get("label", ""),
            created_at=d.get("created_at", ""), expires_at=d.get("expires_at"),
            revoked_at=d.get("revoked_at"),
        )

    @property
    def active(self) -> bool:
        """False once revoked, or past its expiry. A malformed expiry is
        treated as no expiry rather than failing closed on a parse error --
        revocation is the deliberate way to kill a link; a bad timestamp
        should not do it by accident."""
        if self.revoked_at:
            return False
        if self.expires_at:
            try:
                expires = datetime.fromisoformat(self.expires_at)
            except ValueError:
                return True
            if datetime.now(timezone.utc) >= expires:
                return False
        return True


def new_share_id() -> str:
    return secrets.token_urlsafe(24)


class ShareLinkStore:
    def __init__(self, backend: StorageBackend):
        self.backend = backend

    @staticmethod
    def _validate_id(share_id: str) -> str:
        # A share_id is a credential, not a title -- reject anything
        # malformed outright rather than sanitising it into a DIFFERENT
        # (possibly valid) key, which would resolve to the wrong record.
        if not _ID_RE.match(share_id or ""):
            raise ValueError(f"Malformed share id {share_id!r}")
        return share_id

    def _key(self, share_id: str) -> str:
        return f"{_PREFIX}{self._validate_id(share_id)}.json"

    def create(self, owner_user_id: str, entry_wiki_id: str, label: str = "",
              expires_in_days: float | None = None) -> ShareLink:
        expires_at = None
        if expires_in_days is not None and expires_in_days > 0:
            expires_at = (datetime.now(timezone.utc)
                          + timedelta(days=expires_in_days)).isoformat()
        link = ShareLink(
            share_id=new_share_id(), owner_user_id=owner_user_id,
            entry_wiki_id=entry_wiki_id, label=label,
            created_at=datetime.now(timezone.utc).isoformat(),
            expires_at=expires_at,
        )
        self.backend.put_bytes(
            self._key(link.share_id),
            json.dumps(link.to_dict(), ensure_ascii=False, indent=2).encode("utf-8"))
        return link

    def get(self, share_id: str) -> ShareLink | None:
        try:
            key = self._key(share_id)
        except ValueError:
            return None
        raw = self.backend.get_bytes(key)
        if raw is None:
            return None
        try:
            return ShareLink.from_dict(json.loads(raw.data.decode("utf-8")))
        except (ValueError, KeyError):
            return None

    def revoke(self, share_id: str) -> bool:
        link = self.get(share_id)
        if link is None:
            return False
        link.revoked_at = datetime.now(timezone.utc).isoformat()
        self.backend.put_bytes(
            self._key(share_id),
            json.dumps(link.to_dict(), ensure_ascii=False, indent=2).encode("utf-8"))
        return True

    def list_for_owner(self, owner_user_id: str) -> list[ShareLink]:
        """Scans the whole (flat, small) _shares/ prefix and filters -- same
        approach app/graph/conflicts.py's ConflictStore.list() takes for its
        own small, non-indexed collection. Fine at the scale this is built
        for; would need an owner-keyed index before that stops being true."""
        out: list[ShareLink] = []
        for key in self.backend.list_keys(_PREFIX):
            if not key.endswith(".json"):
                continue
            raw = self.backend.get_bytes(key)
            if raw is None:
                continue
            try:
                link = ShareLink.from_dict(json.loads(raw.data.decode("utf-8")))
            except (ValueError, KeyError):
                continue
            if link.owner_user_id == owner_user_id:
                out.append(link)
        out.sort(key=lambda l: l.created_at, reverse=True)
        return out
