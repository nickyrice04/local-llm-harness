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
    # sort by (-count, word) to break ties alphabetically
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return ranked[:n]


def bigrams(text, n=10):
    """The n most frequent adjacent word pairs."""
    ws = words(text)
    pairs = [(ws[i], ws[i + 1]) for i in range(len(ws) - 1)]
    counts = Counter(pairs)
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return ranked[:n]
