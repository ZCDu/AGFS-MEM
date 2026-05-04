import pytest
from pydantic import ValidationError
from memory_system.api.models import (
    ContentItem,
    Message,
    MemoryRequest,
    RetrievedMemory,
    MemoryResponse,
)


def test_content_item_text():
    item = ContentItem(type="input_text", text="hello")
    assert item.text == "hello"


def test_content_item_image():
    item = ContentItem(type="input_image", image_url="http://example.com/img.jpg")
    assert item.image_url == "http://example.com/img.jpg"


def test_message_string_content():
    msg = Message(role="user", content="hello world")
    assert msg.content == "hello world"


def test_message_multimodal_content():
    msg = Message(
        role="user",
        content=[
            ContentItem(type="input_text", text="what's this?"),
            ContentItem(type="input_image", image_url="http://x.com/i.png"),
        ],
    )
    assert len(msg.content) == 2


def test_memory_request_minimal():
    req = MemoryRequest(
        userId="u1",
        sessionId="s1",
        input=[Message(role="user", content="hello")],
    )
    assert req.userId == "u1"
    assert req.sessionId == "s1"
    assert req.model == "memory-v1"


def test_memory_request_missing_user_id():
    with pytest.raises(ValidationError):
        MemoryRequest(sessionId="s1", input=[{"role": "user", "content": "hi"}])


def test_memory_request_missing_session_id():
    with pytest.raises(ValidationError):
        MemoryRequest(userId="u1", input=[{"role": "user", "content": "hi"}])


def test_retrieved_memory():
    mem = RetrievedMemory(
        id="mem_1",
        memory="user likes Python",
        score=0.95,
        created_at="2026-05-04T10:00:00Z",
        importance=0.8,
    )
    assert mem.id == "mem_1"


def test_memory_response():
    resp = MemoryResponse(
        id="resp_1",
        model="memory-v1",
        output_text="ok",
        history=[{"role": "user", "content": "hi"}],
        retrieved_memories=[],
        usage={"total_tokens": 10},
    )
    assert resp.object == "memory.response"
