"""Manual trigger for the auto-capture pipeline.

The background timer (app/autocapture/timer.py) runs automatically when
AUTO_CAPTURE_ENABLED=true, but a human should still be able to run a capture
now — to backfill, to test, or to catch up after the service was down. This
endpoint runs the exact same capture_day() the timer calls, so on-demand and
scheduled capture are one code path.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import require_user
from app.deps import get_storage_backend
from app.storage.backend import StorageBackend

router = APIRouter(dependencies=[Depends(require_user)],
                   prefix="/v1/users/{user_id}/autocapture", tags=["autocapture"])


def _timer():
    """The running AutoCaptureTimer singleton started by the app lifespan."""
    from app.main import _auto_capture_timer
    if _auto_capture_timer is None:
        # The timer is only started in the lifespan; outside it (tests, or if
        # the lifespan did not construct it), expose a read-only empty view.
        return None
    return _auto_capture_timer


@router.get("/schedule")
def get_schedule(user_id: str):
    """Read the current auto-capture schedule and last report."""
    t = _timer()
    return {"user_id": user_id, "schedule": t.schedule if t else {"enabled": False}}


class ScheduleIn(BaseModel):
    time: str | None = Field(
        None, description="HH:MM (24h) to run the daily capture, in `tz`.")
    tz: str | None = Field(
        None, description="IANA timezone for `time`, e.g. Asia/Shanghai.")


@router.post("/schedule")
def set_schedule(user_id: str, body: ScheduleIn,
                 backend: StorageBackend = Depends(get_storage_backend)):
    """Re-schedule the auto-capture timer at runtime (no restart needed).

    Minimal test hook: set the daily time (and optionally the timezone).
    Returns the new schedule. Persists nothing beyond the running process;
    to make it stick across restarts set AUTO_CAPTURE_TIME in .env instead."""
    if body.time is None:
        raise HTTPException(status_code=422,
                            detail="Provide `time` as HH:MM to re-schedule.")
    t = _timer()
    if t is None or not t.enabled:
        raise HTTPException(
            status_code=409,
            detail="Auto-capture timer is not running (AUTO_CAPTURE_ENABLED=false). "
                   "Set AUDIO_CAPTURE_ENABLED in .env and restart to enable it.")
    try:
        t.set_time(body.time, tz=body.tz)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {"user_id": user_id, "schedule": t.schedule}


@router.post("/run")
def run_capture(user_id: str,
                on: date | None = Query(
                    None, description="Day to capture, YYYY-MM-DD. Defaults to "
                                      "the lookback day (yesterday)."),
                backend: StorageBackend = Depends(get_storage_backend)):
    """Run the review-extraction + apply pipeline over a day's journal now.

    Processes only THIS user's sessions (a caller must not trigger capture of
    other users' memories). Idempotent: sessions already captured are skipped.
    """
    from app.autocapture.scheduler import capture_day
    target = on
    report = capture_day(backend, target=target, user_ids=[user_id])
    out = report.to_dict()
    out["user_id"] = user_id
    return out
