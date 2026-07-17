import json
from types import SimpleNamespace

from dream.review.llm_backend import OpenAIReviewBackend
from dream.review.models import ArtifactKind, ReviewRequest


class FakeCompletions:
    def __init__(self, tool_calls: list[object]) -> None:
        self.tool_calls = tool_calls
        self.kwargs: dict[str, object] = {}

    def create(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        message = SimpleNamespace(tool_calls=self.tool_calls, content=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _tool_call(name: str, arguments: dict[str, object]) -> object:
    return SimpleNamespace(
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments))
    )


def test_llm_backend_parses_both_priority_management_tools() -> None:
    completions = FakeCompletions(
        [
            _tool_call(
                "memory_manage",
                {"action": "add", "content": "Prefers concise answers."},
            ),
            _tool_call(
                "decision_card_manage",
                {
                    "id": "verify-before-risky-action",
                    "title": "高风险操作前先验证",
                    "scenario": "用户要求执行不可逆操作",
                    "signals": ["不可逆"],
                    "principle": "先验证，再执行。",
                    "outcome": "避免错误修改。",
                    "boundaries": "低风险操作除外。",
                    "confidence": 0.86,
                },
            ),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    backend = OpenAIReviewBackend(client=client, model="review-model")

    result = backend.review(
        ReviewRequest(
            event_id="evt-llm",
            transcript_text="User: please be concise",
            final_response="Understood.",
            allowed_tools=frozenset({"memory_manage", "decision_card_manage"}),
            current_user_profile="",
            current_decision_rules="",
            current_decision_cards=(),
        )
    )

    assert {action.kind for action in result.actions} == {
        ArtifactKind.USER_PROFILE,
        ArtifactKind.DECISION_CARD,
    }
    assert all(action.source_event_id == "evt-llm" for action in result.actions)
    assert completions.kwargs["model"] == "review-model"
    assert {tool["function"]["name"] for tool in completions.kwargs["tools"]} == {
        "memory_manage",
        "decision_card_manage",
    }


def test_llm_backend_does_not_offer_a_disallowed_management_tool() -> None:
    completions = FakeCompletions([])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    backend = OpenAIReviewBackend(client=client, model="review-model")

    backend.review(
        ReviewRequest(
            event_id="evt-profile-only",
            transcript_text="User: remember this preference",
            final_response="Saved.",
            allowed_tools=frozenset({"memory_manage"}),
        )
    )

    assert [
        tool["function"]["name"] for tool in completions.kwargs["tools"]
    ] == ["memory_manage"]


def test_llm_review_keeps_user_facts_out_of_shared_ai_cards() -> None:
    completions = FakeCompletions([])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    backend = OpenAIReviewBackend(client=client, model="review-model")

    backend.review(
        ReviewRequest(
            event_id="evt-private-user-fact",
            transcript_text="User: my private preference is concise answers",
            final_response="Understood.",
            allowed_tools=frozenset({"memory_manage", "decision_card_manage"}),
        )
    )

    system_prompt = completions.kwargs["messages"][0]["content"]
    assert "Never copy a user's personal facts" in system_prompt
    assert "user-agnostic" in system_prompt


def test_llm_review_accepts_json_only_provider() -> None:
    class JsonOnlyCompletions:
        def create(self, **kwargs: object) -> object:
            if "tools" in kwargs:
                raise RuntimeError("tools unsupported")
            content = json.dumps(
                {
                    "tool_calls": [
                        {
                            "name": "memory_manage",
                            "arguments": {
                                "action": "add",
                                "content": "Prefers concise answers.",
                            },
                        }
                    ]
                }
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=JsonOnlyCompletions())
    )
    backend = OpenAIReviewBackend(client=client, model="agnes-model")

    result = backend.review(
        ReviewRequest(
            event_id="evt-json",
            transcript_text="User: concise please",
            final_response="Understood.",
            allowed_tools=frozenset({"memory_manage"}),
        )
    )

    assert result.status == "success"
    assert result.actions[0].kind is ArtifactKind.USER_PROFILE
