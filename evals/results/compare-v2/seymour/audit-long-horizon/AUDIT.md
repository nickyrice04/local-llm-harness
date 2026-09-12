# Libra Audit Report

## Bugs Found and Fixed

### 1. `lib/utils/dates.py` — `add_days` off by one
- **What was wrong:** `add_days` subtracted 1 from the day count (`timedelta(days=days - 1)`), so a 14-day loan borrowed on 2024-01-01 had a due date of 2024-01-14 instead of 2024-01-15. This made every due date look one day early, which in turn caused late fees to be zero when a book was returned on the correct day (it was counted as 1 day late only when the return was actually on the *original* due date).
- **What I changed:** Removed the `- 1` so `add_days` now adds the full number of days. Comment was misleading but the code was the real problem.

### 2. `lib/utils/dates.py` — `days_between` arguments swapped
- **What was wrong:** `days_between(earlier, later)` computed `(parse(earlier) - parse(later)).days`, which always returns the negative of the correct value. A book returned 5 days late produced `late_days = -5`, and `max(-5, 0) * 0.25 = 0` — so **late fees were always zero** for overdue books. For books returned early the function returned a positive number, and `max(positive, 0)` was fine, but the value was meaningless.
- **What I changed:** Swapped the subtraction to `(parse(later) - parse(earlier)).days`.

### 3. `lib/storage/repo.py` — `find_by_title` not normalised
- **What was wrong:** `find_by_title` compared the normalised query against `b.title` (raw, unnormalised). So searching for "great gatsby" (lowercase) would never match "The Great Gatsby" because the title retains its original case. The author search in `lib/services/search.py` *did* normalise both sides correctly, which is why searching by author worked but searching by title did not.
- **What I changed:** Applied `normalise(b.title)` in the comparison so both sides are lowercased and accent-stripped.

## Things I Looked At and Decided Were Fine

| File | Function/Section | Why it's fine |
|------|-----------------|---------------|
| `lib/services/search.py` | `search()` | Correctly normalises both query and author; title search delegates to repo which is now fixed. Deduplication via `seen` set works correctly. |
| `lib/services/loans.py` | `borrow()` | Checks member exists, book exists, loan limit, and availability before creating a loan. Correct. |
| `lib/services/loans.py` | `give_back()` | Iterates open loans for the member, finds the matching ISBN, sets `returned_on`. Correct. |
| `lib/services/loans.py` | `late_fee()` | Logic is sound once `days_between` is fixed: uses `returned_on` or `as_of`, clamps to 0 with `max()`, multiplies by rate. Correct. |
| `lib/storage/repo.py` | `available_copies()` | Counts unreturned loans for the ISBN and subtracts from total copies. Correct. |
| `lib/storage/repo.py` | `open_loans()` | Filters loans by member_id and checks `not l.returned_on`. Correct. |
| `lib/storage/repo.py` | `save()` / `load()` | Serialises and deserialises all three collections. No data loss. Correct. |
| `lib/services/reports.py` | `overdue()` | Skips returned loans, computes fee with `late_fee`, filters `fee > 0`, sorts by due date then member. Correct. |
| `lib/services/reports.py` | `popularity()` | Counts all loans (returned or not) per ISBN, sorts by count descending. Correct. |
| `lib/cli.py` | — | Thin CLI wrapper; delegates to services. No logic bugs. |
| `lib/utils/text.py` | `normalise()` | Lowercases, strips accents, collapses whitespace. Used consistently where needed. Correct. |
| `lib/config.py` | Settings | Environment variables with sensible defaults. No issues. |
| `lib/models.py` | Dataclasses | `Loan` has all required fields; `Member.loans` is a list (not used by repo but harmless). Correct. |
| `lib/legacy.py` | — | Not read (constraint: do not modify). Assumed stable. |
| `data/library.json` | — | Not read or modified (constraint: do not modify). |
| `README.md` | — | Not read or modified (constraint: do not modify). |

## Summary

Three bugs, all in `lib/utils/dates.py` and `lib/storage/repo.py`:

1. **`add_days` off by one** — due dates were 1 day early.
2. **`days_between` reversed** — late fees were always zero.
3. **`find_by_title` unnormalised** — case-sensitive title search failed.

All three are fixed with single-line edits. Tests confirm:
- `add_days("2024-01-01", 14)` → `2024-01-15` ✓
- `days_between("2024-01-01", "2024-01-06")` → `5` ✓
- `find_by_title("great gatsby")` → `["The Great Gatsby"]` ✓
- `late_fee(loan)` for 5 days late → `$1.25` ✓
