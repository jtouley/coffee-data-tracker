# admin.registry.coffee: the command center's pages

The owner's command center: what the pipeline wrote to the lake, and the actions that replace the CLI (start any
pipeline job, include or exclude a coffee, build a draft, Publish, Roll back). The service, its sign-in and its
deployment are in [README.md](README.md); the wider plan is `docs/design/admin-registry.md` (#112). This file is the
design pass, first for the read-only pages (#125), then for the actions and the cut that came with them (below,
"Actions").

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

## Actions (the second pass)

### Who and what, again

The same owner, now doing what used to need `gcloud run jobs execute`, `coffeedata rollback` or a hand edit:
1. **Run a job off-cycle:** a crawl (one connector, all five, or one roaster), the Shopify retry, fetch health,
   merge, enrich, classify, the build. A test crawl is the default; counting it for a week is a deliberate choice.
2. **Correct the catalog:** keep a product the classifier dropped, or drop one it kept, alone or as a group, with a
   reason, and undo it.
3. **Release it:** build the corrections as a draft, read what changes, Publish, or Roll back.

### Five pages, one way to do each task

| Page | Answer line | Actions |
|---|---|---|
| Week | connectors landed · merge · enrich · classify · build · serving week; then the week's failures | Test crawl (a link per failed store, to Jobs, prefilled) |
| Jobs | N running · N started from here · last by whom | Start any job (a confirm with the exact arguments), Cancel |
| Release | serving X · N decision changes not in it · the latest draft's state | Build a draft, Publish, Roll back, Discard |
| Catalog | N of M products | Tick products, then Include or Exclude them (the bulk bar); Undo |
| Log | N writes recorded | (read only: every write, filterable; the decisions download for `decide export-labels`) |

- **One way each.** A job starts only from Jobs (Week's and Release's links go there, prefilled). A decision is made
  only in the Catalog: tick one product or several, then the bulk bar, in the same confirm sheet as every other
  write (with a reason, and coffee or version). A release changes only on Release.
- **One primary action per view** (Krehel `better-layout`): Start… on a job's form, Build a draft or Publish on
  Release, the decide button in its sheet. Everything else is a text button.
- **The confirm step says exactly what happens** (Owl-Listener `form-design`, `ux-writing`): the Cloud Run job and
  its arguments as they will run, the guards' refusals in words, and the scheduled window with a field for the
  reason to override it. Confirm is off while anything refuses.
- **Destructive confirms** (Cancel a run, Roll back, a Publish where more than 50 public rows leave) fill the confirm
  button with `--danger`; the last also asks for the typed count. No other new colour.
- **Outcome and Undo** (Owl-Listener `feedback-patterns`): a write's result is one line at the foot of the screen
  (`role=status`), with Undo after a decision; the region it changed reloads in place.

### States of a write

From `state-machine` and `error-handling-ux`: every write is in one state, with a way out.

| State | What the owner sees |
|---|---|
| Ready | the form, or the row's Include/Exclude button |
| Checking | the confirm sheet: "Checking what this starts…", then the plan |
| Refused | each reason in `--danger` text; Confirm off; a window refusal offers the override field |
| Working | the bag page's filling cup in the Confirm button, which ignores further clicks (and the server acts once per key) |
| Done | the outcome line; the region reloads; Jobs polls every 5 s while anything runs |
| Failed | the reason in the sheet (Cloud Run refused, the database down: "nothing was done"); the form keeps its key, so a retry acts once |
| Writes off | "Actions are off: <why>", once per page; no action buttons; the pages still read |
| Signed out | a write sends you to sign in, saying the session ended |

Drafts have their own states, all derived (nothing is a status anyone sets): building, failed, built, stale (the
served release moved), publishing, published (from `serving_log`), discarded.

### Skills used (this pass)

Local copies from the skills index:
- MengTo `no-ai-design-slop`: keep Bench Scale; no tiles, badges or pills for status; hierarchy before labels.
- Owl-Listener `critique-information-density` (the bloat pass), `state-machine`, `error-handling-ux`,
  `feedback-patterns`, `form-design`, `ux-writing`, `loading-states`, `accessibility-audit`.
- Krehel `better-layout` (one primary action per view; order by importance; plan for clipping) and
  `better-typography` (tabular numbers; the sheets' serif titles).

### Before, during, after

- **Before** (`scratchpad/admin-actions-shots/before/`: #125's pages as merged): eight tabs' worth of reading spread
  over five pages, two build pickers and two week pickers; the Catalog at 13 columns.
- **During** (`admin-actions-shots/during/`, from the first Playwright runs):
  - the decide sheet's title was the 13 px section label: now a serif title;
  - the `input_hash` choice wrapped mid-word inside a flex label: now a block label with a hanging radio;
  - once the Catalog scrolled sideways, the pinned checkbox column covered product names, and the decision was the
    last column: the product is now pinned beside the checkbox and wraps (bounded) on a phone, with the decision next
    to it;
  - the draft's `sh -c` script made its Jobs row six lines tall: long arguments fold behind "arguments";
  - Release put a 40-row metrics table above the changes that decide a Publish: Changes now comes first, and the
    build facts and metrics fold;
  - Release offered "Build a draft" again for decisions a built draft already held: it now points to that draft;
  - the filled danger confirm failed contrast in dark mode (axe): its text is `--paper` now.
- **After:** `admin-actions-shots/after/`, both Playwright runs (the read-only pages and the actions), every state
  at 390 px and 1,280 px, light and dark, with axe: no serious or critical issue, no sideways scroll at 390 px.

### Cut (the bloat pass)

For every page, panel and control: does the owner need it to replace the CLI or to understand the week? Cut where
not, merge where two overlap.
- **Failures merged into Week** (one week picker; one page answers "did it land, and what failed").
- **Gate and Changes merged into Release** with the draft, Publish and Roll back (one build picker; the gate and the
  changes are what Publish is decided on).
- **Week's two Failures count tables cut:** the answer line already gives each count (and now the not-attempted and
  unavailable ones too).
- **The Catalog's build picker cut:** the Catalog shows the served build, and Release links to any build's catalog.
- **Catalog columns 13 to 8:** coffee, decided by and reason are one "Classifier" column; listed and stock one
  "Status"; origin and process one; the price per 100 g went (the lowest price and its filter stay).
- **Folded:** a build's facts and its metrics (one line says how many moved).
- **Code:** the five page routes are one loop over `PAGES`; the job list is one table (`admin/jobs.py`), and the
  list Terraform grants is derived from it and checked by a test.

### Cut in the final review

- The Catalog's per-row Include…/Exclude… buttons: the bulk bar does the same with one ticked row. They also clipped
  the decision column at 390 px, as did the pinned product column, now placed after a fixed 44 px checkbox column.
- The decide dialog: the shared confirm sheet asks for the reason (and coffee or version) where a write needs one.
  Publish and Roll back take their reason there too, instead of a field on the page.
- Release: the Decisions paragraph is one line under the answer line; Changes shows only the rows when there are 20
  or fewer.
- Jobs: "Site" is a link in the intro line, not a job; the Action column is folded into the job's cell.
- Code: dead code (`Action.acts_on_week`, `WeekState.build_ended`, `ControlPlane.decision_log`); `queries.releases`
  and `build_snapshot` call quality's own rules; Release reads the served release and the drafts once.
- Kept: Release's build select. It is the one way to reach a release served before, which Roll back needs.

## Rules and flags (the third pass)

### Who and what

The same owner, on Monday evening, after Week says the build landed: **what did the rules flag, and what should I do
about it?** A product the classifier dropped that pasted text makes doubtful; two stores' drink mixes that two
families of rules agree aren't coffee; a store that sells no coffee. Then: decide, check the store, and put it in
the draft.

### One page more, not three

The design (#112 §5.1) lists Queue and Case as pages, and settings editing as its own. Here they are one page:
- **Queue** (`/queue`): the served build's open flags, one row per product, products with the same evidence
  together under a band that offers the group's decision; sorted by the public rows a removal touches.
- **A case is a section of the Queue** (`/queue?case=<coffee_id>`, "← Queue" back): the flags with polarity and
  evidence, the decision, the listing, then the store (crawl flag, live check, fetches). It is the Queue's detail
  view (Owl-Listener `critique-information-density`: progressive disclosure, a detail view instead of inlining).
- **The rule settings fold at the foot of the Queue**: they are edited rarely, and only make sense next to the flags
  they produce.

| | Answer line | Then | Primary action |
|---|---|---|---|
| Queue | "3 products with open flags · 2 ready to remove · N public rows" | filters (roaster, rule with counts, products: open, ready, doubted); the table: product, flags ("2 supports · ready", "1 doubt · 1 restore"), decision, public rows, roaster | none: a lone product's decision is a text button; a group's one decision is on its band, and its rows link to their cases |
| Case | the product's one line and its public rows | Decision (with why it is or isn't ready), Flags, Product, Store | the decision the flags propose (Exclude when ready, Include when restored); the other is secondary |

### Decisions on the design

- **No new component.** The band, the summary line, the flags list and the settings fold reuse the answer line,
  `dl.facts`, `.picker`, `.frame` tables and the shared confirm sheet, which asks the reason and coffee-or-version
  for a decision, as the Catalog's does since #128's final review (MengTo `no-ai-design-slop`: repeated components
  only for a repeated content type).
- **The decision before the metadata.** The Queue's columns are product, flags, decision, public rows, roaster: on a
  phone the table scrolls in its frame, and the decision is on screen with the product (the first screenshots had
  it last, off screen at 390 px).
- **Polarity is a word,** never a colour: "supports", "doubts", "restore", "ready", "coverage". No badges.
- **Store text stays text.** Titles, tags and descriptions render escaped; the description keeps its line breaks
  (`white-space: pre-line`) at a readable measure; a store link is shown only when it is `https:`.
- **One live check, one Test crawl.** The design names both on the Case; they are the same job (a test crawl of the
  store), so the Case has one button, "Live check (test crawl)…", whose confirm shows the exact arguments.
- **Settings are JSON in a textarea,** checked against the code's schema in the confirm step, which says what
  changes, which changes decide, and what Publish will need. A form per field would have to change with every rule
  type; this one doesn't (adding a rule is its entry and its model).

### Skills used (this pass)

- MengTo `no-ai-design-slop`: the existing system only; no badges or tiles for polarity; decisions through the
  shared confirm sheet, not a sheet of the Queue's own.
- Owl-Listener `critique-information-density`: the Queue's answer first, five columns, the case as a detail view, the
  settings folded.
- Krehel `better-layout`: one primary action per view, identity before metadata in each row, the band grouped with
  space and the panel tint, nothing scrolls sideways at 390 px.

### The final review's cuts

- **The guard is gone** (its evaluation: 4 of 150 held products were coffee). With it went `held` and "held" in the
  Queue's answer, filter and summaries, and the Case's "the guard keeps it as coffee". `shared_body` stays as a
  doubt shown on the Case.
- **One decision per group.** A product inside an evidence group has no button of its own; the band's decision
  covers it and each row links to its case (Krehel `better-layout`: one primary action per view).
- **The crawl flag's reason** is asked in the confirm sheet, like a decision's, instead of a field always on the page.
- **The rule types' list** in the settings fold went: it repeated the schema and the README.
- **Code:** the Case reads the Queue's own summary row; the Queue's totals come with its rows (one scan fewer); one
  reason check in the control plane; `AUDITED` on one line.

## Sources (the fourth pass)

### Who and what

The owner, with a roaster's name or address in mind (from a bag, a shop or a link someone sent): **is it in our
list, and is it a roaster?** They search, find the row and flag it in one click. On another evening they might work
through the unreviewed cafés whose names say "roast".

### Decisions on the design

- **Search first, one Filter / Sort button beside it.** A pasted address (`https://www.example.com/shop`) searches
  for its domain. Every filter (status, origin, tag, state, city) and the sort (status, name, state, newest) sit in one
  panel: a sheet at the bottom on a phone, a popover under the button on a desktop. The button shows how many are in
  use. On a phone, Enter searches and the Search button is hidden, so the box keeps its width.
- **A data table on a desktop, stacked rows on a phone.** At 760 px and wider each place is one dense 44 px row:
  select, Name, Domain (a link), City, State, Tag, Hints, Status, First seen, ⋯. A header sorts by its column and
  again the other way (`aria-sort`, an arrow); the header row stays at the top while the page scrolls; long cells
  end in an ellipsis with the full text in a `title`; dates are tabular. On a phone the same table shows the name,
  a muted line of domain, city, hints and status, and the ⋯; the sheet's Sort by options replace the headers.
- **No action buttons on rows.** The status is a quiet word (Roaster, Roaster (inherited), Not a roaster,
  Unreviewed). Selecting rows (the box, or a tap
  on the row) shows a sticky action bar at the bottom: "N selected · Roaster · Not a roaster · Clear". That is the
  main path, for one place or many. Each row's ⋯ menu has the same actions for that place, Clear the flag on an
  explicit flag, and Open site. Undo is in the outcome line.
- **One count line.** "N to review of M places · new and gone in the latest run", then one small line of the other
  statuses. The Status and Tag facets carry the per-status and per-tag counts.
- **No "add a roaster" form:** discovery lists about 47,000 places; search, then flag.
- **Inherited is a status, not a flag.** Nothing happens to a registry roaster unless the owner flags one.
- **Reuse.** `catalog.Filters` takes its facets and search columns from the class, so `SourceFilters` subclasses
  it and adds the sort. The pager is a shared macro. The panel and the ⋯ menu are the page's own (a `<dialog>` and a
  `<details>`), styled from the existing tokens.

### Skills used (the redesign's review)

Run on the screenshots of the realistic fixture (about 3,000 cafés beside the registry), at 390 px and desktop,
light and dark:
- MengTo `no-ai-design-slop`: the hint rendered in the danger colour (it borrowed the form-error class), which says
  "error" for a neutral cue. It is now plain `--ink-2` text, like the rest of the row's metadata.
- Owl-Listener `critique-information-density`: the count line ran to four lines on a phone. It is now one answer
  line plus one small line. The filter panel stacked five full-width selects; it now uses two columns.
- Owl-Listener `accessibility-audit`: the action bar could hide the focused row (WCAG 2.4.11); the page now reserves
  scroll padding while it shows. Its count is announced (`aria-live`). The ⋯ menu closes on Escape and returns focus;
  the panel is a modal dialog with Escape and its title. Targets are 44 px. axe passes.
- Krehel `better-layout`: on a phone the search box shrank to fit three controls (Search, Filter / Sort); the
  Search button is hidden there. The panel's actions fell below the fold on a phone sheet; they are sticky at its
  foot. The action bar wrapped Clear to a second line at 390 px; it fits one line. On a desktop the status sat far
  from its row's ⋯; it is right-aligned beside it.

### The owner's two decisions

- **Pull now** is in the action bar for the selected places and in the ⋯ menu of a place flagged Roaster. It opens
  the same confirm sheet as every job, titled in words ("Probe and crawl 2 places?"), naming the places, with the
  exact commands under Details and the window's override when one is needed. The outcome line stays short ("Marked
  1 place as a roaster." · Undo · Dismiss) and steps aside while a confirm is open. Its live status is one small line on Sources
  ("Pull now: probe succeeded · crawl-shopify running · Jobs"), which polls while a run is going.
- **Not a roaster** says what it did in the outcome line: "2 coffees leave the catalog in the next draft". Undo says
  what came back.

### Following Bench Scale (`docs/app/DESIGN.md`)

- **Flat, with hairlines.** The panel, the ⋯ menu and the action bar have a `--control` border and no shadow ("shadows
  read as smudges"). The phone panel is the app's sheet: `--sheet`, 14 px top corners, rising from the bottom.
- **Status text** is the app's: 13 px `--ink-2`, no border, after the name. Chips stay tappable only (the filter
  chips); status is never a chip or a badge.
- **One accent meaning.** The accent marks what you can tap (⋯, Clear, the menu's items); the one filled button is
  "Show places" in the panel and Search on a desktop.
- **Type and space** from the app's scale (13, 15, 17, 24; 4 to 24 px steps); the panel's title is the serif, as
  every sheet's is.
- **Departures, and why:** no grabber on the sheet (it can't be dragged; Close and Escape dismiss it); 44 px targets
  rather than the app's 48, as everywhere in the admin (it's read at a desk, and dense); the action bar sits at the
  foot like the app's dock, not at the top like the Catalog's bulk bar, because the owner asked for it there on a
  phone.

### Cut

- The screening evidence as a hint (see the README: not in the lake).
- An "add a roaster" form, at the owner's request.
- The row buttons, the Flag, Hints, Origin and City columns, the Hint and Change facets, the by-tag run and the
  explanation paragraph.

### The reviews' fixes

- A place flagged Roaster stays in every later draft once it is published. The registry it is compared against is
  the served release's less the places that release's snapshot added.
- The page's copy is keyed on the `candidates` file listing (no read of the files) and made from exactly those files.
- Without the flags (control plane down), the page shows no status, no flag counts and no Status facet.

## The stack review's round

An independent review of the four admin PRs, then the owner's "super cluttery" on Jobs. What changed:

- **Jobs:** one "Run a job" button and a "?" beside the title (shell.html's title actions, filled from each part's
  `<template data-title>`). The run sheet (a sheet on a phone, a popover on a desktop) has a Job dropdown, one hint
  line, only the chosen job's fields, and Run, which opens the usual confirm with every guard and the window's
  override. Site is a link to GitHub. The page is the Runs table: Started, the job in plain words ("Test crawl ·
  Shopify · onyxcoffeelab.com"), Status, Took, Logs, and a ⋯ with Cancel (in the danger colour, named) and the raw
  arguments. One count line.
- **Help, not prose:** Week, Release, Queue and Catalog keep their answer line first; what they read and how they
  work moved behind the "?" (the `help` macro). Week dropped its Connectors table and "Builds of week" (the answer
  line and the job attempts already say it); its build links go to Release.
- **Folds show a chevron** and are 15 px ink; section headings are 17 px ink, so they outweigh the folds. The outcome
  line is the app's undo strip (`--panel`), not an inverted bar. The data region is no longer a live region, so a
  poll doesn't re-read the tables; the outcome line keeps `role=status`.
- Skills invoked: `critique-information-density`, `hicks-law`, `form-design`, `better-layout`, `no-ai-design-slop`,
  `accessibility-audit`, `critique-affordance`, `critique-visual-hierarchy`, `python-resilience`,
  `dbt-transformation-patterns`, `/simplify`.
