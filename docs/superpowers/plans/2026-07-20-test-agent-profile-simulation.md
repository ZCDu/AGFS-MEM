# Independent Codex Task Evolution Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate DREAM with one locked Character Building-style test profile and 36 traceable conversations produced by fresh Codex Agent tasks, with dream cycles after task 5 and task 10 for each of three isolated users.

**Architecture:** The current Codex task is the orchestrator. It generates one synthetic customer message, creates a projectless Codex task containing only the locked Agent profile, active decision rules, the current user's active profile, and that message, then records the returned answer and Codex thread ID through DREAM's existing manual JSONL adapter. Repository code only validates the fixed profile, generalizes source provenance, enforces the 5/5/2 campaign state, and verifies the final report; Codex task creation remains an operator action and does not become a DREAM runtime dependency.

**Tech Stack:** Python 3.11-3.13, Pydantic 2, Markdown/JSON/JSONL, Codex App task tools, pytest 9, Ruff.

## Global Constraints

- Do not add an external model Agent backend, public Agent creator, production profile API, Character.AI dependency, or public seed dataset.
- The test profile is synthetic, belongs to the logical bank Agent, and never claims to be the company's real System Prompt.
- A human must approve the exact profile SHA-256 before task 1.
- The same approved profile hash must be used in all 36 Codex Agent tasks.
- Use fixed scope `tenant_id=dream-lab`, `agent_id=enterprise-colleague`.
- Use exactly three users: `project-manager`, `python-beginner`, and `technical-lead`.
- Use exactly 12 Agent tasks per user: baseline tasks 1-5, evolved-v1 tasks 6-10, evolved-v2 tasks 11-12.
- Dream cycle 1 must be active before task 6; dream cycle 2 must be active before task 11.
- Every Agent response comes from a newly created projectless Codex task with no forked history.
- The Agent task receives no hidden persona, raw prior conversation, other user's profile, candidate artifact, repository path, or secret.
- Every JSONL `session_id` is the real Codex thread ID returned for that task.
- `final_response` equals the exact final assistant response read from that Codex task.
- Hidden persona fixtures remain visible only to the orchestrator and evaluator.
- AI decision cards contain reusable Agent decisions only, never a user's identity or preferences.
- `tests/evaluation/latest.json` is created only from actual completed tasks and human review; never fabricate a passing report.
- Follow red-green-refactor and commit each independently testable task.

---

## File Map

- `src/dream/validation/profile.py`: validate and hash the fixed test Agent profile and its approval.
- `src/dream/sources/manual.py`: identify completed records as generic Codex validation tasks instead of Character.AI-specific input.
- `src/dream/validation/campaign.py`: store Codex task receipts and enforce exact 5/5/2 phase gates.
- `src/dream/validation/evaluation.py`: require fixed-profile and Codex-thread provenance in the formal report.
- `tests/fixtures/agent_profile/`: committed synthetic input, approved Markdown, and approval metadata.
- `tests/fixtures/personas/`: hidden synthetic user traits used only to create customer messages and score results.
- `docs/validation/codex-task-evolution-runbook.md`: exact operator and Codex task procedure.

### Task 1: Fixed Character Building profile and approval verifier

**Files:**
- Create: `src/dream/validation/profile.py`
- Create: `tests/validation/test_profile.py`
- Create: `tests/fixtures/agent_profile/bank-assistant.input.json`
- Create: `tests/fixtures/agent_profile/TEST_AGENT_PROFILE.md`
- Create: `tests/fixtures/agent_profile/approval.json`

**Interfaces:**
- Consumes: synthetic profile input JSON, approved Markdown, and approval JSON.
- Produces: `AgentProfileSeed`, `ProfileApproval`, `ProfileValidationError`, `validate_profile_markdown(seed, markdown)`, `verify_approved_profile(root) -> ProfileApproval`, and CLI `python -m dream.validation.profile verify ROOT`.

- [ ] **Step 1: Write failing profile contract tests**

```python
def test_approved_profile_contains_complete_character_building_sections() -> None:
    root = Path(__file__).parents[1] / "fixtures" / "agent_profile"
    approval = verify_approved_profile(root)
    assert approval.status == "approved"
    assert approval.version == 1


def test_changed_profile_fails_locked_hash(tmp_path: Path) -> None:
    root = copy_profile_fixture(tmp_path)
    profile = root / "TEST_AGENT_PROFILE.md"
    profile.write_text(profile.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    with pytest.raises(ProfileValidationError, match="hash mismatch"):
        verify_approved_profile(root)


def test_profile_rejects_account_like_numbers(tmp_path: Path) -> None:
    seed = valid_seed()
    with pytest.raises(ProfileValidationError, match="account-like"):
        validate_profile_markdown(seed, complete_profile() + "6222021234567890")
```

