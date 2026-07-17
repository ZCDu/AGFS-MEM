# Dreams

Dreams 让 AI 智能体在两次任务之间回顾过去的会话与任务经历，持续提纯用户人物画像，并塑造自身的决策身份。

智能体在工作过程中积累的信息通常是局部和增量的。随着 Session 不断增加，用户信息会出现重复、变化和矛盾，AI 的决策经验也会散落在不同任务中，难以在未来直接复用。

核心思路：**智能体过去的会话与任务经历通过 LLM 回顾，分别提纯为持续进化的 `USER.md` 用户人物画像和 AI 决策卡**；**两类产物再通过周期做梦合并重复信息、修正过时或冲突内容、归档旧经验，并从下一次任务开始生效。**

对用户，Dreams 逐步形成包含身份背景、兴趣喜好、表达习惯、工作方式、长期目标和互动模式的人物画像；对 AI，Dreams 将真实任务中的判断过程提炼成决策卡，使智能体逐渐形成连续、稳定、可追溯的决策方式。

## 架构

```text
智能体已完成的 Session
user / assistant / system / tool
              │
              │ API 推送或增量拉取
              ▼
        事件账本与等待队列
              │
              ▼
       后台回顾与证据分类
              │
       ┌──────┴──────┐
       ▼             ▼
  用户人物证据      AI 决策经验
       │             │
       ▼             ▼
    USER.md       AI 决策卡
       │             │
       └──────┬──────┘
              ▼
         周期做梦整理
   合并重复 / 修正冲突 / 归档旧经验
              │
              ▼
     下一次任务的冻结上下文快照
```

Dreams 有两层做梦：

1. **会话完成后的后台回顾**：判断这一轮经历是否包含值得长期保留的用户证据或 AI 决策经验，只调用对应的管理工具。
2. **周期整理**：分别维护用户人物画像和 AI 决策卡，处理重复、变化、冲突和过时内容，生成更稳定的新版本。

## 核心产物

### `USER.md`：持续进化的用户人物画像

`USER.md` 不是简单的偏好列表，而是从长期 Session 中逐步形成的用户人物形象。它可以包含：

- 身份与背景；
- 兴趣、喜好与明确厌恶；
- 表达方式和沟通习惯；
- 工作方式与协作偏好；
- 长期目标、计划与现实约束；
- 稳定的思考和决策模式；
- 对 AI 的长期期待与互动方式。

每条人物证据保留来源事件，后续做梦必须保留这些证据引用。当前输出是持续进化的 `USER.md`，不是自动生成的可调用 Skill。

### AI 决策卡：智能体的经验单元

AI 决策卡记录智能体在真实任务中形成的可复用判断经验。每张卡片包括：

- 使用场景；
- 触发判断的关键信号；
- 采用的决策原则；
- 本次决策结果；
- 反例与适用边界；
- 置信度与来源事件。

决策卡强调 AI 如何判断，而不是简单记录 AI 做过什么。一次偶然选择不会直接成为永久身份，只有能够指导未来任务、并且具有明确证据和边界的经验才值得保留。

### `DECISION_RULES.md`：稳定的 AI 决策身份

周期做梦会从多张决策卡中提炼 `DECISION_RULES.md`，将分散的经验组合成更稳定的判断规则。

这里的“拟人”不是模仿某种语气，也不是角色扮演，而是让智能体逐渐拥有：

- 连续的判断方式；
- 可追溯的经验来源；
- 相对稳定的原则；
- 能够随新证据修正的认知边界。

AI 决策卡属于智能体级产物，不得复制任何用户的身份、个人偏好或秘密。

## 项目结构

