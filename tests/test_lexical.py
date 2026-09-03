"""
Tests for app/graph/lexical.py's shared tokenizer.

Regression coverage for a real bug: content_tokens() used to tokenize CJK
text one character at a time, then filtered out anything not longer than 2
characters -- which discarded EVERY CJK token, always, since a Han character
run was never split into anything longer than 1 character. content_tokens()
on any Chinese text returned an empty set, so Jaccard overlap against it was
always 0.0 -- the "fuzzy/topical" match tier was silently dead for Chinese,
even though the exact-match tiers above it worked fine (which is why typing
the full literal phrase worked but a shorter, related phrase did not).
"""

from __future__ import annotations

from app.graph.lexical import content_tokens, jaccard

# u5404 u5206 u652f u884c = "each branch" (4 Han characters, one run)
EACH_BRANCH = "各分支行"
# u5206 u652f u884c = "branch" (3 Han characters, a substring concept of the
# above but NOT a literal substring match target here -- it shares no fixed
# starting offset with EACH_BRANCH, which is exactly the case a naive
# substring/whole-token check misses and bigram overlap catches).
BRANCH = "分支行"


def test_content_tokens_is_not_empty_for_chinese_text():
    assert content_tokens(EACH_BRANCH) != set()
    assert content_tokens(BRANCH) != set()


def test_content_tokens_splits_a_cjk_run_into_bigrams():
    # "each branch" (4 chars) -> 3 overlapping 2-character windows.
    assert content_tokens(EACH_BRANCH) == {
        "各分", "分支", "支行",
    }


def test_content_tokens_keeps_a_lone_single_cjk_character():
    assert content_tokens("行") == {"行"}


def test_related_but_differently_worded_chinese_phrases_overlap():
    """The actual regression: a shorter, related phrase must produce a
    non-zero Jaccard overlap against a longer phrase that contains the same
    sub-word, even though neither is a literal substring/prefix of the
    other's raw run."""
    a = content_tokens(EACH_BRANCH)
    b = content_tokens(BRANCH)
    overlap = jaccard(a, b)
    assert overlap > 0.5, overlap


def test_unrelated_chinese_text_has_low_or_no_overlap():
    a = content_tokens(EACH_BRANCH)
    # "quarterly budget review" -- shares no characters with EACH_BRANCH.
    b = content_tokens("季度预算审核")
    assert jaccard(a, b) == 0.0


def test_english_tokenization_is_unaffected():
    tokens = content_tokens("Quarterly launch timeline for the propulsion team.")
    assert tokens == {"quarterly", "launch", "timeline", "propulsion", "team"}
    # Stopwords and 1-2 letter words are still dropped for Latin text.
    assert "the" not in tokens and "for" not in tokens


def test_mixed_cjk_and_latin_text_tokenizes_both_scripts():
    tokens = content_tokens("Alice 需要 launch 时间表")
    assert "alice" in tokens and "launch" in tokens
    assert any(len(t) == 2 for t in tokens if ord(t[0]) >= 0x3400)