- [ ] **Step 2: Run the test and verify RED**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/validation/test_profile.py
```

Expected: collection fails because `dream.validation.profile` does not exist.

- [ ] **Step 3: Implement strict profile input and approval models**

```python
class AgentProfileSeed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=2, max_length=40)
    tagline: str = Field(min_length=4, max_length=120)
    role: str = Field(min_length=2, max_length=80)
    service_scope: tuple[str, ...] = Field(min_length=1, max_length=12)
    personality: tuple[str, ...] = Field(min_length=2, max_length=12)
    response_style: tuple[str, ...] = Field(min_length=1, max_length=12)
    greeting: str = Field(min_length=1, max_length=500)


class ProfileApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["approved"]
    version: int = Field(ge=1)
    approver: str = Field(min_length=1, max_length=80)
    approved_at: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
```

Strip every string, reject blanks, NUL characters, duplicate tuple items, extra fields, and timestamps without time zones.

- [ ] **Step 4: Implement exact Markdown and hash validation**

Require the title `# {seed.name}`, the ten headings from the approved design, at least four `### 示例` headings, every service-scope item, and safety concepts `验证码`, `密码`, `银行卡号`, `真实交易`, `贷款`, and `收益`. Reject files over 12,000 characters and any 12-19 digit sequence. `verify_approved_profile` validates all three files, recomputes SHA-256 from UTF-8 bytes, and compares it to `approval.json`.

- [ ] **Step 5: Create the complete synthetic bank profile fixtures**

`bank-assistant.input.json` contains only the seven schema fields. `TEST_AGENT_PROFILE.md` contains a synthetic bank assistant named `小银`, all ten sections, and four safe examples. `approval.json` records version 1, approver `fenghao`, an actual timezone-aware timestamp, and the actual Markdown SHA-256.

- [ ] **Step 6: Run tests and CLI verification**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/validation/test_profile.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m dream.validation.profile verify tests/fixtures/agent_profile
```

Expected: all tests pass and CLI reports one approved, hash-verified profile.

- [ ] **Step 7: Commit the fixed profile**

```bash
git add src/dream/validation/profile.py tests/validation/test_profile.py tests/fixtures/agent_profile
git commit -m "feat: lock the validation agent profile"
```

### Task 2: Generic Codex task conversation provenance

**Files:**
- Modify: `src/dream/sources/manual.py`
- Modify: `tests/sources/test_manual.py`
- Modify: `src/dream/service.py`
- Modify: `tests/test_api_e2e.py`

**Interfaces:**
- Consumes: completed JSONL with a real Codex thread ID.
- Produces: `ManualConversationRecord.source`, generic `source_refs`, and unchanged idempotent DREAM import.

- [ ] **Step 1: Write failing source and session tests**

```python
def test_manual_codex_record_preserves_real_thread_provenance() -> None:
    record = parse_manual_ndjson(valid_line(
        source="codex-thread",
        session_id="019fd149-example-thread-id",
    ))[0]
    event = manual_record_to_event(record)
    assert event.source_refs == ({
        "source": "codex-thread",
        "session_id": "019fd149-example-thread-id",
    },)


def test_manual_source_accepts_only_validation_sources() -> None:
    with pytest.raises(ManualSourceError):
        parse_manual_ndjson(valid_line(source="unknown-source"))
```

- [ ] **Step 2: Run the focused test and verify RED**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/sources/test_manual.py tests/test_api_e2e.py
```

Expected: the strict record rejects `source` as an extra field.

- [ ] **Step 3: Add a strict source field and generic mapping**

```python
source: Literal["codex-thread", "manual-import"] = "manual-import"
```

Change `manual_record_to_event` to use `record.source` rather than the hard-coded `manual-character-ai`. Keep `session_id` nonblank and opaque; do not parse or invent a URL. Existing offline fixtures without `source` continue to map to `manual-import`.

- [ ] **Step 4: Run tests and verify GREEN**

Run the command from Step 2.

Expected: selected tests pass, duplicate imports remain idempotent, and source metadata contains the exact thread ID.

- [ ] **Step 5: Commit generic provenance**

```bash
git add src/dream/sources/manual.py src/dream/service.py tests/sources/test_manual.py tests/test_api_e2e.py
git commit -m "feat: trace validation events to Codex tasks"
```

