"""Word statistics over a corpus. Correct, and slow on purpose."""

import re

_WORD = re.compile(r"[a-z']+")


def words(text):
    return _WORD.findall(text.lower())


def top_words(text, n=10):
    """The n most frequent words (ties broken alphabetically), with counts."""
    ws = words(text)
    unique = []
    for w in ws:
        if w not in unique:                 # O(n) membership on a list, per word
            unique.append(w)
    counts = []
    for w in unique:
        counts.append((w, ws.count(w)))    # O(n) count, per unique word
    counts.sort(key=lambda pair: (-pair[1], pair[0]))
    return counts[:n]


def bigrams(text, n=10):
    """The n most frequent adjacent word pairs."""
    ws = words(text)
    pairs = [(ws[i], ws[i + 1]) for i in range(len(ws) - 1)]
    unique = []
    for p in pairs:
        if p not in unique:
            unique.append(p)
    counts = [(p, pairs.count(p)) for p in unique]
    counts.sort(key=lambda pair: (-pair[1], pair[0]))
    return counts[:n]
