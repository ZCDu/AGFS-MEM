# Memory Graph Backend

A FastAPI service that stores a per-user knowledge graph as flat files in S3
(or local disk). No database. Each entity is one Markdown file with YAML
front-matter following [Google's OKF v0.2](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
specification. Includes a chat UI with file upload support.

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env    # fill in DEEPSEEK_API_KEY and storage settings
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Opens on `http://localhost:8000`. Interactive API docs at `http://localhost:8000/docs`.

**Chat UI** at `http://localhost:8000/chat` — supports drag-drop file upload, paste,
and paperclip button. Files are extracted for LLM processing (.docx, .xlsx, .pdf, .txt).

**Default admin account** is created automatically on first startup if no users exist:
- Username: `admin`
- Password: `admin123456`
- Change it immediately via `POST /v1/auth/password` or the chat UI sign-in.

## Configuration

Everything lives in `.env`. Copy `.env.example` and fill in the blanks.

### Required

| Var | Notes |
|---|---|
| `STORAGE_BACKEND` | `mirage` (S3), `disk` (local), `s3` (plain boto3) |
| `MIRAGE_S3_BUCKET` | Required when `STORAGE_BACKEND=mirage` |
| `MIRAGE_S3_ACCESS_KEY_ID` | S3 credentials |
| `MIRAGE_S3_SECRET_ACCESS_KEY` | S3 credentials |
| `AUTH_TOKENS` | At least one `token:user_id` pair, e.g. `abc123:demo` |
| `DEEPSEEK_API_KEY` | For LLM extraction and chat |

### Optional

| Var | Default | Notes |
|---|---|---|
| `AUTH_MODE` | `token` | `token` or `off` (dev only) |
| `AUTH_SECRET` | — | Required for password login. Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `AUTH_SESSION_HOURS` | `12` | Session token lifetime |
| `STORAGE_BACKEND` | `mirage` | `mirage`, `s3`, or `disk` |
| `MIRAGE_S3_REGION` | — | S3 region |
| `MIRAGE_S3_ENDPOINT_URL` | — | For non-AWS S3-compatible gateways |
| `MIRAGE_S3_KEY_PREFIX` | `memory_backend/` | Key prefix inside bucket |
| `LLM_MODEL` | `deepseek-chat` | Model for chat/extraction |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI-compatible endpoint |
| `FLUSH_INTERVAL_SECONDS` | `2.0` | Write-behind buffer flush interval |
| `FLUSH_MAX_PENDING` | `100` | Flush early once this many mutations are pending |

## Authentication

The API requires a bearer token. Two credential types, both sent as
`Authorization: Bearer <token>`:

### Static tokens (services, scripts)

Set `AUTH_TOKENS=token:user_id` in `.env`. Multiple tokens: `tok1:user1,tok2:user2`.

### Password login (people)

Set `AUTH_SECRET` in `.env`, then create accounts:

```powershell
python scripts\manage_users.py add alice
python scripts\manage_users.py add ops --admin
python scripts\manage_users.py list
```

Log in via `POST /v1/auth/login` or the chat UI's **Sign in** button.

The default `admin` account is bootstrapped automatically on first startup.

## Storage layout

```
{user_id}/wiki/{type}/{slug}.md           entity files (OKF v0.2 markdown)
{user_id}/wiki/_manifest/snapshot.json     entity index + deltas
{user_id}/wiki/_ops/{date}/*.jsonl         audit log
{user_id}/raw/{session_id}/{file_id}/      uploaded files per session
{user_id}/sessions/{YYYY}/{MM}/{day}/      conversation transcripts
_auth/users.json                           user accounts (scrypt hashed)
```

Entity files are the source of truth. The manifest, ops log, and session files
are derived and rebuildable.

## OKF v0.2 entities

Each entity is a single `.md` file:

```yaml
---
type: person
title: Alice Chen
description: Staff engineer on the retrieval team.
tags: [alice, engineer]
generated: { by: memory_backend/1.0, at: '2026-08-03T08:51:55+00:00' }
status: stable
okf_version: '0.2'
wiki_id: person/alice-chen
facts:
  - text: Leads the retrieval workstream.
    confidence: 0.9
relations:
  - target: project/orion
    category: works_on
metadata:
  significance: 0.8
---

Alice Chen is a staff engineer.

Works on [project/orion](/project/orion.md) (lead engineer).
```

Status values: `stable` (default), `draft`, `deprecated`.

## File uploads

Files are organized by session: `{user_id}/raw/{session_id}/{file_id}/`

The chat UI extracts text from uploaded files for LLM processing:
- `.txt`, `.md`, `.json`, `.csv`, `.log` — read directly
- `.docx` — XML text extraction
- `.xlsx` — cell value extraction via shared strings
- `.pdf` — best-effort text extraction

`POST /v1/users/{user_id}/files` accepts multipart uploads. `session_id` is required.

## Chat UI

Open `http://localhost:8000/chat`. Features:
- Drag and drop files anywhere, Ctrl+V paste, or click the 📎 button
- Files appear as chips below the input, then move into the message bubble on send
- Supports sending messages with files only (no text required)
- Session-based file organization

## LLM extraction

Turns conversation into memory operations. Requires `DEEPSEEK_API_KEY`.

```
POST /v1/users/{user_id}/extract  {"text": "..."}
```

Returns proposed operations (upsert entities, add facts, create relations).
Review them, then apply with `?apply=true` or `POST /extract/apply`.

## Running on S3

```bash
STORAGE_BACKEND=mirage MIRAGE_S3_BUCKET=your-bucket uvicorn app.main:app --port 8000
```

Or plain boto3: `STORAGE_BACKEND=s3 S3_BUCKET=your-bucket`.

Run `python scripts/s3_preflight.py --bucket YOUR_BUCKET` first to validate
the bucket supports conditional writes (required).

### IAM policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::YOUR_BUCKET/memory/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::YOUR_BUCKET",
      "Condition": {"StringLike": {"s3:prefix": ["memory/*"]}}
    }
  ]
}
```

`s3:ListBucket` is not optional — without it S3 returns `403` instead of `404`
for missing keys, which breaks nearly every code path.

## Test suite

```bash
pip install pytest httpx
python -m pytest tests/ -v
```

## API overview

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Health check, reports active storage backend |
| `POST /v1/auth/login` | Username/password → session token |
| `GET /v1/auth/me` | Who am I? (debug 403s) |
| `POST /v1/auth/password` | Change your password |
| `GET /v1/users/{id}/wiki` | List entities (filterable, pageable) |
| `PUT /v1/users/{id}/wiki` | Create or update an entity |
| `GET /v1/users/{id}/wiki/{type}/{slug}` | Get one entity |
| `DELETE /v1/users/{id}/wiki/{type}/{slug}` | Delete (tombstone) |
| `POST /v1/users/{id}/wiki/subgraph` | Traverse graph neighbourhood |
| `POST /v1/users/{id}/wiki/_rebuild_manifest` | Rebuild index from entity files |
| `POST /v1/users/{id}/wiki/_reconcile` | Fix dangling relations |
| `POST /v1/users/{id}/extract` | LLM extraction from text |
| `POST /v1/users/{id}/chat` | Chat with memory context |
| `POST /v1/users/{id}/files` | Upload files |
| `GET /v1/users/{id}/files/{session_id}` | List files in a session |
| `GET /v1/users/{id}/sessions` | List conversation sessions |

Full interactive docs at `http://localhost:8000/docs`.

