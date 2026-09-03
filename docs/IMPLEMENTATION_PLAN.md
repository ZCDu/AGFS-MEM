# Implementation Plan — Enterprise Program Tracking for memory_backend

Status: DRAFT (for approval, no code written yet)
Date: 2026-08-20
Scope: `C:\memory_backend` (FastAPI + mirage/S3 + LLM extraction)

## 0. Goal

Turn the current flat "topic wiki" graph into an **enterprise-savvy program-tracking
graph** for meetings / projects / programs, while keeping it cloud-native (S3),
LLM-driven, and — critically — **auditable without becoming compliance theatre**.

Three workstreams (independent, can ship in any order, all optional):

- **WS-A: Project/Program/Meeting hierarchy** (the "shape")
- **WS-B: Lean decision lifecycle** (the "records" on top of the shape)
- **WS-C: Conflict detection** (the data-quality safeguard)

Each is sized and scoped so we can land them one at a time and verify live.

---

## 1. Current state (grounding — verified in code)

- Entity types (`VALID_TYPES`): `person, organization, project, event, concept,
  artifact, preference, decision`. **No `program` or `meeting` type yet.**
- Relation categories: `related_to, contradicts, refines, causes,
  temporal_before, temporal_after`.
- Storage: `wikis/{scope}/{type}/{slug}.okf.{md,json}` on mirage S3
  (`app/graph/keys.py::wiki_key`). Wiki = org-level with explicit grants
  (`WikiRegistry`), `access: {user: role}`.
- Extraction flow: `POST /extract` (assess → LLM plan, writes nothing) →
  `POST /extract/apply` (materializes). Two-step by design (anti-hallucination).
- Chat `/chat` is read-only on memory (no auto-write).
- Decisions are ALREADY an entity type; each "decision" entity carries a fact.

## 2. WS-A — Program → Project → Meeting hierarchy

