# Shot-logging app: plan

Status: draft, updated with the owner's answers of October 27 (see "Decisions" at the end). Nothing here is
implemented. The design is in [DESIGN.md](DESIGN.md) and the mockups
are in [mockups/](mockups/).

Today a shot reaches the lake through the MCP server or the "Log a shot" issue form. Both work. Neither is pleasant
at the machine at 7am with a portafilter in one hand: the issue form is nine text fields, and the MCP route needs a
laptop or a chat. This plan describes a small phone web app that makes the common case (same coffee as yesterday,
small grind change) the timer plus three taps, and makes a new bag two taps: the photo and a confirm.

## Goals

- Log a repeat shot one-handed, without typing: start and stop the timer, then three taps.
- Start a new bag from a photo of it: read the roaster, coffee and roast date, match it to `dim_coffee`, confirm
  with one tap.
- Record equipment once (grinder, machine, basket) and have every shot point at the grinder it was ground on, so a
  grind setting means something across grinders.
- Show what a shot did the moment it's saved: ratio, time against the 25 to 32 s window, taste, and where it sits
  among this bag's shots. The app doesn't suggest grind changes yet (see Later: a dial-in model).
- Give each coffee a page with its shots, best recipe, and the catalog's current price and stock.
- Keep the lake append-only. New tables and new nullable columns only; every existing shot stays valid.
- Keep shots and bag photos private. Neither reaches the public tables, the public site or git. Shots come off
  the GitHub Pages site (see Privacy).
- Let the owner invite a few people. Each person has their own account, equipment, bags and shots, and sees only
  their own. Accounts are a username and a password, stored as salted hashes in a private Hugging Face dataset.

## Non-goals

- Open sign-up. People join only with an invite code from the owner. No sharing between accounts, no teams, and
  only two roles (owner, member).
- Shared machines. Nobody else pulls shots on the owner's machine; each account describes its own setup.
- Email. No addresses are collected, so there are no emails and no self-service password reset (see Accounts).
- A native app or an app store listing. A home-screen web app is enough.
- Bluetooth scales and machine integrations (Acaia, Decent, Gaggiuino). Chrome on Android does support Web
  Bluetooth, so a scale is possible later; for now typing two numbers is fine.
- Pour-over and filter recipes beyond what `brew_method` already stores. The UI is espresso-first.
- Editing or deleting lake files. Corrections and retractions stay new records.
- Buying coffee. The coffee page links to the roaster's product page and stops there.
- Replacing the MCP server or the issue form. They keep working and gain the new fields where it's cheap.

## User flows

### 1. Capture a bag

1. Today screen → "New bag". The camera opens (a file input with `capture="environment"`, which opens Android's
   camera without the app asking for camera permission).
2. Take the photo. The phone downsizes it to about 1600 px on the long edge and removes EXIF (GPS included) before
   upload.
3. The server sends the image to Claude with a fixed JSON schema: `roaster`, `coffee_name`, `roast_date`,
   `origin`, `process`, `weight_g`, plus a confidence per field. A typical read takes 2 to 5 s; the screen shows a
   skeleton of the match card meanwhile.
4. The server matches roaster first (against `dim_roaster` names and domains), then the coffee within that
   roaster's catalog, using `shots.fuzzy_match_coffee`'s scorer. Listed coffees rank above delisted ones.
5. A confident match uses the rule `shots.log_shot` already applies: the top score is at least 80 and at least 5
   ahead of the next one. The match card then shows the catalog title, roaster, origin, process, tasting notes,
   price per 100 g and stock, and the roast date that was read ("Oct 18 · day 9"). It shows no score; the
   number would only be noise when the answer is clear.
6. One tap on "Use this coffee" writes a `bags` record and opens the shot form. "Pick another match" opens the same
   list as a weak match.
7. A weak match (below 80, or two candidates within 5 points) skips the card. The sheet asks "Which one is it?",
   says what was read from the bag, and lists up to three candidates as rows, each with its score ("match 68") so
   the owner can see how close they are. Tapping a row picks it. "None of these: type the coffee" opens search; if
   nothing matches (a café bag, a roaster not in the registry), the bag is saved with free text, the same way
   `coffee_freetext` works today.
8. If the roast date couldn't be read, a date picker defaults to 7 days ago.

Two taps when the match is confident, three when it's weak. The bag becomes the "current bag" until another one is started or it's marked
finished.

### 2. Log a shot

1. Today shows the current bag and a "Pull again" button with the last recipe ("18.0 → 36.4 g · grind 11").
2. "Pull again" opens the shot form prefilled from the last shot of this bag (or of this coffee, if the bag is
   new): dose, yield, grind, water temp, grinder, basket, brew method, milk drink. Time is not prefilled: it starts
   as "Not timed · last 29 s", because a copied time would be saved as if it were measured. The grind field shows
   the last shot's grind and nothing else; the app doesn't suggest a different one.
