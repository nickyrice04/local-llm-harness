"""Word statistics over a corpus."""

import re
from collections import Counter

_WORD = re.compile(r"[a-z']+")


def words(text):
    return _WORD.findall(text.lower())


def top_words(text, n=10):
    """The n most frequent words (ties broken alphabetically), with counts."""
    counts = Counter(words(text))
    sorted_counts = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return sorted_counts[:n]


def bigrams(text, n=10):
    """The n most frequent adjacent word pairs."""
    ws = words(text)
    pairs = [(ws[i], ws[i + 1]) for i in range(len(ws) - 1)]
    counts = Counter(pairs)
    sorted_counts = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return sorted_counts[:n]
