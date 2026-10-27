# Shot-logging app: plan

Status: draft for discussion. Nothing here is implemented. The design is in [DESIGN.md](DESIGN.md) and the mockups
are in [mockups/](mockups/).

Today a shot reaches the lake through the MCP server or the "Log a shot" issue form. Both work. Neither is pleasant
at the machine at 7am with a portafilter in one hand: the issue form is nine text fields, and the MCP route needs a
laptop or a chat. This plan describes a small phone web app that makes the common case (same coffee as yesterday,
small grind change) three taps, and makes a new bag one photo and one tap.

## Goals

- Log a repeat shot in three taps, one-handed, without typing.
- Start a new bag from a photo of it: read the roaster, coffee and roast date, match it to `dim_coffee`, confirm
  with one tap.
- Record equipment once (grinder, machine, basket) and have every shot point at the grinder it was ground on, so a
  grind setting means something across grinders.
- Show dial-in advice the moment a shot is saved, using the same rules as `shots.suggest_next`.
- Give each coffee a page with its shots, best recipe, and the catalog's current price and stock.
- Keep the lake append-only. New tables and new nullable columns only; every existing shot stays valid.
- Keep bag photos private. They never reach the public tables, the public site or git.

## Non-goals

- More than one user. There is one owner; no sign-up, no sharing, no roles.
- A native app or an app store listing. A home-screen web app is enough.
- Bluetooth scales and machine integrations (Acaia, Decent, Gaggiuino). Web Bluetooth doesn't work in iOS Safari,
  and typing two numbers is fine. Worth revisiting later.
- Pour-over and filter recipes beyond what `brew_method` already stores. The UI is espresso-first.
- Editing or deleting lake files. Corrections and retractions stay new records.
- Buying coffee. The coffee page links to the roaster's product page and stops there.
- Replacing the MCP server or the issue form. They keep working and gain the new fields where it's cheap.

## User flows

### 1. Capture a bag

1. Today screen → "New bag". The camera opens (a file input with `capture="environment"`, so no camera permission
   prompt on iOS).
2. Take the photo. The phone downsizes it to about 1600 px on the long edge and removes EXIF (GPS included) before
   upload.
3. The server sends the image to Claude with a fixed JSON schema: `roaster`, `coffee_name`, `roast_date`,
   `origin`, `process`, `weight_g`, plus a confidence per field. A typical read takes 2 to 5 s; the screen shows a
   skeleton of the match card meanwhile.
4. The server matches roaster first (against `dim_roaster` names and domains), then the coffee within that
   roaster's catalog, using `shots.fuzzy_match_coffee`'s scorer. Listed coffees rank above delisted ones.
5. The match card shows the catalog title, roaster, origin, process, tasting notes, the catalog image, price per
   100 g and stock, and the roast date that was read ("Roasted Oct 18, 9 days ago").
6. One tap on "Use this coffee" writes a `bags` record and opens the shot form. If the match is wrong: up to three
   alternatives as big rows, then "Search" as a fallback. If nothing matches (a café bag, a roaster not in the
   registry), the bag is saved with free text, the same way `coffee_freetext` works today.
7. If the roast date couldn't be read, a date picker defaults to 7 days ago.

Two taps when the read is right. The bag becomes the "current bag" until another one is started or it's marked
finished.

### 2. Log a shot

1. Today shows the current bag and a "Pull again" card with the last recipe ("18.0 → 36.4 g · 28 s · grind 12").
2. "Pull again" opens the shot form prefilled from the last shot of this bag (or of this coffee, if the bag is
   new): dose, yield, time, grind, water temp, grinder, basket, brew method, milk drink. If the last shot's advice
   said "go finer to 11", the grind field shows 12 with a suggested 11 next to it; one tap takes the suggestion.
   The app never changes the value on its own.
