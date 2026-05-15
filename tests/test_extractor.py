import pytest
from unittest.mock import AsyncMock
from memory_system.core.extractor import MemoryExtractor


@pytest.fixture
def llm_mock():
    return AsyncMock()


@pytest.fixture
def extractor(llm_mock):
    return MemoryExtractor(llm_mock)


@pytest.mark.asyncio
async def test_extract_facts(extractor, llm_mock):
    llm_mock.extract_json_field.return_value = (
        [
            "User's name is John",
            "User is a software engineer",
        ],
        {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
    )

    facts, usage = await extractor.extract(
        [{"role": "user", "content": "My name is John, I work at Google as a SWE"}],
    )

    assert len(facts) == 2
    assert "User's name is John" in facts
    assert "User is a software engineer" in facts
    assert usage.total_tokens == 70


@pytest.mark.asyncio
async def test_extract_empty(extractor, llm_mock):
    llm_mock.extract_json_field.return_value = ([], {})

    facts, usage = await extractor.extract(
        [{"role": "user", "content": "Hi"}],
    )

    assert facts == []
    assert usage.total_tokens == 0


@pytest.mark.asyncio
async def test_extract_llm_error(extractor, llm_mock):
    llm_mock.extract_json_field.side_effect = Exception("LLM timeout")

    facts, usage = await extractor.extract(
        [{"role": "user", "content": "hello"}],
    )

    assert facts == []
    assert usage.total_tokens == 0


@pytest.mark.asyncio
async def test_update_memory_add(extractor, llm_mock):
    """No old memories → LLM returns ADD actions."""
    llm_mock.generate_json.return_value = (
        {
            "memory": [
                {"id": "1", "text": "User's name is John", "event": "ADD", "old_memory": ""},
                {"id": "2", "text": "Works at Google", "event": "ADD", "old_memory": ""},
            ]
        },
        {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
    )

    actions, usage = await extractor.update_memory(
        old_memories=[],
        new_facts=["User's name is John", "Works at Google"],
    )

    assert len(actions) == 2
    assert all(a["event"] == "ADD" for a in actions)
    assert usage.total_tokens == 80


@pytest.mark.asyncio
async def test_update_memory_update(extractor, llm_mock):
    """Similar memories exist → LLM returns UPDATE action."""
    llm_mock.generate_json.return_value = (
        {
            "memory": [
                {"id": "abc123", "text": "Lives in Beijing now", "event": "UPDATE", "old_memory": "Lives in Shanghai"},
                {"id": "def456", "text": "Works at Google as SWE", "event": "NONE", "old_memory": ""},
            ]
        },
        {"prompt_tokens": 60, "completion_tokens": 40, "total_tokens": 100},
    )

    actions, usage = await extractor.update_memory(
        old_memories=[
            {"id": "abc123", "text": "Lives in Shanghai"},
            {"id": "def456", "text": "Works at Google as SWE"},
        ],
        new_facts=["Moved to Beijing", "Still at Google"],
    )

    assert len(actions) == 2
    events = {a["id"]: a["event"] for a in actions}
    assert events["abc123"] == "UPDATE"
    assert events["def456"] == "NONE"
    assert usage.total_tokens == 100


@pytest.mark.asyncio
async def test_update_memory_delete(extractor, llm_mock):
    """Contradicting fact → LLM returns DELETE."""
    llm_mock.generate_json.return_value = (
        {
            "memory": [
                {"id": "0", "text": "Likes coffee", "event": "DELETE", "old_memory": ""},
            ]
        },
        {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60},
    )

    actions, usage = await extractor.update_memory(
        old_memories=[{"id": "0", "text": "Likes coffee"}],
        new_facts=["Dislikes coffee"],
    )

    assert len(actions) == 1
    assert actions[0]["event"] == "DELETE"
    assert usage.total_tokens == 60


@pytest.mark.asyncio
async def test_update_memory_fallback_on_parse_error(extractor, llm_mock):
    """JSON parse failure → fallback: ADD all facts."""
    llm_mock.generate_json.return_value = (
        "not valid json{}{",
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )

    actions, usage = await extractor.update_memory(
        old_memories=[{"id": "0", "text": "old fact"}],
        new_facts=["new fact"],
    )

    assert len(actions) == 1
    assert actions[0]["event"] == "ADD"
    assert actions[0]["text"] == "new fact"
    assert usage.total_tokens == 15
