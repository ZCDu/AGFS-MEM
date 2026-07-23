# Character.AI 闭环进化验证手册

本手册用于验证两个结果：DREAM 能否从完整任务中形成持续进化的 `USER.md` 用户人物画像；能否把 AI 的判断经验提炼为决策卡，并通过 `CHARACTER_DEFINITION.md` 让下一轮 Character.AI 对话体现更稳定的拟人决策方式。

Character.AI 在本阶段没有可依赖的正式写入 API。因此 DREAM 自动生成和检查候选文本，测试人员人工粘贴，随后通过 API 确认回写并激活版本。

## 1. 安全边界

- 只使用合成用户，不使用真实员工信息。
- `fixtures/personas/*.gold.json` 只交给 Agnes 模拟用户和最终评估程序。
- 隐藏人物设定不得上传到 DREAM，也不得粘贴到 Character.AI。
- DREAM 只接收 Character.AI 已经完成的公开 user/assistant 对话。
- Agnes 模拟用户和 Agnes 提炼模型必须使用新的独立请求，不能共享上下文。
- `USER.md` 属于单个 `user_id`；AI 决策卡不得包含某个用户的私人事实。
- `.env`、API key 和本地选择的数据集行不得提交 GitHub。

## 2. 准备服务

在 DREAM 项目目录执行：

```bash
source /Users/fenghao/PycharmProjects/dream/.venv/bin/activate
cp .env.example .env
```

第一阶段暂时保持：

```dotenv
DREAM_VALIDATION_REQUIRE_ACTIVE_WRITEBACK=false
```

配置 OpenAI-compatible 提炼模型后启动服务：

```bash
export DREAM_ENV_FILE=.env
uvicorn dream.api:app --host 127.0.0.1 --port 8765
```

后续命令默认使用：

```bash
BASE_URL=http://127.0.0.1:8765
```

## 3. 建立 AI 初始决策身份

从 HelpSteer2 人工选择 20 条清晰、正确、相关且有帮助的回答，从 PKU-SafeRLHF 人工选择 10 条既有帮助又明确说明安全边界的回答。每行只保留：

```json
{"source_dataset":"nvidia/HelpSteer2","source_record_id":"train:123","scenario":"公开问题","assistant_response":"公开回答"}
```

保存到已被 Git 忽略的：

```text
fixtures/ai_seed/selected.local.jsonl
```

严格检查数量、唯一 ID 和字段白名单：

```bash
PYTHONPATH=src python -m dream.validation.seeds validate \
  fixtures/ai_seed/selected.local.jsonl \
  --expected-count 30
```

在 Python 控制台中复用当前配置完成 AI-only 初始化：

```python
from pathlib import Path
from dream.config import build_curator_backend, build_review_backend, build_writeback_backend, load_settings
from dream.curators.ai import AICurator
from dream.scope import resolve_scope
from dream.service import DreamService
from dream.validation.seeds import seed_scope
from dream.writeback import WritebackService

settings = load_settings(Path(".env"))
home = Path(settings.home).expanduser()
service = DreamService(
    home,
    backend=build_review_backend(settings),
    semantic_curator_backend=build_curator_backend(settings),
)
text = Path("fixtures/ai_seed/selected.local.jsonl").read_text(encoding="utf-8")
print(service.import_ai_seed_jsonl(text))
print(service.run_pending(seed_scope()))
paths = resolve_scope(home, seed_scope())
print(AICurator(paths, semantic_backend=service.semantic_curator_backend).run())
print(WritebackService(
    paths,
    backend=build_writeback_backend(settings),
    character_limit=settings.character_definition_limit,
).generate_character())
```

检查 `CHARACTER_DEFINITION.md` 不含任何具体用户信息，然后人工粘贴到同一个 Character 的 Character Definition。种子流程不得生成 `USER.md` 或 `USER_PERSONA.md`。

## 4. 采集第一阶段盲测会话

三个测试用户固定为：

- `project-manager`
- `python-beginner`
- `technical-lead`

