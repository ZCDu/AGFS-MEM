# DREAM README Current Architecture Design

## Goal

Rewrite the root `README.md` so it accurately represents the current DREAM
implementation and serves both project reviewers and developers integrating the
memory framework.

The README must lead with the product value and three core capabilities, then
provide enough technical detail to install, run, test, and integrate DREAM
without requiring readers to understand the repository first.

## Audience

The primary audiences are:

1. Teachers and GitHub reviewers evaluating the purpose, architecture, and
   completeness of the project.
2. Developers integrating DREAM into an existing Agent application.

The project name is consistently written as `DREAM`.

## Proposed Structure

The rewritten README will use this order:

1. Project overview and current scope.
2. Three core capabilities:
   - User Persona formation and evolution.
   - AI Decision Cards and Decision Rules.
   - Task-relevant Memory Retrieval.
3. End-to-end data flow:
   `Conversation → Extraction → Governance → Memory → Retrieval → Agent`.
4. Current layered source structure.
5. Durable artifacts and tenant/agent/user isolation.
6. Memory formation and risk-aware automatic governance.
7. Memory Retrieval Skill behavior and Python usage.
8. FastAPI ingestion, validation, publication, and task-start interfaces.
9. Five-minute local quick start.
10. PyCharm and pytest verification.
11. Existing-Agent integration points.
12. Failure safety, deadlines, snapshots, reports, rollback, and security.
13. Links to detailed API, design, and validation documentation.

## Accuracy Requirements

The README must describe only behavior present in the current source tree:

- Source layers are `application`, `core`, `extraction`, `governance`,
  `memory`, `retrieval`, `curators`, and `integrations`.
- Provider output is adapted into DREAM canonical knowledge before business
  validation and routing.
- Low-risk, sufficiently evidenced Persona and Decision artifacts may activate
  automatically; high-risk artifacts retain the review path.
- Workflow Skills remain classified and auditable candidates. DREAM does not
  claim to provide an active Skill Runtime in this phase.
- `MemoryRetrievalSkill` is an independent Python runtime API, not a FastAPI
  endpoint and not a replacement for the existing `/v1/tasks/start` flow.
- Retrieval is read-only, user-isolated for Persona data, bounded by Top-K and
  context budget, and uses deterministic local filtering and ranking in the
  current version.
- The ordinary Dream transaction retains the configured total deadline,
  snapshots, version reports, rollback, and safe failure behavior.
- Semantic Curator consolidation is optional and disabled by default.

## Quick-Start Design

The quick start will include:

1. Python version and editable installation.
2. Copying `.env.example` to `.env`.
3. Configuring an OpenAI-compatible extraction backend without exposing a real
   API key.
4. Starting Uvicorn locally.
5. Importing NDJSON or posting a completed conversation.
6. Running a validation Dream and interpreting `active` versus
   `ready_for_review`.
7. Inspecting `USER.md`, `USER_PERSONA.md`, Decision Cards, and
   `DECISION_RULES.md`.
8. Calling `MemoryRetrievalSkill` from Python.

Detailed formal campaign instructions remain in `docs/validation/` rather than
being duplicated in the root README.

## Testing Design

The README will provide:

- The full local test command.
- A focused command covering governance, FastAPI closed-loop behavior, and
  retrieval.
- PyCharm interpreter, working-directory, and Uvicorn module configuration.

The README will not hardcode a passing test count because that count changes as
coverage grows.

## Security and Repository Hygiene

The README will explicitly state that these must remain local:

- `.env` and provider API keys.
- `DREAM_HOME` runtime data.
- `validation-run/`.
- `.venv/`, IDE metadata, caches, and `*.local.jsonl` validation conversations.

Only `.env.example` and sanitized, tracked fixtures are suitable for GitHub.

## Non-Goals

This documentation task will not:

- Modify application behavior or public interfaces.
- Add a Retrieval FastAPI endpoint.
- Activate workflow Skill candidates.
- Change memory formats, governance policy, scheduling, or publication state.
- Rewrite detailed design and validation documents already under `docs/`.

## Verification

After rewriting:

1. Check every referenced source path exists.
2. Check every documented API path exists in `src/dream/api.py`.
3. Check shell and Python examples use current module names.
4. Scan for obsolete module references such as root-level `service.py`,
   `review/`, `managers/`, `source_sync.py`, or `hermes_compat`.
5. Run Markdown-oriented text checks and the relevant project test suite.
6. Confirm only documentation files changed.
