"""
Login endpoints.

Deliberately NOT under the /v1/users/{user_id} routers: those require a valid
credential, and login is how you get one. These are the only unauthenticated
write endpoints in the service, which is why the throttle below exists.
"""

from __future__ import annotations

import logging
import threading
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import require_any_credential
from app.config import get_settings
from app.deps import get_storage_backend
from app.users import SessionError, UserStore, issue_session_token, verify_session_token

logger = logging.getLogger("memory_backend.auth")

router = APIRouter(prefix="/v1/auth", tags=["auth"])


# ---------------------------------------------------------------- throttle

class LoginThrottle:
    """Per (username, client-ip) failure counter with a sliding window.

    Login is unauthenticated, so without this a password is brute-forceable at
    network speed — scrypt makes each guess cost ~100ms of SERVER CPU, which
    protects the stored hash but does nothing to stop an attacker hammering the
    endpoint, and in fact turns that into a cheap denial-of-service.

    In-process only. Behind multiple workers each has its own counter, so the
    effective limit multiplies by worker count; a shared store would be needed
    to make it strict. Documented rather than hidden because the difference
    matters if this is ever exposed.
    """

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key, window: float, now: float) -> list[float]:
        recent = [t for t in self._hits.get(key, []) if now - t < window]
        if recent:
            self._hits[key] = recent
        else:
            self._hits.pop(key, None)
        return recent

    def check(self, username: str, client: str, limit: int, window: float) -> None:
        now = time.time()
        key = (username, client)
        with self._lock:
            recent = self._prune(key, window, now)
            if len(recent) >= limit:
                retry = int(window - (now - recent[0])) + 1
                raise HTTPException(
                    status_code=429,
                    detail=f"Too many failed attempts. Try again in {retry}s.",
                    headers={"Retry-After": str(retry)})

    def record_failure(self, username: str, client: str) -> None:
        with self._lock:
            self._hits.setdefault((username, client), []).append(time.time())

    def clear(self, username: str, client: str) -> None:
        with self._lock:
            self._hits.pop((username, client), None)


_throttle = LoginThrottle()


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _store() -> UserStore:
    return UserStore(get_storage_backend())


# ---------------------------------------------------------------- models

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    token: str
    token_type: str = "bearer"
    expires_at: int
    user_id: str
    username: str
    is_admin: bool


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=1)


# ---------------------------------------------------------------- routes

@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request):
    """Exchange a username and password for a short-lived session token.

    The token then goes in `Authorization: Bearer <token>` on every other
    request, exactly like a static token.
    """
    settings = get_settings()
    if settings.auth_mode == "off":
        raise HTTPException(
            status_code=400,
            detail="AUTH_MODE=off, so there is nothing to log in to.")
    if not settings.auth_secret:
        raise HTTPException(
            status_code=503,
            detail="Password login is not enabled: AUTH_SECRET is not set.")

    username = UserStore.normalise(body.username)
    client = _client(request)
    _throttle.check(username, client, settings.auth_login_max_attempts,
                    settings.auth_login_window_seconds)

    user = _store().authenticate(username, body.password)
    if user is None:
        _throttle.record_failure(username, client)
        logger.warning("failed login for %r from %s", username, client)
        # One message for every failure mode — unknown user, wrong password,
        # disabled account. Whether an account exists is not something an
        # unauthenticated caller should be able to learn.
        raise HTTPException(status_code=401, detail="Invalid username or password.",
                            headers={"WWW-Authenticate": "Bearer"})

    _throttle.clear(username, client)
    token, exp = issue_session_token(
        settings.auth_secret, user.user_id, settings.auth_session_hours,
        is_admin=user.is_admin, username=user.username)
    logger.info("login: %r -> user_id %r", user.username, user.user_id)
    return LoginResponse(token=token, expires_at=exp, user_id=user.user_id,
                         username=user.username, is_admin=user.is_admin)


@router.get("/me")
def whoami(caller: dict = Depends(require_any_credential)):
    """What the presented credential is, and what it can reach.

    Useful for debugging a 403: it tells you which user_id your token is
    actually scoped to.
    """
    return caller


@router.post("/password", status_code=204)
def change_password(body: ChangePasswordRequest, request: Request,
                    authorization: str | None = Header(default=None)):
    """Change your own password. Requires a session token, not a static one —
    static tokens are not attached to an account.

    Existing sessions stay valid until they expire, including any an attacker
    may hold. Sessions are stateless, so there is nothing to invalidate; that
    is the documented cost of avoiding a storage read per request.
    """
    settings = get_settings()
    if not settings.auth_secret:
        raise HTTPException(status_code=503, detail="Password login is not enabled.")

    token = (authorization or "")[7:].strip() if (authorization or "").lower().startswith("bearer ") else ""
    try:
        payload = verify_session_token(settings.auth_secret, token)
    except SessionError as e:
        raise HTTPException(status_code=401, detail=f"{e} A session token is required.",
                            headers={"WWW-Authenticate": "Bearer"}) from e

    username = payload.get("n")
    if not username:
        raise HTTPException(status_code=400,
                            detail="This token is not associated with an account.")

    store = _store()
    if store.authenticate(username, body.current_password) is None:
        _throttle.record_failure(username, _client(request))
        raise HTTPException(status_code=401, detail="Current password is incorrect.")

    try:
        store.set_password(username, body.new_password)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    logger.info("password changed for %r", username)
