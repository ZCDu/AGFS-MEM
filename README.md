# short-term-memory

`short-term-memory` 是独立 HTTP 上下文服务。它保留现有 Headroom generation、
CCR、Redis 最近上下文和 Journal 原文，并把 Claude Code 的四层上下文压缩语义移植为
Python，使活动上下文可以反复执行 `AB → ABCD → ABCDE`，而不是只能压缩一次。

生产入口是两个进程：

- `short-term-memory-api`：写入、prepare、Journal Grep/Read、CCR recall。
- `short-term-memory-worker`：Headroom original-only generation 与 L4 Session Memory。

Agent 与 Journal 不需要位于同一机器或容器。Agent 只访问 HTTP 服务返回的
`journal://current-session` Grep/Read 工具。

## 压缩体系

请求发给模型前按下列顺序处理：

```text
L1 request-only stale tool-result clearing
  → L2 Claude threshold and L4→L3 dispatch
  → L4 ten-section background Session Memory
  → L3 nine-section recursive continuity summary
  → Headroom original-only generations + CCR
  → Journal Grep/Read exact-detail recall
```

### L1：Micro Compact

可选的时间触发清理。只有显式 `main...` 请求、距最后 assistant 消息达到阈值时
才运行；仅把较旧的 Read/Bash/PowerShell/Grep/Glob/WebSearch/WebFetch/Edit/Write
工具结果替换为 `[Old tool result content cleared]`。修改只存在于本次请求投影，
不会写回 Redis、Journal 或 Headroom generation，且至少保留最近一个工具结果。

### L2：Auto Compact

在 `/v1/memories/prepare` 中按 Claude 的有效窗口与 13,000 token buffer 判断。
达到阈值后先尝试 L4，L4 不可用或真实 post-compact token 仍过高时调用 L3。
同一链连续失败三次后打开断路器，成功后清零。

### L4：Session Memory

assistant turn 完成写入后，通过独立 Redis 队列后台维护 Claude 十章节 Session
Memory。更新只在模型输出校验成功且 envelope CAS 成功后推进 coverage。L2 使用已完成
revision 快速创建 compact boundary，并按完整对话轮次保留安全尾部。

### L3：Traditional Compact

使用与 Agent 相同 provider 的独立、单轮、无工具 compact 请求。提示词和
full/partial/PTL retry 语义来自 Claude Code。compact 后用
`CompactBoundary + continuity summary + messagesToKeep` 替换旧活动上下文；下一次
L3 会读取上一版摘要和新上下文，因此可递归生成 AB、ABCD、ABCDE。

### Headroom generation 与 CCR

Headroom 只接收从 Redis/Journal 选出的原文，不接收 L3/L4 摘要或旧 generation。
每次成功结果作为 opaque `CompressionGeneration` 存入 v2 envelope。被 L3/L4
boundary 覆盖的 generation 不再进入活动 prompt，但仍暂存在 Redis，marker 仍可由
CCR 召回。存储压力过高时作业明确命名为 `evict_oldest_generation`，只淘汰最旧段，
不冒充 Claude compact；旧队列 JSON 的 `recompress=true` 会惰性迁移。

### Journal 精确召回

Journal 是不可变事实源，也是 Claude transcript 的项目等价物。L3/L4 摘要告诉
Agent 完整记录位于 `journal://current-session`。当用户询问准确代码、错误、工具结果
或原句时，Agent 自动执行：

```text
continuity summary → Grep → Read → exact Journal original → answer
```

用户不需要手动召回，Agent 也无法看到服务端真实文件路径。

## 数据平面

| 平面 | 存储 | 职责 |
|---|---|---|
| 原文 | Journal JSONL | 不可变事件、Grep/Read、最终重建源 |
| 在线尾部 | Redis | 最近 N 轮/retain token budget 内的原文 |
| 细节压缩 | Redis v2 envelope + Headroom CCR | opaque generations、marker、原文召回 |
| 活动连续性 | Redis v2 envelope | L4 SessionMemoryRevision、L3/L4 ContextRevision、tracking |

