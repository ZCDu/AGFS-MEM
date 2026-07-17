"""Provider-neutral structured chat completions with safe bounded retries."""

from dataclasses import dataclass
import json
from typing import Any


@dataclass(frozen=True)
class StructuredToolCall:
    name: str
    arguments: dict[str, object]


class StructuredCompletionError(RuntimeError):
    """Safe failure that excludes provider responses and credentials."""


def _attribute(value: object, name: str, default: object = None) -> object:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class StructuredCompletionClient:
    def __init__(
        self,
        client: Any,
        model: str,
        max_attempts: int = 3,
        max_completion_tokens: int | None = None,
    ) -> None:
        if max_attempts != 3:
            raise ValueError("structured completion uses exactly three total attempts")
        self.client = client
        self.model = model
        self.max_attempts = max_attempts
        self.max_completion_tokens = max_completion_tokens

    def call(
        self,
        *,
        system: str,
        content: str,
        tools: tuple[dict[str, object], ...],
        forced_tool: str | None,
        mode: str,
        allow_empty: bool = False,
    ) -> tuple[StructuredToolCall, ...]:
        if mode not in {"auto", "tools", "json"}:
            raise ValueError("structured mode must be auto, tools, or json")
        if not tools:
            return ()
        attempts = (
            ("tools", "json", "json")
            if mode == "auto"
            else (mode,) * self.max_attempts
        )
        last_category = "structured completion failed"
        for selected in attempts:
            try:
                return self._call_once(
                    system=system,
                    content=content,
                    tools=tools,
                    forced_tool=forced_tool,
                    mode=selected,
                    allow_empty=allow_empty,
                )
            except Exception as exc:
                last_category = type(exc).__name__
        raise StructuredCompletionError(last_category)

    def _call_once(
        self,
        *,
        system: str,
        content: str,
        tools: tuple[dict[str, object], ...],
        forced_tool: str | None,
        mode: str,
        allow_empty: bool,
    ) -> tuple[StructuredToolCall, ...]:
        allowed = {str(tool["function"]["name"]) for tool in tools}
        kwargs: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": 0,
        }
        if self.max_completion_tokens is not None:
            kwargs["max_completion_tokens"] = self.max_completion_tokens
        if mode == "tools":
            kwargs["tools"] = list(tools)
            kwargs["tool_choice"] = (
                {"type": "function", "function": {"name": forced_tool}}
                if forced_tool
                else "auto"
            )
        else:
            kwargs["response_format"] = {"type": "json_object"}
            kwargs["messages"][0]["content"] = (
                f"{system}\nReturn only JSON with this envelope: "
                '{"tool_calls":[{"name":"allowed tool","arguments":{}}]}'
            )
        response = self.client.chat.completions.create(**kwargs)
        choices = _attribute(response, "choices", []) or []
        if not choices:
            raise ValueError("no choices")
        message = _attribute(choices[0], "message")
        if mode == "tools":
            raw_calls = _attribute(message, "tool_calls", []) or []
            calls = [
                {
                    "name": str(_attribute(_attribute(call, "function"), "name", "")),
                    "arguments": json.loads(
                        str(
                            _attribute(
                                _attribute(call, "function"), "arguments", "{}"
                            )
                        )
                    ),
                }
                for call in raw_calls
            ]
        else:
            payload = json.loads(str(_attribute(message, "content", "")))
            calls = payload.get("tool_calls", []) if isinstance(payload, dict) else []
        result: list[StructuredToolCall] = []
        for call in calls:
            name = str(call.get("name", ""))
            arguments = call.get("arguments")
            if name not in allowed or not isinstance(arguments, dict):
                raise ValueError("invalid structured tool call")
            if forced_tool is not None and name != forced_tool:
                raise ValueError("unexpected forced tool")
            result.append(StructuredToolCall(name=name, arguments=arguments))
        if not result and not allow_empty:
            raise ValueError("no structured tool calls")
        return tuple(result)
