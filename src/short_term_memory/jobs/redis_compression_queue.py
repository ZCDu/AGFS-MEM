"""Durable Redis queue for deferred Headroom compression jobs."""

from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class AsyncRedisQueueClient(Protocol):
    async def eval(self, script: str, numkeys: int, *args: str) -> Any: ...


class CompressionJob(BaseModel):
    """The persistent payload; it contains no conversation content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    expected_version: int = Field(ge=0)
    requested_through_sequence: int = Field(ge=1)
    attempt: int = Field(ge=0, default=0)


@dataclass(frozen=True)
class CompressionJobLease:
    job: CompressionJob
    token: str


ENQUEUE_SCRIPT = """
-- dream:compression:enqueue
redis.call('SET', KEYS[1], ARGV[1])
if redis.call('LLEN', KEYS[2]) >= tonumber(ARGV[4]) then
  redis.call('SADD', KEYS[3], ARGV[3])
  return {'pending'}
end
redis.call('RPUSH', KEYS[2], ARGV[2])
return {'ready'}
"""

LEASE_SCRIPT = """
-- dream:compression:lease
local due = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', ARGV[1])
for _, job_id in ipairs(due) do
  if redis.call('ZREM', KEYS[2], job_id) == 1 then
    redis.call('RPUSH', KEYS[1], job_id)
  end
end
local job_id = redis.call('LPOP', KEYS[1])
if not job_id then return {''} end
redis.call('SET', KEYS[3] .. job_id, ARGV[2], 'PX', ARGV[3])
return {job_id}
"""

ACK_SCRIPT = """
-- dream:compression:ack
if redis.call('GET', KEYS[2]) ~= ARGV[1] then return {'0'} end
redis.call('DEL', KEYS[1], KEYS[2])
return {'1'}
"""

RETRY_SCRIPT = """
-- dream:compression:retry
if redis.call('GET', KEYS[2]) ~= ARGV[1] then return {'lost'} end
redis.call('DEL', KEYS[2])
redis.call('SET', KEYS[1], ARGV[2])
if tonumber(ARGV[4]) >= tonumber(ARGV[6]) then
  redis.call('ZADD', KEYS[4], ARGV[5], ARGV[3])
  return {'dead'}
end
redis.call('ZADD', KEYS[3], ARGV[5], ARGV[3])
return {'retry'}
"""


class RedisCompressionQueue:
    READY_KEY = "dream:compression:ready"
    RETRY_KEY = "dream:compression:retry"
    PENDING_KEY = "dream:compression:pending"
    DEAD_KEY = "dream:compression:dead"
    JOB_PREFIX = "dream:compression:job:"
    LEASE_PREFIX = "dream:compression:lease:"

    def __init__(
        self,
        client: AsyncRedisQueueClient,
        *,
        capacity: int = 10_000,
        lease_seconds: int = 300,
        max_attempts: int = 5,
        initial_backoff_seconds: int = 1,
        max_backoff_seconds: int = 300,
    ) -> None:
        if min(capacity, lease_seconds, max_attempts, initial_backoff_seconds, max_backoff_seconds) < 1:
            raise ValueError("queue limits must be positive")
        self.client = client
        self.capacity = capacity
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.initial_backoff_seconds = initial_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds

    async def enqueue(self, job: CompressionJob) -> str:
        result = await self.client.eval(
            ENQUEUE_SCRIPT,
            3,
            self._job_key(job.job_id),
            self.READY_KEY,
            self.PENDING_KEY,
            job.model_dump_json(),
            job.job_id,
            self._session_key(job),
            str(self.capacity),
        )
        return self._text(result[0])

    async def lease(
        self, worker_token: str, *, now_unix_ms: int
    ) -> CompressionJobLease | None:
        if not worker_token:
            raise ValueError("worker_token must not be blank")
        result = await self.client.eval(
            LEASE_SCRIPT,
            3,
            self.READY_KEY,
            self.RETRY_KEY,
            self.LEASE_PREFIX,
            str(now_unix_ms),
            worker_token,
            str(self.lease_seconds * 1000),
        )
        job_id = self._text(result[0])
        if not job_id:
            return None
        # Job payload was written before the ID became ready; a missing payload is
        # treated as an invalid durable record rather than inventing work.
        payload = await self._get(self._job_key(job_id))
        if payload is None:
            return None
        return CompressionJobLease(
            job=CompressionJob.model_validate_json(self._text(payload)), token=worker_token
        )

    async def ack(self, lease: CompressionJobLease) -> bool:
        result = await self.client.eval(
            ACK_SCRIPT,
            2,
            self._job_key(lease.job.job_id),
            self._lease_key(lease.job.job_id),
            lease.token,
        )
        return self._text(result[0]) == "1"

    async def retry(self, lease: CompressionJobLease, *, now_unix_ms: int) -> str:
        job = lease.job.model_copy(update={"attempt": lease.job.attempt + 1})
        backoff_seconds = min(
            self.max_backoff_seconds,
            self.initial_backoff_seconds * (2 ** (job.attempt - 1)),
        )
        due = now_unix_ms + backoff_seconds * 1_000
        result = await self.client.eval(
            RETRY_SCRIPT,
            4,
            self._job_key(job.job_id),
            self._lease_key(job.job_id),
            self.RETRY_KEY,
            self.DEAD_KEY,
            lease.token,
            job.model_dump_json(),
            job.job_id,
            str(job.attempt),
            str(due),
            str(self.max_attempts),
        )
        return self._text(result[0])

    async def _get(self, key: str) -> Any | None:
        get = getattr(self.client, "get", None)
        if get is None:
            raise TypeError("Redis queue client must implement get")
        return await get(key)

    @classmethod
    def _job_key(cls, job_id: str) -> str:
        return f"{cls.JOB_PREFIX}{job_id}"

    @classmethod
    def _lease_key(cls, job_id: str) -> str:
        return f"{cls.LEASE_PREFIX}{job_id}"

    @staticmethod
    def _session_key(job: CompressionJob) -> str:
        return f"{job.user_id}:{job.session_id}"

    @staticmethod
    def _text(value: Any) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)
