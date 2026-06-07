# ADR 0001: Modular Memory Pipeline

## Status

Accepted

## Context

`MemoryService` previously owned session writes, long-term extraction and storage, recall filtering, history assembly, and local journal side effects. That made future replacement of ES, extraction logic, recall strategy, or local storage behavior require edits in the central service.

## Decision

Split the workflow into replaceable modules while keeping the public API unchanged:

- `MemoryService` remains the thin API-facing orchestrator.
- `LongTermMemoryEngine` owns extraction, embedding, vector search, and ADD/UPDATE/DELETE execution.
- `HistoryBuilder` owns recall history assembly and long-term memory context injection.
- `LocalJournalPipeline` owns attachment persistence and journal entry creation.

The app entry point wires these modules explicitly in `create_app()`, and `MemoryService` still provides backward-compatible wrappers for existing internal tests.

## Consequences

Future replacements can target one module at a time. For example, a new vector store can be introduced behind `LongTermMemoryEngine`, and a different recall policy can replace `HistoryBuilder` without changing route handlers or request/response models.

The split adds a few small files, but keeps operational behavior and tests stable.
