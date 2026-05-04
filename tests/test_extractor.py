import pytest
from unittest.mock import AsyncMock
from memory_system.core.extractor import MemoryExtractor


@pytest.fixture
def llm_mock():
    return AsyncMock()


@pytest.fixture
def emb_mock():
    mock = AsyncMock()
    mock.get_embedding.return_value = [0.1, 0.2]
    return mock


@pytest.fixture
def extractor(settings, llm_mock, emb_mock):
    return MemoryExtractor(settings, llm_mock, emb_mock)


@pytest.mark.asyncio
async def test_extract_memories_empty(extractor, llm_mock):
    llm_mock.chat_json.return_value = []

    results = await extractor.extract_memories([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ])

    assert results == []


@pytest.mark.asyncio
async def test_extract_memories_with_facts(extractor, llm_mock):
    llm_mock.chat_json.return_value = [
        {"type": "fact", "content": "用户叫张三", "importance": 0.9},
        {"type": "preference", "content": "喜欢Python", "importance": 0.7},
    ]

    results = await extractor.extract_memories([
        {"role": "user", "content": "我叫张三，我喜欢Python"},
    ])

    assert len(results) == 2
    assert results[0]["content"] == "用户叫张三"


@pytest.mark.asyncio
async def test_extract_filter_low_importance(extractor, llm_mock):
    llm_mock.chat_json.return_value = [
        {"type": "fact", "content": "重要信息", "importance": 0.9},
        {"type": "fact", "content": "不重要信息", "importance": 0.3},
    ]

    results = await extractor.extract_memories([
        {"role": "user", "content": "something"},
    ])

    assert len(results) == 1
    assert results[0]["content"] == "重要信息"


@pytest.mark.asyncio
async def test_resolve_conflicts_add(extractor, llm_mock):
    llm_mock.chat_json.return_value = [
        {
            "action": "add",
            "new_memory": {"content": "用户喜欢游泳", "importance": 0.8},
        }
    ]

    new_memories = [{"content": "用户喜欢游泳", "importance": 0.8}]
    existing = []

    actions = await extractor.resolve_conflicts(new_memories, existing)
    assert actions[0]["action"] == "add"


@pytest.mark.asyncio
async def test_resolve_conflicts_update(extractor, llm_mock):
    llm_mock.chat_json.return_value = [
        {
            "action": "update",
            "old_id": "mem_001",
            "new_content": "用户住在上海",
            "new_importance": 0.9,
        }
    ]

    new_memories = [{"content": "用户住在上海", "importance": 0.9}]
    existing = [{"_id": "mem_001", "memory": "用户住在北京", "importance": 0.8}]

    actions = await extractor.resolve_conflicts(new_memories, existing)
    assert actions[0]["action"] == "update"


@pytest.mark.asyncio
async def test_resolve_conflicts_skip(extractor, llm_mock):
    llm_mock.chat_json.return_value = [{"action": "skip", "reason": "重复"}]

    new_memories = [{"content": "用户叫张三", "importance": 0.9}]
    existing = [{"_id": "mem_001", "memory": "用户叫张三", "importance": 0.9}]

    actions = await extractor.resolve_conflicts(new_memories, existing)
    assert actions[0]["action"] == "skip"
