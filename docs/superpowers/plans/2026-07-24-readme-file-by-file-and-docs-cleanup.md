# README File-by-File Structure and Docs Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Explain every `src/dream` file in the README, reduce `docs/` to two teacher-facing functional documents, and keep `tests/` represented by one concise directory description.

**Architecture:** This is a documentation-only migration. First derive file responsibilities from the current source, then create two replacement functional documents, rewrite only the README project-structure and documentation-links sections, and finally remove superseded documentation after link and content checks pass.

**Tech Stack:** Markdown, Git, `rg`, `find`, pytest, Ruff

## Global Constraints

- Do not modify DREAM functional source code, imports, APIs, data formats, tests, or runtime behavior.
- Every non-cache file under `src/dream/` must appear in the README project tree with a concrete responsibility.
- `tests/` must remain a single summarized line and must not be expanded file by file.
- Final `docs/` must contain only `ai-evolution-and-user-persona.md` and `dream-mechanism.md`.
- Deleted documentation remains recoverable from Git history.
- All README paths must exist after cleanup.

---

### Task 1: Build an Accurate Source Responsibility Inventory

**Files:**
- Read: `src/dream/**/*.py`
- Read: `src/dream/retrieval/retrieval.skill`
- Modify: none

**Interfaces:**
- Consumes: Current Python modules, public classes, public functions, imports, and module docstrings.
- Produces: A verified one-sentence responsibility for every file under `src/dream/`, used verbatim or closely paraphrased in Task 3.

- [ ] **Step 1: List every source file**

Run:

```bash
find src/dream -type f ! -path '*/__pycache__/*' | sort
```

Expected: the list begins with `src/dream/__init__.py`, includes every application, core, extraction, governance, memory, retrieval, curator, integration, and validation module, and ends with validation modules.

- [ ] **Step 2: Extract public definitions and module dependencies**

Run:

```bash
rg -n '^(class|def|async def) |^from dream|^import dream' src/dream
```

Expected: enough evidence to distinguish model files, managers, adapters, orchestrators, storage components, schedulers, and API entry points.

- [ ] **Step 3: Check the retrieval resource separately**

Run:

```bash
sed -n '1,220p' src/dream/retrieval/retrieval.skill
```

Expected: the README description identifies it as the declarative Memory Retrieval Skill contract rather than Python implementation code.

- [ ] **Step 4: Verify full inventory coverage**

Create two temporary sorted lists during validation: actual source paths and README tree paths. Do not write temporary inventory files into the repository.

Expected: Task 3 can prove there are no unexplained source files.

---

### Task 2: Create Two Teacher-Facing Functional Documents

**Files:**
- Create: `docs/ai-evolution-and-user-persona.md`
- Create: `docs/dream-mechanism.md`
- Read: `docs/design/2026-07-15-dream-memory-architecture.md`
- Read: `docs/design/2026-07-21-adaptive-dream-curator-design.md`

**Interfaces:**
- Consumes: Current source behavior and the still-valid sections of the two existing design documents.
- Produces: Two self-contained documents that remain after historical documentation is removed.

- [ ] **Step 1: Write the AI evolution and user persona document**

The document must contain these exact top-level sections:

```markdown
# AI 决策进化与用户画像

## 解决的问题
## 从会话到长期知识
## 用户画像
## AI 决策经验
## 自动治理与生效
## 下一任务如何使用
## 当前能力边界
```

It must explain:

- conversation events are extracted into canonical knowledge;
- Persona uses `new`, `update`, `merge`, and `duplicate`;
- `USER.md` is evidence-preserving durable memory;
- `USER_PERSONA.md` is the Agent-facing projection;
- Decision Cards preserve scenario, signals, principle, boundary, confidence, and sources;
- deterministic AI Curator generates `DECISION_RULES.md`;
- low-risk knowledge may auto-activate while high-risk knowledge remains reviewable;
- Workflow Skill remains an audited candidate and is not an executable Skill Runtime.

- [ ] **Step 2: Write the Dream mechanism document**

The document must contain these exact top-level sections:

```markdown
# DREAM 做梦机制

## 什么是做梦
## 自适应触发
## 单批处理流程
## 两层 Curator
## 自动治理
## 快照、版本与回滚
## 超时与安全失败
## 运行结果
```

It must explain:

- background review is triggered by idle time, event count, token estimate, or maximum wait;
- Agnes normally runs once per batch;
- invalid structured output allows at most one repair call;
- deterministic Curator runs after successful batches and has a daily 03:00 fallback;
- Semantic Curator is disabled by default and uses the configured independent period and idle requirement when enabled;
- local writeback is connected to snapshots, publication versions, reports, activation, and rollback;
- ordinary Dream execution has a 300-second overall deadline and preserves pending work on safe failure.

