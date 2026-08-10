"""Unit tests for AgentChatClient orchestration."""

from __future__ import annotations

import json

import httpx
import pytest

from short_term_memory.agent.agent_chat import AgentChatClient


def _recall_handler(request: httpx.Request) -> httpx.Response:
    """Mock memory API handler: read returns markers, recall returns original."""
    path = request.url.path
    if path == "/v1/memories/read":
        return httpx.Response(
            200,
            json={
                "request_id": "r1",
                "messages": [
                    {"role": "system", "content": '{"current_goal":[]}'},
                    {
                        "role": "assistant",
                        "content": "[20 items compressed. Retrieve more: hash=abc123]",
                    },
                ],
                "memory": {"source": "redis", "compression_segments": 1},
                "headroom": {
                    "proxy_url": "http://headroom:8787/v1",
                    "scope_headers": {"x-headroom-user-id": "u"},
                },
                "ccr_markers": ["abc123"],
            },
        )
    if path == "/v1/memories/recall":
        return httpx.Response(
            200,
            json={
                "results": [
                    {"hash": "abc123", "content": "ORIGINAL_ANCHOR_7391", "recovered": True}
                ]
            },
        )
    if path == "/v1/memories/write":
        return httpx.Response(200, json={"request_id": "w", "accepted": True})
    return httpx.Response(404)


class RecordingModelCall:
    """Fake model: first call requests headroom_retrieve, second answers."""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def __call__(self, **kwargs):
        messages = kwargs["messages"]
        self.calls.append(messages)
        # First call -> request headroom_retrieve
        if len(self.calls) == 1:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "headroom_retrieve",
                            "arguments": json.dumps({"hash": "abc123"}),
                        },
                    }
                ],
            }
        # Second call -> final answer, should contain recalled original
        return {"content": "BASED_ON_RECALLED_ANCHOR", "tool_calls": []}


@pytest.mark.asyncio
async def test_agent_chat_handles_recall_tool_call_loop() -> None:
    model = RecordingModelCall()
    client = AgentChatClient(
        memory_api_url="http://test",
        model_call=model,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_recall_handler)),
    )
    try:
        answer = await client.turn("u-1", "s-1", "那个文档里的细节是什么")
    finally:
        await client.aclose()

    assert answer == "BASED_ON_RECALLED_ANCHOR"
    # model called twice (tool round + final)
    assert len(model.calls) == 2
    # second call should include the tool result with recalled original
    second_messages = model.calls[1]
    assert any(
        m.get("role") == "tool" and "ORIGINAL_ANCHOR_7391" in str(m.get("content", ""))
        for m in second_messages
    )


@pytest.mark.asyncio
async def test_agent_chat_injects_retrieve_guidance() -> None:
    model = RecordingModelCall()

    async def always_answer(**kwargs):
        messages = kwargs["messages"]
        model.calls.append(messages)
        return {"content": "ok", "tool_calls": []}

    client = AgentChatClient(
        memory_api_url="http://test",
        model_call=always_answer,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_recall_handler)),
    )
    try:
        await client.turn("u-1", "s-1", "你好")
    finally:
        await client.aclose()

    first_system = model.calls[0][0]
    assert first_system["role"] == "system"
    assert "headroom_retrieve" in first_system["content"]
