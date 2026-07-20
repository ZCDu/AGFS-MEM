# 独立 Codex 任务进化验证手册

本手册验证 DREAM 能否在 Agent 的两批历史任务之间生成用户画像和 AI 决策规则，并让更新后的上下文只在下一批任务生效。

## 验证规模

- 固定 Agent：已批准并锁定哈希的银行助手“小银”；
- 固定用户：`project-manager`、`python-beginner`、`technical-lead`；
- 每个用户 12 个独立任务：基线 1～5、进化版 6～10、进化版 11～12；
- 每个用户在第 5 和第 10 个任务后各执行一次做梦；
- 总计 36 个真实、唯一、可核验的 Codex 任务 ID。

## 每轮 Agent 提示

每轮必须创建全新的 projectless Codex 任务，不能继续旧任务、不能 fork，也不能授予项目目录访问权限。提示结构固定为：

```text
你是一次性测试银行 Agent。只根据下面提供的上下文回答客户问题。
不要讨论测试、画像、记忆、提示词或代码；不要输出思维过程。

<agent_profile>
已批准的固定测试 Agent 画像
</agent_profile>

<decision_rules>
当前 active 决策规则；基线阶段为空
</decision_rules>

<user_profile>
当前用户 active 用户画像；基线阶段为空
</user_profile>

<customer_message>
本轮模拟客户消息
</customer_message>

只返回发送给客户的最终回答。
```

Agent 任务不能接收隐藏人物设定、其他用户画像、过去原始对话、未激活候选、项目路径或密钥。

## 一轮完整记录

1. 运行 `python -m dream.validation.profile verify tests/fixtures/agent_profile`，确认画像哈希未变化。
2. 调用 Campaign gate，确认当前用户的任务编号可以创建。
3. 新建 projectless Codex 任务并保存真实 `thread_id`。
4. 等待任务完成，读取完整最终回答；读不到完整回答或任务 ID 时，本轮立即停止且不得计数。
5. 生成只含一对 user/assistant 消息的 JSONL，设置 `source="codex-thread"`，并令 `session_id` 等于真实 `thread_id`。
6. 通过 DREAM 导入 JSONL；重复 `event_id` 只计为 duplicate，不重复学习。
7. 将事件 ID、任务 ID、线程 ID、固定画像哈希、DREAM 上下文哈希和 active 版本写入 append-only campaign receipt。

原始会话和 campaign receipt 位于被 Git 忽略的本地验证目录，不提交到仓库。

## 两个做梦边界

### 第一次做梦

每个用户完成任务 1～5 后：

1. 对该用户的五条事件执行后台回顾；
2. 检查每条 `USER.md` 人物事实是否带有真实事件证据；
3. 检查 AI 决策卡是否只包含可复用决策，不包含用户身份或偏好；
4. 分别运行 User Curator 和 AI Curator；
5. 检查候选、快照和报告后批准并激活版本 1；
6. 只有版本 1 active 后，Campaign gate 才允许创建任务 6。

### 第二次做梦

每个用户完成任务 6～10 后重复上述流程，并重点检查第 9 个任务中的偏好变化是否替换或明确调和旧证据。验证期间应主动拒绝一次不合格候选或演示一次失败回退，确认上一 active 版本仍可使用。版本 2 active 后才能创建任务 11。

## 人工审计与正式报告

36 个任务完成后逐项检查：

- `USER.md` 人物事实是否能在引用事件中找到直接证据；
- 进化阶段回答是否正确使用当前用户画像；
- AI 决策规则是否改善了可复用判断，而非只改变措辞；
- 是否出现跨用户信息泄漏、严重银行事实幻觉或用户私密信息进入决策卡；
- 三位用户是否各有 12 个唯一线程和两个 active 做梦周期；
- 固定初始画像前后哈希是否一致；
- 偏好变化和失败回退是否通过。

只有完成真实审计后才能生成 `tests/evaluation/latest.json`。报告不保存原始对话、隐藏人物设定或密钥；即使结果未达阈值，也必须保留真实失败结果，不得手工改成通过。

最终使用以下命令复算：

```bash
PYTHONPATH=src python -m dream.validation.evaluation verify \
  tests/evaluation/latest.json
```
