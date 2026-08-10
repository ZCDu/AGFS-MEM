"""Tests for the recall policy module (moved from chat_loop into SDK source)."""

from __future__ import annotations

from short_term_memory.compression.recall_policy import (
    needs_semantic_recall,
    wants_historical_detail,
)


def test_wants_historical_detail_detects_explicit_phrases() -> None:
    phrases = (
        "你还记得我们之前聊的方案吗",
        "你之前详细讲过这个",
        "你当时怎么说的",
        "之前那段代码怎么写的",
        "那个函数怎么写的来着",
        "刚才那个文件内容是什么",
        "把完整代码复述出来",
        "上次说的那个类",
        "之前给我发的那个文档",
    )
    for phrase in phrases:
        assert wants_historical_detail(phrase), f"应识别历史细节提问: {phrase}"


def test_wants_historical_detail_ignores_normal_questions() -> None:
    phrases = (
        "你好",
        "今天天气怎么样",
        "请帮我写一个排序函数",  # 新需求，不是问历史
        "怎么学习 Python",
        "推荐几本书",
    )
    for phrase in phrases:
        assert not wants_historical_detail(phrase), f"不应误判为历史提问: {phrase}"


def test_semantic_similarity_detects_related_question() -> None:
    # 压缩摘要里讨论的是 Redis 记忆存储
    summary = "我们用 Redis 存储短期记忆，包含消息列表、时间索引、关键词索引。压缩后需要召回原文。"
    # 语义相关的问题（用了相近词）应触发
    related = "Redis 里存的消息和索引结构是怎样的"
    assert needs_semantic_recall(related, summary), "语义相关问题应触发召回"
    # 无关的问题不应触发
    unrelated = "今天晚饭吃什么"
    assert not needs_semantic_recall(unrelated, summary), "无关问题不应触发召回"


def test_semantic_similarity_empty_summary_returns_false() -> None:
    assert not needs_semantic_recall("你好", ""), "空摘要不应触发"
    assert not needs_semantic_recall("你好", "短"), "过短摘要不应触发"
