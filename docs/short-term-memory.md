# short-term-memory 设计与集成边界

## 范围

该组件实现 Redis Session Context、journals 事件日志、Headroom 后台压缩、五类短期摘要，
以及公司 Agent 的回答前/回答后 Python SDK 边界。它不包含中长期记忆模块。

## 模块

```text
src/short_term_memory/
  api/
    conversation_handler.py
    runtime.py
  storage/
    redis_runtime.py
    redis_session_context.py
    journal_store.py
    vfs_adapter.py
  compression/
    headroom_client.py
    policy.py
    scope.py
    summary.py
    telemetry.py
  jobs/
    session_compression_job.py
  config.py
  models.py
  ports.py
```

## 在线读取与写入

`prepare_turn()`：

1. 根据 `user_id + session_id` 检查 Redis。
2. Redis 过期时，从同一 session 的 journals 恢复最近 N 轮。
3. 组装 `summary + 有效压缩 messages + 最近 N 轮`。
4. 把本次用户消息写 Redis 和 journals。
5. 返回 `PreparedTurn.history`、Headroom Proxy URL 和 HMAC 去标识化 headers。

`complete_turn()`：

1. 把助手回答写 Redis 和 journals。
2. 对完整 Redis 短期视图估算 token。
3. token 比例、消息数、session 时长任一达标时投递后台任务。
4. 在线调用立即返回，不等待 Headroom 或 SummaryModel。

## 后台压缩

```text
Redis compression snapshot
        ↓
Headroom /v1/compress
        ↓
injected SummaryModel
        ↓
session:{id}:summary
        ↓
成功后保留最近 N 轮 Redis 原文
```

Headroom 输出保持 message boundary。组件不指定 Kompress、SmartCrusher 或具体 Router，
也不实现私有 CCR 缓存/检索协议。官方 Headroom 负责压缩和 CCR；组件负责触发、失败门控、
摘要生成、Redis 写入和 journals 恢复。

五类语义字段为：

```text
current_goal
preferences
confirmed_facts
pending_items
attachment_references
```

summary 只属于当前 session 的 Redis 短期缓存，不写 Wiki。完整原文始终保留在 journals。

## 失败边界

- development：Headroom 不可用、超时或非法响应时允许原始 messages fallback，并记录不含
  对话正文的 warning/telemetry。
- production：Headroom 失败时不调用 SummaryModel、不写 summary、不执行 Redis LTRIM，
  保留原始消息和 journals，并交给注入的异步 RetryQueue。
- SummaryModel 输出必须通过五类结构和附件引用校验；失败不写 Redis。

## 历史 session 恢复

Redis session 仍存在时只读 Redis。Redis messages/summary 均过期时：

1. 优先读取可选的持久化 summary snapshot；
2. 恢复 journals 最近 N 轮到 Redis；
3. 如果还有更早内容且 snapshot 不存在或已失效，异步投递压缩重建；
4. 不在在线链路读取全量 journals。

## Headroom CCR

后台 `/v1/compress` 和实时 Agent 模型请求使用同一组 `dream-v1` HMAC scope headers。
`PreparedTurn` 将 headers 和 OpenAI-compatible Proxy URL 交给公司 Agent。组件不自行判断
“哪些原文相关”，不调用自制召回协议；如果当前 Headroom/供应商路径支持透明 CCR，官方
Proxy 会负责 marker、相关性判断、`headroom_retrieve` 和模型续跑。

CCR 缓存受 `HEADROOM_CCR_TTL_SECONDS` 限制，不能替代 journals 的完整、可审计原文。

## Python SDK

```python
from short_term_memory import build_runtime

runtime = build_runtime(
    home=home,
    settings=settings,
    redis_client=redis_client,
    token_estimator=token_estimator,
    summary_model=summary_model,
    executor=executor,
    retry_queue=retry_queue,
)

prepared = runtime.prepare_turn(
    "user-001",
    "session-001",
    "本轮问题",
)

# 公司 Agent 使用 prepared.history 生成回答。

result = runtime.complete_turn(
    prepared,
    assistant_content="公司 Agent 的回答",
)
```

该 SDK 不创建 Agent、聊天路由或回答模型。
