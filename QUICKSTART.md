# QUICKSTART

One authoritative set of instructions. If anything elsewhere contradicts
this file, this file is right.

---

## What this program is

A FastAPI service that stores a per-user knowledge graph as flat files in S3.
No database. Each entity is one Markdown file with YAML front-matter:

```
{user_id}/wiki/{type}/{slug}.okf.md
```

Alongside them, two pieces of derived state that the service maintains
automatically:

```
{user_id}/wiki/_manifest/snapshot.json + d/*.json   index of all entities
{user_id}/wiki/_ops/{date}/*.jsonl                  audit log
{user_id}/raw-facts/{date}.jsonl                    raw conversation log
```

The entity files are the source of truth. Everything else is rebuildable
from them.

Storage always goes through a mirage-ai `Workspace`. Two modes:

| `STORAGE_BACKEND` | mounts | use |
|---|---|---|
| `mirage` (default) | `S3Resource` | real deployment, needs `MIRAGE_S3_BUCKET` |
| `disk` | `DiskResource` | offline dev and tests, no credentials |

Both are the same code path, differing only in the mounted resource.

---

## Authentication

The API requires a bearer token, and **the app refuses to start without one**
configured. That is deliberate: `user_id` is a path parameter, so without auth
anyone reaching the port could read or write any user's memory by editing the
URL. An insecure default is how services get shipped insecure.

There are two credential kinds, and both arrive in the same
`Authorization: Bearer <token>` header:

| | for | how you get it |
|---|---|---|
| **Static token** | services, scripts, agents | put it in `.env` |
| **Session token** | people | `POST /v1/auth/login` with a username and password |

Configure at least one. Either satisfies startup.

### Username and password (people)

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```ini
AUTH_MODE=token
AUTH_SECRET=<paste-that-value>
AUTH_SESSION_HOURS=12
```

`AUTH_SECRET` signs session tokens. Then create an account — the CLI exists
because creating an account needs authentication and the first account is what
you would authenticate with:

```powershell
python scripts\manage_users.py add alice
python scripts\manage_users.py add ops --admin
python scripts\manage_users.py list
```

Passwords are prompted for, never passed as arguments — an argument lands in
shell history and in the process list. Accounts are stored in `_auth/users.json`
in the same object store as everything else, hashed with scrypt and a per-user
salt.

Log in:

```
curl.exe --% -X POST http://127.0.0.1:8000/v1/auth/login -H "content-type: application/json" -d "{\"username\":\"alice\",\"password\":\"...\"}"
```

That returns a token to use as a bearer credential for `AUTH_SESSION_HOURS`.
In the GUI, click **Sign in** instead.

Other endpoints: `GET /v1/auth/me` reports what your credential is and which
`user_id` it can reach — the fastest way to diagnose a 403. `POST /v1/auth/password`
changes your own password.

Login is throttled to 8 failures per 5 minutes per username and client, because
scrypt protects the stored hash but does nothing to slow an attacker hammering
the endpoint.

**Sessions are stateless**, so an individual token cannot be revoked before it
expires. Disabling an account stops new logins but existing sessions keep
working. Rotating `AUTH_SECRET` invalidates all of them at once, and that is the
only bulk revocation mechanism. Keep `AUTH_SESSION_HOURS` short.

### Static token (services)

Generate a token and put it in `.env`:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```ini
AUTH_MODE=token
AUTH_TOKENS=<paste-the-token>:demo
# AUTH_ADMIN_TOKEN=<another-token>    # optional, may access ANY user
```

`AUTH_TOKENS` is `token:user_id` pairs, comma-separated. A token is scoped to
its user: using it against a different `user_id` returns 403, not data. Two
tokens may map to the same user, which is how you rotate without downtime.

Send it on every API call:

```
curl.exe --% -H "Authorization: Bearer <token>" http://127.0.0.1:8000/v1/users/demo/wiki/_stats
```

`/healthz`, `/docs` and `/gui` stay open — the first for monitoring, the other
two are static and contain no user data. Every API call the GUI makes is
checked like any other client's.

**For local development only**, `AUTH_MODE=off` disables the check entirely and
warns at startup.

## Setup, once

```powershell
cd C:\memory_backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install pytest httpx
python -m pytest tests/ -q          # expect: 96 passed
```

If `Activate.ps1` is blocked:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

---

## Running it

**Window 1 — the server. Leave it running.**

```powershell
.\run.ps1
```

**Everything else happens through the browser** — there is no separate
terminal setup needed. `http://127.0.0.1:8000/healthz` confirms the server is
up; `/docs` is the interactive API reference if you want to try a request
without opening the GUI.

---

## Visual editor

```
http://127.0.0.1:8000/gui
```

Paste your token into the **Token** field next to the user name. It is held
for the browser tab only. Switching the user without switching the token gives
403, which is the point.

The graph starts with about 40 nodes seeded from the most-connected entities,
not the whole graph. Nodes with a dashed ring and a `+N` badge have neighbours
that are not on the canvas — **double-click to pull them in**, or use the button
in the detail panel. Dragging a node saves its position, so the layout is the
same next time you open it.

An interactive graph. Nodes are colour-coded by entity type and draggable;
clicking one dims the rest of the graph to its immediate neighbours and opens
an editable panel on the right — aliases, significance, summary, facts and
relations, all editable in place. Edge style encodes relation category:
dashed for `refines`, red for `contradicts`, green for `causes`, dotted for
the temporal pair.

The left column adds entities, filters the list, and scores conversation text
against the graph with the assessor.

Served by the app rather than opened as a file, because there is no CORS
middleware — a `file://` page cannot call the API.

`http://127.0.0.1:8000/docs` remains available for raw request/response work.

## Calling the API directly

`/docs` (Swagger UI) covers this for one-off requests — open the endpoint,
fill in the form, **Try it out**. For scripting, plain `Invoke-RestMethod`
needs nothing beyond what's already installed:

```powershell
$H = @{ Authorization = "Bearer <token>" }   # omit if AUTH_MODE=off
$base = "http://127.0.0.1:8000/v1/users/demo"

Invoke-RestMethod "$base/wiki/_stats" -Headers $H
Invoke-RestMethod "$base/wiki" -Method Put -Headers $H -ContentType "application/json" `
  -Body (@{ type='person'; title='Alice Chen'; aliases=@('Alice') } | ConvertTo-Json)
Invoke-RestMethod "$base/wiki/person/alice-chen/facts" -Method Post -Headers $H -ContentType "application/json" `
  -Body (@{ text='Leads retrieval.'; confidence=0.9 } | ConvertTo-Json)
```

---

## Which helper files matter

**Use these:**

| file | what it does |
|---|---|
| `run.ps1` | starts the server |

**Diagnostics, only when something is wrong:**

| script | when |
|---|---|
| `scripts\diagnose_mirage.py` | S3 misbehaving — bypasses FastAPI entirely |
| `scripts\s3_latency_probe.py` | S3 slow — times each primitive separately |
| `scripts\check_bucket.py` | corrupt entity files |
| `scripts\s3_preflight.py` | validate a bucket before deploying to it |
| `scripts\cost_benchmark.py` | storage write-cost model |
| `scripts\browse_bucket.py` | browse/download bucket contents directly when you can't reach Qiniu another way |

---

## Configuration

Everything lives in `.env`.

```ini
STORAGE_BACKEND=mirage
MIRAGE_S3_BUCKET=s3storage
MIRAGE_S3_REGION=cn-east-1
MIRAGE_S3_ENDPOINT_URL=https://s3.cn-east-1.qiniucs.com
MIRAGE_S3_ACCESS_KEY_ID=...
MIRAGE_S3_SECRET_ACCESS_KEY=...
MIRAGE_S3_PATH_STYLE=true
MIRAGE_S3_KEY_PREFIX=memory_backend/

FLUSH_INTERVAL_SECONDS=30
FLUSH_MAX_PENDING=200
```

Two settings deserve a warning:

- **`MIRAGE_INDEX_TTL_SECONDS` must stay at `0`.** Anything higher returns
  stale directory listings when another process writes. Because
  `_rebuild_manifest` replaces the index with whatever a listing returns,
  a non-zero TTL can delete entries for entities that still exist.
- **`MIRAGE_REUSE_CONNECTIONS` should stay `true`.** Setting it false
  restores mirage's stock behaviour of a new TLS connection per operation,
  which measured ~10x slower against a remote endpoint.

---

## LLM extraction

Turns conversation into memory. Optional — without a key everything else
works and `/extract` returns 503.

```ini
DEEPSEEK_API_KEY=sk-...
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

Any OpenAI-compatible endpoint works by changing those last two.

**It plans; it does not write.**

```
curl.exe --% -X POST http://127.0.0.1:8000/v1/users/demo/extract -H "content-type: application/json" -d "{\"text\":\"We decided to move Orion off the legacy index. Alice Chen will lead the migration, deadline 2026-08-15.\"}"
```

Returns a list of proposed operations with a reason for each, and writes
nothing. Review it, then either apply the whole thing:

```
curl.exe --% -X POST "http://127.0.0.1:8000/v1/users/demo/extract?apply=true" -H "content-type: application/json" -d "{\"text\":\"...\"}"
```

or submit the operations you want, having dropped or edited the rest:

```
curl.exe --% -X POST http://127.0.0.1:8000/v1/users/demo/extract/apply -H "content-type: application/json" -d "{\"operations\":[{\"op\":\"upsert_entity\",\"wiki_id\":\"person/nadia\",\"payload\":{\"type\":\"person\",\"title\":\"Nadia\"}}]}"
```

The assessor gates the model call, so chatter costs nothing —
`llm_used: false` in the response means it never reached the model, and
`assessment` says why. `"force": true` overrides that.

| status | meaning |
|---|---|
| 503 | no API key configured |
| 502 | the model was unreachable or returned nothing usable |
| 422 | a submitted operation was malformed |

## Common tasks

```powershell
$H = @{ Authorization = "Bearer <token>" }
$base = "http://127.0.0.1:8000/v1/users/demo"

Invoke-RestMethod "http://127.0.0.1:8000/healthz"
Invoke-RestMethod "$base/wiki/_stats" -Headers $H

# repair: index disagrees with what is actually stored
Invoke-RestMethod "$base/wiki/_rebuild_manifest" -Method Post -Headers $H

# repair: relations pointing at deleted entities
Invoke-RestMethod "$base/wiki/_reconcile" -Method Post -Headers $H
```

---

## Troubleshooting

| symptom | cause |
|---|---|
| App won't start, "no tokens are configured" | set `AUTH_TOKENS` in `.env`, or `AUTH_MODE=off` for dev |
| `401 Missing bearer token` | send `Authorization: Bearer <token>` |
| `403 ... scoped to user 'x'` | that token belongs to a different user; check `GET /v1/auth/me` |
| `401 Invalid username or password` | also shown for unknown or disabled accounts, by design |
| `429 Too many failed attempts` | login throttle; wait for the `Retry-After` value |
| `503 Password login is not enabled` | `AUTH_SECRET` is not set |
| `401 Session expired` | log in again |
| `_stats` shows 0 entities | you are pointed at an empty user id, not an error |
| everything ~600ms | `MIRAGE_REUSE_CONNECTIONS` disabled |
| HTTP 422 mentioning a corrupt file | run `python scripts\check_bucket.py --fix` |

**Extracting the zip.** It contains a top-level `memory_backend/` folder, so
unpacking it directly into `C:\memory_backend` nests it one level too deep.
Extract to a staging folder and copy the contents:

```powershell
Expand-Archive "$HOME\Downloads\memory_backend_cost_optimized.zip" -DestinationPath C:\_extract -Force
Copy-Item C:\_extract\memory_backend\* -Destination C:\memory_backend\ -Recurse -Force
Remove-Item C:\_extract -Recurse -Force
```

This overwrites code and scripts and leaves `.venv`, `.env` and
`local_bucket` alone.
