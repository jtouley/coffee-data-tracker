# Sign-up and invite UX: spec

Status: ready to build. Owner decisions recorded 2026-11-06. Scope: `/join`, owner setup on `/signin`, `/reset`,
Change password, `/invite` (links, reset links, the invite list), and the first visit after the app has scaled to
zero. Changes are subtle: same layout, type, colours and dock. What's new is rule text, inline feedback, busy
states, named invites, and a way through in-app browsers and cold starts.

## 1. What the tester hit and why

All line numbers are on `origin/main` at 1e7a280. The server-side claims were checked with a throwaway pytest
(since deleted) against the real `Accounts.join` / `/api/join`.

| # | What he saw | Root cause |
|---|---|---|
| 1 | Instagram's in-app browser showed only a Meta Pay sheet | Meta's in-app browser puts its own autofill sheet over web forms (contact and payment details; [Meta help][meta-autofill], [AdStatus test][adstatus]). The page can't turn it off (our `payment=()` in `app/web.py:43` only covers the Payment Request API). Nothing in `join.html` notices the in-app browser or offers a way out. |
| 2a | "Validation complained" about the username (his email) | There is **no** client-side content check. `app.js:1188-1218` only checks for empty fields. The server rejects `@` and `.` (`accounts.py:36`, checked at `:533-534`) with "Use 3 to 32 lower-case letters, digits, - or _." The join hint (`join.html:13`) doesn't give the length and doesn't say "not an email". Password managers often fill the email address as the username. |
| 2b | ...and about his 1Password password | **The server never checked it.** `join()` checks the username first and stops (`accounts.py:533-537`). Then `app.js:1209-1214` sets `aria-invalid` on **every** input after any 400, so the password field turned red too. Verified: 2,000 random 20-character passwords from 1Password's exact symbol set, and Memorable ones with every separator, all pass `check_password` (`accounts.py:168-175`). |
| 3 | Submit "not responsive", even after removing `@gmail` | (a) Removing `@gmail` from `name@gmail.com` leaves `name.com`, and the `.` still fails `accounts.py:36`. The reply is the **same text** in the same `role="alert"` (`join.html:24`), so nothing on screen changes and screen readers needn't say it again. (b) The hint sits under the terms paragraph, often below the fold with the keyboard up, and focus isn't moved. (c) While a request runs, `submit.disabled = true` (`app.js:1201`), but `.btn` has no disabled style (`app.css:80-82`, only `:active:not(:disabled)` at `:100`), so nothing shows. The fields stay editable, so edits made mid-request are silently dropped. (d) `post()` has no timeout (`app.js:1167-1176`). |
| 4 | Later: "code already used", so the account existed | `/api/join` isn't idempotent. A success that never reached the page (slow request, reload, app switch) uses the invite up (`accounts.py:538-541`), and a retry gets "That code isn't valid. It may have been used, replaced or expired." (`accounts.py:524`, verified 200 then 400). The slow part is `join()` holding `self.lock` across **two** Hugging Face commits (`accounts.py:529-541` → `_write`/`_write_invite` → `lake.publish`, which retries for up to 2+4+8+16 s each, `lake.py:784-849`). On top of that come the cold start (startup probe allows ~2 min, `cloud_run.tf:59-63`) and Cloud Run's 120 s timeout (`cloud_run.tf:15`). A 504 after the server committed looks like a failure. Also, `joinCode` strips the code from the URL (`app.js:1034`), so after a reload the code field is empty and he had to reopen the link. That check then reported the code as used. |
| 4b | (latent) | `await caches.delete("pages")` (`app.js:1205`) throws a `ReferenceError` where `caches` is undefined (some WebViews, or non-secure contexts). That happens **after** the account is created, and the catch shows "Couldn't reach the server" (`app.js:1208`). A retry then says the code is used. Guard it. |
| 5 | Invite screen (owner) | `act()` (`app.js:1081-1087`) has no busy state and no double-tap guard. Two taps make two links (the first one is left pending). Renew tapped twice errors after succeeding ("That link can't be renewed"). Each tap waits on a HF commit (seconds). |

## 2. Decisions

**Username: no email, no email field. Allow dots.** (Owner decision.)
- The privacy policy promises only "your username and a hash of your password" (`privacy.html:33`). The users table
  is append-only on Hugging Face with version history (`privacy.html:83-86`), so an email stored there is hard to
  truly delete. Usernames are shown to the owner (`invite.html:18`; `accounts.py:43`).
