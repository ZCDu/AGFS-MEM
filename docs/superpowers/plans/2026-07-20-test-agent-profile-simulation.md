# Test Agent Profile and Evolution Simulation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate and lock a Character Building-style synthetic bank Agent profile, simulate three isolated users talking to that Agent, periodically distill the completed tasks into user profiles and AI decision rules, and measure whether loading those artifacts improves later tasks.

**Architecture:** Keep the existing ledger, Background Review, Curators, publication state, snapshots, and evaluation code as the memory core. Add a validation-only profile generator and approval store, a replaceable OpenAI-compatible test Agent backend, a synthetic conversation runner that never persists hidden personas, and a campaign coordinator that runs fixed baseline/evolved phases while recording the profile hash. The generated profile is a test fixture, not a production profile service and not a replacement for the bank Agent's existing System Prompt.

**Tech Stack:** Python 3.11-3.13, Pydantic 2, OpenAI-compatible chat completions, Markdown/JSON/JSONL, pytest 9, pytest-asyncio, Ruff.

## Global Constraints

- Do not add a production-facing Agent profile API or customer-facing Agent creator.
- The test profile belongs to the logical Agent, not to the external model provider.
- Generate the profile from structured synthetic inputs only; never use real bank names, customer data, account data, internal policy, or credentials.
- A generated draft cannot be used by the test Agent until a human approval record locks its SHA-256.
- The approved profile hash must remain identical before and after every evolution phase in one campaign.
- Only `decision-cards/*.md`, `DECISION_RULES.md`, `users/<user_id>/USER.md`, and bounded publication artifacts may evolve.
- Hidden persona files may be read only by the simulator and evaluator; they must never enter DREAM events, reports, snapshots, decision cards, profiles, or Agent prompts.
- Use the existing fixed scope `tenant_id=dream-lab`, `agent_id=enterprise-colleague`; users remain `project-manager`, `python-beginner`, and `technical-lead`.
- Use the same external Agent model, endpoint, temperature (`0`), and token limit for baseline and evolved comparisons.
- Each completed simulated task contains exactly one synthetic user message and one Agent response; `final_response` equals the assistant message.
- Collect 12 tasks per user: tasks 1-5 baseline, dream cycle 1, tasks 6-10 evolved, dream cycle 2, tasks 11-12 evolved again.
- Keep the previously required 30 manually selected public AI seed records: 20 HelpSteer2 and 10 PKU-SafeRLHF, each with a stable source row ID.
- Never fabricate public rows, simulated conversations, human scores, successful writebacks, or `tests/evaluation/latest.json`.
- Follow red-green-refactor and commit each independently testable task.

---

## File Map

- `src/dream/validation/profile.py`: strict profile seed, draft, approval, Markdown validation, and disk store.
- `src/dream/validation/profile_backend.py`: deterministic and OpenAI-compatible profile draft generation.
- `src/dream/validation/profile_prompts.py`: isolated Character Building-style profile generation prompt and tool schema.
- `src/dream/validation/agent.py`: fixed-profile test Agent context composition and external model call.
- `src/dream/validation/simulation.py`: one-task synthetic user/Agent runner and append-only local conversation store.
- `src/dream/validation/campaign.py`: fixed 5/5/2 phase boundaries and periodic dream activation.
- `src/dream/validation/evaluation.py`: profile-hash and model-identity acceptance fields.
- `src/dream/config.py`: validation-only provider settings and builders.
- `tests/fixtures/agent_profile/`: synthetic profile seed and approved offline sample.
- `tests/fixtures/personas/`: synthetic hidden personas used only by simulator/evaluator.
- `docs/validation/test-agent-evolution-runbook.md`: exact operational procedure.

### Task 1: Strict Character Building profile contract

**Files:**
- Create: `src/dream/validation/profile.py`
- Create: `tests/validation/test_profile.py`

**Interfaces:**
- Consumes: synthetic profile input dictionaries and generated Markdown.
- Produces: `AgentProfileSeed`, `ProfileDraft`, `ProfileApproval`, `ProfileValidationError`, `validate_profile_markdown(seed, markdown) -> None`, and `sha256_text(text) -> str`.

- [ ] **Step 1: Write failing model and validation tests**

Add tests that define the exact required fields, reject extras and real-looking account data, and require all ten Markdown sections:

```python
from dream.validation.profile import (
    AgentProfileSeed,
    ProfileValidationError,
    sha256_text,
    validate_profile_markdown,
)


def valid_seed() -> AgentProfileSeed:
    return AgentProfileSeed(
        name="小银",
        tagline="可靠、清晰的银行业务智能助手",
        role="银行智能客服",
        service_scope=("银行卡常见问题", "转账流程说明"),
        personality=("专业", "耐心", "谨慎", "有边界感"),
        response_style=("先回答核心问题", "再给操作步骤"),
        greeting="您好，我是小银，请问您需要了解什么银行业务？",
    )


def test_profile_seed_forbids_unknown_and_sensitive_fields() -> None:
    with pytest.raises(ValidationError):
        AgentProfileSeed.model_validate({
            **valid_seed().model_dump(),
            "real_account_number": "6222021234567890",
        })


def test_profile_markdown_requires_complete_character_building_sections() -> None:
    with pytest.raises(ProfileValidationError, match="示例对话"):
        validate_profile_markdown(valid_seed(), "# 小银\n\n## 身份与职责\n银行客服\n")


def test_profile_hash_is_stable() -> None:
    assert sha256_text("same\n") == sha256_text("same\n")
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/validation/test_profile.py
```

Expected: collection fails because `dream.validation.profile` does not exist.

- [ ] **Step 3: Implement strict frozen Pydantic models**

Implement these public models and exact status values:

```python
class AgentProfileSeed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=2, max_length=40)
    tagline: str = Field(min_length=4, max_length=120)
    role: str = Field(min_length=2, max_length=80)
    service_scope: tuple[str, ...] = Field(min_length=1, max_length=12)
    personality: tuple[str, ...] = Field(min_length=2, max_length=12)
    response_style: tuple[str, ...] = Field(min_length=1, max_length=12)
    greeting: str = Field(default="", max_length=500)


class ProfileStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"


class ProfileDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["draft"] = "draft"
    markdown: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProfileApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["approved"] = "approved"
    version: int = Field(ge=1)
    approver: str = Field(min_length=1, max_length=80)
    approved_at: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
```

Normalize every tuple item by stripping whitespace, reject blanks and case-folded duplicates, and reject NUL characters. The seed input must contain only the seven declared fields.

- [ ] **Step 4: Implement deterministic Markdown validation**

Require these exact headings:

```python
REQUIRED_HEADINGS = (
    "## 简短定位",
    "## 身份与职责",
    "## 核心目标",
    "## 性格与行为",
    "## 服务范围",
    "## 回答风格",
    "## 安全边界",
    "## 转人工条件",
    "## 开场白",
    "## 示例对话",
)
```

`validate_profile_markdown` must reject blank/NUL content, text over 12,000 characters, a title other than `# {seed.name}`, a missing heading, fewer than four `### 示例` headings, a missing seed service-scope item, and any 12-19 digit sequence. Require the safety section to contain the normalized concepts `验证码`, `密码`, `银行卡号`, `真实交易`, `贷款`, and `收益`. Error messages identify only the failed rule and never echo the input text.

- [ ] **Step 5: Run the test and verify GREEN**

Run the same command from Step 2.

Expected: all profile contract tests pass.

- [ ] **Step 6: Commit the profile contract**

```bash
git add src/dream/validation/profile.py tests/validation/test_profile.py
git commit -m "feat: validate synthetic agent profiles"
```

### Task 2: Structured profile draft generation

**Files:**
- Create: `src/dream/validation/profile_prompts.py`
- Create: `src/dream/validation/profile_backend.py`
- Create: `tests/validation/test_profile_backend.py`

**Interfaces:**
- Consumes: `AgentProfileSeed` and an OpenAI-compatible client.
- Produces: `ProfileDraftBackend.generate(seed) -> str`, `DeterministicProfileDraftBackend`, and `OpenAIProfileDraftBackend`.

- [ ] **Step 1: Write failing backend isolation and structure tests**

Use a recording completion client and assert that the backend sends only the seed fields, forces `generate_agent_profile`, and rejects malformed output:

```python
def test_openai_profile_backend_sends_only_structured_seed() -> None:
    completions = RecordingCompletions(markdown=complete_markdown())
    backend = OpenAIProfileDraftBackend(
        client=client_for(completions),
        model="test-model",
        structured_mode="tools",
    )

    result = backend.generate(valid_seed())

    assert result == complete_markdown()
    payload = completions.calls[0]["messages"][1]["content"]
    assert "real customer" not in payload
    assert set(json.loads(payload)) == set(valid_seed().model_dump())


def test_profile_backend_rejects_unvalidated_markdown() -> None:
    backend = OpenAIProfileDraftBackend(
        client=client_returning("# incomplete"),
        model="test-model",
        structured_mode="tools",
    )
    with pytest.raises(ProfileGenerationError, match="profile generation failed"):
        backend.generate(valid_seed())
```

