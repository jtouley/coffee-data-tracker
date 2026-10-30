# PR Review #54 — Display shots outside the traditional espresso window

```
RUN_BY_COMMAND: pr-review-local
RUN_BY_SKILL: pr-review
Last reviewed commit: b52e09931bf4de0268545979e461a2b83cca7d8b
```

**PR:** https://github.com/jtouley/coffee-data-tracker/pull/54  
**Branch:** `soup-profile` → `main` (draft)  
**Review checkout:** `/Users/jasontouleyrou/.cursor/worktrees/pr54-review-d25404c6/coffee-data-tracker-6cabaf14fc83`  
**Tracker:** TICKET_SKIPPED (reason: no cadence adapter ticket on this PR)  
**Lenses:** none (no Go / scalar-lib footprint)  
**PT schema gate:** skipped (gate script not present in this environment)

## Verdict

**Approve with required fixes** — product intent is sound and the new integration test passes. One display regression on the default (most-used) chart must land before merge; docs that still prescribe the old verdict copy should be updated in the same change or a linked follow-up.

---

## 1. Summary

The PR stops clamping shot marks to the 18–38 s × 1:1.6–2.5 plot box, expands axes when shots fall outside, relabels the shaded band as a traditional espresso *reference*, and replaces judgmental saved-screen copy (`Inside the window` / `Ran fast… outside the window`) with a neutral ratio-and-time line.

Diff: 3 files, +48 / −37. One commit.

## 2. Findings

### Required

1. **[V] Default ratio tick `1:2.0` becomes `1:2`** — `app/views.py` (`target_svg` tick loop)  
   - **What:** Tick labels switched from `1:{r:.1f}` to `1:{g(r)}`. `g` uses `:g`, so `2.0` renders as `2`. On the default axes (every in-window bag), labels are `1:1.8`, `1:2`, `1:2.2`, `1:2.4`.  
   - **Evidence:** Verified in REVIEW_CHECKOUT with `target_svg([in-window shot])`; main used `1:2.0`.  
   - **Recommendation:** Keep one decimal for ratio ticks (e.g. `f"1:{r:.1f}"` or `g(r, 1)`). Do not reuse bare `g()` for axis labels that must stay aligned.

### Should-fix

2. **[V] Product docs still prescribe the old verdict copy** — `docs/app/PLAN.md` §2 step 5; mockups under `docs/app/mockups/{saved,today}.html`  
   - **What:** PLAN still requires “Inside the window…” / “Ran fast… outside the window” and “time against the 25 to 32 s window”. Live templates now emit neutral copy. DESIGN/PLAN remain the written product contract for the app.  
   - **Evidence:** Read PLAN.md lines 84–87 on the PR head; `summary()` no longer emits those strings; mockup HTML still shows the old sentence.  
   - **Recommendation:** Update PLAN (and mockups if they are still the design source of truth) in this PR, or open a follow-up and link it from the PR body. Treat as a deliberate product decision, not an accidental drift.

3. **[V] New coverage only hits one expansion corner** — `tests/test_app.py::test_long_ratio_shots_are_shown_without_espresso_time_judgment`  
   - **What:** Test covers fast times + high ratios and “no judgment words”. It does not lock tmax-only, rmin-only, or the default-axis label format (finding 1).  
   - **Evidence:** Read the test; adversarial probes separately confirmed tmax-only and rmin-only paths.  
   - **Recommendation:** Add cases for time > 38 s only, ratio < 1.6 only, and assert default ratio ticks still show `1:2.0` (or whatever format you choose after fixing finding 1).

### Nit / Optional

4. **[V] Axis pad can push `tmin` below 0 s** — `target_svg`  
   - When `time_s == 1` (legal minimum), `tmin = min(times) - 2` → `-1`; `_ticks` can emit `0 s`. Harmless mathematically; odd for a time axis. Cap with `max(0.0, …)` if you care.

5. **[L] Expanded time axes get sparse ticks** — `_ticks(13, 38) → [20, 30]`  
   - Loses the 25 s grid that sits on the espresso band edge. Acceptable; improve later if the chart feels empty outside the default range.

6. **[I] Aria text hardcodes `25 to 32` / `1:1.8 to 1:2.3`** while geometry uses `TARGET_TIME` / `TARGET_RATIO`  
   - Values match today. Prefer formatting from the constants if either can change.

### FYI (not blocking)

- `coffeedata.shots.suggest_next` still talks about “the window”. That path is grind advice / issue reply, not the app saved-screen copy this PR changes. Out of scope unless you want one vocabulary everywhere.
- Draft PR; validation note that deps were unavailable is obsolete in this review checkout — focused tests ran green here.

---

## Reproductions

