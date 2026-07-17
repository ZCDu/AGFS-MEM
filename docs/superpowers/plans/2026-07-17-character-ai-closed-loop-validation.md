# Character.AI Closed-Loop Evolution Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a disk-verifiable validation loop that imports manually captured Character.AI tasks, dreams them into isolated `USER.md` profiles and shared AI decision rules, generates bounded writeback text, requires human writeback confirmation, and blocks the next validation task until the latest stable version is active.

**Architecture:** Keep the existing ledger, Background Review, managers, Curators, snapshots, reports, and rollback services as the memory core. Add a validation input adapter, durable review progress, a per-user publication state machine, separate AI/user writeback renderers, and one closed-loop coordinator; Character.AI remains a manual external surface, while Agnes is accessed only through replaceable OpenAI-compatible backends and an isolated simulator client.

**Tech Stack:** Python 3.11-3.13, FastAPI, Pydantic 2, HTTPX 0.28, OpenAI-compatible chat completions, Markdown/JSON/JSONL, pytest 9, pytest-asyncio, Ruff.

## Global Constraints

- Test scope is fixed to `tenant_id=dream-lab` and `agent_id=enterprise-colleague`; three allowed users are `project-manager`, `python-beginner`, and `technical-lead`.
- Hidden persona files may be read only by the Agnes simulator and evaluator; they must never enter DREAM events, prompts, snapshots, reports, writeback files, or Character.AI.
- A JSONL line is one normally completed task containing the full user/assistant transcript; a single message or summary is not accepted.
- `event_id` is the stable idempotency key; `final_response` must equal the last assistant message.
- Shared AI artifacts must contain no user-specific identity, preference, secret, or task content; user artifacts remain isolated by `tenant_id + agent_id + user_id`.
- `DECISION_RULES.md` and `USER.md` remain the full, inspectable sources of truth; `CHARACTER_DEFINITION.md` and `USER_PERSONA.md` are bounded publication artifacts only.
- Character.AI writeback is manual. Do not call non-public Character.AI APIs, scrape the website, or automate page edits.
- Candidate versions cannot affect a task already in progress. After the phase-one blind collection batch has been scored, every later validation task starts only from an `active` version that processed the latest completed event.
- A no-change dream still advances `processed_through_event_id`; a failed dream remains inspectable and falls back to the last active version.
- LLM calls receive at most two automatic retries after the first attempt. Invalid structure, missing citations, cross-user leakage, or oversized output cannot be activated.
- Use synthetic users only; do not add real employee personal data.
- File-knowledge, Skill, and todo extraction remain outside this implementation plan.
- Follow red-green-refactor for each production behavior and commit each independently reviewable task.

## File Map

- `src/dream/sources/manual.py`: validates the approved manual JSONL contract and maps records to `TaskCompletedEvent`.
- `src/dream/review/progress.py`: persists which ledger events completed Background Review and restores unprocessed work after restart.
- `src/dream/structured_llm.py`: provides strict tool/JSON structured completion with two retries and no secret-bearing errors.
- `src/dream/publication.py`: owns per-user version state and legal publication transitions.
- `src/dream/writeback.py`: independently renders and validates Character Definition and User Persona artifacts.
- `src/dream/closed_loop.py`: coordinates review, Curators, snapshots, writeback candidates, failure fallback, and activation.
- `src/dream/validation/agnes.py`: generates one synthetic user message from an in-memory hidden persona without persisting it.
- `src/dream/validation/seeds.py`: imports manually selected public AI seed examples as AI-only events.
- `src/dream/validation/evaluation.py`: combines structural checks, gold-persona comparison inputs, human scores, and before/after task results.
- `src/dream/api.py`: exposes raw NDJSON import and manual review/writeback lifecycle endpoints; applies the optional next-task barrier.

---

### Task 1: Manual completed-task JSONL adapter

**Files:**
- Create: `src/dream/sources/manual.py`
- Create: `tests/sources/test_manual.py`
- Modify: `src/dream/service.py`
- Modify: `src/dream/api.py`
- Modify: `tests/test_api_e2e.py`

**Interfaces:**
- Consumes: UTF-8 NDJSON with `event_id`, scope IDs, `session_id`, `task_id`, `completed_at`, `messages`, and `final_response`.
- Produces: `ManualConversationRecord`, `parse_manual_ndjson(text: str) -> tuple[ManualConversationRecord, ...]`, `manual_record_to_event(record) -> TaskCompletedEvent`, and `DreamService.import_manual_ndjson(text) -> dict[str, int]`.

- [ ] **Step 1: Write failing contract tests**

```python
def test_manual_record_requires_complete_task_and_matching_final_response() -> None:
    record = parse_manual_ndjson(json.dumps({
        "event_id": "evt_project_manager_001",
        "tenant_id": "dream-lab",
        "agent_id": "enterprise-colleague",
        "user_id": "project-manager",
        "session_id": "session-001",
        "task_id": "task-001",
        "completed_at": "2026-07-17T10:00:00+08:00",
        "messages": [
            {"role": "user", "content": "先告诉我结论。"},
            {"role": "assistant", "content": "结论：接口尚待联调。"},
        ],
        "final_response": "结论：接口尚待联调。",
    }))
    assert record[0].task_id == "task-001"


def test_manual_record_rejects_mismatched_final_response() -> None:
    with pytest.raises(ManualSourceError, match="last assistant"):
        parse_manual_ndjson(valid_line(final_response="different"))


def test_manual_record_rejects_hidden_persona_fields() -> None:
    with pytest.raises(ManualSourceError, match="line 1"):
        parse_manual_ndjson(valid_line(hidden_persona={"role": "manager"}))
```

