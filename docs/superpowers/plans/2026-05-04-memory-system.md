# 记忆存储与召回系统 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个 OpenAI 兼容 API 格式的记忆存储与召回系统，支持会话记忆（Redis）和长期记忆（ES + Embedding）。

**Architecture:** FastAPI 全异步单体服务，内部模块化分层。API 层兼容 OpenAI responses.create 格式，核心编排层协调 SessionManager（Redis 会话窗口）、LongTermMemory（ES 向量检索）和 MemoryExtractor（LLM 记忆提取）。所有外部调用通过 httpx AsyncClient。

**Tech Stack:** Python 3.12+, FastAPI, uvicorn, pydantic-settings, redis (async), elasticsearch-py (async), httpx, pytest + pytest-asyncio

---

### Task 1: 项目骨架搭建

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `src/memory_system/__init__.py`
- Create: `src/memory_system/api/__init__.py`
- Create: `src/memory_system/core/__init__.py`
- Create: `src/memory_system/clients/__init__.py`
- Create: `src/memory_system/utils/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: 创建 pyproject.toml**

```toml
[project]
name = "memory-system"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.30.0",
    "pydantic-settings>=2.5.0",
    "redis>=5.2.0",
    "elasticsearch[async]>=8.17.0",
    "httpx>=0.28.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3.0",
    "pytest-asyncio>=0.24.0",
    "httpx>=0.28.0",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: 创建 .env.example**

```bash
# Redis
REDIS_URL=redis://localhost:6379/0

# Elasticsearch
ES_URL=http://localhost:9200

# Embedding service
EMBEDDING_API_URL=http://localhost:8080/v1/embeddings
EMBEDDING_DIM=768

# LLM service (OpenAI-compatible)
LLM_API_URL=http://localhost:8081/v1
LLM_API_KEY=sk-local

# Session
SESSION_WINDOW_SIZE=10
SESSION_TTL_SECONDS=86400
ARCHIVED_ROUNDS_MAX=50

# Thresholds
RELEVANCE_THRESHOLD=0.7
MEM_IMPORTANCE_THRESHOLD=0.5
TIME_DECAY_LAMBDA=0.01

# Retrieval
MEM_RETRIEVAL_TOP_K=10
```

- [ ] **Step 3: 创建目录结构和空 __init__.py**

Run:
```bash
mkdir -p src/memory_system/{api,core,clients,utils} tests
touch src/memory_system/__init__.py
touch src/memory_system/api/__init__.py
touch src/memory_system/core/__init__.py
touch src/memory_system/clients/__init__.py
touch src/memory_system/utils/__init__.py
touch tests/__init__.py
```

- [ ] **Step 4: 创建 tests/conftest.py（最小占位）**

```python
import pytest

# Settings fixture will be added in Task 2 after config.py is created
```

- [ ] **Step 5: 安装依赖并验证**

```bash
cd /Users/zhaoguoqing/Project/Claude/memory
pip install -e ".[dev]"
python -c "import memory_system; print('OK')"
```

Expected: prints "OK"

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: scaffold project structure with dependencies"
```

---

### Task 2: 配置管理

**Files:**
- Create: `src/memory_system/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: 编写失败测试**

```python
# tests/test_config.py
from memory_system.config import Settings


def test_settings_defaults():
    s = Settings()
    assert s.session_window_size == 10
    assert s.session_ttl_seconds == 86400
    assert s.relevance_threshold == 0.7
    assert s.embedding_dim == 768


def test_settings_custom_values():
    s = Settings(
        redis_url="redis://custom:6379",
        session_window_size=5,
        embedding_dim=1024,
    )
    assert s.redis_url == "redis://custom:6379"
    assert s.session_window_size == 5
    assert s.embedding_dim == 1024
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_config.py -v
```
Expected: FAIL (module not found)

- [ ] **Step 3: 实现 Settings**

```python
# src/memory_system/config.py
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    session_window_size: int = 10
    session_ttl_seconds: int = 86400
    archived_rounds_max: int = 50
    relevance_threshold: float = 0.7
    mem_importance_threshold: float = 0.5
    time_decay_lambda: float = 0.01
    mem_retrieval_top_k: int = 10

    redis_url: str = "redis://localhost:6379/0"
    es_url: str = "http://localhost:9200"
    embedding_api_url: str = ""
    embedding_dim: int = 768
    llm_api_url: str = ""
    llm_api_key: str = ""
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_config.py -v
```
Expected: 2 PASS

- [ ] **Step 5: 完善 conftest.py settings fixture**

```python
# tests/conftest.py — 替换为：
import pytest
from memory_system.config import Settings


@pytest.fixture
def settings():
    return Settings(
        redis_url="redis://localhost:6379/0",
        es_url="http://localhost:9200",
        embedding_api_url="http://localhost:8080/v1/embeddings",
        embedding_dim=768,
        llm_api_url="http://localhost:8081/v1",
        llm_api_key="test-key",
        session_window_size=3,
        session_ttl_seconds=86400,
        archived_rounds_max=10,
        relevance_threshold=0.7,
        mem_importance_threshold=0.5,
        time_decay_lambda=0.01,
        mem_retrieval_top_k=5,
    )
```

- [ ] **Step 6: Commit**

```bash
git add src/memory_system/config.py tests/test_config.py tests/conftest.py
git commit -m "feat: add pydantic-settings config with defaults"
```

---

### Task 3: 文本工具（multimodal content 提取）

**Files:**
- Create: `src/memory_system/utils/text_utils.py`
- Create: `tests/test_text_utils.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_text_utils.py
from memory_system.utils.text_utils import extract_text, messages_to_text


def test_extract_text_from_string():
    assert extract_text("hello world") == "hello world"


def test_extract_text_from_content_list():
    content = [
        {"type": "input_text", "text": "what's in this image?"},
        {
            "type": "input_image",
            "image_url": "https://example.com/image.jpg",
        },
    ]
    result = extract_text(content)
    assert result == "what's in this image?"


def test_extract_text_empty_list():
    assert extract_text([]) == ""


def test_extract_text_only_image():
    content = [
        {
            "type": "input_image",
            "image_url": "https://example.com/image.jpg",
        },
    ]
    assert extract_text(content) == ""


def test_messages_to_text():
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    result = messages_to_text(messages)
    assert result == "user: hello\nassistant: hi there"


def test_messages_to_text_with_multimodal():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "what's this?"},
                {"type": "input_image", "image_url": "http://x.com/img.png"},
            ],
        },
        {"role": "assistant", "content": "that's a cat"},
    ]
    result = messages_to_text(messages)
    assert result == "user: what's this?\nassistant: that's a cat"


def test_get_last_user_query():
    from memory_system.utils.text_utils import get_last_user_query

    messages = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second question"},
    ]
    assert get_last_user_query(messages) == "second question"


def test_get_last_user_query_multimodal():
    from memory_system.utils.text_utils import get_last_user_query

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "last question"},
                {"type": "input_image", "image_url": "http://x.com/img.png"},
            ],
        },
    ]
    assert get_last_user_query(messages) == "last question"
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_text_utils.py -v
```

