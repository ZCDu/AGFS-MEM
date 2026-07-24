# DREAM Current Architecture README Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the outdated root README with an accurate, reviewer-friendly and developer-ready guide to the current DREAM memory framework.

**Architecture:** Documentation is organized from product value to runtime architecture, then from quick start to detailed integration. The README remains the entry point while API contracts, design history, and formal validation procedures stay in their existing documents under `docs/`.

**Tech Stack:** Markdown, Python 3.11–3.13, FastAPI, Uvicorn, pytest, Ruff.

## Global Constraints

- Modify documentation only; do not change application behavior or interfaces.
- Use `DREAM` consistently as the project name.
- Describe the current `application`, `core`, `extraction`, `governance`, `memory`, `retrieval`, `curators`, and `integrations` layers.
- State that `MemoryRetrievalSkill` is a read-only Python runtime API, not a FastAPI endpoint.
- State that workflow Skills remain candidates and are not an active Skill Runtime.
- Do not expose a real provider API key, personal runtime path, or `validation-run` data.
- Do not hardcode the current number of passing tests.

---

### Task 1: Rewrite the root README

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: public APIs from `src/dream/api.py`, `MemoryRetrievalSkill.retrieve()` from `src/dream/retrieval/skill.py`, environment settings from `.env.example`, and the current source package layout.
- Produces: the canonical project entry document for reviewers and integrators.

- [ ] **Step 1: Replace the stale introduction and architecture**

Write an opening that defines DREAM as an Agent long-term memory formation,
governance, and retrieval framework. Present these three core capabilities:

```text
User Persona
AI Decision Evolution
Task-Relevant Memory Retrieval
```

Add the current end-to-end flow:

```text
Conversation
    ↓
Knowledge Extraction
    ↓
Knowledge Governance
    ↓
Persona / Decision Cards / Skill Candidates
    ↓
Snapshots and Versioned Writeback
    ↓
Memory Retrieval
    ↓
External Agent
```

- [ ] **Step 2: Replace the project tree and artifact descriptions**

Document the current source layers and the `DREAM_HOME` artifact tree. The
source tree must include:

```text
src/dream/
├── api.py
├── config.py
├── application/
├── core/
├── extraction/
├── governance/
├── memory/
├── retrieval/
├── curators/
├── integrations/
└── validation/
```

Explain the user-scoped `USER.md` and `USER_PERSONA.md`, agent-scoped Decision
Cards and `DECISION_RULES.md`, publication versions, snapshots, reports, and
review traces.

- [ ] **Step 3: Document memory formation and governance**

Explain provider adaptation, canonical knowledge, Persona evolution, Decision
Card generation, and deterministic routing. Include the governance outcomes:

```text
low risk + sufficient evidence  → auto activate
uncertain or incomplete         → observe as candidate
high risk                       → require review
```

State that Skill candidates remain auditable candidates and are not callable
runtime Skills.

- [ ] **Step 4: Add the Retrieval Skill contract and example**

Document the exact constructor and call:

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
print(result.context)
```

Explain user isolation, artifact-type/domain filtering, deterministic ranking,
deduplication, conflict preference, Top-K, and context budget.

- [ ] **Step 5: Add quick start, API, PyCharm, and integration guidance**

Include exact commands:

```bash
python -m pip install -e '.[dev]'
cp .env.example .env
uvicorn dream.api:app --host 127.0.0.1 --port 8765
python -m pytest -q
ruff check src tests
```

Document the completed-conversation ingestion endpoint, manual NDJSON import,
validation Dream, publication review/activation branch, `/v1/tasks/start`, and
the separate Retrieval Python API. Include PyCharm interpreter, module,
parameters, working directory, and `DREAM_ENV_FILE` configuration.

- [ ] **Step 6: Add safety and repository hygiene**

Document the 300-second default total Dream deadline, provider timeout,
snapshot restore, failed publication, retry-safe pending events, semantic
Curator default-off behavior, and local-only files:

```text
.env
.env.*
.venv/
.idea/
validation-run/
__pycache__/
*.local.jsonl
```

Link detailed API, design, and validation documents instead of duplicating
their full content.

### Task 2: Validate README facts and references

**Files:**
- Verify: `README.md`
- Verify against: `src/dream/api.py`
- Verify against: `src/dream/retrieval/skill.py`
- Verify against: `.env.example`

**Interfaces:**
- Consumes: the rewritten README.
- Produces: evidence that documented paths, endpoints, settings, and examples match the repository.

- [ ] **Step 1: Reject obsolete source-layout references**

Run:

```bash
rg -n 'src/dream/(review|managers|hermes_compat)|`(service|scheduler|closed_loop|publication|snapshots|rollback|reports|source_sync)\.py`' README.md
```

Expected: no matches.

- [ ] **Step 2: Confirm required current concepts**

Run:

```bash
rg -n 'MemoryRetrievalSkill|Knowledge Governance|auto_activate|ready_for_review|DREAM_ENV_FILE|300|application/|extraction/|retrieval/' README.md
```

Expected: all required concepts appear.

- [ ] **Step 3: Check referenced local paths**

Run:

```bash
test -f src/dream/api.py
test -f src/dream/retrieval/skill.py
test -f src/dream/governance/policy.py
test -f docs/api/short-term-memory-contract.md
test -f docs/validation/codex-task-evolution-runbook.md
```

Expected: every command exits successfully.

- [ ] **Step 4: Check documented endpoints against FastAPI**

Run:

```bash
rg -n '/v1/(dream/conversations|tasks/start|validation/import|validation/dream|validation/publications)' src/dream/api.py README.md
```

Expected: every endpoint documented in the quick-start flow exists in
`src/dream/api.py`.

### Task 3: Run regression checks and commit

**Files:**
- Modify: `README.md`
- Verify: `docs/superpowers/specs/2026-07-24-readme-current-architecture-design.md`
- Verify: `docs/superpowers/plans/2026-07-24-readme-current-architecture.md`

**Interfaces:**
- Consumes: completed README and validation evidence.
- Produces: one documentation-only commit.

- [ ] **Step 1: Run project verification**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -p no:cacheprovider
.venv/bin/ruff check --no-cache src tests
git diff --check
```

Expected: pytest passes, Ruff reports `All checks passed!`, and
`git diff --check` produces no output.

- [ ] **Step 2: Confirm documentation-only scope**

Run:

```bash
git status --short
git diff --name-only
```

Expected: only `README.md` and this plan document are part of the current
documentation work.

- [ ] **Step 3: Commit**

Run:

```bash
git add README.md docs/superpowers/plans/2026-07-24-readme-current-architecture.md
git commit -m "docs: update readme for current architecture"
```

Expected: a documentation-only commit is created.