- [ ] **Step 2: Run the source tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider tests/sources/test_manual.py
```

Expected: collection fails because `dream.sources.manual` does not exist.

- [ ] **Step 3: Implement strict models and event mapping**

Use `ConfigDict(extra="forbid")`, nonblank validators, timezone validation, at least one user and one assistant message, and this final-response check:

```python
@model_validator(mode="after")
def completed_task_is_consistent(self) -> "ManualConversationRecord":
    assistants = [message.content for message in self.messages if message.role == "assistant"]
    if not assistants:
        raise ValueError("completed task requires an assistant message")
    if assistants[-1] != self.final_response:
        raise ValueError("final_response must equal the last assistant message")
    if not any(message.role == "user" for message in self.messages):
        raise ValueError("completed task requires a user message")
    return self
```

Map without summaries or hidden fields:

```python
def manual_record_to_event(record: ManualConversationRecord) -> TaskCompletedEvent:
    return TaskCompletedEvent(
        event_id=record.event_id,
        task_id=record.task_id,
        scope=ScopeIds(record.tenant_id, record.agent_id, record.user_id),
        completed_at=record.completed_at,
        interrupted=False,
        tool_iterations=10,
        transcript=tuple(message.model_dump() for message in record.messages),
        final_response=record.final_response,
        source_refs=({"source": "manual-character-ai", "session_id": record.session_id},),
    )
```

- [ ] **Step 4: Add idempotent service import and raw NDJSON API**

Add this service contract:

```python
def import_manual_ndjson(self, text: str) -> dict[str, int]:
    imported = duplicates = 0
    for record in parse_manual_ndjson(text):
        event = manual_record_to_event(record)
        if self.ledger.contains(event.event_id):
            duplicates += 1
            continue
        self.ingest_conversation(event)
        imported += 1
    return {"imported": imported, "duplicates": duplicates}
```

Expose `POST /v1/validation/import` with a raw `str` body using media type `application/x-ndjson`; return HTTP 422 for `ManualSourceError` and never echo the invalid body.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider tests/sources/test_manual.py tests/test_api_e2e.py
```

Expected: all selected tests pass; importing the same line twice reports one duplicate and leaves one ledger event.

- [ ] **Step 6: Commit the manual adapter**

```bash
git add src/dream/sources/manual.py src/dream/service.py src/dream/api.py tests/sources/test_manual.py tests/test_api_e2e.py
git commit -m "feat: import manual Character AI tasks"
```

### Task 2: Durable Background Review progress and restart recovery

**Files:**
- Create: `src/dream/review/progress.py`
- Create: `tests/review/test_progress.py`
- Modify: `src/dream/scheduler.py`
- Modify: `src/dream/service.py`
- Modify: `tests/test_scheduler.py`
- Modify: `tests/test_review_application.py`

**Interfaces:**
- Consumes: append-only `EventLedger` events.
- Produces: `ReviewProgressStore.contains(event_id)`, `append(event_id)`, `DreamService.recover_pending()`, and scope-filtered `run_pending(ids: ScopeIds | None = None)`.

- [ ] **Step 1: Write restart and no-change tests**

```python
def test_restart_recovers_only_unprocessed_events(tmp_path: Path) -> None:
    first = DreamService(tmp_path)
    first.ingest_conversation(event("evt-1", user="alice"))
    first.run_pending()
    first.ingest_conversation(event("evt-2", user="alice"))

    restarted = DreamService(tmp_path)

    assert restarted.scheduler.pending_event_ids() == ("evt-2",)


def test_successful_no_change_review_is_still_durable(tmp_path: Path) -> None:
    service = DreamService(tmp_path)
    service.ingest_conversation(event("evt-no-change", text="ordinary chat"))
    result = service.run_pending()
    assert result[0]["status"] == "success"
    assert service.review_progress.contains("evt-no-change")
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider tests/review/test_progress.py tests/test_review_application.py
```

Expected: FAIL because review progress is not durable and constructor recovery is absent.

- [ ] **Step 3: Implement append-only progress persistence**

Write one fsynced JSON line per accepted event:

```python
class ReviewProgressStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()

    def read_all(self) -> tuple[str, ...]:
        if not self.path.exists():
            return ()
        return tuple(dict.fromkeys(
            str(json.loads(line)["event_id"])
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ))

    def contains(self, event_id: str) -> bool:
        return event_id in self.read_all()

    def append(self, event_id: str) -> None:
        with self._lock:
            if self.contains(event_id):
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event_id": event_id}) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
```

- [ ] **Step 4: Restore unprocessed events and support scope filtering**

Add `DreamScheduler.enqueue_unless_pending(event)` and `pop_pending(scope=None)`. The scoped pop must rotate through the queue without dropping other scopes. In `DreamService.__init__`, enqueue every ledger event not present in `ReviewProgressStore`. Mark progress only after a nonfailed review report has been written; a failed review remains recoverable and must not be marked processed.

