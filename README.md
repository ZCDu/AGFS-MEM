# Memory System

Memory System 为 AI Agent 提供会话上下文管理 + 长期记忆存储。核心思路：**会话历史用 Redis + bigram 相似度压缩**，**长期记忆通过 LLM 提取 → Embedding → ES 向量检索**，并按 mem0 的 ADD/UPDATE/DELETE 模式维护记忆一致性。

## 架构

```
Client
  │  POST /v1/memory/store     POST /v1/memory/recall
  ▼
MemoryService (编排层)
  ├── SessionManager           ── Redis (会话轮次, bigram 相似度)
  ├── MemoryExtractor          ── LLM HTTP (事实提取, 更新决策)
  ├── EmbeddingClient          ── DashScope HTTP (文本 → 向量)
  ├── ESHttpClient             ── ES HTTP REST (向量检索, CRUD)
  └── LocalStorage             ── 本地文件 (附件 + 日志)
```

## 依赖

- **Redis** — 会话存储 (轮次列表, key `memory:sess:{user_id}:{session_id}`)
- **Elasticsearch 8.x** — 长期记忆向量存储 (dense_vector, cosine 相似度)
- **LLM API** (OpenAI-compatible) — 事实提取 + 记忆更新决策
- **Embedding API** — 文本向量化 (1024 维)
- 无需 `mem0ai`/`elasticsearch-py` 等第三方重量依赖，全部走 HTTP

## API

### `POST /v1/memory/store`

保存一轮对话到会话，后台异步执行记忆提取和存储。

```json
{
  "model": "memory-v1",
  "userId": "user_123",
  "sessionId": "sess_abc",
  "memory_settings": {
    "recent_rounds_full": 3
  },
  "input": [
    {"role": "user", "content": "我叫张三，在北京工作"},
    {"role": "assistant", "content": "你好张三！"}
  ]
}
```

`memory_settings` 为可选，支持按请求动态调整行为：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `recent_rounds_full` | int | `3` (来自配置) | 最近多少轮完整展示，不进行 bigram 压缩 |


### `POST /v1/memory/recall`

检索当前查询相关的会话历史 + 长期记忆，返回拼接好的 history 列表。

```json
{
  "model": "memory-v1",
  "userId": "user_123",
  "sessionId": "sess_abc",
  "input": [
    {"role": "user", "content": "我叫什么名字？"}
  ]
}
```

响应包含 `history`（可直接喂给 LLM 的消息列表）和 `retrieved_memories`（检索到的长期记忆明细）。

### `GET /health`

健康检查。

## 记忆增加流程 (ADD/UPDATE/DELETE)

`store()` 调用后会在后台异步执行 `_extract_and_store()`，流程如下：

```
┌─────────────────────────────────────────────────────────────────┐
│                    新消息 (raw_messages)                        │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                    ┌──────▼───────┐
                    │  LLM 提取事实  │  ← mem0 USER_MEMORY_EXTRACTION_PROMPT
                    │  (只从 user   │    提取用户相关的偏好、计划、个人信息等
                    │  消息中提取)   │
                    └──────┬───────┘
                           │ facts: ["姓名是张三", "在北京工作"]
                           │
              ┌────────────▼────────────┐
              │  对每个 fact:           │
              │  1. Embedding          │
              │  2. ES KNN 搜索相似记忆  │
              └────────────┬───────────┘
                           │
                    找到相似记忆？
                    ╱          ╲
                  否             是
                  │             │
          ┌───────▼──────┐  ┌──▼──────────────┐
          │ 直接 ADD      │  │ LLM 决策         │
          │ (跳过 LLM)    │  │ (UPDATE_PROMPT)  │
          │               │  │                  │
          │ 为每个 fact:   │  │ 对每条旧记忆 +    │
          │ embedding     │  │ 新事实，决定:     │
          │ → ES index    │  │ • ADD → 新文档    │
          │ doc_id=md5    │  │ • UPDATE → 同 ID  │
          └───────────────┘  │   更新 vector+text│
                             │ • DELETE → 删除   │
                             │ • NONE → 跳过     │
                             └──────────────────┘
```

### 为什么不直接用 mem0 库？

mem0 的提示词（提取 + 更新决策）是公开的，核心逻辑就是 LLM 提取 + 向量搜索 + ADD/UPDATE/DELETE。直接内联提示词、用 HTTP 调用自己的 embedding/LLM/ES 服务，避免依赖一个功能重叠的 Python 库，同时保留对所有调用的完全控制。

