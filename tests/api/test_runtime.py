from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path

from short_term_memory import build_runtime
from short_term_memory.compression.headroom_client import HeadroomHttpClient
from short_term_memory.config import (
    HeadroomServiceSettings,
    ShortTermMemorySettings,
)
from short_term_memory.models import (
    HeadroomCompressionResult,
    HeadroomCompressionStatus,
)
from tests.storage.fake_redis import FakeRedis


class FixedTokenEstimator:
    def estimate(self, messages):
        return 1


class FixedSummaryModel:
    def summarize(self, messages):
        return {
            "current_goal": [],
            "preferences": [],
            "confirmed_facts": [],
            "pending_items": [],
            "attachment_references": [],
        }


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[object, tuple[object, ...]]] = []

    def submit(self, function, *args):
        self.calls.append((function, args))
        return Future()


class RecordingRetryQueue:
    def schedule(self, *args):
        return None


class FixedCompressionClient:
    def compress(
        self,
        messages,
        *,
        model,
        correlation_id=None,
        scope_headers=None,
    ):
        return HeadroomCompressionResult(
            status=HeadroomCompressionStatus.SUCCESS,
            messages=messages,
            fallback_used=False,
            tokens_before=1,
            tokens_after=1,
            tokens_saved=0,
        )


def test_builder_exposes_only_short_term_memory_boundary(tmp_path: Path) -> None:
    base = ShortTermMemorySettings()
    settings = replace(
        base,
        redis_session=replace(
            base.redis_session,
            context_window_tokens=1,
            trigger_ratio=0.65,
        ),
    )
    executor = RecordingExecutor()

    runtime = build_runtime(
        home=tmp_path,
        settings=settings,
        redis_client=FakeRedis(),
        token_estimator=FixedTokenEstimator(),
        summary_model=FixedSummaryModel(),
        executor=executor,
        retry_queue=RecordingRetryQueue(),
        compression_client=FixedCompressionClient(),
    )

    prepared = runtime.prepare_turn(
        "user-1", "session-1", "请继续 DREAM", session_seconds=10
    )
    completed = runtime.complete_turn(
        prepared,
        assistant_content="公司 Agent 生成的回答",
    )

    assert prepared.history[-1] == {"role": "user", "content": "请继续 DREAM"}
    assert completed.headroom_queued is True
    assert len(executor.calls) == 1
    assert runtime.compression_job.store is runtime.session_context
    assert runtime.session_context.recovery_queue is runtime.compression_queue
    assert not hasattr(runtime, "memory_retriever")
    assert not hasattr(runtime, "hot_memory_index")
    assert not hasattr(runtime, "wiki_store")
    assert not hasattr(runtime, "answer_generator")
    assert not hasattr(runtime, "router")


def test_builder_accepts_replaceable_compression_client(tmp_path: Path) -> None:
    plugin = FixedCompressionClient()
    runtime = build_runtime(
        home=tmp_path,
        settings=ShortTermMemorySettings(),
        redis_client=FakeRedis(),
        token_estimator=FixedTokenEstimator(),
        summary_model=FixedSummaryModel(),
        executor=RecordingExecutor(),
        retry_queue=RecordingRetryQueue(),
        compression_client=plugin,
    )

    assert runtime.compression_client is plugin
    assert runtime.compression_job.compression_client is plugin


def test_builder_defaults_to_headroom_http_adapter(tmp_path: Path) -> None:
    settings = replace(
        ShortTermMemorySettings(),
        headroom_service=HeadroomServiceSettings(
            url="http://127.0.0.1:8787",
            timeout_seconds=123.0,
            compression_model="gpt-4o",
            ccr_ttl_seconds=7_200,
        ),
    )

    runtime = build_runtime(
        home=tmp_path,
        settings=settings,
        redis_client=FakeRedis(),
        token_estimator=FixedTokenEstimator(),
        summary_model=FixedSummaryModel(),
        executor=RecordingExecutor(),
        retry_queue=RecordingRetryQueue(),
    )

    assert isinstance(runtime.compression_client, HeadroomHttpClient)
    assert runtime.compression_client.service_url == "http://127.0.0.1:8787"
    assert runtime.compression_client.timeout_seconds == 123.0


def test_runtime_exposes_proxy_for_every_prepared_agent_turn(
    tmp_path: Path,
) -> None:
    settings = replace(
        ShortTermMemorySettings(),
        headroom_service=HeadroomServiceSettings(url="http://headroom:8787"),
    )
    runtime = build_runtime(
        home=tmp_path,
        settings=settings,
        redis_client=FakeRedis(),
        token_estimator=FixedTokenEstimator(),
        summary_model=FixedSummaryModel(),
        executor=RecordingExecutor(),
        retry_queue=RecordingRetryQueue(),
    )

    prepared = runtime.prepare_turn(
        "user", "session", "question"
    )

    assert prepared.headroom_proxy_url == "http://headroom:8787/v1"
