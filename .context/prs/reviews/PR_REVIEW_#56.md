RUN_BY_COMMAND: pr-review-local
Last reviewed commit: f09276d39ccbd5552dbddf8e09a8fee5f03cd57b

**Tracker:** github-issues
**Ticket:** #56
**PR:** https://github.com/jtouley/coffee-data-tracker/pull/56
**Title:** Protect catalog fetches and queued writes
**Branch:** `fix/crawl-and-outbox-integrity` → `main`
**Verdict:** APPROVE WITH NITS

The reviewer used a detached worktree at `f09276d`. The user tree stayed on its own branch.

This PR does two integrity jobs. Catalog fetches check robots and budgets on every redirect hop and reject incomplete Squarespace pages. The phone outbox binds each queued write to the account that made it and refuses stale IndexedDB updates after a newer edit. The unit suite for those areas passes on this commit when `ANTHROPIC_API_KEY` is unset. No must-fix blocks merge. Two should-fix items remain for live-photo 403 handling and a prepare-race user message.

## Summary of findings

| ID | Severity | Confidence | Topic |
|----|----------|------------|-------|
| SF-1 | should-fix | [V] | Live photo upload treats account-mismatch 403 as a hard fail, not Held |
| SF-2 | should-fix | [L] | `prepare()` failure still shows saved-on-phone / will-sync copy |
| N-1 | nit | [V] | One PR mixes crawl client and phone outbox |
| FYI-1 | fyi | [V] | `ANTHROPIC_API_KEY` in the shell flips `test_photos_are_served_to_their_owner_only` |

## 1. What changed

- `PoliteClient` stops auto-follow. `_follow` checks robots and per-host spacing on each hop. Robots cache keys are `scheme://netloc`.
- Squarespace pagination: non-dict pagination, `nextPage` without a real offset, and still-more after `MAX_PAGES` raise `FetchError`. A first page without `items` is `NotCatalog`.
- App write routes for shots, bags, equipment, and photos use `OutboxWriter`. The client must send `X-Expected-User` equal to the session user.
- Outbox entries carry `version`. `settle` / `prepare` ignore responses that no longer match the stored version. Held 403 reasons stay on the entry. Legacy rows with no `user` need an explicit Assign action.

## 2. Findings

### SF-1. Live photo POST does not Held on account-mismatch 403 [V] should-fix

Outbox `deliverOnce` maps 403 to `Held` and keeps the entry. The online bag-photo path in `app/static/app.js` does not use `send` / `deliver`. On `!r.ok` it calls `failed(...)` and drops the in-memory upload.

After a cookie switch in another tab, a live photo POST sends `X-Expected-User: ME` from the stale page meta while the cookie is the other account. The server returns 403 with a sync-account message. The screen treats that as a failed photo. The photo never enters the outbox.

Verified in REVIEW_CHECKOUT: read `app/static/app.js` around the `/api/photos/${id}` fetch and `app/main.py` `outbox_writer`. `tests/test_accounts.py` and the new browser flow cover account-mismatch 403 for outbox writes. The live photo path has no matching Held test.

Fix (simple): on 403 for that fetch, use the same Held / outbox path used when the network throws. Or show the Held copy and keep the blob under `photo:${id}`.

### SF-2. `prepare()` null still looks like a queued save [L] should-fix

`deliver` throws `Error("This save changed while it was waiting.")` when `prepare` finds no matching version. Call sites treat every non-`Rejected` error as an offline queue success. `send` still runs `showSync()` in `finally`. If Undo removed the entry, or a newer edit replaced it, `mine()` can show zero waiting entries. The form can still say the save is on the phone.

Verified in REVIEW_CHECKOUT: code path read in `app/static/app.js` (`deliver`, `logShot` / bag catch blocks). No browser assertion covers prepare failure. Race tests cover settle on 200/403/422 only.

Fix (simple): throw a dedicated type that the UI maps to a replaced-or-undone message. Do not claim a sync will happen when the key is gone.