- [ ] **Step 3: 实现**

```python
# src/memory_system/utils/text_utils.py
def extract_text(content: str | list[dict]) -> str:
    """Extract plain text from content field (string or multimodal array)."""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if item.get("type") == "input_text" and item.get("text"):
            parts.append(item["text"])
    return " ".join(parts)


def messages_to_text(messages: list[dict]) -> str:
    """Convert messages list to a single text block for embedding/LLM."""
    lines = []
    for msg in messages:
        role = msg["role"]
        text = extract_text(msg.get("content", ""))
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


def get_last_user_query(messages: list[dict]) -> str:
    """Extract the text of the last user message."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return extract_text(msg.get("content", ""))
    return ""
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_text_utils.py -v
```
Expected: 8 PASS

- [ ] **Step 5: Commit**

```bash
git add src/memory_system/utils/text_utils.py tests/test_text_utils.py
git commit -m "feat: add multimodal content text extraction utilities"
```

---

### Task 4: Redis 客户端

**Files:**
- Create: `src/memory_system/clients/redis_client.py`
- Create: `tests/test_redis_client.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_redis_client.py
import pytest
from unittest.mock import AsyncMock, patch
from memory_system.clients.redis_client import RedisClient


@pytest.fixture
def redis_client(settings):
    return RedisClient(settings)


@pytest.mark.asyncio
async def test_get_redis_returns_client(redis_client):
    with patch("redis.asyncio.from_url") as mock_from_url:
        mock_client = AsyncMock()
        mock_from_url.return_value = mock_client

        result = await redis_client.get_redis()
        assert result is mock_client
        mock_from_url.assert_called_once()


@pytest.mark.asyncio
async def test_get_redis_reuses_connection(redis_client):
    with patch("redis.asyncio.from_url") as mock_from_url:
        mock_client = AsyncMock()
        mock_from_url.return_value = mock_client

        c1 = await redis_client.get_redis()
        c2 = await redis_client.get_redis()
        assert c1 is c2
        mock_from_url.assert_called_once()


@pytest.mark.asyncio
async def test_close(redis_client):
    with patch("redis.asyncio.from_url") as mock_from_url:
        mock_client = AsyncMock()
        mock_from_url.return_value = mock_client

        await redis_client.get_redis()
        await redis_client.close()
        mock_client.aclose.assert_called_once()
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_redis_client.py -v
```

- [ ] **Step 3: 实现 RedisClient**

```python
# src/memory_system/clients/redis_client.py
import redis.asyncio as aioredis
from memory_system.config import Settings


class RedisClient:
    def __init__(self, settings: Settings):
        self._url = settings.redis_url
        self._client: aioredis.Redis | None = None

    async def get_redis(self) -> aioredis.Redis:
        if self._client is None:
            self._client = aioredis.from_url(self._url, decode_responses=True)
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_redis_client.py -v
```
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/memory_system/clients/redis_client.py tests/test_redis_client.py
git commit -m "feat: add async Redis client with connection pooling"
```

---

### Task 5: Elasticsearch 客户端

**Files:**
- Create: `src/memory_system/clients/es_client.py`
- Create: `tests/test_es_client.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_es_client.py
import pytest
from unittest.mock import AsyncMock, patch
from memory_system.clients.es_client import ESClient


@pytest.fixture
def es_client(settings):
    return ESClient(settings)


@pytest.mark.asyncio
async def test_get_es_returns_client(es_client):
    with patch("elasticsearch.AsyncElasticsearch") as mock_es_cls:
        mock_instance = AsyncMock()
        mock_es_cls.return_value = mock_instance

        result = await es_client.get_es()
        assert result is mock_instance
        mock_es_cls.assert_called_once()


@pytest.mark.asyncio
async def test_get_es_reuses_connection(es_client):
    with patch("elasticsearch.AsyncElasticsearch") as mock_es_cls:
        mock_instance = AsyncMock()
        mock_es_cls.return_value = mock_instance

        c1 = await es_client.get_es()
        c2 = await es_client.get_es()
        assert c1 is c2
        mock_es_cls.assert_called_once()


@pytest.mark.asyncio
async def test_close(es_client):
    with patch("elasticsearch.AsyncElasticsearch") as mock_es_cls:
        mock_instance = AsyncMock()
        mock_es_cls.return_value = mock_instance

        await es_client.get_es()
        await es_client.close()
        mock_instance.close.assert_called_once()
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_es_client.py -v
```

- [ ] **Step 3: 实现 ESClient**

```python
# src/memory_system/clients/es_client.py
from elasticsearch import AsyncElasticsearch
from memory_system.config import Settings


class ESClient:
    def __init__(self, settings: Settings):
        self._url = settings.es_url
        self._client: AsyncElasticsearch | None = None

    async def get_es(self) -> AsyncElasticsearch:
        if self._client is None:
            self._client = AsyncElasticsearch(self._url)
        return self._client

    async def close(self):
        if self._client:
            await self._client.close()
            self._client = None
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_es_client.py -v
```
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/memory_system/clients/es_client.py tests/test_es_client.py
git commit -m "feat: add async Elasticsearch client"
```

---

### Task 6: Embedding 客户端

**Files:**
- Create: `src/memory_system/clients/embedding_client.py`
- Create: `tests/test_embedding_client.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_embedding_client.py
import pytest
from unittest.mock import AsyncMock, patch
from memory_system.clients.embedding_client import EmbeddingClient


@pytest.fixture
def emb_client(settings):
    return EmbeddingClient(settings)


@pytest.mark.asyncio
async def test_get_embedding_success(emb_client):
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [{"embedding": [0.1, 0.2, 0.3]}]
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_http

        result = await emb_client.get_embedding("hello world")
        assert len(result) == 3
        assert result == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_get_embedding_batch(emb_client):
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [
            {"embedding": [0.1, 0.2]},
            {"embedding": [0.3, 0.4]},
        ]
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_http

        result = await emb_client.get_embeddings(["text1", "text2"])
        assert len(result) == 2
        assert result[0] == [0.1, 0.2]


@pytest.mark.asyncio
async def test_get_embedding_api_error(emb_client):
    mock_response = AsyncMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_http

        with pytest.raises(RuntimeError, match="Embedding API error"):
            await emb_client.get_embedding("test")
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_embedding_client.py -v
```

- [ ] **Step 3: 实现 EmbeddingClient**