- Rule: `^[a-z0-9._-]{3,32}$` after trim and lower-case, so `first.last` and the tester's `name.com` edit both work.
  Typing `@` shows its own message and a one-tap suggestion: the part before `@`, cleaned up. Never strip characters
  as he types (better-accessibility `forms.md`: "Never block typing or filter characters").
- Capitals are accepted and shown as they'll be stored ("You'll sign in as maya"). The server already lower-cases
  (`accounts.py:528`). Existing usernames aren't affected: sign-in doesn't re-check the pattern (`accounts.py:406-431`).

**Password: 10 to 256 characters, any character, no composition rules.** (Owner decision on the minimum.)
- **The minimum stays 10 (`MIN_PASSWORD`, `accounts.py:37`); that's the owner's decision.** For reference, NIST SP
  800-63B-4 §3.1.1.2 asks for 15 when a password is the only factor ([NIST][nist]). The rest of this spec reads the
  minimum from the rules module, so a later change is one constant.
- Accept every character, spaces and Unicode included. No trimming. Count code points. Normalise to NFC before
  hashing (§3.1.1.2); on verify, try NFC and then the raw string, so any older non-NFC hash still matches. Maximum
  256 (NIST asks for at least 64).
- Keep the whole-password `COMMON` blocklist (`accounts.py:48-98`), "doesn't contain your username" and "at least 4
  different characters". Each one gets its own message instead of the shared "too easy to guess".
