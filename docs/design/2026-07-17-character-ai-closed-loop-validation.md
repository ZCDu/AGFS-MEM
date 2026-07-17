# Dreams × Character.AI 闭环进化验证设计

**状态：** 已确认  
**日期：** 2026-07-17  
**范围：** 测试数据接入、用户画像提炼、AI 决策提炼、梦境整理、人工回写和下一任务验证

## 1. 目标

在上游智能体暂时没有真实 Session 数据时，使用 Agnes 模拟用户、使用 Character.AI 扮演企业同事型 AI，通过 10～15 个连续任务验证 Dreams 的两个核心能力：

1. 从过去的对话中提炼持续进化的 `USER.md` 用户人物画像，并验证 AI 在下一任务中能否根据画像提供个性化服务。
2. 从 Character.AI 的判断和回答中提炼 AI 决策卡，通过周期做梦形成稳定的决策身份，并验证同一个 Character 在下一任务中是否表现出更新后的判断方式。

本验证必须形成完整闭环：

```text
完成任务
  → Dreams 做梦
  → 生成用户画像和 AI 决策规则
  → 整理、检查和发布新版本
  → 回写 Character.AI
  → 开启新聊天执行下一任务
  → 再次做梦
```

只生成本地 Markdown 文件不算完成；新产物必须能够影响下一任务。

## 2. 验证边界

### 2.1 本阶段包含

- 约 30 条人工筛选的公开样本，用于建立企业同事型 AI 的初始决策身份。
- 3 个隔离的模拟用户，每个用户完成 10～15 个任务。
- 手工将完整对话整理为 JSONL。
- Agnes API 同时承担模拟用户和语义提炼，但两个角色严格隔离。
- 用户画像与 AI 决策卡的事件级提炼和周期做梦。
- 新旧版本、快照、报告、失败保护和回滚。
- 将压缩后的产物人工回写 Character.AI。
- 在全新聊天中验证个性化服务和 AI 进化效果。

### 2.2 本阶段不包含

- 调用 Character.AI 的非公开接口。
- 浏览器抓取或自动修改 Character.AI 页面。
- 把 Agnes 的隐藏人物设定直接交给 Character.AI。
- 使用真实员工个人信息。
- 自动写回最终企业智能体。
- 文件知识、技能和待办提炼。

Character.AI 当前没有本设计可以依赖的正式公开写入 API。因此，本阶段采用“自动做梦、人工检查、人工回写、自动生成回写文本”的方式。后续接入支持 API 的企业智能体时，人工回写可以替换为自动发布，不改变 Dreams 的核心提炼逻辑。

## 3. 角色与隔离

### 3.1 Agnes 模拟用户

模拟用户请求可以读取一个隐藏人物设定，只负责生成自然的用户消息。测试使用三个用户：

| `user_id` | 人物方向 | 主要特征 |
|---|---|---|
| `project-manager` | 项目经理 | 关注结论、进度、负责人和风险 |
| `python-beginner` | Python 初学者 | 需要分步解释、示例和低术语密度 |
| `technical-lead` | 技术负责人 | 关注证据、边界、风险和回滚 |

隐藏人物设定只用于产生测试消息和最终对照，不得进入 Dreams 的输入、提示词、快照、报告或 Character.AI。

### 3.2 Character.AI

Character.AI 使用固定的企业同事型角色，负责回答用户消息和完成任务。目标人格包括：

- 可靠；
- 主动推进；
- 先给结论；
- 能识别风险；
- 能说明能力和信息边界；
- 给出可执行的下一步。

### 3.3 Agnes 提炼模型

提炼请求使用新的独立请求或会话，只能看到本次完整对话、当前有效产物和允许调用的管理工具。它不能读取隐藏人物设定。

Agnes 仅用于第一阶段低成本验证。Dreams 的模型后端保持可替换，正式企业部署前应使用多个候选模型比较准确率、幻觉率、结构化输出稳定性、速度和成本。

### 3.4 作用域

