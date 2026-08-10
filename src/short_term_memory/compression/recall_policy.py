"""Recall decision policy: when should the agent pull compressed originals back?

This module centralizes the "when to recall" logic so that Agent integrators get
it without copying chat_loop. It provides two signals used before/around the
model call, plus a system prompt that lets the model itself decide.

Design notes:
- 方案1: keyword triggers — the user explicitly asks about earlier discussion.
- 方案2: lightweight semantic similarity (character n-gram) — the question is
  topically related to the compressed history even without explicit keywords.
- 方案3: model self-judgement — a system prompt tells the model to call
  ``headroom_retrieve`` when the compressed context is insufficient.
"""

from __future__ import annotations

# 方案1: explicit phrases that signal "ask about earlier content".
HISTORICAL_MARKERS: tuple[str, ...] = (
    "你之前",
    "你还记得",
    "你当时",
    "你详细讲过",
    "你详细介绍了",
    "你刚刚",
    "你刚才",
    "你上次",
    "之前说的",
    "之前提到",
    "之前讲过",
    "之前那个",
    "之前那段",
    "之前写",
    "之前给",
    "之前发",
    "上次说的",
    "上次提到",
    "上次那个",
    "刚才说的",
    "刚才讲的",
    "刚才那个",
    "刚才写",
    "当时说",
    "当时讲的",
    "当时介绍",
    "当时那个",
    "那时候",
    "那天",
    "完整内容",
    "完整复述",
    "完整代码",
    "原样",
    "怎么写的",
    "怎么写的来着",
    "那个函数",
    "那个类",
    "那段代码",
    "那段内容",
    "那个文件",
    "那份文档",
    "那个方法",
    "详细讲一下",
    "展开讲讲",
    "具体是什么",
)

# 方案2: default n-gram size and similarity threshold.
DEFAULT_NGRAM_SIZE = 2
DEFAULT_SIMILARITY_THRESHOLD = 0.18


def _ngrams(text: str, n: int = DEFAULT_NGRAM_SIZE) -> set[str]:
    normalized = "".join(ch for ch in text.lower() if ch.isalnum() or ch.isspace())
    if len(normalized) < n:
        return set()
    return {normalized[i : i + n] for i in range(len(normalized) - n + 1)}


def text_similarity(a: str, b: str, n: int = DEFAULT_NGRAM_SIZE) -> float:
    """Cosine-like overlap between character n-gram sets (0.0 .. 1.0)."""
    ga = _ngrams(a, n)
    gb = _ngrams(b, n)
    if not ga or not gb:
        return 0.0
    overlap = len(ga & gb)
    return overlap / (len(ga) ** 0.5 * len(gb) ** 0.5)


def wants_historical_detail(query: str) -> bool:
    """方案1: explicit keyword match that the user asks about earlier content."""
    return any(marker in query for marker in HISTORICAL_MARKERS)


def needs_semantic_recall(
    query: str,
    compressed_summary: str,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> bool:
    """方案2: topical overlap between the query and the compressed history."""
    if not compressed_summary or len(compressed_summary.strip()) < 20:
        return False
    return text_similarity(query, compressed_summary) >= threshold


def should_recall(query: str, compressed_summary: str) -> bool:
    """Combined 方案1+方案2 decision (before the model call)."""
    return wants_historical_detail(query) or needs_semantic_recall(query, compressed_summary)


# 方案3: system prompt that lets the model decide to call headroom_retrieve.
RETRIEVE_GUIDANCE = (
    "当前对话包含压缩后的历史记忆，其中可能带有 `Retrieve more: hash=...` 标记。"
    "当你需要回答用户问题，但现有上下文中的信息不足以给出准确回答时，"
    "请主动调用 `headroom_retrieve` 工具（参数 hash 从压缩标记中提取），"
    "取回原始内容后再回答。不要猜测或编造历史细节。"
)


def retrieve_guidance() -> str:
    """Return the system prompt used to enable model self-judgement (方案3)."""
    return RETRIEVE_GUIDANCE
