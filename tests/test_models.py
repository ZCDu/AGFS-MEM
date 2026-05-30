import pytest
from pydantic import ValidationError
from memory_system.api.models import (
    ContentItem,
    Message,
    MemoryRequest,
    MemorySettings,
    HistoryMessage,
    Usage,
    RetrievedMemory,
    MemoryStoreResponse,
    MemoryRecallResponse,
)


# ── ContentItem ──────────────────────────────────────────────────────────────

def test_content_item_text():
    item = ContentItem(type="input_text", text="hello")
    assert item.text == "hello"


def test_content_item_image():
    item = ContentItem(type="input_image", image_url="http://example.com/img.jpg")
    assert item.image_url == "http://example.com/img.jpg"


def test_content_item_invalid_type():
    with pytest.raises(ValidationError):
        ContentItem(type="unknown_type", text="x")


# ── Message ──────────────────────────────────────────────────────────────────

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


def test_message_invalid_role():
    with pytest.raises(ValidationError):
        Message(role="admin", content="hello")


# ── MemoryRequest ────────────────────────────────────────────────────────────

def test_memory_request_minimal():
    req = MemoryRequest(
        userId="u1",
        sessionId="s1",
        input=[Message(role="user", content="hello")],
    )
    assert req.userId == "u1"
    assert req.sessionId == "s1"
    assert req.model == "memory-v1"


def test_memory_request_with_settings():
    req = MemoryRequest(
        userId="u1",
        sessionId="s1",
        memory_settings=MemorySettings(recent_rounds_full=5),
        input=[Message(role="user", content="hello")],
    )
    assert req.memory_settings.recent_rounds_full == 5


def test_memory_request_missing_user_id():
    with pytest.raises(ValidationError):
        MemoryRequest(sessionId="s1", input=[{"role": "user", "content": "hi"}])


def test_memory_request_missing_session_id():
    with pytest.raises(ValidationError):
        MemoryRequest(userId="u1", input=[{"role": "user", "content": "hi"}])


def test_memory_request_empty_input():
    with pytest.raises(ValidationError):
        MemoryRequest(userId="u1", sessionId="s1", input=[])


def test_memory_request_empty_user_id():
    with pytest.raises(ValidationError):
        MemoryRequest(userId="", sessionId="s1", input=[{"role": "user", "content": "hi"}])


@pytest.mark.parametrize("field", ["userId", "sessionId"])
def test_memory_request_rejects_path_like_identifiers(field):
    payload = {
        "userId": "u1",
        "sessionId": "s1",
        "input": [{"role": "user", "content": "hi"}],
    }
    payload[field] = "../escape"

    with pytest.raises(ValidationError):
        MemoryRequest(**payload)


# ── MemorySettings ───────────────────────────────────────────────────────────

def test_memory_settings_recent_rounds_non_positive():
    with pytest.raises(ValidationError):
        MemorySettings(recent_rounds_full=0)


# ── RetrievedMemory ──────────────────────────────────────────────────────────

def test_retrieved_memory():
    mem = RetrievedMemory(
        id="mem_1",
        memory="user likes Python",
        score=0.95,
        created_at="2026-05-04T10:00:00Z",
        importance=0.8,
    )
    assert mem.id == "mem_1"


def test_retrieved_memory_score_out_of_range():
    with pytest.raises(ValidationError):
        RetrievedMemory(
            id="m1", memory="x", score=2.5, created_at="2026-01-01T00:00:00Z", importance=0.5,
        )


def test_retrieved_memory_importance_out_of_range():
    with pytest.raises(ValidationError):
        RetrievedMemory(
            id="m1", memory="x", score=0.5, created_at="2026-01-01T00:00:00Z", importance=-0.1,
        )


# ── Response models ──────────────────────────────────────────────────────────

def test_store_response():
    resp = MemoryStoreResponse()
    assert resp.object == "memory.store"
    assert resp.status == "stored"


def test_recall_response():
    resp = MemoryRecallResponse(
        id="resp_1",
        model="memory-v1",
        history=[HistoryMessage(role="user", content="hi")],
        retrieved_memories=[],
        usage=Usage(total_tokens=10),
    )
    assert resp.object == "memory.recall"
    assert len(resp.history) == 1
    assert resp.history[0].role == "user"
    assert resp.usage.total_tokens == 10


def test_recall_response_json_serialization():
    resp = MemoryRecallResponse(
        history=[HistoryMessage(role="user", content="hello")],
        usage=Usage(total_tokens=5),
    )
    data = resp.model_dump()
    assert data["history"] == [{"role": "user", "content": "hello"}]
    assert data["usage"] == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 5}


# ── HistoryMessage ───────────────────────────────────────────────────────────

def test_history_message():
    msg = HistoryMessage(role="assistant", content="got it")
    assert msg.role == "assistant"
    assert msg.content == "got it"


# ── Usage ────────────────────────────────────────────────────────────────────

def test_usage_default():
    usage = Usage()
    assert usage.total_tokens == 0


def test_usage_negative():
    with pytest.raises(ValidationError):
        Usage(total_tokens=-1)
