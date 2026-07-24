# DREAM

DREAM 是一个面向 Agent 的长期记忆形成、治理与检索框架。它在任务间歇回顾已经完成的会话，将其中具有长期价值的信息转化为可追溯的用户画像和 AI 决策经验，并在下一次任务中按需提供相关记忆。

DREAM 解决三个核心问题：

| 能力 | 解决的问题 | 主要产物 |
|---|---|---|
| User Persona | Agent 如何持续理解同一用户的偏好、习惯与约束 | `USER.md`、`USER_PERSONA.md` |
| AI Decision Evolution | Agent 如何从历史任务中沉淀可复用的判断经验 | Decision Cards、`DECISION_RULES.md` |
| Memory Retrieval | 当前任务应该读取哪些记忆，而不是加载全部历史 | Top-K Memories、受预算控制的 Agent Context |

DREAM 不直接替代业务 Agent。它负责回答两个问题：

1. 历史经历中什么值得长期记住？
2. 当前任务需要取回哪些相关记忆？

## 整体架构

```text
已完成的 Conversation / Agent Task
                  │
                  ▼
       Event Ledger 与等待队列
                  │
                  ▼
        Knowledge Extraction
    Agnes / OpenAI-compatible Provider
                  │
                  ▼
     Provider Adapter 与结构化校验
                  │
                  ▼
        Knowledge Governance
      分类 / 规范化 / 风险判断 / 路由
           ┌──────┼─────────┐
           ▼      ▼         ▼
      User Persona  Decision Cards  Skill Candidates
           │      │         │
           └──────┼─────────┘
                  ▼
       Snapshot / Version / Writeback
                  │
                  ▼
           Durable Memory
                  │
                  ▼
        Memory Retrieval Skill
     过滤 / 排序 / 去重 / 上下文预算
                  │
                  ▼
             External Agent
```

从外部模型到 DREAM 核心采用适配层隔离：

```text
不稳定的 Provider 输出
          ↓
Provider Adapter
JSON解析 / wrapper解包 / 字段归一化
          ↓
DREAM Canonical Knowledge
          ↓
严格业务校验与本地事务
```

模型负责发现知识，DREAM 负责知识类型、存储位置、风险治理、版本和回滚。

## 三项核心能力

### 1. 用户画像

DREAM 从用户消息中识别具有长期价值的事实和行为偏好，例如：

- 沟通方式与回答结构；
- 工作习惯和协作偏好；
- 风险接受程度；
- 稳定的兴趣与领域经验；
- 长期目标和现实约束；
- 对 Agent 的持续性要求。

画像以原子条目写入用户作用域的 `USER.md`。每条画像保留来源事件、置信度和领域等元数据。Persona Canonicalizer 与 Persona Merge Strategy 负责区分：

```text
new       → 新领域或新的独立画像
update    → 更新已有原子画像
merge     → 为已有画像增加新的维度
duplicate → 没有新增信息，不重复写入
```

`USER_PERSONA.md` 是面向 Agent 的画像投影，`USER.md` 是保留证据的长期画像仓库。

### 2. AI 决策进化

DREAM 关注 Agent 在任务中如何判断，而不是只保存最终回答。可复用经验会形成 Decision Card：

- 使用场景；
- 决策信号；
- 决策原则；
- 结果与证据；
- 反例和适用边界；
- 置信度与来源事件。

确定性 AI Curator 会从有效 Decision Cards 生成 `DECISION_RULES.md`，使下一任务可以加载稳定、可追溯并能随新证据修正的决策规则。

AI 决策经验位于 Agent 作用域，可以服务同一 Agent 下的不同用户；用户画像位于 User 作用域，不能跨用户读取。

### 3. Memory Retrieval

直接把所有画像、规则和决策卡塞入模型上下文会带来无关信息、Token 浪费和领域污染。`MemoryRetrievalSkill` 提供独立、只读的运行时检索能力：

```python
from pathlib import Path

from dream.retrieval import MemoryRetrievalSkill

skill = MemoryRetrievalSkill(
    home=Path("/path/to/dream-home"),
    tenant_id="enterprise-a",
    agent_id="service-agent",
)

result = skill.retrieve(
    user_id="user-001",
    query="供应商收款账户变更应该如何处理？",
    task_context={"domain": "finance"},
    limit=5,
)

for memory in result.memories:
    print(memory.type, memory.content)

print(result.context)
```

当前版本采用本地确定性检索，不依赖向量数据库：