每次只把对应的 `*.gold.json` 交给 Agnes，让 Agnes 返回一条自然的用户消息。把这条消息发送给 Character.AI，等待完整回答，然后手工整理成一行 JSONL。`final_response` 必须与最后一条 assistant 消息完全一致。

文件建议使用已被 Git 忽略的：

```text
fixtures/conversations/project_manager.local.jsonl
fixtures/conversations/python_beginner.local.jsonl
fixtures/conversations/technical_lead.local.jsonl
```

先为每个用户收集 5 个完整任务。不得提前把 `USER_PERSONA.md` 写回 Character.AI，否则第一阶段会泄漏隐藏答案。

## 5. 导入、做梦和人工检查

以下以 `project-manager` 为例。导入完整 NDJSON：

```bash
curl --fail-with-body -X POST "$BASE_URL/v1/validation/import" \
  -H 'Content-Type: application/x-ndjson' \
  --data-binary @fixtures/conversations/project_manager.local.jsonl
```

运行该用户的闭环做梦：

```bash
curl --fail-with-body -X POST "$BASE_URL/v1/validation/dream" \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"dream-lab","agent_id":"enterprise-colleague","user_id":"project-manager"}'
```

检查磁盘上的：

```text
<DREAM_HOME>/tenants/dream-lab/agents/enterprise-colleague/
├── decision-cards/
├── DECISION_RULES.md
├── CHARACTER_DEFINITION.md
└── users/project-manager/
    ├── USER.md
    └── USER_PERSONA.md
```

人工确认 `USER.md` 的每项事实都能指向导入的 `event_id`，决策卡没有具体用户私人信息。设候选版本号为 `1`，批准候选：

```bash
curl --fail-with-body -X POST "$BASE_URL/v1/validation/publications/1/approve" \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"dream-lab","agent_id":"enterprise-colleague","user_id":"project-manager"}'
```

如果检查不通过，拒绝并恢复做梦前状态：

```bash
curl --fail-with-body -X POST "$BASE_URL/v1/validation/publications/1/reject" \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"dream-lab","agent_id":"enterprise-colleague","user_id":"project-manager"}'
```

## 6. 人工回写、确认和激活

把 `CHARACTER_DEFINITION.md` 粘贴到 Character Definition，把当前用户的 `USER_PERSONA.md` 粘贴到该用户的 User Persona。粘贴成功后才能确认：

```bash
curl --fail-with-body -X POST "$BASE_URL/v1/validation/publications/1/confirm-writeback" \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"dream-lab","agent_id":"enterprise-colleague","user_id":"project-manager","character_definition_written":true,"user_persona_written":true}'
```

激活版本：

```bash
curl --fail-with-body -X POST "$BASE_URL/v1/validation/publications/1/activate" \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"dream-lab","agent_id":"enterprise-colleague","user_id":"project-manager"}'
```

查看最新和当前 active 版本：

```bash
curl --fail-with-body -G "$BASE_URL/v1/validation/publications/status" \
  --data-urlencode 'tenant_id=dream-lab' \
  --data-urlencode 'agent_id=enterprise-colleague' \
  --data-urlencode 'user_id=project-manager'
```

三个用户的第一版都完成审核和激活后，把 `.env` 改为：

```dotenv
DREAM_VALIDATION_REQUIRE_ACTIVE_WRITEBACK=true
```

重启 FastAPI，配置才会生效。

## 7. 下一任务启动屏障

每次开始新任务前调用：

```bash
curl --fail-with-body -X POST "$BASE_URL/v1/tasks/start" \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"dream-lab","agent_id":"enterprise-colleague","user_id":"project-manager"}'
```

如果上一任务尚未完成做梦和回写，接口返回 HTTP 409，并给出最新事件、active 已处理事件和下一步。只有返回新 `snapshot_id` 后，才能打开全新的 Character.AI 聊天开始下一任务。

后续每个任务都执行：新聊天 → Agnes 生成一条用户消息 → Character.AI 回答 → 追加一行 JSONL → 导入 → 做梦 → 审核 → 回写 → 激活 → 下一任务。

