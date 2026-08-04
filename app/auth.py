"""
Bearer-token authentication.

Before this existed, `user_id` was just a path parameter: anyone who could
reach the port could read or write any user's memory by editing the URL. That
is fine on a laptop and a data breach anywhere else.

WHAT IT CHECKS
    Every request under /v1/users/{user_id} must carry a token that is bound
    to that specific user_id. A valid token for `alice` gets 403 on
    `/v1/users/bob/...` — authentication and authorisation in one step, since
    with one namespace per user they are the same question.

    /healthz, /docs and /gui stay open: the first is for monitoring, and the
    other two are static assets that contain no user data. Every API call the
    GUI makes goes through this check like any other client.

FAIL CLOSED
    AUTH_MODE defaults to "token" and the app refuses to START if no tokens
    are configured, rather than quietly serving an open API. An insecure
    default is how services get shipped insecure — the failure has to happen
    at deploy time, loudly, not silently at runtime.

    AUTH_MODE=off is available for local development and logs a warning on
    every startup. It exists because forcing tokens into a throwaway disk-mode
    dev loop would just get worked around, and a documented escape hatch is
    safer than one people invent themselves.

WHAT THIS IS NOT
    Static shared secrets. There is no expiry, rotation, revocation list, or
    per-request signing, and tokens sit in the environment. That is the same
    trust model as a cloud provider API key, which is appropriate for
    service-to-service traffic and NOT appropriate for end users logging in
    from browsers. If real user accounts are ever needed, this is the seam to
    replace with OIDC or a session layer — not something to extend.
"""

from __future__ import annotations

import hmac
import logging
import secrets

from fastapi import Header, HTTPException

from app.config import Settings, get_settings
from app.users import SessionError, looks_like_session_token, verify_session_token

logger = logging.getLogger("memory_backend.auth")

_BEARER = "bearer "


def parse_token_map(raw: str) -> dict[str, str]:
    """Parse `token:user,token:user` into {token: user_id}.

    Tokens are the key because that is the direction lookups go, and because
    two tokens may legitimately map to the same user (rotation without
    downtime: add the new one, drain, remove the old).
    """
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise ValueError(
                f"AUTH_TOKENS entry {pair!r} is malformed; expected 'token:user_id'")
        token, user = pair.split(":", 1)
        token, user = token.strip(), user.strip()
        if not token or not user:
            raise ValueError(f"AUTH_TOKENS entry {pair!r} has an empty token or user_id")
        out[token] = user
    return out


def suggest_token() -> str:
    return secrets.token_urlsafe(32)


def validate_at_startup(settings: Settings) -> None:
    """Refuse to start rather than serve an unauthenticated API.

    Called from create_app(), so a misconfiguration is a startup crash with
    instructions instead of an open endpoint nobody notices.
    """
    if settings.auth_mode == "off":
        logger.warning(
            "AUTH_MODE=off — the API is UNAUTHENTICATED. Any client that can "
            "reach this port can read and write every user's memory. Set "
            "AUTH_MODE=token before exposing this beyond localhost.")
        return

    if settings.auth_mode != "token":
        raise RuntimeError(
            f"AUTH_MODE={settings.auth_mode!r} is not recognised; use 'token' or 'off'")

    try:
        tokens = parse_token_map(settings.auth_tokens)
    except ValueError as e:
        raise RuntimeError(str(e)) from e

    if not tokens and not settings.auth_admin_token and not settings.auth_secret:
        raise RuntimeError(
            "AUTH_MODE=token but no tokens are configured, so every request "
            "would be rejected.\n"
            "Add a line like this to .env:\n\n"
            f"    AUTH_TOKENS={suggest_token()}:demo\n\n"
            "Format is token:user_id, comma-separated for more than one. "
            "An AUTH_ADMIN_TOKEN may access any user.\n\n"
            "Alternatively set AUTH_SECRET to enable username/password login, "
            "then create an account:\n"
            "    python scripts/manage_users.py add alice\n\n"
            "For local development without any auth, set AUTH_MODE=off.")

    logger.info("auth: token mode, %d token(s), admin token %s",
                len(tokens), "set" if settings.auth_admin_token else "not set")