- 严格校验 `tenant_id / agent_id / user_id`；
- 读取 `USER.md`、`USER_PERSONA.md`、`DECISION_RULES.md` 和 `decision-cards/`；
- 支持 finance、crypto、coding、writing、research 等领域识别；
- 综合关键词相关性、Memory 类型、置信度和更新时间排序；
- 合并近似重复记忆；
- 冲突时优先较新且置信度更高的记忆；
- 默认返回最多 5 条，并受上下文预算限制；
- 用户 Persona 严格隔离，Agent 级 Decision Rules 和 Cards 可以共享。

`MemoryRetrievalSkill` 当前是 Python Runtime API，不是 FastAPI 路由，也不会替换 `/v1/tasks/start` 的现有快照流程。外部 Agent 应在处理具体任务时主动调用它。

## Knowledge Governance

知识提取后先进入风险治理层，再决定是否影响 Active Memory：

```text
低风险 + 证据和置信度充分  → auto_activate
信息不完整或仍需观察       → observe candidate
敏感、权限或高风险内容     → ready_for_review
```

普通、稳定的用户偏好和结构完整的决策经验可以自动写回并激活。涉及敏感身份、权限放宽、绕过审批或高风险操作的内容保留人工审核。

Workflow Skill 可以被识别并记录为 `pending_skill_implementation` 候选，用于审计和后续开发。当前阶段不提供根据任务自动调用这些候选的 Skill Runtime；Memory Retrieval 也不会把它们当作已验证的可执行能力。

## 调度与两层 Curator

Background Review 按用户作用域自适应触发，任一条件满足即可处理：

- 用户空闲达到配置时长，默认 2 小时；
- 待处理事件达到批量上限；
- 估算 Token 达到批量上限；
- 最早事件等待达到最大时间。

每个成功批次之后，本地确定性 Curator 会立即整理发生变化的用户画像或 AI 决策卡。每天配置时刻（默认本地时间凌晨 3 点）还会进行幂等兜底检查。

大模型 Semantic Curator 默认关闭。启用后按照独立周期运行，默认要求：

- 距离上次尝试至少 168 小时；
- 对应作用域至少空闲 2 小时。

## 项目结构

```text
DREAM/
├── src/dream/
│   ├── api.py                       # FastAPI 入口
│   ├── config.py                    # 配置加载与后端构造
│   ├── application/                 # Dream 应用编排
│   │   ├── service.py
│   │   ├── closed_loop.py
│   │   ├── scheduler.py
│   │   ├── deadline.py
│   │   ├── progress.py
│   │   └── review_orchestrator.py
│   ├── core/                        # 事件、账本、作用域和标识符
│   ├── extraction/                  # LLM调用、Provider适配和结构化输出
│   │   ├── llm_backend.py
│   │   ├── provider_adapter.py
│   │   ├── structured.py
│   │   ├── models.py
│   │   ├── prompts.py
│   │   └── cache.py
│   ├── governance/                  # 知识规范化、路由和自动治理
│   ├── memory/                      # 长期记忆、发布和本地事务
│   │   ├── managers/
│   │   └── storage/
│   ├── retrieval/                   # 运行时记忆检索
│   │   ├── skill.py
│   │   ├── loader.py
│   │   ├── filters.py
│   │   ├── ranker.py
│   │   ├── context_builder.py
│   │   └── retrieval.skill
│   ├── curators/                    # 确定性与语义 Curator
│   ├── integrations/                # 手工输入和 Internship 数据源
│   └── validation/                  # 正式验证工具
├── tests/                           # 与源码分层对应的测试
├── fixtures/                        # 脱敏样例和测试数据
├── docs/
│   ├── api/
│   ├── design/
│   └── validation/
├── .env.example
├── pyproject.toml
└── README.md
```

## 运行产物

运行数据位于 `DREAM_HOME`，不应提交到 Git：

```text
<DREAM_HOME>/
├── ledger/
│   └── events.jsonl
├── source-state/
└── tenants/<tenant_id>/agents/<agent_id>/
    ├── users/<user_id>/
    │   ├── USER.md
    │   └── USER_PERSONA.md
    ├── decision-cards/
    │   └── *.md
    ├── skills/
    │   └── *.skill                    # 候选产物，不代表已注册 Runtime
    ├── DECISION_RULES.md
    ├── CHARACTER_DEFINITION.md
    ├── publication/users/<user_id>/
    │   ├── active.json
    │   ├── latest.json
    │   ├── pending.json
    │   └── versions/
    ├── snapshots/
    ├── curator-state/
    └── dream-reports/
        └── review-traces/
```