### Finding 1 — default ratio label regression

```bash
cd "$REVIEW_CHECKOUT"
uv run python - <<'PY'
import re
from datetime import datetime, timezone
from app.views import target_svg
from coffeedata.models import Shot
s = Shot(shot_id="s1", user_id="o", source="app",
         brewed_at=datetime(2026,10,28,tzinfo=timezone.utc),
         dose_g=18, yield_g=36, time_s=28, grinder_setting=11,
         rating=4, taste="balanced", grinder_id="g1", basket_id="b1", bag_id="bag1")
labels = re.findall(r'text-anchor="end">([^<]+)</text>', target_svg([s]))
print(labels)
PY
```

**Observed:** `['1:1.8', '1:2', '1:2.2', '1:2.4', 'espresso']`  
**Expected (main):** `1:2.0` among the ratio ticks.

### Finding 3 / happy path — new test

```bash
uv run pytest -q \
  tests/test_app.py::test_long_ratio_shots_are_shown_without_espresso_time_judgment \
  tests/test_app.py::test_save_shows_saved_and_today_without_grind_advice
```

**Observed:** `2 passed`.

### `_ticks` fail-closed on equal bounds (latent; current callers avoid it)

```bash
uv run python -c 'from app.views import _ticks; _ticks(5,5)'
```

**Observed:** `ValueError: math domain error` (`log10(0)`). Current axis policy never calls `_ticks` with `high == low`.

---

## End-to-end walk

PR body validation checklist, run on the review worktree at `b52e099`:

| Step | Result |
|------|--------|
| Install deps in worktree (`uv` sync via pytest bootstrap) | Cleared |
| `test_long_ratio_shots_are_shown_without_espresso_time_judgment` | Cleared |
| `test_save_shows_saved_and_today_without_grind_advice` (updated assertion) | Cleared |
| Broader `tests/test_app.py -k "shot or save or coffee or long_ratio…"` (10 tests) | Cleared |
| Browser flow `tests/browser/app_flow.js` | Not run (needs live app URLs); assertion regex updated and looks consistent with new `summary()` |
| Lance Hedrick / SOUP fetch | N/A — PR correctly avoids prescribing a recipe |

Unchecked PR claim “Tests were added but could not be run” — **cleared here**; say so in the PR before review.

---

## Adversarial inputs

| Input | Expected | Observed |
|-------|----------|----------|
| No shots (`target_svg([])`) | Default 18–38 / 1.6–2.5 axes; espresso band present | Default ticks; `"espresso"` + traditional-reference aria present |
| In-window only | Fixed tick set; marks unclamped within box | Default ticks; label regression on `1:2` (finding 1) |
| Times `< 18` and `> 38` together | Axes expand both ways; distinct mark x | Distinct cxs; band still positive size |
| Times at legal min/max (`1`, `600`) with extreme ratios | Distinct marks; no crash | Distinct marks; `tmin` pad → negative (nit 4); ticks include `0 s` |
| Ratio only above 2.5 | `rmax` expands; times stay default | Distinct cy; ticks via `_ticks` |
| Ratio only below 1.6 | `rmin` expands (floored at 0) | Works; labels `1:1`, `1:1.5`, … |
| `_ticks(low, low)` | Should not be called; would throw | `ValueError` — latent footgun |
| Adjacent / corrupt / offline `gh` | n/a: no new CLI, hook, or installer | n/a: view/SVG + tests only |
| Workspace never ran Cadence | n/a | n/a |

---

## Contracts

| Claim / doc | Status |
|-------------|--------|
| PR intent: show outside-bound shots; no single prescribed recipe | Matches code |
| `docs/app/PLAN.md` saved-screen copy (window verdict sentences) | **Contradicts** this PR — author decision; update docs or explicitly supersede (finding 2) |
| `app/views.py` module docstring: descriptions only, no grind advice | Still holds for `summary` / chart |
| `TARGET_TIME` / `TARGET_RATIO` as geometry for the shaded band | Unchanged; still imported from `coffeedata.shots` |
| `core:dry-extension` | `g()` reused for ticks loses format contract — prefer a dedicated tick formatter (finding 1) |
| `core:standalone-package` | N/A (app code, not Cadence peers) |

---

## Design choices

**Finding 1 (tick labels):** one clear fix — restore fixed decimal ratio labels. No option matrix.

**Summary copy (product):** scenario matrix (expected):

| Scenario | Keep relative “faster/slower” without window words | Shipped: ratio+time only | Over-engineered: per-profile windows |
|----------|----------------------------------------------------|---------------------------|--------------------------------------|
| Dial-in user comparing to yesterday | Clear delta | Must parse two times | Clear but heavy |
| Non-espresso / long-ratio shot | Neutral if window words gone | Neutral | Neutral |
| Matches PLAN.md today | No | No | No |
| Diff size | Small | Smallest (shipped) | Large |