### 2.1 Problem
Today a wiki is a single flat topic. An enterprise program (e.g. "Q3 Cloud
Migration Program") contains projects ("Billing Engine", "Warehouse"),
and each project is fed by meeting notes. There is no parent/child — everything
is sibling wikis.
The idea (from Youtu-GraphRAG) is *schema-guided hierarchical structure*:
impose the containment relationship up front so the graph is navigable by
"program → projects → meetings".

### 2.2 Design
**Add two entity types:** `program` and `meeting`.

- `meeting` = a single session (has attendees, date, agenda, decisions, action items).
- `program` = an org-level umbrella that owns a set of `project`/`subprogram` entities.

**Relationships (reuse existing categories, add if needed):**
- `program contains project`  → new category `contains` (or reuse `related_to` with label="contains"; prefer explicit `contains`).
- `project belongs_to program`
- `meeting belongs_to project`
- `meeting refines project` (an update) — reuse `refines`.
- `decision influences project` — via WS-B.

**Where the hierarchy is DECLARED (not the graph):** the wiki *metadata*, not just
entity edges. `WikiMeta` gains:
```
"parents": ["<wiki_id>"]   // e.g. program wiki lists its project wikis
"children": ["<wiki_id>"]  // derived/reverse index
"kind": "program" | "project" | "meeting" | "topic"
```
Rationale: a "program wiki" and a "project wiki" are real top-level wikis with
their own grants; the hierarchy links them. A meeting need NOT be its own wiki
(too fine-grained) — see 2.3.

### 2.3 What a meeting is
Two options, need a decision from you:

- **(A) Meeting = entity inside a project/program wiki** (recommended for
  lightweight). `meeting/2026-08-20-logistics-review` entity in the project wiki.
- **(B) Meeting = its own wiki** under the program. Only if you want per-meeting
  grants/history as first-class wikis.

Recommend **(A)** — meetings are the leaves, not containers.

### 2.4 Extraction changes
- Extend `SYSTEM_PROMPT`: entities can be typed `program`, `meeting`; emit
  `contains`/`belongs_to`/`refines` links so the hierarchy comes out of the LLM
  naturally.
- Extend `VALID_TYPES` + `VALID_RELATION_CATEGORIES`.
- Extend the GUI to render a **tree/hierarchy view** (program → projects →
  meetings) alongside the existing force graph. (UI phase; backend first.)

### 2.5 Storage/migration
- New `WikiMeta` fields are additive; existing wikis default `kind: "topic"`,
  empty `parents/children` → **no migration needed** (back-compat).
- New entity types are just new `{type}/{slug}` folders under a wiki — additive.

### 2.6 Acceptance
- Can create a `program`, attach `project` wikis as children, attach `meeting`
  entities to a project, and see the tree in the GUI.
- Extraction of a meeting note produces `meeting` entities + `contains`/`refines`
  links correctly.
- Existing flat wikis still work (no regression).

---

## 3. WS-B — Lean Decision Lifecycle

### 3.1 Problem
You already store `decision` entities with evidence. What's missing (the useful
part of Semantica's Decision Intelligence, minus the audit bloat):

- **Who/when/reasoning/outcome/confidence as structured fields** on a decision.
- **Causal ordering**: "this decision led to that action/decision."
- **Precedent search**: "what did we decide last time this topic came up."
- **Rollup**: per program/project, "decisions since last meeting."

### 3.2 Design — enrich the `decision` entity
Extend the decision entity payload/schema with structured fields:
```
decision / <slug>   (type=decision)
  - statement       (the decision made)
  - decided_by      (person entities)
  - decided_at      (date)
  - reasoning       (short why)
  - outcome         (status: proposed / accepted / rejected / deferred / done)
  - confidence      (0-1, from extraction)
  - precedent_of    (→ prior decision entities this supersedes/relates to)
  - source          (evidence ref: session/file)
```
This is additive to the existing `.okf.json` — back-compat safe.

### 3.3 Precedent search endpoint
`GET /v1/wikis/{id}/decisions?q=...&since=...` → returns decisions on a wiki/news
of hierarchy, ranked, with outcome + source. Cheap query over the wiki's
`decision/*` entities (already listable via `store.list_entities`).

### 3.4 "Decisions since last meeting" rollup
`GET /v1/wikis/{id}/decisions/rollup` → decisions grouped by `decided_at`,
optionally filtered to since a given meeting entity's date. Sorted newest-first.

### 3.5 NOT building (be explicit)
- W3C PROV-O export / regulator formats — skip (not a regulated domain).
- SHACL/OWL policy gates — skip.
- Causal chains with downstream-impact analytics — skip for now; keep it a flat,
  ordered, searchable decision list per scope.

### 3.6 Acceptance
- Extraction records decisions with structured fields.
- Precedent search returns past decisions for a project/program, with outcome.
- Rollup shows "decisions since last meeting" for a project/program.
- No regression to existing decision entities/wikis.

---

## 4. WS-C — Conflict Detection

### 4.1 Problem
Today `add_fact` **appends unconditionally**. Conflicting facts (e.g. "Alice
leads Orion" then "Alice was replaced as Orion lead") accumulate side by side
with no signal. In an enterprise program graph, that silently corrupts decisions
made on top of it.

### 4.2 Design (Semantica's "flag, don't silently overwrite")
On `POST /extract/apply`, before writing each `add_fact`/`link_entities`:

1. Look up existing facts on the target entity (already on disk).
2. Run a **cheap semantic contradiction check** against the incoming fact:
   - same entity + opposite polarity on a shared predicate (e.g. "leads" vs
     "no longer leads", "decided X" vs "decided Y against X");
   - rely on the LLM for a ternary judgment (`conflicts | refines | compatible`)
     when heuristics are ambiguous.
3. Outcome, recorded on the operation:
   - `compatible` → append as today.
   - `refines` → append with link to prior fact (Supersedes). 
   - `conflicts` → **flag** in the plan: do NOT silently overwrite the old fact;
     create a pending "conflict" review item. Human resolves (keep-new / keep-old
     / merge).

Storage: a small per-entity `_conflicts/{date}.jsonl` op-log or a `conflict`
field on facts — keep it additive. Surface conflicts in the GUI review panel.

### 4.3 Confidence/scope
- Only check entities' facts that already exist in the wiki (bounded cost).
- Gate the LLM conflict call — only when the fact is new and heuristic ambiguity
  exists, to limit token spend.

### 4.4 Acceptance
- Feeding a contradicting claim produces a flagged conflict, not a silent append.
- Existing compatible facts still append normally (no false positives).
- Conflict shows in the review UI with keep-new/keep-old/merge resolution.

---

## 5. Cross-cutting / non-goals

### Do
- Cloud-native (S3) only; no Neo4j/RDF/local graph store.
- Additive changes; every piece back-compat with existing wikis/entities.
- Everything gated behind the existing two-step review (write-nothing until apply).
- Prefer prompt-level / metadata-level changes first; code only where necessary.

### Don't (explicitly deferred or rejected)
- PROV-O/OWL/SHACL/governance exports (WS-B skip) — unless you later say "regulated".
- Replacing the S3 store or adding Neo4j/FalkorDB (rejected — you said cloud S3 is a must).
- Auto-writing memory on chat (stays read-only) — unchanged.
- Same-message multi-wiki segmentation (separate open item, not in this plan).

## 6. Suggested build order (each independently shippable + verifiable)
1. **WS-C conflict detection** — highest data-quality value, self-contained.
2. **WS-B decision fields + precedent + rollup** — additive, low-risk, useful alone.
3. **WS-A hierarchy** — biggest UX/architectural change; do after B so decisions
   have a scope to roll up into.

## 7. Open questions for you before I write code
1. **Meetings**: (A) entity inside a project wiki, or (B) their own wiki? (rec  A)
2. **New relation category** `contains` — OK to add, or reuse `related_to` + label?
3. **Conflict check scope**: only same-wiki facts, or also across linked wikis
   in the hierarchy (once WS-A exists)?
4. **Priority/order**: agree with C → B → A, or do you want the hierarchy (A)
   first because it changes how you enter data?
5. **Program granularity**: does a "program" ever contain other "programs"
   (nested), or strictly program→project→meeting?

---

*This is a DRAFT for your approval. I have not modified any code. Review the
open questions (especially #1 and #4) and I'll start on the first workstream you
pick.*