### N-1. Mixed scope [V] nit

Sixteen files span crawl HTTP/Squarespace and the signed-in app outbox. The title names both. A split would ease review, but the themes share an integrity goal. Acceptable as one PR.

### FYI-1. Photo test depends on a missing API key [V] fyi

With `ANTHROPIC_API_KEY` set in the reviewer shell, `tests/test_coffee.py::test_photos_are_served_to_their_owner_only` returned `status == "illegible"` instead of `"no_key"`. With the key unset, the same test passed. This is environment sensitivity in an existing assertion, not a logic regression from this PR. Keep CI jobs free of that key for unit runs, or mock `labels.read` in the test.

## Reproductions

Worktree: `/Users/jasontouleyrou/.cursor/worktrees/pr56-review-e3f3bc13/coffee-data-tracker-6cabaf14fc83` at `f09276d`.

### Affected unit suite (API key unset)

```bash
unset ANTHROPIC_API_KEY
uv run pytest -q \
  tests/test_ratelimit.py tests/test_catalog_failures.py tests/test_accounts.py \
  tests/test_bags.py tests/test_app.py tests/test_coffee.py tests/test_invites.py \
  tests/test_m8_deploy.py tests/test_app_browser.py --tb=line
```

Observed: `209 passed, 1 skipped in 39.41s` (browser skipped: local Node failed to load `libsimdjson`).

### Redirect robots and Squarespace guards

```bash
uv run pytest -q \
  tests/test_ratelimit.py::test_redirect_checks_the_destination_robots_before_fetching \
  tests/test_ratelimit.py::test_robots_redirect_loop_fails_closed \
  tests/test_catalog_failures.py::test_squarespace_next_page_without_offset_raises \
  tests/test_accounts.py::test_two_real_accounts_cant_see_each_others_shots -q
```

Observed: all passed. Account test asserts 403 when `X-Expected-User` is missing or names the wrong account, and 200 when it matches Maria.

### Bool `nextPageOffset`

```bash
uv run python  # PoliteClient + squarespace.fetch with nextPageOffset: true
```

Observed: `FetchError: collection /shop has a next page without an offset`.

### FYI-1 with key present

```bash
# ANTHROPIC_API_KEY set in the environment
uv run pytest -q tests/test_coffee.py::test_photos_are_served_to_their_owner_only -vv
```

Observed: `AssertionError: assert 'illegible' == 'no_key'`. Unset key → passed.

### SF-1 / SF-2

No separate failing test. Checked by reading the live photo fetch and `prepare` / catch paths in `app/static/app.js` on this commit.

## End-to-end walk

| Step | Result |
|------|--------|
| Fetch PR head into review worktree | Cleared (`f09276d`) |
| Read diff + full `http.py`, Squarespace `_collection`, outbox JS, `outbox_writer` | Cleared |
| Run affected pytest (key unset) | Cleared: 209 passed, 1 skipped |
| Browser flow `tests/browser/app_flow.js` (account switch, races, assign) | Not run here: host Node binary fails (`libsimdjson` missing). PR body says browser flow passed on the author’s machine. CI with `REQUIRE_BROWSER=1` remains the gate. |
| Full `make test` | Not finished in this review. Author noted a prior interrupt at 509 passed. A full run is still required before merge confidence on unrelated modules. |

## Adversarial inputs

