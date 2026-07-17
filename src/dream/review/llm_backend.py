"""OpenAI-compatible Hermes-style Background Review backend."""

from typing import Any

from dream.hermes_compat.prompts import DREAM_COMBINED_REVIEW_PROMPT
from dream.review.models import (
    ArtifactKind,
    ReviewAction,
    ReviewRequest,
    ReviewResult,
)
from dream.structured_llm import (
    StructuredCompletionClient,
    StructuredCompletionError,
)


_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "memory_manage",
        "description": "Add, replace, or remove a durable fact in this user's isolated profile.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                "content": {"type": "string"},
                "old_content": {"type": "string"},
            },
            "required": ["action", "content"],
            "additionalProperties": False,
        },
    },
}

_DECISION_CARD_TOOL = {
    "type": "function",
    "function": {
        "name": "decision_card_manage",
        "description": "Create or update a reusable AI decision card backed by this conversation.",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "pattern": "^[a-z0-9][a-z0-9-]{0,63}$",
                },
                "title": {"type": "string"},
                "scenario": {"type": "string"},
                "signals": {"type": "array", "items": {"type": "string"}},
                "principle": {"type": "string"},
                "outcome": {"type": "string"},
                "boundaries": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": [
                "id",
                "title",
                "scenario",
                "signals",
                "principle",
                "outcome",
                "boundaries",
                "confidence",
            ],
            "additionalProperties": False,
        },
    },
}

_TOOLS = {
    "memory_manage": _MEMORY_TOOL,
    "decision_card_manage": _DECISION_CARD_TOOL,
}


class OpenAIReviewBackend:
    def __init__(
        self,
        *,
        client: Any,
        model: str,
        max_completion_tokens: int = 2000,
        structured_mode: str = "auto",
    ) -> None:
        self.client = client
        self.model = model
        self.max_completion_tokens = max_completion_tokens
        self.structured_mode = structured_mode
        self.structured = StructuredCompletionClient(
            client,
            model,
            max_completion_tokens=max_completion_tokens,
        )

    def review(self, request: ReviewRequest) -> ReviewResult:
        tools = [
            _TOOLS[name]
            for name in ("memory_manage", "decision_card_manage")
            if name in request.allowed_tools
        ]
        if not tools:
            return ReviewResult(actions=(), summary="No management tools were allowed.")
        current_cards = "\n\n".join(request.current_decision_cards) or "(none)"
        review_input = (
            "<completed_conversation>\n"
            f"{request.transcript_text}\n"
            "</completed_conversation>\n\n"
            "<foreground_final_response>\n"
            f"{request.final_response}\n"
            "</foreground_final_response>\n\n"
            "<current_user_profile>\n"
            f"{request.current_user_profile or '(empty)'}\n"
            "</current_user_profile>\n\n"
            "<current_decision_rules>\n"
            f"{request.current_decision_rules or '(empty)'}\n"
            "</current_decision_rules>\n\n"
            "<current_decision_cards>\n"
            f"{current_cards}\n"
            "</current_decision_cards>"
        )
        try:
            tool_calls = self.structured.call(
                system=DREAM_COMBINED_REVIEW_PROMPT,
                content=review_input,
                tools=tuple(tools),
                forced_tool=None,
                mode=self.structured_mode,
                allow_empty=True,
            )
        except StructuredCompletionError:
            return ReviewResult(
                actions=(),
                summary="LLM review returned invalid structured output.",
                status="failed",
                error="structured completion failed",
            )
        actions: list[ReviewAction] = []
        errors: list[str] = []
        for tool_call in tool_calls:
            name = tool_call.name
            if name not in request.allowed_tools or name not in _TOOLS:
                continue
            try:
                payload = dict(tool_call.arguments)
                if name == "memory_manage":
                    kind = ArtifactKind.USER_PROFILE
                    payload.setdefault("target", "user")
                else:
                    kind = ArtifactKind.DECISION_CARD
                actions.append(
                    ReviewAction(
                        kind=kind,
                        tool_name=name,
                        payload=payload,
                        source_event_id=request.event_id,
                    )
                )
            except (TypeError, ValueError) as exc:
                errors.append(f"{name}: {exc}")
        status = "partial" if errors and actions else "failed" if errors else "success"
        summary = (
            f"LLM review proposed {len(actions)} management action(s)."
            if actions
            else "Nothing to save."
        )
        return ReviewResult(
            actions=tuple(actions),
            summary=summary,
            status=status,
            error="; ".join(errors) or None,
        )
