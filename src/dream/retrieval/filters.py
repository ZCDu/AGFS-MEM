"""Strict scope and artifact-type filtering for memory retrieval."""

from collections.abc import Iterable

from dream.retrieval.models import MemoryRecord, RetrievalQuery


class MemoryFilters:
    def apply(
        self,
        records: Iterable[MemoryRecord],
        query: RetrievalQuery,
    ) -> tuple[MemoryRecord, ...]:
        requested_kinds = set(query.kinds)
        return tuple(
            record
            for record in records
            if record.tenant_id == query.tenant_id
            and record.agent_id == query.agent_id
            and record.user_id in {None, query.user_id}
            and (not requested_kinds or record.kind in requested_kinds)
        )