Return the event identity from every run:

```python
runs.append({
    "run_id": run_id,
    "source_event_ids": [event.event_id],
    "status": status,
    "artifact_kinds": applied_kinds,
    "errors": errors,
})
```

- [ ] **Step 5: Run recovery tests and verify GREEN**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_scheduler.py tests/review/test_progress.py tests/test_review_application.py
```

Expected: all selected tests pass; events from another scope remain pending after a scoped run.

- [ ] **Step 6: Commit durable review progress**

```bash
git add src/dream/review/progress.py src/dream/scheduler.py src/dream/service.py tests/review/test_progress.py tests/test_scheduler.py tests/test_review_application.py
git commit -m "feat: persist completed dream reviews"
```

### Task 3: Structured LLM compatibility and retry policy

**Files:**
- Create: `src/dream/structured_llm.py`
- Create: `tests/test_structured_llm.py`
- Modify: `src/dream/review/llm_backend.py`
- Modify: `src/dream/curators/llm_backend.py`
- Modify: `tests/review/test_llm_backend.py`
- Modify: `tests/curators/test_llm_backend.py`
- Modify: `src/dream/config.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: an OpenAI-compatible `client.chat.completions.create` method.
- Produces: `StructuredToolCall` and `StructuredCompletionClient.call(system, content, tools, forced_tool, mode) -> tuple[StructuredToolCall, ...]`, supporting multiple review actions plus `tools`, `json`, and `auto` modes with three total attempts.

- [ ] **Step 1: Write tool, JSON fallback, retry, and redaction tests**

```python
def test_auto_mode_falls_back_to_json_when_tools_are_unsupported() -> None:
    completions = RejectToolsThenReturnJson({"summary": "ok"})
    result = StructuredCompletionClient(client_for(completions), "agnes", 3).call(
        system="Return the schema.", content="input", tools=(SUMMARY_TOOL,),
        forced_tool="summarize", mode="auto"
    )
    assert result[0].arguments == {"summary": "ok"}
    assert completions.calls == 2


def test_structured_call_stops_after_initial_attempt_plus_two_retries() -> None:
    completions = AlwaysFail("Authorization: Bearer secret")
    with pytest.raises(StructuredCompletionError) as error:
        StructuredCompletionClient(client_for(completions), "agnes", 3).call(
            system="system", content="input", tools=(SUMMARY_TOOL,),
            forced_tool="summarize", mode="tools"
        )
    assert completions.calls == 3
    assert "secret" not in str(error.value)
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_structured_llm.py
```

Expected: FAIL because `dream.structured_llm` does not exist.

- [ ] **Step 3: Implement the common structured client**

The client must force `forced_tool` when it is not `None`, allow multiple tool calls when it is `None`, request `response_format={"type": "json_object"}` in JSON mode, and expose only safe error categories. JSON mode uses the exact envelope `{"tool_calls":[{"name":"...","arguments":{...}}]}` so the review backend can still propose both management actions in one response. Every name must match one of the supplied tool schemas and every `arguments` value must be an object:

```python
@dataclass(frozen=True)
class StructuredToolCall:
    name: str
    arguments: dict[str, object]


class StructuredCompletionClient:
    def __init__(self, client: Any, model: str, max_attempts: int = 3) -> None:
        if max_attempts != 3:
            raise ValueError("validation uses exactly three total attempts")
        self.client = client
        self.model = model
        self.max_attempts = max_attempts

    def call(self, *, system: str, content: str,
             tools: tuple[dict[str, object], ...],
             forced_tool: str | None,
             mode: str) -> tuple[StructuredToolCall, ...]:
        modes = ("tools", "json") if mode == "auto" else (mode,)
        last_category = "structured completion failed"
        attempts = 0
        for selected in modes:
            while attempts < self.max_attempts:
                attempts += 1
                try:
                    return self._call_once(
                        system, content, tools, forced_tool, selected
                    )
                except Exception as exc:
                    last_category = type(exc).__name__
                    if selected == "tools" and mode == "auto":
                        break
        raise StructuredCompletionError(last_category)
```

Refine the loop so total attempts never exceeds three and malformed JSON is retried. `_call_once` must validate the returned function name in tool mode and reject non-object JSON in both modes.

- [ ] **Step 4: Refactor both existing semantic backends to use the client**

Keep their public methods unchanged. Add `structured_mode: str = "auto"` to constructors. `OpenAIReviewBackend` passes all allowed management schemas with `forced_tool=None` and maps every returned `StructuredToolCall`; `OpenAICuratorBackend` passes one schema with its exact forced tool name. Validate required keys before building `ReviewAction`, `AICurationPlan`, or `UserCurationPlan`. Add `DREAM_LLM_STRUCTURED_MODE=auto` to configuration and pass it to both backends.