### Task 3: 5/5/2 Codex campaign state and gates

**Files:**
- Create: `src/dream/validation/campaign.py`
- Create: `tests/validation/test_campaign.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: one `ManualConversationRecord`, real Codex thread ID, approved profile hash, active publication version, and DREAM context hash.
- Produces: `CampaignPhase`, `CodexTaskReceipt`, `UserCampaignState`, `CodexCampaignStore.record(receipt)`, `assert_can_create(user_id, task_number)`, `note_active_cycle(user_id, cycle, version)`, and `summary()`.

- [ ] **Step 1: Write failing phase-gate and uniqueness tests**

```python
def test_task_six_waits_for_first_active_dream(tmp_path: Path) -> None:
    store = CodexCampaignStore(tmp_path, approved_profile_sha="a" * 64)
    for number in range(1, 6):
        store.record(receipt("project-manager", number))
    with pytest.raises(CampaignBlocked, match="cycle 1"):
        store.assert_can_create("project-manager", 6)
    store.note_active_cycle("project-manager", cycle=1, version=1)
    store.assert_can_create("project-manager", 6)


def test_task_eleven_waits_for_second_active_dream(tmp_path: Path) -> None:
    store = state_after_ten_tasks(tmp_path)
    with pytest.raises(CampaignBlocked, match="cycle 2"):
        store.assert_can_create("python-beginner", 11)


def test_thread_id_and_event_id_are_globally_unique(tmp_path: Path) -> None:
    store = CodexCampaignStore(tmp_path, approved_profile_sha="a" * 64)
    store.record(receipt("project-manager", 1, thread_id="thread-one"))
    with pytest.raises(CampaignValidationError, match="thread ID"):
        store.record(receipt("technical-lead", 1, thread_id="thread-one"))
```

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/validation/test_campaign.py
```

Expected: collection fails because `dream.validation.campaign` does not exist.

- [ ] **Step 3: Implement exact immutable receipt fields**

```python
class CampaignPhase(StrEnum):
    BASELINE = "baseline"
    EVOLVED_V1 = "evolved_v1"
    EVOLVED_V2 = "evolved_v2"


class CodexTaskReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    user_id: Literal["project-manager", "python-beginner", "technical-lead"]
    task_number: int = Field(ge=1, le=12)
    event_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dream_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_publication_version: int = Field(ge=0)
```

Derive phase from task number rather than accepting caller-provided phase. Baseline receipts require publication version 0 and SHA-256 of empty DREAM context. Evolved receipts require an active version greater than zero.

- [ ] **Step 4: Implement append-only state and gates**

Store one fsynced JSON line per receipt at `validation-run/campaign/receipts.jsonl` and atomic user summaries at `validation-run/campaign/users/<user_id>.json`. Reject duplicate thread IDs, event IDs, task numbers, skipped task numbers, changed profile hashes, task 6 without cycle 1, task 11 without cycle 2, and more than 12 tasks.

`summary()` succeeds only when all three users have 12 tasks and two active cycles.

- [ ] **Step 5: Add local runtime ignores**

```gitignore
validation-run/
tests/fixtures/conversations/*.local.jsonl
tests/evaluation/*.local.json
```

- [ ] **Step 6: Run tests and verify GREEN**

Run the command from Step 2.

Expected: tests pass for phase transitions, restart recovery, profile hash mismatch, duplicated threads, and complete 36-task summary.

- [ ] **Step 7: Commit campaign state**

```bash
git add src/dream/validation/campaign.py tests/validation/test_campaign.py .gitignore
git commit -m "feat: track independent Codex validation tasks"
```

### Task 4: Formal evaluation requires real Codex tasks

**Files:**
- Modify: `src/dream/validation/evaluation.py`
- Modify: `tests/validation/test_evaluation.py`

**Interfaces:**
- Consumes: existing human evidence scores plus campaign provenance.
- Produces: reproducible acceptance that fails for missing threads, changed profile hashes, wrong task counts, or missing cycles.

- [ ] **Step 1: Write failing provenance tests**

