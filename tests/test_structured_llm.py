import json
from types import SimpleNamespace

import pytest

from dream.structured_llm import (
    StructuredCompletionClient,
    StructuredCompletionError,
)


SUMMARY_TOOL = {
    "type": "function",
    "function": {
        "name": "summarize",
        "parameters": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
}


class RejectToolsThenReturnJson:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs: object) -> object:
        self.calls += 1
        if "tools" in kwargs:
            raise RuntimeError("tools unsupported")
        content = json.dumps(
            {"tool_calls": [{"name": "summarize", "arguments": {"summary": "ok"}}]}
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


class AlwaysFail:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs: object) -> object:
        self.calls += 1
        raise RuntimeError("Authorization: Bearer secret")


def client_for(completions: object) -> object:
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def test_auto_mode_falls_back_to_json_when_tools_are_unsupported() -> None:
    completions = RejectToolsThenReturnJson()

    result = StructuredCompletionClient(client_for(completions), "agnes", 3).call(
        system="Return the schema.",
        content="input",
        tools=(SUMMARY_TOOL,),
        forced_tool="summarize",
        mode="auto",
    )

    assert result[0].arguments == {"summary": "ok"}
    assert completions.calls == 2


def test_structured_call_stops_after_initial_attempt_plus_two_retries() -> None:
    completions = AlwaysFail()

    with pytest.raises(StructuredCompletionError) as error:
        StructuredCompletionClient(client_for(completions), "agnes", 3).call(
            system="system",
            content="input",
            tools=(SUMMARY_TOOL,),
            forced_tool="summarize",
            mode="tools",
        )

    assert completions.calls == 3
    assert "secret" not in str(error.value)


def test_json_mode_rejects_unknown_tool_name() -> None:
    class UnknownTool:
        def create(self, **kwargs: object) -> object:
            content = '{"tool_calls":[{"name":"unknown","arguments":{}}]}'
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    with pytest.raises(StructuredCompletionError):
        StructuredCompletionClient(client_for(UnknownTool()), "agnes", 3).call(
            system="system",
            content="input",
            tools=(SUMMARY_TOOL,),
            forced_tool=None,
            mode="json",
        )
