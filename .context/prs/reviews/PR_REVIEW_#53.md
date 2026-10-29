RUN_BY_COMMAND: pr-review-local
RUN_BY_SKILL: pr-review
Last reviewed commit: 78f71240fbb63db749e22c783c8cdefb46b13254

**Tracker:** github-issues
**Ticket:** #53
**Verdict:** APPROVE WITH NOTES

The reviewer used a detached worktree at that commit
(`/Users/jasontouleyrou/.cursor/worktrees/pr-review-local-a22665d8/coffee-data-tracker-6cabaf14fc83`).
The user branch stayed untouched.

This PR lets a person with two or more open bags pick which bag Today and Log
shot use. Selection lives in an HttpOnly `selected_bag` cookie. Offline new-bag
saves carry `selectBag` in the outbox and apply `/bag/{id}/use` after sync when
no later selection exists. The client and the service worker delete the `/` and `/log` page cache on select.
The PR body names a narrow multi-tab race. That race still reproduces. It is a
should-fix, not a merge block, because the window is small and the Log form
blocks a submit when localStorage disagrees with the rendered bag.

## Findings

### Should-fix

1. `[V]` Multi-tab race can re-apply an older selection after `/use` returns.
   `deliverOnce` checks `selectedBag()?.at` before `fetch("/bag/.../use")`, then
   always calls `rememberBag(entry.selectBag, entry.at)` after the fetch. A
   newer click in another tab updates localStorage during the await. The in-flight
   `/use` still sets the cookie, and `rememberBag` writes the older `{id, at}`
   back. Verified in REVIEW_CHECKOUT: Python asyncio model of the same control
   flow printed `RACE_REPRODUCED` with final id `old-bag`. The PR body already
   names this. Fix: after the fetch, if `selectedBag()?.at > entry.at`, call
   `/use` for the newer id (or skip `rememberBag` and re-select the newer bag).
   Do not leave the cookie on the older bag.

### Nits

2. `[V]` Finishing the selected bag clears localStorage in `deliverOnce` but
   never clears the `selected_bag` cookie. Prefill falls back when the bag is
   finished, so Today and Log still show an open bag. The cookie still holds the
   finished id until the next `/use`. Verified in REVIEW_CHECKOUT: after finish,
   cookie was `bag2` while prefill returned `bag1`. Prefer
   `delete_cookie("selected_bag")` on the bag write when that bag is the cookie
   value, or clear it from `/use` of the fallback path.

3. `[V]` `/bag/{bag_id}/use` changes state on an HTTP `GET`. `SameSite=lax` and
   UUID bag ids keep cross-site risk low. `next` only chooses `/` or `/log`
   (evil `next` redirected to `/`). An HTTP `POST` would match the rest of the
   write API better. Not required for merge.

4. `[V]` `/bags/switch` with zero open bags still returns 200 and the heading.
   The Today link hides when `open_bags | length <= 1`, so this is URL-only.
   An empty state line would help if someone bookmarks the page.

### Test gaps (non-blocking)

5. `[V]` `tests/test_app.py::test_switch_open_bag_uses_its_own_shots_and_stays_selected`
   passes and covers switch UI, cookie selection, per-bag prefill, cross-user
   404, and finished-bag 404. There is no test that finish leaves or clears the
   cookie. Browser coverage for `selectBag` after sync exists in
   `tests/browser/app_flow.js` but could not run here (system Node failed to
   load `libsimdjson`).

## Reproductions

Worktree head: `78f71240fbb63db749e22c783c8cdefb46b13254`.

```text
$ uv run pytest tests/test_app.py::test_switch_open_bag_uses_its_own_shots_and_stays_selected -q
1 passed

$ uv run pytest tests/test_app.py -q
27 passed in 2.90s
```

Cookie remains after finish (finding 2):

```text
SELECT status 303 set-cookie selected_bag=bag2; HttpOnly; ...
FINISH status 200
cookies after finish (server never clears) bag2
prefill after finish bag1 expected bag1 fallback
use finished bag 404
evil next location / status 303
bob use alice bag 404
REPRO_OK
```