### 相似记忆搜索的去重

同一个旧记忆可能被多个 fact 的 KNN 搜索命中（例如 fact "喜欢编程" 和 "是工程师" 都搜到了相同旧记忆），通过 `old_memories_map` (key=doc_id) 去重后再传给 LLM 做决策。

### 容错策略

- LLM 提取失败 → 返回空列表，不影响 store 响应
- Embedding 失败 → 跳过该 fact
- ES 搜索失败 → 跳过，视为无相似记忆
- LLM 更新决策 JSON 解析失败 → 降级为直接 ADD 所有 fact

## 会话历史构建

`/recall` 调用 `build_history()` 构建会话上下文：

1. 从 Redis 读取该 session 的所有 rounds
2. **最后一个 round**（最新）：始终完整保留
3. **历史 rounds**：用**bigram 重叠系数**与当前查询做相似度计算
   - `overlap = |bigrams(query) ∩ bigrams(round_first_q)| / min(|bigrams(query)|, |bigrams(round_first_q)|)`
   - `>= relevance_threshold` → 相关，完整展示全轮
   - `< relevance_threshold` → 不相关，仅保留首条用户问题 + `[previous response omitted]` 占位
4. 不使用 embedding —— bigram 比较速度远快于向量化，且中文/英文均适用

### 为什么用重叠系数而不是标准 Jaccard？

标准 Jaccard `|A ∩ B| / |A ∪ B|` 对短 query vs 长历史文本不公平（分母受长文本主导）。重叠系数 `|A ∩ B| / min(|A|, |B|)` 以较短者归一化，短查询匹配长文本时得到更合理的分数。

## 核心模块

| 模块 | 文件 | 职责 |
|------|------|------|
| `MemoryService` | `core/memory_service.py` | 编排 store/recall 流程 |
| `SessionManager` | `core/session_manager.py` | Redis 会话读写 + bigram 相似度筛选 |
| `MemoryExtractor` | `core/extractor.py` | LLM 事实提取 + ADD/UPDATE/DELETE 决策 |
| `LLMClient` | `clients/llm_client.py` | OpenAI-compatible HTTP 客户端 |
| `EmbeddingClient` | `clients/embedding_client.py` | DashScope embedding HTTP 客户端 |
| `ESHttpClient` | `clients/es_http_client.py` | ES REST 操作 (无 elasticsearch-py) |
| `RedisClient` | `clients/redis_client.py` | Redis 连接管理 |
| `LocalStorage` | `storage/local_storage.py` | 文件 + 日志本地持久化 |
| `prompts.py` | `prompts.py` | mem0 提示词 (内联，无需 mem0ai) |

## 配置

`.env` 文件：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `REDIS_URL` | Redis 连接 | `redis://localhost:6379/0` |
| `LLM_API_URL` | LLM API 地址 | — |
| `LLM_API_KEY` | LLM API 密钥 | — |
| `LLM_MODEL` | LLM 模型名 | `qwen-plus` |
| `EMBEDDING_API_URL` | Embedding API 地址 | — |
| `EMBEDDING_API_KEY` | Embedding API 密钥 | — |
| `EMBEDDING_DIM` | 向量维度 | `1024` |
| `ES_HOST` | ES 地址 | `localhost` |
| `ES_PORT` | ES 端口 | `9200` |
| `ES_USER` | ES 用户名 | — |
| `ES_PASSWORD` | ES 密码 | — |
| `ES_USE_SSL` | 启用 HTTPS | `false` |
| `ES_VERIFY_CERTS` | 验证 TLS 证书 | `false` |
| `ES_INDEX_NAME` | ES 索引名 | `mem0` |
| `RELEVANCE_THRESHOLD` | bigram 相似度阈值 | `0.35` |
| `MEM_RETRIEVAL_TOP_K` | ES 检索数量 | `10` |
| `SESSION_TTL_SECONDS` | 会话过期时间 | `86400` |
| `ARCHIVED_ROUNDS_MAX` | 最大保留轮次 | `50` |
| `STORAGE_BASE_PATH` | 本地存储路径 | `~/memory_system_data` |

## 开发

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

### ES 本地环境

```bash
# 如果需要认证:
export ES_USER=elastic
export ES_PASSWORD=elastic
```