测试固定使用：

```text
tenant_id = dream-lab
agent_id  = enterprise-colleague
```

AI 决策产物在 `agent_id` 下共享；用户画像按 `tenant_id + agent_id + user_id` 隔离。任何用户事实都不得写入共享 AI 人格。

## 4. 总体数据流

```text
隐藏人物设定
      │
      ▼
Agnes 生成用户消息
      │
      ▼
Character.AI 完成任务 N
      │
      ▼
人工整理完整 Session 为 JSONL
      │
      ▼
事件账本去重并进入 pending
      │
      ▼
后台回顾只调用必要管理工具
      ├── 用户人物证据 ──→ USER.md 候选更新
      └── AI 决策经验 ──→ AI 决策卡候选更新
      │
      ▼
User Curator / AI Curator 周期做梦
      ├── 合并重复
      ├── 处理冲突和变化
      ├── 归档过时信息
      └── 保留来源证据
      │
      ▼
生成新快照、报告和回写文本
      │
      ▼
人工检查并回写 Character.AI
      ├── CHARACTER_DEFINITION.md → Character Definition
      └── USER_PERSONA.md → 对应用户的 User Persona
      │
      ▼
版本标记为 active
      │
      ▼
开启全新聊天执行任务 N+1
```

开启全新聊天是必要的，避免 Character.AI 仅依靠当前聊天上下文，让测试能够验证长期产物是否真正生效。

## 5. 测试输入

### 5.1 一条记录代表一个完成的任务

JSONL 每一行保存一个已正常完成的任务和完整消息历史。不能把单条消息当作独立任务，也不能只保存摘要。

```json
{
  "event_id": "evt_project_manager_001",
  "tenant_id": "dream-lab",
  "agent_id": "enterprise-colleague",
  "user_id": "project-manager",
  "session_id": "session-001",
  "task_id": "task-001",
  "completed_at": "2026-07-17T10:00:00+08:00",
  "messages": [
    {
      "role": "user",
      "content": "这个项目现在进度怎么样？先告诉我结论。"
    },
    {
      "role": "assistant",
      "content": "结论：核心功能已经完成，但接口联调还没有通过。"
    },
    {
      "role": "user",
      "content": "以后汇报时还要告诉我负责人和风险。"
    },
    {
      "role": "assistant",
      "content": "明白，后续我会同时提供结论、进度、负责人和主要风险。"
    }
  ],
  "final_response": "明白，后续我会同时提供结论、进度、负责人和主要风险。"
}
```

字段约束：

- `event_id` 是全局稳定的幂等键，重复导入不能重复学习。
- `user_id` 决定 `USER.md` 的隔离路径。
- `session_id` 标识一次聊天。
- `task_id` 标识一次完整任务。
- `completed_at` 用于增量同步和时间冲突判断。
- `messages` 必须包含完整的 user/assistant 对话。
- `final_response` 必须与本任务最后一条 assistant 消息一致。

### 5.2 建议的测试文件

```text
tests/
├── fixtures/
│   ├── ai_seed/
│   │   └── ai_seed.jsonl
│   ├── personas/
│   │   ├── project_manager.gold.json
│   │   ├── python_beginner.gold.json
│   │   └── technical_lead.gold.json
│   └── conversations/
│       ├── project_manager.jsonl
│       ├── python_beginner.jsonl
│       └── technical_lead.jsonl
└── evaluation/
```

`*.gold.json` 文件只能由模拟用户和评估程序使用，不得进入提炼请求。

## 6. AI 初始决策身份

第一次正式对话前，从以下公开数据中人工筛选约 30 条样本：