```python
# src/memory_system/clients/embedding_client.py
import httpx
from memory_system.config import Settings


class EmbeddingClient:
    def __init__(self, settings: Settings):
        self._url = settings.embedding_api_url
        self._dim = settings.embedding_dim

    async def get_embedding(self, text: str) -> list[float]:
        """Get embedding vector for a single text."""
        results = await self.get_embeddings([text])
        return results[0]

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        """Get embeddings for multiple texts in one batch."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._url,
                json={"input": texts, "model": "embedding"},
                timeout=30.0,
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Embedding API error: status={resp.status_code}, "
                    f"body={resp.text}"
                )
            data = resp.json()
            return [item["embedding"] for item in data["data"]]
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_embedding_client.py -v
```
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/memory_system/clients/embedding_client.py tests/test_embedding_client.py
git commit -m "feat: add HTTP embedding client with batch support"
```

---

### Task 7: LLM 客户端

**Files:**
- Create: `src/memory_system/clients/llm_client.py`
- Create: `tests/test_llm_client.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_llm_client.py
import pytest
from unittest.mock import AsyncMock, patch
from memory_system.clients.llm_client import LLMClient


@pytest.fixture
def llm_client(settings):
    return LLMClient(settings)


@pytest.mark.asyncio
async def test_chat_success(llm_client):
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "Hello!"}}]
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_http

        result = await llm_client.chat(
            messages=[{"role": "user", "content": "hi"}],
            system_prompt="Be helpful",
        )
        assert result == "Hello!"


@pytest.mark.asyncio
async def test_chat_json_mode(llm_client):
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": '{"key": "value"}'}}]
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_http

        result = await llm_client.chat_json(
            messages=[{"role": "user", "content": "extract"}],
            system_prompt="Return JSON",
        )
        assert result == {"key": "value"}


@pytest.mark.asyncio
async def test_chat_json_invalid_response(llm_client):
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "not valid json"}}]
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_http

        with pytest.raises(RuntimeError, match="Failed to parse LLM JSON"):
            await llm_client.chat_json(
                messages=[{"role": "user", "content": "test"}],
                system_prompt="Return JSON",
            )


@pytest.mark.asyncio
async def test_chat_api_error(llm_client):
    mock_response = AsyncMock()
    mock_response.status_code = 500
    mock_response.text = "Server Error"

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_http

        with pytest.raises(RuntimeError, match="LLM API error"):
            await llm_client.chat([{"role": "user", "content": "hi"}])
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_llm_client.py -v
```

- [ ] **Step 3: 实现 LLMClient**

```python
# src/memory_system/clients/llm_client.py
import json
import httpx
from memory_system.config import Settings


class LLMClient:
    def __init__(self, settings: Settings):
        self._url = settings.llm_api_url.rstrip("/")
        self._key = settings.llm_api_key

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

    async def chat(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> str:
        """Send chat completion request and return text response."""
        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._url}/chat/completions",
                headers=self._headers(),
                json={
                    "model": "default",
                    "messages": full_messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=60.0,
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"LLM API error: status={resp.status_code}, body={resp.text}"
                )
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def chat_json(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        max_tokens: int = 1024,
    ) -> dict | list:
        """Send chat request and parse response as JSON."""
        text = await self.chat(
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=0.0,
        )
        # Try to extract JSON from the response text
        text = text.strip()
        if text.startswith("```"):
            # Strip markdown code fences
            lines = text.split("\n")
            text = "\n".join(lines[1:-1])
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError(f"Failed to parse LLM JSON response: {text[:200]}")
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_llm_client.py -v
```
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add src/memory_system/clients/llm_client.py tests/test_llm_client.py
git commit -m "feat: add OpenAI-compatible LLM HTTP client"
```

---

### Task 8: API 模型（Pydantic 请求/响应）

**Files:**
- Create: `src/memory_system/api/models.py`
- Create: `tests/test_models.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_models.py
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
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_models.py -v
```

- [ ] **Step 3: 实现模型**

```python
# src/memory_system/api/models.py
import uuid
from pydantic import BaseModel, Field


class ContentItem(BaseModel):
    type: str  # "input_text" | "input_image"
    text: str | None = None
    image_url: str | None = None


class Message(BaseModel):
    role: str
    content: str | list[ContentItem]


class MemoryRequest(BaseModel):
    model: str = "memory-v1"
    userId: str
    sessionId: str
    reasoning: dict | None = None
    input: list[Message]


class RetrievedMemory(BaseModel):
    id: str
    memory: str
    score: float
    created_at: str
    importance: float


class MemoryResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"mem_{uuid.uuid4().hex[:12]}")
    object: str = "memory.response"
    model: str = "memory-v1"
    output_text: str = ""
    history: list[dict] = []
    retrieved_memories: list[RetrievedMemory] = []
    usage: dict = Field(default_factory=lambda: {"total_tokens": 0})
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_models.py -v
```
Expected: 9 PASS

- [ ] **Step 5: Commit**

```bash
git add src/memory_system/api/models.py tests/test_models.py
git commit -m "feat: add OpenAI-compatible Pydantic request/response models"
```

---

### Task 9: SessionManager（Redis 会话管理）

