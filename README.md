# Memory Graph Backend（记忆图谱后端）

一个 FastAPI 服务，将每个用户的知识图谱以 OKF v0.2 规范的 Markdown 文件存储在 S3（或本地磁盘）中。无需数据库。每条实体是一个带 YAML front-matter 的 `.md` 文件。包含带文件上传功能的聊天界面。

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env    # 填入 DEEPSEEK_API_KEY 和存储配置
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

服务运行在 `http://localhost:8000`。交互式 API 文档在 `http://localhost:8000/docs`。

**聊天界面** `http://localhost:8000/chat` — 支持拖拽上传文件、粘贴和纸夹按钮。文件会被提取文本供 LLM 处理（.docx、.xlsx、.pdf、.txt）。

**默认管理员账号** 首次启动时如果没有任何用户会自动创建：
- 用户名：`admin`
- 密码：`admin123456`
- 请立即通过 `POST /v1/auth/password` 或聊天界面的登录功能修改密码。

## 配置

所有配置都在 `.env` 文件中。复制 `.env.example` 并填入实际值。

### 必填

| 变量 | 说明 |
|---|---|
| `STORAGE_BACKEND` | `mirage`（S3）、`disk`（本地）、`s3`（原生 boto3） |
| `MIRAGE_S3_BUCKET` | `STORAGE_BACKEND=mirage` 时必填 |
| `MIRAGE_S3_ACCESS_KEY_ID` | S3 凭证 |
| `MIRAGE_S3_SECRET_ACCESS_KEY` | S3 凭证 |
| `AUTH_TOKENS` | 至少一对 `token:user_id`，如 `abc123:demo` |
| `DEEPSEEK_API_KEY` | LLM 提取和聊天功能所需 |