3. Adjust what changed with steppers (dose and yield in 0.1 g, time in 1 s, grind in the grinder's own step size).
   Time has two ways in, both always available: the built-in timer (Start when the pump starts, Stop when it
   stops) or typing it. "Type" opens a numeric keypad; tapping any numeral does the same for that field.
4. Pick taste (sour, balanced, bitter) and rating (1 to 5). Both start empty; they are the only new information in
   a repeat shot. A "Same as last" chip above them ("Same as last: balanced, 4") fills both in one tap when the shot
   tasted like yesterday's.
5. "Save shot". The saved screen shows the ratio, the time against the 25 to 32 s window, the taste, the shot's
   place on the dial-in target among this bag's shots, and one line comparing it with the last shot ("Inside the
   window, two seconds slower than Monday's shot"; "Ran fast at 22 s, outside the window"). It describes what
   happened and doesn't say what to change. "Undo" stays for 5 s. Undo writes a retraction record (`deleted: true`), not a delete.

Repeat shot that tasted the same: Pull again, Start, Stop, "Same as last", Save. Three taps plus the timer. If the
taste or rating differs, it's taste and rating instead of "Same as last": four taps plus the timer. Saving without
a time turns the button into "Save with 29 s?". A second tap saves the last shot's time, recorded as copied
(`time_source` = `copied`), so it never passes for a measured time and the dial-in model leaves it out.

### 3. Review a coffee

From Today or from a bag in the history list: the coffee page. Header with the bag photo, roaster and coffee name,
origin, process and tasting notes from the catalog, plus current price per 100 g, stock and a link to the roaster.
Below: best recipe so far (one whole shot: highest rating, the latest of equal ratings, as `mart_dial_in` will pick
it; see dbt changes), a ratio-and-time chart of
every shot of this coffee coloured by taste, and the shot list. Each shot opens a read-only detail with "Correct"
(writes a new record with the same `shot_id`) and "Retract".

### 4. Manage equipment

Settings → Equipment. A list of grinders, machines and baskets. Each has a name and a few fields that matter for
the steppers and, later, the dial-in model: a grinder's scale (min, max, step) and which way is finer, a basket's nominal dose. One of each is marked
default. Adding a grinder takes a name and those three numbers. Retiring one hides it from pickers but keeps old
shots pointing at it. Changes are new records, with the latest per `equipment_id` winning.

Most mornings nobody touches this screen. The shot form shows the current grinder and basket as one small chip
("DF64 V2 · Pullman 18 g"); tapping it switches.

First run asks for a grinder (dial range, step, which way is finer) and a basket (nominal dose); nothing is
hard-coded. The owner's setup, entered then:

| Kind | Name | Settings |
|---|---|---|
| Grinder | DF64 V2 | stepless dial, `scale_min` 0, `scale_max` 90, `finer_is_lower` true, stepper step 0.5 (changeable) |
| Machine | Lelit Bianca | default |
| Basket | Pullman Filtration Basket, 18 g | `model` = `basket17-pullm-v-v`, `basket_dose_g` 18 |

The basket's 18 g becomes the default dose for a coffee's first shot.

## Screens

| Screen | Purpose | Mockup |
|---|---|---|
| Today | Current bag, "Pull again", last three shots, "New bag" | `mockups/today.html` |
| First run | No bag yet: photograph one, or add a grinder | `mockups/first-run.html` |
| Reading | The bag is being read: skeleton card, "Type the coffee" | `mockups/reading.html` |
| Match | Camera and a confident match | `mockups/capture.html` |
| Which one is it | A weak match: candidates with their scores | `mockups/match-weak.html` |
| Read failed | The label couldn't be read: Retake, or type the coffee | `mockups/read-failed.html` |
| Log shot | The prefilled form; the one screen that has to be fast | `mockups/log-shot.html` |
| Saved | Ratio, time against the window, taste, the target, a one-line comparison, undo | `mockups/saved.html` |
| Coffee | One coffee: catalog facts, best recipe, chart, shots | `mockups/coffee.html` |
| Setup | Equipment, plus the account: invite someone, sign out | `mockups/equipment.html` |
| Sign in | Username and password | `mockups/sign-in.html` |
| Sign up | Invite code, username, password | `mockups/sign-up.html` |
| Invite | Owner only: a new single-use code to send | `mockups/invite.html` |
| Search | Fallback when the photo match is wrong | not mocked; a search field over the match card list |
| History | All shots, newest first, grouped by bag | not mocked; same row component as Today |

## Architecture

### What has to be decided

- Where the app runs, so the Hugging Face write token and the Anthropic key stay on a server.
- How people sign up and sign in, and where their accounts live.
- How a shot gets from the phone into the lake.
- Where the bag photo is read.
- What happens with no network.

### Hosting

| Option | For | Against |
|---|---|---|
| A. Hugging Face Space (Docker, FastAPI) | Same account as the lake, so the token never leaves HF. Python, so it imports `coffeedata` directly: `log_shot`, `fuzzy_match_coffee` and the lake writer are reused, not rewritten. Secrets in Space settings. | Docker Spaces now need an HF PRO account, even on the free CPU hardware (found in #15). Spaces sleep after 48 hours without traffic, so the first shot after a trip waits for a cold start (tens of seconds). The disk is ephemeral, so nothing can wait in local staging. |
| B. Cloudflare Workers + Pages | Never sleeps, fast everywhere, free at this volume. | JavaScript. Matching and the Parquet writer would be a second implementation that drifts from the Python one. Parquet writing from a Worker is awkward. |
| C. Small VM or container host (Fly.io, Render, a home server) | Python reuse, always on if paid. | Another account and bill to look after; a home server isn't reachable without a tunnel. |
| D. No server: static app that opens GitHub issues | Reuses the existing issue-form workflow. | Needs a GitHub token on the phone, takes a minute per shot through Actions, and can't do photos or show the saved summary right away. |

Recommendation: A, the Space. Code reuse is the deciding factor: the matching rules and the lake writer already have tests,
and a second implementation would drift. Creating the Space in #15 returned 402: Hugging Face now requires PRO for
Docker Spaces even on free CPU, so this option costs a PRO subscription. If the owner would rather not pay for it,
C (a small container host) runs the same Docker image with no code change. The 48-hour sleep doesn't matter for daily use; if it starts to annoy,
upgrading the Space's hardware to an always-on tier is one setting. The Space's own code lives in this repo
(`app/`) and is pushed to the Space by a workflow, so the Space repo never holds anything hand-edited.

The proposal was a Space with the token server-side. I agree with that part and push back on two details. First,
the Space must not rely on its disk: it publishes each write to the lake before acknowledging it, and the phone
keeps the write in an outbox until it's acknowledged (see Offline). Second, reading shots by globbing `shots/*.parquet` over
`hf://` is one HTTP request per file. That's fine at 200 shots and slow at 2,000. The server keeps an in-memory
copy of shots, bags and equipment, updated on every write. At start, shots come from the weekly
`marts/fct_shot.parquet` export, which keeps each shot's own columns (`shot_coffee_id` and so on, beside the values
`fct_shot` fills in from the bag) and `logged_at` (#15). Then every shot file written since the export's snapshot
began is read on top, latest wins. The export records `snapshot_at`, the time dbt started reading shots, and the
seed reads files from that time on, not just files newer than the latest `logged_at` in the export: a file staged
before the snapshot but published after it would otherwise be missed. Bags and equipment are few, so they're read
straight from their files. With no export yet, the Space reads every shot file.

### Domain: registry.coffee

The owner used to own `registry.coffee` and is happy to buy it back. The name fits: the catalog is a registry of
US roasters and their coffees, and the lake already has a `registry` table. The proposed split:

- `registry.coffee`: the public site that GitHub Pages builds. Pages supports a custom apex domain with HTTPS for
  free; it needs the A/AAAA records and a `CNAME` file in the site output. #19 makes its root a launch page and moves
  the catalog dashboard to `registry.coffee/app/`.
- `app.registry.coffee`: the shot-logging app (the Space). Invitees get a short, memorable address, and the app's
  cookies are scoped to that host, apart from the public site.

Two ways to put the Space on `app.registry.coffee`:

| Option | Cost | Notes |
|---|---|---|
| Hugging Face's own custom domains for Spaces | needs an HF PRO account (or a Team/Enterprise org) | A CNAME to `hf.space` set in the Space's settings. Simplest, and HF handles HTTPS. Forum threads report domains stuck on "pending" for a while; HF support sorts those out. |
| Cloudflare in front of the app | free | DNS on Cloudflare and a small Worker that forwards `app.registry.coffee` to the app's own address. Works for any host, but it's one more piece to own, and the direct `hf.space` address still works (it's behind the same sign-in, so that's fine). |

Recommendation: HF's own custom domain. The Space needs PRO anyway (see Hosting), and PRO includes custom domains,
so Cloudflare would only add a part. Cloudflare stays the route if the app ends up on a container host instead.
One naming wrinkle: the catalog dashboard at `registry.coffee/app/` (#19) and the shot app at `app.registry.coffee`
are different things with similar addresses (open question 5). Either way, the session cookie is
host-only on `app.registry.coffee`, and links in invites use that address.

### Accounts and sign-in

The owner asked for a simple sign-up and login of our own, with the hashed credentials kept in a Hugging Face
dataset, instead of "Sign in with Hugging Face" (most invitees won't have an HF account). So:

#### Where accounts live

A third private dataset, `hf://datasets/jtouley/coffee-data-users`, holding two
append-only tables, `users` and `invites` (see Data model). Keeping accounts apart from the lake and the photos
means nothing that reads the lake (dbt, the MCP server, the weekly run) can see a password hash, and the token
the weekly run uses doesn't need access to it. The Space loads both tables into memory at start and writes a new
record (one commit) for every change.

#### Passwords

Hashed with Argon2id (`argon2-cffi`, its default RFC 9106 low-memory parameters: 64 MiB, 3 passes,
4 lanes), with a per-hash random salt. Before hashing, the password is HMAC-ed with a pepper kept as a Space secret
(`AUTH_PEPPER`), so a copy of the users dataset alone isn't enough to start guessing. Rules follow NIST 800-63B:
at least 10 characters, no composition rules, and a check against a short list of common passwords. Changing a
password writes a new `users` record.

#### Sign-up

Only with an invite code.

1. The owner taps "Invite someone" and gets a code like `K7QD-M2XF-9RPA-T4WE`: 80 random bits as 16 characters of Crockford base32 (digits
   and capitals without I, L, O and U; on input O reads as 0, I and L as 1, case and dashes don't matter), valid for
   7 days and usable once, and a link, `app.registry.coffee/join#K7QD-M2XF-9RPA-T4WE`. "Share link" opens
   Android's share sheet. Only the code's SHA-256 is stored, so it can't be shown again later; a pending invite
   offers "Send a new link", which revokes the old code and makes a new one. The code sits after `#`, so it never
   reaches server logs as part of the URL.
2. The invitee opens the link, lands on "jtouley invited you" with the code filled in, picks a username and a
   password, and is signed in. Typing the code by hand also works.
3. Bootstrapping the owner: when the `users` table is empty, the first sign-up needs `OWNER_SETUP_CODE`, a Space
   secret, and creates the `owner` account. Each value of the secret works once: the server stores the SHA-256 of
   every setup code it has accepted, in the users dataset, and refuses a value it has seen. A setup code that has
   been used is inert, so leaving it in the Space's settings is no standing way into the owner's account.

#### Sign-in

Username and password. A failed attempt gets the same message whether the username exists or not, and
an unknown username is still checked against a dummy hash so the timing doesn't tell. Usernames are visible to
anyone who has been invited, so a lockout keyed on the username alone would let anyone keep the owner locked out.
Instead:

- Five failures for one username from one IP lock that pair for 15 minutes.
- Twenty failures from one IP lock that IP for 15 minutes.
- Failures for one username across all IPs only slow it down: each one doubles a delay before the next attempt is
  checked, from 1 s up to 30 s. They never lock it.

All of this is kept in memory; a restart clears it, which is acceptable at this size.

#### Sessions

No session table in the dataset (that would be a commit per login). The cookie is a signed token
(HMAC-SHA256 with the `SESSION_SECRET` Space secret) carrying `user_id`, `session_epoch` and the issue time. It is
`HttpOnly`, `Secure` and `SameSite=Lax`, lasts 90 days, and renews itself on use. Each `users` record has a
`session_epoch`. "Sign out everywhere", a password change and the owner disabling an account all bump it, which
invalidates every older cookie. Writes also need an `X-Requested-With` header, which a cross-site form can't send,
as CSRF protection on top of `SameSite`.

#### Forgotten passwords

There's no email, so the owner issues a reset code from the Invite screen. It works like an
invite but is tied to an existing account, and using it sets a new password and bumps the epoch. To reset the
owner's own password, the owner sets a new value of `OWNER_SETUP_CODE` in the Space's settings (which proves control
of the Space) and uses it once on the reset screen; as with setup, a value that has been used never works again.

#### Every request is scoped

The server reads `user_id` from the cookie, never from the request body, and filters
every read and write by it. The in-memory caches are keyed by user. Tests cover one user trying to read, correct
or retract another user's shot, bag, photo or equipment.

Rejected: Sign in with Hugging Face (invitees would need HF accounts), a private Space (same problem), passkeys only
(good, but more code to get right; worth adding later as a second way in), and magic links (need email).

### How writes reach the lake

Each action is one Hugging Face commit, made by the Space with the existing `Lake.write` and `Lake.publish`:

- Sign-up, password change, invite: one commit each to the users dataset.
- New bag: two commits. First the photo's Parquet file to the photo dataset, then the `bags` row to the lake. If
  the second fails, the photo is orphaned but harmless, and the outbox retries the bag.
- Shot: one `shots` file.
- Equipment change: one `equipment` file.

`Lake.publish` already retries with backoff and treats files that landed on a lost response as uploaded. A commit
takes a second or two. The phone shows the saved screen immediately (the summary needs only the shot and data the server
already holds) and marks the shot as synced when the commit returns.

The phone generates `shot_id` and `bag_id` (UUIDs) before sending, and keeps each write in its outbox until the
server acknowledges it. The outbox sends a user's entries one at a time, in the order they were made. Three rules
keep retries from undoing anything:

- Every record carries `rev`, a counter the phone sets to one more than the `rev` of the version it last saw (1 for a
  new record). The server drops a write whose `rev` isn't greater than the stored one. So a save whose response was
  lost, resent after an Undo or a Correct, can't bring the old version back. `logged_at` is still stamped by the
  server, but it no longer decides between two versions from the phone.
- A resend identical to what's stored is acknowledged without writing (#13).
- File names are `<id>__<logged_at>-<6 random hex>.parquet` for every table, so two records for one id in the same
  second never collide. #13 does this for shots; equipment and bags need the suffix too.

Each dataset (lake, photos, users) has its own staging folder (`build/lake-staging`, `build/photos-staging`,
`build/users-staging`), and `Lake.publish` uploads only that dataset's folder, one publish at a time per dataset,
under a lock. A shot publish can't carry a password hash or a photo into the main lake. A file that went up in
another request's commit counts as published, so a request never retries a commit that already succeeded.

The weekly run and the Space can commit to the dataset at the same time: they touch different paths, and the
`concurrency: lake` group in Actions doesn't cover the Space, so this relies on HF commits not conflicting on
disjoint files. The owner has approved one test during milestone 2: a commit from a throwaway Space to a test path
while a weekly run is committing.

### Where the bag photo is read

On the server, with the Anthropic key as a Space secret. One request per photo: the image, a short instruction and
a JSON schema through structured outputs, so the response always parses. Bag labels are short but varied
(hand-lettered names, stickers on kraft paper, dates as "R 10.18" or "Roasted on 18/10").

The model is chosen by measurement, as the owner asked: Claude Opus 5.5 (`claude-opus-5-5`) and Claude Haiku 5.5
(`claude-haiku-5-5`) are compared on the same set of real bags before either becomes the default.

- The set: real bag photos with the right answer attached. The app builds this as it's used: every bag confirmed
  on the match card (or corrected through "Pick another match") is a photo with a known `coffee_id` and roast date. The
  first comparison runs on whatever bags exist when capture ships, plus any old bags or photos the owner has to
  hand, and it's re-run once there are 20. Hard labels (hand lettering, a sticker on kraft paper, a date stamp, a
  café's own bag) are worth adding on purpose.
- Until the comparison has 20 bags, Opus is the default, since it's the safer reader and costs about a cent a bag.
- Each model reads every photo three times at low effort, with the same prompt and schema.
- Scored per field: roaster, coffee name and roast date exact (after normalizing case and date format), plus the
  end-to-end result that matters: did the Python matcher land on the right `coffee_id`? Also logged: latency and
  cost per read, and whether the three runs agreed.
- Rule: use Haiku if it gets the same number of correct end-to-end matches as Opus (or misses only bags that Opus
  also struggles with) and its median latency is lower. Otherwise use Opus; at one bag a week the cost difference
  is under a dollar a year, so accuracy decides.
- The comparison is a script in the repo (`app/eval/bag_read.py`) with the photos in the private photo dataset, so
  it can be re-run whenever the prompt or the models change.

With Opus, the request turns on the API's server-side refusal fallback, so a declined read falls back to another
model instead of failing. Haiku 5.5 has no server-side fallback, so with Haiku a declined or failed read drops to
the manual path ("Type the coffee") and is logged.

The model only reads the label. Matching to the catalog stays deterministic, in Python, against `dim_coffee`; the
model never sees the catalog. That keeps the read cheap and the matching testable.

Rejected: reading on the phone (on-device OCR gets the text but not "which of these words is the roaster"), and
sending the catalog to the model (45,000 rows, and it would guess).

### Offline

Kitchen Wi-Fi is usually fine, but a shot should never be lost to a dropped connection.

- The app is a PWA with a service worker that caches the shell, so it opens offline.
- Every write goes through an outbox in IndexedDB and is retried until the server acknowledges the commit. The
  Today screen shows a small "2 waiting to sync" line when the outbox isn't empty.
- Offline, the form still prefills from the locally cached last shots, and the saved screen draws its summary and
  target from them. That summary (the ratio, the window check, the difference from the last shot) is the one piece
  of the server's logic repeated in the browser. It's a few lines, kept in step with the Python version by shared
  test cases.
- Bag capture needs the network (the photo has to be read). Offline, "New bag" saves the photo to the outbox and
  lets the owner type the coffee or pick a recent one; the read and match happen when it syncs.

## Data model

All new tables follow the existing pattern: one immutable Parquet file per record, latest record per id wins,
schemas in `lake.SCHEMAS`, globs in `lake.GLOBS` and `dbt/macros/lake.sql`, a zero-row schema file per table.

Every personal record carries a `user_id`: equipment, bags, photos and shots. Shots logged before accounts existed
have no `user_id`; dbt fills it with the owner's id (a dbt var, `owner_user_id`), so they stay the owner's. The
Space applies the same rule when it loads shots, so a shot logged through the MCP server or the issue form shows up
for the owner straight away, not after the next weekly run. The MCP server and the issue form write `source` (`mcp`,
`issue`) and leave `user_id` empty.

### New lake tables

`equipment/<equipment_id>__<logged_at>.parquet`

| Column | Type | Notes |
|---|---|---|
| `equipment_id` | string | UUID |
| `user_id` | string | whose equipment |
| `logged_at` | timestamp | latest per id wins |
| `kind` | string | `grinder`, `machine` or `basket` |
| `name` | string | "DF64 V2" |
| `model` | string | optional |
| `scale_min`, `scale_max`, `scale_step` | double | grinders only; the step feeds the stepper |
| `finer_is_lower` | bool | grinders only; `suggest_next` takes it, and the dial-in model will need it |
| `basket_dose_g` | double | baskets only |
| `is_default` | bool | |
| `retired` | bool | hidden from pickers, kept for history |
| `notes` | string | |

`bags/<bag_id>__<logged_at>.parquet`

| Column | Type | Notes |
|---|---|---|
| `bag_id` | string | UUID |
| `user_id` | string | |
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

`photos/<photo_id>.parquet`, in the separate private photo dataset (see Privacy)

| Column | Type | Notes |
|---|---|---|
| `photo_id` | string | UUID |
| `user_id` | string | |
| `taken_at` | timestamp | |
| `image` | struct<`bytes`: binary, `path`: string> | Hugging Face's `Image` layout: the JPEG bytes (EXIF removed, about 1600 px on the long edge) and a file name, `<photo_id>.jpg` |
| `media_type` | string | `image/jpeg` |
| `width`, `height`, `bytes` | int | |
| `sha256` | string | of `image`; the same bag photographed twice is easy to spot |

Photos are Parquet like everything else, one photo per file, so `Lake.write` handles them with one new schema
entry and no new code path. The `image` column uses the same layout as the `datasets` library's `Image` feature, and
the schema carries the same metadata it writes, under the `huggingface` key:

```json
{"info": {"features": {"photo_id": {"dtype": "string", "_type": "Value"}, "image": {"_type": "Image"}, ...}}}
```

`Lake.write` builds the table against the schema in `SCHEMAS`, so putting this metadata on the `photos` schema is
enough for every file to carry it. With it, `datasets.load_dataset` returns the column as images, and the Hub's
dataset viewer shows thumbnails. (The viewer works on private datasets only for a PRO account or a Team or
Enterprise organization, so on the free tier the thumbnails won't appear in the browser; the files are the same
either way.) The photo dataset is opened as its own `Lake`
(`COFFEEDATA_PHOTOS=hf://datasets/jtouley/coffee-data-photos`). The `photo_id` on a `bags` row is the only link
from the main lake. Parquet's compression won't shrink a JPEG further, so the files are about the size of the
image.

In the users dataset (`hf://datasets/jtouley/coffee-data-users`):

`users/<user_id>__<logged_at>.parquet`

| Column | Type | Notes |
|---|---|---|
| `user_id` | string | UUID |
| `logged_at` | timestamp | latest per id wins |
| `username` | string | lower-cased, 3 to 32 letters, digits, `-` or `_`; unique among current records |
| `display_name` | string | optional |
| `password_hash` | string | the Argon2id PHC string (`$argon2id$v=19$m=65536,t=3,p=4$…`), which includes its salt |
| `role` | string | `owner` or `member` |
| `session_epoch` | int | bumped to sign out everywhere |
| `invite_id` | string | the invite used to join; null for the owner |
| `disabled` | bool | set by the owner; disabled users can't sign in |

`invites/<invite_id>__<logged_at>.parquet`

| Column | Type | Notes |
|---|---|---|
| `invite_id` | string | UUID |
| `logged_at` | timestamp | |
| `code_sha256` | string | the code itself is never stored |
| `kind` | string | `join` or `reset` |
| `for_user_id` | string | `reset` only |
| `created_by` | string | the owner's `user_id` |
| `expires_at` | timestamp | 7 days after creation |
| `used_by`, `used_at` | string, timestamp | set by a later record when redeemed; a used invite can't be used again |
| `revoked` | bool | |

Usernames are unique "among current records": two people signing up with the same name at the same moment is
handled by the Space, which is a single process and checks under a lock before writing.

### Changes to `shots`

New nullable columns, appended to the `shots` schema:

| Column | Type | Notes |
|---|---|---|
| `user_id` | string | whose shot; null on old shots means the owner |
| `grinder_id` | string | `equipment_id` of the grinder |
| `machine_id` | string | |
| `basket_id` | string | |
| `bag_id` | string | gives `roast_date` when the shot doesn't have one |
| `source` | string | `app`, `mcp` or `issue` |
| `rev` | int | the phone's version counter for this shot (see How writes reach the lake); `equipment` and `bags` get it too |
| `time_source` | string | `timer`, `typed` or `copied` ("Save with 29 s?"); null on old shots. Copied times are left out of the dial-in model |

Old shot files don't have these columns. The lake is read with `union_by_name = true`, so they come back as null,
and nothing about an old shot changes. `Shot` in `models.py` gains the same optional fields; it has
`extra = "forbid"`, so this has to land before the app sends them. The MCP `log_shot` tool gains an
optional `grinder_name` argument that resolves to an `equipment_id` by name. (Not `grinder`: `shots.ALIASES`
already maps `grinder` to `grinder_setting`.)

A grind setting is now only comparable within one grinder, so `mart_dial_in` groups by user, coffee and
`grinder_id`, treating null as "the grinder used before equipment existed". `suggest_next` doesn't change: the app
doesn't use it, and it keeps serving the MCP server and the issue-form replies as it does today.

### dbt changes

- `stg_equipment`, `stg_bags`: latest record per id, without deleted ones, the same shape as `stg_shots`. Photos are
  not modelled in dbt; the Space reads them directly from the photo dataset.
- `dim_equipment`: current equipment.
- `fct_shot`: join `stg_bags` (`roast_date` falls back to the bag's), `dim_equipment` (grinder name, scale), and add
  `grind_position = (grinder_setting - scale_min) / (scale_max - scale_min)` so shots across grinders can at least
  be compared roughly.
- `mart_dial_in`: add `user_id` and `grinder_id` to the per-coffee grain. Every mart that touches shots is per user.
  The best recipe is one whole shot, picked by rating and then the latest `brewed_at`
  (`qualify row_number() over (... order by rating desc, brewed_at desc) = 1`). Today it takes `arg_max(col, rating)`
  column by column, so ties are arbitrary and the dose, yield and time can come from different shots.
- `mart_bag` (new): one row per bag with shot count, best shot, days off roast at first and last shot.
- `public_tables_are_facts_only.sql`: add `user_id`, `photo_id`, `bag_id`, `grinder_id`, `machine_id`,
  `basket_id`, `read_json` and `sha256` to the forbidden columns.
- The users dataset is never a dbt source.
- The weekly export adds `marts/fct_shot.parquet` (private lake only), which the Space uses as its starting copy.
  It keeps each shot's own columns and `logged_at` (#15), and records `snapshot_at`, when dbt started reading shots.

## Privacy

- Bag photos stay private. They go to a separate private dataset, `hf://datasets/jtouley/coffee-data-photos`, as
  Parquet files in Hugging Face's `Image` layout, one photo per file. The main lake is private too, but a separate dataset means a
  mistake in the public export can't reach a photo, and the photos could be deleted wholesale without touching the
  lake's history. Only the `photo_id` is in the main lake.
- Why Parquet and not plain JPEG files: a Parquet photo goes through the same `Lake.write` and `Lake.publish` as every
  other record, and the image and its metadata (who, when, size, hash) are one immutable file that can't drift apart.
  Plain JPEGs would need a second write path for binary files and a separate metadata table kept in step with them.
- EXIF is removed on the phone before upload, so GPS coordinates of the kitchen never leave it. The server
  re-encodes the JPEG as a second check before writing the bytes into Parquet.
- Photos are served only through the Space, to the user who took them, never by a direct HF URL. The Space reads
  the `image` column and returns the bytes.
- Invited people's data lives in the owner's private datasets, so the owner can read it. The sign-up screen says so
  in one sentence. The app itself never shows one user another's data.
- Leaving. Retracting records hides them in the app and in dbt, but the lake is append-only and HF keeps file
  history, so a retraction isn't erasure. If an invitee asks for their data to be erased, the owner deletes their
  files from the three datasets and squashes the history (`HfApi.super_squash_history`). It's a manual
  exception to append-only, and it's the reason accounts and photos live in their own datasets: erasing them doesn't
  rewrite the catalog's history.
- Passwords never leave the Space in plain text and are never logged. Request logging drops the body of the
  sign-in, sign-up and reset routes.
- The image goes to the Anthropic API to be read. Under standard API terms it isn't used for training; it is still
  a third party seeing a photo of a kitchen counter. The capture screen frames the bag tightly to keep the
  background small.
- Shots leave the public site. Today `site.py` renders "My shots: rating by brew ratio" and "Recent shots" from
  `fct_shot` on the GitHub Pages site; milestone 1 removes both sections and the scatter chart, and adds a test
  that the generated site contains no shot fields. Shots are then visible only in the app.
- The "Log a shot" issue form posts the shot and the reply as a GitHub issue, which is public if the repo is. #15
  kept it, since `suggest_next` still serves its replies. Whether it goes once the app works is open question 2.

## Cost

| Item | Estimate |
|---|---|
| Hugging Face Space, CPU basic | the hardware is free, but Docker Spaces need an HF PRO subscription (#15); PRO also covers the custom domain. The owner accepts the cold start after two idle days |
| Bag reads, Claude Opus 5.5 ($4 / $20 per million tokens in / out) | ~1,600 image tokens + ~400 prompt + ~300 out ≈ $0.014 per bag. One bag a week ≈ 75 cents a year |
| Same with Claude Haiku 5.5 ($0.10 / $0.50) | ≈ $0.00035 per bag, about 2 cents a year |
| The Opus/Haiku comparison | 30 photos × 3 runs × 2 models ≈ $1.30, almost all of it Opus; once per prompt change |
| Photo storage | ~300 KB per bag ≈ 15 MB a year, inside the free private storage |
| Shot and equipment files | a few KB each; negligible next to the weekly catalog snapshots |
| Invited users | their bag reads go through the owner's Anthropic key, so each account is capped at 10 reads a day (a Space setting). A handful of people logging a bag a week stays at a few dollars a year with Opus |

The app adds nothing to the weekly run's cost.

## Milestones

Each one is usable on its own and goes through the normal PR and CI process.

1. Data model: new lake tables and shot columns, `Shot` model fields, `stg_`/`dim_` models, the public-table
   test update, the `fct_shot` mart export. The MCP `log_shot` tool accepts `grinder_name`. Shot sections removed from
   the public site. No app yet; tests over a synthetic lake with old and new shots mixed.
2. App skeleton: FastAPI on a Space, the users dataset, owner bootstrap, sign-in, sessions and per-user scoping
   (with the cross-user tests), the deploy workflow, the in-memory shot cache. Today and Log shot with prefill, the timer and typed time, and the saved summary. Online only. Includes the approved
   concurrent-commit test.
3. Equipment, invites and the domain: the Setup screen, grinder-aware steppers; invites, sign-up and
   reset codes; `app.registry.coffee` in front of the Space and `registry.coffee` on the Pages site.
4. Bag capture: photo upload, EXIF stripping, the photo dataset, the Claude read, roaster-then-coffee matching,
   the match card and its fallbacks. The Opus/Haiku comparison on real bags decides the default model.
5. Coffee page: catalog facts, best recipe, chart, shot list, correct and retract.
6. Offline: service worker and outbox.
7. Polish: dark mode check in a dark kitchen, haptics (Android Chrome supports them), left-hand layout, the
   empty states.

Milestones 1 and 2 are the minimum that replaces the issue form at the machine, for the owner alone. Invitees can
join from milestone 3. Capture (4) is the reason for the
app, but it needs the bag and photo tables from 1 first.

## Later: a dial-in model

The app doesn't suggest grind changes. The current rule ("sour or fast, go finer") is a rule of thumb, not something
learned from these shots, and a confident-looking suggestion from it would be wrong often enough to mislead. The
data the app collects is what a better suggestion needs: grind, dose, yield, time, taste, rating, grinder and the
bag's roast date.

Once a coffee has enough shots on one grinder (about 20), fit a small per-bag model of time and taste against grind,
dose and days off roast; a regression with a term per bag is enough to start. Compare it with the rule of thumb on
held-out shots: for each held-out shot, which one better predicted the time and taste of the next one? Show a
suggestion in the app only when the model reports its confidence and beats the rule of thumb on that test, and say
how sure it is ("Try 11. Likely 26 to 30 s.") instead of stating a single answer.

`shots.suggest_next` stays as it is for the MCP server and the issue-form replies.

## Decisions (October 27)

| Question | Answer | What it changes |
|---|---|---|
| Phone | Android | Haptics work through `navigator.vibrate`. The PWA installs from Chrome and keeps its login like a normal tab. Web Bluetooth is available if a scale is wanted later. |
| Equipment | DF64 V2 (dial 0 to 90, lower is finer), Lelit Bianca, 18 g Pullman Filtration Basket (`basket17-pullm-v-v`) | Entered on first run, which asks for a grinder and a basket (#13); 18 g is the default first dose. The mockups show them. |
| Public site | No shots on it | Shot sections left `site.py` in milestone 1 (#13). The issue form stays for now (#15; open question 2). |
| Cold start | Fine for now | Free CPU hardware. Docker Spaces turned out to need PRO (#15); see open question 6. |
| Photos | Stay private, still Parquet, in Hugging Face's image layout | Separate private dataset, one photo per Parquet file, the `image` column as `struct<bytes, path>` with the `Image` feature metadata. One write path, and the photo and its metadata stay together in one immutable file; plain JPEGs would need a separate metadata table kept in step. |
| Grind suggestions | Deferred until there's a model trained on the owner's shots | The app describes each shot and suggests nothing. `shots.suggest_next` stays for the MCP and issue-form replies. See Later: a dial-in model. |
| Shot timer | Built in, but typing must also work | The time row has Start/Stop and Type side by side. |
| Rating default | Empty, with a way to say "same as last" | Taste and rating start empty; a "Same as last" chip fills both. |
| Other users | The owner plus people they invite; nobody else uses the owner's machine | Invite-only sign-up, per-user data, a `user_id` on every personal record. |
| Sign-in | A simple sign-up and login of our own, hashes stored in an HF dataset | Username and password, Argon2id with a pepper, in a private `coffee-data-users` dataset. Replaces Sign in with Hugging Face. |
| Concurrent-commit test | Yes | Runs in milestone 2, to a test path only. |
| Bag-read model | Compare Haiku and Opus | The comparison in "Where the bag photo is read" picks the default. |
| Bags for the comparison | Maybe some | The set grows from confirmed captures; Opus is the default until it has 20. |
| Domain | The owner will buy back `registry.coffee` | The launch page and catalog on `registry.coffee` (GitHub Pages, #19), the app on `app.registry.coffee` through HF's custom domain for Spaces. |

## Open questions for the owner

1. Is 0.5 the right stepper step on the DF64's dial, or do you move it in whole numbers?
2. Remove the "Log a shot" issue form once the app works (the plan's default), or keep it as a fallback?
3. How many people do you expect to invite? It decides whether the 10-reads-a-day cap is about right.
4. Name: the sign-in screen now says "registry.coffee", since that will be the address. Should the project (repo,
   public dataset, site title) be renamed to match, or keep "Coffee Data"?
5. `registry.coffee/app/` is the catalog dashboard (#19) and `app.registry.coffee` is the shot app. Keep both
   names, or move one (for example the catalog to `registry.coffee/catalog/`)?
6. Docker Spaces need HF PRO. Take PRO (which also covers the custom domain), or run the same image on a small
   container host instead?
