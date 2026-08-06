"""Atomic async Redis persistence for original memory events and summaries."""

from typing import Any, Literal, Protocol

from short_term_memory.models import EventReservation, MemoryEvent, MemorySummaryEnvelope
from short_term_memory.storage.vfs_adapter import safe_component


class EventConflictError(ValueError):
    """Raised when an idempotency key is reused with a different digest."""


class AsyncRedisClient(Protocol):
    async def eval(self, script: str, numkeys: int, *args: str) -> Any: ...

    async def lrange(self, key: str, start: int, end: int) -> list[Any]: ...

    async def get(self, key: str) -> Any | None: ...

    async def set(self, key: str, value: str, **kwargs: Any) -> Any: ...


RESERVE_EVENT_SCRIPT = """
-- dream:reserve-event
local digest = redis.call('HGET', KEYS[2], 'digest')
if digest then
  if digest ~= ARGV[1] then return {'conflict', '0'} end
  return {redis.call('HGET', KEYS[2], 'status'), redis.call('HGET', KEYS[2], 'sequence')}
end
local sequence = redis.call('INCR', KEYS[1])
redis.call('HSET', KEYS[2], 'digest', ARGV[1], 'status', 'pending', 'sequence', sequence)
redis.call('EXPIRE', KEYS[2], ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return {'reserved', tostring(sequence)}
"""

COMMIT_EVENT_SCRIPT = """
-- dream:commit-event
local status = redis.call('HGET', KEYS[4], 'status')
if not status then return {'missing'} end
if redis.call('HGET', KEYS[4], 'sequence') ~= ARGV[2] then return {'sequence_conflict'} end
if redis.call('HGET', KEYS[4], 'digest') ~= ARGV[3] then return {'digest_conflict'} end
if status == 'committed' then return {'duplicate'} end
redis.call('RPUSH', KEYS[2], ARGV[1])
redis.call('HSET', KEYS[4], 'status', 'committed')
redis.call('EXPIRE', KEYS[2], ARGV[4])
redis.call('EXPIRE', KEYS[3], ARGV[4])
redis.call('EXPIRE', KEYS[4], ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[4])
return {'committed'}
"""

CAS_ENVELOPE_SCRIPT = """
-- dream:compare-and-set-envelope
local current = redis.call('GET', KEYS[1])
if current then
  local parsed = cjson.decode(current)
  if tostring(parsed.version) ~= ARGV[1] then return {'0'} end
elseif ARGV[1] ~= '0' then
  return {'0'}
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return {'1'}
"""

RELEASE_LEASE_SCRIPT = """
-- dream:release-compression-lease
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return {'0'} end
redis.call('DEL', KEYS[1])
return {'1'}
"""


