# short-term-memory

面向大模型应用的独立短期记忆 HTTP 服务。它保存精确原文、异步调用官方 Headroom 做压缩，并把 Headroom Proxy 上下文交给独立的 DeepSeek 聊天调用方。

## 关键边界

| 能力 | 负责方 |
|---|---|
| 在线原文、压缩 envelope、任务队列 | Redis |
| 精确原文与恢复 | Journal JSONL |
| 压缩、CCR 缓存/marker、原文召回 | 官方 Headroom 服务 |
| 最终回答 | DeepSeek 官方 OpenAI-compatible API |

memory-api 不读取 Headroom 的 LRU/SQLite，不解析 CCR marker，也不调用 DeepSeek。worker 每次只把 Redis/Journal 的精确原文发给 Headroom，绝不会把既有压缩结果再次压缩。

```text
调用方 ── write/read ──> memory-api ──> Redis + Journal
worker ── originals only ─────────────> Headroom /v1/compress
调用方 ── OpenAI SDK + read结果 ─────> Headroom Proxy ──> DeepSeek
```

## 两个业务接口

所有业务请求使用 `Authorization: Bearer <MEMORY_API_AUTH_TOKEN>`。

### 存记忆

```http
POST /v1/memories/write
Content-Type: application/json

{
  "user_id": "u-001",
  "session_id": "s-001",
  "session_seconds": 30,
  "events": [{
    "event_id": "evt-001",
    "role": "user",
    "content_type": "conversation",
    "content": "继续刚才的问题",
    "metadata": {}
  }]
}
```

`event_id` 是幂等键。原文先落 Journal，再提交 Redis；达到 token 比例、消息数或 session 时长任一阈值时异步排队，不阻塞 write。

### 读记忆

```http
POST /v1/memories/read
Content-Type: application/json

{
  "user_id": "u-001",
  "session_id": "s-001",
  "history_turns": 10,
  "include_effective_config": true
}
```

响应包括：

- `messages`：语义摘要、有效 Headroom generation、最近精确原文；
- `headroom.proxy_url` 与 `headroom.scope_headers`：独立模型 SDK 的 Proxy 参数；
- `memory`：覆盖序号、最新序号、来源和 generation 数；
- `timing_ms`：各阶段耗时；
- 可选的非敏感 `effective_config`。

另有运维端点 `/health`、`/ready`、`/metrics`，不属于业务接口。

## 快速安装

要求 Python 3.11–3.13、uv、Redis 和可访问的 Headroom HTTP 服务。

```bash
uv sync --extra api --extra deepseek --extra dev
cp .env.example .env
```

本地只启动 Redis：

```bash
docker compose -f compose.redis.yml up -d
```

分别启动 HTTP API 和后台 worker：

```bash
uv run short-term-memory-api
uv run short-term-memory-worker
```

生产部署使用 `compose.memory.yml`，由部署方提供已构建的 `MEMORY_SERVICE_IMAGE` 和官方/已审核的 `HEADROOM_IMAGE`：

```bash
export MEMORY_SERVICE_IMAGE=your-registry/short-term-memory:0.1.0
export HEADROOM_IMAGE=your-registry/headroom:0.33.0
export MEMORY_API_AUTH_TOKEN='replace-me'
export SHORT_TERM_MEMORY_SCOPE_SECRET='replace-me-too'
docker compose -f compose.memory.yml up -d
```

该拓扑默认 4 个 API 进程、每进程入口容量至少 100、8 个后台压缩 loop；Headroom HTTP Proxy 限制为 200。Redis 开启 AOF `everysec` 和命名卷，memory-api 与 worker 共享 Journal 卷。

## 独立调用 DeepSeek

安装 `deepseek` extra 后运行示例：

```bash
export DEEPSEEK_API_KEY='your-deepseek-key'
export MEMORY_API_AUTH_TOKEN='your-memory-token'
uv run python examples/deepseek_chat.py \
  --user-id u-001 \
  --session-id s-001 \
  --prompt '总结我们刚才的设计'
```

示例顺序是：写 user → 读 memory → 使用官方 `OpenAI` SDK 通过 Headroom Proxy 调 DeepSeek → 写 assistant。默认模型为 `deepseek-v4-flash`。`DEEPSEEK_API_KEY` 不会发送到 memory write/read，也不会打印。

## 存储与时间

| 内容 | 位置 | 默认时间 |
|---|---|---:|
| 精确原文（在线副本） | Redis | 12 小时 |
| 精确原文（恢复/审计） | Journal JSONL | 30 天 |
| 压缩 generation envelope | Redis | Redis TTL；generation CCR 有效期 12 小时 |
| CCR 原文与 marker | Headroom 自己的 backend | 由 Headroom 配置，需与 12 小时设置对齐 |

本项目不宣称 Headroom backend 必定是纯内存 LRU 或 SQLite；这是 Headroom 版本和部署选择。项目对 `~/.headroom/ccr_store.db` 没有任何读写。

## 核心配置

| 环境变量 | 默认值 | 是否敏感 |
|---|---:|---:|
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | 视部署而定 |
| `REDIS_SESSION_TTL_SECONDS` | `43200` | 否 |
| `REDIS_HISTORY_TURNS` | `10` | 否 |
| `JOURNAL_RETENTION_DAYS` | `30` | 否 |
| `HEADROOM_SERVICE_URL` | production 必填 | 否 |
| `HEADROOM_CCR_TTL_SECONDS` | `43200` | 否 |
| `HEADROOM_TRIGGER_RATIO` | `0.65`，限制 0.60–0.70 | 否 |
| `HEADROOM_MAX_MESSAGES` | `100` | 否 |
| `HEADROOM_MAX_SESSION_SECONDS` | `14400` | 否 |
| `MEMORY_API_WORKERS` | `4` | 否 |
| `MEMORY_API_CONCURRENCY_LIMIT` | `100` | 否 |
| `MEMORY_API_AUTH_TOKEN` | production 必填 | 是 |
| `SHORT_TERM_MEMORY_SCOPE_SECRET` | production 必填 | 是 |
| `DEEPSEEK_API_KEY` | 聊天调用方提供 | 是 |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | 否 |

完整对齐矩阵见 [Memory API 对齐说明](docs/memory-api-alignment.md)。

## 测试

```bash
uv run pytest -q
uv run ruff check src tests examples scripts
uv run python -m build
```

案例覆盖对话、代码、文档和有效 `SKILL.md`。真实 Redis、Headroom、DeepSeek 和 100 并发验收必须显式打开，未运行时只会显示 `SKIPPED`，不会冒充通过。

更多资料：

- [设计与故障边界](docs/short-term-memory.md)
- [Headroom 集成边界](docs/third_party/headroom.md)
- [性能与负载测试](docs/performance.md)
- [已知限制和未执行验收](docs/known-limitations-and-skipped-tests.md)