### 可选

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AUTH_MODE` | `token` | `token` 或 `off`（仅开发环境） |
| `AUTH_SECRET` | — | 密码登录必填。用 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成 |
| `AUTH_SESSION_HOURS` | `12` | 会话令牌有效期（小时） |
| `MIRAGE_S3_REGION` | — | S3 区域 |
| `MIRAGE_S3_ENDPOINT_URL` | — | 非 AWS 的 S3 兼容网关地址 |
| `MIRAGE_S3_KEY_PREFIX` | `memory_backend/` | 存储桶内的键前缀 |
| `LLM_MODEL` | `deepseek-chat` | 聊天/提取使用的模型 |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容的 API 端点 |
| `FLUSH_INTERVAL_SECONDS` | `2.0` | 写缓冲刷新间隔 |
| `FLUSH_MAX_PENDING` | `100` | 累积此数量后提前刷新 |

## 认证

API 需要 Bearer 令牌。两种凭证类型，都以 `Authorization: Bearer <token>` 发送：

### 静态令牌（服务、脚本）

在 `.env` 中设置 `AUTH_TOKENS=token:user_id`。多个令牌：`tok1:user1,tok2:user2`。

### 密码登录（人员）

在 `.env` 中设置 `AUTH_SECRET`，然后创建账号：

```powershell
python scripts\manage_users.py add alice
python scripts\manage_users.py add ops --admin
python scripts\manage_users.py list
```

通过 `POST /v1/auth/login` 或聊天界面的**登录**按钮登录。

首次启动时会自动创建默认 `admin` 账号。

## 存储结构

```
{user_id}/wiki/{type}/{slug}.md          实体文件（OKF v0.2 markdown）
{user_id}/wiki/_manifest/snapshot.json    实体索引 + 增量
{user_id}/wiki/_ops/{date}/*.jsonl        审计日志
{user_id}/raw/{session_id}/{file_id}/     按会话组织的上传文件
{user_id}/sessions/{YYYY}/{MM}/{day}/     对话记录
_auth/users.json                          用户账号（scrypt 哈希）
```

实体文件是唯一的数据源。清单、操作日志和会话文件都是派生的，可以重建。

## OKF v0.2 实体

每条实体是一个单独的 `.md` 文件：

```yaml
---
type: person
title: 陈爱丽
description: 检索团队的资深工程师。
tags: [爱丽, 工程师]
generated: { by: memory_backend/1.0, at: '2026-08-03T08:51:55+00:00' }
status: stable
okf_version: '0.2'
wiki_id: person/alice-chen
facts:
  - text: 负责检索工作流。
    confidence: 0.9
relations:
  - target: project/orion
    category: works_on
metadata:
  significance: 0.8
---

陈爱丽是一名资深工程师。

负责 [project/orion](/project/orion.md)（主管工程师）。
```

状态值：`stable`（默认）、`draft`、`deprecated`。

## 文件上传

文件按会话组织：`{user_id}/raw/{session_id}/{file_id}/`

聊天界面会从上传的文件中提取文本供 LLM 处理：
- `.txt`、`.md`、`.json`、`.csv`、`.log` — 直接读取
- `.docx` — XML 文本提取
- `.xlsx` — 通过共享字符串提取单元格值
- `.pdf` — 尽力文本提取

`POST /v1/users/{user_id}/files` 接受 multipart 上传。`session_id` 为必填项。

## 聊天界面

打开 `http://localhost:8000/chat`。功能：
- 页面任意位置拖放文件、Ctrl+V 粘贴、或点击 📎 按钮
- 文件在输入框下方显示为标签，发送后移入消息气泡中
- 支持仅发送文件（无需文字）
- 按会话组织文件存储

## LLM 提取

将对话转化为记忆操作。需要 `DEEPSEEK_API_KEY`。

```
POST /v1/users/{user_id}/extract  {"text": "..."}
```

返回建议的操作（创建实体、添加事实、建立关系）。审核后使用 `?apply=true` 或 `POST /extract/apply` 应用。

## 在 S3 上运行

```bash
STORAGE_BACKEND=mirage MIRAGE_S3_BUCKET=your-bucket uvicorn app.main:app --port 8000
```

或原生 boto3：`STORAGE_BACKEND=s3 S3_BUCKET=your-bucket`。

先运行 `python scripts/s3_preflight.py --bucket YOUR_BUCKET` 验证存储桶是否支持条件写入（必需）。

### IAM 策略

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::YOUR_BUCKET/memory/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::YOUR_BUCKET",
      "Condition": {"StringLike": {"s3:prefix": ["memory/*"]}}
    }
  ]
}
```

`s3:ListBucket` 不可省略——没有它，S3 对不存在的键返回 `403` 而非 `404`，会导致几乎所有代码路径失效。

## 测试

```bash
pip install pytest httpx
python -m pytest tests/ -v
```

## API 概览

| 端点 | 用途 |
|---|---|
| `GET /healthz` | 健康检查，报告当前使用的存储后端 |
| `POST /v1/auth/login` | 用户名/密码 → 会话令牌 |
| `GET /v1/auth/me` | 我是谁？（调试 403 用） |
| `POST /v1/auth/password` | 修改密码 |
| `GET /v1/users/{id}/wiki` | 列出实体（可筛选、分页） |
| `PUT /v1/users/{id}/wiki` | 创建或更新实体 |
| `GET /v1/users/{id}/wiki/{type}/{slug}` | 获取单个实体 |
| `DELETE /v1/users/{id}/wiki/{type}/{slug}` | 删除（逻辑删除） |
| `POST /v1/users/{id}/wiki/subgraph` | 遍历图谱邻域 |
| `POST /v1/users/{id}/wiki/_rebuild_manifest` | 从实体文件重建索引 |
| `POST /v1/users/{id}/wiki/_reconcile` | 修复悬空关系 |
| `POST /v1/users/{id}/extract` | 从文本中 LLM 提取 |
| `POST /v1/users/{id}/chat` | 带记忆上下文的聊天 |
| `POST /v1/users/{id}/files` | 上传文件 |
| `GET /v1/users/{id}/files/{session_id}` | 列出会话中的文件 |
| `GET /v1/users/{id}/sessions` | 列出对话会话 |

完整交互式文档：`http://localhost:8000/docs`。

## 故障排查

| 症状 | 原因 |
|---|---|
| 应用无法启动，"no tokens configured" | 在 `.env` 中设置 `AUTH_TOKENS` |
| `401 Missing bearer token` | 发送 `Authorization: Bearer <token>` |
| `401 Invalid username or password` | 未知或已禁用账号也会显示此提示 |
| `429 Too many failed attempts` | 登录限流；等待 Retry-After 指定的时间 |
| `503 Password login is not enabled` | 未设置 `AUTH_SECRET` |
| `401 Session expired` | 重新登录 |
| 每次请求约 600ms | `MIRAGE_REUSE_CONNECTIONS` 被禁用，或存储桶区域不对 |
| `_stats` 显示 0 条实体 | 当前指向的是一个空的 user_id |
| HTTP 422 "Entity file is corrupt" | 运行 `python scripts\check_bucket.py --fix` |

## 架构说明

- **实体文件是唯一数据源。** 清单和操作日志是派生的，可通过 `POST /wiki/_rebuild_manifest` 重建。
- **写缓冲** 将清单和操作日志保存在内存中，定时刷新。将每次 upsert 的 3 次 PUT 减少为 1 次。
- **日志结构清单** 使用快照 + 增量，而非重写单个不断增长的文件。总写入量是 O(N log N) 而非 O(N²)。
- **条件写入**（S3 `If-Match` / `If-None-Match`）防止并发写入冲突。Mirage 通过先读后写模拟此功能。
- **无状态会话令牌** — 每个请求无需读取存储。代价：单个令牌在过期前无法撤销。轮换 `AUTH_SECRET` 可使所有会话失效。
- **邻接索引** 在清单中维护，`traverse()` 零次存储读取即可完成——每条实体的边都已索引。
- **连接复用** 修补了 mirage 使其保持 TLS 连接，而非每次操作重新连接（远程端点延迟提升约 10 倍）。