class AsyncRedisMemoryStore:
    def __init__(self, client: AsyncRedisClient, *, ttl_seconds: int = 43_200) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self.client = client
        self.ttl_seconds = ttl_seconds

    async def reserve_event(
        self, user_id: str, session_id: str, event_id: str, digest: str
    ) -> EventReservation:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("digest must be a SHA-256 hex digest")
        keys = self._keys(user_id, session_id, event_id)
        result = await self.client.eval(
            RESERVE_EVENT_SCRIPT,
            2,
            keys.sequence,
            keys.event,
            digest,
            str(self.ttl_seconds),
            str(self.ttl_seconds),
        )
        state, sequence = self._result(result)
        if state == "conflict":
            raise EventConflictError("event_id is already reserved for another digest")
        return EventReservation(sequence=int(sequence), state=state)

    async def commit_event(
        self, user_id: str, session_id: str, event: MemoryEvent
    ) -> Literal["committed", "duplicate"]:
        keys = self._keys(user_id, session_id, event.event_id)
        result = await self.client.eval(
            COMMIT_EVENT_SCRIPT,
            4,
            keys.sequence,
            keys.messages,
            keys.summary,
            keys.event,
            event.model_dump_json(),
            str(event.sequence),
            event.sha256,
            str(self.ttl_seconds),
        )
        status = self._result(result)[0]
        if status in {"committed", "duplicate"}:
            return status
        if status == "missing":
            raise ValueError("event must be reserved before it is committed")
        if status == "digest_conflict":
            raise EventConflictError("event digest does not match its reservation")
        raise ValueError("event sequence does not match its reservation")

    async def read_recent_originals(
        self, user_id: str, session_id: str, history_turns: int
    ) -> tuple[MemoryEvent, ...]:
        if history_turns < 1:
            raise ValueError("history_turns must be positive")
        keys = self._keys(user_id, session_id)
        return self._events(await self.client.lrange(keys.messages, -history_turns, -1))

    async def read_originals_after(
        self, user_id: str, session_id: str, sequence: int
    ) -> tuple[MemoryEvent, ...]:
        if sequence < 0:
            raise ValueError("sequence must not be negative")
        keys = self._keys(user_id, session_id)
        return tuple(
            event
            for event in self._events(await self.client.lrange(keys.messages, 0, -1))
            if event.sequence > sequence
        )

    async def read_envelope(
        self, user_id: str, session_id: str
    ) -> MemorySummaryEnvelope | None:
        value = await self.client.get(self._keys(user_id, session_id).summary)
        if value is None:
            return None
        return MemorySummaryEnvelope.model_validate_json(self._text(value))

    async def compare_and_set_envelope(
        self,
        user_id: str,
        session_id: str,
        expected_version: int,
        envelope: MemorySummaryEnvelope,
    ) -> bool:
        if expected_version < 0:
            raise ValueError("expected_version must not be negative")
        result = await self.client.eval(
            CAS_ENVELOPE_SCRIPT,
            1,
            self._keys(user_id, session_id).summary,
            str(expected_version),
            envelope.model_dump_json(),
            str(self.ttl_seconds),
        )
        return self._result(result)[0] == "1"

    async def acquire_compression_lease(
        self, user_id: str, session_id: str, token: str
    ) -> bool:
        if not token:
            raise ValueError("lease token must not be blank")
        result = await self.client.set(
            self._keys(user_id, session_id).compression_lock,
            token,
            nx=True,
            px=self.ttl_seconds * 1000,
        )
        return bool(result)

    async def release_compression_lease(
        self, user_id: str, session_id: str, token: str
    ) -> bool:
        if not token:
            raise ValueError("lease token must not be blank")
        result = await self.client.eval(
            RELEASE_LEASE_SCRIPT,
            1,
            self._keys(user_id, session_id).compression_lock,
            token,
        )
        return self._result(result)[0] == "1"

    @staticmethod
    def _events(values: list[Any]) -> tuple[MemoryEvent, ...]:
        return tuple(
            MemoryEvent.model_validate_json(AsyncRedisMemoryStore._text(value))
            for value in values
        )

    @staticmethod
    def _text(value: Any) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    @classmethod
    def _result(cls, result: Any) -> tuple[str, ...]:
        return tuple(cls._text(value) for value in result)

    @staticmethod
    def _keys(user_id: str, session_id: str, event_id: str | None = None) -> "_Keys":
        user = safe_component(user_id, "user_id")
        session = safe_component(session_id, "session_id")
        prefix = f"dream:session:{user}:{session}"
        return _Keys(
            sequence=f"{prefix}:sequence",
            messages=f"{prefix}:messages",
            summary=f"{prefix}:summary",
            event=(
                f"{prefix}:event:{safe_component(event_id, 'event_id')}"
                if event_id is not None
                else ""
            ),
            compression_lock=f"{prefix}:compression-lock",
        )


class _Keys:
    def __init__(
        self,
        *,
        sequence: str,
        messages: str,
        summary: str,
        event: str,
        compression_lock: str,
    ) -> None:
        self.sequence = sequence
        self.messages = messages
        self.summary = summary
        self.event = event
        self.compression_lock = compression_lock