- [ ] **Step 5: Run all LLM backend tests and verify GREEN**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_structured_llm.py tests/review/test_llm_backend.py tests/curators/test_llm_backend.py tests/test_config.py
```

Expected: all selected tests pass in forced-tool, JSON-only, and automatic modes.

- [ ] **Step 6: Commit structured provider support**

```bash
git add src/dream/structured_llm.py src/dream/review/llm_backend.py src/dream/curators/llm_backend.py src/dream/config.py .env.example tests/test_structured_llm.py tests/review/test_llm_backend.py tests/curators/test_llm_backend.py tests/test_config.py
git commit -m "feat: support structured Agnes responses"
```

### Task 4: Publication version state machine

**Files:**
- Create: `src/dream/publication.py`
- Create: `tests/test_publication.py`
- Modify: `src/dream/service.py`

**Interfaces:**
- Consumes: scope paths, source event IDs, snapshot IDs, artifact hashes, and manual writeback confirmations.
- Produces: `PublicationStatus`, `PublicationVersion`, `PublicationStore.begin`, `mark_ready_for_review`, `approve`, `confirm_writeback`, `activate`, `fail`, `active`, and `latest`.

- [ ] **Step 1: Write legal-transition and persistence tests**

```python
def test_publication_requires_both_manual_writebacks_before_activation(tmp_path: Path) -> None:
    store = PublicationStore(paths(tmp_path, "python-beginner"))
    version = store.begin(("evt-1",), "evt-1", "before-snapshot")
    version = store.mark_ready_for_review(version.version, "after-snapshot", "char-sha", "user-sha")
    version = store.approve(version.version)
    version = store.confirm_writeback(version.version, character_written=True, user_written=False)
    with pytest.raises(PublicationTransitionError, match="both writebacks"):
        store.activate(version.version)


def test_failed_candidate_keeps_previous_active_version(tmp_path: Path) -> None:
    store = active_v1_store(tmp_path)
    candidate = store.begin(("evt-2",), "evt-2", "before-v2")
    store.fail(candidate.version, "curator failed")
    assert store.active().version == 1
    assert store.latest().status is PublicationStatus.FAILED
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_publication.py
```

Expected: FAIL because `dream.publication` does not exist.

- [ ] **Step 3: Implement immutable version records and transition validation**

Use these exact statuses:

```python
class PublicationStatus(StrEnum):
    PENDING = "pending"
    DREAMING = "dreaming"
    READY_FOR_REVIEW = "ready_for_review"
    READY_FOR_WRITEBACK = "ready_for_writeback"
    ACTIVE = "active"
    FAILED = "failed"
```

`PublicationVersion` must persist: `version`, `status`, `source_event_ids`, `processed_through_event_id`, `before_snapshot_id`, `after_snapshot_id`, both output hashes, both writeback booleans, `created_at`, `activated_at`, `failure_reason`, and `fallback_version`. Store one atomically replaced state record at `publication/users/<user_id>/versions/000001.json` for each version; never delete an older version. Store `latest.json` and `active.json` as small atomic pointers.

Legal transitions are:

```python
_ALLOWED = {
    PublicationStatus.PENDING: {PublicationStatus.DREAMING, PublicationStatus.FAILED},
    PublicationStatus.DREAMING: {PublicationStatus.READY_FOR_REVIEW, PublicationStatus.FAILED},
    PublicationStatus.READY_FOR_REVIEW: {PublicationStatus.READY_FOR_WRITEBACK, PublicationStatus.FAILED},
    PublicationStatus.READY_FOR_WRITEBACK: {PublicationStatus.ACTIVE, PublicationStatus.FAILED},
    PublicationStatus.ACTIVE: set(),
    PublicationStatus.FAILED: set(),
}
```

- [ ] **Step 4: Mark ingested validation scopes pending without changing active state**

In `DreamService.ingest_conversation`, call `PublicationStore(paths).note_completed_event(event.event_id)`. This writes `publication/users/<user_id>/pending.json` containing the ordered unprocessed event IDs, but never changes `active.json`.

- [ ] **Step 5: Run publication tests and verify GREEN**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_publication.py tests/test_review_application.py
```

Expected: all selected tests pass; failed and partial versions cannot replace the active pointer.

- [ ] **Step 6: Commit the state machine**

```bash
git add src/dream/publication.py src/dream/service.py tests/test_publication.py tests/test_review_application.py
git commit -m "feat: add dream publication versions"
```

### Task 5: Bounded Character Definition and User Persona rendering

**Files:**
- Create: `src/dream/writeback.py`
- Create: `src/dream/curators/writeback_prompts.py`
- Create: `tests/test_writeback.py`
- Modify: `src/dream/config.py`
- Modify: `src/dream/snapshots.py`
- Modify: `.env.example`
- Modify: `tests/test_snapshots.py`

**Interfaces:**
- Consumes: only `DECISION_RULES.md` for AI rendering and only the current user's `USER.md` for persona rendering.
- Produces: `WritebackBackend`, `OpenAIWritebackBackend`, `DeterministicWritebackBackend`, `WritebackArtifacts`, `WritebackService.generate_character(ids) -> ArtifactVersion`, and `WritebackService.generate(ids) -> WritebackArtifacts`.

- [ ] **Step 1: Write isolation, bounds, citation, and atomicity tests**