所有主要产物都是可检查的本地文件。Publication、Snapshot 和 Report 共同记录一次 Dream 的输入、候选、版本、激活状态和失败恢复信息。

## 快速开始

### 环境要求

- Python `>=3.11,<3.14`
- macOS、Linux 或 Windows
- 可选：OpenAI-compatible LLM Provider

### 安装

```bash
git clone <repository-url>
cd DREAM
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Windows PowerShell 激活虚拟环境：

```powershell
.venv\Scripts\Activate.ps1
```

### 配置

```bash
cp .env.example .env
```

最小模型配置：

```dotenv
DREAM_HOME=~/.dream
DREAM_REVIEW_BACKEND=openai
DREAM_REVIEW_MODEL=your-openai-compatible-model
DREAM_REVIEW_BASE_URL=https://api.openai.com/v1
DREAM_LLM_API_KEY=your-secret
DREAM_LLM_STRUCTURED_MODE=auto
DREAM_LLM_TIMEOUT_SECONDS=90
DREAM_DEADLINE_SECONDS=300
DREAM_CURATOR_CONSOLIDATE=false
```

离线单元测试不需要真实 API Key。真实知识提取需要配置可用的 OpenAI-compatible Provider。

如果本机代理环境会干扰模型连接，可以在确认安全和网络策略后设置：

```dotenv
DREAM_LLM_TRUST_ENV=false
```

### 启动 FastAPI

```bash
uvicorn dream.api:app --host 127.0.0.1 --port 8765
```

如果需要使用其他环境文件：

```bash
DREAM_ENV_FILE=/absolute/path/to/.env.local \
uvicorn dream.api:app --host 127.0.0.1 --port 8765
```

Swagger 页面：

```text
http://127.0.0.1:8765/docs
```

## 完成一次 Memory Formation

### 1. 提交完整会话

```bash
curl --fail-with-body \
  -X POST http://127.0.0.1:8765/v1/dream/conversations \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "enterprise-a",
    "agent_id": "service-agent",
    "user_id": "user-001",
    "event_id": "evt-demo-001",
    "conversation_id": "session-demo-001",
    "completed_at": "2026-07-24T10:00:00+08:00",
    "interrupted": false,
    "tool_iterations": 4,
    "messages": [
      {
        "role": "user",
        "content": "以后处理高风险问题时，请先给结论，再明确暂停条件和继续条件。"
      },
      {
        "role": "assistant",
        "content": "明白。高风险任务会先核验事实，并明确当前能做什么、何时可以继续。"
      }
    ],
    "final_response": "已按结论、暂停条件和继续条件给出处理建议。"
  }'
```

成功返回：

```json
{
  "event_id": "evt-demo-001",
  "status": "queued"
}
```

`event_id` 是幂等键，重复事件不会被重复学习。

也可以将完整任务按 NDJSON 直接提交到：

```http
POST /v1/validation/import
Content-Type: application/x-ndjson
```

### 2. 显式执行一次 Dream

```bash
curl --fail-with-body \
  -X POST http://127.0.0.1:8765/v1/validation/dream \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "enterprise-a",
    "agent_id": "service-agent",
    "user_id": "user-001"
  }'
```

返回状态可能是：

- `active`：低风险结果已经自动写回并激活；
- `ready_for_review`：高风险候选需要执行 approve、confirm-writeback、activate；
- HTTP `503`：候选生成失败，修改前状态已经恢复，pending 事件保留。

不要在失败后重复导入相同事件；修复原因后重新调用一次 `/v1/validation/dream`。

### 3. 读取下一任务上下文

```bash
curl --fail-with-body \
  -X POST http://127.0.0.1:8765/v1/tasks/start \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "enterprise-a",
    "agent_id": "service-agent",
    "user_id": "user-001"
  }'