```text
Dreams/
├── src/dream/
│   ├── api.py                       # HTTP API 与后台工作循环
│   ├── config.py                    # 环境变量和模型配置
│   ├── events.py                    # 已完成 Session 事件模型
│   ├── ledger.py                    # 不可变 JSONL 事件账本
│   ├── service.py                   # 做梦流程编排
│   ├── scheduler.py                 # 后台回顾与周期任务调度
│   ├── source_sync.py               # NDJSON 与 cursor 增量同步
│   ├── closed_loop.py               # 做梦、审核、回写、激活与失败回退闭环
│   ├── publication.py               # 用户级候选版本和 active 版本状态机
│   ├── writeback.py                 # Character Definition 与 User Persona 生成
│   ├── review/
│   │   ├── orchestrator.py          # 会话回顾与产物分类
│   │   ├── llm_backend.py           # LLM 结构化管理调用
│   │   └── backend.py               # 可替换的回顾后端
│   ├── managers/
│   │   ├── memory.py                # USER.md 证据写入
│   │   └── decision_cards.py        # AI 决策卡写入
│   ├── curators/
│   │   ├── user.py                  # 用户人物画像周期整理
│   │   ├── ai.py                    # AI 决策卡周期整理
│   │   └── llm_backend.py           # 语义合并与冲突处理
│   ├── snapshots.py                 # 下一任务冻结上下文
│   ├── rollback.py                  # 修改前快照与恢复
│   ├── reports.py                   # 每次做梦的 JSON 报告
│   ├── artifacts.py                 # 磁盘产物原子写入
│   └── validation/
│       ├── agnes.py                 # 隔离的模拟用户消息生成器
│       ├── seeds.py                 # AI 初始决策种子校验和导入
│       └── evaluation.py            # 闭环效果评估与通过标准复算
├── docs/
│   ├── api/                         # Session 接入契约
│   ├── design/                      # 架构说明
│   └── validation/                  # Character.AI 人工闭环验证手册
├── tests/                           # 单元与闭环测试
├── .env.example                     # 配置示例
├── .gitignore
├── README.md
└── pyproject.toml
```

运行数据默认位于 `DREAM_HOME`：

```text
<DREAM_HOME>/
├── ledger/events.jsonl
├── source-state/
└── tenants/<tenant_id>/agents/<agent_id>/
    ├── users/<user_id>/USER.md
    ├── users/<user_id>/USER_PERSONA.md
    ├── decision-cards/*.md
    ├── DECISION_RULES.md
    ├── CHARACTER_DEFINITION.md
    ├── publication/users/<user_id>/
    ├── curator-state/*.json
    ├── dream-reports/*.json
    └── snapshots/
```

## API

### `POST /v1/dream/conversations`

提交一轮已经完成的智能体 Session。Dreams 将其写入事件账本并加入后台回顾队列。

```json
{
  "tenant_id": "enterprise-a",
  "agent_id": "service-agent",
  "user_id": "user-001",
  "event_id": "evt-20260716-001",
  "conversation_id": "session-001:round-01",
  "completed_at": "2026-07-16T10:00:00+08:00",
  "interrupted": false,
  "tool_iterations": 12,
  "headroom_summary": "用户长期希望先给结论。",
  "messages": [
    {"role": "user", "content": "以后先给我简短结论，再解释原因。"},
    {"role": "assistant", "content": "明白。涉及不可逆操作时，我会先验证再执行。"}
  ],
  "final_response": "已记录你的沟通偏好，并完成只读验证。"
}
```

成功入队返回：

```json
{
  "event_id": "evt-20260716-001",
  "status": "queued"
}
```

`event_id` 是幂等键。重复事件不会被再次学习。

### `POST /v1/tasks/start`

为新任务创建冻结上下文。新的做梦结果只会在再次调用该接口时出现。

```json
{
  "tenant_id": "enterprise-a",
  "agent_id": "service-agent",
  "user_id": "user-001"
}
```

响应包括：

```json
{
  "snapshot_id": "sha256...",
  "user_profile": "持续进化的 USER.md 内容",
  "decision_rules": "整理后的 AI 决策规则",
  "decision_cards": ["当前有效的 AI 决策卡"]
}
```

一次前台任务应该始终使用同一个 `snapshot_id` 对应的内容，避免后台做梦改变正在执行的任务。

### 做梦与维护接口

