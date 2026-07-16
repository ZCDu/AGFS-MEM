# 会话增量导出 API 对接文档

## 1. 对接目标

请提供一个只读 HTTP API，用于按顺序增量导出智能体已经完成的会话轮次。

消费方会定期调用该接口，读取所有已授权用户的新会话，并从中生成：

- AI 决策卡：从 AI 的历史处理过程和最终回答中提炼可复用的决策经验；
- `USER.md`：从同一用户的长期交互中持续提炼身份、偏好、习惯和协作方式。

接口只需提供完整的原始会话轮次及必要的标识信息，不需要提供 Redis、Mirage、
Elasticsearch、Embedding、实体图谱或已经提炼的长期记忆。

## 2. 需要提供的接口

```http
GET /v1/memory/dream-export?after=<cursor>&limit=100
Accept: application/x-ndjson
Authorization: Bearer <api-token>
```

建议的完整地址示例：

```text
https://memory.example.com/v1/memory/dream-export
```

### 查询参数

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `after` | 是 | 上一次成功处理的游标。首次请求传空字符串，即 `after=`。服务端必须把空字符串视为“从当前仍可读取的最早记录开始”。 |
| `limit` | 是 | 本次最多返回的记录数。接口必须支持 `limit=100`。 |

`cursor` 是由服务端生成和解释的不透明字符串。消费方不会解析、计算或比较游标，
只会把最后一条成功处理记录的 `cursor` 原样放入下一次请求的 `after`。

### 请求示例

首次读取：

```bash
curl --get 'https://memory.example.com/v1/memory/dream-export' \
  --data-urlencode 'after=' \
  --data-urlencode 'limit=100' \
  --header 'Accept: application/x-ndjson' \
  --header 'Authorization: Bearer <api-token>'
```

继续读取：

```bash
curl --get 'https://memory.example.com/v1/memory/dream-export' \
  --data-urlencode 'after=opaque-cursor-000102' \
  --data-urlencode 'limit=100' \
  --header 'Accept: application/x-ndjson' \
  --header 'Authorization: Bearer <api-token>'
```

## 3. 响应格式

成功响应：

```http
HTTP/1.1 200 OK
Content-Type: application/x-ndjson; charset=utf-8
```

响应体采用 NDJSON：一行是一条完整 JSON 记录，记录之间使用换行符分隔；
不能用一个 JSON 数组包裹所有记录。

```jsonl
{"cursor":"opaque-cursor-000101","event_id":"evt_7f98b81a","user_id":"user_001","session_id":"session_1001","round_id":"round_07","completed_at":"2026-07-16T09:20:00Z","messages":[{"role":"user","content":"以后先给我结论，再解释原因。"},{"role":"assistant","content":"好的，之后我会先给结论。"}],"final_response":"好的，之后我会先给结论。"}
{"cursor":"opaque-cursor-000102","event_id":"evt_b17c4f60","user_id":"user_002","session_id":"session_2003","round_id":"round_11","completed_at":"2026-07-16T09:21:30Z","messages":[{"role":"user","content":"删除数据前先让我确认。"},{"role":"assistant","content":"明白，遇到删除操作时我会先请求确认。"}],"final_response":"明白，遇到删除操作时我会先请求确认。"}
```

没有新增记录时仍返回 HTTP 200，响应体为空。

## 4. 字段定义

| 字段 | 类型 | 必填 | 要求 |
| --- | --- | --- | --- |
| `cursor` | string | 是 | 非空、不透明的读取位置；必须能唯一确定这条记录在全局导出序列中的位置。 |
| `event_id` | string | 是 | 非空、稳定且全局唯一。同一条导出记录被重复读取时必须保持不变。 |
| `user_id` | string | 是 | 非空、稳定的用户标识，是区分不同用户人物画像的关键字段。 |
| `session_id` | string | 是 | 非空、稳定的会话标识。 |
| `round_id` | string | 是 | 非空、稳定的会话轮次标识。 |
| `completed_at` | string | 是 | 带时区的 ISO 8601 时间，例如 `2026-07-16T09:20:00Z` 或 `2026-07-16T17:20:00+08:00`。 |
| `messages` | array | 是 | 该轮完整消息，至少包含一条 `user` 消息和最终的 `assistant` 消息。 |
| `final_response` | string | 是 | 非空；该轮最终的 AI 回答，通常等于最后一条 `assistant` 消息的文本。 |

### `messages` 子字段

```json
{
  "role": "user",
  "content": "消息正文"
}
```

| 字段 | 类型 | 必填 | 要求 |
| --- | --- | --- | --- |
| `role` | string | 是 | 只能是 `user`、`assistant`、`system` 或 `tool`。 |
| `content` | string | 是 | 非空文本。若内部保存的是结构化内容数组，请在接口出口按原顺序转换为字符串。 |

不得导出尚未完成的流式片段、只有用户输入而没有最终 AI 回答的中间状态，或经过检索、
摘要后无法还原该轮完整交互的内容。

## 5. 游标和分页语义