```python
def test_acceptance_requires_36_real_codex_tasks() -> None:
    report = evaluate_validation_run(passing_run(codex_thread_count=35))
    assert report.passed is False
    assert any("36 Codex" in reason for reason in report.failure_reasons)


def test_acceptance_requires_unchanged_initial_profile() -> None:
    report = evaluate_validation_run(passing_run(
        agent_profile_sha256_before="a" * 64,
        agent_profile_sha256_after="b" * 64,
    ))
    assert report.passed is False
    assert any("profile hash" in reason for reason in report.failure_reasons)


def test_public_seed_data_is_not_an_acceptance_input() -> None:
    assert "public_seed_count" not in ValidationRunInput.model_fields
```

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/validation/test_evaluation.py
```

Expected: Pydantic rejects the new provenance fields.

- [ ] **Step 3: Add exact report fields**

```python
agent_profile_version: int = Field(ge=1)
agent_profile_sha256_before: str = Field(pattern=r"^[0-9a-f]{64}$")
agent_profile_sha256_after: str = Field(pattern=r"^[0-9a-f]{64}$")
codex_thread_count: int = Field(ge=0)
missing_codex_threads: int = Field(ge=0)
duplicate_codex_threads: int = Field(ge=0)
```

Acceptance additionally requires identical profile hashes, `codex_thread_count == 36`, zero missing/duplicate threads, three users with exactly 12 tasks, and at least two active dream cycles per user. Preserve all existing evidence, personalization, AI evolution, hallucination, leakage, conflict, and rollback checks.

- [ ] **Step 4: Update recomputation and CLI output**

`verify_report` must recompute every new failure. CLI output adds `codex_threads=36` and `profile_unchanged=true`. Saved reports missing these fields are invalid rather than silently defaulted.

- [ ] **Step 5: Run tests and verify GREEN**

Run the command from Step 2.

Expected: tests pass; a changed hash, 35 threads, duplicate thread, missing thread, or user with 11 tasks fails.

- [ ] **Step 6: Commit formal provenance checks**

```bash
git add src/dream/validation/evaluation.py tests/validation/test_evaluation.py
git commit -m "test: require real Codex task provenance"
```

### Task 5: Codex task operator runbook

**Files:**
- Create: `docs/validation/codex-task-evolution-runbook.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: approved profile, Codex App task tools, manual JSONL import, closed-loop DREAM endpoints, and campaign store.
- Produces: one repeatable 36-task procedure without an external Agent API.

- [ ] **Step 1: Document the exact fresh-task prompt**

The runbook includes this literal prompt structure:

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

State that each Agent must be a new projectless Codex task, not a fork, not a continuation, and not a project task with repository access.

- [ ] **Step 2: Document one complete recorded round**

Show: verify profile hash; call campaign gate; create the Codex task; save the returned thread ID; read the final response; create a two-message JSONL line with `source="codex-thread"`; import it; and append a receipt. Explain that inability to read the full response or confirm the task ID blocks that round.

- [ ] **Step 3: Document both dream boundaries**

After tasks 1-5, run scoped Background Review and Curators, inspect every `USER.md` fact and decision card, approve and activate cycle 1, then unlock task 6. Repeat after tasks 6-10 before task 11. Demonstrate one rejected candidate or rollback without deleting history.

- [ ] **Step 4: Document human scoring and privacy audit**

Require checking source event citations, current-user personalization, decision-card privacy, cross-user leakage, severe bank hallucinations, preference change at task 9, profile hash stability, and 36 live Codex thread IDs. Hidden persona contents and raw conversations stay out of `latest.json`.

- [ ] **Step 5: Update README positioning**

Describe the validation as fixed initial Agent profile plus independent Codex tasks; remove Character.AI website writeback and public seed instructions from the active validation path. Keep production behavior described as provider-neutral.

- [ ] **Step 6: Commit the runbook**

```bash
git add docs/validation/codex-task-evolution-runbook.md README.md
git commit -m "docs: explain independent Codex validation tasks"
```

### Task 6: Execute 36 real Codex Agent tasks

**Files created operationally:**
- `tests/fixtures/conversations/project_manager.local.jsonl`
- `tests/fixtures/conversations/python_beginner.local.jsonl`
- `tests/fixtures/conversations/technical_lead.local.jsonl`
- `validation-run/campaign/receipts.jsonl`
- `validation-run/campaign/users/*.json`