**Files:**
- Create: `src/memory_system/core/session_manager.py`
- Create: `tests/test_session_manager.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_session_manager.py
import json
import pytest
from unittest.mock import AsyncMock, patch
from memory_system.core.session_manager import SessionManager


@pytest.fixture
def redis_mock():
    return AsyncMock()


@pytest.fixture
def emb_client_mock():
    mock = AsyncMock()
    mock.get_embedding.return_value = [0.1, 0.2, 0.3]
    return mock


@pytest.fixture
def session_mgr(settings, emb_client_mock):
    return SessionManager(settings, emb_client_mock)


@pytest.mark.asyncio
async def test_get_session_empty(session_mgr, redis_mock):
    redis_mock.hgetall.return_value = {}

    result = await session_mgr.get_session(redis_mock, "u1", "s1")
    assert result == {"messages": [], "archived_rounds": []}


@pytest.mark.asyncio
async def test_get_session_existing(session_mgr, redis_mock):
    redis_mock.hgetall.return_value = {
        "messages": json.dumps([{"role": "user", "content": "hi"}]),
        "archived_rounds": json.dumps([]),
        "created_at": "2026-05-04T10:00:00Z",
    }

    result = await session_mgr.get_session(redis_mock, "u1", "s1")
    assert len(result["messages"]) == 1
    assert result["messages"][0]["role"] == "user"


@pytest.mark.asyncio
async def test_add_round_within_window(session_mgr, redis_mock):
    redis_mock.hgetall.return_value = {
        "messages": json.dumps([{"role": "user", "content": "old"}]),
        "archived_rounds": json.dumps([]),
    }

    new_msgs = [
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new answer"},
    ]

    await session_mgr.add_round(redis_mock, "u1", "s1", new_msgs)

    # Verify HSET was called
    call_args = redis_mock.hset.call_args
    assert call_args is not None
    key = call_args[0][0]
    assert "u1" in key and "s1" in key


@pytest.mark.asyncio
async def test_add_round_exceeds_window(session_mgr, redis_mock):
    # 3 messages in window (3 rounds), adding another should archive oldest
    existing = json.dumps([
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
        {"role": "assistant", "content": "a3"},
    ])
    redis_mock.hgetall.return_value = {
        "messages": existing,
        "archived_rounds": json.dumps([]),
    }

    new_msgs = [
        {"role": "user", "content": "q4"},
        {"role": "assistant", "content": "a4"},
    ]

    await session_mgr.add_round(redis_mock, "u1", "s1", new_msgs)

    # Should have archived q1,a1
    call_args = redis_mock.hset.call_args
    mapping = call_args[0][1]
    messages = json.loads(mapping["messages"])
    archived = json.loads(mapping["archived_rounds"])

    # After adding q4,a4, should still have 6 messages (3 rounds * 2)
    assert len(messages) == 6
    assert messages[0]["content"] == "q2"  # q1 was archived
    assert len(archived) == 1  # one archived round


@pytest.mark.asyncio
async def test_build_history(session_mgr, redis_mock):
    archived = [
        {
            "round_id": "r1",
            "messages": [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ],
            "embedding": [0.1, 0.2, 0.3],
        }
    ]
    messages = [
        {"role": "user", "content": "recent q"},
        {"role": "assistant", "content": "recent a"},
    ]
    redis_mock.hgetall.return_value = {
        "messages": json.dumps(messages),
        "archived_rounds": json.dumps(archived),
    }

    query_embedding = [0.1, 0.2, 0.3]  # high similarity

    result = await session_mgr.build_history(
        redis_mock, "u1", "s1", query_embedding
    )

    # archived round should be included because of high relevance
    assert "old question" in result["history_str"]
    assert "old answer" in result["history_str"]
    assert len(result["history_messages"]) == 3  # 1 archived + 2 recent


@pytest.mark.asyncio
async def test_build_history_irrelevant_archived(session_mgr, redis_mock):
    archived = [
        {
            "round_id": "r1",
            "messages": [
                {"role": "user", "content": "unrelated question"},
                {"role": "assistant", "content": "unrelated answer"},
            ],
            "embedding": [0.9, 0.8, 0.7],
        }
    ]
    messages = [{"role": "user", "content": "recent q"}]
    redis_mock.hgetall.return_value = {
        "messages": json.dumps(messages),
        "archived_rounds": json.dumps(archived),
    }

    query_embedding = [0.1, 0.2, 0.3]  # low similarity

    result = await session_mgr.build_history(
        redis_mock, "u1", "s1", query_embedding
    )

    # unrelated question should be truncated, answer omitted
    assert "unrelated question" in result["history_str"]
    assert "[previous response omitted]" in result["history_str"]
    assert "unrelated answer" not in result["history_str"]
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_session_manager.py -v
```

- [ ] **Step 3: 实现 SessionManager**

```python
# src/memory_system/core/session_manager.py
import json
import math
import time
import uuid
from datetime import datetime, timezone
from memory_system.config import Settings
from memory_system.utils.text_utils import extract_text


class SessionManager:
    def __init__(self, settings: Settings, embedding_client):
        self._settings = settings
        self._embedding = embedding_client

    def _key(self, user_id: str, session_id: str) -> str:
        return f"memory:sess:{user_id}:{session_id}"

    async def get_session(self, redis, user_id: str, session_id: str) -> dict:
        """Load session data from Redis."""
        raw = await redis.hgetall(self._key(user_id, session_id))
        if not raw:
            return {"messages": [], "archived_rounds": []}
        return {
            "messages": json.loads(raw.get("messages", "[]")),
            "archived_rounds": json.loads(raw.get("archived_rounds", "[]")),
        }

    async def add_round(
        self, redis, user_id: str, session_id: str, messages: list[dict]
    ):
        """Add a new round of messages to the session, managing window."""
        key = self._key(user_id, session_id)
        session = await self.get_session(redis, user_id, session_id)

        now = datetime.now(timezone.utc).isoformat()
        window = self._settings.session_window_size

        # Append new messages
        session["messages"].extend(messages)

        # Archive oldest round if exceeds window
        while len(session["messages"]) > window * 2:
            # Pop oldest Q+A pair
            archived_pair = session["messages"].pop(0)  # user
            archived_pair2 = session["messages"].pop(0)  # assistant

            # Compute embedding for the archived query
            query_text = extract_text(archived_pair.get("content", ""))
            emb = []
            if query_text:
                try:
                    emb = await self._embedding.get_embedding(query_text)
                except Exception:
                    pass

            archive_entry = {
                "round_id": uuid.uuid4().hex[:8],
                "messages": [archived_pair, archived_pair2],
                "embedding": emb,
            }
            session["archived_rounds"].append(archive_entry)

        # Trim archived rounds to max
        max_archived = self._settings.archived_rounds_max
        if len(session["archived_rounds"]) > max_archived:
            session["archived_rounds"] = session["archived_rounds"][-max_archived:]

        # Save back to Redis
        pipeline = redis.pipeline()
        pipeline.hset(
            key,
            mapping={
                "messages": json.dumps(session["messages"], ensure_ascii=False),
                "archived_rounds": json.dumps(
                    session["archived_rounds"], ensure_ascii=False
                ),
                "updated_at": now,
            },
        )
        pipeline.expire(key, self._settings.session_ttl_seconds)
        await pipeline.execute()

    async def build_history(
        self, redis, user_id: str, session_id: str, query_embedding: list[float]
    ) -> dict:
        """Build history: long-term memories + filtered archived rounds + recent messages."""
        session = await self.get_session(redis, user_id, session_id)
        threshold = self._settings.relevance_threshold
        history_messages = []
        history_str_parts = []

        # Process archived rounds with relevance filter
        for entry in session["archived_rounds"]:
            emb = entry.get("embedding", [])
            if emb and query_embedding:
                sim = self._cosine_similarity(query_embedding, emb)
                if sim >= threshold:
                    # Full display
                    for msg in entry["messages"]:
                        history_messages.append(msg)
                        text = extract_text(msg.get("content", ""))
                        if text:
                            history_str_parts.append(f"{msg['role']}: {text}")
                else:
                    # Truncated: keep query, replace answer
                    q_msg = entry["messages"][0]
                    q_text = extract_text(q_msg.get("content", ""))
                    if len(q_text) > 200:
                        q_text = q_text[:200] + "..."
                    history_str_parts.append(f"{q_msg['role']}: {q_text}")
                    history_str_parts.append("assistant: [previous response omitted]")
                    history_messages.append(q_msg)
                    history_messages.append(
                        {"role": "assistant", "content": "[previous response omitted]"}
                    )
            else:
                # No embedding, keep query truncated
                q_msg = entry["messages"][0]
                q_text = extract_text(q_msg.get("content", ""))
                if len(q_text) > 200:
                    q_text = q_text[:200] + "..."
                history_str_parts.append(f"{q_msg['role']}: {q_text}")
                history_str_parts.append("assistant: [previous response omitted]")

        # Add recent messages
        for msg in session["messages"]:
            history_messages.append(msg)
            text = extract_text(msg.get("content", ""))
            if text:
                history_str_parts.append(f"{msg['role']}: {text}")

        return {
            "history_messages": history_messages,
            "history_str": "\n".join(history_str_parts),
        }

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_session_manager.py -v
```
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add src/memory_system/core/session_manager.py tests/test_session_manager.py
git commit -m "feat: add Redis session manager with sliding window and relevance filter"
```

---

### Task 10: LongTermMemory（ES 长期记忆）

**Files:**
- Create: `src/memory_system/core/long_term.py`
- Create: `tests/test_long_term.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_long_term.py
import math
import pytest
from unittest.mock import AsyncMock
from memory_system.core.long_term import LongTermMemory