接口需要满足以下规则：

1. 所有授权用户的已完成轮次进入同一个有确定顺序的导出序列。
2. 返回 `after` 指定位置之后的记录，不包含 `after` 对应的记录。
3. 每次最多返回 `limit` 条，并按从旧到新的顺序排列。
4. 响应中的最后一条记录，是消费方下一次请求使用的游标位置。
5. 相同的 `after` 和 `limit` 在没有数据清理的情况下，应返回相同顺序的记录。
6. 新记录只能追加，不能因为原 Session 被压缩、截断或过期而改变已有导出记录。
7. 消费方只有在一条记录已经持久化或确认重复后，才会保存该记录的 `cursor`。

如果一次产生的记录超过 `limit`，剩余记录由后续请求继续读取，不需要在单次响应中全部返回。

## 6. 记录生成时机

线上 `/store` 会在 AI 最终回答完成后，一次性收到完整的 user/assistant 消息。
因此，请在 `/store` 成功保存完整轮次时生成一条导出记录：

1. 使用 `/store` 收到的完整消息作为 `messages`；
2. 使用最后一条 `assistant` 消息生成 `final_response`；
3. 使用现有会话管理生成的轮次 ID 作为 `round_id`；
4. 记录服务端完成时间 `completed_at`；
5. 为这条记录生成稳定的 `event_id` 和新的 `cursor`；
6. 将记录追加到持久化导出账本。

不需要增加单独的“会话完成通知”接口，也不需要修改现有记忆提取、Embedding、
检索或 ADD/UPDATE/DELETE 逻辑。

会话保存与导出记录之间必须有可靠交付保证：可以在同一次持久化处理中写入两者，
也可以先写入可重试的 outbox，再由后台任务追加导出账本。不能在 `/store` 已经向调用方
报告成功后，无记录、无重试地丢弃该轮导出事件。

推荐在项目内部抽象一个与存储实现无关的导出账本：

```python
class DreamExportStore:
    def append(self, completed_round) -> str:
        """持久化一条完成轮次，返回新 cursor。"""

    def read_after(self, cursor: str, limit: int):
        """按顺序返回 cursor 之后的记录。"""
```

具体实现可以使用 Redis、Mirage 兼容的追加日志或其他持久化方式。HTTP 契约不依赖内部实现。

## 7. 数据保留要求

导出账本不能直接依赖会过期或会截断的短期 Session 列表，否则消费方暂停同步后可能永久漏掉记录。

- 已导出的事件在保留期内不得因 Session TTL、最近轮数上限或上下文压缩而消失；
- 保留时间必须长于双方约定的最大同步中断时间；
- 推荐至少保留 30 天，或者长期保留并通过独立归档任务清理；
- 如果 `after` 已超过保留期、服务端无法继续读取，应返回 HTTP 410，不能静默从最新位置继续。

## 8. 认证和错误响应

接口使用 Bearer Token：

```http
Authorization: Bearer <api-token>
```

建议错误状态：

| HTTP 状态 | 场景 |
| --- | --- |
| `400 Bad Request` | `after` 格式无法识别。 |
| `401 Unauthorized` | 未提供 Token 或 Token 无效。 |
| `403 Forbidden` | Token 有效，但没有导出权限。 |
| `410 Gone` | 游标对应的数据已经超过保留期，无法继续增量读取。 |
| `422 Unprocessable Entity` | `limit` 非法或超过服务端允许范围。 |
| `500 Internal Server Error` | 服务端内部错误。 |
| `503 Service Unavailable` | 导出存储暂时不可用。 |

错误响应可以使用普通 JSON，不要求使用 NDJSON：

```json
{
  "error": "cursor_expired",
  "message": "The requested cursor is no longer available."
}
```

错误信息中不得返回 Token、数据库连接信息或用户会话正文。

## 9. 验收标准

接口交付前，请确认以下场景均能通过：

1. `after=` 能读取当前仍保留的最早记录。
2. 一次写入两个不同用户的完整轮次后，接口能按写入顺序返回两条记录。
3. 返回内容是合法 NDJSON，而不是 JSON 数组。
4. `messages` 包含完整 user/assistant 对话，`final_response` 是最终 AI 回答。
5. 使用最后一条 `cursor` 再次请求时，不会重复返回该条记录。
6. 使用相同的旧游标重复请求时，`event_id`、内容和顺序保持不变。
7. 新写入一轮后，使用原最后游标只能读到新增记录。
8. Session 被压缩、截断或过期后，保留期内的导出记录仍可读取。
9. 空结果返回 HTTP 200 和空响应体。
10. 无效 Token 无法读取任何数据。

## 10. 对方最终需要提供的信息

完成接口后，请提供：

```text
接口 URL：
Bearer Token：
测试环境或联调地址：
导出记录保留时间：
单次 limit 最大值：
首次同步从最早记录还是指定时间开始：
```

消费方只需要以上信息，不需要数据库、Redis、Mirage、Elasticsearch 或 Embedding 的访问权限。