- [ ] **Step 1: Verify and lock the profile before task creation**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m dream.validation.profile verify tests/fixtures/agent_profile
```

Expected: approved profile and exact SHA-256 verified.

- [ ] **Step 2: Run baseline tasks 1-5 for all three users**

For each round, the orchestrator generates one natural synthetic customer message from the corresponding hidden persona, creates a new projectless Codex task with only the fixed profile and that message, reads the exact response, saves the real thread ID, imports the two-message event, and records the receipt with empty DREAM-context hash and publication version 0.

Expected: 15 unique Codex threads and five ordered records per user; no `USER.md` or `DECISION_RULES.md` is sent to baseline Agent tasks.

- [ ] **Step 3: Run and activate dream cycle 1**

Run scoped review and Curators for every user, inspect outputs, reject any user-profile fact without an event ID and any decision card containing a user-specific fact, then activate the approved candidate. Record one active cycle per user.

Expected: task 6 gate opens only for users whose version 1 is active.

- [ ] **Step 4: Run evolved-v1 tasks 6-10**

Create 15 new projectless Codex tasks. Inject only the same locked profile, current active shared rules, current user's active profile, and new message. Task 9 contains the explicit preference change.

Expected: 30 unique Agent threads total and ten ordered records per user.

- [ ] **Step 5: Run and activate dream cycle 2**

Repeat review and Curators, verify the preference conflict was replaced or explicitly reconciled with task-9 evidence, inspect Agent-level cards for private user data, and activate version 2. Demonstrate one candidate failure fallback or rollback while preserving the last active version.

- [ ] **Step 6: Run evolved-v2 tasks 11-12**

Create the final six projectless Codex tasks with the second active context.

Expected: exactly 36 unique live thread IDs, 12 tasks per user, unchanged profile hash, and two active cycles per user.

### Task 7: Produce the honest formal evaluation report

**Files:**
- Create from actual evidence: `tests/evaluation/latest.json`

- [ ] **Step 1: Audit all profile facts and decisions**

For every `USER.md` entry, open its cited JSONL event and mark supported or unsupported. For evolved responses, score whether the answer correctly used the current user preference and improved the reusable Agent decision behavior. Count severe hallucinations, cross-user leaks, private user facts in cards, missing event IDs, and missing/duplicate Codex threads.

- [ ] **Step 2: Build the report from actual counts**

Use `ValidationRunInput` with the actual profile version/hash, 36 thread count, three 12-task users, per-user evidence/personalization counts, AI-evolution results, two completed cycles per user, preference-change result, and fallback/rollback result. Do not include raw conversations, hidden personas, or secrets.

- [ ] **Step 3: Verify the saved report**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m dream.validation.evaluation verify tests/evaluation/latest.json
```

Expected for a passing experiment: 36 Codex threads, unchanged profile hash, evidence rate at least 0.85, personalization at least 0.80, AI evolution at least 0.80, zero severe hallucinations and cross-user leaks, two cycles per user, preference change passed, and fallback/rollback passed. If any threshold fails, preserve the honest failed report and do not claim completion.

- [ ] **Step 4: Commit only the non-sensitive report**

Inspect `latest.json` before staging, then:

```bash
git add tests/evaluation/latest.json
git commit -m "test: record Codex evolution evaluation"
```

### Task 8: Full verification and local-main merge

- [ ] **Step 1: Run all automated verification**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/fenghao/PycharmProjects/dream/.venv/bin/python \
  -m pytest -q -p no:cacheprovider
/Users/fenghao/PycharmProjects/dream/.venv/bin/python -m ruff check src tests
/Users/fenghao/PycharmProjects/dream/.venv/bin/python -m compileall -q src/dream
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 2: Inspect staged and ignored data**

Ensure `.env`, local conversation JSONL, campaign receipts, raw thread responses, hidden persona expansion, and secrets are not staged. `latest.json` may contain only synthetic IDs, aggregate counts, profile hash, and no raw content.

- [ ] **Step 3: Merge only after real acceptance**

Use the branch-completion workflow to merge `feature/character-ai-closed-loop` into local `main`, rerun the full test suite on `main`, and remove the owned worktree only after the merge passes. Do not push to GitHub unless separately requested.

## Final Acceptance Checklist

- [ ] One complete Character Building-style synthetic bank Agent profile is approved and hash-locked.
- [ ] Three synthetic users each have exactly 12 real completed tasks.
- [ ] All 36 records reference unique, real Codex thread IDs.
- [ ] Every Agent response came from a fresh projectless Codex task with no prior history.
- [ ] Baseline Agent tasks received no DREAM profile or decision rules.
- [ ] Tasks 6-10 used cycle-1 active artifacts; tasks 11-12 used cycle-2 active artifacts.
- [ ] The initial Agent profile hash remained unchanged.
- [ ] Every user-profile fact has actual event evidence.
- [ ] Agent decision cards contain no user-specific private facts.
- [ ] One preference change and one fallback/rollback were demonstrated.
- [ ] Severe hallucinations and cross-user leaks are zero.
- [ ] `tests/evaluation/latest.json` is recomputable and contains no raw conversations or secrets.
- [ ] Full pytest, Ruff, compile, diff, and post-merge checks pass.
