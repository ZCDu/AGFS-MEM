import json
import math
import uuid
from datetime import datetime, timezone
from memory_system.config import Settings
from memory_system.utils.text_utils import extract_text


class SessionManager:
    def __init__(self, settings: Settings, embedding_client):
        self._settings = settings
        self._embedding = embedding_client

    def _key(self, user_id: str, session_id: str) -> str:
        return f"memory:sess:{user_id}:{session_id}"

    async def get_session(self, redis, user_id: str, session_id: str) -> dict:
        """Load session data from Redis."""
        raw = await redis.hgetall(self._key(user_id, session_id))
        if not raw:
            return {"messages": [], "archived_rounds": []}
        return {
            "messages": json.loads(raw.get("messages", "[]")),
            "archived_rounds": json.loads(raw.get("archived_rounds", "[]")),
        }

    async def add_round(
        self, redis, user_id: str, session_id: str, messages: list[dict]
    ):
        """Add a new round of messages to the session, managing window."""
        key = self._key(user_id, session_id)
        session = await self.get_session(redis, user_id, session_id)

        now = datetime.now(timezone.utc).isoformat()
        window = self._settings.session_window_size

        # Append new messages
        session["messages"].extend(messages)

        # Archive oldest round if exceeds window
        while len(session["messages"]) > window * 2:
            # Pop oldest Q+A pair
            archived_pair = session["messages"].pop(0)  # user
            archived_pair2 = session["messages"].pop(0)  # assistant

            # Compute embedding for the archived query
            query_text = extract_text(archived_pair.get("content", ""))
            emb = []
            if query_text:
                try:
                    emb = await self._embedding.get_embedding(query_text)
                except Exception:
                    pass

            archive_entry = {
                "round_id": uuid.uuid4().hex[:8],
                "messages": [archived_pair, archived_pair2],
                "embedding": emb,
            }
            session["archived_rounds"].append(archive_entry)

        # Trim archived rounds to max
        max_archived = self._settings.archived_rounds_max
        if len(session["archived_rounds"]) > max_archived:
            session["archived_rounds"] = session["archived_rounds"][-max_archived:]

        # Save back to Redis
        await redis.hset(
            key,
            mapping={
                "messages": json.dumps(session["messages"], ensure_ascii=False),
                "archived_rounds": json.dumps(
                    session["archived_rounds"], ensure_ascii=False
                ),
                "updated_at": now,
            },
        )
        await redis.expire(key, self._settings.session_ttl_seconds)

    async def build_history(
        self, redis, user_id: str, session_id: str, query_embedding: list[float]
    ) -> dict:
        """Build history: long-term memories + filtered archived rounds + recent messages."""
        session = await self.get_session(redis, user_id, session_id)
        threshold = self._settings.relevance_threshold
        history_messages = []
        history_str_parts = []

        # Process archived rounds with relevance filter
        for entry in session["archived_rounds"]:
            emb = entry.get("embedding", [])
            if emb and query_embedding:
                sim = self._cosine_similarity(query_embedding, emb)
                if sim >= threshold:
                    # Full display
                    for msg in entry["messages"]:
                        history_messages.append(msg)
                        text = extract_text(msg.get("content", ""))
                        if text:
                            history_str_parts.append(f"{msg['role']}: {text}")
                else:
                    # Truncated: keep query, replace answer
                    q_msg = entry["messages"][0]
                    q_text = extract_text(q_msg.get("content", ""))
                    if len(q_text) > 200:
                        q_text = q_text[:200] + "..."
                    history_str_parts.append(f"{q_msg['role']}: {q_text}")
                    history_str_parts.append("assistant: [previous response omitted]")
                    history_messages.append(q_msg)
                    history_messages.append(
                        {"role": "assistant", "content": "[previous response omitted]"}
                    )
            else:
                # No embedding, keep query truncated
                q_msg = entry["messages"][0]
                q_text = extract_text(q_msg.get("content", ""))
                if len(q_text) > 200:
                    q_text = q_text[:200] + "..."
                history_str_parts.append(f"{q_msg['role']}: {q_text}")
                history_str_parts.append("assistant: [previous response omitted]")

        # Add recent messages
        for msg in session["messages"]:
            history_messages.append(msg)
            text = extract_text(msg.get("content", ""))
            if text:
                history_str_parts.append(f"{msg['role']}: {text}")

        return {
            "history_messages": history_messages,
            "history_str": "\n".join(history_str_parts),
        }

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
