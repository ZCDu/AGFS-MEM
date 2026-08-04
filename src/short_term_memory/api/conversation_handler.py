"""Agent-facing Redis short-term conversation boundary."""

from datetime import datetime

from short_term_memory.compression.policy import HeadroomPolicy
from short_term_memory.compression.scope import OptimizationScopeFactory
from short_term_memory.models import CompletionResult, PreparedTurn
from short_term_memory.ports import SessionCompressionQueue, TokenEstimator
from short_term_memory.storage.journal_store import JournalStore
from short_term_memory.storage.redis_session_context import RedisSessionContext


class ConversationHandler:
    """Prepare Agent context and persist completed turns without answering."""

    def __init__(
        self,
        *,
        session_context: RedisSessionContext,
        journal_store: JournalStore,
        headroom_policy: HeadroomPolicy,
        token_estimator: TokenEstimator,
        headroom_queue: SessionCompressionQueue,
        history_turns: int,
        optimization_scope_factory: OptimizationScopeFactory,
        headroom_proxy_url: str | None = None,
    ) -> None:
        if history_turns < 1:
            raise ValueError("history_turns must be positive")
        self.session_context = session_context
        self.journal_store = journal_store
        self.headroom_policy = headroom_policy
        self.token_estimator = token_estimator
        self.headroom_queue = headroom_queue
        self.history_turns = history_turns
        self.optimization_scope_factory = optimization_scope_factory
        self.headroom_proxy_url = headroom_proxy_url

    def prepare_turn(
        self,
        user_id: str,
        session_id: str,
        content: str,
        *,
        timestamp: datetime | None = None,
        session_seconds: int = 0,
    ) -> PreparedTurn:
        self.session_context.ensure_session_loaded(
            user_id, session_id, self.history_turns
        )
        prior_history = self.session_context.build_history(
            user_id, session_id, self.history_turns
        )
        user_message = {"role": "user", "content": content}
        self.session_context.append_message(user_id, session_id, user_message)
        self.journal_store.append_message(
            user_id,
            session_id,
            role="user",
            content=content,
            timestamp=timestamp,
        )
        scope = self.optimization_scope_factory.for_session(user_id, session_id)
        return PreparedTurn(
            user_id=user_id,
            session_id=session_id,
            history=(*prior_history, user_message),
            timestamp=timestamp,
            session_seconds=session_seconds,
            headroom_headers=scope.as_headroom_headers(),
            headroom_proxy_url=self.headroom_proxy_url,
        )

    def complete_turn(
        self,
        prepared: PreparedTurn,
        *,
        assistant_content: str,
    ) -> CompletionResult:
        assistant_message = {"role": "assistant", "content": assistant_content}
        self.session_context.append_message(
            prepared.user_id, prepared.session_id, assistant_message
        )
        self.journal_store.append_message(
            prepared.user_id,
            prepared.session_id,
            role="assistant",
            content=assistant_content,
            timestamp=prepared.timestamp,
        )

        snapshot = self.session_context.compression_snapshot(
            prepared.user_id, prepared.session_id
        )
        compression_input = snapshot.messages
        should_compress = self.headroom_policy.should_compress(
            estimated_tokens=self.token_estimator.estimate(compression_input),
            message_count=snapshot.processed_message_count,
            session_seconds=prepared.session_seconds,
        )
        if should_compress:
            self.headroom_queue.enqueue(
                prepared.user_id,
                prepared.session_id,
                compression_input,
                snapshot.processed_message_count,
                self.history_turns,
            )
        return CompletionResult(headroom_queued=should_compress)