- [ ] **Step 2: Run focused tests and verify RED**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/validation/test_profile_backend.py
```

Expected: collection fails because the profile backend modules do not exist.

- [ ] **Step 3: Define the tool schema and fixed generation prompt**

`PROFILE_GENERATION_TOOL` must expose exactly one argument:

```python
PROFILE_GENERATION_TOOL = {
    "type": "function",
    "function": {
        "name": "generate_agent_profile",
        "description": "Return a synthetic test Agent profile in Markdown.",
        "parameters": {
            "type": "object",
            "properties": {"markdown": {"type": "string"}},
            "required": ["markdown"],
            "additionalProperties": False,
        },
    },
}
```

The system prompt must require all ten headings, four example categories, no real bank facts, no real user data, no credentials, no account access, and no chain-of-thought. It must say the returned document remains a draft until human approval.

- [ ] **Step 4: Implement both backends**

Use the existing `StructuredCompletionClient` with three total attempts, `temperature=0`, and safe errors:

```python
class ProfileDraftBackend(Protocol):
    def generate(self, seed: AgentProfileSeed) -> str: ...


class OpenAIProfileDraftBackend:
    def generate(self, seed: AgentProfileSeed) -> str:
        calls = self.structured.call(
            system=PROFILE_GENERATION_PROMPT,
            content=json.dumps(seed.model_dump(), ensure_ascii=False, sort_keys=True),
            tools=(PROFILE_GENERATION_TOOL,),
            forced_tool="generate_agent_profile",
            mode=self.structured_mode,
        )
        markdown = calls[0].arguments.get("markdown")
        if not isinstance(markdown, str):
            raise ProfileGenerationError("profile generation failed")
        validate_profile_markdown(seed, markdown)
        return markdown.strip() + "\n"
```

`DeterministicProfileDraftBackend` renders the same required sections from the seed and inserts a fixed synthetic safety section plus four fixed scenario examples. It exists for offline tests, not for the final qualitative evaluation.

- [ ] **Step 5: Run backend tests and verify GREEN**

Run the same command from Step 2.

Expected: all profile backend tests pass in deterministic, tool, JSON, retry, and malformed-output cases.

- [ ] **Step 6: Commit profile generation**

```bash
git add src/dream/validation/profile_prompts.py src/dream/validation/profile_backend.py tests/validation/test_profile_backend.py
git commit -m "feat: generate Character Building test profiles"
```

### Task 3: Draft approval, hash locking, configuration, and CLI

**Files:**
- Modify: `src/dream/validation/profile.py`
- Modify: `src/dream/config.py`
- Modify: `.env.example`
- Create: `tests/validation/test_profile_store.py`
- Modify: `tests/test_config.py`
- Create: `tests/fixtures/agent_profile/bank-assistant.input.json`
- Create: `tests/fixtures/agent_profile/bank-assistant.approved.md`
- Create: `tests/fixtures/agent_profile/bank-assistant.approval.json`

**Interfaces:**
- Consumes: `AgentProfileSeed`, `ProfileDraftBackend`, and a validation-run root directory.
- Produces: `AgentProfileStore.generate(seed, backend)`, `approve(expected_sha256, approver)`, `load_approved()`, `verify_locked()`, and `python -m dream.validation.profile` commands.

- [ ] **Step 1: Write failing approval and immutability tests**

```python
def test_draft_cannot_be_loaded_before_human_approval(tmp_path: Path) -> None:
    store = AgentProfileStore(tmp_path)
    draft = store.generate(valid_seed(), DeterministicProfileDraftBackend())
    assert draft.status == "draft"
    with pytest.raises(ProfileApprovalError, match="not approved"):
        store.load_approved()


def test_approval_locks_exact_profile_hash(tmp_path: Path) -> None:
    store = AgentProfileStore(tmp_path)
    draft = store.generate(valid_seed(), DeterministicProfileDraftBackend())
    approval = store.approve(draft.sha256, approver="validation-owner")

    assert approval.sha256 == draft.sha256
    assert store.verify_locked().sha256 == draft.sha256
    store.approved_path.write_text("changed\n", encoding="utf-8")
    with pytest.raises(ProfileApprovalError, match="hash mismatch"):
        store.verify_locked()


def test_approval_rejects_stale_expected_hash(tmp_path: Path) -> None:
    store = AgentProfileStore(tmp_path)
    store.generate(valid_seed(), DeterministicProfileDraftBackend())
    with pytest.raises(ProfileApprovalError, match="draft hash"):
        store.approve("0" * 64, approver="validation-owner")
```

- [ ] **Step 2: Run focused tests and verify RED**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/validation/test_profile_store.py tests/test_config.py
```

Expected: profile-store tests fail because `AgentProfileStore` does not exist.

- [ ] **Step 3: Implement atomic draft and approval storage**

Use `AtomicArtifactStore(root)` where `root` is the profile directory, and use
these exact paths relative to that root:

```text
input.json
draft.md
TEST_AGENT_PROFILE.md
approval.json
```

