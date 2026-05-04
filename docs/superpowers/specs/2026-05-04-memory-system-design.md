# 记忆存储与召回系统 — 设计文档

## 概述

一个高并发的记忆存储与召回系统，Python 实现，API 兼容 OpenAI responses.create 格式，为上游应用提供透明的「会话连续性」和「长期记忆检索」能力。

## API 设计

### 请求格式

```
POST /v1/memory
```

兼容 OpenAI responses.create 格式，额外增加 `userId` 和 `sessionId`：

```json
{
    "model": "memory-v1",
    "userId": "user_123",
    "sessionId": "sess_abc",
    "reasoning": {"effort": "low"},
    "input": [
        {"role": "user", "content": "我叫张三，今年30岁"},
        {"role": "assistant", "content": "你好张三！"}
    ]
}
```

- `userId` / `sessionId` 必填
- `input` 中 `content` 支持字符串和数组两种形式（兼容 OpenAI multimodal）
- 同一接口同时完成存储新记忆 + 召回历史 + 返回会话上下文

### 响应格式

```json
{
    "id": "mem_xxx",
    "object": "memory.response",
    "model": "memory-v1",
    "output_text": "你好张三，我记得你...",
    "history": [
        {"role": "user", "content": "我叫张三"},
        {"role": "assistant", "content": "你好张三！"}
    ],
    "retrieved_memories": [
        {
            "id": "mem_001",
            "memory": "用户叫张三",
            "score": 0.92,
            "created_at": "2026-05-04T10:00:00Z",
            "importance": 0.9
        }
    ],
    "usage": {"total_tokens": 150}
}
```

- `history`：检索到的长期记忆 + 已归档会话轮次（相关性过滤后）+ 最近 N 轮对话
- `retrieved_memories`：本次从 ES 召回的记忆条目
- 字段可扩展

## 架构

### 整体结构

```
FastAPI (uvicorn)
├── API Layer  — 路由 + Pydantic 模型
├── MemoryService — 核心编排
│   ├── SessionManager (Redis)
│   ├── LongTermMemory (ES + Embedding)
│   └── MemoryExtractor (LLM)
└── Clients
    ├── Redis (async)
    ├── Elasticsearch (async)
    ├── Embedding (HTTP)
    └── LLM (HTTP, OpenAI 兼容)
```

全链路异步，所有外部调用通过 httpx AsyncClient。

### 请求处理流程

1. 参数校验，提取 userId / sessionId / messages
2. 加载会话上下文（Redis）
3. 并行执行：长期记忆检索（ES kNN）+ 当前消息写入 Redis
4. 合并上下文 → history
5. 异步后台（不阻塞响应）：记忆提取 + 归档轮次相关性标记
6. 返回响应

## 会话记忆（Redis）

### 数据结构

```
Key: memory:sess:{userId}:{sessionId}
Type: Hash
Fields:
  messages        — JSON 数组，最近 N 轮完整对话
  archived_rounds — JSON 数组，已归档轮次 [{query, answer, embedding, round_id}]
  created_at
  updated_at
TTL: 可配置，默认 1 天
```

### 窗口管理策略

不采用 LLM 摘要，改用「相关性判断 + 截断」：

- 当前轮次超出 N 轮时，最旧的一轮弹出 → archived_rounds
- 弹出时同时计算 embedding（供后续相关性判断）+ 送入 mem0 管道（LLM 提取 → ES）
- 构建 history 时遍历 archived_rounds：
  - cosine_similarity(current_query_embedding, round_embedding) > 阈值 → 全量展示
  - 否则 → query 保留（截断），answer 替换为占位符 `[previous response omitted]`

## 长期记忆（Elasticsearch）

### 索引设计

索引名：`memory:long_term:{userId}`（按用户隔离）

```json
{
    "id": "mem_uuid",
    "user_id": "user_123",
    "session_id": "sess_abc",
    "memory": "用户叫张三，今年30岁",
    "embedding": [0.12, -0.34, ...],
    "memory_type": "fact",
    "metadata": {
        "source_round": 3,
        "confidence": 0.9
    },
    "created_at": "2026-05-04T10:00:00Z",
    "importance": 0.9
}
```

- embedding 维度可配置（环境变量）
- 索引 mapping 按实际维度创建

### 写入流程（异步，LLM 过滤）

1. 对话轮次 → LLM 提取：判断是否有值得长期记住的信息，返回结构化记忆或空数组
2. importance < 阈值的丢弃，空数组跳过
3. 有效记忆 → embedding → ES 查重（kNN）
4. LLM 判断冲突：add / update / skip / delete+add
5. 执行 ES 操作

### 检索流程（同步）

1. query → embedding 服务 → 向量
2. ES kNN 检索（含 user_id 过滤）
3. 时间衰减：score = cosine_similarity * e^(-λ * days_since_created)，λ 可配置
4. 过滤 score < 阈值的条目
5. 返回 top-k

## 项目结构

```
memory/
├── pyproject.toml
├── .env.example
├── src/
│   └── memory_system/
│       ├── __init__.py
│       ├── main.py
│       ├── config.py
│       ├── api/
│       │   ├── __init__.py
│       │   ├── routes.py
│       │   └── models.py
│       ├── core/
│       │   ├── __init__.py
│       │   ├── memory_service.py
│       │   ├── session_manager.py
│       │   ├── long_term.py
│       │   └── extractor.py
│       ├── clients/
│       │   ├── __init__.py
│       │   ├── redis_client.py
│       │   ├── es_client.py
│       │   ├── embedding_client.py
│       │   └── llm_client.py
│       └── utils/
│           ├── __init__.py
│           └── text_utils.py
└── tests/
    ├── conftest.py
    ├── test_memory_service.py
    ├── test_session_manager.py
    ├── test_long_term.py
    └── test_api.py
```

## 错误处理

- 外部服务不可用（Redis/ES/Embedding/LLM）时返回明确错误码，不静默失败
- Embedding 服务超时：返回 503，含超时时间
- ES 写入失败：记录日志，后台重试 3 次，不影响主请求
- Redis 不可用：会话上下文为空，提示调用方"无历史"，不阻塞
- 所有错误统一格式 `{"error": {"type": "...", "message": "..."}}`

## 配置项

| 变量 | 默认值 | 说明 |
|------|--------|------|
| SESSION_WINDOW_SIZE | 10 | 会话窗口 N 轮 |
| SESSION_TTL_SECONDS | 86400 | 会话 TTL，默认 1 天 |
| ARCHIVED_ROUNDS_MAX | 50 | archived_rounds 最大保留条数 |
| RELEVANCE_THRESHOLD | 0.7 | 相关轮次判断阈值 |
| MEM_IMPORTANCE_THRESHOLD | 0.5 | 记忆写入重要性阈值 |
| TIME_DECAY_LAMBDA | 0.01 | 时间衰减系数 |
| MEM_RETRIEVAL_TOP_K | 10 | 记忆检索 top-k |
| REDIS_URL | - | Redis 连接地址 |
| ES_URL | - | ES 连接地址 |
| EMBEDDING_API_URL | - | Embedding 服务地址 |
| EMBEDDING_DIM | 768 | Embedding 维度 |
| LLM_API_URL | - | LLM 服务地址 |
| LLM_API_KEY | - | LLM API Key |
