# Memory Graph Backend

A real, runnable FastAPI service implementing the wiki/graph portion of
`PLAN.md`'s memory design: entities stored as OKF (Open Knowledge Format)
markdown files with YAML front-matter, organized by type, with a manifest
(avoids directory scans, doubles as a compact-view cache) and an `_ops`
audit log. This is scoped to **long-term memory + graph only** — no Redis
session layer, no journals/raw/source ingestion pipeline, no persistence
classifier, no daily jobs, no TTL cleanup. Those are upstream of what's
here; see `PLAN.md` for the full system.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env    # fill in whichever STORAGE_BACKEND section you need
./run.sh
```

Opens on `http://localhost:8000`. Interactive API docs at
`http://localhost:8000/docs`. Defaults to `STORAGE_BACKEND=local`, which
writes to `./local_bucket/` — no AWS account needed to try it.

**Config is loaded from `.env` automatically at startup** (via
`python-dotenv`, called at the top of `app/main.py`) — no need to
`$env:`/`export` anything by hand, and no need to re-set it every time you
open a new terminal. If `.env` isn't found, or `python-dotenv` isn't
installed, it just falls back to whatever's already in your environment
(the old `$env:` workflow still works, it's just no longer required).
Check `/healthz` to confirm what got picked up — it reports the active
`storage_backend`.

Run the test suite:

```bash
pip install pytest httpx
python -m pytest tests/ -v
```

Run the CRUD + timing script against a live server:
```bash
python scripts/crud_timing_test.py --url http://localhost:8000 --repeat 5
```

## Configuration (env vars)

| Var | Default | Notes |
|---|---|---|
| `STORAGE_BACKEND` | `local` | `local`, `s3`, or `mirage` |
| `LOCAL_BUCKET_ROOT` | `./local_bucket` | used when `STORAGE_BACKEND=local` |
| `S3_BUCKET` | — | required when `STORAGE_BACKEND=s3` |
| `S3_PREFIX` | `""` | optional key prefix inside the bucket |
| `AWS_DEFAULT_REGION` | — | standard boto3 credential/region chain applies |
| `MANIFEST_WRITE_MODE` | `buffered` | `buffered` or `sync` — see "Write cost" below |
| `OPS_LOG_WRITE_MODE` | `buffered` | `buffered` or `sync` |
| `FLUSH_INTERVAL_SECONDS` | `2.0` | max staleness / crash-loss window for buffered state |
| `FLUSH_MAX_PENDING` | `100` | flush early once this many mutations are pending |

To use real S3 directly (plain boto3): `pip install boto3`, set
`STORAGE_BACKEND=s3` and `S3_BUCKET`, and make sure AWS credentials are
available (env vars or IAM role) — nothing else changes, same routes, same
behavior.

### Using mirage-ai instead of raw boto3 (`STORAGE_BACKEND=mirage`)

This routes storage through a [mirage-ai](https://pypi.org/project/mirage-ai/)
`Workspace` — the same storage library the original memory-system repo used
— instead of calling boto3 directly. Useful if you want to reuse that
repo's bucket/credentials, or you're on an S3-compatible gateway (e.g.
Qiniu Kodo) rather than AWS S3 itself.

```bash
pip install "mirage-ai[redis]" aioboto3
```

```
STORAGE_BACKEND=mirage
MIRAGE_S3_BUCKET=your-bucket-name
MIRAGE_S3_REGION=us-east-1                # optional
MIRAGE_S3_ENDPOINT_URL=                    # set this for a non-AWS gateway, e.g. Qiniu Kodo
MIRAGE_S3_ACCESS_KEY_ID=...
MIRAGE_S3_SECRET_ACCESS_KEY=...
MIRAGE_S3_KEY_PREFIX=memory_backend/       # default; mirage applies this automatically
```

Same routes, same request/response shapes as `local`/`s3` — only the
storage plumbing underneath changes. See `app/storage/mirage_backend.py`
for how the sync `StorageBackend` interface bridges to mirage's async
`Workspace.ops` API (via a dedicated background event-loop thread), and
for two important caveats:

- **No atomic conditional-write primitive.** Unlike `S3Backend` (which
  uses S3's native `If-Match` in one round-trip), mirage's `ops.write()`
  has none, so `MirageBackend` emulates it: read the current object,
  compare a locally-computed hash, then write. That's 2 network
  round-trips per conditional write (1 read + 1 write) — the minimum
  possible for this emulation, but still more than S3Backend's 1. It's
  also best-effort, not truly atomic — same limitation the original
  repo's own code documents for its in-process locks: safe against races
  within one worker process, not across multiple processes/replicas
  writing the same entity concurrently.
- **Round-trip count multiplies with per-call network latency.** A single
  `upsert_entity()` call does 3 separate read-modify-writes internally
  (the entity file, the manifest, the ops log) — roughly 10 network
  round-trips total. On a fast connection this is unnoticeable; on a slow
  or high-latency path to your storage gateway (e.g. a few seconds per
  round-trip), it adds up linearly and can turn one API call into tens of
  seconds. If you hit this, `STORAGE_BACKEND=s3` (plain boto3, native
  conditional PUT) does the same conceptual work in fewer round-trips per
  write and is the better choice if mirage's latency profile doesn't work
  for your network.

`tests/test_mirage_backend.py` exercises the full `EntityGraphStore`/
`RawFactLog` stack through `MirageBackend` (using mirage's `DiskResource`
as the mount, so the tests don't need real AWS credentials/network) — read
it for a working example of wiring `MirageBackend` up directly in code
instead of through env vars.

## Running on real S3

```bash
pip install boto3
python scripts/s3_preflight.py --bucket YOUR_BUCKET --prefix memory   # do this first
STORAGE_BACKEND=s3 S3_BUCKET=YOUR_BUCKET S3_PREFIX=memory \
  python -m uvicorn app.main:app --port 8000
```

Credentials come from the normal boto3 chain — env vars, `~/.aws/credentials`,
or an attached IAM role. Nothing AWS-specific is configured in code.

### IAM policy

`s3:ListBucket` on the **bucket** ARN is not optional, and its absence is the
most common way a correct-looking deployment fails. Without it S3 returns
`403 AccessDenied` instead of `404 NoSuchKey` for a key that does not exist —
deliberately, so it doesn't leak key existence. This service asks "does this
object exist?" on nearly every code path, so a bucket you can read and write
perfectly well will still fail on the first upsert. `s3_preflight.py` checks
for this explicitly.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ObjectAccess",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::YOUR_BUCKET/memory/*"
    },
    {
      "Sid": "ListBucketRequiredFor404Semantics",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::YOUR_BUCKET",
      "Condition": {"StringLike": {"s3:prefix": ["memory/*"]}}
    }
  ]
}
```

### Conditional writes are required

Every write goes through S3's conditional-write support: `If-None-Match: *`
to create, `If-Match: <etag>` to update. AWS shipped `If-None-Match` in
August 2024 and `If-Match` on `PutObject` in November 2024, so any current
region works — but you need a recent boto3 (`pip install -U boto3`), and
**not every S3-compatible store implements these**. Cloudflare R2 and recent
MinIO do; older MinIO/Ceph builds and various gateways do not. Without them
this backend silently loses concurrent writes, so the preflight script probes
both headers and fails loudly rather than letting you find out in production.

Point the preflight at a non-AWS endpoint with `--endpoint-url`:

```bash
python scripts/s3_preflight.py --bucket b --endpoint-url https://s3.example.com
```

### Bucket configuration

- **Do not enable versioning** without a lifecycle rule. The manifest is
  rewritten regularly, and every rewrite retains a noncurrent version you pay
  to store. Add an `NoncurrentVersionExpiration` rule (7–30 days) or use a
  dedicated bucket.
- **Add a lifecycle rule for `*/wiki/_ops/`** if you don't need audit history
  forever — it's the fastest-growing prefix.
- **Region matters.** Put the bucket in the same region as the service; every
  API call is on the critical path and cross-region latency compounds badly
  given the round-trip counts below.
- **`FLUSH_INTERVAL_SECONDS` is a real durability setting on S3.** Buffered
  manifest state lives only in this process's memory until it flushes. See
  the tradeoffs below.

### Multi-instance deployments

If you run more than one server process against the same bucket — an ASGI
worker pool, an autoscaling group, several containers — set
`MANIFEST_WRITE_MODE=sync`. The buffered manifest is per-process, so two
processes writing the same user will each hold a partial view and the last
one to flush wins, dropping the other's entries. Entity files are unaffected
(they use conditional writes), so this costs manifest accuracy, not data —
but `sync` avoids it, and `/wiki/_rebuild_manifest` repairs it.

## Write cost

Object stores charge per request, and a PUT costs ~12.5x a GET. Worse, they
have no append and no partial update: changing one byte means re-uploading
the whole object. The naive design did three synchronous PUTs per entity
write — the entity file, the whole manifest, and the whole day's ops log —
and because two of those rewrote a file that grows with use, total bytes
written grew with the SQUARE of the entity count.

Two separate fixes were needed, and it is worth being precise about which
one does what, because the first is not sufficient on its own.

**1. Write-behind buffering** (app/storage/writebehind.py) holds the
manifest and ops log in memory and flushes them on a timer. This removes
PUTs from the hot path — reads cost zero writes, and an upsert costs one
PUT instead of three.

**2. Log-structured storage** for both derived files. Buffering alone
divides a quadratic cost by the batch size; it does not change the curve.
Measured at the default batch size, a single-file manifest still grew ~3.6x
per doubling of entity count (4.0x is perfect quadratic). So neither file is
stored as one mutable object any more:

- Ops log: immutable per-flush segments under `_ops/{date}/`, folded up by
  `_compact_ops`.
- Manifest: a `_manifest/snapshot.json` plus a chain of `_manifest/d/*.json`
  deltas. A write appends a delta sized by the number of CHANGED entries,
  not by the size of the index. Deltas fold into a new snapshot once they
  reach `COMPACT_RATIO` of its size, or exceed `COMPACT_MAX_DELTAS` objects.
  Because the trigger is proportional to current size, each entry is
  rewritten O(log N) times rather than O(N) — total volume O(N log N).

Manifest bytes written, at default settings:

| entities | single file | snapshot + deltas |
|---|---|---|
| 200 | 0.093 MB | 0.054 MB |
| 400 | 0.312 MB (3.3x) | 0.108 MB (2.01x) |
| 800 | 1.127 MB (3.6x) | 0.217 MB (2.00x) |
| 1600 | ~4.2 MB (est.) | 0.438 MB (2.01x) |

2.00x per doubling is linear. The gap widens indefinitely with scale.

Per-operation request counts:

| | before | after |
|---|---|---|
| PUTs per `upsert_entity` | 3 | 1 |
| PUTs per `add_fact` | 3 | 1 |
| PUTs per `GET` (`touch=true`) | 1 | 0 |

`last_accessed` moved into the manifest. It was the hottest-written and
least valuable-to-persist field in the system — every read triggered a full
read-modify-write of the entity file just to advance a clock. It now rides
along with the buffered manifest, so reads cost zero writes.

**What you trade for this.** Entity files remain the source of truth
(ADR-001); everything else is rebuildable or audit state.

- A hard crash can lose up to `FLUSH_INTERVAL_SECONDS` of *manifest*
  updates — staleness, never data loss. `POST /wiki/_rebuild_manifest`
  reconstructs it from the entity files. Graceful shutdown flushes.
- The same crash can lose that window of *audit records*, which is a real
  loss. Set `OPS_LOG_WRITE_MODE=sync` if that is unacceptable.
- Buffering holds manifest state this process's peers cannot see until
  flush. With multiple workers writing the same user, run
  `MANIFEST_WRITE_MODE=sync`, or treat the manifest as
  eventually-consistent and lean on `/wiki/_reconcile`.
- Compaction deletes only the deltas it folded in, so a delta written
  concurrently by another process survives and is picked up on next read.

Cold reads cost 1 GET for the snapshot plus one per outstanding delta
(capped at `COMPACT_MAX_DELTAS`), then everything is served from memory.

## When every round-trip is expensive

Some S3-compatible gateways have a high fixed per-request cost. Measured
against Qiniu Kodo (cn-east-1) from Taiwan, every primitive cost ~600ms
regardless of type:

| operation | median |
|---|---|
| PUT (new key) | 699 ms |
| GET (existing) | 549 ms |
| GET (missing) | 598 ms |
| LIST (populated) | 627 ms |
| DELETE | 706 ms |
| PUT (if_match) | 1,884 ms (= 3 round-trips) |

`path_style` made no difference (680ms vs 699ms), and LIST was no worse than
GET. Nothing is pathological; the baseline round-trip is just slow. When that
is the situation, latency is simply `round-trips x RTT`, and the only lever
is doing fewer of them.

Ops per operation, and what each config change removes:

| | ops | at 620ms |
|---|---|---|
| `upsert_entity`, defaults | 5 | ~3.1s |
| with `MIRAGE_VERIFY_CONDITIONAL_WRITES=false` | 4 | ~2.5s |
| plus a long flush interval (manifest + ops amortised) | 2 | ~1.2s |
| `get_entity` | 1 | ~0.6s |

**`MIRAGE_VERIFY_CONDITIONAL_WRITES`** (default `true`) controls whether
`put_bytes(if_match=...)` re-reads the object to check its ETag before
overwriting. `_mutate` has usually just read that object, so it is the same
bytes fetched twice. Within one process the check is meaningful — `put_bytes`
holds a per-key lock across read and write, so concurrent threads cannot lose
updates. Across processes it was never a real guarantee, since mirage has no
compare-and-swap. Set it to `false` only if round-trip latency dominates and
you run a single writer.

**Flush interval matters much more than it looks.** With
`FLUSH_INTERVAL_SECONDS=2` and requests taking seconds, the buffer timer
fires between every request, so each one pays a manifest PUT and an ops-log
PUT that were meant to be amortised across many. On a slow backend use
`FLUSH_INTERVAL_SECONDS=30` and `FLUSH_MAX_PENDING=200`; the cost is a larger
crash-loss window (staleness for the manifest, recoverable with
`_rebuild_manifest`; real loss for audit records).

Use `scripts/s3_latency_probe.py` to find out whether you are in this
situation. If every primitive is uniformly slow, no code change will help
much and the answer is a closer or faster bucket.

## OKF conformance

Entity files target Google Cloud's [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
v0.2. Conformance (§11) requires only that every non-reserved `.md` file has a
parseable YAML frontmatter block containing a non-empty `type`; everything
else in the spec is guidance a consumer must tolerate the absence of.

Frontmatter leads with the spec's own field names (§4.1, §5), then producer
extensions (§4.1 "Extensions") for our internal data model:

```yaml
type: person                 # REQUIRED by §11
title: Alice Chen
description: Staff engineer on the retrieval team.
tags: [alice, engineer]
generated: { by: memory_backend/1.0, at: '2026-08-03T08:51:55+00:00' }
status: stable
# --- producer extensions below ---
okf_version: '0.2'
wiki_id: person/alice-chen
facts: [...]
relations: [...]
metadata: {...}
```

Generic OKF consumers see the spec fields and ignore the rest. Our own
reconcile/traverse/cascade read the extensions.

**Status values** follow the spec: `stable` (default), `draft`, `deprecated`.
Absent `status` ⇒ `stable` (§5.4). `generated` records provenance (§5.2).

**The body carries the summary as free-form markdown.** Relationships are
expressed as inline markdown links (§6.1), using bundle-relative form:

```markdown
works on [project/orion](/project/orion.md) (lead engineer).
```

### File layout

Every entity is a single `.md` file:

    {user_id}/wiki/{type}/{slug}.md

e.g. `default/u_123/wiki/person/alice-chen.md`

The concept ID is the path with `.md` stripped (§2): `person/alice-chen`.

Not yet done: `index.md` directory listings (§8) and `log.md` history (§9) are
optional and absent.

## Raw file attachments

    {user_id}/raw/{session_id}/{file_id}/content     the bytes, unmodified
    {user_id}/raw/{session_id}/{file_id}/meta.json   name, type, size, sha256

Files are organized by session, not by date. `POST /v1/users/{user_id}/files`
accepts multipart uploads with an optional `session_id` form field.
improvements to extraction — or a PDF reader, which does not exist yet — apply
to files uploaded before them. That only works because the original bytes are
kept.

Binary formats are stored faithfully but reported as unreadable rather than
decoded into mojibake: a model given garbled bytes invents content
confidently, which is worse than being told the file cannot be read. The same
applies to a missing attachment — the model is told, not left to assume.

Filenames are never used as storage keys. The key uses a generated id and the
name is display-only, so an awkward or hostile filename cannot escape its
prefix, collide with another upload, or overwrite anything.

Files can be cited in a fact's `evidence` as `file:{date}:{file_id}`, the same
shape as session references.

## Conversation sessions and evidence

    {user_id}/sessions/{YYYY}/{MM}/{YYYY-MM-DD}/{session_id}.jsonl

Year and month directories above the day: a flat day-level layout puts every
day of every year in one listing, so "what happened last March" means scanning
the whole history. `list_month` reads a month from one prefix, and `list_range`
lists whole months in one call each, walking day by day only at the partial
months on either end. The full date is repeated in the leaf so a path is
readable without reassembling it from three parents.

One file per conversation, not per day. Object stores have no append, so
adding a line means re-uploading the object: with a day file the Nth message
rewrites the N-1 before it, which is quadratic in messages per day. A session
file is bounded by one conversation, so the rewrite stays small however long
the system is used.

A day file also cannot answer "show me that conversation", which is what makes
a session usable as an **evidence pointer**. Facts carry
`session:{date}:{session_id}` in their `evidence` list, so "why do you believe
this?" is: find the fact through the graph (no storage reads — the index is in
memory), then one targeted GET for the exact conversation. The date is part of
the reference so resolving it is a single read rather than a search backwards
through daily listings.

The journal records each turn verbatim **and what shaped it** — which memories
were retrieved, which files were attached, which model answered. The messages
alone do not explain a surprising reply; knowing what the model was actually
given does.

`POST /chat` logs both turns automatically and returns the `session_id`; send
it back on later turns so the conversation lands in one file. This also makes
**re-extraction** possible: when the prompt or schema improves, the original
transcripts are still there. `GET /sessions/{id}?as_transcript=true` returns
exactly the shape the extractor accepts.

Retrieval here is by identity, not content. Scanning sessions for "when did we
discuss X" is O(total history) and grows without bound; that question belongs
to the graph, which answers it in constant time from the index.

Session ids are validated rather than sanitised — they go straight into an
object key, and a silently rewritten id would leave evidence pointers that no
longer resolve.

## Duplicate facts across repeated extraction

Applying extraction to overlapping conversations used to accumulate
near-identical facts. Dedup compared exact text after case and punctuation
normalisation, which does not survive the model rephrasing the same claim
between runs:

| run 1 | run 2 | exact match? |
|---|---|---|
| `Leads the retrieval workstream.` | `Alice leads the retrieval workstream.` | no |
| `The deadline is 2026-08-15.` | `Deadline is 2026-08-15.` | no |

`_is_near_duplicate` in app/extract/extractor.py compares content words after
dropping filler, with two deliberate constraints:

**Numbers are compared exactly and any difference blocks the merge**, however
similar the prose. `2026-08-15` and `2026-09-01` share every other word and
are NOT the same fact — one supersedes the other. A duplicate is untidy; a
silently swallowed correction is wrong. Negations are likewise never treated
as filler, so `uses BM25` and `does not use BM25` stay distinct.

**Containment, not Jaccard.** The commonest rephrasing is naming or implying
the subject, because facts hang off an entity. Jaccard scores
`Leads the retrieval workstream` against `Alice leads the retrieval workstream`
at 0.75 and calls them different; containment scores 1.0, which is the truth.
A size guard stops a short fact being swallowed by a longer one that merely
contains its words.

Skipped facts are reported in the plan's `rejected` list with what they
duplicated, rather than vanishing.

## Slug collisions

`wiki_id` is derived from the title by discarding every non-alphanumeric
character, so distinct titles can collide. `"C++"` and `"C#"` both become
`concept/c`. Previously the second write merged into the first and discarded
its title with no error — silent data loss.

Two different situations produce the same slug, and they need opposite
handling:

| titles | slug | correct behaviour |
|---|---|---|
| `Alice Chen` / `ALICE  chen!` | same | same entity, typed differently — merge |
| `C++` / `C#` | same | different concepts — must not merge |

`title_resolver._normalize` cannot tell them apart: it strips punctuation, so
both `C++` and `C#` normalise to `c`. `_title_key` in app/graph/store.py
case-folds and collapses whitespace but strips punctuation only at the ENDS,
because internal punctuation carries meaning while trailing punctuation does
not.

Behaviour now:

- **Variant spelling** merges as before, but the variant is recorded as an
  **alias** rather than discarded, so the other spelling still resolves later.
  The canonical title is not overwritten.
- **Genuinely different titles** raise `SlugConflictError` -> **HTTP 409**,
  carrying both titles and a ready-to-use `suggested_wiki_id`.
- **`on_conflict`** on `PUT /wiki` chooses: `error` (default), `disambiguate`
  (creates `concept/c-2`), or `merge` (the old behaviour, now opt-in and
  logged).
- **Titles with no letters or digits** are rejected with 422. `_slugify` fell
  back to the literal `"entity"`, so `"!!!"` and `"???"` both became
  `concept/entity` and collided with each other and with anything else
  unnameable.

Note the inverse defect still exists: `Node.js` and `nodejs` produce
*different* slugs but normalise identically, so they can become two entities.
`POST /wiki/resolve` catches that at write time; nothing catches it if you
call `PUT /wiki` directly with both spellings.

## Two views of the graph, two endpoints

`GET /wiki` and the graph view want different things, and serving both from one
endpoint was the actual defect behind "no pagination".

**Catalogue — `GET /wiki`.** A flat, filterable, pageable list: `limit`,
`offset`, `q` (matches title, aliases and wiki_id), `type`. Paging is correct
here because it is a list. `limit` is unset by default, so existing clients are
unaffected.

**Graph — `POST /wiki/subgraph`.** A drawable neighbourhood. Paging a graph by
index is meaningless: page 2 is a set of nodes whose edges point mostly at nodes
you were not sent, which cannot be laid out and cannot be told apart from
genuinely dangling references.

The invariant that makes it usable is CLOSURE — every returned edge has both
endpoints in `nodes`. A neighbourhood is closed by construction; a page is not.
Measured on a 500-node graph: the full index is 222 KB, a depth-2 neighbourhood
is 3 KB.

```
POST /wiki/subgraph
{"entry_wiki_ids": ["person/alice-chen"], "max_depth": 2, "max_nodes": 50}
->
{"nodes": [{wiki_id, type, title, compact, degree, hidden_neighbours, x, y}],
 "edges": [{source, target, category}],
 "truncated": false, "total_entities": 500}
```

Omit `entry_wiki_ids` to seed from the highest-degree nodes — the hubs are the
useful way into an unfamiliar graph. Expansion follows edges in BOTH
directions, or half the graph is invisible depending on which end you started
from.

`hidden_neighbours` counts neighbours that were left out, which is what lets a
UI offer an expand affordance instead of implying a node is a leaf.

### This also disposes of the O(n^2) layout problem

The GUI now seeds ~40 nodes and expands on double-click rather than drawing
everything. At 40 nodes an all-pairs force simulation is ~800 comparisons per
frame, which is nothing — the algorithm never needed replacing, the amount of
work did. A 500-node force layout is also an unreadable hairball, so rendering
it faster would not have helped.

`POST /wiki/layout` persists node coordinates (`{"positions": {wiki_id: [x, y]}}`).
The simulation then runs once rather than on every page load, and a node stays
where you left it between sessions. Positions are written on drag END and
coalesced, and survive entity edits — layout is set by a different caller than
entity writes.

## Adjacency index

`traverse()` returns wiki_ids only — never entity content — so the entire
query can be answered without opening a single entity file. Each manifest
entry therefore mirrors its entity's outbound edges as compact
`{"t": target, "c": category}` records.

| depth-2 walk | before | after |
|---|---|---|
| 31 nodes | 31 storage reads | **0** |
| 101 nodes | 101 storage reads | **0** |

Zero, because the manifest is normally already in memory. Reads scaled with
node count before; now they do not scale at all.

The index also carries `status`, so tombstoned entities and dangling
references are filtered without a file read.

**Entity files remain the source of truth.** The index is derived and
rebuildable with `POST /wiki/_rebuild_manifest`, so divergence costs accuracy,
never data. If the index were authoritative, a lost write would be lost data.

`edges=None` and `edges=[]` mean different things: `None` is "written before
the index existed, edges unknown" and triggers a one-off batched file read;
`[]` is "indexed, genuinely no edges". Without that distinction every
pre-existing manifest entry would silently look like an isolated node.

**One contract change.** traverse used to guarantee that every returned
wiki_id parsed, because it read each file. It now guarantees that every
returned wiki_id is active in the index. A corrupt entity file therefore
stays in traversal results, and fetching it returns a 422 naming the file —
more useful than a node vanishing with no explanation.

## Reducing S3 latency

Over object storage the dominant cost is round-trips, not bytes. Each GET is
tens of milliseconds of pure network wait, so what matters is how many
requests happen *in sequence*.

**Batch independent reads.** `StorageBackend.get_many(keys)` fetches many
objects at once. The base implementation is a sequential loop; `MirageBackend`
overrides it with `asyncio.gather` on its existing event loop, so N
independent reads cost roughly one round-trip instead of N.

`traverse()` uses this for level-synchronous BFS. It previously popped one
node at a time and issued a GET per node, so wall time scaled with node
count. It now fetches a whole BFS level per batch, so round-trips scale with
**depth** — which is small and bounded — rather than **breadth**, which is
not. Measured with a 30ms RTT injected:

| nodes | sequential | level-parallel | |
|---|---|---|---|
| 11 | 367 ms | 85 ms | 4.3x |
| 31 | 1041 ms | 195 ms | 5.4x |

The speedup grows with graph size. `stats()` batches the same way.

**Connection reuse (the big one on remote endpoints).**
`mirage/core/s3/_client.py` builds a new `aioboto3.Session` AND a new client
for every single operation:

```python
session = async_session(config)
async with session.client(**_client_kwargs(config)) as client:
    resp = await client.get_object(...)
```

Nothing is pooled, so every read, write, stat, listing and delete pays a full
DNS + TCP + TLS handshake and then discards the connection. A TLS handshake is
2-3 round-trips, which produces a flat per-operation floor identical across
operation types regardless of payload size. Measured against a Qiniu endpoint:

| operation | median |
|---|---|
| PUT (new key) | 699 ms |
| GET (existing) | 549 ms |
| GET (missing) | 598 ms |
| LIST (populated) | 627 ms |
| DELETE | 706 ms |

Everything within ~150ms of everything else is the signature of connection
setup, not of the work being done. `path_style` made no difference (680ms vs
699ms), which rules out the usual virtual-host DNS suspect.

`MirageBackend` patches `async_session` so the client it hands back is
long-lived and `__aexit__` is a no-op, leaving the connection open. This is
only safe because the backend owns exactly one persistent event loop —
aioboto3 clients are bound to their creating loop. Clients are closed in
`close()`. Disable with `MIRAGE_REUSE_CONNECTIONS=false`.

Other levers, in rough order of payoff:

1. **Region.** A bucket in the wrong region costs 150-250ms per request and
   swamps every other optimisation. Nothing in the code can recover that.
2. **Batch more read paths.** `reconcile()` and `rebuild()` still read entity
   files one at a time; both are whole-graph scans and are the obvious next
   `get_many` candidates.
3. **Mirage's file cache** (`MIRAGE_FILE_CACHE_LIMIT`, default 512MB) already
   serves repeat reads locally. Raising it helps read-heavy workloads. Do
   **not** raise `MIRAGE_INDEX_TTL_SECONDS` to chase listing speed — see the
   correctness warning below.
4. **Conditional writes cost an extra full GET.** `put_bytes(if_match=...)`
   re-reads the whole object to hash it, because mirage exposes no
   compare-and-swap. `stat().fingerprint` would be a HEAD instead, at the
   cost of changing the etag scheme.
5. **Fewer writes beats faster writes** — see "Write cost" above.

Use `bench.ps1` to see where time actually goes; it separates server
processing (`X-Process-Time-Ms`) from network and client overhead.

## Storage: mirage only

Storage runs through a [mirage-ai](https://github.com/strukto-ai/mirage)
`Workspace` mounting a resource at `/s3`. There is no non-mirage backend in
the service — `MirageBackend` is the only implementation `deps.py` builds.

| `STORAGE_BACKEND` | Mount | Use |
|---|---|---|
| `mirage` (default) | `S3Resource` | Real deployment. Requires `MIRAGE_S3_BUCKET`. |
| `disk` | `DiskResource` | Offline dev and tests. No credentials. |

`s3` is accepted as an alias for `mirage`, and `local` for `disk`, so older
configs keep working.

Both modes are the same `MirageBackend` differing only in the mounted
resource, so local development exercises the code that actually ships —
the Workspace ops, the conditional-write emulation, and the recursive
`list_keys`. `LocalFSBackend` is now a test double only and is not reachable
from `deps.py`.

Starting with no `MIRAGE_S3_BUCKET` set fails fast rather than silently
falling back to local disk.

## Mirage implementation notes

`mirage-ai` and `aioboto3` are hard dependencies in requirements.txt, and
`app/storage/mirage_backend.py` imports `Workspace`, `MountMode`,
`DiskResource`, `S3Config`, `S3Resource` and `FileType` at module scope, so
a broken install fails loudly at import time.

Four things to know if you touch that file:

**`list_keys` must walk, not readdir.** This is the sharpest semantic
difference between backends. LocalFSBackend uses `os.walk` and S3Backend
uses `list_objects_v2(Prefix=...)`, so both return every key beneath a
prefix at any depth. mirage's `ops.readdir()` is POSIX-style: immediate
children only, with directories as entries. Returning it directly makes
`list_keys("u/wiki/")` yield `["u/wiki/person", "u/wiki/_manifest"]`
instead of the nested `.md` files, and every caller that scans for
entity files (`manifest.rebuild`, `reconcile`, `stats`) silently sees an
empty graph. The implementation recurses via `stat().type`, which mirage
serves from its index cache after the first walk.

**Missing paths raise more than `FileNotFoundError`.** `readdir` on a
non-existent or non-directory path raises `NotADirectoryError`. Callers
expect `[]` for an absent prefix (e.g. reading the delta chain of a
manifest that has never been written), so both are caught.

**Shutdown order matters.** The write-behind buffers are attached to the
backend instance and flush by calling back into `put_bytes`, which needs
the backend's event loop alive. `MirageBackend.close()` therefore drains
those buffers before stopping the loop. Reversing that order deadlocks:
a flush timer fires after the loop is gone and blocks on a future that
never completes. `_EventLoopThread.run()` now raises immediately on a
stopped loop rather than hanging for its 30s timeout.

**The index cache TTL must stay at 0.** Every mirage Workspace ships a
two-layer cache: a FILE cache for bytes and an INDEX cache for listings and
metadata. The file cache is safe — measured, reads reflect other processes'
writes immediately. The index cache is not: at its default 600s TTL a
Workspace sees its own writes in a listing straight away but does **not**
see another process's until expiry, under both `ConsistencyPolicy.LAZY` and
`ALWAYS`. Only `ttl=0` is fresh; 1s and 5s still returned stale listings in
testing.

That is a correctness issue here, not a tuning knob, because `list_keys` is
load-bearing:

- `WikiManifest` reads its delta chain by listing `_manifest/d/` — a missed
  delta silently discards another worker's writes.
- `WikiOpsLog.read_day` lists segments — a missed segment drops audit records.
- `WikiManifest.rebuild()` lists entity files and **replaces** the index with
  what it finds. Rebuilding from a stale listing deletes entries for
  entities that still exist, and rebuild is the recovery path — so it would
  corrupt state exactly when someone is trying to repair it.

`MIRAGE_INDEX_TTL_SECONDS` defaults to `0`. Raise it only if you run a
single writer process and want the listing speedup. `MIRAGE_FILE_CACHE_LIMIT`
(default `512MB`) tunes the byte cache, which has no such hazard.

For multiple workers, mirage's `[redis]` extra provides
`RedisIndexCacheStore` / `RedisFileCacheStore` so cache state is shared
rather than per-process — this is also the natural home for PLAN.md §8's
L1 Redis layer.

**Buffers must be drained before the interpreter exits.** CPython shuts
thread pools down *before* running `atexit` handlers, so by the time
`writebehind.py`'s atexit hook fires, mirage's aiofiles executor is gone and
any flush raises "cannot schedule new futures after shutdown" — losing
buffered manifest deltas and audit records. The FastAPI lifespan hook in
`app/main.py` therefore calls `backend.close()` on graceful shutdown, which
drains the buffers while the loop is still alive. atexit is only a backstop.

**Conditional writes are best-effort.** mirage exposes no compare-and-swap,
so `put_bytes(if_match=...)` is a read-then-write with a real (narrow)
race. Safe within one process, not across replicas. Use `S3Backend` if you
need genuine atomicity — it uses S3's native `If-Match`.

### Untapped: mirage has a native `append`

`ops.append(path, data)` and ranged `ops.read(path, offset, size)` both
exist. The whole log-structured design above works around object storage
having no append — with mirage that constraint doesn't apply, so the ops
log could append to one object per day instead of writing segments. This
is not wired up: `StorageBackend` has no `append` in its interface, and
LocalFS/S3 can't implement it. It would need an optional capability flag
with a segment fallback.

## OKF schema

Every entity is stored as `{user_id}/wiki/{type}/{slug}.md` —
markdown with YAML front-matter, not raw JSON. `type` is one of `person,
organization, project, event, concept, artifact, preference, decision`
and is fixed at creation (`wiki_id` is derived from it — see
`app/graph/store.py`'s docstring for why reclassifying an entity's type
isn't supported).

The split is deliberate: **front-matter holds everything that needs to
stay strictly structured and machine-parseable** — `facts`, `relations`,
`metadata`, `status` — since cascade delete, the reconcile sweep, and the
title resolver all depend on filtering/rewriting these programmatically.
**The markdown body holds `summary`** as plain readable text, for
skimming and for dropping straight into an LLM prompt with no translation
step.

```markdown
---
okf_version: '0.2'
wiki_id: person/alice-chen
type: person
title: Alice Chen
aliases: []
compact: Software engineer on the Orion team.
facts:
- fact_id: fact_0001
  text: Works on Project Orion.
  confidence: 0.9
  evidence: []
  created_at: '2026-07-28T01:34:21.918527+00:00'
  updated_at: '2026-07-28T01:34:21.918527+00:00'
relations:
- relation_id: rel_0001
  target: project/orion
  category: related_to
  label: works_on
  weight: 1.0
  reason: works on
  evidence: []
  fact_ids: []
  created_at: '2026-07-28T01:34:21.921096+00:00'
  updated_at: '2026-07-28T01:34:21.921096+00:00'
status: stable
merged_into: null
metadata:
  significance: 0.5
  last_accessed: '2026-07-28T01:34:21.921096+00:00'
  created_at: '2026-07-28T01:34:21.904156+00:00'
  updated_at: '2026-07-28T01:34:21.921096+00:00'
  user_id: u1
---

# Alice Chen

Software engineer on the Orion team. Based in Taipei.
```

`relation.category` is a fixed, filterable enum (`related_to, contradicts,
refines, causes, temporal_before, temporal_after`) — this is what
`traverse()`/`get_edges()` filter on. `relation.label` is free-form
(`works_on`, `hosted_on`, ...) for domain meaning without needing to grow
the structural enum.

`compact` has no real summarization behind it yet — it's auto-derived as a
truncated first sentence of `summary` if you don't supply one explicitly.
`facts[].evidence` / `relations[].evidence` are references into journals
(e.g. `"journals/2026-07-23.jsonl#L12"`) — empty by default since this
service doesn't own the journals/ingestion pipeline; populate them from
whatever upstream system does.

Alongside each user's entities: a manifest (`wiki/_manifest.json`,
`wiki_id -> {type, title, aliases, compact, status, updated_at}` — avoids
directory scans, doubles as a compact-view cache per PLAN.md §9.3) and an
`_ops` audit log (`wiki/_ops/{date}_op.jsonl`, one line per write —
PLAN.md §8/ADR-005). Both are kept in sync automatically by
`EntityGraphStore`; you don't call them directly.

## API surface

Single-tenant per user: every route is scoped by `{user_id}` in the path,
matching PLAN.md's `<user_id>/wiki/...` layout exactly — no team/org
prefix. Two different `user_id`s never collide, but there's currently no
notion of a shared/multi-org boundary above that (see the auth note below
for what this does and doesn't protect against).

**Wiki entities**
- `PUT /v1/users/{user_id}/wiki` — create/update (upsert) an entity. Body: `{type, title, aliases?, summary_append?, compact?, significance?}`
- `GET /v1/users/{user_id}/wiki?type=person` — list from the manifest, optional type filter (`?include_deleted=true` to also see tombstones)
- `GET /v1/users/{user_id}/wiki/{type}/{title}` — fetch one (`?touch=false` to skip updating the retention-decay clock, `?include_deleted=true` to see a tombstone)
- `DELETE /v1/users/{user_id}/wiki/{type}/{title}` — tombstones the entity (atomic — see the note on this below), then by default also cascades (strips dangling relations other entities held pointing at it, `?cascade=false` to skip) and physically removes the file (`?hard_delete=false` to keep it as a permanent tombstone instead)
- `POST /v1/users/{user_id}/wiki/{type}/{title}/facts` — append a fact. Body: `{text, confidence?, evidence?}`
- `PATCH /v1/users/{user_id}/wiki/{type}/{title}/facts/{fact_id}` — update a fact's text/confidence/evidence
- `DELETE /v1/users/{user_id}/wiki/{type}/{title}/facts/{fact_id}` — remove a fact
- `POST /v1/users/{user_id}/wiki/{type}/{title}/relations` — link to another entity. Body: `{target_wiki_id, category?, label?, weight?, reason?, fact_ids?, evidence?, bidirectional?}`
- `GET /v1/users/{user_id}/wiki/{type}/{title}/relations?category=related_to&category=causes` — filtered edge list
- `PATCH /v1/users/{user_id}/wiki/{type}/{title}/relations/{relation_id}` — update a relation's label/weight/reason/fact_ids/evidence
- `DELETE /v1/users/{user_id}/wiki/{type}/{title}/relations/{relation_id}` — remove a relation (`?bidirectional=true` to also remove the mirrored relation on the target side, if the pair was created with `bidirectional: true`)

**Title resolver / dedup (§7.3)** — use this instead of the raw `PUT /wiki` above when you want automatic dedup/merge rather than always creating by exact `(type, title)`:
- `POST /v1/users/{user_id}/wiki/resolve` — resolves a candidate title against existing entities before writing anything. Body: `{title, type_hint?, aliases?, summary_append?, compact?, significance?}`. Returns `{action: "matched"|"created"|"inbox", wiki_id?, inbox_id?, reason, entity?}`:
  - Exactly one existing entity matches (by title, alias, word-containment like "Alice" ↔ "Alice Chen", or fuzzy similarity) → merges into it, adding the searched title as a new alias.
  - No match and `type_hint` given → creates a new entity of that type.
  - No match and no `type_hint`, or more than one ambiguous match → held in the inbox instead of guessing.
- `GET /v1/users/{user_id}/wiki/_inbox` — list held candidates awaiting manual classification
- `GET /v1/users/{user_id}/wiki/_inbox/{candidate_id}` — fetch one
- `POST /v1/users/{user_id}/wiki/_inbox/{candidate_id}/resolve` — manually assign a type (and optionally override the title). Body: `{type, title?}`. Still runs through the same matching logic scoped to that type, so it merges if there's now an unambiguous match rather than blindly creating a duplicate.
- `DELETE /v1/users/{user_id}/wiki/_inbox/{candidate_id}` — discard a candidate without creating anything

**Graph**
- `POST /v1/users/{user_id}/wiki/traverse` — BFS from one or more entry `wiki_id`s. Body: `{entry_wiki_ids, max_depth?, max_nodes?, categories?}`
- `GET /v1/users/{user_id}/wiki/_stats` — entity/edge counts
- `POST /v1/users/{user_id}/wiki/_rebuild_manifest` — rebuild the manifest from the entity files (repair/migration; the only full directory scan here)
- `POST /v1/users/{user_id}/wiki/_compact_ops?on=YYYY-MM-DD` — fold a day's ops-log segments into one object
- `POST /v1/users/{user_id}/wiki/_reconcile` — full sweep for dangling relations (relations pointing at entities that no longer exist). Idempotent — safe to call on a schedule or on demand. Returns `{entities_scanned, entities_fixed, relations_removed}`

**Raw fact log** (separate feature, unaffected by the OKF migration)
- `POST /v1/users/{user_id}/raw-facts` — append a batch of facts to today's shard
- `GET /v1/users/{user_id}/raw-facts?on=2026-07-24` — read one day's shard
- `GET /v1/users/{user_id}/raw-facts/range?start=...&end=...` — read across days (capped at 366 days per request)

**Health**
- `GET /healthz` (alias: `/health`)

## Structure

```
app/
  main.py             - FastAPI app factory, logging, exception handlers
  config.py            - env-based Settings
  deps.py               - dependency injection (backend, stores)
  storage/backend.py     - StorageBackend / LocalFSBackend / S3Backend
  storage/writebehind.py  - FlushBuffer: write-behind batching + tradeoff notes
  storage/mirage_backend.py - StorageBackend over a mirage-ai Workspace /s3 mount
  storage/mirage_backend.py - StorageBackend on top of a mirage-ai Workspace
  graph/
    store.py                - EntityGraphStore, OKF Entity/Fact/Relation/Metadata
    manifest.py               - WikiManifest (avoids directory scans, compact-view cache, buffered)
    ops_log.py                  - WikiOpsLog (§8/ADR-005 audit log, segment-based)
    title_resolver.py             - WikiTitleResolver + WikiInbox (§7.3 dedup/merge)
  rawlog/log.py            - RawFactLog (separate feature, sharded JSONL)
  api/
    models.py               - pydantic request/response schemas
    routes_entities.py       - wiki entity + graph routes
    routes_rawlog.py          - raw fact log routes
    routes_health.py           - /healthz
tests/
  test_api.py                  - 26 tests covering the wiki/graph/raw-log routes
  test_mirage_backend.py         - 5 tests running the same store through mirage-ai
  test_cascade_resilience.py       - 3 tests covering cascade/reconcile failure resilience
  test_title_resolver.py             - 13 tests covering §7.3 dedup, merge, and the inbox workflow
  test_writebehind.py                  - 13 tests pinning the write-cost properties and recovery paths
  test_s3_backend.py                   - 12 tests for S3Backend via moto: conditional writes, prefixing, IAM failure modes
scripts/
  crud_timing_test.py             - runs the full CRUD cycle against a live server, times each step
```

## Things this does NOT do (be aware before treating it as done)

- **Out of scope by design**: Redis session layer, journals, raw/source
  ingestion, file processing, persistence classification, daily jobs, TTL
  cleanup. This service only owns the wiki/graph layer — see `PLAN.md` for
  where the rest fits.
- **Title resolver matching is deliberately simple, not ML-based.** It's
  exact/alias/word-containment/fuzzy-string matching (see
  `app/graph/title_resolver.py`) — explainable and debuggable, but it
  will miss genuine matches that don't share words or spelling (e.g. a
  nickname with no textual overlap, or a name in a different script) and
  can occasionally over-match on short, generic titles. It's also only
  used if you call `POST /wiki/resolve` — the raw `PUT /wiki` endpoint
  still creates/updates by exact `(type, title)` with no dedup, since
  sometimes you genuinely want that (e.g. you already know the canonical
  wiki_id and don't want fuzzy matching in the way).
- **No auth, and no tenant isolation above `user_id`.** `{user_id}` in the
  path is trusted as-is — anyone who can reach this API can read/write any
  user_id's data just by putting a different value in the URL. Add real
  auth (API keys, JWT, whatever your gateway expects) before exposing this
  beyond a trusted internal network. This is a bigger gap than the old
  `X-Team-Id` header ever protected against — that header was never a
  security boundary either, just a namespacing convenience, and it's been
  removed entirely (stripped to match PLAN.md's single-tenant-per-user
  design exactly).
- **`compact` is a truncation stopgap**, not real summarization — see the
  schema section above.
- **`traverse()` does live sequential file reads**, not a prebuilt
  adjacency index. Fine at shallow depth / modest entity counts; won't hit
  PLAN.md's ~500ms retrieval SLA at scale without an index-builder pass
  layered on top later.
- **Referential integrity on delete is tombstone-based**, closing the
  correctness race that used to exist here. `delete_entity()` first does
  ONE atomic write — flipping `status` to `"deprecated"` — before anything
  else happens. Every read path (`get_entity()`, `traverse()`,
  `list_entities()`) checks that status, not just file existence, so the
  moment that single write lands, the entity is instantly and consistently
  invisible everywhere — no reader can ever observe it as "existing" again,
  regardless of whether the cascade cleanup or the physical file removal
  has finished yet. `DELETE .../wiki/{type}/{title}?hard_delete=false`
  keeps the tombstone permanently instead of reclaiming the file, as an
  audit trail (`?include_deleted=true` on the GET/list routes to see it).

  **Cascade and reconcile are also resilient, not just present.** A
  failure fixing any ONE entity's dangling relation — a write conflict
  that exhausts its retries, a transient storage error — no longer aborts
  the whole scan. `cascade_orphaned_relations()` and
  `reconcile_dangling_relations()` both continue past individual failures,
  fixing everything they can and returning which entities failed
  (`{"fixed": [...], "failed": [...]}` / `entities_failed` respectively) so
  a caller can see and retry specifically those. And critically,
  `delete_entity()` never lets a cascade failure turn a successful delete
  into a failed API call — the tombstone write already committed before
  cascade even starts, so the DELETE request still succeeds either way;
  any resulting dangling relations elsewhere are left for the next
  `/wiki/_reconcile` sweep, which itself is safe to rerun until everything
  converges (rerunning after a transient failure clears simply fixes
  whatever's left).
- **No vector or keyword index** — PLAN.md's retrieval design assumes all
  three (vector/graph/keyword) run in parallel; only graph exists here.
- **No candidate → hydrate → rerank → context-pack retrieval layer.**
  Everything here is CRUD + traversal primitives; PLAN.md §9.2's higher-level
  retrieval flow would sit on top of this, not inside it.
- **Conditional S3 writes need a recent boto3** (`pip install -U boto3`) —
  AWS added native `If-Match` support to S3 PutObject in 2024.