@pytest.fixture
def es_mock():
    mock = AsyncMock()
    return mock


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
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_long_term.py -v
```

- [ ] **Step 3: 实现 LongTermMemory**

```python
# src/memory_system/core/long_term.py
import math
import uuid
from datetime import datetime, timezone
from memory_system.config import Settings


class LongTermMemory:
    def __init__(self, settings: Settings, embedding_client):
        self._settings = settings
        self._embedding = embedding_client

    @staticmethod
    def index_name(user_id: str) -> str:
        return f"memory_long_term_{user_id}"

    async def ensure_index(self, es, user_id: str):
        """Create ES index with dense_vector mapping if not exists."""
        name = self.index_name(user_id)
        exists = await es.indices.exists(index=name)
        if exists:
            return

        body = {
            "mappings": {
                "properties": {
                    "id": {"type": "keyword"},
                    "user_id": {"type": "keyword"},
                    "session_id": {"type": "keyword"},
                    "memory": {"type": "text"},
                    "embedding": {
                        "type": "dense_vector",
                        "dims": self._settings.embedding_dim,
                        "index": True,
                        "similarity": "cosine",
                    },
                    "memory_type": {"type": "keyword"},
                    "importance": {"type": "float"},
                    "metadata": {"type": "object", "enabled": False},
                    "created_at": {"type": "date"},
                }
            }
        }
        await es.indices.create(index=name, body=body)

    async def search(
        self, es, user_id: str, query_embedding: list[float]
    ) -> list[dict]:
        """kNN search with time decay in application layer."""
        await self.ensure_index(es, user_id)
        name = self.index_name(user_id)

        body = {
            "knn": {
                "field": "embedding",
                "query_vector": query_embedding,
                "k": self._settings.mem_retrieval_top_k,
                "num_candidates": self._settings.mem_retrieval_top_k * 5,
            }
        }
        resp = await es.search(index=name, body=body, source=True)

        results = []
        now = datetime.now(timezone.utc)
        for hit in resp["hits"]["hits"]:
            src = hit["_source"]
            src["_id"] = hit["_id"]
            cosine_score = hit["_score"] or 0.0

            # Time decay
            try:
                created = datetime.fromisoformat(
                    src["created_at"].replace("Z", "+00:00")
                )
            except (ValueError, KeyError):
                created = now
            days_ago = (now - created).total_seconds() / 86400.0
            decay = math.exp(-self._settings.time_decay_lambda * days_ago)
            src["time_decayed_score"] = cosine_score * decay
            results.append(src)

        # Sort by time-decayed score and filter
        results.sort(key=lambda x: x["time_decayed_score"], reverse=True)
        return results

    async def upsert_memory(self, es, user_id: str, doc: dict):
        """Insert or update a memory document."""
        await self.ensure_index(es, user_id)
        name = self.index_name(user_id)

        memory_id = doc.get("id") or uuid.uuid4().hex
        doc["id"] = memory_id
        doc["user_id"] = user_id
        doc.setdefault("created_at", datetime.now(timezone.utc).isoformat())

        # Compute embedding for the memory text
        emb = await self._embedding.get_embedding(doc["memory"])
        doc["embedding"] = emb

        await es.index(index=name, id=memory_id, body=doc, refresh=True)

    async def delete_memory(self, es, user_id: str, memory_id: str):
        """Delete a memory by id."""
        name = self.index_name(user_id)
        try:
            await es.delete(index=name, id=memory_id)
        except Exception:
            pass  # Already deleted or doesn't exist
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_long_term.py -v
```
Expected: 8 PASS

- [ ] **Step 5: Commit**

```bash
git add src/memory_system/core/long_term.py tests/test_long_term.py
git commit -m "feat: add ES long-term memory with kNN search and time decay"
```

---

### Task 11: MemoryExtractor（LLM 记忆提取 + 冲突处理）

**Files:**
- Create: `src/memory_system/core/extractor.py`
- Create: `tests/test_extractor.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_extractor.py
import pytest
from unittest.mock import AsyncMock
from memory_system.core.extractor import MemoryExtractor


@pytest.fixture
def llm_mock():
    mock = AsyncMock()
    return mock


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
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_extractor.py -v
```

- [ ] **Step 3: 实现 MemoryExtractor**

```python
# src/memory_system/core/extractor.py
import json
from memory_system.config import Settings
from memory_system.utils.text_utils import messages_to_text


EXTRACTION_PROMPT = """你是一个记忆提取系统。分析以下对话，提取值得长期记住的用户信息。

只提取具有长期价值的信息：用户事实、偏好、重要事件、关系等。
忽略日常闲聊、临时性的问候、无信息量的内容。
如果没有值得记住的信息，返回空数组 []。

返回 JSON 数组，每条包含：
- type: "fact" | "preference" | "event"
- content: 第三人称描述的记忆事实，如"用户叫张三，今年30岁"
- importance: 0.0-1.0 的重要性评分，越高越重要

对话内容：
{messages}

仅返回 JSON 数组，不要其他内容。"""


CONFLICT_PROMPT = """你是一个记忆冲突处理系统。判断新记忆与已有记忆之间的关系，决定如何处理。

已有记忆：
{existing}

新提取的记忆：
{new_memories}

对每条新记忆，返回一个操作：
- add: 全新的信息，需要添加
  {{"action": "add", "new_memory": {{"content": "...", "importance": 0.8}}}}
- update: 新信息替代/修正旧信息（如地址变更、年龄更新）
  {{"action": "update", "old_id": "mem_001", "new_content": "...", "new_importance": 0.9}}
- skip: 重复信息，或旧信息比新信息更详细，无需操作
  {{"action": "skip", "reason": "重复"}}
- delete: 旧信息已过时且无需替换
  {{"action": "delete", "old_id": "mem_001", "reason": "已过时"}}

返回 JSON 数组，仅返回 JSON。"""


class MemoryExtractor:
    def __init__(self, settings: Settings, llm_client, embedding_client):
        self._settings = settings
        self._llm = llm_client
        self._embedding = embedding_client

    async def extract_memories(self, messages: list[dict]) -> list[dict]:
        """Extract long-term memories from conversation messages."""
        text = messages_to_text(messages)
        prompt = EXTRACTION_PROMPT.format(messages=text)

        try:
            items = await self._llm.chat_json(
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception:
            return []

        if not isinstance(items, list):
            return []

        # Filter by importance threshold
        threshold = self._settings.mem_importance_threshold
        return [
            item for item in items
            if item.get("importance", 0) >= threshold
        ]

    async def resolve_conflicts(
        self, new_memories: list[dict], existing_memories: list[dict]
    ) -> list[dict]:
        """Determine what to do with each new memory (add/update/skip/delete)."""
        if not new_memories:
            return []

        existing_str = json.dumps(
            [
                {"id": m.get("_id", ""), "content": m.get("memory", ""),
                 "importance": m.get("importance", 0)}
                for m in existing_memories
            ],
            ensure_ascii=False,
        )
        new_str = json.dumps(new_memories, ensure_ascii=False)

        prompt = CONFLICT_PROMPT.format(
            existing=existing_str, new_memories=new_str
        )

        try:
            actions = await self._llm.chat_json(
                messages=[{"role": "user", "content": prompt}],
            )
            if not isinstance(actions, list):
                return []
            return actions
        except Exception:
            # On failure, default to add all
            return [
                {"action": "add", "new_memory": m} for m in new_memories
            ]
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_extractor.py -v
```
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add src/memory_system/core/extractor.py tests/test_extractor.py
git commit -m "feat: add LLM memory extractor with conflict resolution"
```

---

### Task 12: MemoryService（核心编排）

**Files:**
- Create: `src/memory_system/core/memory_service.py`
- Create: `tests/test_memory_service.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_memory_service.py
import pytest
from unittest.mock import AsyncMock, patch
from memory_system.core.memory_service import MemoryService


@pytest.fixture
def redis_mock():
    return AsyncMock()


@pytest.fixture
def es_mock():
    return AsyncMock()


@pytest.fixture
def services(settings):
    session_mgr = AsyncMock()
    long_term = AsyncMock()
    extractor = AsyncMock()
    embedding_client = AsyncMock()
    embedding_client.get_embedding.return_value = [0.1, 0.2, 0.3]

    session_mgr.get_session.return_value = {
        "messages": [{"role": "user", "content": "hi"}],
        "archived_rounds": [],
    }
    session_mgr.build_history.return_value = {
        "history_messages": [{"role": "user", "content": "hi"}],
        "history_str": "user: hi",
    }
    long_term.search.return_value = []
    extractor.extract_memories.return_value = []

    return session_mgr, long_term, extractor, embedding_client


@pytest.mark.asyncio
async def test_process_minimal_request(settings, services):
    session_mgr, long_term, extractor, emb_client = services
    svc = MemoryService(
        settings, session_mgr, long_term, extractor, emb_client,
        redis_client=AsyncMock(), es_client=AsyncMock(),
    )
    # Mock the client get_redis/get_es methods
    mock_redis = AsyncMock()
    mock_es = AsyncMock()
    svc._redis_client.get_redis.return_value = mock_redis
    svc._es_client.get_es.return_value = mock_es

    from memory_system.api.models import MemoryRequest, Message

    req = MemoryRequest(
        userId="u1",
        sessionId="s1",
        input=[Message(role="user", content="hello")],
    )

    mock_redis.hgetall.return_value = {}
    mock_redis.hset = AsyncMock()

    resp = await svc.process(req)

    assert resp.object == "memory.response"
    assert resp.model == "memory-v1"


@pytest.mark.asyncio
async def test_process_with_retrieved_memories(settings, services):
    session_mgr, long_term, extractor, emb_client = services
    long_term.search.return_value = [
        {
            "_id": "mem_1",
            "memory": "user likes Python",
            "score": 0.95,
            "time_decayed_score": 0.95,
            "created_at": "2026-05-01T10:00:00Z",
            "importance": 0.8,
        }
    ]

    svc = MemoryService(
        settings, session_mgr, long_term, extractor, emb_client,
        redis_client=AsyncMock(), es_client=AsyncMock(),
    )
    mock_redis = AsyncMock()
    mock_es = AsyncMock()
    svc._redis_client.get_redis.return_value = mock_redis
    svc._es_client.get_es.return_value = mock_es

    mock_redis.hgetall.return_value = {
        "messages": '[]',
        "archived_rounds": '[]',
    }
    mock_redis.hset = AsyncMock()

    resp = await svc.process(req)

    assert len(resp.retrieved_memories) == 1
    assert resp.retrieved_memories[0].memory == "user likes Python"
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_memory_service.py -v
```

- [ ] **Step 3: 实现 MemoryService**

```python
# src/memory_system/core/memory_service.py
import asyncio
import logging
from datetime import datetime, timezone
from memory_system.config import Settings
from memory_system.api.models import (
    MemoryRequest,
    MemoryResponse,
    RetrievedMemory,
)
from memory_system.utils.text_utils import (
    messages_to_text,
    get_last_user_query,
)

logger = logging.getLogger(__name__)


class MemoryService:
    def __init__(
        self,
        settings: Settings,
        session_manager,
        long_term_memory,
        memory_extractor,
        embedding_client,
        redis_client=None,
        es_client=None,
    ):
        self._settings = settings
        self._session = session_manager
        self._ltm = long_term_memory
        self._extractor = memory_extractor
        self._embedding = embedding_client
        self._redis_client = redis_client
        self._es_client = es_client

    async def _get_redis(self):
        return await self._redis_client.get_redis()

    async def _get_es(self):
        return await self._es_client.get_es()

    async def process(self, request: MemoryRequest) -> MemoryResponse:
        user_id = request.userId
        session_id = request.sessionId
        raw_messages = [msg.model_dump() for msg in request.input]

        redis = await self._get_redis()
        es = await self._get_es()

        # 1. Get last user query and its embedding
        query_text = get_last_user_query(raw_messages)
        query_embedding = []
        if query_text:
            try:
                query_embedding = await self._embedding.get_embedding(query_text)
            except Exception as e:
                logger.warning(f"Failed to get query embedding: {e}")

        # 2. Parallel: search long-term memories + add round to session
        retrieved_raw = []
        if query_embedding:
            async_tasks = [
                self._ltm.search(es, user_id, query_embedding),
                self._session.add_round(redis, user_id, session_id, raw_messages),
            ]
            results = await asyncio.gather(*async_tasks, return_exceptions=True)
            if isinstance(results[0], list):
                retrieved_raw = results[0]
            else:
                logger.warning(f"LTM search failed: {results[0]}")
        else:
            await self._session.add_round(redis, user_id, session_id, raw_messages)

        # 3. Build session history
        history_data = await self._session.build_history(
            redis, user_id, session_id, query_embedding
        )

        # 4. Prepend retrieved memories to history
        retrieved_memories = []
        for mem in retrieved_raw:
            retrieved_memories.append(
                RetrievedMemory(
                    id=mem.get("_id", ""),
                    memory=mem.get("memory", ""),
                    score=mem.get("time_decayed_score", 0.0),
                    created_at=mem.get("created_at", ""),
                    importance=mem.get("importance", 0.0),
                )
            )
            # Insert retrieved memory into history
            history_data["history_messages"].insert(
                0,
                {
                    "role": "system",
                    "content": f"[Retrieved memory: {mem.get('memory', '')}]",
                },
            )

        # 5. Background: extract memories from any archived rounds
        session = await self._session.get_session(redis, user_id, session_id)
        if session["archived_rounds"]:
            asyncio.create_task(
                self._archive_pipeline(
                    es, user_id, session_id, session["archived_rounds"]
                )
            )

        # 6. Build response
        output_text = messages_to_text(history_data["history_messages"])

        return MemoryResponse(
            model=request.model,
            output_text=output_text,
            history=history_data["history_messages"],
            retrieved_memories=retrieved_memories,
            usage={"total_tokens": len(output_text.split())},
        )

    async def _archive_pipeline(
        self, es, user_id: str, session_id: str, archived_rounds: list
    ):
        """Background task: extract and store long-term memories."""
        for entry in archived_rounds:
            try:
                messages = entry["messages"]
                memories = await self._extractor.extract_memories(messages)
                if not memories:
                    continue

                # Check existing similar memories
                combined = " ".join(m.get("content", "") for m in memories)
                emb = await self._embedding.get_embedding(combined)
                existing = await self._ltm.search(es, user_id, emb)

                # Resolve conflicts
                actions = await self._extractor.resolve_conflicts(memories, existing)

                for action in actions:
                    act = action.get("action")
                    if act == "add":
                        nm = action.get("new_memory", {})
                        doc = {
                            "session_id": session_id,
                            "memory": nm.get("content", ""),
                            "memory_type": nm.get("type", "fact"),
                            "importance": nm.get("importance", 0.5),
                            "metadata": {"source_round": entry.get("round_id", "")},
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        }
                        await self._ltm.upsert_memory(es, user_id, doc)

                    elif act == "update":
                        old_id = action.get("old_id", "")
                        doc = {
                            "id": old_id,
                            "session_id": session_id,
                            "memory": action.get("new_content", ""),
                            "memory_type": "fact",
                            "importance": action.get("new_importance", 0.5),
                            "metadata": {"source_round": entry.get("round_id", "")},
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        }
                        await self._ltm.upsert_memory(es, user_id, doc)

                    elif act == "delete":
                        old_id = action.get("old_id", "")
                        await self._ltm.delete_memory(es, user_id, old_id)

                    # skip: do nothing

            except Exception as e:
                logger.error(f"Archive pipeline error for round: {e}")
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_memory_service.py -v
```
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add src/memory_system/core/memory_service.py tests/test_memory_service.py
git commit -m "feat: add memory service orchestrator with async pipeline"
```

---

### Task 13: API 路由

**Files:**
- Create: `src/memory_system/api/routes.py`
- Create: `tests/test_routes.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_routes.py
import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def client(settings):
    from memory_system.main import create_app

    app = create_app(settings)
    return TestClient(app)


def test_memory_endpoint_success(client):
    """Test the memory endpoint with a valid request."""
    # We need to mock the MemoryService.process call
    with patch(
        "memory_system.api.routes.get_memory_service"
    ) as mock_get_svc:
        mock_svc = AsyncMock()
        from memory_system.api.models import MemoryResponse

        mock_svc.process.return_value = MemoryResponse(
            model="memory-v1",
            output_text="ok",
            history=[{"role": "user", "content": "hello"}],
            retrieved_memories=[],
            usage={"total_tokens": 5},
        )
        mock_get_svc.return_value = mock_svc

        resp = client.post(
            "/v1/memory",
            json={
                "model": "memory-v1",
                "userId": "user_123",
                "sessionId": "sess_abc",
                "input": [{"role": "user", "content": "hello"}],
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "memory.response"
        assert data["model"] == "memory-v1"
        assert "history" in data


def test_memory_endpoint_missing_user_id(client):
    """Test validation error when userId is missing."""
    resp = client.post(
        "/v1/memory",
        json={
            "model": "memory-v1",
            "sessionId": "sess_abc",
            "input": [{"role": "user", "content": "hello"}],
        },
    )

    assert resp.status_code == 422


def test_memory_endpoint_multimodal(client):
    """Test endpoint with multimodal content."""
    with patch(
        "memory_system.api.routes.get_memory_service"
    ) as mock_get_svc:
        mock_svc = AsyncMock()
        from memory_system.api.models import MemoryResponse

        mock_svc.process.return_value = MemoryResponse(
            model="memory-v1",
            output_text="ok",
            history=[],
            retrieved_memories=[],
            usage={"total_tokens": 0},
        )
        mock_get_svc.return_value = mock_svc

        resp = client.post(
            "/v1/memory",
            json={
                "model": "memory-v1",
                "userId": "user_123",
                "sessionId": "sess_abc",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "what's this?"},
                            {
                                "type": "input_image",
                                "image_url": "https://example.com/img.jpg",
                            },
                        ],
                    }
                ],
            },
        )

        assert resp.status_code == 200


