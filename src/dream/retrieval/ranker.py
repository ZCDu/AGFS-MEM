"""Deterministic local relevance ranking without model dependencies."""

from collections.abc import Iterable
import re

from dream.retrieval.models import MemoryRecord, RankedMemory


_TOKEN = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]")


def relevance_tokens(text: str) -> frozenset[str]:
    return frozenset(match.group(0).casefold() for match in _TOKEN.finditer(text))


class LexicalRanker:
    def rank(
        self,
        records: Iterable[MemoryRecord],
        query_text: str,
    ) -> tuple[RankedMemory, ...]:
        query_tokens = relevance_tokens(query_text)
        ranked = tuple(
            RankedMemory(
                record=record,
                score=self._score(record, query_tokens),
            )
            for record in records
        )
        return tuple(
            sorted(
                ranked,
                key=lambda item: (
                    -item.score,
                    item.record.memory_id,
                ),
            )
        )

    @staticmethod
    def _score(record: MemoryRecord, query_tokens: frozenset[str]) -> float:
        if not query_tokens:
            relevance = 0.0
        else:
            relevance = len(relevance_tokens(record.content) & query_tokens) / len(
                query_tokens
            )
        return relevance + (record.confidence * 0.001)