| Input | Expected | Observed |
|-------|----------|----------|
| Catalog redirect to host that Disallows the path | `Blocked` before the target request | Pass: `test_redirect_checks_the_destination_robots_before_fetching` |
| Redirect http↔https with different robots | Check the destination origin’s robots | Pass: `test_redirect_to_http_checks_that_origins_robots` |
| Robots redirect loop | Fail closed | Pass: `test_robots_redirect_loop_fails_closed` (`RobotsUnavailable` / TooManyRedirects) |
| Redirect `javascript:...` | Reject non-http(s) | Fail closed: client raised `InvalidURL` (not a successful fetch) |
| Squarespace `nextPage` without offset | `FetchError` | Pass |
| Squarespace `nextPageOffset: true` (bool) | `FetchError` (bool is not a real offset) | Pass in ad-hoc script |
| More pages after `MAX_PAGES` | `FetchError`, not a silent truncate | Pass: `test_squarespace_page_cap_raises_when_more_remain` |
| Outbox write, no `X-Expected-User` | 403, no row written | Pass: accounts test |
| Outbox write, expected user ≠ cookie user | 403, no row written | Pass: accounts test |
| Stale outbox response after newer version or drop | Must not drop or mark the newer row | Pass: browser race block in `app_flow.js` (not executed here). Code and author’s claim support it. |
| Legacy outbox row with `user == null` | Hidden from sync until Assign | Code + tests assert assign flow |
| Offline host / missing `gh` | n/a for runtime change | n/a: not a Cadence installer PR |

## Contracts

None. Findings do not fight a written design in-repo for these modules. `OutboxWriter` matches the new client header contract. Robots bootstrap still skips `_allowed` on robots redirects on purpose (comment in `_follow`).

`core:dry-extension` / `core:standalone-package`: n/a (application code, not a Cadence stage package).

## Design choices

### SF-1

Scenario matrix: account cookie ≠ page `ME` during live photo POST. The photo must stay recoverable. Or the person must see a Held message that names the account mismatch.

| Option | Matrix | Size | Notes |
|--------|--------|------|-------|
| A. Queue blob on 403 like the offline catch | Full | Small | Matches outbox Held |
| B. Keep failed UI, change copy only | Partial | Tiny | Still drops the upload |
| C. Poll session user before photo POST | Extra | Medium | Does not help a mid-flight switch alone |

Pick A. Cheap idea from B: reuse the server detail string in the Held banner.

### SF-2

Scenario matrix: `prepare` returns null → UI must not claim the entry will sync.

| Option | Matrix | Size |
|--------|--------|------|
| A. Dedicated error type + copy | Full | Small |
| B. `showSync` only, no offlineSaved | Partial | Tiny |
| C. Re-read outbox and branch on key presence | Full | Medium |

Pick A. Add a browser case that drops the key between `send` put and `deliverOnce`.

## Scope

- Type: `fix` matches the diff.
- Files: 16 changed, +445 / −78.
- Concerns: crawl client + Squarespace adapter + app outbox/API. Mixed but titled honestly. Split optional (N-1).

## Lookback

Failure classes:

1. **Cross-tab session drift** — cookie and page identity diverge. Header bind fixes outbox writes. Live photo still uses a one-shot fetch (SF-1).
2. **Stale async completion** — older responses must not win. Versioned `settle` / `prepare` address that. Prepare miss still uses the offline-success copy (SF-2).
3. **Silent catalog truncation** — redirects without per-hop robots, and Squarespace next page without offset or past the page cap. This PR closes those.

After fixes for SF-1 and SF-2, re-run the affected pytest list with `ANTHROPIC_API_KEY` unset. Run the browser flow where Node works (or CI `REQUIRE_BROWSER=1`). Then re-read the combined app.js diff for any new `!r.ok` path that bypasses Held.

## Test sufficiency

Strong for the stated goals. Coverage includes new redirect tests, Squarespace pagination tests, accounts mismatch cases, and a large browser block for races / assign / Held banner. Gaps: SF-1 live photo 403, SF-2 prepare miss. This host did not run the browser file.

No partial-success thresholds in new tests.

## Must fix before merge

None.

## Should fix (non-blocking)

1. SF-1 — Held or outbox the live photo on account-mismatch 403.
2. SF-2 — Stop claiming a sync when `prepare` fails.

## Nits

1. N-1 — Consider splitting crawl vs outbox in a follow-up if more work lands on either side.

## Merge recommendation