def test_health_endpoint(client):
    """Test health check endpoint."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_routes.py -v
```

- [ ] **Step 3: 实现路由**

```python
# src/memory_system/api/routes.py
from fastapi import APIRouter, Request
from memory_system.api.models import MemoryRequest, MemoryResponse

router = APIRouter()


# This will be set by the app factory
_memory_service = None


def set_memory_service(svc):
    global _memory_service
    _memory_service = svc


def get_memory_service():
    return _memory_service


@router.post("/v1/memory", response_model=MemoryResponse)
async def process_memory(request: MemoryRequest):
    """Process a memory request: store session + retrieve relevant memories."""
    svc = get_memory_service()
    return await svc.process(request)


@router.get("/health")
async def health():
    return {"status": "ok"}
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_routes.py -v
```
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add src/memory_system/api/routes.py tests/test_routes.py
git commit -m "feat: add memory API routes with health endpoint"
```

---

### Task 14: FastAPI 入口

**Files:**
- Create: `src/memory_system/main.py`

- [ ] **Step 1: 实现 main.py**

```python
# src/memory_system/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
from memory_system.config import Settings
from memory_system.api.routes import router, set_memory_service
from memory_system.core.memory_service import MemoryService
from memory_system.core.session_manager import SessionManager
from memory_system.core.long_term import LongTermMemory
from memory_system.core.extractor import MemoryExtractor
from memory_system.clients.redis_client import RedisClient
from memory_system.clients.es_client import ESClient
from memory_system.clients.embedding_client import EmbeddingClient
from memory_system.clients.llm_client import LLMClient


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    # Clients
    redis_client = RedisClient(settings)
    es_client = ESClient(settings)
    embedding_client = EmbeddingClient(settings)
    llm_client = LLMClient(settings)

    # Core services
    session_manager = SessionManager(settings, embedding_client)
    long_term = LongTermMemory(settings, embedding_client)
    extractor = MemoryExtractor(settings, llm_client, embedding_client)
    memory_service = MemoryService(
        settings,
        session_manager,
        long_term,
        extractor,
        embedding_client,
        redis_client=redis_client,
        es_client=es_client,
    )

    set_memory_service(memory_service)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await redis_client.close()
        await es_client.close()

    app = FastAPI(title="Memory System", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
```

- [ ] **Step 2: 验证导入成功**

```bash
cd /Users/zhaoguoqing/Project/Claude/memory
python -c "from memory_system.main import app; print('App created:', app.title)"
```
Expected: `App created: Memory System`

- [ ] **Step 3: Commit**

```bash
git add src/memory_system/main.py
git commit -m "feat: add FastAPI app factory with lifespan management"
```

---

### Task 15: 集成测试与运行验证

**Files:**
- Modify: `tests/conftest.py` (update if needed)
- Create: `tests/test_integration.py`

- [ ] **Step 1: 编写集成测试**

```python
# tests/test_integration.py
import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from memory_system.main import create_app
from memory_system.config import Settings


@pytest.fixture
def test_settings():
    return Settings(
        redis_url="redis://localhost:6379/0",
        es_url="http://localhost:9200",
        embedding_api_url="http://localhost:8080/v1/embeddings",
        embedding_dim=768,
        llm_api_url="http://localhost:8081/v1",
        llm_api_key="test-key",
        session_window_size=3,
        session_ttl_seconds=86400,
        archived_rounds_max=10,
        relevance_threshold=0.7,
        mem_importance_threshold=0.5,
        time_decay_lambda=0.01,
        mem_retrieval_top_k=5,
    )


@pytest.fixture
def app(test_settings):
    return create_app(test_settings)


@pytest.fixture
def client(app):
    return TestClient(app)


def test_full_flow_mocked(client):
    """Integration test with all external services mocked."""
    with patch("memory_system.clients.redis_client.RedisClient.get_redis") as mock_redis_get, \
         patch("memory_system.clients.es_client.ESClient.get_es") as mock_es_get, \
         patch("memory_system.clients.embedding_client.EmbeddingClient.get_embedding") as mock_emb, \
         patch("memory_system.clients.llm_client.LLMClient.chat_json") as mock_llm_json, \
         patch("memory_system.clients.llm_client.LLMClient.chat") as mock_llm_chat:

        # Mock Redis
        mock_redis = AsyncMock()
        mock_redis.hgetall.return_value = {
            "messages": '[]',
            "archived_rounds": '[]',
        }
        mock_redis.hset = AsyncMock()
        mock_redis.pipeline.return_value = AsyncMock()
        mock_redis_get.return_value = mock_redis

        # Mock ES
        mock_es = AsyncMock()
        mock_es.indices.exists.return_value = True
        mock_es.search.return_value = {"hits": {"hits": []}}
        mock_es_get.return_value = mock_es

        # Mock embedding
        mock_emb.return_value = [0.1] * 768

        # Mock LLM
        mock_llm_chat.return_value = "ok"
        mock_llm_json.return_value = []

        # Send request
        resp = client.post(
            "/v1/memory",
            json={
                "model": "memory-v1",
                "userId": "user_123",
                "sessionId": "sess_abc",
                "input": [{"role": "user", "content": "hello"}],
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "memory.response"
        assert "history" in data
        assert "retrieved_memories" in data


def test_multimodal_content_flow(client):
    """Test that multimodal content is handled correctly."""
    with patch("memory_system.clients.redis_client.RedisClient.get_redis") as mock_redis_get, \
         patch("memory_system.clients.es_client.ESClient.get_es") as mock_es_get, \
         patch("memory_system.clients.embedding_client.EmbeddingClient.get_embedding") as mock_emb, \
         patch("memory_system.clients.llm_client.LLMClient.chat_json") as mock_llm_json, \
         patch("memory_system.clients.llm_client.LLMClient.chat") as mock_llm_chat:

        mock_redis = AsyncMock()
        mock_redis.hgetall.return_value = {
            "messages": '[]',
            "archived_rounds": '[]',
        }
        mock_redis.hset = AsyncMock()
        mock_redis.pipeline.return_value = AsyncMock()
        mock_redis_get.return_value = mock_redis

        mock_es = AsyncMock()
        mock_es.indices.exists.return_value = True
        mock_es.search.return_value = {"hits": {"hits": []}}
        mock_es_get.return_value = mock_es

        mock_emb.return_value = [0.1, 0.2, 0.3]
        mock_llm_chat.return_value = "ok"
        mock_llm_json.return_value = []

        resp = client.post(
            "/v1/memory",
            json={
                "model": "memory-v1",
                "userId": "user_123",
                "sessionId": "sess_abc",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "what's in this image?"},
                            {
                                "type": "input_image",
                                "image_url": "https://example.com/img.jpg",
                            },
                        ],
                    },
                    {"role": "assistant", "content": "it's a cat"},
                ],
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "memory.response"


def test_error_response_format(client):
    """Test that error responses follow the expected format."""
    resp = client.post(
        "/v1/memory",
        json={"model": "memory-v1"},
        # missing userId and sessionId
    )

    assert resp.status_code == 422
    data = resp.json()
    assert "detail" in data
```

- [ ] **Step 2: 运行全部测试**

```bash
cd /Users/zhaoguoqing/Project/Claude/memory
pytest tests/ -v
```
Expected: all tests PASS (target: ~45 tests)

- [ ] **Step 3: 运行类型检查（如可用）**

```bash
python -c "
from memory_system.main import app
from memory_system.api.models import MemoryRequest, Message, MemoryResponse
from memory_system.config import Settings

# Verify imports work
print('All imports OK')
print('Config defaults:', Settings().session_window_size)
"
```
Expected: prints "All imports OK" and config value

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "test: add integration tests for full request flow"
```

---

## 运行说明

启动服务：
```bash
cd /Users/zhaoguoqing/Project/Claude/memory
cp .env.example .env
# 编辑 .env 填入实际的服务地址
uvicorn memory_system.main:app --host 0.0.0.0 --port 8000 --workers 4
```

测试请求：
```bash
curl -X POST http://localhost:8000/v1/memory \
  -H "Content-Type: application/json" \
  -d '{
    "model": "memory-v1",
    "userId": "user_123",
    "sessionId": "sess_abc",
    "input": [
      {"role": "user", "content": "我叫张三，今年30岁"}
    ]
  }'
```
