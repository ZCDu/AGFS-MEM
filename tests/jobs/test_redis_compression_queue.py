import json

import pytest

from short_term_memory.jobs.redis_compression_queue import CompressionJob, RedisCompressionQueue


class QueueRedis:
    def __init__(self):
        self.values = {}
        self.ready = []
        self.leases = {}
        self.retry_jobs = {}
        self.dead = {}
        self.pending = set()

    async def get(self, key):
        return self.values.get(key)

    async def eval(self, script, numkeys, *args):
        keys, values = args[:numkeys], args[numkeys:]
        if "dream:compression:enqueue" in script:
            job_key, ready_key, pending_key = keys
            payload, job_id, session_key, capacity = values
            self.values[job_key] = payload
            if len(self.ready) >= int(capacity):
                self.pending.add(session_key)
                return ["pending"]
            self.ready.append(job_id)
            return ["ready"]
        if "dream:compression:lease" in script:
            ready_key, retry_key, lease_prefix = keys
            now, token, lease_ms = values
            for job_id, due in list(self.retry_jobs.items()):
                if due <= int(now):
                    self.ready.append(job_id)
                    del self.retry_jobs[job_id]
            if not self.ready:
                return [""]
            job_id = self.ready.pop(0)
            self.leases[f"{lease_prefix}{job_id}"] = token
            return [job_id]
        if "dream:compression:ack" in script:
            job_key, lease_key = keys
            token = values[0]
            if self.leases.get(lease_key) != token:
                return ["0"]
            self.values.pop(job_key, None)
            self.leases.pop(lease_key, None)
            return ["1"]
        if "dream:compression:retry" in script:
            job_key, lease_key, retry_key, dead_key = keys
            token, payload, job_id, attempt, due, max_attempts = values
            if self.leases.get(lease_key) != token:
                return ["lost"]
            self.leases.pop(lease_key, None)
            self.values[job_key] = payload
            if int(attempt) >= int(max_attempts):
                self.dead[job_id] = int(due)
                return ["dead"]
            self.retry_jobs[job_id] = int(due)
            return ["retry"]
        raise AssertionError("unsupported queue script")


def compression_job(*, through_sequence=10, expected_version=0):
    return CompressionJob(
        job_id=f"job-{through_sequence}-{expected_version}",
        user_id="u",
        session_id="s",
        expected_version=expected_version,
        requested_through_sequence=through_sequence,
        attempt=0,
    )


@pytest.mark.asyncio
async def test_queue_persists_then_leases_and_acks_a_job():
    redis = QueueRedis()
    queue = RedisCompressionQueue(redis, capacity=2, lease_seconds=30)
    job = compression_job()

    assert await queue.enqueue(job) == "ready"
    lease = await queue.lease("worker-1", now_unix_ms=10)

    assert lease is not None
    assert lease.job == job
    assert json.loads(redis.values["dream:compression:job:job-10-0"])["job_id"] == job.job_id
    assert await queue.ack(lease) is True
    assert "dream:compression:job:job-10-0" not in redis.values


@pytest.mark.asyncio
async def test_queue_overflow_is_recorded_and_retry_becomes_dead_letter():
    redis = QueueRedis()
    queue = RedisCompressionQueue(
        redis, capacity=1, max_attempts=2, initial_backoff_seconds=1
    )
    first, second = compression_job(through_sequence=1), compression_job(through_sequence=2)
    assert await queue.enqueue(first) == "ready"
    assert await queue.enqueue(second) == "pending"
    assert redis.pending == {"u:s"}

    lease = await queue.lease("worker-1", now_unix_ms=100)
    assert lease is not None
    assert await queue.retry(lease, now_unix_ms=100) == "retry"
    retry_lease = await queue.lease("worker-2", now_unix_ms=1_100)
    assert retry_lease is not None
    assert await queue.retry(retry_lease, now_unix_ms=1_100) == "dead"
    assert "job-1-0" in redis.dead
