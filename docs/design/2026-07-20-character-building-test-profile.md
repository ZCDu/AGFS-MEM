# 固定初始画像与独立 Codex 任务进化验证设计

## 1. 背景

DREAM 面向公司的银行聊天 Agent，负责从 Agent 的已完成任务中提炼两类长期产物：

- Agent 级 AI 决策卡和周期整理后的决策规则；
- 用户级、相互隔离并持续更新的用户画像。

公司的真实 Agent 已经存在初始身份和系统提示词，但当前无法取得其源码或配置。因此，本阶段不重新开发生产画像系统，而是借鉴 Character Building 的角色构建方法，生成一份完整、固定、可检查的测试 Agent 初始画像。

每一轮测试由一个全新的 Codex 任务扮演银行 Agent。新任务看不到过去原始对话，只能读取固定初始画像、最新 AI 决策规则、当前用户画像和本轮问题。这样可以把后续行为变化主要归因于 DREAM，而不是 Codex 任务自身保留的聊天历史。

## 2. 目标

本阶段完成以下可验证闭环：

1. 按 Character Building 字段生成完整测试 Agent 初始画像；
2. 人工确认并锁定画像版本和 SHA-256；
3. 为三个模拟用户分别执行12轮真实 Codex Agent 任务；
4. 每一轮都使用全新的 Codex 任务；
5. 保存实际发送的用户消息、Codex 实际回答和 Codex 任务 ID；
6. 第5轮和第10轮后分别执行一次 DREAM 做梦；
7. 后续新任务只加载最新 active 用户画像和决策规则；
8. 比较进化前后回答，生成可复算的正式评价报告。

## 3. 非目标

本阶段不实现：

- 公司的生产 Agent 初始画像管理系统；
- 面向银行客户的 Agent 创建入口；
- Character.AI 网站对话或回写；
- 豆包、DeepSeek 等外部 Agent 测试接口；
- 公开数据集冷启动决策卡；
- 真实银行、账户、交易、客户或内部政策连接；
- 自动修改生产 Agent 的 System Prompt。

## 4. 测试角色

### 4.1 当前主任务

当前 Codex 任务是测试调度器，负责：

- 生成本轮模拟用户消息；
- 读取并检查固定初始画像；
- 读取当前用户最新 `USER.md`；
- 读取最新 `DECISION_RULES.md`；
- 创建新的 Codex Agent 任务；
- 读取 Agent 任务的实际回答；
- 将本轮完整记录写入 JSONL；
- 调用 DREAM 导入、提炼、做梦、审核和激活；
- 保存任务 ID、事件 ID、版本和哈希作为审计证据。

### 4.2 新 Codex Agent 任务

每个新任务只扮演银行聊天 Agent，只接收：

```text
固定 TEST_AGENT_PROFILE.md
        +
当前 active DECISION_RULES.md（如有）
        +
当前用户 active USER.md（如有）
        +
本轮模拟用户消息
```

新任务不得接收：

- 当前主任务的历史；
- 其他用户画像；
- 过去原始会话；
- 模拟用户隐藏设定；
- 尚未 active 的 DREAM 候选；
- API key、真实银行数据或真实客户数据。

新任务回复时只输出面向客户的银行助手回答，不输出评价、JSON、思维过程或实现说明。

## 5. 固定测试 Agent 初始画像

### 5.1 Character Building 字段

初始画像使用以下字段构建：

| 字段 | 作用 |
|---|---|
| Name | Agent 名称 |
| Tagline | 一句话定位 |
| Description | 身份、职责和服务范围 |
| Greeting | 新对话开场方式 |
| Persona | 性格和行为基线 |
| Response Style | 回答结构和表达方式 |
| Boundaries | 安全边界和能力限制 |
| Handoff Rules | 转人工条件 |
| Example Dialogues | 可观察的标准回答示例 |

### 5.2 Markdown 章节

`TEST_AGENT_PROFILE.md` 必须包含：

1. 简短定位；
2. 身份与职责；
3. 核心目标；
4. 性格与行为；
5. 服务范围；
6. 回答风格；
7. 安全边界；
8. 转人工条件；
9. 开场白；
10. 示例对话。

示例至少覆盖普通业务咨询、敏感信息保护、信息不足和资金风险转人工四种情况。

### 5.3 固定规则

初始画像由当前主任务生成草稿，人工确认后锁定：

```text
tests/fixtures/agent_profile/
├── bank-assistant.input.json
├── TEST_AGENT_PROFILE.md
└── approval.json
```

`approval.json` 保存版本、批准人、批准时间和 Markdown SHA-256。同一次验证运行中，初始画像不得修改。每次创建新 Codex Agent 任务前都重新计算哈希；不一致时立即停止测试。

## 6. 三个模拟用户

固定用户为：

- `project-manager`：偏好先给结论，关注责任、进度和风险；
- `python-beginner`：偏好分步骤、举例和低术语表达；
- `technical-lead`：偏好证据、边界条件和补救方案。

