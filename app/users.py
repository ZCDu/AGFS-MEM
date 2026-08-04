"""
Username/password accounts and signed session tokens.

Static tokens (app/auth.py) suit service-to-service traffic: one long-lived
secret in an environment variable. They are wrong for people. A person needs a
credential they can remember, change, and have revoked, and one that is not
sitting in a config file on every machine that talks to the API.

THE FLOW
    POST /v1/auth/login  {username, password}  ->  {token, expires_at}
    ...then that token is used as `Authorization: Bearer <token>` exactly like
    a static token.

    Passwords appear once, at login. Every other request verifies a signed
    token, so the request path needs no storage read and no password handling.

PASSWORD STORAGE
    scrypt, from hashlib — memory-hard, so custom hardware buys an attacker far
    less than it would against SHA-family hashing. Per-user random salt, so
    identical passwords produce different hashes and one rainbow table cannot
    cover two accounts. Parameters are stored WITH each hash, so they can be
    raised later without invalidating existing accounts.

    Verification is constant-time via compare_digest.

SESSION TOKENS ARE STATELESS
    `v1.<payload>.<signature>`, signed HMAC-SHA256 with AUTH_SECRET. The
    payload carries user_id and expiry.

    Stateless on purpose: checking a revocation list would mean a storage read
    on every single request, which on this backend is a real network
    round-trip. The cost of that choice is that an individual token cannot be
    revoked before it expires.

    So: keep AUTH_SESSION_HOURS short, and understand that the revocation
    story is "rotate AUTH_SECRET", which invalidates every session at once.
    That is an acceptable trade for an internal service and NOT acceptable if
    you later have real end users — at which point this becomes a session
    table, and the seam is `verify_session_token` below.

WHY THE USER STORE LIVES IN THE OBJECT STORE
    `{prefix}_auth/users.json`, through the same StorageBackend as everything
    else. No new infrastructure, and it survives restarts and redeploys. It is
    read only at login and when managing accounts, never on the hot path.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass, field

from app.storage.backend import StorageBackend

logger = logging.getLogger("memory_backend.users")

USERS_KEY = "_auth/users.json"

# scrypt "interactive" parameters: roughly 16MB and ~100ms per hash on typical
# server hardware. Stored per-record so they can be raised without breaking
# existing accounts.
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32

MIN_PASSWORD_LENGTH = 10


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------- passwords

def hash_password(password: str) -> dict:
    """Returns a self-describing record: the parameters travel with the hash."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                        r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN)
    return {"algo": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
            "dklen": SCRYPT_DKLEN, "salt": _b64e(salt), "hash": _b64e(dk)}


def verify_password(password: str, record: dict) -> bool:
    if not record or record.get("algo") != "scrypt":
        return False
    try:
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=_b64d(record["salt"]),
            n=int(record["n"]), r=int(record["r"]), p=int(record["p"]),
            dklen=int(record["dklen"]))
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(_b64e(dk), record["hash"])