```

响应包括：

```json
{
  "snapshot_id": "sha256...",
  "user_profile": "当前 Active 用户画像",
  "decision_rules": "当前 Active AI 决策规则",
  "decision_cards": ["当前 Active Decision Cards"]
}
```

同一前台任务应固定使用一个 `snapshot_id`，避免后台 Dream 改变正在执行的任务。

## FastAPI 接口

### 会话与任务

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/v1/dream/conversations` | 写入一段已完成会话 |
| `POST` | `/v1/tasks/start` | 创建下一任务的冻结上下文 |
| `POST` | `/v1/dream/run-pending` | 立即处理所有 pending 作用域 |
| `POST` | `/v1/dream/run-curators` | 强制运行指定作用域的确定性 Curator |
| `POST` | `/v1/dream/run-due-curators` | 执行已到期的每日兜底 Curator |
| `POST` | `/v1/dream/rollback/{snapshot_id}` | 恢复指定快照 |
| `GET` | `/v1/dream/reports/{run_id}` | 读取 Dream 报告 |

### 验证与发布

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/v1/validation/import` | 导入完整任务 NDJSON |
| `POST` | `/v1/validation/dream` | 对指定用户执行一次闭环 Dream |
| `GET` | `/v1/validation/publications/status` | 查询 latest 与 active 版本 |
| `POST` | `/v1/validation/publications/{version}/approve` | 批准高风险候选 |
| `POST` | `/v1/validation/publications/{version}/confirm-writeback` | 确认两份回写投影 |
| `POST` | `/v1/validation/publications/{version}/activate` | 激活已就绪版本 |
| `POST` | `/v1/validation/publications/{version}/reject` | 拒绝候选并恢复 |
| `POST` | `/v1/validation/publications/{version}/rollback` | 恢复历史 Active 版本 |

Memory Retrieval 当前不提供 HTTP 接口。请通过 `dream.retrieval.MemoryRetrievalSkill` 从 Agent Runtime 调用。

## 接入现有 Agent

现有 Agent 只需要连接两个位置。

### 任务完成后：写入经历

将完整的 user、assistant、system、tool 消息和 final response 提交给 DREAM：

```text
Agent completes task
        ↓
POST /v1/dream/conversations
        ↓
DREAM Event Ledger
```

也可以通过 `integrations/internship` 从只读 NDJSON 导出接口增量拉取。Cursor 只有在事件持久化成功或确认重复后才推进。

### 新任务开始前：读取相关记忆

```text
Agent receives query
        ↓
MemoryRetrievalSkill.retrieve(...)
        ↓
Top-K Persona / Decision Rules / Decision Cards
        ↓
Inject result.context into Agent prompt
```

如果业务要求严格的版本冻结，可以先调用 `/v1/tasks/start` 获得 Active Snapshot，再由外部 Agent 使用 Retrieval Skill 构造更紧凑的任务上下文。

## 多租户和作用域

DREAM 使用三级作用域：

```text
tenant_id / agent_id / user_id
```

- `tenant_id`：租户或组织；
- `agent_id`：同一组织内的 Agent；
- `user_id`：Agent 服务的具体用户。

三个 ID 仅允许字母、数字、下划线和连字符，长度不超过 64。调用方不能传入磁盘路径。

User Persona 严格位于用户作用域。Decision Cards 和 `DECISION_RULES.md` 位于 Agent 作用域，用于同一 Agent 的通用决策进化。

## 可靠性与安全失败

- 普通 Dream 事务默认总截止时间为 300 秒；
- 单次模型请求有独立超时；
- 正常结构化输出只调用一次模型；
- 仅在检测到非法结构化输出时最多进行一次修复调用；
- Provider 输出先归一化，再进行严格业务校验；
- 已验证的语义结果进入本地缓存，本地步骤重试可以复用；
- 修改前创建 Snapshot，写回、版本、报告和激活在本地事务中衔接；
- 任一步骤失败都会恢复修改前状态；
- 失败 Publication 记录 `failure_reason` 和 `fallback_version`；
- pending 事件不会因失败丢失；
- 本地文件使用原子替换，避免读取到半写入结果；
- API Key、认证头和不必要的完整敏感对话不会写入 Review Trace。

## 主要配置

| 变量 | 说明 | 默认值 |
|---|---|---|
| `DREAM_HOME` | Ledger、Memory、Snapshot 和 Report 目录 | `~/.dream` |
| `DREAM_REVIEW_BACKEND` | `deterministic` 或 `openai` | `deterministic` |
| `DREAM_REVIEW_MODEL` | Background Review 模型 | — |
| `DREAM_REVIEW_BASE_URL` | OpenAI-compatible Base URL | — |
| `DREAM_LLM_API_KEY` | Provider API Key | — |
| `DREAM_REVIEW_IDLE_HOURS` | 空闲触发时长 | `2` |
| `DREAM_REVIEW_MAX_BATCH_TOKENS` | 单批估算 Token 上限 | `16000` |
| `DREAM_REVIEW_MAX_BATCH_EVENTS` | 单批事件上限 | `20` |
| `DREAM_REVIEW_MAX_WAIT_HOURS` | 最早事件最大等待 | `24` |
| `DREAM_LLM_STRUCTURED_MODE` | `auto`、`tools` 或 `json` | `auto` |
| `DREAM_LLM_TIMEOUT_SECONDS` | 单次 Provider 请求超时 | `90` |
| `DREAM_LLM_TRUST_ENV` | 是否读取 HTTP(S) 代理环境 | `true` |
| `DREAM_DEADLINE_SECONDS` | 一次普通 Dream 总截止时间 | `300` |
| `DREAM_TIMEZONE` | 调度时区 | `Asia/Shanghai` |
| `DREAM_CURATOR_DAILY_HOUR` | 确定性 Curator 每日兜底小时 | `3` |
| `DREAM_CURATOR_CONSOLIDATE` | 是否启用语义 Curator | `false` |
| `DREAM_CURATOR_CONSOLIDATE_INTERVAL_HOURS` | 语义 Curator 周期 | `168` |
| `DREAM_CURATOR_CONSOLIDATE_MIN_IDLE_HOURS` | 语义 Curator 最小空闲 | `2` |
| `DREAM_VALIDATION_REQUIRE_ACTIVE_WRITEBACK` | 下一任务是否强制要求 Active 版本 | `false` |

完整配置见 [.env.example](.env.example)。

## 在 PyCharm 中运行

### Python 解释器

选择项目自己的解释器：

```text
<project>/DREAM/.venv/bin/python
```

### FastAPI Run Configuration

在 `Run/Debug Configurations` 中新增 Python 配置：

```text
Name: DREAM API
Run: Module name
Module: uvicorn
Parameters: dream.api:app --host 127.0.0.1 --port 8765
Working directory: <project>/DREAM
Environment variables:
  DREAM_ENV_FILE=<project>/DREAM/.env
