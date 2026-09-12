# Libra Audit Report

Three bugs found and fixed. No changes to `data/library.json`, `README.md`, `lib/models.py`, `lib/config.py`, or `lib/legacy.py`.

## Bugs Fixed

### 1. Search finds nothing (accent-insensitive titles)

- **File:** `lib/storage/repo.py`, function `find_by_title`, line 22
- **What was wrong:** The query was normalised (lowercased, accents stripped) but compared against the raw `b.title`. Titles with accents (e.g. "Café Linux") never matched a normalised search query (e.g. "cafe linux").
- **What I changed:** Applied `normalise()` to `b.title` before the `in` check.

### 2. Late fees come out as zero or negative

- **File:** `lib/utils/dates.py`, function `days_between`, line 23
- **What was wrong:** The subtraction was reversed: `parse(earlier) - parse(later)`. Since `later` is after `earlier`, this always produced a negative `timedelta`, so `late_days` was negative and `max(late_days, 0)` always returned 0. (It could also go negative if `max` wasn't there.)
- **What I changed:** Swapped the operands to `parse(later) - parse(earlier)`.

### 3. Due dates look off (one day early)

- **File:** `lib/utils/dates.py`, function `add_days`, line 18
- **What was wrong:** `timedelta(days=days - 1)` subtracted one day from the loan period. A 14-day loan starting on Jan 1 was due on Jan 14 instead of Jan 15. The comment "inclusive of the starting day" was misleading — the standard interpretation of a 14-day loan is that the due date is 14 calendar days after borrowing, not 13.
- **What I changed:** Removed the `- 1`, so `timedelta(days=days)` gives the correct due date.

## Things Looked At — No Bug Found

| File / Function | Why I checked | Verdict |
|---|---|---|
| `lib/services/search.py::search` | Combines title and author results, deduplicates by ISBN | Fine — works correctly with the fixed `find_by_title` |
| `lib/services/loans.py::borrow` | Creates the loan with `due_on=add_days(on, LOAN_DAYS)` | Fine — was using the buggy `add_days`, now correct |
| `lib/services/loans.py::late_fee` | Computes `days_between(loan.due_on, end)` and multiplies by rate | Fine — was using the buggy `days_between`, now correct |
| `lib/services/loans.py::give_back` | Marks the correct open loan as returned | Fine — iterates `open_loans` and matches by ISBN |
| `lib/storage/repo.py::available_copies` | Counts open (unreturned) loans for a book | Fine — correctly filters by `not l.returned_on` |
| `lib/storage/repo.py::open_loans` | Filters loans by member and unreturned status | Fine |
| `lib/storage/repo.py::load` / `save` | JSON round-trip for books, members, loans | Fine — uses `__dict__` which matches the dataclass fields |
| `lib/utils/text.py::normalise` | Strips accents, lowercases, collapses whitespace | Fine — standard NFKD decomposition |
| `lib/utils/text.py::slug` | Builds URL-safe slugs | Fine — not used by any reported path |
| `lib/config.py` | Default values for loan days, fee, max loans | Fine — defaults are reasonable |
| `lib/models.py` | Book, Member, Loan dataclasses | Fine — no logic here |
| `lib/legacy.py` | Legacy helpers | Not used by any active path (no callers found) |
| `lib/services/reports.py` | Report generation | Not implicated in the three reported issues |
| `lib/cli.py` | CLI entry point | Thin wrapper around services; no logic bugs |