| 方法 | 路径 | 作用 |
|------|------|------|
| `POST` | `/v1/dream/run-pending` | 立即处理等待中的 Session |
| `POST` | `/v1/dream/run-curators` | 强制整理指定用户画像和智能体决策卡 |
| `POST` | `/v1/dream/run-due-curators` | 只运行已经到期的周期整理任务 |
| `POST` | `/v1/dream/rollback/{snapshot_id}` | 恢复到指定修改前快照 |
| `GET` | `/v1/dream/reports/{run_id}` | 获取一次做梦的运行报告 |

### 闭环发布接口

Character.AI 验证使用候选版本，确保上一任务产生的画像和决策经验完成做梦、人工检查和回写后，才影响下一任务。

| 方法 | 路径 | 作用 |
|------|------|------|
| `POST` | `/v1/validation/import` | 导入人工整理的完整任务 NDJSON |
| `POST` | `/v1/validation/dream` | 生成用户画像、决策规则和两份回写候选 |
| `POST` | `/v1/validation/publications/{version}/approve` | 人工检查通过候选版本 |
| `POST` | `/v1/validation/publications/{version}/confirm-writeback` | 确认两份文本已经写入 Character.AI |
| `POST` | `/v1/validation/publications/{version}/activate` | 激活版本并允许开始下一任务 |
| `POST` | `/v1/validation/publications/{version}/reject` | 拒绝候选并恢复做梦前快照 |
| `POST` | `/v1/validation/publications/{version}/rollback` | 恢复一个曾经激活的历史版本 |
| `GET` | `/v1/validation/publications/status` | 查看当前候选版本和 active 版本 |

完整的人工验证顺序、`curl` 示例和通过标准见 [Character.AI 闭环验证手册](docs/validation/character-ai-runbook.md)。

## 用户人物画像提纯

用户提纯以单个用户的现有 `USER.md`、本次 Session 和历史证据为输入。

```text
user 消息与已有 USER.md
          │
          ▼
   判断是否为长期人物证据
          │
          ├── 新的稳定信息 ──→ 添加
          ├── 已有信息变化 ──→ 替换
          ├── 明确不再成立 ──→ 移除
          └── 一次性要求 ────→ 不保存
          │
          ▼
  保留来源事件的 USER.md
          │
          ▼
      周期人物画像做梦
  合并同义项 / 修正过时项 / 标记冲突
```

提纯时遵循以下原则：

1. 用户明确陈述或在多次 Session 中稳定表现出的信息，才适合成为人物画像。
2. 一次普通请求不能自动推断成永久偏好。
3. 较新的明确证据可以更新旧结论。
4. 证据无法解决冲突时，同时保留并标记争议，而不是擅自选择一方。
5. 周期整理必须保留全部来源事件，不能生成没有证据支持的人物特征。

## AI 决策身份提纯

AI 提纯关注智能体在任务中如何判断，而不是只保存最终答案。

```text
assistant / tool 消息与最终结果
               │
               ▼
       是否存在可复用的决策过程
               │
        ┌──────┴──────┐
        │             │
       否             是
        │             │
      忽略            ▼
                生成 AI 决策卡
          场景 / 信号 / 原则 / 结果 / 边界
                      │
                      ▼
                  周期做梦
          合并重叠卡 / 归档旧卡 / 提炼规则
                      │
                      ▼
             DECISION_RULES.md
```

一张决策卡的磁盘内容类似：

```markdown
# 高风险操作前先验证

## 使用场景

任务要求执行难以回滚的操作。

## 决策信号

- 操作不可逆
- 关键信息不足

## 决策原则

先完成只读验证，再决定是否执行。

## 本次结果

验证发现了错误前提，避免了错误修改。

## 反例与边界

低风险且可回滚的操作无需反复确认。
```

决策卡不是固定人格设定。它允许随着新任务、新结果和反例不断修正，逐渐形成更真实的 AI 决策身份。

## 周期做梦

持续写入会使人物画像和决策卡出现重复、矛盾与过时内容。周期做梦负责重新综合现有产物。

### 用户人物画像做梦

用户整理器只处理一个用户的 `USER.md`：

