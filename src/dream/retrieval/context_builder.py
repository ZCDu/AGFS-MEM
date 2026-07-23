"""Build a bounded Markdown context from ranked memory records."""

import re

from dream.retrieval.models import RetrievedContext, RetrievalResult


_TOKEN = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]")


def estimated_tokens(text: str) -> int:
    return len(tuple(_TOKEN.finditer(text)))


class ContextBuilder:
    def __init__(self, token_budget: int = 4_000) -> None:
        if token_budget < 1:
            raise ValueError("context token budget must be positive")
        self.token_budget = token_budget

    def build(self, result: RetrievalResult) -> RetrievedContext:
        chunks: list[str] = []
        memory_ids: list[str] = []
        remaining = self.token_budget
        for match in result.matches:
            record = match.record
            chunk = f"[{record.kind.value}:{record.memory_id}]\n{record.content.strip()}\n"
            token_count = estimated_tokens(chunk)
            if token_count <= remaining:
                chunks.append(chunk)
                memory_ids.append(record.memory_id)
                remaining -= token_count
                continue
            if chunks:
                continue
            excerpt = self._truncate(chunk, remaining)
            if excerpt:
                chunks.append(excerpt)
                memory_ids.append(record.memory_id)
                remaining = 0
            break
        markdown = "\n".join(chunks)
        return RetrievedContext(
            markdown=markdown,
            included_memory_ids=tuple(memory_ids),
            estimated_tokens=estimated_tokens(markdown),
        )

    @staticmethod
    def _truncate(text: str, budget: int) -> str:
        matches = tuple(_TOKEN.finditer(text))
        if not matches or budget < 1:
            return ""
        if len(matches) <= budget:
            return text
        return text[: matches[budget - 1].end()].rstrip() + "\n"