Race model (finding 1):

```text
{'result': 'applied', 'final': {'id': 'old-bag', 'at': 1000}}
RACE_REPRODUCED
```

## End-to-end walk

Path from the PR body: select an open bag, keep bag-specific prefill, keep
offline new-bag selection, invalidate cached Today/Log.

1. Two open bags show `Switch coffee` on Today. Cleared.
2. `/bags/switch` lists open bags. Cleared.
3. `/bag/bag1/use` sets cookie and Today/Log prefill use bag1 grind. Cleared
   (`grinder_setting` 12 vs 20).
4. Finish selected bag2. Prefill falls back to bag1. Cleared. Cookie stale
   (finding 2).
5. Other user cannot `/use` this bag. Cleared (404).
6. `next=https://evil.example` does not open-redirect. Cleared (Location `/`).
7. Offline browser flow in `app_flow.js` was not executed. System Node is broken
   on this host (`dyld` missing `libsimdjson`). JS brace balance check only.

## Adversarial inputs

| Input | Result |
| --- | --- |
| Finished bag `/use` | 404 `no such open bag` |
| Other user's bag_id | 404 (scoped by `store.bag`) |
| `next=https://evil.example` | 303 to `/` |
| `next=log` | 303 to `/log` |
| Cookie points at finished bag | Prefill uses `current_bag` |
| Zero open bags on `/bags/switch` | 200, heading, no rows |
| One open bag on Today | No `Switch coffee` link |
| Outbox `selectBag` with older `at` after newer click | Cookie/localStorage can go back to older bag (finding 1) |
| Offline navigate to `/bag/.../use` | Not in `KEEP`. SW serves `/offline`. Existing open bags cannot switch offline. |
| Missing / corrupt localStorage | `selectedBag` returns null. Server cookie still drives prefill. |

No new CLI, hook, or installer in this PR.

## Contracts

No written app contract forbids cookie selection or outbox `selectBag`.
`core:dry-extension` and `core:standalone-package` do not apply to this app
diff (Cadence packaging rules).

The PR body accepts the multi-tab race as a known limitation. Treat finding 1
as an author decision: ship with the note, or add the post-fetch re-select.
Either matches the stated design. The safer fix is small.

Offline switch of an existing open bag needs network today. That matches the
cookie-on-server design. It is a product limit, not a contract break.

## Design choices

Finding 1 has more than one fix.

Scenario matrix (expected):

| Scenario | Expected |
| --- | --- |
| Sync select, no later click | Selected bag is the new one |
| Later click before `/use` starts | Later bag wins (current `at` check) |
| Later click during `/use` await | Later bag wins |
| Finish selected bag | Prefill uses another open bag |

Options (not built in separate worktrees, scored from code reading):

1. **Simple:** After `/use`, if `selectedBag()?.at > entry.at`, `fetch` `/use`
   for the newer id and `rememberBag` that id. Diff is small. Covers the matrix
   row the PR misses. No new API.
2. **Medium:** Change `/use` to POST with a client `at` token. Server stores
   last-write-wins per user. More surface area. Better for many tabs.
3. **Over-engineered:** Move selection fully into IndexedDB and stop using a
   cookie. Breaks current server prefill unless every page reads a header.

Pick option 1 if you fix before merge. Keep the PR note if you defer.

Finding 2 is one line on the finish path or a `delete_cookie` beside the
localStorage clear. No matrix needed.

## Scope

Title type matches the diff: allow switching between open bags.
8 files, +121 / −7. Server routes, cookie, templates, SW cache bust, outbox
`selectBag`, one unit test, one browser assertion. One concern. No split.

## Lookback

Failure class for finding 1: check-then-act across an await without a second
check, while a side effect (Set-Cookie) already landed. Same class as other
outbox vs UI races in this app. After a fix, re-run `tests/test_app.py` and add
a focused unit or browser case that starts `/use`, writes a newer
`selected-bag:${ME}`, then asserts the newer bag remains.

Failure class for finding 2: two stores (cookie + localStorage) updated on
select but only one cleared on finish. Prefill masks it. Clear both on the same
event.