`MemorySummaryEnvelope` v2 使用独立 coverage：

- `compressed_through_sequence`：Headroom generation 覆盖；
- `session_memory.covered_through_sequence`：L4 覆盖；
- `active_revision.boundary.covered_through_sequence`：当前活动摘要覆盖。

所有并发写通过 envelope version CAS；generation 写只更新 generation 字段并保留
Session Memory、active revision 和 auto-compact tracking。

## HTTP 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/v1/memories/write` | Journal 先落盘，再提交 Redis；调度 Headroom/L4 |
| POST | `/v1/memories/read` | 读取当前活动上下文与 Headroom proxy 信息 |
| POST | `/v1/memories/prepare` | L1 → L2 → L4/L3 后返回本次模型上下文 |
| POST | `/v1/memories/recall` | 按 CCR marker hash 召回原文 |
| POST | `/v1/memories/transcript/grep` | 在当前 session Journal transcript 中定位 sequence |
| POST | `/v1/memories/transcript/read` | 按 sequence 范围读取精确原文 |
| GET | `/health/live`、`/health/ready` | 存活与依赖就绪检查 |
| GET | `/metrics` | Prometheus 指标 |

生产模式需要 `Authorization: Bearer <MEMORY_API_AUTH_TOKEN>`。Transcript 接口还校验
prepare 返回的 session scope，防止跨 session 读取。

## 快速开始

```bash
uv sync --extra api --extra deepseek --extra dev
cp .env.example .env
short-term-memory-api
short-term-memory-worker
```

Agent 使用 `AgentChatClient`：

```python
from short_term_memory import AgentChatClient

client = AgentChatClient(
    memory_api_url="http://127.0.0.1:8080",
    model_call=model_call,
    auth_token="...",
    context_window_tokens=128_000,
    max_output_tokens=8_192,
)
answer = await client.turn("user-1", "session-1", "继续之前的任务")
```

`model_call` 是可注入的 async provider adapter。服务端的
`ContinuityCompactionModel` 也必须由部署层注入，并与 Agent 使用同一 LLM/provider；
它发起独立 compact 请求，不复用当前生成请求。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis 连接 |
| `REDIS_SESSION_TTL_SECONDS` | `43200` | 在线 session TTL |
| `REDIS_HISTORY_TURNS` | `10` | 最近对话轮数 |
| `REDIS_RETAIN_RATIO` | `0.25` | 原文尾部 token budget 比例 |
| `CONTEXT_WINDOW_TOKENS` | `128000` | 默认上下文窗口 |
| `HEADROOM_SERVICE_URL` | 空 | Headroom 服务；production 必填 |
| `HEADROOM_TRIGGER_RATIO` | `0.65` | generation 压缩策略阈值 |
| `HEADROOM_CCR_TTL_SECONDS` | `43200` | CCR 生命周期 |
| `CONTINUITY_COMPACTION_ENABLED` | `true` | 启用 L2/L3/L4 |
| `CONTINUITY_COMPACTION_MODEL` | `DEEPSEEK_MODEL` | 独立 compact 模型名 |
| `COMPACTION_PREPARE_TIMEOUT_SECONDS` | `300` | Agent prepare HTTP timeout |
| `TIME_BASED_MICROCOMPACT_ENABLED` | `false` | 启用时间触发 L1 |
| `TIME_BASED_MICROCOMPACT_GAP_MINUTES` | `60` | L1 空闲间隔阈值 |
| `TIME_BASED_MICROCOMPACT_KEEP_RECENT` | `5` | L1 保留最近工具结果；0 在运行时下限为 1 |

完整默认值见 `.env.example`。

## Claude source parity