- [NVIDIA HelpSteer2](https://huggingface.co/datasets/nvidia/HelpSteer2)：选择清晰、正确、相关且有帮助的回答。
- [PKU-SafeRLHF](https://huggingface.co/datasets/PKU-Alignment/PKU-SafeRLHF)：选择同时体现帮助性和安全边界的回答。

初始样本只能调用 AI 决策卡管理工具，不能生成 `USER.md`。数据集中的虚构用户内容不能成为用户画像。

```text
人工筛选约 30 条样本
  → 提炼初始 AI 决策卡
  → AI Curator 合并整理
  → 生成第一版 CHARACTER_DEFINITION.md
  → 人工写入 Character.AI
  → 开始正式连续对话
```

初始身份建立后，后续进化主要来自实际任务经历。

## 7. 梦境产物

### 7.1 长期产物

```text
agents/<agent_id>/
├── decision-cards/                 AI 决策卡历史
├── DECISION_RULES.md               当前有效的完整 AI 决策规则
├── CHARACTER_DEFINITION.md         压缩后的 Character.AI 回写文本
└── users/<user_id>/
    ├── USER.md                     完整用户人物画像
    └── USER_PERSONA.md             压缩后的 Character.AI 回写文本
```

`DECISION_RULES.md` 和 `USER.md` 是可检查、可追溯的长期产物。`CHARACTER_DEFINITION.md` 和 `USER_PERSONA.md` 是根据目标平台限制生成的发布产物，不能替代长期产物。

### 7.2 回写规则

- `CHARACTER_DEFINITION.md` 写入 Character Definition，对同一个 Character 的所有用户生效。
- `USER_PERSONA.md` 写入对应用户的 User Persona，只对该用户生效。
- 共享 AI 人格不得包含具体用户的身份、偏好、秘密或任务内容。
- 用户画像只能包含有对话证据支持、适合影响未来服务的信息。
- 回写文本必须压缩，不能直接拼接全部历史卡片或全部历史画像。
- 最重要、最稳定的规则放在 Character Definition 前部，降低长定义被截断的影响。

## 8. 版本与发布状态

一次梦境运行依次经过：

```text
pending
  → dreaming
  → ready_for_review
  → ready_for_writeback
  → active
```

- `pending`：完成任务已进入等待队列。
- `dreaming`：正在提炼或运行 Curator。
- `ready_for_review`：新候选版本已经生成，等待检查。
- `ready_for_writeback`：检查通过，可以人工回写。
- `active`：Character Definition 与当前用户的 User Persona 均已回写，可以开始下一任务。

每个版本必须保存：

- 修改前快照；
- 修改后快照；
- 来源事件 ID；
- 变更报告；
- 回写清单；
- 激活时间；
- 可回滚的上一版本。

回写清单示例：

```json
{
  "version": 3,
  "agent_id": "enterprise-colleague",
  "user_id": "python-beginner",
  "character_definition_written": true,
  "user_persona_written": true,
  "activated_at": "2026-07-17T18:00:00+08:00"
}
```

只有两项回写状态均为 `true`，该用户的下一任务才允许使用这个版本。后台生成中的候选版本不能改变正在执行的任务。

### 8.1 下一任务启动屏障

开始任务 N+1 前，必须检查当前发布状态已经处理到任务 N 的 `event_id`：

```text
latest_completed_event_id == active.processed_through_event_id
```

如果两者不一致，说明上一任务尚未完成做梦、检查或回写，不能开始新的验证任务。系统应先处理等待事件并运行两个 Curator，再生成回写清单。

如果本次对话没有产生值得长期保留的新信息，做梦报告仍需记录该事件已处理，并把 `processed_through_event_id` 推进到最新事件；这种情况不要求重复粘贴没有变化的 Character Definition 或 User Persona。

如果做梦失败，报告记录失败事件和原因。测试人员可以选择修复后重试；只有明确执行降级并记录“继续使用上一稳定版本”，才允许开始下一任务，不能把失败误报为已经更新。

## 9. 三阶段验证

### 9.1 第一阶段：画像提炼准确性

每个用户先完成约 5 个任务：

1. Agnes 根据隐藏人物设定产生用户消息。
2. Character.AI 正常回答。
3. Dreams 只根据对话生成第一版 `USER.md`。
4. 将画像与隐藏人物设定和原始对话证据对照。
5. 通过检查后生成 `USER_PERSONA.md`。

这一阶段在评分前不回写用户画像，防止 Character.AI 提前获得隐藏答案。

### 9.2 第二阶段：画像应用与 AI 进化

1. 将 `USER_PERSONA.md` 写入对应 User Persona。
2. 将 `CHARACTER_DEFINITION.md` 写入 Character Definition。
3. 标记新版本为 `active`。
4. 开启全新聊天。
5. Agnes 提出新的、未出现过的任务。
6. 检查 Character.AI 是否表现出正确的个性化服务和目标决策方式。

### 9.3 第三阶段：变化、冲突和持续进化

再完成约 5 个任务，并让部分偏好发生明确变化。例如：

```text
旧信息：用户需要每一步 Git 操作都详细解释。
新信息：用户已经熟悉 Git，以后的 Git 操作可以简写。
```

新一轮做梦应当用明确的新证据更新旧结论，保留变更记录，并在下一任务前发布新版本。证据不足时必须标记冲突，不能擅自覆盖。

## 10. 通过标准

### 10.1 用户画像

- 至少 85% 的画像事实有明确对话证据支持。
- 严重虚构事实为 0。
- 不同用户之间的信息泄漏为 0。
- 新偏好有明确证据后能够更新旧偏好。
- 回写后，至少 80% 的新任务体现正确的个性化方式。

只评价已经在对话中实际表达的隐藏人物特征，不能要求 Dreams 猜出用户从未透露的信息。

### 10.2 AI 决策身份

- 每张决策卡都能追溯到具体事件或初始样本。
- AI 人格中的具体用户私人信息为 0。
- 重复或高度相似规则能够合并。
- 被新证据否定的旧规则能够替换或归档。
- 回写后，至少 80% 的新任务体现先给结论、主动推进、风险判断、边界说明和可执行下一步。

### 10.3 梦境机制

- 每个正常完成的复杂任务进入待做梦队列。
- 下一任务开始前存在一个完整的 `active` 版本。
- 候选版本不能改变正在执行的任务。
- 每次更新都有报告、证据、快照和版本号。
- 做梦失败或回写未完成时继续使用上一稳定版本。
- 任意已发布版本可以回滚。

### 10.4 评价方式

Agnes 不能单独评价自己提炼的结果。最终结果由以下方法共同判断：

```text
结构和隔离的程序检查
+ 隐藏人物设定对照
+ 原始对话证据检查
+ 人工检查
+ 回写前后任务表现比较
```

## 11. Agnes 接入与失败处理

Dreams 启动测试前检查 Agnes API 是否支持普通对话、结构化 JSON 和工具调用：

- 支持工具调用时，使用现有结构化管理工具。
- 不支持工具调用但支持 JSON 时，使用 Agnes 专用后端输出固定结构，再由 Dreams 严格校验。
- 不符合结构的结果不能直接写入长期产物。

以下情况禁止激活新版本：

- Agnes API 调用失败或响应格式错误；
- 用户画像混入其他用户信息；
- AI 决策卡包含用户私人信息；
- 产物缺少来源证据；
- 回写文本超过目标字段限制；
- Curator 运行中断；
- Character Definition 或 User Persona 尚未完成回写。

处理顺序：

1. 最多自动重试两次。
2. 仍然失败时记录完整报告。
3. 保留候选产物供检查，但不标记为 `active`。
4. 下一任务继续使用上一稳定版本。
5. 必要时回滚。

## 12. 完成条件

本验证在满足以下条件时完成：

1. 三个模拟用户均完成至少 10 个任务。
2. 用户画像、AI 决策卡和两类压缩回写文本均可在磁盘检查。
3. 至少完成两轮“做梦 → 回写 → 新聊天验证”。
4. 用户画像和 AI 决策身份达到本设计的通过标准。
5. 可以展示一次信息变化被新证据更新的完整报告。
6. 可以展示一次失败后继续使用上一稳定版本或成功回滚。
