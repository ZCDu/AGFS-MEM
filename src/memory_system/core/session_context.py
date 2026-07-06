import hashlib
import json
from datetime import datetime, timezone
from typing import Protocol

from memory_system.config import Settings
from memory_system.utils.text_utils import extract_text


class SessionContextStrategy(Protocol):
    """Strategy interface for compressing irrelevant older session rounds."""

    async def compress_round(
        self,
        redis,
        *,
        user_id: str,
        session_id: str,
        round_entry: dict,
        kept_message: dict,
        omitted_messages: list[dict],
        query_text: str,
    ) -> dict:
        ...

    async def retrieve(self, redis, context_hash: str) -> dict | None:
        ...


class OmitSessionContextStrategy:
    """Original irreversible behavior: replace old response with a placeholder."""

    async def compress_round(
        self,
        redis,
        *,
        user_id: str,
        session_id: str,
        round_entry: dict,
        kept_message: dict,
        omitted_messages: list[dict],
        query_text: str,
    ) -> dict:
        return {"role": "assistant", "content": "[previous response omitted]"}

    async def retrieve(self, redis, context_hash: str) -> dict | None:
        return None


class ReversibleSessionContextStrategy:
    """Headroom-style reversible behavior: marker in history, originals in Redis."""

    def __init__(self, ttl_seconds: int):
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def _key(context_hash: str) -> str:
        return f"memory:ctx:{context_hash}"

    @staticmethod
    def _hash_payload(user_id: str, session_id: str, round_id: str, messages: list[dict]) -> str:
        payload = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(
            f"{user_id}\0{session_id}\0{round_id}\0{payload}".encode()
        ).hexdigest()[:24]

    async def compress_round(
        self,
        redis,
        *,
        user_id: str,
        session_id: str,
        round_entry: dict,
        kept_message: dict,
        omitted_messages: list[dict],
        query_text: str,
    ) -> dict:
        context_hash = self._hash_payload(
            user_id,
            session_id,
            round_entry.get("round_id", ""),
            omitted_messages,
        )
        now = datetime.now(timezone.utc).isoformat()
        omitted_text = "\n".join(
            extract_text(message.get("content", ""))
            for message in omitted_messages
            if extract_text(message.get("content", ""))
        )
        kept_text = extract_text(kept_message.get("content", ""))

        await redis.hset(
            self._key(context_hash),
            mapping={
                "hash": context_hash,
                "user_id": user_id,
                "session_id": session_id,
                "round_id": round_entry.get("round_id", ""),
                "kept_message": json.dumps(kept_message, ensure_ascii=False),
                "omitted_messages": json.dumps(omitted_messages, ensure_ascii=False),
                "query_text": query_text,
                "kept_text": kept_text,
                "omitted_text": omitted_text,
                "created_at": now,
            },
        )
        await redis.expire(self._key(context_hash), self._ttl_seconds)

        return {
            "role": "assistant",
            "content": (
                "[previous response omitted] "
                f"Retrieve more: hash={context_hash}]"
            ),
        }

    async def retrieve(self, redis, context_hash: str) -> dict | None:
        raw = await redis.hgetall(self._key(context_hash))
        if not raw:
            return None
        omitted_messages = json.loads(raw.get("omitted_messages", "[]"))
        return {
            "hash": raw.get("hash", context_hash),
            "user_id": raw.get("user_id", ""),
            "session_id": raw.get("session_id", ""),
            "round_id": raw.get("round_id", ""),
            "messages": omitted_messages,
            "query_text": raw.get("query_text", ""),
            "created_at": raw.get("created_at", ""),
        }


def create_session_context_strategy(settings: Settings) -> SessionContextStrategy:
    strategy = settings.history_compression_strategy.lower()
    if strategy == "omit":
        return OmitSessionContextStrategy()
    if strategy == "reversible":
        return ReversibleSessionContextStrategy(settings.context_compression_ttl_seconds)
    raise ValueError(
        "Unsupported history_compression_strategy "
        f"{settings.history_compression_strategy!r}; expected 'omit' or 'reversible'"
    )
