# Audit Report

Three real bugs were found and fixed. Two were in `lib/utils/dates.py`, one in
`lib/storage/repo.py`.

---

## Bug 1 — Search finds nothing for visible books

**File:** `lib/storage/repo.py`
**Function:** `find_by_title`
**What was wrong:** The query string was normalised (lowercased, accents stripped)
but the book title was not.  The `in` check compared a normalised query against
a raw title, so a search for `"le petit prince"` would never match a book whose
title field still contained `"Le Petit Prince"` (with capital letters and
accents).
**Fix:** Changed the comparison from

```python
if wanted in b.title
```

to

```python
if wanted in normalise(b.title)
```

---

## Bug 2 — Late fees come out as zero or negative

**File:** `lib/utils/dates.py`
**Function:** `days_between`
**What was wrong:** The subtraction was in the wrong order:

```python
(parse(earlier) - parse(later)).days
```

This produces a **negative** number when `later` is after `earlier`.  The docstring
says it should be "positive when later is after earlier".  Because `late_fee`
uses `max(late_days, 0)`, every overdue book returned a fee of zero.
**Fix:** Swapped the operands:

```python
(parse(later) - parse(earlier)).days
```

---

## Bug 3 — Due dates look off

**File:** `lib/utils/dates.py`
**Function:** `add_days`
**What was wrong:** The function subtracted 1 from the day count:

```python
timedelta(days=days - 1)
```

The comment "Inclusive of the starting day" does not justify this — a 14-day
loan from August 1 should land on August 15 (14 calendar days later), not
August 14.
**Fix:** Removed the `- 1`:

```python
timedelta(days=days)
```

---

## Items examined but judged fine (red herrings)

| File | Function / Code | Reason it looks suspicious | Verdict |
|------|----------------|--------------------------|---------|
| `lib/legacy.py` | `days_lue` returns `None` | `bug_fix = None` looks like a stub | Dead code — nothing imports or calls it |
| `lib/config.py:8` | `# TODO: move to a config file` | TODO implies incomplete | Just a wish-list comment; the config works as-is |
| `lib/config.py:1` | `DATE_FORMAT = "%Y-%m-%d"` | No environment override mechanism | Used correctly everywhere via `lib.utils.dates.parse` / `fmt` |
| `lib/storage/repo.py:42` | `indent=1` in `json.dump` | Unusual indentation width | Cosmetic only; not a correctness issue |
| `lib/utils/__init__.py` | Empty | Missing exports? | Modules are imported directly by path; no `__all__` needed |
| `lib/storage/__init__.py` | Empty | Same as above | Same reasoning |
| `lib/services/__init__.py` | Empty | Same as above | Same reasoning |
| `lib/models.py` | Plain dataclasses | No validation logic | Dataclasses are sufficient; validation is in the service layer |
| `lib/services/search.py` | `slug` unused | Function defined but never called | Harmless dead code |
| `data/library.json` | Existing loan has `due_on: "2026-08-15"` for 14-day loan from Aug 1 | Correct data | Confirms `add_days` should produce Aug 15 (bug 3 confirmed by mismatch) |
| `lib/services/reports.py` | `popularity` counts returned loans too | Counts historical data | Not one of the three reported symptoms; acceptable behavior |
| `lib/legacy.py` | `days_late` superseded by `late_fee` | Old function name shadows new one | `days_late` is dead code; the new function `late_fee` is the correct path |