```python
def test_writeback_backend_never_receives_profile_when_rendering_character(tmp_path: Path) -> None:
    backend = RecordingWritebackBackend()
    service = writeback_service(tmp_path, backend)
    service.generate(ScopeIds("dream-lab", "enterprise-colleague", "project-manager"))
    assert backend.character_input == "# AI Decision Rules\n\n- 先给结论。\n"
    assert "项目经理" not in backend.character_input
    assert backend.persona_input.startswith("用户偏好")


def test_oversized_writeback_is_rejected_without_replacing_stable_files(tmp_path: Path) -> None:
    service = writeback_service(tmp_path, OversizedBackend())
    with pytest.raises(WritebackValidationError, match="limit"):
        service.generate(ScopeIds("dream-lab", "enterprise-colleague", "project-manager"))
    assert stable_character(tmp_path) == "stable character\n"
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_writeback.py
```

Expected: FAIL because `dream.writeback` does not exist.

- [ ] **Step 3: Implement separate rendering calls**

Define a backend that makes two structurally separate calls:

```python
class WritebackBackend(Protocol):
    def render_character(self, decision_rules: str, limit: int) -> str: ...
    def render_user_persona(self, user_profile: str, limit: int) -> str: ...
```

`OpenAIWritebackBackend` must use `StructuredCompletionClient` and two distinct prompts. The Character prompt must say that no user profile is provided and all rules must be user-agnostic. The User Persona prompt must preserve only evidence-supported service preferences and must not invent unobserved identity facts.

`generate_character(ids)` reads only shared `DECISION_RULES.md` and writes only `CHARACTER_DEFINITION.md`; use it for the initial AI seed bootstrap before any real simulated user exists. `generate(ids)` calls `generate_character(ids)` and separately renders the scoped user persona.

- [ ] **Step 4: Validate before two-file publication**

Generate both strings in memory, reject blank/NUL/oversized output, require at least one `dream-source` or `Evidence cards` marker in the corresponding full source, capture a rollback snapshot for both target paths, and only then atomically write:

```text
CHARACTER_DEFINITION.md
users/<user_id>/USER_PERSONA.md
```

Return SHA-256 values, byte counts, and the rollback snapshot ID. Add `DREAM_CHARACTER_DEFINITION_LIMIT=3200` and `DREAM_USER_PERSONA_LIMIT=1200` to `.env.example` and strict positive integer parsing to `config.py`.

Extend `SnapshotStore._relative_files` to include `CHARACTER_DEFINITION.md` and `users/<user_id>/USER_PERSONA.md`. Add `restore(snapshot_id, ids)` that validates the manifest scope, removes decision cards created after the snapshot, and atomically restores every captured file. This restore method is used only by the closed-loop coordinator; the existing mutation rollback API remains unchanged.

- [ ] **Step 5: Run writeback tests and verify GREEN**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_writeback.py tests/test_config.py tests/test_snapshots.py
```

Expected: all selected tests pass; user facts never enter the Character renderer input.

- [ ] **Step 6: Commit bounded writeback artifacts**

```bash
git add src/dream/writeback.py src/dream/curators/writeback_prompts.py src/dream/config.py src/dream/snapshots.py .env.example tests/test_writeback.py tests/test_config.py tests/test_snapshots.py
git commit -m "feat: generate bounded writeback artifacts"
```

### Task 6: Closed-loop coordinator, failure fallback, and next-task barrier

**Files:**
- Create: `src/dream/closed_loop.py`
- Create: `tests/test_closed_loop.py`
- Modify: `src/dream/service.py`
- Modify: `src/dream/api.py`
- Modify: `src/dream/config.py`
- Modify: `.env.example`
- Modify: `tests/test_api_e2e.py`

**Interfaces:**
- Consumes: pending event IDs, `DreamService.run_pending(ids)`, both Curators, `WritebackService`, snapshots, and `PublicationStore`.
- Produces: `ClosedLoopCoordinator.dream(ids)`, `approve(ids, version)`, `confirm_writeback(ids, version, ...)`, `activate(ids, version)`, `status(ids)`, and `assert_task_can_start(ids)`.

- [ ] **Step 1: Write a complete two-task barrier test**

```python
def test_next_task_waits_for_latest_event_to_be_active(tmp_path: Path) -> None:
    coordinator, service = closed_loop(tmp_path)
    ids = ScopeIds("dream-lab", "enterprise-colleague", "python-beginner")
    service.ingest_conversation(event("evt-1", ids))

    with pytest.raises(TaskStartBlocked, match="evt-1"):
        coordinator.assert_task_can_start(ids)

    candidate = coordinator.dream(ids)
    coordinator.approve(ids, candidate.version)
    coordinator.confirm_writeback(ids, candidate.version, True, True)
    active = coordinator.activate(ids, candidate.version)

    assert active.processed_through_event_id == "evt-1"
    coordinator.assert_task_can_start(ids)
```

Also test no-change advancement, Curator failure, renderer failure, one missing writeback, hash-identical output that requires no repeated paste, candidate rejection, and rollback to the previous active version.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_closed_loop.py
```

Expected: FAIL because `dream.closed_loop` does not exist.

- [ ] **Step 3: Implement one coordinator transaction boundary**

The `dream` method must execute in this order:

```python
def dream(self, ids: ScopeIds) -> PublicationVersion:
    pending = self.publications(ids).pending_event_ids()
    if not pending:
        raise ValueError("no completed events are waiting for a dream")
    before = self.snapshots(ids).create(ids)
    version = self.publications(ids).begin(
        pending, pending[-1], before.snapshot_id
    )
    try:
        version = self.publications(ids).mark_dreaming(version.version)
        reviews = self.service.run_pending(ids)
        if any(run["status"] == "failed" for run in reviews):
            raise RuntimeError("background review failed")
        self.service.run_curators(ids)
        writeback = self.writebacks.generate(ids)
        after = self.snapshots(ids).create(ids)
        return self.publications(ids).mark_ready_for_review(
            version.version,
            after.snapshot_id,
            writeback.character.sha256,
            writeback.user_persona.sha256,
        )
    except Exception as exc:
        self.snapshots(ids).restore(before.snapshot_id, ids)
        self.publications(ids).fail(version.version, type(exc).__name__)
        raise ClosedLoopError("dream candidate failed") from exc
```

Reports must include the version number, source event IDs, both snapshot IDs, both artifact hashes, errors, and fallback version. Error text must not include API keys, hidden personas, or upstream response bodies.

- [ ] **Step 4: Add manual lifecycle endpoints**

Expose:

```text
POST /v1/validation/dream
POST /v1/validation/publications/{version}/approve
POST /v1/validation/publications/{version}/confirm-writeback
POST /v1/validation/publications/{version}/activate
POST /v1/validation/publications/{version}/reject
POST /v1/validation/publications/{version}/rollback
GET  /v1/validation/publications/status
```

All requests carry `tenant_id`, `agent_id`, and `user_id`. `confirm_writeback` accepts exactly two booleans. When a candidate artifact hash equals the corresponding artifact in the active version, the coordinator records that writeback as satisfied without requiring another paste. Rejecting a candidate restores `before_snapshot_id` and marks the candidate failed. Rolling back restores the selected previous version's `after_snapshot_id`, changes `active.json` to that version, and writes a rollback report without deleting later version records. Return HTTP 409 for illegal transitions and HTTP 503 when candidate generation fails.

- [ ] **Step 5: Add an opt-in barrier to `/v1/tasks/start`**

Add `DREAM_VALIDATION_REQUIRE_ACTIVE_WRITEBACK=false`. When true, call `coordinator.assert_task_can_start(ids)` before creating the context snapshot; return HTTP 409 with `latest_completed_event_id`, `active_processed_through_event_id`, and safe next action. Existing nonvalidation behavior remains unchanged when false. During phase-one blind collection, keep this setting false, collect the first five tasks for each user without publishing `USER_PERSONA.md`, score the first profiles, publish version 1, then restart with the barrier true for phase two and phase three.

- [ ] **Step 6: Run closed-loop and API tests and verify GREEN**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_closed_loop.py tests/test_api_e2e.py tests/test_publication.py tests/test_review_application.py
```

Expected: all selected tests pass; a failed candidate never changes `active.json`.

- [ ] **Step 7: Commit the lifecycle**

```bash
git add src/dream/closed_loop.py src/dream/service.py src/dream/api.py src/dream/config.py .env.example tests/test_closed_loop.py tests/test_api_e2e.py
git commit -m "feat: enforce the dream writeback lifecycle"
```

### Task 7: Isolated Agnes user simulation and AI-only seed ingestion

**Files:**
- Create: `src/dream/validation/__init__.py`
- Create: `src/dream/validation/agnes.py`
- Create: `src/dream/validation/seeds.py`
- Create: `tests/validation/__init__.py`
- Create: `tests/validation/test_agnes.py`
- Create: `tests/validation/test_seeds.py`
- Create: `tests/fixtures/personas/project_manager.gold.json`
- Create: `tests/fixtures/personas/python_beginner.gold.json`
- Create: `tests/fixtures/personas/technical_lead.gold.json`
- Create: `tests/fixtures/ai_seed/sample.jsonl`
- Modify: `.gitignore`
- Modify: `src/dream/service.py`

**Interfaces:**
- Consumes: one hidden persona object in memory and one public Character.AI transcript; separately consumes selected public seed records.
- Produces: `AgnesSimulator.next_user_message(...) -> str`, `parse_seed_jsonl(text)`, `validate_seed_file(path, expected_count)`, a `dream.validation.seeds:main` CLI, and AI-only seed events.

- [ ] **Step 1: Write strict isolation and seed-routing tests**

```python
def test_simulator_returns_only_the_user_message_and_persists_nothing(tmp_path: Path) -> None:
    simulator = AgnesSimulator(fake_client("请先给出结论。"), "agnes-model")
    message = simulator.next_user_message(
        hidden_persona={"role": "project manager", "preference": "conclusions first"},
        public_history=(),
        task_number=1,
    )
    assert message == "请先给出结论。"
    assert list(tmp_path.rglob("*")) == []