**Pick:** shipped neutral line is coherent with “no prescribed recipe”. Miss cost of dropping “faster/slower” is UX, not correctness. Update PLAN (finding 2). Optional later: restore relative delta without window judgment (`two seconds slower than Monday’s shot` only).

---

## Scope

- **Title type:** feature/UX — matches diff.  
- **Files:** 3 (`app/views.py`, `tests/test_app.py`, `tests/browser/app_flow.js`).  
- **Mix:** runtime + tests only; **docs/mockups not updated** despite product-copy change — recommend folding PLAN/mockup edits into this PR or a tight follow-up, not a split of the code itself.  
- No need to split the code change.

---

## Lookback

| Finding | Failure class |
|---------|---------------|
| 1 | Formatter reuse without preserving the display contract (`:g` vs fixed precision) |
| 2 | Product copy changed in code without updating the written plan/mockups |
| 3 | Happy-path test for the motivating case; other axis branches unpinned |
| 4 | Padding applied before domain floor (time ≥ 0) |

After fixes: re-run the two focused tests plus a default-axis label assertion; re-read `target_svg` tick formatting and PLAN §2.

---

## Test sufficiency

- Good: end-to-end HTML assertions for judgment removal and unclamped mark positions on `/`, `/saved/s2`, `/coffee/bag1`.  
- Gap: finding 3.  
- No partial-success thresholds.  
- Browser assertion loosened to a regex — appropriate given dynamic weekday/`_when` text.

## Merge blockers

1. Fix default ratio tick formatting (Required #1).  
2. Resolve PLAN/mockup contract drift (Should-fix #2) — update docs or record an explicit supersede in the PR.  
3. Mark draft ready when those are done; CI + human approval still required.

---

## Update 2026-10-29

```
RUN_BY_COMMAND: pr-review-update
```

**Range:** `b52e09931bf4de0268545979e461a2b83cca7d8b..9bb5c217fcfa6139324c749cf0bd49ea06104662`  
**Commit:** `9bb5c21` Keep default chart ratio labels consistent  
**Files:** `app/views.py`, `tests/test_app.py` (+7 / −2)  
**GitHub threads:** none new (one general comment is the prior review body)

### Earlier findings

| # | Severity | Status | Evidence |
|---|----------|--------|----------|
| 1 | Required | **fixed** | Default axes render `1:2.0`. New test asserts `>1:2.0</text>` in `target_svg([])`. Reproduced in detached worktree at `9bb5c21`. |
| 2 | Should-fix | **open** | `docs/app/PLAN.md` §2 step 5 and `mockups/{saved,today}.html` still prescribe “Inside the window…” / “Ran fast…”. PR body does not supersede them. |
| 3 | Should-fix | **partial** | A test pins the default tick format. No case covers tmax-only or rmin-only. |
| 4 | Nit | open | Unchanged. |
| 5 | Nit | open | Unchanged. |
| 6 | Nit | open | Unchanged. |

### New findings

7. **[V] Required — `ruff format` fails CI on this head** — `app/views.py`, `tests/test_app.py`  
   - **What:** CI job `test` fails at `make lint` / `ruff-format`. Ruff wants fewer parentheses on the tick `for` lines in `views.py`, and double quotes in the new tick test.  
   - **Evidence:** Run `40944066238` failed. Local `uv run ruff format --check` on `9bb5c21` reports both files. `b52e099` already failed format on `views.py`. The fix commit adds the test quote miss. PR Validation claims Ruff already passed. That claim is false on this head.  
   - **Recommendation:** Run `uv run ruff format app/views.py tests/test_app.py`, then commit. Run CI again. Align the Validation note with the result.

### Reproductions (update)

Default labels after the fix:

```text
in-window labels: ['1:1.8', '1:2.0', '1:2.2', '1:2.4', 'espresso']
```

Focused tests on `9bb5c21`:

```text
3 passed  (default tick, long-ratio, save/today)
11 passed (shot/save/coffee/long_ratio/default_chart filter)
```

### Verdict

**Approve with required fixes** — Required #1 now matches main and a test covers it. Format CI fails and blocks merge. Finding 2 (PLAN/mockup drift) stays open as should-fix. Finding 3 stays partial.

### Merge blockers (update)

1. Clear `ruff format` so CI `test` is green (new Required #7).  
2. Resolve PLAN/mockup drift (Should-fix #2) or state a supersede in the PR body.  
3. Keep the PR as draft until those land. Human approval is still required.

Last reviewed commit: 9bb5c217fcfa6139324c749cf0bd49ea06104662