`generate` validates the seed, calls the backend, validates Markdown again, writes input and draft atomically, and returns `ProfileDraft`. `approve` compares the caller-supplied hash with the current draft, writes the approved Markdown, and writes `ProfileApproval(version=1, ...)`. Refuse approval if an approved profile already exists. `verify_locked` recomputes the approved file hash on every call.

- [ ] **Step 4: Add profile-provider configuration**

Extend `DreamSettings` with:

```python
validation_profile_backend: str = "inherit"
validation_profile_model: str = ""
validation_profile_max_completion_tokens: int = 4000
```

Add these environment variables:

```dotenv
DREAM_VALIDATION_PROFILE_BACKEND=inherit
DREAM_VALIDATION_PROFILE_MODEL=
DREAM_VALIDATION_PROFILE_MAX_COMPLETION_TOKENS=4000
```

Allowed backend values are `inherit`, `deterministic`, and `openai`. `build_profile_backend(settings, client_factory=None)` reuses the review URL and API key; `inherit` selects deterministic only when the review backend is deterministic, otherwise selects OpenAI and uses `DREAM_VALIDATION_PROFILE_MODEL` or `DREAM_REVIEW_MODEL`.

- [ ] **Step 5: Implement CLI commands**

Support:

```text
python -m dream.validation.profile generate INPUT_JSON RUN_ROOT --env-file .env
python -m dream.validation.profile approve RUN_ROOT --sha256 HASH --approver NAME
python -m dream.validation.profile verify RUN_ROOT
```

`generate` prints only status, output path, and hash. `approve` prints only version and hash. `verify` returns exit 0 only when the approval metadata and approved Markdown hash match. No command prints the API key or complete provider error.

- [ ] **Step 6: Add synthetic committed fixtures**

The input fixture uses the exact seven allowed fields and the approved Markdown contains the ten sections and four examples from the design. `bank-assistant.approval.json` records `approver="offline-fixture"`, version 1, a fixed ISO timestamp, and the actual Markdown SHA-256. Tests must recompute and verify the fixture hash.

- [ ] **Step 7: Run tests and verify GREEN**

Run the command from Step 2. The tests must also load the three committed
`bank-assistant.*` fixtures, recompute the Markdown hash, and exercise the CLI
against a temporary approved profile directory.

Expected: selected tests pass and the CLI test observes
`approved profile verified` with exit 0.

- [ ] **Step 8: Commit the approval workflow**

```bash
git add src/dream/validation/profile.py src/dream/config.py .env.example tests/test_config.py tests/validation/test_profile_store.py tests/fixtures/agent_profile
git commit -m "feat: lock approved test agent profiles"
```

### Task 4: Fixed-profile simulated Agent backend

**Files:**
- Create: `src/dream/validation/agent.py`
- Create: `tests/validation/test_agent.py`
- Modify: `src/dream/config.py`
- Modify: `.env.example`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: an approved `AgentProfileStore`, `DreamService.start_context(ids)`, one user message, and an OpenAI-compatible client.
- Produces: `AgentReply`, `ValidationAgentBackend.reply(...)`, `OpenAIValidationAgentBackend`, and `ProfiledValidationAgent.respond(ids, user_message, include_dream_context) -> AgentReply`.

- [ ] **Step 1: Write failing context-isolation tests**

```python
def test_baseline_agent_receives_profile_but_no_dream_artifacts(tmp_path: Path) -> None:
    backend = RecordingAgentBackend("基线回答")
    agent = profiled_agent(tmp_path, backend)

    reply = agent.respond(ids("project-manager"), "如何换卡？", include_dream_context=False)

    assert reply.content == "基线回答"
    assert "# 小银" in backend.system
    assert "DECISION_RULES" not in backend.system
    assert "USER.md" not in backend.system


def test_evolved_agent_receives_only_current_user_profile(tmp_path: Path) -> None:
    write_dream_artifacts(tmp_path, alice="需要分步骤", bob="喜欢简短")
    backend = RecordingAgentBackend("个性化回答")
    agent = profiled_agent(tmp_path, backend)

    agent.respond(ids("project-manager"), "如何换卡？", include_dream_context=True)

    assert "需要分步骤" in backend.system
    assert "喜欢简短" not in backend.system
    assert "Evidence cards" in backend.system
```

- [ ] **Step 2: Run focused tests and verify RED**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/validation/test_agent.py tests/test_config.py
```

Expected: collection fails because `dream.validation.agent` does not exist.

- [ ] **Step 3: Implement provider-neutral Agent calls**

Use these immutable records:

```python
class AgentReply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    content: str = Field(min_length=1)
    model: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dream_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ValidationAgentBackend(Protocol):
    model: str
    def reply(self, *, system: str, user_message: str) -> str: ...
```

`OpenAIValidationAgentBackend` calls `client.chat.completions.create` with one system message, one user message, `temperature=0`, no tools, and configured `max_completion_tokens`. Reject tool calls, blank output, and provider failures using safe `ValidationAgentError` messages.

- [ ] **Step 4: Implement deterministic context composition**

`ProfiledValidationAgent` calls `profile_store.verify_locked()` for every task. Baseline system input contains only the approved profile. Evolved input additionally contains the current scope's `decision_rules` and `user_profile` from `DreamService.start_context(ids)`. It never includes raw previous conversation, hidden persona, decision-card files, another user's profile, API keys, or provider responses.

Hash the exact optional DREAM context string; use SHA-256 of the empty string in baseline tasks.

- [ ] **Step 5: Add Agent provider settings**

Extend `DreamSettings` and `.env.example`:

```dotenv
DREAM_VALIDATION_AGENT_MODEL=
DREAM_VALIDATION_AGENT_BASE_URL=
DREAM_VALIDATION_AGENT_API_KEY=
DREAM_VALIDATION_AGENT_MAX_COMPLETION_TOKENS=1200
```

`build_validation_agent_backend` requires all credentials only when an operational simulated Agent is requested. It must not affect normal DREAM startup or tests that inject a fake backend.

- [ ] **Step 6: Run tests and verify GREEN**

Run the command from Step 2.

Expected: tests pass and show that the profile hash is identical in baseline/evolved calls while the DREAM context hash differs.

- [ ] **Step 7: Commit the simulated Agent backend**

```bash
git add src/dream/validation/agent.py src/dream/config.py .env.example tests/validation/test_agent.py tests/test_config.py
git commit -m "feat: run fixed-profile validation agents"
```

### Task 5: Synthetic user-to-Agent task capture

**Files:**
- Create: `src/dream/validation/simulation.py`
- Create: `tests/validation/test_simulation.py`
- Modify: `tests/fixtures/personas/project_manager.gold.json`
- Modify: `tests/fixtures/personas/python_beginner.gold.json`
- Modify: `tests/fixtures/personas/technical_lead.gold.json`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `AgnesSimulator`, `ProfiledValidationAgent`, hidden persona mapping, and a fixed `ScopeIds`.
- Produces: `SimulationTaskResult`, `SyntheticTaskRunner.run_task(...)`, and `ValidationConversationStore.append(record)`.

- [ ] **Step 1: Write failing no-leakage and JSONL tests**

```python
def test_simulated_task_persists_only_public_messages(tmp_path: Path) -> None:
    runner = synthetic_runner(tmp_path, user_message="请分步骤说明换卡。", reply="第一步……")
    hidden = {"secret_trait": "step_by_step", "scenario": "card replacement"}

    result = runner.run_task(
        ids=ids("python-beginner"),
        hidden_persona=hidden,
        task_number=1,
        include_dream_context=False,
    )

    encoded = result.record.model_dump_json()
    assert "secret_trait" not in encoded
    assert "step_by_step" not in encoded
    assert result.record.final_response == "第一步……"


def test_conversation_store_is_idempotent_and_append_only(tmp_path: Path) -> None:
    store = ValidationConversationStore(tmp_path / "python_beginner.local.jsonl")
    record = completed_record("evt-python-beginner-001")
    assert store.append(record) is True
    assert store.append(record) is False
    assert parse_manual_ndjson(store.path.read_text(encoding="utf-8")) == (record,)
```

- [ ] **Step 2: Run focused tests and verify RED**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/validation/test_simulation.py tests/validation/test_agnes.py
```

Expected: collection fails because `dream.validation.simulation` does not exist.

- [ ] **Step 3: Extend synthetic persona fixtures without changing trait keys**

Keep the existing `user_id` and `traits`, and add only synthetic fields used by Agnes:

```json
{
  "user_id": "python-beginner",
  "traits": ["step_by_step", "examples", "low_jargon"],
  "bank_context": "第一次使用手机银行，需要逐步说明并避免专业术语",
  "allowed_topics": ["银行卡换卡", "转账限额", "手机银行登录", "可疑短信"],
  "preference_change_at": 9
}
```

Create equivalent bank-context fields for the other two fixed users. All values remain synthetic and must never be sent to DREAM or the Agent backend.

- [ ] **Step 4: Implement one completed task per isolated call**

For each task:

1. Call `AgnesSimulator.next_user_message` with the hidden persona, empty `public_history`, and task number.
2. Call `ProfiledValidationAgent.respond` with only that returned message.
3. Build `ManualConversationRecord` with two messages, a stable event ID `evt-{user_id}-{task_number:03d}`, a new session ID per task, and matching `final_response`.
4. Return profile hash and DREAM context hash separately in `SimulationTaskResult`; do not add them to the public conversation record.

Never persist `hidden_persona`, Agnes request messages, or Agnes provider responses.

- [ ] **Step 5: Implement fsynced idempotent local JSONL storage**

`ValidationConversationStore` reads existing event IDs with `parse_manual_ndjson`, appends one `record.model_dump_json()` line, flushes, and calls `os.fsync`. Add these exact ignore patterns:

```gitignore
tests/fixtures/conversations/*.local.jsonl
tests/evaluation/*.local.json
validation-run/
```

- [ ] **Step 6: Run tests and verify GREEN**

Run the command from Step 2 and scan test output files for the hidden keys `bank_context`, `allowed_topics`, and `preference_change_at`; expected count outside persona fixtures is zero.

- [ ] **Step 7: Commit task capture**

```bash
git add src/dream/validation/simulation.py tests/validation/test_simulation.py tests/fixtures/personas .gitignore
git commit -m "feat: capture synthetic bank agent tasks"
```

### Task 6: Periodic 5/5/2 dream campaign

**Files:**
- Create: `src/dream/validation/campaign.py`
- Create: `tests/validation/test_campaign.py`
- Modify: `src/dream/closed_loop.py`
- Modify: `tests/test_closed_loop.py`

**Interfaces:**
- Consumes: `SyntheticTaskRunner`, `DreamService`, `ClosedLoopCoordinator`, three fixed users, and one approved profile.
- Produces: `CampaignPhase`, `CampaignState`, `ValidationCampaign.run_next_task(user_id)`, `run_due_dreams()`, and `verify_phase_gate(user_id)`.

- [ ] **Step 1: Write failing 5/5/2 phase and activation tests**

```python
def test_campaign_uses_fixed_profile_across_two_dream_cycles(tmp_path: Path) -> None:
    campaign = fake_campaign(tmp_path)

    for _ in range(5):
        campaign.run_next_task("project-manager")
    first = campaign.run_due_dreams()["project-manager"]
    for _ in range(5):
        campaign.run_next_task("project-manager")
    second = campaign.run_due_dreams()["project-manager"]
    for _ in range(2):
        campaign.run_next_task("project-manager")

    state = campaign.state("project-manager")
    assert state.task_count == 12
    assert state.active_cycles == 2
    assert state.profile_sha256_before == state.profile_sha256_after
    assert first.version < second.version


def test_evolved_phase_cannot_start_before_due_dream_is_active(tmp_path: Path) -> None:
    campaign = fake_campaign(tmp_path)
    for _ in range(5):
        campaign.run_next_task("technical-lead")
    with pytest.raises(CampaignBlocked, match="dream cycle 1"):
        campaign.run_next_task("technical-lead")
```

- [ ] **Step 2: Run focused tests and verify RED**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/validation/test_campaign.py tests/test_closed_loop.py
```

Expected: collection fails because `dream.validation.campaign` does not exist.

- [ ] **Step 3: Implement explicit campaign phases**

Use exact phase boundaries:

```python
class CampaignPhase(StrEnum):
    BASELINE = "baseline"       # tasks 1-5
    EVOLVED_V1 = "evolved_v1"   # tasks 6-10
    EVOLVED_V2 = "evolved_v2"   # tasks 11-12
    COMPLETE = "complete"
```

`run_next_task` determines `include_dream_context` from the phase, rejects task 6 until cycle 1 is active, rejects task 11 until cycle 2 is active, imports the completed record through `DreamService.import_manual_ndjson`, and atomically records only event ID, task number, phase, profile hash, DREAM context hash, and active publication version in `validation-run/campaign/users/<user_id>.json`.

- [ ] **Step 4: Reuse the existing closed-loop lifecycle for local context publication**

Add `ClosedLoopCoordinator.confirm_local_context(ids, version)` that:

- permits only `READY_FOR_WRITEBACK` candidates;
- recomputes `CHARACTER_DEFINITION.md` and scoped `USER_PERSONA.md` hashes;
- compares them with the candidate record;
- marks both writebacks satisfied only when the files exist and hashes match;
- does not call an external API or claim that Character.AI was updated.

`ValidationCampaign.run_due_dreams` calls `dream`, requires a human-review callback to return `True`, calls `approve`, `confirm_local_context`, and `activate`. A false callback rejects the candidate and blocks the next phase.

- [ ] **Step 5: Require all three users before campaign completion**

`campaign.summary()` succeeds only when every fixed user has 12 tasks, two active cycles, identical initial/final profile hashes, and no missing local conversation file. The shared Agent decision rules remain at agent scope while user personas remain per user.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run the command from Step 2.

Expected: all selected tests pass, including a rejected cycle, a failed LLM cycle that preserves the previous active version, and a changed profile hash that blocks the campaign.

- [ ] **Step 7: Commit the periodic campaign**

```bash
git add src/dream/validation/campaign.py src/dream/closed_loop.py tests/validation/test_campaign.py tests/test_closed_loop.py
git commit -m "feat: run periodic evolution campaigns"
```

### Task 7: Evaluation provenance for fixed profile and model identity

**Files:**
- Modify: `src/dream/validation/evaluation.py`
- Modify: `tests/validation/test_evaluation.py`
- Modify: `tests/evaluation/.gitkeep`

**Interfaces:**
- Consumes: campaign summary, existing human evidence counts, approved profile hash, and external model identity.
- Produces: an extended `ValidationRunInput` and reproducible `EvaluationReport` that fail when the baseline changed.

- [ ] **Step 1: Write failing provenance acceptance tests**

```python
def test_acceptance_requires_unchanged_profile_and_model() -> None:
    changed_profile = passing_run(
        agent_profile_sha256_before="a" * 64,
        agent_profile_sha256_after="b" * 64,
    )
    changed_model = passing_run(
        baseline_model="model-a",
        evolved_model="model-b",
    )

    assert evaluate_validation_run(changed_profile).passed is False
    assert evaluate_validation_run(changed_model).passed is False


def test_acceptance_records_twelve_tasks_and_two_cycles_per_user() -> None:
    report = evaluate_validation_run(passing_run())
    assert all(user.task_count == 12 for user in report.run.users)
    assert report.run.completed_dream_writeback_cycles >= 2
    assert report.passed is True
```

- [ ] **Step 2: Run focused tests and verify RED**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/validation/test_evaluation.py
```

Expected: Pydantic rejects the new fields because the existing model forbids extras.

- [ ] **Step 3: Add exact provenance fields**

Extend `ValidationRunInput`:

```python
agent_profile_version: int = Field(ge=1)
agent_profile_sha256_before: str = Field(pattern=r"^[0-9a-f]{64}$")
agent_profile_sha256_after: str = Field(pattern=r"^[0-9a-f]{64}$")
baseline_model: str = Field(min_length=1)
evolved_model: str = Field(min_length=1)
baseline_temperature: float = 0.0
evolved_temperature: float = 0.0
public_seed_count: int = Field(ge=0)
helpsteer2_seed_count: int = Field(ge=0)
pku_saferlhf_seed_count: int = Field(ge=0)
```

Acceptance additionally requires identical profile hashes, identical model IDs, both temperatures equal to zero, exactly 30 public seeds split 20/10, and exactly three users with at least 10 tasks each. Preserve every existing evidence, safety, isolation, cycle, conflict, and rollback requirement.

- [ ] **Step 4: Keep saved reports reproducible**

`verify_report` must recompute the new provenance failures. Update every test fixture and test helper with literal synthetic hashes and model IDs; do not silently default missing fields in saved reports.

- [ ] **Step 5: Run tests and verify GREEN**

Run the command from Step 2.

Expected: all evaluation tests pass; one changed hash, model, temperature, or seed count fails acceptance with a specific reason.

- [ ] **Step 6: Commit provenance evaluation**

```bash
git add src/dream/validation/evaluation.py tests/validation/test_evaluation.py tests/evaluation/.gitkeep
git commit -m "test: verify fixed-profile evolution provenance"
```

### Task 8: Runbook, real data, real simulation, and formal report

**Files:**
- Create: `docs/validation/test-agent-evolution-runbook.md`
- Modify: `README.md`
- Create operationally: `tests/fixtures/ai_seed/selected.local.jsonl`
- Create operationally: `tests/fixtures/conversations/project_manager.local.jsonl`
- Create operationally: `tests/fixtures/conversations/python_beginner.local.jsonl`
- Create operationally: `tests/fixtures/conversations/technical_lead.local.jsonl`
- Create after evidence review: `tests/evaluation/latest.json`

**Interfaces:**
- Consumes: all implemented validation components, 30 genuine public dataset rows, configured LLM credentials, and human review decisions.
- Produces: one locked initial profile, three 12-task conversation files, at least two active dream cycles, and a recomputable formal evaluation report.

- [ ] **Step 1: Write the exact operator runbook**

Document this sequence without Character.AI website dependency:

```text
1. Validate the synthetic profile seed.
2. Generate the complete profile draft with the configured model.
3. Inspect all ten sections and four examples.
4. Approve and lock the exact SHA-256.
5. Manually select and validate 20 HelpSteer2 plus 10 PKU-SafeRLHF rows.
6. Run tasks 1-5 for each synthetic user with profile only.
7. Inspect USER.md and decision cards, approve and activate dream cycle 1.
8. Run tasks 6-10 with active DREAM context.
9. Demonstrate one explicit preference change, then approve and activate cycle 2.
10. Run tasks 11-12 with the second active context.
11. Audit citations, isolation, private-data leakage, hallucinations, fallback, and rollback.
12. Save actual counts and hashes to tests/evaluation/latest.json.
```

Include exact CLI commands and state that a login, CAPTCHA, missing API key, unavailable dataset row, or failed model call blocks the real run rather than permitting fabricated output.

