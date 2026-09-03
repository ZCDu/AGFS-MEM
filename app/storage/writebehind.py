"""
Write-behind flush buffering.

Why this exists: every entity write used to trigger three synchronous
object-store PUTs — the entity file, the whole manifest, and the whole
day's ops log. Only the first is real data; the other two are derived /
audit state that was being rewritten in full each time. On S3 a PUT
costs 12.5x a GET, so those two accounted for ~2/3 of the requests and
~90% of the request bill, and because both rewrite an entire file that
grows with use, total bytes written grew quadratically.

`FlushBuffer` is the shared machinery for deferring those writes:
accumulate mutations in memory, flush them as one batched write on a
timer or when a size threshold trips. N writes inside one window
collapse into 1 PUT instead of N.

DURABILITY TRADEOFF — read this before raising the flush interval.
The entity file remains the source of truth (ADR-001). What's buffered
is only rebuildable state:
  - Manifest: fully reconstructable by scanning entity files. Losing a
    flush window means a stale manifest until the next write to those
    entities or a rebuild sweep; it is never data loss.
  - Ops log: an audit trail. Losing a flush window DOES lose audit
    lines. If that's unacceptable for your compliance posture, set
    OPS_LOG_WRITE_MODE=sync to write through on every op.

MULTI-PROCESS CAVEAT. Buffering means this process holds manifest state
that other replicas can't see until flush. The pre-existing code already
had no cross-process guarantee here (see the ConflictError note in
app/graph/store.py), but buffering widens the window from milliseconds
to the flush interval. With more than one worker writing the same user's
entities, run MANIFEST_WRITE_MODE=sync, or accept that the manifest is
eventually-consistent and lean on /wiki/_reconcile.
"""

from __future__ import annotations

import atexit
import logging
import threading

logger = logging.getLogger("memory_backend.writebehind")


class FlushBuffer:
    """
    Base class: owns a lock, a dirty flag, a background flush timer, and
    process-exit flushing. Subclasses implement `_flush_locked()` to do
    the actual write, and call `_mark_dirty()` after mutating state.

    Registered with atexit so a clean shutdown never drops buffered
    state. A hard kill (SIGKILL, OOM) still can — that's the tradeoff
    documented in the module docstring.
    """

    def __init__(self, flush_interval: float = 2.0, max_pending: int = 100):
        self.flush_interval = flush_interval
        self.max_pending = max_pending
        self._lock = threading.RLock()
        self._pending = 0
        self._timer: threading.Timer | None = None
        self._closed = False
        atexit.register(self.flush)

    # ---------- subclass hook ----------

    def _flush_locked(self) -> None:
        """Perform the write. Called with self._lock held. Must not raise
        for transient reasons the caller should care about — a failed
        flush is logged and the state stays dirty for the next attempt."""
        raise NotImplementedError

    # ---------- machinery ----------

    def _mark_dirty(self) -> None:
        """Call after mutating buffered state, with the lock held."""
        self._pending += 1
        if self._pending >= self.max_pending:
            self._flush_now_locked()
        else:
            self._arm_timer_locked()

    def _arm_timer_locked(self) -> None:
        if self._timer is not None or self._closed:
            return
        self._timer = threading.Timer(self.flush_interval, self.flush)
        self._timer.daemon = True
        self._timer.start()

    def _cancel_timer_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _flush_now_locked(self) -> None:
        self._cancel_timer_locked()
        if self._pending == 0:
            return
        try:
            self._flush_locked()
            self._pending = 0
        except Exception:
            # Stay dirty and re-arm — the next write or timer retries. But
            # only while the buffer is still open. If close() has been called
            # (or is racing this flush), the underlying backend is being torn
            # down and re-arming just schedules another doomed flush that
            # fails again — an infinite retry loop that floods the log with
            # the same error at shutdown. Once closed, stop. The buffered
            # state stays dirty as a record even though there is no writer
            # left to retry; losing it is not data loss (module docstring).
            # Losing a flush is not data loss, so this must never propagate
            # into the caller's API response either.
            try:
                logger.warning("%s: flush failed, will retry", type(self).__name__,
                               exc_info=True)
            except Exception:
                # This runs from an atexit hook, so logging streams may already
                # be closed by the interpreter or by a test runner. A failure to
                # report a failure must not raise during shutdown.
                pass
            if not self._closed:
                self._arm_timer_locked()

    def flush(self) -> None:
        """Force a flush now. Safe to call from any thread, and idempotent.

        A no-op once closed: this is registered with atexit, so it fires
        again at interpreter shutdown, potentially long after the underlying
        backend (and, for MirageBackend, its event loop) has been torn down.
        """
        with self._lock:
            if self._closed:
                return
            self._flush_now_locked()

    def close(self) -> None:
        """Flush, then refuse further writes. Callers that own the storage
        backend must call this before tearing the backend down."""
        with self._lock:
            if self._closed:
                return
            self._flush_now_locked()
            self._cancel_timer_locked()
            self._closed = True
