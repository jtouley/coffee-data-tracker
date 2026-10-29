RUN_BY_COMMAND: pr-review-local
Last reviewed commit: 37fd59bdf7ec42ce3c80536575a69334983302a6

**Tracker:** github-issues
**Ticket:** #55
**Verdict:** REQUEST CHANGES

The reviewer used a detached worktree at that commit. The user tree stayed on its own branch.

This PR adds catalog search on Edit bag and an explicit `coffee_id` when the person picks a result. Shots stay on the bag. That path works. The search endpoint writes a 503 for a dead catalog, but `fuzzy_match_coffee` never raises, so a failed catalog read returns an empty hit list. The screen then says no coffees were found. That is a must-fix. [V]

## Reproductions

Worktree: `/Users/jasontouleyrou/.cursor/worktrees/pr55-review-cd172c3c/coffee-data-tracker-6cabaf14fc83` at `37fd59b`.

1. Happy path (passes):

```
uv run pytest -q tests/test_bags.py::test_search_and_explicitly_relink_a_bag_keeps_its_shots \
  tests/test_bags.py::test_editing_a_bag_to_the_right_lot_matches_it_again -xvs
```

```
2 passed in 2.16s
```

2. Catalog down (must-fix):

```
uv run python - <<'PY'
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.main import create_app
from tests.test_app import HeaderAuth
from coffeedata.shots import _Catalog
from coffeedata import shots
shots._catalog_cache.clear()
c = TestClient(create_app(auth=HeaderAuth()))
with patch("coffeedata.shots._load", return_value=_Catalog(ok=False)):
    r = c.get("/api/coffees/search", params={"q": "Counter Culture"})
print(r.status_code, r.json())
PY
```

```
200 {'coffees': []}
```

`shots.mart_columns()` raises `RuntimeError` on the same fake catalog. The search handler catches `RuntimeError` around `fuzzy_match_coffee`, which returns `[]` when the index is empty and never raises. The 503 branch is dead. The UI maps empty results to "No coffees found. Try another search."

## End-to-end walk

PR path: wrong photo match → Edit bag → Search catalog → pick → Save → shots still on the bag.

1. `GET /bag/{id}/edit` renders the Search catalog control and the current link line. Pass (asserted in the new test).
2. `GET /api/coffees/search?q=…` needs a signed-in `Me`. Short queries (`len < 2` after strip) return `{"coffees": []}`. Pass.
3. Hits go through `describe`, so past lots show `past: true`. Pass (asserted).
4. Save with an explicit `coffee_id` keeps shots on `bag_id`. Pass (asserted: detail still has `/shot/s1`).
5. Browser wiring in `bagEdit`: select sets `selected`, puts the title into `coffee_freetext`, and on submit sends `coffee_id: selected.coffee_id` with `match_score: null`. Typing in the name field clears `selected`. Enter in the search box searches and does not submit the form. Read in source; not clicked in a browser.
6. The PR body said the new test could not run. It runs clean here after `uv sync --extra app`.

Unchecked from the PR: live browser click of Search → pick → Save. Server path for that body is covered.

## Adversarial inputs

| Input | Result |
| --- | --- |
| `q` length 0 or 1 after strip | `{"coffees": []}`. Pass. |
| `q` over 120 chars | FastAPI 422. Pass. |
| Catalog read failed (`_Catalog(ok=False)`) | 200 empty list. Fail. Must-fix. |
| Catalog ok but no rows | Empty list. Acceptable. |
| Stale in-flight search after a new keystroke | `request` counter drops the late reply. Pass (read). |
| Select, then edit the name | `selected = null`; save rematches. Pass (read). |
| Select, then save | Explicit `coffee_id`; `save_bag` does not rematch. Pass. |
| CSRF on search | GET with `Me` cookie; no `Writer` header. Pass. |
| Cross-user bag edit | Unchanged; edit still loads by owner. Pass. |

No new CLI or installer.

## Contracts

The handler docstring says it offers candidates and never silently changes a bag. Explicit pick matches that. The 503 message in the same handler says a dead catalog must surface as unavailable. Shipping empty hits against that message is a must-fix. It does not need a new design choice about silence; the code already chose 503 and missed the check.

The PR "Known limitation" names this gap. Naming it does not clear the dead branch.

Nit. Search does one `store.coffee` lake read per hit (up to 12). The photo match path already does the same.

Nit. There is no control to hide the search panel without a pick. Save still works.

Should-fix. No browser test covers `selected` → POST body. A server test posts `coffee_id` directly. A regression in the JS assign would ship green.

## Design choices

| Case | Expected |
| --- | --- |
| S1 Catalog read failed | 503; UI says the catalog is unavailable (existing `!response.ok` path). |
| S2 Catalog ok, no lexical hits | 200 `{"coffees": []}`; "No coffees found…". |
| S3 Catalog ok, hits | 200 with described rows. |
| S4 Query under 2 chars | 200 empty; "Type at least two characters." |

| Option | Rule | Matrix | Size |
| --- | --- | --- | --- |
| Simple | If not `shots._load(lake).ok`, raise 503 before fuzzy match. | S1–S4 pass. Uses a private helper. | ~3 lines in `search_coffees`. |
| Medium | Call `shots.mart_columns(lake)` and keep the existing `except RuntimeError → 503`. | S1–S4 pass. Uses the public "read failed" API. | Replace the try body; same size. |
| Over-built | Structured `detail` with `code`, `retry_after_s`, and `meta` on 200. | S1–S4 pass; JS must learn the new shape for a better message. | New contract for little gain; UI already handles non-OK. |

Medium wins. The RuntimeError catch is already written for `mart_columns`. Simple is fine if the team accepts `_load`. Over-built does not help the phone.

Lab spikes (uncommitted) in the worktree scored Medium as the ship pick. See §11.

## Scope

Title: search the catalog to correct a bag’s coffee. 4 files, +113 / −1. One feature. No split.

## Lookback

Failure class: error handling wrapped around a helper that swallows the failure (`fuzzy_match_coffee` → empty index → `[]`), while a sibling API (`mart_columns`) fails closed. After the fix, re-run the catalog-down script and assert 503; keep the empty-ok catalog case at 200. Re-read the JS status string so a 503 still hits the connection message, or teach it the 503 body text.

## 11. Spec-driven proof (worktree, not committed)

RUN_BY_COMMAND: spec-driven
Worktree: `/Users/jasontouleyrou/.cursor/worktrees/pr55-review-cd172c3c/coffee-data-tracker-6cabaf14fc83`
No commit. Spikes are local only.

### Proof the review is true

| Claim | Test | Result |
| --- | --- | --- |
| Failed catalog read returns 200 `[]`, not 503 | `test_witness_shipped_search_hides_catalog_failure_as_empty_results` | pass as bug record: shipped returns 200 `[]` |
| Simple fix raises 503 | `test_spike_simplest_raises_503_when_catalog_failed` | pass |
| Medium fix raises 503 and still returns hits | `test_spike_middle_raises_503_and_returns_hits_when_ok` | pass |
| Over-built returns structured detail | `test_spike_overengineered_raises_503_with_reason_code` | pass |

```
uv run pytest -q tests/test_pr55_review_spikes.py -xvs
5 passed
```

### Issue 1 (catalog-down search) -- three fixes

| Variant | Function | What it does | Complexity | vs Stage 6.6 | Note |
| --- | --- | --- | --- | --- | --- |
| Simplest | `search_simplest` | `_load(...).ok` gate | tiny | n/a (Python) | touches private `_load` |
| Middle | `search_middle` | `mart_columns` then fuzzy match | tiny | n/a | matches existing except |
| Overengineer | `search_overengineered` | structured detail + meta | small | n/a | new client contract |

**Pick if shipping:** Middle, because the handler already catches `RuntimeError` and `mart_columns` is the public fail-closed read.

### Files in the worktree (do not merge)

- `tests/pr55_search_spikes.py`
- `tests/test_pr55_review_spikes.py`

## PT schema gate

`pt_schema_gate.py --mode code --base origin/main --head HEAD` → `triggered: false` (`no_pt_schema_scope_in_diff`).

## Update 2026-10-29

RUN_BY_COMMAND: pr-review-update

Commit under review: `b055a467dbce294b432de0e3060928dae8c2ab59` (`Report unavailable catalog during bag search`).

Worktree: `/Users/jasontouleyrou/.cursor/worktrees/pr55-update-1793317864` at `b055a46`. The user tree stayed on `main`.

### Finding status

| Finding | Status |
| --- | --- |
| Must-fix: catalog-down search returns 200 empty hits | fixed |
| Nit: one `store.coffee` lake read per hit | open |
| Nit: no control to hide the search panel | open |
| Should-fix: no browser test for `selected` → POST | open |

### Fix check (must-fix)

`search_coffees` now calls `shots.mart_columns(lake)` before `fuzzy_match_coffee`. A failed catalog read raises `RuntimeError`. The handler maps that to HTTP 503.

Reproduction (same script as the first review):

```
503 {'detail': 'The catalog is unavailable. Try again when you’re online.'}
```

```
uv run pytest -q \
  tests/test_bags.py::test_catalog_search_reports_unavailable \
  tests/test_bags.py::test_search_and_explicitly_relink_a_bag_keeps_its_shots \
  tests/test_bags.py::test_editing_a_bag_to_the_right_lot_matches_it_again
```

```
3 passed in 3.11s
```

`test_catalog_search_reports_unavailable` asserts 200 empty for `q` shorter than 2 chars, and 503 for a real search when `_load` returns `_Catalog(ok=False)`.

The bag-edit JS still throws on `!response.ok`. The catch path shows the connection message. A 503 no longer looks like "No coffees found."

This matches the Medium option from the first review.

### New findings

None. No new must-fix. No new nit from the fix commit.

### Verdict

**APPROVE WITH NITS**

The fix closes the catalog-down must-fix. The reviewer reproduced the 503. Open items are the earlier nits and the should-fix browser gap. They do not block merge.

Last reviewed commit: b055a467dbce294b432de0e3060928dae8c2ab59