Approve with nits. Ship after the author’s claimed full-suite pass (or a CI green run) with `ANTHROPIC_API_KEY` absent from unit jobs. Prefer fixing SF-1 and SF-2 in a fast follow-up if not in this PR.

## Update 2026-10-29

RUN_BY_COMMAND: pr-review-update

**Range:** `f09276d39ccbd5552dbddf8e09a8fee5f03cd57b`..`cf26daa95ef962f150365bc61e336a4443efe54a` (2 commits)

**Worktree:** `/Users/jasontouleyrou/.cursor/worktrees/pr56-update-1793317859` at `cf26daa`. The user tree stayed on its own branch.

**Commits:**

1. `a3b722b` — Reject WooCommerce catalogs truncated at the page cap.
2. `cf26daa` — Clarify contract serving policy (error-severity rules only).

### Earlier finding status

| ID | Status | Notes |
|----|--------|-------|
| SF-1 | open | Live photo `!r.ok` still calls `failed(...)`. No Held / outbox path. `app/static/app.js` unchanged in this range. |
| SF-2 | open | `prepare` null still throws a plain `Error`. Catch paths still use offline-saved copy. Unchanged in this range. |
| N-1 | open (nit) | Scope still mixed. Contract + WooCommerce edits stay on the catalog side. |
| FYI-1 | open (fyi) | Not re-run. Keep unit jobs free of `ANTHROPIC_API_KEY`. |

No earlier finding was a must-fix. None of SF-1 / SF-2 closed in this range.

### New findings

#### SF-3. Square Online still returns a truncated catalog at `MAX_PAGES` [L] should-fix

`shopify.fetch` and the new `woocommerce.fetch` raise `FetchError` when the page loop ends on a full page. `square_online.fetch` breaks only when `page >= total_pages`. If `total_pages` is greater than `MAX_PAGES` (20), the loop ends and the function returns the partial list. That is the same silent-truncate class this PR closed for Squarespace and WooCommerce.

Verified in REVIEW_CHECKOUT: read `src/coffeedata/adapters/square_online.py` lines 98–106. No `else` raise. No test asserts a raise when `total_pages > MAX_PAGES`.

Fix (simple): after the loop, if the last page was below `total_pages`, raise `FetchError` with the same “still more products” shape. Add a unit test that sets `total_pages` above `MAX_PAGES`.

Pre-existing. Not introduced by `a3b722b`. Raised here because this range closed the same gap for WooCommerce.

#### Contract serving wording [V] cleared

`contracts/coffee-data.odps.yaml` and `contracts/public-catalog.odcs.yaml` now say only error-severity rules block serve. That matches dbt’s default (warn does not fail the build unless warn-as-error). `cli.cmd_build` serves only when `passed` is true from the dbt return code. No code change in this commit. No new defect.

### Reproductions (this range)

```bash
unset ANTHROPIC_API_KEY
uv run pytest -q \
  tests/test_adapters.py::test_woocommerce_page_cap_rejects_a_full_final_page \
  tests/test_adapters.py::test_woocommerce_short_final_page_at_cap_is_complete \
  tests/test_adapters.py::test_woocommerce_fixture_fetch \
  tests/test_catalog_failures.py --tb=short
```

Observed: all passed (2 new WooCommerce cases + catalog failure suite).

WooCommerce `for`/`else`: a final page of length 100 at `MAX_PAGES` raises `FetchError`. A short final page at the cap returns the items. Matches Shopify’s fail-closed shape.

GitHub: 0 review threads. CI on `cf26daa`: `test` SUCCESS, `check` SUCCESS, `deploy` SKIPPED.

### Verdict

APPROVE WITH NITS. No must-fix. Ship is fine with SF-1 / SF-2 / SF-3 as follow-ups. Prefer SF-3 in the same integrity follow-up as the other adapter caps.

Last reviewed commit: cf26daa95ef962f150365bc61e336a4443efe54a
