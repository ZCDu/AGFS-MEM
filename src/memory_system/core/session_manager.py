import asyncio
import json
import uuid
from datetime import datetime, timezone
from memory_system.config import Settings
from memory_system.core.session_context import (
    SessionContextStrategy,
    create_session_context_strategy,
)
from memory_system.utils.text_utils import extract_text


class SessionManager:
    def __init__(
        self,
        settings: Settings,
        context_strategy: SessionContextStrategy | None = None,
    ):
        self._settings = settings
        self._context_strategy = context_strategy or create_session_context_strategy(settings)

    def _key(self, user_id: str, session_id: str) -> str:
        return f"memory:sess:{user_id}:{session_id}"

    async def get_session(self, redis, user_id: str, session_id: str) -> dict:
        raw = await redis.hgetall(self._key(user_id, session_id))
        if not raw:
            return {"rounds": []}
        return {
            "rounds": json.loads(raw.get("rounds", "[]")),
        }

    async def add_round(
        self, redis, user_id: str, session_id: str, messages: list[dict]
    ):
        """Store messages as one round. No embedding — uses text similarity instead."""
        key = self._key(user_id, session_id)
        lock_key = f"{key}:lock"
        token = uuid.uuid4().hex

        await self._acquire_lock(redis, lock_key, token)
        try:
            await self._add_round_unlocked(redis, user_id, session_id, messages)
        finally:
            await self._release_lock(redis, lock_key, token)

    async def _add_round_unlocked(
        self, redis, user_id: str, session_id: str, messages: list[dict]
    ):
        key = self._key(user_id, session_id)
        session = await self.get_session(redis, user_id, session_id)
        now = datetime.now(timezone.utc).isoformat()

        # Extract first user text for later relevance matching
        first_user_text = ""
        for msg in messages:
            if msg.get("role") == "user":
                first_user_text = extract_text(msg.get("content", ""))
                break
        # NOTE：提取了当前请求的首条用户问句，方便后续做相似度过滤用
        round_entry = {
            "round_id": uuid.uuid4().hex[:8],
            "messages": messages,
            "first_user_text": first_user_text,
        }
        session["rounds"].append(round_entry)

        max_rounds = self._settings.archived_rounds_max
        if len(session["rounds"]) > max_rounds:
            session["rounds"] = session["rounds"][-max_rounds:]

        await redis.hset(
            key,
            mapping={
                "rounds": json.dumps(session["rounds"], ensure_ascii=False),
                "updated_at": now,
            },
        )
        await redis.expire(key, self._settings.session_ttl_seconds)

    @staticmethod
    async def _acquire_lock(redis, lock_key: str, token: str):
        for _ in range(20):
            acquired = await redis.set(lock_key, token, nx=True, ex=5)
            if acquired:
                return
            await asyncio.sleep(0.05)
        raise TimeoutError(f"Could not acquire session lock: {lock_key}")

    @staticmethod
    async def _release_lock(redis, lock_key: str, token: str):
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        end
        return 0
        """
        await redis.eval(script, 1, lock_key, token)

    async def build_history(
        self,
        redis,
        user_id: str,
        session_id: str,
        query_text: str = "",
        recent_rounds_full: int | None = None,
    ) -> dict:
        """Build history with relevance-based compression.

        Relevance is determined by bigram overlap coefficient between
        the query text and each round's first user message — no embedding API calls.

        The most recent N rounds are always kept in full (default from settings,
        overridable via recent_rounds_full parameter). Older rounds are either
        fully shown (if relevant to query) or compressed to Q-only + placeholder.
        """
        session = await self.get_session(redis, user_id, session_id)
        rounds = session["rounds"]
        if not rounds:
            return {"history_messages": [], "history_str": ""}

        full_count = recent_rounds_full if recent_rounds_full is not None else self._settings.recent_rounds_full
        full_count = min(full_count, len(rounds))

        older_rounds = rounds[:-full_count] if full_count > 0 else rounds
        recent_rounds = rounds[-full_count:] if full_count > 0 else []

        threshold = self._settings.relevance_threshold
        history_messages = []
        history_str_parts = []

        for entry in older_rounds:
            round_text = entry.get("first_user_text", "")
            relevant = (
                query_text
                and round_text
                and self._bigram_overlap(query_text, round_text) >= threshold
            )

            if relevant:
                for msg in entry["messages"]:
                    history_messages.append(msg)
                    text = extract_text(msg.get("content", ""))
                    if text:
                        history_str_parts.append(f"{msg['role']}: {text}")
            else:
                if not entry.get("messages"):
                    continue
                q_msg = entry["messages"][0]
                q_text = extract_text(q_msg.get("content", ""))
                if len(q_text) > 200:
                    q_text = q_text[:200] + "..."
                history_str_parts.append(f"{q_msg['role']}: {q_text}")
                history_messages.append(q_msg)
                omitted_messages = entry["messages"][1:]
                marker_msg = await self._context_strategy.compress_round(
                    redis,
                    user_id=user_id,
                    session_id=session_id,
                    round_entry=entry,
                    kept_message=q_msg,
                    omitted_messages=omitted_messages,
                    query_text=query_text,
                )
                history_str_parts.append(f"assistant: {marker_msg['content']}")
                history_messages.append(marker_msg)

        # Recent rounds: always full, no bigram filtering
        for entry in recent_rounds:
            for msg in entry["messages"]:
                history_messages.append(msg)
                text = extract_text(msg.get("content", ""))
                if text:
                    history_str_parts.append(f"{msg['role']}: {text}")

        return {
            "history_messages": history_messages,
            "history_str": "\n".join(history_str_parts),
        }

    async def retrieve_context(self, redis, context_hash: str) -> dict | None:
        return await self._context_strategy.retrieve(redis, context_hash)

    @staticmethod
    def _bigrams(text: str) -> set[str]:
        """Character bigrams — language-agnostic, no tokenizer needed."""
        t = text.lower()
        return {t[i : i + 2] for i in range(len(t) - 1)}

    @staticmethod
    def _bigram_overlap(a: str, b: str) -> float:
        """Overlap coefficient: |A ∩ B| / min(|A|, |B|).

        Better than standard Jaccard for short-vs-long text comparison.
        A short query matching a long round gets a fair score.
        """
        ba = SessionManager._bigrams(a)
        bb = SessionManager._bigrams(b)
        if not ba or not bb:
            return 0.0
        return len(ba & bb) / min(len(ba), len(bb))
