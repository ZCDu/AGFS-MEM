"""
Environment-based settings. No pydantic-settings dependency — plain and explicit.

Env vars:
  DEEPSEEK_API_KEY     enables the LLM extraction layer (POST /extract).
                         Without it the rest of the service works normally and
                         /extract returns 503 — memory storage must not depend
                         on a third party being reachable.
  LLM_BASE_URL         default https://api.deepseek.com/v1. Any
                         OpenAI-compatible endpoint works (OpenAI, Together,
                         Ollama, vLLM) by changing this and LLM_MODEL.
  LLM_MODEL            default deepseek-chat
  LLM_TIMEOUT_SECONDS  default 60
  OKF_MODE             "companion" (default) or "frontmatter".
                         companion: the .okf.md carries only the fields OKF
                           §4.1 defines and points at a sibling .okf.json via
                           `resource`. Matches the spec's own examples; costs
                           one extra write per entity change.
                         frontmatter: one self-contained file with the
                           structured data in the YAML block. Fewer writes,
                           but ~40 lines of frontmatter.
                         Both are conformant. Reads accept either, so
                         switching needs no migration.

  LLM_MAX_TOKENS       default 10000. Extraction from a long conversation
                         produces a lot of JSON; at the old 2000 the reply was
                         cut off mid-object and failed to parse. Also the cap
                         for one chat reply (the chat path previously hardcoded
                         1200 and truncated mid-JSON on long answers). Raise
                         further if you extract very long transcripts.
  LLM_MIN_DECISION     "review" (default) or "store". The assessor verdict a
                         conversation must reach before a model call is spent
                         on it. "store" is stricter and cheaper.

  AUTO_CAPTURE_ENABLED   "false" (default) or "true". When true, a background
                         timer reads each day's journaled sessions and runs the
                         same review-extraction + apply pipeline automatically
                         (no UI button), routing new topics into properly-named
                         wikis and continuations back into the wiki they belong
                         to. Off by default so nothing writes on its own until a
                         deployment opts in.
  AUTO_CAPTURE_TIME      "<HH:MM>" time of day (24h) to run the daily capture,
                         in the process's local timezone (default "22:00").
  AUTO_CAPTURE_TZ        IANA timezone for AUTO_CAPTURE_TIME, e.g. "Asia/Shanghai"
                         (default: the host's local timezone).
  AUTO_CAPTURE_LOOKBACK_DAYS  default 0. The capture runs for the SAME day
                         when 0 (the night's run captures that day's journal),
                         or N days back for older ones. 1 = "yesterday" (the
                         prior day's journal, final once it has ended) --
                         useful if a deployment wants a day to fully settle
                         before capturing it.
  AUTO_CAPTURE_BACKFILL_DAYS default 7. On startup the timer walks back this
                         many days and captures any day the cursor has not
                         already processed, so days missed while the service
                         was down are caught up automatically (idempotent —
                         nothing is ever double-stored).

  DIARY_ENABLED        "true" (default) or "false". When true (and
                         AUTO_CAPTURE_ENABLED=true), after each capture run the
                         timer also generates a daily "diary" (a chronological
                         AI summary of the user's actions) for every user with
                         a home wiki, stored under their home wiki and visible
                         to them + admins. Off forces the capture to run
                         without any diary generation.

  AUTH_MODE            "token" (default) or "off". Token mode requires
                         AUTH_TOKENS or AUTH_ADMIN_TOKEN and the app refuses
                         to start without one, so an unauthenticated API can
                         never be shipped by accident. "off" disables auth
                         entirely and warns loudly at startup; local dev only.
  AUTH_TOKENS          "token:user_id" pairs, comma-separated. Two tokens may
                         map to the same user, which is how you rotate without
                         downtime.
  AUTH_ADMIN_TOKEN     optional token that may access any user_id. Intended
                         for cross-tenant maintenance jobs.
  WIKI_CREATE_REQUIRES_ADMIN
                         "false" (default) lets any authenticated user create
                         a wiki, which is what the router's auto-creation
                         needs. Set "true" to restrict it; auto-creation then
                         fails rather than silently doing nothing.

  AUTH_SECRET          HMAC key for signing login session tokens. Required to
                         enable username/password login. Rotating it
                         invalidates every existing session, which is the only
                         bulk revocation mechanism (sessions are stateless).
  AUTH_SESSION_HOURS   session lifetime, default 12. Keep it short: an
                         individual session cannot be revoked before expiry.
  AUTH_LOGIN_MAX_ATTEMPTS / AUTH_LOGIN_WINDOW_SECONDS
                         login throttle, default 8 failures per 300s per
                         username+client. Without this, passwords are
                         brute-forceable at network speed.

  STORAGE_BACKEND      "mirage" (default) or "disk".
                         Storage always runs through a mirage Workspace.
                         "mirage" mounts an S3Resource at /s3 — the real
                         deployment, needs MIRAGE_S3_BUCKET.
                         "disk" mounts a DiskResource at /s3 — offline
                         development and tests, no credentials needed.
                         ("s3" is accepted as an alias for "mirage" and
                         "local" for "disk", so older configs keep working.)
  LOCAL_BUCKET_ROOT     directory mounted at /s3 when STORAGE_BACKEND=disk
                         (default ./local_bucket)
  S3_BUCKET              required when STORAGE_BACKEND=s3
  S3_PREFIX              optional key prefix within the bucket (default "")

  MANIFEST_WRITE_MODE   "buffered" (default) or "sync". Buffered keeps the
                          manifest in memory and flushes it on a timer,
                          collapsing N entity writes into 1 PUT. Use "sync"
                          if more than one process writes the same user.
  OPS_LOG_WRITE_MODE    "buffered" (default) or "sync". Sync writes one
                          audit segment per op before returning; buffered
                          batches them and can lose a flush window on a hard
                          crash.
  FLUSH_INTERVAL_SECONDS  default 2.0 — max staleness/loss window.
  FLUSH_MAX_PENDING       default 100 — flush early after this many ops.

  CONFLICT_CHECK_ENABLED  "false" (default) or "true". When true, writes to
                          the entity graph (add_fact / link_entities /
                          upsert_entity) run Semantica-backed conflict
                          detection; detected contradictions are stored as
                          OKF conflict records under wikis/{scope}/_conflicts/
                          and the write still lands (flag-only) unless a
                          resolution strategy is set.
  CONFLICT_RESOLUTION_STRATEGY  default "" (flag-only). One of
                          most_recent|highest_confidence|voting|
                          credibility_weighted|first_seen. When set, a
                          detected conflict blocks/decides the write via
                          Semantica's resolver instead of just flagging it.

  EMBEDDING_ENABLED      "false" (default) or "true". When true, entity
                          search (GET /wiki, GET /entities, chat retrieval)
                          falls back to a local-embedding semantic match
                          when lexical/bigram matching finds nothing —
                          see app/graph/embeddings.py. Requires the optional
                          `FlagEmbedding` package and the bge-m3 model
                          downloaded locally; degrades to lexical-only
                          (never crashes) if either is missing. Off by
                          default, and the fallback only ever runs on a
                          lexical miss — most searches never touch it.
  EMBEDDING_SIMILARITY_FLOOR  default 0.5. Minimum cosine similarity for a
                          semantic-fallback match to count as a hit.

  When STORAGE_BACKEND=mirage (routes storage through a mirage-ai Workspace's
  /s3 mount instead of raw boto3 — see app/storage/mirage_backend.py):
  MIRAGE_S3_BUCKET, MIRAGE_S3_REGION, MIRAGE_S3_ENDPOINT_URL,
  MIRAGE_S3_ACCESS_KEY_ID, MIRAGE_S3_SECRET_ACCESS_KEY, MIRAGE_S3_SESSION_TOKEN,
  MIRAGE_S3_PROFILE, MIRAGE_S3_PATH_STYLE ("true"/"false"), MIRAGE_S3_KEY_PREFIX
  — same shape as the original repo's mirage_s3_* settings, so if you're
  reusing that repo's bucket/credentials you can copy the values across.
  MIRAGE_S3_ENDPOINT_URL is what lets this point at an S3-compatible
  gateway (e.g. Qiniu Kodo) instead of AWS S3 itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    storage_backend: str
    local_bucket_root: str
    s3_bucket: str | None
    s3_prefix: str

    # Write-behind tuning — see app/storage/writebehind.py for the
    # durability/cost tradeoff these control.
    llm_api_key: str | None
    llm_base_url: str
    llm_model: str
    llm_timeout_seconds: float
    llm_max_tokens: int
    okf_mode: str
    llm_min_decision: str

    auth_mode: str
    auth_tokens: str
    auth_admin_token: str | None
    auth_secret: str | None
    wiki_create_requires_admin: bool
    auth_session_hours: float
    auth_login_max_attempts: int
    auth_login_window_seconds: float

    auto_capture_enabled: bool
    auto_capture_time: str
    auto_capture_tz: str
    auto_capture_lookback_days: int
    auto_capture_backfill_days: int
    diary_enabled: bool

    mirage_reuse_connections: bool
    mirage_index_ttl_seconds: float
    mirage_file_cache_limit: str
    mirage_verify_conditional_writes: bool

    manifest_write_mode: str
    ops_log_write_mode: str
    flush_interval_seconds: float
    flush_max_pending: int

    conflict_check_enabled: bool
    conflict_resolution_strategy: str

    embedding_enabled: bool
    embedding_similarity_floor: float

    mirage_s3_bucket: str | None
    mirage_s3_region: str | None
    mirage_s3_endpoint_url: str | None
    mirage_s3_access_key_id: str | None
    mirage_s3_secret_access_key: str | None
    mirage_s3_session_token: str | None
    mirage_s3_profile: str | None
    mirage_s3_path_style: bool
    mirage_s3_key_prefix: str | None

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            storage_backend=os.environ.get("STORAGE_BACKEND", "mirage").lower(),
            local_bucket_root=os.environ.get("LOCAL_BUCKET_ROOT", "./local_bucket"),
            s3_bucket=os.environ.get("S3_BUCKET"),
            s3_prefix=os.environ.get("S3_PREFIX", ""),
            llm_api_key=(os.environ.get("DEEPSEEK_API_KEY")
                         or os.environ.get("LLM_API_KEY") or None),
            llm_base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1"),
            llm_model=os.environ.get("LLM_MODEL", "deepseek-chat"),
            llm_timeout_seconds=float(os.environ.get("LLM_TIMEOUT_SECONDS", "60")),
            llm_max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "10000")),
            okf_mode=os.environ.get("OKF_MODE", "companion").strip().lower(),
            llm_min_decision=os.environ.get("LLM_MIN_DECISION", "review").strip().lower(),
            auth_mode=os.environ.get("AUTH_MODE", "token").strip().lower(),
            auth_tokens=os.environ.get("AUTH_TOKENS", ""),
            auth_admin_token=os.environ.get("AUTH_ADMIN_TOKEN") or None,
            auth_secret=os.environ.get("AUTH_SECRET") or None,
            wiki_create_requires_admin=os.environ.get(
                "WIKI_CREATE_REQUIRES_ADMIN", "false").lower() in ("1", "true", "yes"),
            auth_session_hours=float(os.environ.get("AUTH_SESSION_HOURS", "12")),
            auth_login_max_attempts=int(os.environ.get("AUTH_LOGIN_MAX_ATTEMPTS", "8")),
            auth_login_window_seconds=float(
                os.environ.get("AUTH_LOGIN_WINDOW_SECONDS", "300")),
            auto_capture_enabled=os.environ.get(
                "AUTO_CAPTURE_ENABLED", "false").lower() in ("1", "true", "yes"),
            auto_capture_time=os.environ.get("AUTO_CAPTURE_TIME", "22:00"),
            auto_capture_tz=os.environ.get("AUTO_CAPTURE_TZ", ""),
            auto_capture_lookback_days=int(
                os.environ.get("AUTO_CAPTURE_LOOKBACK_DAYS", "0")),
            auto_capture_backfill_days=int(
                os.environ.get("AUTO_CAPTURE_BACKFILL_DAYS", "7")),
            diary_enabled=os.environ.get("DIARY_ENABLED", "true").lower()
            in ("1", "true", "yes"),
            mirage_reuse_connections=os.environ.get(
                "MIRAGE_REUSE_CONNECTIONS", "true").lower() in ("1", "true", "yes"),
            mirage_index_ttl_seconds=float(os.environ.get("MIRAGE_INDEX_TTL_SECONDS", "0")),
            mirage_file_cache_limit=os.environ.get("MIRAGE_FILE_CACHE_LIMIT", "512MB"),
            mirage_verify_conditional_writes=os.environ.get(
                "MIRAGE_VERIFY_CONDITIONAL_WRITES", "true").lower() in ("1", "true", "yes"),
            manifest_write_mode=os.environ.get("MANIFEST_WRITE_MODE", "buffered").lower(),
            ops_log_write_mode=os.environ.get("OPS_LOG_WRITE_MODE", "buffered").lower(),
            flush_interval_seconds=float(os.environ.get("FLUSH_INTERVAL_SECONDS", "2.0")),
            flush_max_pending=int(os.environ.get("FLUSH_MAX_PENDING", "100")),
            conflict_check_enabled=os.environ.get(
                "CONFLICT_CHECK_ENABLED", "false").lower() in ("1", "true", "yes"),
            conflict_resolution_strategy=os.environ.get(
                "CONFLICT_RESOLUTION_STRATEGY", "").strip().lower(),
            embedding_enabled=os.environ.get(
                "EMBEDDING_ENABLED", "false").lower() in ("1", "true", "yes"),
            embedding_similarity_floor=float(
                os.environ.get("EMBEDDING_SIMILARITY_FLOOR", "0.5")),
            mirage_s3_bucket=os.environ.get("MIRAGE_S3_BUCKET"),
            mirage_s3_region=os.environ.get("MIRAGE_S3_REGION"),
            mirage_s3_endpoint_url=os.environ.get("MIRAGE_S3_ENDPOINT_URL"),
            mirage_s3_access_key_id=os.environ.get("MIRAGE_S3_ACCESS_KEY_ID"),
            mirage_s3_secret_access_key=os.environ.get("MIRAGE_S3_SECRET_ACCESS_KEY"),
            mirage_s3_session_token=os.environ.get("MIRAGE_S3_SESSION_TOKEN"),
            mirage_s3_profile=os.environ.get("MIRAGE_S3_PROFILE"),
            mirage_s3_path_style=os.environ.get("MIRAGE_S3_PATH_STYLE", "false").lower() == "true",
            mirage_s3_key_prefix=os.environ.get("MIRAGE_S3_KEY_PREFIX", "memory_backend/"),
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings
