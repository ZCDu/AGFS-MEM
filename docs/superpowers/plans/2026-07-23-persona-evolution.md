# Persona Evolution Implementation Plan

> **For agentic workers:** Execute inline with TDD; preserve the current dirty worktree and do not commit unrelated changes.

**Goal:** Preserve canonical `new` personas as separate domain-aware atoms and ensure every active high-confidence persona domain appears in the bounded agent-facing projection.

**Architecture:** Add deterministic persona metadata and domain inference beside the existing canonical model. Make lifecycle intent authoritative in `PersonaMergeStrategy`, with domain-gated lookup only for update/merge candidates. Generate `USER_PERSONA.md` from the complete `USER.md` repository using a coverage-preserving local projector.

**Tech Stack:** Python 3.13, dataclasses, pytest, Ruff.

## Global Constraints

- Do not change Agnes prompts, Review Adapter behavior, MemoryManager, DecisionCardManager, Snapshot/Rollback, Publication, or FastAPI.
- Preserve bootstrap `add` behavior and existing atomic `replace` validation.
- Do not write Skill candidates into the runtime skills directory.

---

### Task 1: Domain-aware persona lifecycle

**Files:**
- Modify: `src/dream/governance/persona_models.py`
- Modify: `src/dream/governance/persona_merge.py`
- Test: `tests/governance/test_persona_canonicalization.py`

- [ ] Add failing tests proving a canonical `new` crypto persona is added beside a bank persona, while a same-domain update replaces its explicit target.
- [ ] Add a canonical persona domain and persisted atom metadata model.
- [ ] Make `new` authoritative; restrict update/merge lookup to matching domains.
- [ ] Run the focused governance tests.

### Task 2: Coverage-preserving persona projection

**Files:**
- Modify: `src/dream/writeback.py`
- Test: `tests/test_writeback.py`
- Test: `tests/context/test_selector.py`

- [ ] Add failing tests for five-domain projection coverage within the configured limit.
- [ ] Generate the deterministic projection from all current persona atoms, not a prefix slice.
- [ ] Verify `/v1/tasks/start` receives the crypto persona from the regenerated projection.

### Task 3: Regression verification

**Files:**
- No production changes expected.

- [ ] Run focused governance, writeback, context, and closed-loop tests.
- [ ] Run the complete pytest suite.
- [ ] Run Ruff against `src` and `tests`.