- 合并表达相同含义的重复人物信息；
- 优先采用较新的明确证据；
- 对无法解决的冲突保留双方并标记；
- 保留所有证据事件；
- 输出符合长度限制的新版本。

### AI 决策身份做梦

AI 整理器读取当前决策卡与 `DECISION_RULES.md`：

- 合并表达同一原则的决策卡；
- 归档被取代或重复的卡片；
- 保留有效卡片和证据标识；
- 将多张卡片提炼为紧凑的决策规则；
- 防止一次性行为被误写成永久身份。

默认调度会定期检查是否到期，也可以通过 API 强制运行。每次运行都会生成报告和修改前快照。

## Session 接入

Dreams 支持两种 Session 接入方式。

### 主动推送

调用方在一轮 Session 完成后，将完整消息和最终回复提交到：

```http
POST /v1/dream/conversations
```

### NDJSON 增量拉取

Dreams 也可以定期读取只读导出接口：

```http
GET <export-url>?after=<cursor>&limit=<batch-size>
Accept: application/x-ndjson
Authorization: Bearer <source-api-key>
```

每一行是一条完整的 JSON 记录，必须包含：

| 字段 | 作用 |
|------|------|
| `cursor` | 单调前进的读取位置 |
| `event_id` | 稳定唯一的事件标识 |
| `user_id` | 用户人物画像作用域 |
| `session_id` | 原始 Session 标识 |
| `round_id` | 原始轮次标识 |
| `completed_at` | 带时区的完成时间 |
| `messages` | 完整的 Session 消息 |
| `final_response` | 该轮最终回复 |

Dreams 只有在事件持久化成功或确认事件已经存在后才推进 `cursor`。没有新增记录时，导出接口返回 HTTP 200 和空响应体即可。

## 多用户服务

Dreams 使用三个作用域标识：

```text
tenant_id / agent_id / user_id
```

- `USER.md` 位于用户作用域，同一智能体服务不同用户时，各自的人物画像不会互相读取。
- AI 决策卡和 `DECISION_RULES.md` 位于智能体作用域，同一智能体可以把通用决策经验应用到不同用户的未来任务。
- 三个 ID 只允许字母、数字、下划线和连字符，最长 64 个字符，调用方不能传入磁盘路径。

用户级隔离是服务多个用户的安全边界，但用户提纯的目标仍然是形成完整、持续进化的人物画像。

## 快照、报告与回滚

### 下一任务快照

`POST /v1/tasks/start` 会把当前 `USER.md`、AI 决策卡和决策规则冻结为上下文快照，并用内容哈希生成 `snapshot_id`。

后台做梦不会改变已经返回的快照，因此新结果只影响下一次任务。

### 修改前快照

每次自主写入前，Dreams 会保存目标文件修改前的状态。发生误提炼时，可以通过快照 ID 恢复原内容。

### 运行报告

每次后台回顾和周期整理都会写入 JSON 报告，记录：

- 运行 ID 与状态；
- 来源事件；
- 修改的产物类型；
- 归档数量；
- 错误信息；
- 对应的回滚快照 ID。

所有主要产物都写在磁盘上，可以直接检查、修改、归档或恢复。

## 容错策略

- 重复 `event_id`：拒绝重复写入，不重复学习。
- Session 中断或没有最终回复：不进入后台提纯。
- 增量拉取失败：保留上次 `cursor`，下一周期继续读取。
- 单条记录校验失败：停止推进该记录之后的 `cursor`。
- 某类产物写入失败：在报告中标记部分失败，不伪装成完整成功。
- 周期整理产生空规则、未知归档目标或丢失证据：拒绝应用该结果。
- 文件更新：使用原子替换，避免读取到半写入文件。
- 错误提纯：使用修改前快照回滚。

## 核心模块

