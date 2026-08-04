"""
FastAPI dependency providers. Storage backend and stores are built once
(process-lifetime singletons via lru_cache) and injected per-request.
"""

from __future__ import annotations

from functools import lru_cache

from app.config import Settings, get_settings
from app.graph.store import EntityGraphStore
from app.graph.title_resolver import WikiTitleResolver
from app.rawlog.log import RawFactLog
from app.storage.backend import StorageBackend
from app.storage.mirage_backend import MirageBackend


@lru_cache
def get_storage_backend() -> StorageBackend:
    settings = get_settings()
    return _build_backend(settings)


def _build_backend(settings: Settings) -> StorageBackend:
    """Storage always goes through a mirage Workspace.

    There is no non-mirage backend here on purpose. S3-via-mirage is the
    storage layer this service is built on, not one option among several,
    so both modes below are the same MirageBackend differing only in which
    resource is mounted at /s3:

      mirage  - S3Resource. The real deployment.
      disk    - DiskResource. Offline development and tests, with no AWS
                credentials required. Same Workspace, same ops, same
                conditional-write emulation, same recursive list_keys — so
                what runs locally is the code that ships.
    """
    if settings.storage_backend in ("mirage", "s3"):
        if not settings.mirage_s3_bucket:
            raise RuntimeError(
                "STORAGE_BACKEND=%s requires MIRAGE_S3_BUCKET to be set"
                % settings.storage_backend
            )
        return MirageBackend.from_s3_config(
            bucket=settings.mirage_s3_bucket,
            region=settings.mirage_s3_region,
            endpoint_url=settings.mirage_s3_endpoint_url,
            aws_access_key_id=settings.mirage_s3_access_key_id,
            aws_secret_access_key=settings.mirage_s3_secret_access_key,
            aws_session_token=settings.mirage_s3_session_token,
            aws_profile=settings.mirage_s3_profile,
            path_style=settings.mirage_s3_path_style,
            key_prefix=settings.mirage_s3_key_prefix,
            index_ttl=settings.mirage_index_ttl_seconds,
            file_cache_limit=settings.mirage_file_cache_limit,
            reuse_connections=settings.mirage_reuse_connections,
            verify_conditional_writes=settings.mirage_verify_conditional_writes,
        )
    if settings.storage_backend in ("disk", "local"):
        return MirageBackend.from_disk(
            root=settings.local_bucket_root,
            index_ttl=settings.mirage_index_ttl_seconds,
            file_cache_limit=settings.mirage_file_cache_limit,
            reuse_connections=settings.mirage_reuse_connections,
            verify_conditional_writes=settings.mirage_verify_conditional_writes,
        )
    raise RuntimeError(
        f"Unknown STORAGE_BACKEND={settings.storage_backend!r}, expected 'mirage' or 'disk'"
    )


def get_graph_store() -> EntityGraphStore:
    return EntityGraphStore(get_storage_backend())


def get_title_resolver() -> WikiTitleResolver:
    return WikiTitleResolver(get_graph_store())


def get_raw_log() -> RawFactLog:
    return RawFactLog(get_storage_backend())


@lru_cache(maxsize=1)
def get_llm_client() -> "LLMClient":
    """Cached: it holds no connection, but rebuilding it per request would
    re-read settings needlessly."""
    from app.extract.llm import LLMClient
    s = get_settings()
    return LLMClient(api_key=s.llm_api_key, base_url=s.llm_base_url,
                     model=s.llm_model, timeout=s.llm_timeout_seconds)


def get_extractor() -> "ConversationExtractor":
    from app.extract.extractor import ConversationExtractor
    settings = get_settings()
    extractor = ConversationExtractor(get_graph_store(), get_llm_client(),
                                      min_decision=settings.llm_min_decision)
    extractor.max_tokens = settings.llm_max_tokens
    return extractor