- Add `passwordrules="minlength: 10; maxlength: 256; allowed: unicode;"` to every new-password input (Safari's
  attribute, an open WHATWG proposal, [Apple][apple-rules], [whatwg#3518][whatwg]; 1Password ships a parser for it
  [[1P parser]][1p-parser]). Keep `autocomplete="new-password"` / `current-password` / `username`, allow paste, and
  keep Show (NIST §3.1.1.2, WCAG 3.3.8).

**1Password compatibility (verified from the generator page's own code, `xplatform-spg.min.js` on 1password.com):**
- Random: 8 to 100 characters (default 20). Letters are always on; Numbers and Symbols are toggles. The symbols are
  exactly `!#%)*+,-.:=>?@]^_}~`, and the look-alikes `0O1Il5S` are never used. The owner's sample `)]=_:!%` fits.
  Only lengths 8 and 9 fail, and only on length.
- Memorable: 3 to 15 words (default 4), from a list of 18,237 lower-case a–z words of 3 to 8 letters
  (`/txt/AgileWords.txt`). "Capitalize first letter" and "Use full words" (off means syllables) are toggles. The web
  generator uses hyphens. The library also supports `digits`, `spaces`, `underscores`, `periods` and `none`, and the
  apps add commas and "numbers and symbols" ([1Password community][1p-sep]). With any separator, 3 words come to at
  least 11 characters and pass. Only 3 short words with no separator (as few as 9) can fail, and the live length
  check shows that.

**Cold start: a waking page on the public site, not `min_instances = 1`.** Details in §7.

## 3. Rules: one source, the same words on both sides

New `app/rules.py` holds the rules as data: `id`, `field`, the text shown under the field, and the error message.
`check_username(name)` and `check_password(pw, username)` return **every** failing rule, not just the first.
`accounts.py` calls them everywhere it checks today (`:384-386`, `:533-537`, `:438`, `:567`). The page gets the same
data as `<script type="application/json" id="rules">{{ rules | tojson }}</script>`. JSON data blocks don't run, so
`web.py`'s `script-src 'self'` allows them (comment at `app/web.py:17-18`). `app.js` evaluates them. The common
list (47 entries) ships too. The server answers 400 with
`{"detail": [{"field": "password", "rule": "length", "message": "…"}]}`. `post()` keeps a plain-string `detail`
working and places field errors inline. A parity test (§9) holds the two sides to the same verdicts and words.

| Rule id | Shown under the field (✓/✗ list) | Error message (server and client, word for word) |
|---|---|---|
| `u.length` | 3 to 32 characters | Use 3 to 32 characters. |
| `u.chars` | Letters, digits, `.` `-` or `_` | Use only letters, digits, . - or _. |
| `u.email` | (only when `@` is typed) | A username isn't an email address. Try {suggestion}. |
| `u.taken` | (server only, on submit) | That username is taken. Try another. |
| `p.length` | 10 to 256 characters | Use at least 10 characters. / Use at most 256 characters. |
| `p.common` | Not a common password | That's one of the most common passwords. Pick another. |
| `p.username` | Doesn't contain your username | Leave your username out of the password. |
| `p.variety` | At least 4 different characters | Use at least 4 different characters. |

Hint lines (always visible under the label, `.fhint`): Username: "You'll sign in with it. Not your email."
Password: "Any characters, spaces and symbols included. A password manager's suggestion or a few unrelated words
work well." The rule list sits under the hint, as `<ul class="rules" id="password-rules">`, and the input's
`aria-describedby` points at the hint, the list and the error line.

## 4. Account recovery without email

How it works today, kept as is and made visible:
- The owner makes a reset code for one member: `Accounts.invite(by, kind="reset", for_user_id=…)` (`accounts.py:470`,
  via `POST /api/invites` with `kind: "reset"`, `main.py:435-440`). A new code revokes any earlier open one for that
  person (`accounts.py:479-482`). It lasts 7 days, works once, and only its SHA-256 is stored.
- The code is shown once, on the owner's screen. The owner sends it however they like. The member enters it on
  `/reset` with a new password (`accounts.py:544-576`), which signs them out everywhere. The owner recovers their
  own account with `OWNER_SETUP_CODE` (`accounts.py:551-559`).

Changes:
- **A reset link instead of a bare code:** `{app}/reset#{code}`. `/reset` reads the code from the hash, as join
  does (`app.js:1009-1037`). Reuse `joinCode`, with the status "Reset link for {username}, valid until {date}."
- **Sign-in copy** (`signin.html:31`): "Forgot your password? Ask the person who invited you for a reset link".
  It links to `/reset`, whose intro becomes: "Ask the person who invited you for a reset link. It works once, and
  signs you out everywhere else."
- **Owner's member list** (`invite.html:20`): the action reads "Make reset link".
  - First tap: `confirmTap`, "Tap again to make a reset link for {username}. Any earlier one stops working."
  - While it runs: busy, "Making a reset link…".
  - Then the codebox, labelled "Reset link for {username}", with Share link / Copy link → "Copied", and the note
    "Send it to {username}. It works once, until {date}, and signs them out everywhere."
- **Out of scope: self-service email recovery.** It would mean storing email addresses, which breaks the privacy
  promise of "your username and a hash of your password" (`privacy.html:33`). That policy would have to change
  first.

## 5. Named invites (owner requirement)

- **What:** an optional **For** note on each invite, so the owner can track links: free text, at most 60 characters
  after trimming and collapsing whitespace, with no control characters. Owner only. **Never** sent to `/join`,
  `/api/invites/check`, the invitee, or other members.
- **Stored:** a new nullable `for_label` field on `Invite` (`accounts.py:119-134`), plus `created_at`. The
  in-memory map keeps only the latest `logged_at`, so creation time has to be stored. Add both columns to
  `SCHEMAS["invites"]` (`lake.py:444-457`). Old files read as NULL: `latest` uses `union_by_name` (`lake.py:704`),
  and `publish` refreshes a schema file that lacks columns (`lake.py:792-797`). For old rows, the loader
  (`accounts.py:272-286`) falls back to `expires_at − 7 days` for `created_at`. `_write_invite` (`accounts.py:330-350`)
  writes both. Reset codes get no note; they already name their member.
- **Making one:** above "Make an invite link", add an input labelled "For (optional)", with the hint "Only you see
  this. Up to 60 characters." and a counter "{n} of 60" (`form-design`: always show the limit). `POST /api/invites`
  takes `for_label`. Renewing keeps the note (`accounts.py:503-512`).
- **The list** replaces "Links not used yet" (`invite.html:27-36`). It's a new `Accounts.invite_log()` covering every
  join invite, newest first. Each row reads "{label or "Invite"}" with its status in `<em>` and a date line:

| Status | Line under the name |
|---|---|
| open | Made {date} · expires {date} |
| used | Used by {username} on {date} · made {date} |
| expired | Expired {date} · made {date} |
| revoked | Revoked {date} · made {date} |

  Open rows keep "Send a new link" and "Revoke invite". Every row gets "Edit note" and "Delete invite"; used rows
  get only "Edit note". Expired and revoked rows older than 90 days are collapsed under "Older links ({n})".
- **Edit and clear:** "Edit note" opens the input in place (Save / Cancel, busy state "Saving…"), calling
  `POST /api/invites/{id}/label` with `{for_label}`. An empty value clears the note. The lake is append-only, so an
  edit writes the new version **and deletes the record's earlier files in the same commit** (`CommitOperationDelete`,
  as `erase.py` does). The old note is then gone from the dataset, apart from version history, which the privacy
  page already covers.
- **Delete:** `POST /api/invites/{id}/delete`, behind `confirmTap` ("Tap again to delete this invite and its note").
  It's allowed for revoked or expired invites (an open one is revoked first) and removes every file of the record.
  A used invite can't be deleted on its own: it records how the account was made, and goes with the account
  (`erase.py:28` already erases invites by `used_by`). Clearing its note is the way to drop the name.
- **Privacy page** (`privacy.html`, "What the app collects", after the account item). Add this line and bump
  `LEGAL_UPDATED` (`site.py:73`):
  `<li><strong>Invite notes:</strong> the owner may note who an invite link is for. The note is kept only with that invite, and is deleted with it.</li>`

## 6. Target behaviour by state

Guidance used: Owl-Listener `form-design` (labels, errors under the field, a check mark where correctness isn't
obvious), `error-handling-ux` (what happened, why, what to do; keep the input), `loading-states` (100 ms–1 s subtle,
1–10 s clear, over 10 s explain), `feedback-patterns` (acknowledge every action), and `accessibility-audit`;
jakubkrehel `better-accessibility` (`forms.md` "Submit behavior", `semantics-and-aria.md` "Disabled states", live
regions) and `interface-review` (every new component has its idle, loading, error, disabled and narrow states).
Paths are in `SKILLS-INDEX.md`.

**Join form (`join.html`). Owner setup and reset reuse the same pieces.**

| State | What shows |
|---|---|
| Idle | Today's form. Each field has its hint and, for username and password, a rule list with neutral bullets (`○`, `--ink-3`). Button "Create account". |
| Typing | Each rule turns ✓ (`--balanced`) as it's met. Unmet rules stay neutral while typing. Red only appears on blur or submit (`form-design`: validate on blur), so the live checks read as progress. Nothing is announced per keystroke. Capitals in the username: the hint becomes "You'll sign in as {lower}." Typing `@` shows the `u.email` line at once, with a text button "Use {suggestion}". The suggestion is the part before `@`, lower-cased, with characters outside `[a-z0-9._-]` removed, cut to 32 characters, and only offered when it passes. |
| Invalid (blur or submit) | Unmet rules turn ✗ (`--danger`), with visually-hidden "met" / "not met" text. One error line under the field (`<p class="ferr" id="password-error">`) shows the first failing rule's message, and the field gets `aria-invalid="true"`. On submit, focus moves to the first invalid field. Only fields that fail are marked. The form-level `[data-hint]` is kept for errors that belong to no field, and is cleared before each new message so a repeat is announced again. |
| Submitting (0–4 s) | The button gets `aria-disabled="true"` (not `disabled`, which drops focus), a 14 px spinner before the label, and the label "Creating account…". Repeat taps do nothing. The form gets `aria-busy="true"`; the inputs become `readonly`. The style is `.btn[aria-disabled="true"], .btn:disabled { opacity: .72 }`. With reduced motion the spinner is a static `…`. |
| Slow (> 4 s) | A status line under the button (`role="status"`, in the DOM from the start): "Still working. The app may be waking up, which can take a minute or two. Keep this page open." At 45 s: "Still working. Saving can take a little longer when the app has just woken up." |
| Lost reply | The fetch times out at 130 s, or the network fails, or a 502/503/504 comes back. The page then resends the **same** body up to 3 times (2 s, 5 s, 10 s apart). That's safe: join is idempotent (§8). If all of them fail: "Couldn't reach the app. Nothing you typed is lost. Try again in a moment." The button reads "Try again". |
| Success | Button: "Account created". `globalThis.caches?.delete("pages")`, then `location.replace("/")` with the notice "Welcome, {username}." |
| Error, a field / other | The server's message under its field, or in the form hint: the server's text, or "Something went wrong on our side. Try again." for a 500. |
| Code already used | `#code-h`: "This invite has already been used. If you made an account with it, sign in. If not, ask whoever sent it for a new link." The words "sign in" link to `/signin`. |
| Code from link | "Valid until {date}." as today. Keep the code in `sessionStorage['join-code']` before `replaceState` (`app.js:1034`), so a reload keeps it. Clear it on success. |

**In-app browser banner (join, reset, and the waking page).** Detect it on the server from the User-Agent in
`join_page` (`main.py:352-356`): `/\bInstagram\b|FBAN\/|FBAV\/|FB_IAB\/|FBIOS|MessengerForiOS/` (tokens: Instagram;
Facebook FBAN/FBAV on iOS, FB_IAB on Android [[detect-inapp]][inapp-ua], [[SO]][so-fb]). Render a `.notice` at the
top of `main`; it needs no JS to show:
> **Opening from Instagram?** Its browser can cover this form and won't keep your sign-in. Tap ••• and choose
> *Open in external browser*, or copy the link and paste it into Safari or Chrome. [Copy link]

"Copy link" rebuilds the link from the code field. If the clipboard is refused, it shows the link in an
`<input readonly>`: "Press and hold to copy." No `x-safari-https://` or `intent://`: the first is reported broken
on iOS 18.5 ([gist][xsafari]), and `intent://` takes over the `#` the code lives in.

**Invite screen (`invite.html`, `invites()` in `app.js`).** The same busy pattern covers every action: Make an
invite link, Make reset link, Send a new link, Revoke, Edit note, Delete, Turn off/on.

| State | What shows |
|---|---|
| Busy (0–4 s) | The tapped button is `aria-disabled`, with a spinner and its own label: "Making a link…", "Making a reset link…", "Making a new link…", "Revoking…", "Saving…", "Deleting…", "Turning off…" / "Turning on…". Repeat taps are ignored. |
| Slow (> 4 s) | `[data-hint]` neutral, `role="status"`: "Still saving. This takes a few seconds when the app has just woken up." |
| Ready | The codebox appears, and focus moves to its label (`tabindex="-1"`). Status: "Invite link ready. It's shown only once." The button becomes "Share link" / "Copy link"; "Copied" for 2 s. A filled-in For note shows on the codebox label: "Invite for {label}". |
| Error | `[data-hint]` with the server text, and the button goes back to idle. Note too long: "Keep the note to 60 characters." |

Links point to `{site}/join/#{code}` (§7) instead of `location.origin` (`app.js:1056`), with
`data-site="{{ site.site }}"` on the dock. Reset links stay on the app, because the member already has it open.

## 7. Cold start

**Measure first (S).** Log `logs.event("ready", seconds=…)` at the end of `create_app`, timed from process start.
Pull a week from `run.googleapis.com/container/startup_latencies`, and from the request log's
`httpRequest.latency` for the first request after each `started` event. Record p50 and p95 in `RUNNING.md`. From
here: one warm `/health` took 0.56 s and `/join` 0.65 s. No cold one was seen, and none was forced. Start-up reads
the lake from HF before the port opens (`cloud_run.tf:59`), and the 15-minute uptime check wakes it from zero
(`alerts.tf:63`).

**Waking page on the public site (M).** GitHub Pages is always up. Add `src/coffeedata/templates/join.html`, written
by `site.py` to `/join/index.html` next to `privacy`/`terms` (`site.py:403-405`). Its inline script is allowed by
hash in `with_csp` (`site.py:130-149`), and `connect-src` adds the app's origin for this page only. It uses the site's
brand styles, in the same quiet, centred style:
- `<h1>` **Brewing…**, and under it one status line, `<p role="status" aria-live="polite">`. The line steps every
  4 s through "Warming up the machine", "Grinding fresh" and "Pulling your shot", then holds on the last.
  `aria-live` announces each new step once, which comes to three announcements in all.
- A three-dot "…" after the step fades in and out. Under `prefers-reduced-motion: reduce` there's no animation, and
  the steps still change as text.
- When `/health` answers: the heading becomes **Ready**, the status reads "Ready. Opening the app…", and then
  `location.replace("{app}/join#{code}")` after 600 ms (0 ms with reduced motion).
- **Error** after 150 s: the heading becomes **Still brewing**, the status (`role="alert"`) reads "The machine's
  taking longer than usual. Try again in a minute.", and a button reads "Try again". The button restarts the polling
  and moves focus to the heading.
- Polling: `GET {app}/health`, `fetch(…, {mode: "cors", cache: "no-store"})` with a 10 s `AbortController`, every
  2 s. `/health` (`main.py:275-278`) adds `Access-Control-Allow-Origin: {SITE_URL}` and nothing else. The code stays
  in the fragment and is never sent anywhere.
- It shows the in-app browser banner (UA test in JS). Copy link copies the site link.

Old `app.registry.coffee/join#…` links keep working. The privacy policy needs nothing more for this page: it's part
of using the app.

**`min_instances = 1`: not now.** An idle minimum instance is billed at about $0.0000025 per vCPU-second and per
GiB-second ([Cloud Run pricing][run-pricing]). For 1 vCPU and 1 GiB that's about **$13 a month**, against a bill now
inside the free tier (`PLAN.md:591`). Revisit if the measured p95 is over 30 s and members, not just invitees, meet
it often.

## 8. Idempotent submit

- **Client:** one request in flight per form (the `aria-disabled` guard). Retries resend the identical body.
- **Join (server):** if the code matches a *join* invite that is used, and `used_by` is an account whose username
  equals the submitted one and whose password verifies, return that account. The route sets the cookie and answers
  200. This needs the code *and* the password, so it's no weaker than sign-in, and it counts against the sign-in
  limiter (`accounts.py:410-418`). Anything else on a used join code gives the "already used" message (code 409,
  rule `c.used`).
- **One commit:** today the account is published before the invite is marked used (`accounts.py:538-541`). If the
  second publish fails, the code stays open and can make a second account. Write both rows, then call
  `lake.publish` **once**. That halves the wait and makes the pair atomic.
- **Invites:** the client guard is enough. A stray link shows in the list and can be revoked or deleted. The
  [IETF Idempotency-Key draft][idem] is the pattern if that ever changes.

## 9. Test plan

- **Unit (`tests/test_rules.py`, `tests/test_invites.py`):**
  - every rule id and message; `@`, `.`, capitals, 2/3/32/33 characters;
  - 9/10/256/257-character passwords;
  - every failing field comes back at once;
  - a repeat join with the same details gives 200 and a cookie; a different password gives 409;
  - one publish per join;
  - NFC: composed and decomposed `é` verify the same.
- **Named invites:**
  - a note of 60 characters is accepted, and 61 is refused with the message;
  - the note never appears in `/api/invites/check`, `/join`, or the invitee's pages (search the HTML and JSON for it);
  - only the owner can set it (a member gets 404/400);
  - it survives a restart and a renew;
  - the list shows each status (open, used by `maya`, expired, revoked) with its dates;
  - after an edit, a clear or a delete, no file of the record in the users lake contains the old note;
  - old invite files without the columns still load, with `created_at` filled from the fallback;
  - `erase.py` removes an invite whose note was set.
- **Reset links:**
  - `/reset#CODE` fills the code;
  - "Make reset link" asks for a second tap, revokes the earlier code, and shows Copy link;
  - the sign-in page shows the new copy.
- **1Password fixtures (`tests/fixtures/passwords/`):** a seeded generator script and its committed output.
  - 60 Random passwords from `ABCDEFGHJKLMNPQRTUVWXYZabcdefghijkmnopqrstuvwxyz2346789` and `!#%)*+,-.:=>?@]^_}~`,
    lengths 8, 9, 10, 20, 64 and 100, each toggle combination.
  - 60 Memorable ones with 3, 4 and 15 words, each separator (`-`, space, `.`, `,`, `_`, digit, digit-or-symbol,
    none), capitalised or not, from a 200-word sample of the list.
  - Safari (`abcdef-ghijkl-mnopqr`), Chrome (15 characters), a passphrase with spaces, and emoji.
  - Expected: all ≥ 10 pass, and shorter ones fail with `p.length` only.
- **Parity:** `node tests/browser/rules_parity.js` runs `app.js`'s checker over the same fixtures and usernames;
  pytest compares verdicts **and messages** with `rules.py`.
- **End to end (Playwright, `tests/browser/app_flow.js`):** at widths 320, 360, 390 and 430, plus a height of 640
  with the error line visible above the dock.
  - Live ✓, red on blur, and the `@` suggestion.
  - A pasted 1Password password passes.
  - Slow server: `create_app` over a fake users lake whose `publish` sleeps 6 s. Check `aria-disabled`, "Creating
    account…", read-only inputs, the 4 s text, and that five taps send one `POST /api/join`.
  - Lost reply: `page.route` lets the first `/api/join` reach the server, then aborts the reply. The page ends
    signed in.
  - `delete window.caches` (via `addInitScript`): join still lands on `/`.
  - Reload after `/join#CODE`: the code is still filled in.
  - Invite screen: a double tap makes one link; a note is shown; edit, clear and delete each show their busy state.
  - axe on every state above, light and dark.
- **In-app user agents:** `…Mobile/15E148 Instagram 390.0.0.27.105 (iPhone15,2; iOS 18_5; …)` and
  `…[FBAN/FBIOS;FBAV/480.0.0.40.104;…]` show the banner; plain Safari and Chrome don't.
- **Waking page:** a server whose `/health` waits 20 s. Check:
  - steps at 0, 4 and 8 s, three polite announcements, and "Ready" before the redirect;
  - no animation under reduced motion;
  - the error text at 150 s, and Try again restarting the polling;
  - `#code` kept through the redirect;
  - the CSP allowing only that `connect-src`.
- **By hand, once:** iPhone Safari with 1Password, the Instagram app, Android Chrome, and VoiceOver through join and
  reset.

## 10. Tasks, in order

1. **S** Quick fixes: guard `caches` (`app.js:1205`, `:1241`), mark only the failing field, clear-then-set the hint,
   `:disabled` / `aria-disabled` styles, keep the code in `sessionStorage`.
2. **M** `app/rules.py`: every rule and message, with the 10 minimum. `accounts.py` uses it. Field-tagged 400s, dots
   in usernames, NFC. Unit tests.
3. **M** Idempotent join, `c.used` on check and join, one publish per join. Tests.
4. **M** Templates: hints, rule lists, the rules JSON block, `passwordrules` (minlength 10), aria wiring, error
   lines. On join, owner setup, reset and Change password.
5. **L** `app.js`: live checks, the `@` suggestion, the submit states (busy, slow, retry, timeout), focus, and
   `post()` reading field details. CSS: rule list, spinner, `.ferr`.
6. **S** Recovery without email: reset links (`/reset#code`), the sign-in and reset copy, "Make reset link" with its
   confirm, busy and copy states.
7. **S–M** Named invites: `for_label` and `created_at` on `Invite` and the schema, the For input, `invite_log()`
   and the list with statuses, edit, clear and delete (removing old files), the privacy line and `LEGAL_UPDATED`.
   Tests.
8. **S–M** Invite screen busy states and focus for every action; the invite link points at the site.
9. **S** In-app browser banner (UA helper in `web.py`, a template block, Copy link).
10. **S** Measure cold start (`ready` event, query in `RUNNING.md`).
11. **M** The site's `/join/` "Brewing…" waking page, CORS on `/health`, site CSP for that page.
12. **L** Fixtures, parity test, Playwright cases, in-app user agents, slow and lost-reply servers, waking-page tests.
13. **S** `DESIGN.md` copy table, `RUNNING.md` cold-start numbers. Decide on `min_instances` once there's data.

[meta-autofill]: https://www.meta.com/help/meta-pay/1896636927418202/
[adstatus]: https://adstatus.app/blog/meta-in-app-browser-autofill-lead-gen-2026
[nist]: https://pages.nist.gov/800-63-4/sp800-63b.html
[apple-rules]: https://developer.apple.com/password-rules/
[whatwg]: https://github.com/whatwg/html/issues/3518
[1p-parser]: https://github.com/1Password/password-rules-parser
[1p-sep]: https://www.1password.community/discussions/1password/feature-request-add-numbers-and-symbols-to-memorable-password-generator-recipe/75231
[inapp-ua]: https://github.com/f2etw/detect-inapp/issues/52
[so-fb]: https://stackoverflow.com/questions/31569518/how-to-detect-facebook-in-app-browser
[xsafari]: https://gist.github.com/felquis/a08ee196747f71689dcb
[run-pricing]: https://cloud.google.com/run/pricing
[idem]: https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/