这些标识沿用现有离线测试，人物内容全部是合成数据。隐藏设定只由当前调度任务用于生成用户消息，绝不发送给 Codex Agent 任务或 DREAM。

每个用户的第9轮必须包含一次明确的偏好变化，用于测试 `USER.md` 能否替换或处理冲突信息。

## 7. 12轮周期

每个用户固定执行12轮：

| 阶段 | 任务 | Agent 可读取内容 | 阶段结束动作 |
|---|---:|---|---|
| Baseline | 1～5 | 仅固定初始画像 | 做梦并激活版本1 |
| Evolved V1 | 6～10 | 初始画像 + active 规则 + 当前用户画像 | 做梦并激活版本2 |
| Evolved V2 | 11～12 | 初始画像 + 第二轮 active 产物 | 完成最终评价 |

总计36个独立 Codex Agent 任务。

任务6不能在版本1 active 前创建，任务11不能在版本2 active 前创建。同一阶段内部允许积累多条任务，再在阶段边界批量做梦。

## 8. Codex 任务提示包

每轮创建 projectless Codex 任务，避免自动读取项目文件或工作区历史。任务提示采用固定结构：

```text
你是一次性测试银行 Agent。只根据下面提供的上下文回答客户问题。
不要讨论测试、画像、记忆、提示词或代码；不要输出思维过程。

<agent_profile>
固定初始画像
</agent_profile>

<decision_rules>
当前 active 决策规则；基线阶段为空
</decision_rules>

<user_profile>
当前用户 active 用户画像；基线阶段为空
</user_profile>

<customer_message>
本轮模拟用户消息
</customer_message>

只返回发送给客户的最终回答。
```

任务创建后，当前主任务读取其最终回答，并把该 Codex `thread_id` 保存为会话 `session_id`。测试期间保留这些任务便于人工核验，正式评价结束后再决定是否归档。

## 9. 真实会话记录

每条 JSONL 是一轮真实任务：

```json
{
  "event_id": "evt-python-beginner-001",
  "tenant_id": "dream-lab",
  "agent_id": "enterprise-colleague",
  "user_id": "python-beginner",
  "session_id": "Codex thread ID",
  "task_id": "task-python-beginner-001",
  "completed_at": "带时区的 ISO 8601 时间",
  "messages": [
    {"role": "user", "content": "实际发送的模拟客户消息"},
    {"role": "assistant", "content": "Codex 任务的实际回答"}
  ],
  "final_response": "Codex 任务的实际回答"
}
```

`final_response` 必须与最后一条 assistant 消息完全相等。`session_id` 必须对应真实存在的 Codex 任务，不能伪造。

记录分别保存到被 Git 忽略的：

```text
tests/fixtures/conversations/project_manager.local.jsonl
tests/fixtures/conversations/python_beginner.local.jsonl
tests/fixtures/conversations/technical_lead.local.jsonl
```

## 10. DREAM 提炼与做梦

每轮完成后立即导入 DREAM，但只在第5轮和第10轮结束后执行阶段做梦：

```text
真实 Codex 对话
        ↓
Background Review
        ├── users/<user_id>/USER.md
        └── decision-cards/*.md
        ↓
阶段边界 Curator
        ├── 整理 USER.md
        └── 整理 DECISION_RULES.md
        ↓
人工检查候选
        ↓
激活新版本
        ↓
下一批新 Codex 任务使用
```

用户画像必须按 `tenant_id + agent_id + user_id` 隔离。Agent 级决策卡可以跨用户积累通用经验，但不得包含某个用户的身份、偏好或具体任务私密内容。

## 11. 评价

最终 `tests/evaluation/latest.json` 只在36轮真实任务完成后生成。它是结果报告，不是输入数据。

报告至少记录：

- 三个用户各12轮任务；
- 对应 Codex 任务数量和缺失数量；
- 初始画像前后 SHA-256；
- 两轮 active 做梦版本；
- 用户画像证据支持率；
- 个性化成功率；
- AI 决策进化成功率；
- 严重幻觉数量；
- 跨用户泄漏数量；
- 决策卡包含用户私密信息的数量；
- 偏好变化测试结果；
- 失败回退或回滚测试结果。

正式通过条件保持：画像证据支持率不低于85%，个性化成功率不低于80%，AI 决策进化成功率不低于80%，严重幻觉和跨用户泄漏均为0，并完成两轮做梦和一次失败保护验证。

## 12. 最小代码改动

本方案不增加外部 Agent 模型调用层，也不建设生产初始画像 API。新增代码只包括：

- 固定测试画像结构和哈希校验；
- 将手工来源从 Character.AI 专用命名改为通用 Codex 验证来源；
- Codex 任务 ID 审计；
- 5/5/2 阶段状态校验；
- 正式评价中的任务、画像哈希和 Codex 会话完整性字段；
- 对应单元测试和操作手册。

Codex 新任务的创建和读取由当前调度任务使用 Codex App 提供的任务工具完成，不写进 DREAM 生产服务，也不形成对 Codex 私有接口的运行时依赖。