| Python 模块 | Claude TypeScript 来源 | 直接翻译 | 必要项目适配 |
|---|---|---|---|
| `compression/micro_compact.py` | `services/compact/microCompact.ts`：`evaluateTimeBasedTrigger`、`maybeTimeBasedMicrocompact`、token helpers | 时间判断、compactable tools、keep floor、copy-on-write、4/3 padding | query source 使用 `main...`；只改 HTTP 请求投影；cached cache-edit API 分支不移植 |
| `compression/auto_compact.py` | `services/compact/autoCompact.ts` | effective window、13k/3k buffer、L4→L3、三失败断路器 | model profile 来自 `/prepare` 请求 |
| `compression/session_memory_prompt.py` | Session Memory prompt/template | 十章节模板和更新指令 | 文件 revision 改存 Redis envelope |
| `compression/session_memory_state.py` | Session Memory update predicate/state | 10k、5 tool calls、40k、15s/60s 常量语义 | assistant durable write 后投递 Redis 队列 |
| `compression/session_memory.py` | Session Memory extraction/update | 独立更新请求、完整校验后推进 coverage | transcript 输入来自 Journal sequence |
| `compression/session_memory_compact.py` | `trySessionMemoryCompaction` 与 tail keep helpers | L4 fast path、完整轮次保尾、post-token 复核 | UUID boundary 改为 Journal sequence |
| `compression/compact_prompt.py` | `services/compact/compact.ts` prompt/format helpers | full、partial、continuation 和九章节摘要提示 | transcript 地址翻译为 `journal://current-session`，明确 Agent 自动 Grep/Read |
| `compression/traditional_compact.py` | Traditional/partial compact、grouping、PTL retry | 单轮无工具请求、完整 API round 裁剪、最多三次重试 | provider 可注入；coverage 使用 `stm_sequence_through` |
| `compression/context_query.py` | compact activity-chain replacement | boundary + summary + visible tail，旧摘要替换不追加 | Headroom generation 按 sequence boundary 隐藏 |
| `service/context_coordinator.py` | `query.ts` pre-request ordering | L1 先于 L2、CAS 后返回 replacement context | 分布式 Redis lease/CAS 和一次 stale reload |
| `jobs/session_memory_worker.py` | Session Memory background extraction lifecycle | 串行 extraction、valid-only coverage | Redis durable queue；Journal 是完整输入 |
| `transcript/journal_transcript.py` | Claude transcript JSONL | 单一逻辑 transcript、稳定行/sequence | 跨日期 Journal 合并为虚拟 URI |
| `transcript/grep_tool.py` | Grep tool | regex、context、head limit、offset、output modes | HTTP session scope，不暴露真实路径 |
| `transcript/read_tool.py` | Read tool | offset/limit 精确范围 | offset 对应 Journal sequence |
| `agent/agent_chat.py` | Claude tool-use continuation loop | model 自主 Grep→Read 后继续采样 | 工具调用通过 memory HTTP 服务执行 |
| `jobs/compression_worker.py` | 项目原有 Headroom 机制（非 Claude compact） | 不适用 | 严格 original-only；generation/CCR 保留；eviction 与 L3/L4 分离 |

## 从旧五类摘要迁移

- 已删除同步 `build_runtime`、`SessionCompressionJob`、`RedisSessionContext` 五类摘要链。
- 应用改用 `AgentChatClient` 调用独立 HTTP 服务。
- 旧 Redis envelope 在读取边界惰性迁移到 v2；`current_goal` 等旧字段被丢弃，
  Journal 原文不受影响。
- 旧 compression queue 中的 `recompress=true` 会迁移为
  `evict_oldest_generation=true`。
- Headroom marker 的 CCR cache 与 Journal Grep/Read 仍是精确细节恢复路径。

## 验证

```bash
uv run python -m pytest -q
uv run python -m ruff check src tests
uv build
```

集成测试覆盖递归 AB→ABCD→ABCDE、迟到 generation 隐藏、Journal 原文不变，以及
Agent 自动 Grep→Read 后回答精确原文。
