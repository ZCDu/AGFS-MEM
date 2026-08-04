import json
from types import SimpleNamespace

from short_term_memory.compression.summary import SessionSummaryGenerator
from short_term_memory.models import SessionSummaryPayload


class RecordingCompletions:
    def __init__(self, contents: list[object]) -> None:
        self.contents = contents
        self.requests: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.requests.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.contents.pop(0))
                )
            ]
        )


def _payload() -> SessionSummaryPayload:
    return SessionSummaryPayload(
        current_goal=["完成短期记忆迁移"],
        preferences=["使用 Python SDK"],
        confirmed_facts=["journals 保存完整原文"],
        pending_items=["运行真实 Redis 测试"],
        attachment_references=[],
    )


def test_summary_generator_returns_only_five_plan_categories() -> None:
    payload = _payload()
    completions = RecordingCompletions(
        [json.dumps(payload.model_dump(), ensure_ascii=False)]
    )
    generator = SessionSummaryGenerator(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="summary-model",
    )

    summary = generator.summarize(
        ({"role": "user", "content": "compressed goal"},)
    )

    assert summary == payload
    assert set(summary.model_dump()) == {
        "current_goal",
        "preferences",
        "confirmed_facts",
        "pending_items",
        "attachment_references",
    }


def test_summary_generator_repairs_invalid_structured_output_once() -> None:
    valid = json.dumps(_payload().model_dump(), ensure_ascii=False)
    completions = RecordingCompletions(["not json", valid])
    generator = SessionSummaryGenerator(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="summary-model",
    )

    assert generator.summarize(
        ({"role": "user", "content": "compressed"},)
    ) == _payload()
    assert len(completions.requests) == 2
    assert "validation_feedback" in completions.requests[1]["messages"][1][
        "content"
    ]