- [ ] **Step 2: Validate 30 genuine public records**

Create the ignored `selected.local.jsonl` only from manually inspected source rows and run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m dream.validation.seeds validate tests/fixtures/ai_seed/selected.local.jsonl --expected-count 30
```

Expected: `30 valid AI-only seed records; 0 user-profile fields`. Verify the count split is exactly 20 HelpSteer2 and 10 PKU-SafeRLHF before importing.

- [ ] **Step 3: Generate and approve the real test profile**

Use the committed synthetic input and a local ignored run root:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m dream.validation.profile generate tests/fixtures/agent_profile/bank-assistant.input.json validation-run/agent-profile --env-file .env
```

Inspect `validation-run/agent-profile/draft.md`, then compute the exact draft
hash locally and pass that value to the approval command:

```bash
PROFILE_SHA256=$(shasum -a 256 validation-run/agent-profile/draft.md | awk '{print $1}')
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m dream.validation.profile approve validation-run/agent-profile --sha256 "$PROFILE_SHA256" --approver fenghao
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m dream.validation.profile verify validation-run/agent-profile
```

The operator must still read the draft before running approval; successful hash
verification alone is not a qualitative or safety approval.

- [ ] **Step 4: Execute the 5/5/2 campaign**

Run three synthetic users until every local file has exactly 12 records. Do not enter phase 2 or phase 3 until the prior dream version is human-reviewed and active. Every task must use a fresh Agent call, the same configured model, temperature 0, and the still-locked profile hash.

After every phase, scan all DREAM artifacts for the hidden persona-only keys and require zero matches outside `tests/fixtures/personas`.

- [ ] **Step 5: Perform actual human scoring**

For each `USER.md` fact, open its cited event and record supported/unsupported. For each evolved task, compare with the baseline rubric and record personalization and AI decision success. Record severe hallucinations, user-scope leaks, private user data in Agent-level cards, missing source events, incomplete activations, preference-change handling, and rollback/fallback outcome.

The profile itself is not counted as AI evolution; only behavior attributable to active `DECISION_RULES.md` and scoped `USER.md` counts.

- [ ] **Step 6: Generate and verify the formal report**

Create `tests/evaluation/latest.json` from actual counts and hashes only, then run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m dream.validation.evaluation verify tests/evaluation/latest.json
```

Expected for a successful experiment: `passed=true`, profile evidence rate at least 0.85, personalization at least 0.80, AI evolution at least 0.80, no severe hallucinations or cross-user leaks, fixed profile/model provenance, 30 genuine seeds, and two completed cycles. If the report fails, keep the honest failed result and do not claim completion.

- [ ] **Step 7: Run complete automated verification**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m pytest -q -p no:cacheprovider
/Users/fenghao/PycharmProjects/dream/.venv/bin/python -m ruff check src tests
/Users/fenghao/PycharmProjects/dream/.venv/bin/python -m compileall -q src/dream
git diff --check
```

Expected: every command exits 0. Inspect `git status` and ensure no `.env`, hidden persona expansion outside fixtures, local conversation text, selected dataset text, or API key is staged.

- [ ] **Step 8: Commit documentation and non-sensitive formal output**

Only after inspecting `latest.json` for synthetic IDs, aggregate counts, model ID, profile hash, and no raw conversation or secrets:

```bash
git add README.md docs/validation/test-agent-evolution-runbook.md tests/evaluation/latest.json
git commit -m "docs: record fixed-profile evolution validation"
```

- [ ] **Step 9: Complete the branch**

After all verification passes, use the branch-completion workflow to merge `feature/character-ai-closed-loop` into local `main`, rerun the full suite on `main`, and only then remove the owned worktree. Do not push to GitHub unless the user separately asks.

## Final Acceptance Checklist

- [ ] One complete synthetic bank Agent profile was generated from the strict seed.
- [ ] A human approved the exact profile hash before any task used it.
- [ ] The profile hash and external model identity remained unchanged across the experiment.
- [ ] Thirty genuine public AI-only seed rows were validated with stable source IDs and a 20/10 dataset split.
- [ ] Three synthetic users each completed exactly 12 tasks.
- [ ] Two periodic dream cycles were human-reviewed and activated.
- [ ] Every user-profile fact is backed by an actual event ID.
- [ ] Agent-level decision cards contain no user-specific private facts.
- [ ] The evolved Agent demonstrably used the current user's profile and shared decision rules.
- [ ] One preference change and one failure fallback or rollback were demonstrated.
- [ ] Severe hallucinations and cross-user leaks are both zero.
- [ ] `tests/evaluation/latest.json` is recomputable and contains no raw conversation, hidden persona, or secret.
- [ ] Full pytest, Ruff, compile, and diff checks pass before local-main merge.
