import math
import pytest
from unittest.mock import AsyncMock
from memory_system.core.long_term import LongTermMemory


@pytest.fixture
def es_mock():
    return AsyncMock()


@pytest.fixture
def emb_client_mock():
    mock = AsyncMock()
    mock.get_embedding.return_value = [0.1, 0.2, 0.3]
    return mock


@pytest.fixture
def ltm(settings, emb_client_mock):
    return LongTermMemory(settings, emb_client_mock)


def test_index_name(ltm):
    assert ltm.index_name("user_123") == "memory_long_term_user_123"


@pytest.mark.asyncio
async def test_ensure_index_creates(ltm, es_mock):
    es_mock.indices.exists.return_value = False

    await ltm.ensure_index(es_mock, "user_123")

    es_mock.indices.create.assert_called_once()
    call_args = es_mock.indices.create.call_args
    body = call_args[1]["body"]
    assert "embedding" in body["mappings"]["properties"]


@pytest.mark.asyncio
async def test_ensure_index_skips_existing(ltm, es_mock):
    es_mock.indices.exists.return_value = True

    await ltm.ensure_index(es_mock, "user_123")

    es_mock.indices.create.assert_not_called()


@pytest.mark.asyncio
async def test_search(ltm, es_mock):
    es_mock.search.return_value = {
        "hits": {
            "hits": [
                {
                    "_id": "mem_1",
                    "_score": 0.95,
                    "_source": {
                        "memory": "user likes Python",
                        "importance": 0.8,
                        "created_at": "2026-05-01T10:00:00Z",
                    },
                }
            ]
        }
    }

    results = await ltm.search(es_mock, "user_123", [0.1, 0.2, 0.3])

    assert len(results) == 1
    assert results[0]["memory"] == "user likes Python"
    assert "time_decayed_score" in results[0]


@pytest.mark.asyncio
async def test_search_with_time_decay(ltm, es_mock):
    es_mock.search.return_value = {
        "hits": {"hits": []}
    }

    results = await ltm.search(es_mock, "user_123", [0.1, 0.2, 0.3])
    assert results == []


@pytest.mark.asyncio
async def test_upsert_memory(ltm, es_mock):
    doc = {
        "id": "mem_1",
        "user_id": "user_123",
        "session_id": "sess_1",
        "memory": "user likes Python",
        "memory_type": "preference",
        "importance": 0.8,
        "metadata": {"source_round": 3},
    }

    await ltm.upsert_memory(es_mock, "user_123", doc)

    es_mock.index.assert_called_once()


@pytest.mark.asyncio
async def test_delete_memory(ltm, es_mock):
    await ltm.delete_memory(es_mock, "user_123", "mem_1")

    es_mock.delete.assert_called_once_with(
        index="memory_long_term_user_123", id="mem_1"
    )
