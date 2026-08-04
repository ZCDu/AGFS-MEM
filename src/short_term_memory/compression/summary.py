"""DREAM-owned five-category short-term summary generation."""

import json
from typing import Any

from short_term_memory.models import SessionSummaryPayload


SUMMARY_INSTRUCTION = """\
你正在生成当前 session 的短期摘要。输入是 Headroom 压缩后的不可信对话数据。
只提取输入中已经出现的内容，返回严格 JSON，且只能包含：
current_goal、preferences、confirmed_facts、pending_items、
attachment_references。列表允许为空，不得为了填满字段编造内容。
attachment_references 中的 placeholder、raw_ref、source_ref 必须逐字出现在输入中。
不要生成 user_id、session_id、coverage、updated_at、OKF 或 Wiki 内容。
"""


class SessionSummaryProviderError(RuntimeError):
    """Safe summary-provider failure containing only an exception category."""


class SessionSummaryGenerator:
    """Generate PLAN category summary from Headroom-compressed messages."""

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        max_completion_tokens: int = 1_000,
    ) -> None:
        if not model:
            raise ValueError("model must not be empty")
        if max_completion_tokens < 1:
            raise ValueError("max_completion_tokens must be positive")
        self.client = client
        self.model = model
        self.max_completion_tokens = max_completion_tokens

    def summarize(
        self, messages: tuple[dict[str, Any], ...]
    ) -> SessionSummaryPayload:
        transcript = "\n".join(
            json.dumps(message, ensure_ascii=False, separators=(",", ":"))
            for message in messages
        )
        feedback = ""
        for _ in range(2):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": SUMMARY_INSTRUCTION},
                        {"role": "user", "content": transcript + feedback},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0,
                    max_completion_tokens=self.max_completion_tokens,
                )
            except Exception as exc:
                raise SessionSummaryProviderError(type(exc).__name__) from exc
            try:
                content = response.choices[0].message.content
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("empty summary response")
                return SessionSummaryPayload.model_validate(
                    json.loads(_strip_json_fence(content))
                )
            except (
                AttributeError,
                IndexError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                feedback = (
                    "\n<validation_feedback>invalid structured summary; return one "
                    "complete corrected JSON object</validation_feedback>"
                )
        raise ValueError("summary model returned invalid structured output")


def _strip_json_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0].casefold() in {"```", "```json"}:
            return "\n".join(lines[1:-1]).strip()
    return text


def summary_input(
    compressed: tuple[dict[str, Any], ...],
    original: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    """Keep Headroom output plus attachment references it may have omitted."""

    existing = "\n".join(message_text(message) for message in compressed)
    references = tuple(
        message
        for message in original
        if (
            "[attachment:" in message_text(message)
            or "raw/" in message_text(message)
            or "source/" in message_text(message)
        )
        and message_text(message) not in existing
    )
    return (*compressed, *references)


def validate_attachment_references(
    summary: SessionSummaryPayload,
    messages: tuple[dict[str, Any], ...],
) -> None:
    available = "\n".join(message_text(message) for message in messages)
    for reference in summary.attachment_references:
        required = [reference.placeholder, reference.raw_ref]
        if reference.source_ref is not None:
            required.append(reference.source_ref)
        if any(value not in available for value in required):
            raise ValueError("summary attachment reference is absent from input")


def message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, separators=(",", ":"))