def test_ai_seed_can_create_cards_but_never_user_profile(tmp_path: Path) -> None:
    service = DreamService(tmp_path, backend=SeedAwareBackend())
    service.import_ai_seed_jsonl(seed_line())
    service.run_pending()
    root = resolve_scope(tmp_path, seed_scope()).agent_root
    assert list((root / "decision-cards").glob("*.md"))
    assert not (root / "users" / "seed-only" / "USER.md").exists()
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider tests/validation/test_agnes.py tests/validation/test_seeds.py
```

Expected: FAIL because the validation modules do not exist.

- [ ] **Step 3: Implement an in-memory-only simulator request**

`AgnesSimulator` receives no filesystem object and exposes no logging callback. Build a fresh request for every message, pass the hidden persona only inside that request, include public history without DREAM artifacts, and return one nonblank string. Reject tool calls, JSON objects, and assistant-prefixed output.

Use three synthetic gold files with these stable trait sets:

```json
{"user_id":"project-manager","traits":["conclusion_first","progress_owner_risk"]}
{"user_id":"python-beginner","traits":["step_by_step","examples","low_jargon"]}
{"user_id":"technical-lead","traits":["evidence","boundaries","rollback"]}
```

Gold files are passed only to the simulator/evaluator constructors and never to `DreamService`.

- [ ] **Step 4: Implement AI-only seed records**

Seed records contain `source_dataset`, `source_record_id`, `scenario`, and `assistant_response`. Map them to the fixed `seed-only` user with `source_refs=({"source": "ai-seed", "source_dataset": record.source_dataset, "source_record_id": record.source_record_id},)`. In `DreamService.run_pending`, detect this source and set:

```python
allowed_tools = (
    frozenset({"decision_card_manage"})
    if any(ref.get("source") == "ai-seed" for ref in event.source_refs)
    else frozenset({"memory_manage", "decision_card_manage"})
)
```

The committed `sample.jsonl` contains one synthetic HelpSteer-style helpfulness example and one synthetic safe-boundary example for automated tests; it must not claim to reproduce dataset text.

Implement `python -m dream.validation.seeds validate <path> --expected-count <n>` with `argparse`; exit 0 only when every line validates, IDs are unique, the count matches exactly, and no user-profile or hidden-persona field exists.

After seed import, run scoped Background Review with decision-card-only tools, run `AICurator`, and call `WritebackService.generate_character(seed_scope)`; do not generate a `USER_PERSONA.md` or require a seed-user publication version.

- [ ] **Step 5: Run validation-source tests and verify GREEN**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider tests/validation/test_agnes.py tests/validation/test_seeds.py tests/test_review_application.py
```

Expected: all selected tests pass and seed ingestion cannot create `USER.md`.

- [ ] **Step 6: Operationally select the initial 30 samples**

Create a local, non-secret `tests/fixtures/ai_seed/selected.local.jsonl` containing exactly 30 manually reviewed records: 20 from HelpSteer2 emphasizing correctness/relevance/helpfulness and 10 from PKU-SafeRLHF emphasizing helpful boundaries. Store dataset name and stable source row ID in every line; do not copy hidden test labels into the event transcript. Add this exact path to `.gitignore` so the selected dataset text remains local while the synthetic `sample.jsonl` remains committed. Verify:

```bash
../.venv/bin/python -m dream.validation.seeds validate tests/fixtures/ai_seed/selected.local.jsonl --expected-count 30
```

Expected: `30 valid AI-only seed records; 0 user-profile fields`.

- [ ] **Step 7: Commit simulator and seed support**

```bash
git add src/dream/validation tests/validation tests/fixtures/personas tests/fixtures/ai_seed/sample.jsonl .gitignore src/dream/service.py
git commit -m "feat: add isolated validation data sources"
```

### Task 8: Evaluation report and three-phase acceptance harness

**Files:**
- Create: `src/dream/validation/evaluation.py`
- Create: `tests/validation/test_evaluation.py`
- Create: `tests/fixtures/conversations/project_manager.jsonl`
- Create: `tests/fixtures/conversations/python_beginner.jsonl`
- Create: `tests/fixtures/conversations/technical_lead.jsonl`
- Create: `tests/evaluation/.gitkeep`
- Modify: `tests/test_api_e2e.py`

**Interfaces:**
- Consumes: gold trait keys, original conversation evidence, `USER.md`, decision cards, publication manifests, and human before/after scores.
- Produces: `EvaluationReport`, `evaluate_validation_run(...)`, `verify_report(path)`, a `dream.validation.evaluation:main` CLI, and `tests/evaluation/latest.json`.

- [ ] **Step 1: Write metric boundary tests**

```python
def test_acceptance_requires_evidence_personalization_and_zero_leakage() -> None:
    report = evaluate_validation_run(run_fixture(
        supported_profile_facts=17,
        total_profile_facts=20,
        severe_hallucinations=0,
        cross_user_leaks=0,
        personalized_successes=8,
        personalized_tasks=10,
        evolved_ai_successes=8,
        evolved_ai_tasks=10,
    ))
    assert report.profile_evidence_rate == 0.85
    assert report.personalization_rate == 0.80
    assert report.ai_evolution_rate == 0.80
    assert report.passed is True
```