- [ ] **Step 3: Check the new documents for obsolete names**

Run:

```bash
rg -n 'dream\\.(review|sources|source_sync)|src/dream/(review|sources)|Dreams ×|Character\\.AI' \
  docs/ai-evolution-and-user-persona.md docs/dream-mechanism.md
```

Expected: no matches.

- [ ] **Step 4: Check document readability**

Run:

```bash
rg -n '^#{1,3} ' docs/ai-evolution-and-user-persona.md docs/dream-mechanism.md
```

Expected: both files contain the required section sequence and no empty sections.

- [ ] **Step 5: Commit the replacement documents**

```bash
git add docs/ai-evolution-and-user-persona.md docs/dream-mechanism.md
git commit -m "docs: add core DREAM feature guides"
```

---

### Task 3: Rewrite the README Project Structure

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: The complete source responsibility inventory from Task 1 and the two documents from Task 2.
- Produces: A project tree where every `src/dream` file has a specific explanation, `docs/` has two explained files, and `tests/` is summarized in one line.

- [ ] **Step 1: Replace only the project structure code block**

The tree must include:

```text
DREAM/
├── src/
│   └── dream/
│       ├── __init__.py
│       ├── api.py
│       ├── config.py
│       ├── application/
│       ├── core/
│       ├── extraction/
│       ├── governance/
│       ├── memory/
│       ├── retrieval/
│       ├── curators/
│       ├── integrations/
│       └── validation/
├── docs/
│   ├── ai-evolution-and-user-persona.md
│   └── dream-mechanism.md
├── tests/                              # 单元、集成和端到端测试
├── fixtures/
├── .env.example
├── pyproject.toml
├── README.md
└── .gitignore
```

Expand every package below `src/dream/` and include every actual file. Each file comment must describe concrete behavior; comments such as “工具文件”, “相关逻辑”, or a repetition of the filename are not acceptable.

- [ ] **Step 2: Update README documentation links**

Replace the old document link list with:

```markdown
- [AI 决策进化与用户画像](docs/ai-evolution-and-user-persona.md)
- [DREAM 做梦机制](docs/dream-mechanism.md)
```

- [ ] **Step 3: Verify every README source path**

Run a local read-only path comparison between `find src/dream` output and paths transcribed from the README tree.

Expected: zero missing paths and zero stale paths.

- [ ] **Step 4: Verify obsolete documentation links are gone**

Run:

```bash
rg -n 'docs/(api|design|validation|superpowers)/' README.md
```

Expected: no matches.

- [ ] **Step 5: Check Markdown whitespace**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 6: Commit the README update**

```bash
git add README.md
git commit -m "docs: explain project files in readme"
```

---

### Task 4: Remove Superseded Documentation and Verify the Repository

**Files:**
- Delete: `docs/api/`
- Delete: `docs/design/`
- Delete: `docs/validation/`
- Delete: `docs/superpowers/`
- Preserve: `docs/ai-evolution-and-user-persona.md`
- Preserve: `docs/dream-mechanism.md`

**Interfaces:**
- Consumes: Replacement documents and updated links from Tasks 2 and 3.
- Produces: A minimal `docs/` directory with no broken repository references.

- [ ] **Step 1: Confirm replacement documents exist before deletion**

Run:

```bash
test -f docs/ai-evolution-and-user-persona.md
test -f docs/dream-mechanism.md
```

Expected: both commands exit with status 0.

- [ ] **Step 2: Remove only the approved documentation directories**

Delete the four approved directories through explicit patch deletions or explicit validated paths. Do not delete the `docs/` root or either replacement document.

- [ ] **Step 3: Confirm the final documentation inventory**

Run:

```bash
find docs -type f | sort
```

Expected:

```text
docs/ai-evolution-and-user-persona.md
docs/dream-mechanism.md
```

- [ ] **Step 4: Scan the repository for broken references**

Run:

```bash
rg -n 'docs/(api|design|validation|superpowers)/' README.md src tests pyproject.toml .env.example
```

Expected: no matches.

- [ ] **Step 5: Run documentation and code verification**

Run:

```bash
git diff --check
pytest -q
ruff check src tests
```

Expected:

- `git diff --check`: no output;
- `pytest -q`: all tests pass;
- `ruff check src tests`: `All checks passed!`.

- [ ] **Step 6: Commit cleanup**

```bash
git add -A docs
git commit -m "docs: remove superseded project documents"
```