def check_password_strength(password: str) -> None:
    """Length only. Composition rules (a digit, a symbol, a capital) push
    people towards predictable substitutions and measurably do not help, so
    they are not imposed here."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")


# ---------------------------------------------------------------- store

@dataclass
class UserRecord:
    username: str
    user_id: str
    password: dict = field(default_factory=dict)
    is_admin: bool = False
    disabled: bool = False
    created_at: str = ""
    password_changed_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "UserRecord":
        return UserRecord(
            username=d["username"], user_id=d["user_id"],
            password=d.get("password", {}), is_admin=bool(d.get("is_admin")),
            disabled=bool(d.get("disabled")), created_at=d.get("created_at", ""),
            password_changed_at=d.get("password_changed_at", ""))


class UserStore:
    """Accounts in the object store. Read at login and by the admin CLI only."""

    def __init__(self, backend: StorageBackend, key: str = USERS_KEY):
        self.backend = backend
        self.key = key

    def _read(self) -> dict[str, dict]:
        raw = self.backend.get_bytes(self.key)
        if raw is None:
            return {}
        try:
            data = json.loads(raw.data.decode("utf-8"))
        except json.JSONDecodeError:
            logger.error("user store at %s is not valid JSON", self.key)
            raise
        return data.get("users", {})

    def _write(self, users: dict[str, dict]) -> None:
        payload = json.dumps({"version": 1, "users": users},
                             indent=2, ensure_ascii=False).encode("utf-8")
        self.backend.put_bytes(self.key, payload)

    @staticmethod
    def normalise(username: str) -> str:
        # Case-insensitive so "Alice" and "alice" cannot become two accounts —
        # the same collision that silently merges entity slugs elsewhere.
        return username.strip().lower()

    def get(self, username: str) -> UserRecord | None:
        raw = self._read().get(self.normalise(username))
        return UserRecord.from_dict(raw) if raw else None

    def list_users(self) -> list[UserRecord]:
        return [UserRecord.from_dict(v) for v in self._read().values()]

    def create(self, username: str, password: str, user_id: str | None = None,
               is_admin: bool = False) -> UserRecord:
        name = self.normalise(username)
        if not name:
            raise ValueError("Username cannot be empty.")
        check_password_strength(password)
        users = self._read()
        if name in users:
            raise ValueError(f"User {name!r} already exists.")
        now = _now()
        rec = UserRecord(username=name, user_id=user_id or name,
                         password=hash_password(password), is_admin=is_admin,
                         created_at=now, password_changed_at=now)
        users[name] = rec.to_dict()
        self._write(users)
        return rec

    def set_password(self, username: str, password: str) -> None:
        name = self.normalise(username)
        check_password_strength(password)
        users = self._read()
        if name not in users:
            raise ValueError(f"No such user {name!r}.")
        users[name]["password"] = hash_password(password)
        users[name]["password_changed_at"] = _now()
        self._write(users)

    def set_disabled(self, username: str, disabled: bool) -> None:
        name = self.normalise(username)
        users = self._read()
        if name not in users:
            raise ValueError(f"No such user {name!r}.")
        users[name]["disabled"] = disabled
        self._write(users)

    def delete(self, username: str) -> None:
        name = self.normalise(username)
        users = self._read()
        if users.pop(name, None) is None:
            raise ValueError(f"No such user {name!r}.")
        self._write(users)

    def authenticate(self, username: str, password: str) -> UserRecord | None:
        """Returns the user on success, None otherwise.

        Runs the hash even when the username is unknown, so a caller cannot
        tell the difference from response timing. The endpoint returns one
        generic message for the same reason — whether an account exists is not
        something an unauthenticated caller should learn.
        """
        rec = self.get(username)
        if rec is None:
            # Deliberate dummy work against a throwaway hash.
            verify_password(password, hash_password("timing-equaliser"))
            return None
        if not verify_password(password, rec.password):
            return None
        if rec.disabled:
            return None
        return rec


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- sessions

class SessionError(Exception):
    pass


def issue_session_token(secret: str, user_id: str, hours: float,
                        is_admin: bool = False, username: str = "") -> tuple[str, int]:
    """Returns (token, expires_at_unix)."""
    if not secret:
        raise SessionError("AUTH_SECRET is not set; cannot issue session tokens.")
    exp = int(time.time() + hours * 3600)
    payload = {"u": user_id, "n": username, "a": bool(is_admin), "e": exp}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"v1.{body}.{sig}", exp


def verify_session_token(secret: str, token: str) -> dict:
    """Returns the payload, or raises SessionError.

    Signature is checked BEFORE the payload is parsed, so unsigned input never
    reaches the JSON decoder.
    """
    if not secret:
        raise SessionError("AUTH_SECRET is not set.")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        raise SessionError("Not a session token.")
    _, body, sig = parts
    expected = _b64e(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise SessionError("Bad signature.")
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError) as e:
        raise SessionError("Malformed payload.") from e
    if int(payload.get("e", 0)) < time.time():
        raise SessionError("Session expired. Log in again.")
    return payload


def looks_like_session_token(token: str) -> bool:
    """Cheap discriminator so a static token is never run through signature
    verification and vice versa."""
    return token.startswith("v1.") and token.count(".") == 2