## Troubleshooting

| Symptom | Cause |
|---|---|
| App won't start, "no tokens configured" | Set `AUTH_TOKENS` in `.env` |
| `401 Missing bearer token` | Send `Authorization: Bearer <token>` |
| `401 Invalid username or password` | Also shown for unknown/disabled accounts |
| `429 Too many failed attempts` | Login throttle; wait for Retry-After |
| `503 Password login is not enabled` | `AUTH_SECRET` not set |
| `401 Session expired` | Log in again |
| Everything ~600ms per request | `MIRAGE_REUSE_CONNECTIONS` disabled, or bucket in wrong region |
| `_stats` shows 0 entities | You're pointed at an empty user_id |
| HTTP 422 "Entity file is corrupt" | Run `python scripts\check_bucket.py --fix` |

## Architecture notes

- **Entity files are source of truth.** Manifest and ops log are derived and
  rebuildable with `POST /wiki/_rebuild_manifest`.
- **Write-behind buffering** holds manifest and ops log in memory, flushing on
  a timer. Reduces writes from 3 PUTs per upsert to 1.
- **Log-structured manifest** uses snapshots + deltas instead of rewriting a
  single growing file. Total write volume is O(N log N) instead of O(N²).
- **Conditional writes** (S3 `If-Match` / `If-None-Match`) protect against
  concurrent write races. Mirage emulates this with read-then-write.
- **Stateless session tokens** — no storage read on every request. Tradeoff:
  individual tokens cannot be revoked before expiry. Rotate `AUTH_SECRET` to
  invalidate all sessions.
- **Adjacency index** in the manifest means `traverse()` costs zero storage
  reads — edges are indexed per entity.
- **Connection reuse** patches mirage to keep TLS connections alive instead of
  reconnecting per operation (~10x latency improvement on remote endpoints).
