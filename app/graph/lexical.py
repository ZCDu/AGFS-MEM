"""
Shared tokenizer/stopword primitives for lexical (non-LLM, non-embedding)
text matching. Originally private to app/verify/assessor.py's chat-retrieval
linking; extracted here so app/graph/search.py can reuse the exact same
tokenization for description-based entity search without duplicating it.
"""

from __future__ import annotations

import re

# Words carrying no topical information. Kept deliberately small: an
# aggressive stopword list starts deleting meaning ("no", "not", "never" are
# exactly the words that make a correction a correction).
STOPWORDS = frozenset("""
a an the and or but if then than so because as at by for from in into of on
to with without is are was were be been being am do does did doing have has
had having i you he she it we they me him her us them my your his its our
their this that these those there here what which who whom when where how
will would shall should can could may might must just very really quite too
also only even still yet about over under again more most some any each
""".split())

# Word regex: an alphanumeric run (with internal apostrophe/hyphen, covers
# contractions and hyphenated names), or a contiguous run of CJK ideographs
# (Han script has no whitespace between words, so a run is the raw unit --
# content_tokens() below is what turns a run into meaningful sub-tokens).
# Unicode codepoint escapes used deliberately (not literal multi-byte
# characters) so this file stays plain ASCII on disk -- app/graph is scanned
# elsewhere via a locale-default text read that chokes on literal multi-byte
# UTF-8 in this directory (see test_wiki_keys_all_go_through_one_helper in
# tests/test_wikis.py).
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\u2019\-]*|[\u3400-\u9fff]+")

_CJK_LO = 0x3400
_CJK_HI = 0x9fff


def _tokens(text: str) -> list[str]:
    return [m.group(0).lower() for m in _WORD.finditer(text)]


def _is_cjk(ch: str) -> bool:
    return _CJK_LO <= ord(ch) <= _CJK_HI


def content_tokens(text: str) -> set[str]:
    """Tokens worth comparing for topical overlap.

    Latin/digit runs: kept if not a stopword and longer than 2 characters --
    English words carry little meaning at 1-2 letters ("a", "an", "is").

    CJK runs: there is no whitespace between Chinese/Japanese/Korean words,
    so _WORD's regex can only find whole contiguous runs, not individual
    words within them ("no real segmenter" is a deliberate non-goal here --
    see PLAN.md's "never call an LLM just to check usefulness"). A run
    collapsed to single characters (the previous approach) is USELESS: every
    Han character is exactly one character, so a length-based filter tuned
    for English discards literally all of them, and content_tokens() on any
    Chinese text always returned an empty set -- Jaccard overlap against it
    was therefore always 0.0, silently. Instead, split each CJK run into
    overlapping 2-character bigrams (the standard cheap technique for CJK
    term matching without a real segmenter): "each branch" (4 chars) becomes
    {"each-br", "ranch-b", ...} in spirit -- concretely, three overlapping
    2-character windows -- so a shorter, differently-worded run sharing a
    real sub-word still produces set overlap instead of always comparing
    unequal whole strings. A lone single CJK character (already isolated by
    the regex, e.g. by punctuation on both sides) is kept as its own token
    rather than discarded, since single-character CJK terms are common.
    """
    out: set[str] = set()
    for t in _tokens(text):
        if not t:
            continue
        if _is_cjk(t[0]):
            if len(t) == 1:
                out.add(t)
            else:
                out.update(t[i:i + 2] for i in range(len(t) - 1))
        elif t not in STOPWORDS and len(t) > 2:
            out.add(t)
    return out


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