| 模块 | 文件 | 职责 |
|------|------|------|
| `DreamService` | `service.py` | 编排事件写入、后台回顾、周期整理和任务上下文 |
| `EventLedger` | `ledger.py` | 持久化不可变 Session 事件并提供幂等检查 |
| `DreamScheduler` | `scheduler.py` | 管理等待回顾的事件和周期状态 |
| `BackgroundReviewOrchestrator` | `review/orchestrator.py` | 将 Session 分类为用户人物证据或 AI 决策经验 |
| `MemoryManager` | `managers/memory.py` | 增加、替换或移除 `USER.md` 人物证据 |
| `DecisionCardManager` | `managers/decision_cards.py` | 将 AI 决策经验写成可检查的 Markdown 卡片 |
| `UserCurator` | `curators/user.py` | 周期整理单个用户的人物画像 |
| `AICurator` | `curators/ai.py` | 周期整理决策卡并生成 `DECISION_RULES.md` |
| `SnapshotStore` | `snapshots.py` | 生成下一任务使用的冻结上下文 |
| `RollbackService` | `rollback.py` | 保存和恢复自主写入前的文件状态 |
| `DreamReportStore` | `reports.py` | 保存每次做梦的可审计报告 |
| Session 增量同步 | `source_sync.py` | 拉取 NDJSON、转换事件并持久化 `cursor` |

## 配置

复制示例配置：

```bash
cp .env.example .env
```

主要模型配置：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DREAM_HOME` | 事件、画像、卡片、报告和快照的数据目录 | `~/.dream` |
| `DREAM_REVIEW_BACKEND` | 后台回顾后端：`deterministic` 或 `openai` | `deterministic` |
| `DREAM_REVIEW_MODEL` | 后台回顾模型 | — |
| `DREAM_REVIEW_BASE_URL` | OpenAI-compatible API 地址 | — |
| `DREAM_LLM_API_KEY` | 后台回顾 API 密钥 | — |
| `DREAM_REVIEW_MAX_COMPLETION_TOKENS` | 单次后台回顾最大输出 token | `2000` |
| `DREAM_CURATOR_BACKEND` | 周期整理后端：`inherit`、`deterministic` 或 `openai` | `inherit` |
| `DREAM_CURATOR_MODEL` | 周期整理模型；为空时可继承回顾模型 | — |
| `DREAM_CURATOR_BASE_URL` | 周期整理 API 地址 | — |
| `DREAM_CURATOR_LLM_API_KEY` | 周期整理 API 密钥 | — |
| `DREAM_CURATOR_MAX_COMPLETION_TOKENS` | 单次周期整理最大输出 token | `3000` |
| `DREAM_CHARACTER_DEFINITION_LIMIT` | Character Definition 最大字符数 | `3200` |
| `DREAM_USER_PERSONA_LIMIT` | User Persona 最大字符数 | `1200` |
| `DREAM_VALIDATION_REQUIRE_ACTIVE_WRITEBACK` | 是否在下一任务前强制检查 active 版本 | `false` |

可选的 Session 增量拉取参数、批量大小、请求超时和同步周期见 `.env.example`。

真实 `.env` 已被 Git 忽略，不要把 API 密钥提交到仓库。

## Character.AI 闭环验证

项目包含三类离线验证工具：

- `python -m dream.validation.seeds validate ...`：检查 AI 初始决策样本只能包含公开场景和回答，不能携带用户画像或隐藏人物设定；
- `AgnesSimulator`：在单次内存请求中根据隐藏人物设定产生一条模拟用户消息，不把隐藏设定写入 DREAM；
- `python -m dream.validation.evaluation verify ...`：重新计算画像证据率、个性化成功率、AI 决策进化率和安全隔离指标，防止直接修改报告中的通过结果。

自动化测试使用少量合成会话验证结构。正式效果结论仍需由三个模拟用户分别完成 10～15 个任务，并经过人工证据检查；不能用合成测试结果冒充正式验证结果。

## 开发

安装项目与开发依赖：

```bash
python -m pip install -e '.[dev]'
```

运行测试：

```bash
python -m pytest -v
```

启动本地 API：

```bash
uvicorn dream.api:app --host 127.0.0.1 --port 8765
```

默认只监听本机。需要在企业网络中提供服务时，应在外部网关配置认证、授权、TLS、限流与审计。

完整的 Session 字段与作用域约束见 `docs/api/short-term-memory-contract.md`。