3. Adjust what changed with steppers (dose and yield in 0.1 g, time in 1 s, grind in the grinder's own step size).
   An optional shot timer: tap the time numeral to start, tap again to stop.
4. Pick taste (sour, balanced, bitter) and rating (1 to 5). Taste and rating have no default; they are the only
   new information in a repeat shot.
5. "Save shot". The saved screen shows the ratio, the time against the 25 to 32 s window and the next-shot advice,
   with "Undo" for 5 s. Undo writes a retraction record (`deleted: true`), not a delete.

Repeat shot with nothing changed but taste and rating: Pull again, taste, rating, Save. Four taps, or three if
rating defaults to the last value (open question 7).

### 3. Review a coffee

From Today or from a bag in the history list: the coffee page. Header with the bag photo, roaster and coffee name,
origin, process and tasting notes from the catalog, plus current price per 100 g, stock and a link to the roaster.
Below: best recipe so far (highest rating, latest wins ties, same as `mart_dial_in`), a ratio-and-time chart of
every shot of this coffee coloured by taste, and the shot list. Each shot opens a read-only detail with "Correct"
(writes a new record with the same `shot_id`) and "Retract".

### 4. Manage equipment

Settings → Equipment. A list of grinders, machines and baskets. Each has a name and a few fields that matter for
advice: a grinder's scale (min, max, step) and which way is finer, a basket's nominal dose. One of each is marked
default. Adding a grinder takes a name and those three numbers. Retiring one hides it from pickers but keeps old
shots pointing at it. Changes are new records, with the latest per `equipment_id` winning.

Most mornings nobody touches this screen. The shot form shows the current grinder and basket as one small chip
("Niche Zero · 18 g VST"); tapping it switches.

## Screens

| Screen | Purpose | Mockup |
|---|---|---|
| Today | Current bag, "Pull again", last three shots, "New bag" | `mockups/today.html` |
| First run | No bag yet: photograph one, or add a grinder | `mockups/first-run.html` |
| Reading | The bag is being read: skeleton card, "Type it instead" | `mockups/reading.html` |
| Capture | Camera and match card | `mockups/capture.html` |
| Log shot | The prefilled form; the one screen that has to be fast | `mockups/log-shot.html` |
| Saved | Ratio, time window, advice, undo | `mockups/saved.html` |
| Coffee | One coffee: catalog facts, best recipe, chart, shots | `mockups/coffee.html` |
| Equipment | Grinders, machines, baskets | `mockups/equipment.html` |
| Search | Fallback when the photo match is wrong | not mocked; a search field over the match card list |
| History | All shots, newest first, grouped by bag | not mocked; same row component as Today |

## Architecture

### What has to be decided

- Where the app runs, so the Hugging Face write token and the Anthropic key stay on a server.
- How the single owner signs in.
- How a shot gets from the phone into the lake.
- Where the bag photo is read.
- What happens with no network.

### Hosting

| Option | For | Against |
|---|---|---|
| A. Hugging Face Space (Docker, FastAPI) | Same account as the lake, so the token never leaves HF. Python, so it imports `coffeedata` directly: `log_shot`, `fuzzy_match_coffee`, `suggest_next` and the lake writer are reused, not rewritten. Free CPU tier. Secrets in Space settings. | Free Spaces sleep after 48 hours without traffic, so the first shot after a trip waits for a cold start (tens of seconds). The disk is ephemeral, so nothing can wait in local staging. |
| B. Cloudflare Workers + Pages | Never sleeps, fast everywhere, free at this volume. | JavaScript. Matching, advice and the Parquet writer would be a second implementation that drifts from the Python one. Parquet writing from a Worker is awkward. |
| C. Small VM or container host (Fly.io, Render, a home server) | Python reuse, always on if paid. | Another account and bill to look after; a home server isn't reachable without a tunnel. |
| D. No server: static app that opens GitHub issues | Reuses the existing issue-form workflow. | Needs a GitHub token on the phone, takes a minute per shot through Actions, and can't do photos or instant advice. |

Recommendation: A, the Space. Code reuse is the deciding factor: the matching and advice rules already have tests,
and a second implementation would drift. The 48-hour sleep doesn't matter for daily use; if it starts to annoy,
upgrading the Space's hardware to an always-on tier is one setting. The Space's own code lives in this repo
(`app/`) and is pushed to the Space by a workflow, so the Space repo never holds anything hand-edited.

The proposal was a Space with the token server-side. I agree with that part and push back on two details. First,
the Space must not rely on its disk: it publishes each write to the lake before acknowledging it, and the phone
keeps the write in an outbox until it's acknowledged (see Offline). Second, reading shots by globbing `shots/*.parquet` over
`hf://` is one HTTP request per file. That's fine at 200 shots and slow at 2,000. The server keeps an in-memory
copy of shots, bags and equipment, loaded at start from the weekly `marts/fct_shot.parquet` export plus the files
newer than it, and updated on every write.

### Auth for one owner

| Option | Notes |
|---|---|
| Sign in with Hugging Face (OAuth for Spaces) plus an allowlist of one username | Built into Spaces, no passwords to store. The session cookie lasts weeks. Recommended. |
| Private Space | Only the owner's HF account can open it. Simple, but iOS home-screen apps keep their own cookie jar, so the HF login can drop out of the standalone app. Fine as an extra layer, not as the only one. |
| Passkey (WebAuthn) set up once | Nice on a phone, but it's code we'd own and get wrong. |
| A long secret in the URL or a cookie | Easy to leak through browser history or a screenshot. No. |

Recommendation: a public Space whose every route except `/login` requires an HF OAuth session whose username equals
`OWNER` (a Space variable). The app's code is public anyway (this repo); the data and the keys aren't. If the owner
prefers, make the Space private as well.

### How writes reach the lake

Each action is one Hugging Face commit, made by the Space with the existing `Lake.write` and `Lake.publish`:

- New bag: the photo file, the `photos` row and the `bags` row in one commit.
- Shot: one `shots` file.
- Equipment change: one `equipment` file.

`Lake.publish` already retries with backoff and treats files that landed on a lost response as uploaded. A commit
takes a second or two. The phone shows the saved screen immediately (the advice is computed from data the server
already holds) and marks the shot as synced when the commit returns.

The phone generates `shot_id` and `bag_id` (UUIDs) before sending. A retried request writes a second file for the
same id; latest-wins collapses it, as for any correction. So retries are safe without any locking.

The weekly run and the Space can commit to the dataset at the same time: they touch different paths, and the
`concurrency: lake` group in Actions doesn't cover the Space, so this relies on HF commits not conflicting on
disjoint files. Worth one test before relying on it (open question 9).

### Where the bag photo is read

On the server, with the Anthropic key as a Space secret. One request per photo: the image, a short instruction and
a JSON schema through structured outputs, so the response always parses. The default model is Claude Opus 5.5
(`claude-opus-5-5`) at low effort. Bag labels are short but varied (hand-lettered names, stickers on kraft paper,
dates as "R 10.18" or "Roasted on 18/10"), and one bag a week makes cost a non-issue. If the owner wants it cheaper,
Claude Haiku 5.5 is a one-line change; test it against a handful of real bags first. The request turns on the API's
server-side refusal fallback, so a declined read falls back to another model instead of failing.

The model only reads the label. Matching to the catalog stays deterministic, in Python, against `dim_coffee`; the
model never sees the catalog. That keeps the read cheap and the matching testable.

Rejected: reading on the phone (on-device OCR gets the text but not "which of these words is the roaster"), and
sending the catalog to the model (45,000 rows, and it would guess).

### Offline

Kitchen Wi-Fi is usually fine, but a shot should never be lost to a dropped connection.

- The app is a PWA with a service worker that caches the shell, so it opens offline.
- Every write goes through an outbox in IndexedDB and is retried until the server acknowledges the commit. The
  Today screen shows a small "2 waiting to sync" line when the outbox isn't empty.
- Offline, the form still prefills from the locally cached last shots, and advice is computed in the browser with
  a short JavaScript port of `suggest_next`'s rules. That's the one piece of duplicated logic, kept small and
  checked against the Python version with shared test cases.
- Bag capture needs the network (the photo has to be read). Offline, "New bag" saves the photo to the outbox and
  lets the owner type the coffee or pick a recent one; the read and match happen when it syncs.

## Data model

All new tables follow the existing pattern: one immutable Parquet file per record, latest record per id wins,
schemas in `lake.SCHEMAS`, globs in `lake.GLOBS` and `dbt/macros/lake.sql`, a zero-row schema file per table.

### New lake tables

`equipment/<equipment_id>__<logged_at>.parquet`

| Column | Type | Notes |
|---|---|---|
| `equipment_id` | string | UUID |
| `logged_at` | timestamp | latest per id wins |
| `kind` | string | `grinder`, `machine` or `basket` |
| `name` | string | "Niche Zero" |
| `model` | string | optional |
| `scale_min`, `scale_max`, `scale_step` | double | grinders only; the step feeds the stepper and the advice |
| `finer_is_lower` | bool | grinders only; `suggest_next` already takes this argument |
| `basket_dose_g` | double | baskets only |
| `is_default` | bool | |
| `retired` | bool | hidden from pickers, kept for history |
| `notes` | string | |

`bags/<bag_id>__<logged_at>.parquet`

| Column | Type | Notes |
|---|---|---|
| `bag_id` | string | UUID |
| `logged_at` | timestamp | |
| `coffee_id` | string | null when unmatched |
| `coffee_freetext` | string | what was read or typed |
| `roast_date` | date | |
| `opened_on` | date | |
| `finished_on` | date | set by a later record when the bag is done |
| `weight_g` | int | |
| `photo_id` | string | |
| `read_json` | string | the model's raw structured output, kept for re-matching later |
| `match_score` | int | |
| `deleted` | bool | |

`photos/meta/<photo_id>.parquet` and `photos/img/<photo_id>.jpg`

| Column | Type | Notes |
|---|---|---|
| `photo_id` | string | UUID |
| `taken_at` | timestamp | |
| `path` | string | `photos/img/<photo_id>.jpg` |
| `width`, `height`, `bytes` | int | |
| `sha256` | string | |

Photos are the first non-Parquet files in the lake. `Lake.write` is Parquet-only, so this needs a small
`Lake.write_blob(rel, bytes)` with the same no-overwrite check, and `Lake.publish` already uploads whatever is
staged. See Privacy for where these files live.

### Changes to `shots`

New nullable columns, appended to the `shots` schema:

| Column | Type | Notes |
|---|---|---|
| `grinder_id` | string | `equipment_id` of the grinder |
| `machine_id` | string | |
| `basket_id` | string | |
| `bag_id` | string | gives `roast_date` when the shot doesn't have one |
| `source` | string | `app`, `mcp` or `issue` |

Old shot files don't have these columns. The lake is read with `union_by_name = true`, so they come back as null,
and nothing about an old shot changes. `Shot` in `models.py` gains the same optional fields; it has
`extra = "forbid"`, so this has to land before the app sends them. The MCP `log_shot` tool and the issue form gain
an optional `grinder` argument that resolves by name.

A grind setting is now only comparable within one grinder. `suggest_next` and `mart_dial_in` should group by
`(coffee, grinder_id)` when `grinder_id` is set, and treat null as "the grinder used before equipment existed".

### dbt changes

- `stg_equipment`, `stg_bags`, `stg_photos`: latest record per id, without deleted ones, the same shape as
  `stg_shots`.
- `dim_equipment`: current equipment.
- `fct_shot`: join `stg_bags` (`roast_date` falls back to the bag's), `dim_equipment` (grinder name, scale), and add
  `grind_position = (grinder_setting - scale_min) / (scale_max - scale_min)` so shots across grinders can at least
  be compared roughly.
- `mart_dial_in`: add `grinder_id` to the per-coffee grain.
- `mart_bag` (new): one row per bag with shot count, best shot, days off roast at first and last shot.
- `public_tables_are_facts_only.sql`: add `photo_id`, `bag_id`, `grinder_id`, `machine_id`, `basket_id`, `path`,
  `read_json` and `sha256` to the forbidden columns.
- The weekly export adds `marts/fct_shot.parquet` (private lake only), which the Space uses as its starting copy.

## Privacy

- Bag photos go to a separate private dataset, `hf://datasets/jtouley/coffee-data-photos`, not the main lake. The
  main lake is private too, but a separate dataset means a mistake in the public export can't reach a photo, and
  the photos could be deleted wholesale without touching the lake's history. The `photos/meta` rows stay in the
  main lake so dbt can see that a photo exists.
- EXIF is removed on the phone before upload, so GPS coordinates of the kitchen never leave it. The server
  re-encodes the JPEG as a second check.
- Photos are served only through the Space, behind the owner check, never by a direct HF URL.
- The image goes to the Anthropic API to be read. Under standard API terms it isn't used for training; it is still
  a third party seeing a photo of a kitchen counter. The capture screen frames the bag tightly to keep the
  background small.
- The public site already lists recent shots and a rating chart (`site.py`). That's coffee name, ratio, time and
  rating; it never showed notes, and it won't show equipment or photos. Whether shots should stay on the public
  site at all is open question 6.

## Cost

| Item | Estimate |
|---|---|
| Hugging Face Space, CPU basic | free; an always-on upgrade, if wanted, is billed by the hour (check the current HF price list) |
| Bag reads, Claude Opus 5.5 ($4 / $20 per million tokens in / out) | ~1,600 image tokens + ~400 prompt + ~300 out ≈ $0.015 per bag. One bag a week ≈ $1 a year |
| Same with Claude Haiku 5.5 ($0.10 / $0.50) | well under a cent a year |
| Photo storage | ~300 KB per bag ≈ 15 MB a year, inside the free private storage |
| Shot and equipment files | a few KB each; negligible next to the weekly catalog snapshots |

The app adds nothing to the weekly run's cost.

## Milestones

Each one is usable on its own and goes through the normal PR and CI process.

1. **Data model.** New lake tables and shot columns, `Shot` model fields, `stg_`/`dim_` models, the public-table
   test update, the `fct_shot` mart export. The MCP `log_shot` tool and issue form accept `grinder`. No app yet;
   tests over a synthetic lake with old and new shots mixed.
2. **App skeleton.** FastAPI on a Space, HF OAuth with the owner allowlist, the deploy workflow, the in-memory shot
   cache. Today and Log shot with prefill and advice. Online only.
3. **Equipment.** Equipment screen, grinder-aware steppers and advice.
4. **Bag capture.** Photo upload, EXIF stripping, the photo dataset, the Claude read, roaster-then-coffee matching,
   the match card and its fallbacks. An offline eval: 20 real bag photos with the expected match, run by hand
   before changing the model or prompt.
5. **Coffee page.** Catalog facts, best recipe, chart, shot list, correct and retract.
6. **Offline.** Service worker, outbox, the JavaScript advice port with shared test cases.
7. **Polish.** Dark mode check in a dark kitchen, haptics where the browser allows, left-hand layout, the shot
   timer, the empty states.

Milestones 1 and 2 are the minimum that replaces the issue form at the machine. Capture (4) is the reason for the
app, but it needs the bag and photo tables from 1 first.

## Open questions for the owner

1. iPhone or Android? It decides whether haptics work (iOS Safari has no vibration API) and how the home-screen app
   keeps its login.
2. Which grinder, machine and basket do you use now? Is the grinder stepped or stepless, and is a lower number
   finer?
3. Is a cold start of up to a minute after two days away acceptable, or should the Space be always-on (paid)?
4. Is a separate private dataset for photos fine, or would you rather keep them in the main lake?
5. Do you want the built-in shot timer, or does the machine's own timer do the job and you'd rather type the time?
6. Should shots stay on the public GitHub Pages site once equipment and bags exist, or move entirely into the app?
7. Should rating default to the last shot's rating, making a repeat shot three taps, or stay empty so every rating
   is a fresh judgement?
8. Is anyone else pulling shots on this machine? If so, "one owner" may need to become "one household".
9. Can we run one test commit to the lake from a throwaway Space while a weekly run is committing, to confirm the
   two don't conflict? (Nothing would be written outside a test path.)
10. Is Opus for the bag read fine at about a dollar a year, or do you want to try Haiku first?