每个用户最终完成 10～15 个任务，至少激活两轮完整做梦。第三阶段必须包含一次明确偏好变化，例如“以前需要详细 Git 步骤，现在 Git 命令可以简写”。

## 8. 失败保护和回滚

做梦失败返回 HTTP 503，系统恢复候选生成前快照，`active.json` 仍指向上一稳定版本。修复模型或配置后重新运行 `/v1/validation/dream`。

要恢复一个曾经激活的版本，例如版本 1：

```bash
curl --fail-with-body -X POST "$BASE_URL/v1/validation/publications/1/rollback" \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"dream-lab","agent_id":"enterprise-colleague","user_id":"project-manager"}'
```

回滚不会删除后续版本记录。测试必须演示一次失败回退或一次回滚。

## 9. 生成和验证评估报告

人工统计每个用户的任务数、画像事实数、证据支持事实数、个性化成功任务数；再填写 AI 拟人行为成功数、严重幻觉、跨用户泄漏、来源缺失和发布状态。使用 `EvaluationReport` 保存报告到：

```text
tests/e2e/evaluation/latest.json
```

先把原始计数保存为已被 Git 忽略的 `tests/e2e/evaluation/run-input.local.json`：

```json
{
  "users": [
    {"user_id":"project-manager","task_count":10,"supported_profile_facts":17,"total_profile_facts":20,"personalized_successes":8,"personalized_tasks":10},
    {"user_id":"python-beginner","task_count":10,"supported_profile_facts":17,"total_profile_facts":20,"personalized_successes":8,"personalized_tasks":10},
    {"user_id":"technical-lead","task_count":10,"supported_profile_facts":17,"total_profile_facts":20,"personalized_successes":8,"personalized_tasks":10}
  ],
  "severe_hallucinations": 0,
  "cross_user_leaks": 0,
  "evolved_ai_successes": 8,
  "evolved_ai_tasks": 10,
  "completed_dream_writeback_cycles": 2,
  "change_conflict_case_passed": true,
  "failure_fallback_or_rollback_passed": true,
  "missing_source_event_ids": 0,
  "incomplete_writebacks": 0,
  "inactive_publications": 0,
  "decision_cards_with_private_user_data": 0,
  "agnes_advisory": "只能作为参考，不能决定通过"
}
```

这些数字是格式示例，必须替换为实际人工检查和磁盘审计结果。生成包含可复算指标的报告：

```python
from pathlib import Path
from dream.validation.evaluation import ValidationRunInput, evaluate_validation_run

source = Path("tests/e2e/evaluation/run-input.local.json")
target = Path("tests/e2e/evaluation/latest.json")
run = ValidationRunInput.model_validate_json(source.read_text(encoding="utf-8"))
report = evaluate_validation_run(run)
target.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
print(target, report.passed, report.failure_reasons)
```

报告只允许包含合成用户 ID 和汇总计数，不得包含隐藏人物全文、完整会话或 API key。来源事件和产物哈希应在磁盘审计记录中检查，最终只把缺失数量写入报告。

验证报告没有被篡改，并重新计算通过标准：

```bash
PYTHONPATH=src python -m dream.validation.evaluation verify \
  tests/e2e/evaluation/latest.json
```

正式通过必须同时满足：

- 每个用户至少 10 个完整任务；
- 画像证据支持率不低于 85%；
- 回写后的个性化成功率不低于 80%；
- AI 拟人决策行为成功率不低于 80%；
- 严重幻觉和跨用户泄漏均为 0；
- 至少两轮完整做梦和回写；
- 偏好变化测试通过；
- 失败回退或回滚测试通过；
- 来源、回写和 active 发布记录完整。

Agnes 的自评只能写入 `agnes_advisory` 作为参考，不能改变最终 `passed`。

## 10. 自动化检查

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m pytest -q -p no:cacheprovider
python -m ruff check src tests
python -m compileall -q src/dream
```

自动化测试只证明代码流程、隔离和失败保护可运行，不能替代 3 个用户各 10～15 个真实连续任务的人工效果验证。
