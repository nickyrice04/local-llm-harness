"""Word statistics over a corpus. Correct, and slow on purpose."""

import re
from collections import Counter

_WORD = re.compile(r"[a-z']+")


def words(text):
    return _WORD.findall(text.lower())


def top_words(text, n=10):
    """The n most frequent words (ties broken alphabetically), with counts."""
    ws = words(text)
    counts = Counter(ws)
    return sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:n]


def bigrams(text, n=10):
    """The n most frequent adjacent word pairs."""
    ws = words(text)
    pairs = list(zip(ws, ws[1:]))
    counts = Counter(pairs)
    return sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:n]
