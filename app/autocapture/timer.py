"""Background auto-capture timer.

Wires the capture pipeline (app/autocapture/scheduler.capture_day) to a
scheduled timer inside the app process, so memory is captured from the day's
journal without anyone pressing a button.

DESIGN
    A single daemon thread with a compute-next-run loop. It is deliberately
    lightweight:
      - It runs only when AUTO_CAPTURE_ENABLED=true (no-op otherwise, so the
        default behaviour — everything at the explicit /extract step — is
        unchanged).
      - The capture itself reuses the exact same assessor->LLM extract->route
        ->apply pipeline as /extract?apply=true, so the "auto" is only the
        TRIGGER, never a different write path.
      - A cursor (autocapture/cursor.json) makes each date:session idempotent:
        a restart does not re-store yesterday's memories.
      - It targets "yesterday" by default (AUTO_CAPTURE_LOOKBACK_DAYS=0), so
        the full day's journal is final before capture. This runs once per
        day at AUTO_CAPTURE_TIME.

    A timer rather than a full scheduler library: the job is a single daily
    task, and a library (APScheduler etc.) would need threading anyway for an
    in-process FastAPI service. The loop recomputes the next run time after
    each fire and sleeps until then, so clock edits are handled at the next
    wake.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.deps import get_storage_backend
from app.autocapture.scheduler import capture_day

logger = logging.getLogger("memory_backend.autocapture.timer")

# The scheduled thread should notice config/clock changes within this bound.
_MAX_SLEEP_SECONDS = 15 * 60


def _parse_hhmm(value: str) -> tuple[int, int]:
    """Parse 'HH:MM' -> (hour, minute). Raises ValueError on bad input so a
    misconfigured deployment fails loudly at startup rather than silently
    never running."""
    parts = (value or "").split(":")
    if len(parts) != 2:
        raise ValueError(f"AUTO_CAPTURE_TIME must be HH:MM, got {value!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"AUTO_CAPTURE_TIME must be HH:MM, got {value!r}") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"AUTO_CAPTURE_TIME out of range: {value!r}")
    return hour, minute


def _next_run_utc(hour: int, minute: int, tz_name: str) -> datetime:
    """Next (UTC-aware) datetime whose wall clock in `tz_name` is HH:MM.

    Uses UTC arithmetic throughout so the loop always compares against
    datetime.now(timezone.utc): the scheduler is agnostic to the host's local
    timezone. Falls back to UTC interpretation if the tz is unknown."""
    tz = timezone.utc
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception:
            logger.warning("unknown AUTO_CAPTURE_TZ %r; using UTC", tz_name)
            tz = timezone.utc
    now = datetime.now(timezone.utc)
    local_now = now.astimezone(tz)
    candidate_local = local_now.replace(hour=hour, minute=minute, second=0,
                                        microsecond=0)
    candidate_utc = candidate_local.astimezone(timezone.utc)
    if candidate_utc <= now:
        candidate_utc += timedelta(days=1)
    return candidate_utc


class AutoCaptureTimer:
    """Runs capture_day() once per day at the configured time, in a thread."""

    def __init__(self, settings: Settings):
        self._settings = settings
        # Runtime-settable schedule (overrides the frozen Settings on set_time).
        self._time: str = settings.auto_capture_time
        self._tz: str = settings.auto_capture_tz
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_report = None
        self._diary_report = None
        self._wait_event = threading.Event()

    def set_time(self, hhmm: str, tz: str | None = None) -> None:
        """Re-schedule the daily capture at runtime.

        Validates the time (HH:MM), updates the running timer's schedule, and
        wakes the loop so it recomputes the next run immediately — the change
        takes effect without a restart. Used by the admin/test endpoint.
        Returns nothing; raises ValueError on a bad HH:MM.
        """
        _parse_hhmm(hhmm)  # validate
        self._time = hhmm
        if tz is not None:
            self._tz = tz
        logger.info("auto-capture timer re-scheduled to %s tz=%r", hhmm, self._tz)
        if self._thread is not None and self._thread.is_alive():
            self._wait_event.set()  # wake the loop so it recomputes next run

    @property
    def schedule(self) -> dict:
        """Current schedule + last/next run info, for the status endpoint."""
        return {
            "enabled": self.enabled,
            "time": self._time,
            "tz": self._tz or "system-local",
            "lookback_days": self._settings.auto_capture_lookback_days,
            "backfill_days": self._settings.auto_capture_backfill_days,
            "last_report": self._last_report,
            "diary_report": self._diary_report,
        }

    @property
    def enabled(self) -> bool:
        return self._settings.auto_capture_enabled

    def start(self) -> None:
        """Start the background thread if enabled. Idempotent."""
        if not self.enabled:
            logger.info("auto-capture timer disabled (AUTO_CAPTURE_ENABLED=false)")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="autocapture",
                                        daemon=True)
        self._thread.start()
        logger.info("auto-capture timer started (daily at %s tz=%r; backfill %d days)",
                    self._time, self._tz or "system-local",
                    self._settings.auto_capture_backfill_days)

    def stop(self) -> None:
        self._stop.set()
        self._wait_event.set()  # interrupt an in-progress wait

    def _loop(self) -> None:
        # On startup, catch up any days missed while the service was down. This
        # is idempotent (cursor), so it can never double-store; it just closes
        # the gap so "closed overnight / over the weekend" self-heals.
        self._backfill_missed()
        while not self._stop.is_set():
            # Re-read the runtime schedule every iteration so a set_time()
            # (which wakes us) takes effect immediately.
            try:
                hour, minute = _parse_hhmm(self._time)
            except ValueError as e:
                logger.error("auto-capture time invalid %r: %s", self._time, e)
                return
            next_run = _next_run_utc(hour, minute, self._tz)
            wait = (next_run - datetime.now(timezone.utc)).total_seconds()
            logger.info("auto-capture next run at %s (in %.0fs)", next_run, wait)
            # Block until the next run time is reached, OR the max-sleep slice
            # elapses, OR set_time()/stop() wakes us. Whichever comes first.
            slice_s = min(max(wait, 0), _MAX_SLEEP_SECONDS)
            self._wait_event.wait(slice_s)
            self._wait_event.clear()
            if self._stop.is_set():
                break
            if datetime.now(timezone.utc) < next_run:
                continue  # not due yet (sliced wake or a reschedule); re-evaluate
            self._fire()

    def _backfill_missed(self) -> None:
        """Run backfill over the trailing window once at startup (oldest->newest)."""
        days = self._settings.auto_capture_backfill_days
        if days <= 0:
            logger.info("auto-capture startup backfill disabled (BACKFILL_DAYS=%d)",
                        days)
            return
        logger.info("auto-capture startup backfill: scanning last %d days", days)
        try:
            from app.autocapture.scheduler import backfill
            reports = backfill(get_storage_backend(), days=days)
            totals = {"stored": 0, "skipped": 0, "failed": 0}
            for r in reports:
                totals["stored"] += r.stored
                totals["skipped"] += r.skipped
                totals["failed"] += r.failed
            self._last_report = totals
            logger.info("auto-capture startup backfill complete: %s", totals)
        except Exception as e:
            logger.exception("auto-capture startup backfill failed: %s", e)
            self._last_report = {"error": str(e)}

    def _fire(self) -> None:
        lookback = self._settings.auto_capture_lookback_days
        target = (datetime.now(timezone.utc) - timedelta(days=lookback)).date()
        logger.info("auto-capture firing for %s", target)
        try:
            report = capture_day(get_storage_backend(), target=target)
            self._last_report = report
            logger.info("auto-capture done for %s: %s", target, report.to_dict())
        except Exception as e:
            logger.exception("auto-capture run failed: %s", e)
            self._last_report = {"error": str(e)}
        # After capture, run the scheduled diary+summary pass: generate a
        # chronological "diary" of the day's activity for every user who has a
        # home wiki, stored under their home wiki so it is visible to them (and
        # to admins for oversight). Best-effort and independent: a diary failure
        # must never make the capture look like it failed, and a config without
        # DIARY support just skips.
        try:
            if not self._settings.diary_enabled:
                logger.info("diary pass disabled (DIARY_ENABLED=false)")
                self._diary_report = {"skipped": True}
            else:
                from app.views.diary import run_diary_pass
                self._diary_report = run_diary_pass(get_storage_backend(), day=target)
        except Exception:
            logger.exception("diary pass failed after capture")
