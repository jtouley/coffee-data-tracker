# admin.registry.coffee: the command center's pages

The first admin pages: read-only views of what the pipeline wrote to the lake, for one person who wants the raw
truth fast. The service, its sign-in and its deployment are in [README.md](README.md). The wider plan (queue,
drafts, publish) is `docs/design/admin-registry.md` (#112). This file is the design pass made before any code, and
what changed after the screens were driven in a browser.

## Who and what

The owner, alone, usually on Monday evening or Tuesday morning, on a laptop and sometimes on a phone. Three
questions, in this order:
1. **Did the week land?** Every connector's crawl, the merge, enrich, classify and the build, and which release
   customers are getting.
2. **What failed, and is it getting worse?** Which stores, with which HTTP status or edge challenge, this week and
   the weeks before. This is the weekly 202/429 triage.
3. **What would change, and why?** The gate's tests, the metrics against the last passing build, the release's
   row changes with reasons, and any product, coffee or not.

Every page is a plain query over a named lake file, and says which. Nothing is summarised by a model, scored or
guessed. A number links to the rows behind it, and a row links to its evidence: the store's URL, the CI run
(`ci_run_url`), the release folder.

## Skills used

Read from the local copies listed in the skills index, and applied where named below:
- Owl-Listener `critique-information-density` (hierarchy, scanning, progressive disclosure), `loading-states`,
  `error-handling-ux`, `state-machine`, `search-ux` (facets, counts, removable filters, zero results),
  `dark-mode-design`, `accessibility-audit`;
- MengTo `no-ai-design-slop` (keep the existing direction; no dashboard tiles, eyebrows or decorative charts);
- Krehel `better-typography` (tabular numbers in numeric columns) and `better-layout` (`minmax(0, 1fr)` and
  `min-width: 0` for wide tables).

## Information hierarchy

One rule from `critique-information-density`: the primary answer first, as a sentence of counts, then the table it
came from. No tiles, no charts in this pass.

| Page | First line (the answer) | Then | Source files named on the page |
|---|---|---|---|
| Week | "5 of 5 connectors done · merge done · enrich done · classify done · build passed · serving 2026-11-02" | the served release (build, week, when, why, CI run, release folder); one row per job attempt: job, connector, pass, start, end, status, todo/ok/errors/parked/skipped, 429s, execution | `job_manifests`, `serving_log`, `marts/release.parquet` |
| Failures | "212 of 2,984 fetches failed: 202 × 140 · 429 × 62 · siteground 31 · cloudflare 9 · partial 4 · quarantined 2" | counts by HTTP status; by challenge, partial and quarantined; the trend across weeks when there are several; then every failed store with its status, error and store link | `fetches`, ops `run_completeness` |
| Gate | "build 2026-11-04 passed: 0 failing tests of 214" | failing and warning tests first; then metrics against the previous passing build, with the change | `builds` (kind node and metric) |
| Changes | "public_coffee +12 −3 · 2 unexplained" | counts by table, change and reason, `unexplained` marked; then the rows, filterable by table, change and reason | ops `release_changes` |
| Catalog | "2,431 of 9,876 products" (after filters) | search, facet filters with counts, removable filter chips; one row per product | ops `product_decisions`, release `dim_coffee` |

The week picker and the build picker live in the page header, in plain `<select>`s that submit a GET form, so every
view has a URL the owner can bookmark or paste.

## Density

The app's rows are 56 px tall because they are tapped with a wet thumb. Here they are read at a desk, so
tables are dense: 13 px text in rows about 36 px tall, with tabular numerals right-aligned (`better-typography`) and
labels left-aligned for an F-scan. On a phone the same table scrolls sideways inside its own frame, with the first
column pinned, and the page itself never scrolls sideways. Links in rows keep a 24 px target (WCAG 2.5.8).

The page widens from the app's 480 px to 1,200 px. Nothing else in the layout changes: the same paper, ink, hairline
rules, Archivo and Newsreader, 8 px radii and the one brown accent for things you can tap (`no-ai-design-slop`:
keep the established direction).

## Colour

No new colours. Status is a word first (`done`, `running`, `failed`, `missing`), with a mark beside it: a filled
dot for done, a ring for running, a square for failed, a dash for missing. `--danger` marks failed and `unexplained`
only (an error is what `--danger` is for). Green stays taste-only, as in the app, so "done" is ink.

## States

From `state-machine`: every data region is in exactly one state, and each has a way out.

| State | When | What the region shows |
|---|---|---|
| Loading | the page's lake read is in flight | the bag page's filling cup and "Reading `job_manifests` from the lake…", naming the file. The page frame (header, nav, pickers) is already there |
| Slow | still loading after 8 s | the same cup, plus "The lake is slow. Still reading." The read gives up at 25 s |
| Error | the read failed or timed out | "Couldn't read `fetches`: the lake didn't answer in 25 s." with Retry; the error's class, never a stack. Other regions keep their own state (`error-handling-ux`: keep context) |
| Empty | the file has no rows for the week or build | "Nothing for week 2026-11-09 yet: `job_manifests` has no rows for it." with a link to the latest week that has some |
| Stale | the served release is older than the latest week with fetches | a line above the answer: "Serving week 2026-11-02; week 2026-11-09 has fetches but no release yet." |
| Not recorded | a build from before #124 has no ops tables | "Build X has no ops tables (written from #124 on)." |
| Data | otherwise | the answer line, then the tables |

Reduced motion: the cup is still and half full, as on the bag page.

Signed-out states, each a single centred column like the app's sign-in:
- **Sign in:** one primary button, "Sign in with a passkey". A failure says what to do next ("That passkey isn't
  registered here. Use the other one, or a recovery code.").
- **Enroll:** only while no passkey exists: the enrollment token field and "Register this passkey". After the first
  passkey, the page says enrollment is closed.
- **Recovery codes:** shown once, after enrollment or a new set: ten codes in the invite code's dashed box, with
  "I've saved them".
- **Session ended:** after 12 hours or a sign-out elsewhere: "Your session ended. Sign in again." on the sign-in page.
- **Switched off:** when the service's own secret is missing or too short: "The admin is switched off" and nothing
  else. It never falls back to showing data.

## Search and filters (Catalog)

From `search-ux`:
- one search box over title, roaster, product id and URL, submitted on Enter;
- facets for roaster, origin, process, coffee or not, classified_by, listed or removed, and stock, each value with its
  count over the current result set, and a price range;
- every applied filter is a removable chip above the table, with "Clear all";
- zero results says what was searched and offers to clear the last filter.

The Catalog reads the build's two files into a local DuckDB file once per `build_id` (releases are immutable), so
filters never rescan the lake.

## Accessibility

From `accessibility-audit`: every table has a `<caption>` and `scope`d headers; status never by colour alone;
focus is the app's 3 px accent ring; the loading region is `aria-live="polite"` and `aria-busy` while reading; axe
runs on every page and state in both themes.

## Review log

Screenshots are taken before, during and after the build, at 390 px and 1,280 px, light and dark. The findings of each
pass and what changed are in the PR.