def _bearer(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith(_BEARER):
        return None
    return authorization[len(_BEARER):].strip() or None


def _match(token: str, settings: Settings) -> tuple[bool, str | None]:
    """Returns (is_admin, user_id_or_None).

    Accepts two credential kinds through the same header:
      - a signed session token from POST /v1/auth/login (people)
      - a static token from AUTH_TOKENS (services)

    Session tokens are checked first and identified by their `v1.` prefix, so a
    static token is never fed to signature verification and vice versa.

    Static tokens are compared against every configured value with
    compare_digest rather than by dict lookup, so elapsed time does not reveal
    how much of a token was correct.
    """
    if looks_like_session_token(token):
        if not settings.auth_secret:
            raise HTTPException(
                status_code=401,
                detail="Session token presented but AUTH_SECRET is not configured.",
                headers={"WWW-Authenticate": "Bearer"})
        try:
            payload = verify_session_token(settings.auth_secret, token)
        except SessionError as e:
            raise HTTPException(status_code=401, detail=str(e),
                                headers={"WWW-Authenticate": "Bearer"}) from e
        return bool(payload.get("a")), payload.get("u")

    if settings.auth_admin_token and hmac.compare_digest(token, settings.auth_admin_token):
        return True, None
    found: str | None = None
    for candidate, user in parse_token_map(settings.auth_tokens).items():
        if hmac.compare_digest(token, candidate):
            found = user
    return False, found


def require_user(user_id: str, authorization: str | None = Header(default=None)) -> str:
    """Router-level dependency: the caller's token must be bound to `user_id`.

    Attached to whole routers rather than individual routes, so a new endpoint
    is protected by default instead of being protected only if someone
    remembers to decorate it.
    """
    settings = get_settings()
    if settings.auth_mode == "off":
        return user_id

    token = _bearer(authorization)
    if token is None:
        raise HTTPException(
            status_code=401,
            detail="Missing bearer token. Send 'Authorization: Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    is_admin, mapped = _match(token, settings)
    if is_admin:
        return user_id
    if mapped is None:
        # Deliberately the same message as a user mismatch below would be too
        # helpful in reverse — but distinguishing "unknown token" from "wrong
        # user" is useful to legitimate callers and tells an attacker nothing
        # they cannot already determine by trying their own token.
        raise HTTPException(
            status_code=401, detail="Unrecognised token.",
            headers={"WWW-Authenticate": "Bearer"})
    if mapped != user_id:
        raise HTTPException(
            status_code=403,
            detail=f"This token is scoped to user {mapped!r} and cannot access {user_id!r}.")
    return user_id


def require_any_credential(authorization: str | None = Header(default=None)) -> dict:
    """Validate a credential without tying it to a path user_id.

    /v1/auth/me has no {user_id} in its path, so it cannot use require_user.
    Returns a description of the caller, which is the fastest way to debug a
    403 — it says which user_id the token is actually scoped to.
    """
    settings = get_settings()
    if settings.auth_mode == "off":
        return {"auth": "off", "user_id": None, "is_admin": True,
                "note": "AUTH_MODE=off; every request is permitted."}

    token = _bearer(authorization)
    if token is None:
        raise HTTPException(
            status_code=401,
            detail="Missing bearer token. Send 'Authorization: Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"})

    is_admin, mapped = _match(token, settings)
    kind = "session" if looks_like_session_token(token) else "static_token"
    if is_admin and mapped is None:
        return {"auth": kind, "user_id": None, "is_admin": True,
                "note": "Admin credential: may access any user_id."}
    if mapped is None:
        raise HTTPException(status_code=401, detail="Unrecognised token.",
                            headers={"WWW-Authenticate": "Bearer"})
    return {"auth": kind, "user_id": mapped, "is_admin": is_admin}