```

如果使用独立测试数据，创建新的 `.env.local` 并设置新的 `DREAM_HOME`，不要删除或复用正式运行目录。

## 测试

运行完整测试：

```bash
PYTHONDONTWRITEBYTECODE=1 \
python -m pytest -q -p no:cacheprovider
```

运行三个核心能力的最小验证：

```bash
PYTHONDONTWRITEBYTECODE=1 \
python -m pytest -q -p no:cacheprovider \
  tests/governance/test_closed_loop_governance.py \
  tests/e2e/test_api.py \
  tests/retrieval/test_memory_retrieval_skill.py
```

代码检查：

```bash
ruff check src tests
```

正式端到端测试必须通过 FastAPI 导入原始会话，并由 DREAM 完成提取、治理、写回、版本和激活。测试脚本不能直接调用模型后手工写 `USER.md` 或 Decision Cards。

## GitHub 与本地数据安全

可以提交：

- `src/dream/`
- `tests/`
- `docs/`
- 脱敏并已跟踪的 `fixtures/`
- `README.md`
- `pyproject.toml`
- `.env.example`

必须留在本地：

```text
.env
.env.*
.venv/
.idea/
validation-run/
__pycache__/
.pytest_cache/
.ruff_cache/
*.local.jsonl
```

不要把 API Key、真实聊天记录、Ledger、用户画像、决策卡、Snapshot 或运行报告上传到 GitHub。

## 文档

- [短期记忆与会话接入协议](docs/api/short-term-memory-contract.md)
- [Conversation Export API](docs/api/conversation-export-api.md)
- [DREAM Memory 架构设计](docs/design/2026-07-15-dream-memory-architecture.md)
- [自适应 Dream 与 Curator 设计](docs/design/2026-07-21-adaptive-dream-curator-design.md)
- [闭环验证说明](docs/validation/character-ai-runbook.md)
- [Agent 进化验证流程](docs/validation/codex-task-evolution-runbook.md)

## 当前边界

- Retrieval 当前使用本地词法匹配和确定性排序，尚未接入 Embedding、BM25 或外部 Reranker；
- Memory Retrieval 是独立 Python Runtime，尚未提供 HTTP 接口；
- Workflow Skill 仍处于候选和审计阶段，没有完整的检索、选择和执行 Runtime；
- 语义 Curator 默认关闭；
- DREAM 默认只监听本机，生产部署需要由外部网关提供认证、授权、TLS、限流和审计。