Also test that one severe hallucination, one cross-user leak, missing source IDs, incomplete writeback, or fewer than 10 tasks for any user fails acceptance.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider tests/validation/test_evaluation.py
```

Expected: FAIL because `dream.validation.evaluation` does not exist.

- [ ] **Step 3: Implement combined programmatic and human scoring**

Use exact thresholds from the design:

```python
passed = all((
    profile_evidence_rate >= 0.85,
    severe_hallucinations == 0,
    cross_user_leaks == 0,
    personalization_rate >= 0.80,
    ai_evolution_rate >= 0.80,
    all(user.task_count >= 10 for user in users),
    completed_dream_writeback_cycles >= 2,
    change_conflict_case_passed,
    failure_fallback_or_rollback_passed,
))
```

Programmatic checks count citations, source event existence, scope leakage, publication status, writeback hashes, and task counts. Human input records evidence support, personalization success, AI behavior success, severe hallucinations, and the preference-change case. Agnes output may be included as one advisory field but cannot alone set `passed=True`.

Implement `python -m dream.validation.evaluation verify <path>` with `argparse`; print the computed rates and return exit code 0 only when `EvaluationReport.passed` is true.

- [ ] **Step 4: Add representative offline fixtures and an end-to-end state test**

Each committed conversation fixture contains two synthetic tasks for fast automated testing, including one explicit preference change. The operational run appends until each file contains 10-15 completed tasks. The E2E test must exercise two complete publication cycles and one failed candidate while verifying that the first active version remains usable.

- [ ] **Step 5: Run evaluation and E2E tests and verify GREEN**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider tests/validation/test_evaluation.py tests/test_api_e2e.py tests/test_closed_loop.py
```

Expected: all selected tests pass; the small synthetic fixture is structurally valid but is reported as incomplete for the full 10-task acceptance threshold.

- [ ] **Step 6: Commit the evaluation harness**

```bash
git add src/dream/validation/evaluation.py tests/validation/test_evaluation.py tests/fixtures/conversations tests/evaluation tests/test_api_e2e.py
git commit -m "test: add closed-loop evolution evaluation"
```

### Task 9: Operator runbook, full verification, and first real validation run

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Create: `docs/validation/character-ai-runbook.md`
- Create at runtime: `tests/evaluation/latest.json`

**Interfaces:**
- Consumes: implemented validation endpoints and the approved design.
- Produces: a reproducible manual Character.AI procedure and one inspectable evaluation report.

- [ ] **Step 1: Document the exact operator sequence**

The runbook must show these commands and manual gates in order:

```text
1. Validate 30 AI seed records and generate initial decision rules.
2. Generate CHARACTER_DEFINITION.md and paste it into Character Definition.
3. Keep the active-writeback barrier off only for phase-one blind collection.
4. For each user, ask Agnes for one synthetic user message.
5. Manually obtain the Character.AI answer and append the complete task to JSONL.
6. After five tasks per user, import the batch, dream, and score USER.md before any User Persona writeback.
7. Approve version 1, paste both writeback files, confirm both, and activate the barrier.
8. Open a brand-new chat for every later task; dream and activate before the following task.
9. Repeat until every user has 10-15 tasks and at least two cycles are complete.
10. Record human evidence and before/after scores; generate latest.json.
11. Demonstrate one preference change and one failure fallback or rollback.
```

Include safe `curl` examples for every endpoint. State explicitly that hidden persona files are never uploaded to DREAM or Character.AI.

- [ ] **Step 2: Run the complete automated suite**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest -q -p no:cacheprovider
```

Expected: all tests pass with zero failures.

- [ ] **Step 3: Run static checks**

Run:

```bash
../.venv/bin/python -m ruff check src tests
../.venv/bin/python -m compileall -q src/dream
```

Expected: both commands exit 0 with no Ruff diagnostics or compile errors.

- [ ] **Step 4: Execute the three-phase manual validation**

Use the runbook with all three synthetic users. Do not unlock phase two until phase-one profile facts are scored against actual conversation evidence. Save the generated report at `tests/evaluation/latest.json`; verify it records three users, at least 10 tasks each, two active cycles, the explicit preference-change result, and the fallback/rollback result.

- [ ] **Step 5: Verify acceptance data and disk artifacts**

Run:

```bash
../.venv/bin/python -m dream.validation.evaluation verify tests/evaluation/latest.json
```

Expected for completion: `PASS` followed by evidence rate `>= 0.85`, personalization `>= 0.80`, AI evolution `>= 0.80`, severe hallucinations `0`, and cross-user leaks `0`.

- [ ] **Step 6: Commit documentation and non-sensitive evaluation output**

Before committing, inspect `latest.json` and confirm that it contains only synthetic user IDs, aggregate metrics, event IDs, and artifact hashes. Then run:

```bash
git add README.md .env.example docs/validation/character-ai-runbook.md tests/evaluation/latest.json
git commit -m "docs: add Character AI validation runbook"
```

## Final Acceptance Checklist

- [ ] Three synthetic users each completed at least 10 full tasks.
- [ ] Each evaluated user-profile fact is linked to an actual conversation event.
- [ ] Cross-user leaks and severe hallucinations are both zero.
- [ ] AI decision cards contain no specific user's private facts.
- [ ] At least two dream-review-writeback-new-chat cycles are active and inspectable.
- [ ] One explicit preference change replaced or conflicted with the old value based on evidence.
- [ ] One failure preserved the previous stable version or one rollback restored it.
- [ ] Every active version has source IDs, before/after snapshots, artifact hashes, writeback confirmation, and activation time.
- [ ] The next-task barrier rejects stale or incomplete publication state.
- [ ] Agnes simulator secrets and hidden personas are absent from ledger, reports, snapshots, and writeback artifacts.
- [ ] Full pytest, Ruff, and compile verification pass from a clean checkout.
