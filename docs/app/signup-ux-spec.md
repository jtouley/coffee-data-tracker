# Sign-up and invite UX: spec

Status: ready to build (2026-11-06). Scope: `/join`, owner setup on `/signin`, `/reset`, Change password, `/invite`,
and the first visit after the app has scaled to zero. Changes are subtle: same layout, type, colours and dock. What's
new is rule text, inline feedback, busy states and a way through in-app browsers and cold starts.

## 1. What the tester hit and why

All line numbers are on `origin/main` at 1e7a280. The server-side claims were checked with a throwaway pytest
(since deleted) against the real `Accounts.join` / `/api/join`.

| # | What he saw | Root cause |
|---|---|---|
| 1 | Instagram's in-app browser showed only a Meta Pay sheet | Meta's in-app browser puts its own autofill sheet over web forms (contact and payment details; [Meta help][meta-autofill], [AdStatus test][adstatus]). The page can't turn it off (our `payment=()` in `app/web.py:43` only covers the Payment Request API). Nothing in `join.html` notices the in-app browser or offers a way out. |
| 2a | "Validation complained" about the username (his email) | There is **no** client-side content check. `app.js:1188-1218` only checks for empty fields. The server rejects `@` and `.` (`accounts.py:36`, checked at `:533-534`) with "Use 3 to 32 lower-case letters, digits, - or _." The join hint (`join.html:13`) doesn't give the length and doesn't say "not an email". Password managers (iCloud Keychain, 1Password) often fill the email address as the username. |
| 2b | ...and about his 1Password password | **The server never checked it.** `join()` checks the username first and stops (`accounts.py:533-537`). Then `app.js:1209-1214` sets `aria-invalid` on **every** input after any 400, so the password field turned red too. Verified: 2,000 random 20-character passwords from 1Password's exact symbol set, and Memorable ones with every separator, all pass `check_password` (`accounts.py:168-175`). |
| 3 | Submit "not responsive", even after removing `@gmail` | (a) Removing `@gmail` from `name@gmail.com` leaves `name.com`, and the `.` still fails `accounts.py:36`. The reply is the **same text** in the same `role="alert"` (`join.html:24`), so nothing on screen changes and screen readers needn't say it again. (b) The hint sits under the terms paragraph, often below the fold with the keyboard up, and focus isn't moved. (c) While a request runs, `submit.disabled = true` (`app.js:1201`), but `.btn` has no disabled style (`app.css:80-82`, only `:active:not(:disabled)` at `:100`), so nothing shows. The fields stay editable, so edits made mid-request are silently dropped. (d) `post()` has no timeout (`app.js:1167-1176`). |
| 4 | Later: "code already used", so the account existed | `/api/join` isn't idempotent. A success that never reached the page (slow request, reload, app switch) uses the invite up (`accounts.py:538-541`), and a retry gets "That code isn't valid. It may have been used, replaced or expired." (`accounts.py:524`, verified 200 then 400). The slow part is `join()` holding `self.lock` across **two** Hugging Face commits (`accounts.py:529-541` → `_write`/`_write_invite` → `lake.publish`, which retries for up to 2+4+8+16 s each, `lake.py:784-849`). On top of that come the cold start (startup probe allows ~2 min, `cloud_run.tf:59-63`) and Cloud Run's 120 s timeout (`cloud_run.tf:15`). A 504 after the server committed looks like a failure. Also, `joinCode` strips the code from the URL (`app.js:1034`), so after a reload the code field is empty and he had to reopen the link. That check then reported the code as used. |
| 4b | (latent) | `await caches.delete("pages")` (`app.js:1205`) throws a `ReferenceError` where `caches` is undefined (some WebViews, or non-secure contexts). That happens **after** the account is created, and the catch shows "Couldn't reach the server" (`app.js:1208`). A retry then says the code is used. Guard it. |
| 5 | Invite screen (owner) | `act()` (`app.js:1081-1087`) has no busy state and no double-tap guard. Two taps make two links (the first one is left pending). Renew tapped twice errors after succeeding ("That link can't be renewed"). Each tap waits on a HF commit (seconds). |

## 2. Decisions

**Username: no email, no email field. Allow dots.** Recommendation:
- Keep usernames. The privacy policy promises only "your username and a hash of your password"
  (`src/coffeedata/templates/privacy.html:33`). The users table is append-only on Hugging Face with version history
  (`privacy.html:83-86`), so an email stored there is hard to truly delete. Usernames are shown to others
  (`accounts.py:43`, `invite.html:18`). The app sends no mail, and resets go through owner codes, so an email would
  be a liability with no feature behind it.
- Rule: `^[a-z0-9._-]{3,32}$` after trim and lower-case. Adding `.` means `first.last` works, and so does the tester's
  `name.com` edit. `@` gets its own message and a one-tap suggestion: the part before `@`, cleaned up. Never strip
  characters as he types (better-accessibility `forms.md`: "Never block typing or filter characters").
- Capitals are accepted and shown as they'll be stored ("You'll sign in as maya"). The server already lower-cases
  (`accounts.py:528`). Existing usernames aren't affected: sign-in doesn't re-check the pattern (`accounts.py:406-431`).

**Password: length, a blocklist, and nothing about composition.** Recommendation:
- **15 to 256 characters** for every *new* password (join, owner setup, reset, change). NIST SP 800-63B-4 §3.1.1.2
  requires at least 15 for a password used alone and asks for a maximum of at least 64 ([NIST][nist]). Existing
  passwords keep working. The defaults all pass: 1Password Random is 20 characters, Memorable is 4 words with
  hyphens (≥ 15 because words are 3+ letters), Safari's strong password is 20, Chrome's is 15. *Owner's call:* to keep
  10, change `MIN_PASSWORD` only; everything below reads it from the one rules module.
- Accept every character, spaces and Unicode included. No trimming. Count code points. Normalise to NFC before
  hashing (§3.1.1.2); on verify, try NFC and then the raw string, so any older non-NFC hash still matches.
- Keep the whole-password `COMMON` blocklist (`accounts.py:48-98`), "doesn't contain your username" and "at least 4
  different characters". Each one gets its own message instead of the shared "too easy to guess".
- Add `passwordrules="minlength: 15; maxlength: 256; allowed: unicode;"` to every new-password input (Safari's
  attribute, an open WHATWG proposal, [Apple][apple-rules], [whatwg#3518][whatwg]; 1Password ships a parser for it
  [[1P parser]][1p-parser]). Keep `autocomplete="new-password"` / `current-password` / `username`, allow paste, and
  keep Show (NIST §3.1.1.2, WCAG 3.3.8).

**1Password compatibility (verified from the generator page's own code, `xplatform-spg.min.js` on 1password.com):**
- Random: 8 to 100 characters (default 20). Letters are always on; Numbers and Symbols are toggles. The symbols are
  exactly `!#%)*+,-.:=>?@]^_}~`, and the look-alikes `0O1Il5S` are never used. The owner's sample `)]=_:!%` fits.
- Memorable: 3 to 15 words (default 4), from a list of 18,237 lower-case a–z words of 3 to 8 letters
  (`/txt/AgileWords.txt`). "Capitalize first letter" and "Use full words" (off means syllables) are toggles. The web
  generator uses hyphens. The library also supports `digits`, `spaces`, `underscores`, `periods` and `none`, and the
  apps add commas and "numbers and symbols" ([1Password community][1p-sep]). The only Memorable output the server
  rejects is a short one: 3 words is 11 to 26 characters, and with no separator as few as 9. The live length check
  shows that.

**Cold start: a waking page on the public site, not `min_instances = 1`.** Details in §5.

## 3. Rules: one source, the same words on both sides

New `app/rules.py` holds the rules as data: `id`, `field`, the text shown under the field, and the error message.
`check_username(name)` and `check_password(pw, username)` return **every** failing rule, not just the first.
`accounts.py` calls them everywhere it checks today (`:384-386`, `:533-537`, `:438`, `:567`). The page gets the same
data as `<script type="application/json" id="rules">{{ rules | tojson }}</script>`. JSON data blocks don't run, so
`web.py`'s `script-src 'self'` allows them (comment at `app/web.py:17-18`). `app.js` evaluates them. The common
list (47 entries) ships too. The server answers 400 with
`{"detail": [{"field": "password", "rule": "length", "message": "…"}]}`. `post()` keeps a plain-string `detail`
working and places field errors inline. A parity test (§7) holds the two sides to the same verdicts and words.

| Rule id | Shown under the field (✓/✗ list) | Error message (server and client, word for word) |
|---|---|---|
| `u.length` | 3 to 32 characters | Use 3 to 32 characters. |
| `u.chars` | Letters, digits, `.` `-` or `_` | Use only letters, digits, . - or _. |
| `u.email` | (only when `@` is typed) | A username isn't an email address. Try {suggestion}. |
| `u.taken` | (server only, on submit) | That username is taken. Try another. |
| `p.length` | 15 to 256 characters | Use at least 15 characters. / Use at most 256 characters. |
| `p.common` | Not a common password | That's one of the most common passwords. Pick another. |
| `p.username` | Doesn't contain your username | Leave your username out of the password. |
| `p.variety` | At least 4 different characters | Use at least 4 different characters. |

Hint lines (always visible under the label, `.fhint`): Username: "You'll sign in with it. Not your email."
Password: "Any characters, spaces and symbols included. A password manager's suggestion or a few unrelated words
work well." The rule list sits under the hint, as `<ul class="rules" id="password-rules">`, and the input's
`aria-describedby` points at the hint, the list and the error line.

## 4. Target behaviour by state

Guidance used: Owl-Listener `form-design` (labels, errors under the field, a check mark where correctness isn't
obvious), `error-handling-ux` (what happened, why, what to do; keep the input), `loading-states` (100 ms–1 s subtle,
1–10 s clear, over 10 s explain and offer a way out), `feedback-patterns` (acknowledge every action), and
`accessibility-audit`; jakubkrehel `better-accessibility` (`forms.md` "Submit behavior", `semantics-and-aria.md`
"Disabled states", live regions) and `interface-review` (every new component has its idle, loading, error, disabled
and narrow states). Paths are in `SKILLS-INDEX.md`.

**Join form (`join.html`). Owner setup and reset reuse the same pieces.**

| State | What shows |
|---|---|
| Idle | Today's form. Each field has its hint and, for username and password, a rule list with neutral bullets (`○`, `--ink-3`). Button "Create account". |
| Typing | Each rule turns ✓ (`--balanced`) as it's met. Unmet rules stay neutral while typing. Red only appears on blur or submit (`form-design`: validate on blur), so the live checks read as progress, not scolding. Nothing is announced per keystroke. The list is a description, not a live region. Capitals in the username: the hint becomes "You'll sign in as {lower}." Typing `@` shows the `u.email` line at once, with a text button "Use {suggestion}". The suggestion is the part before `@`, lower-cased, with characters outside `[a-z0-9._-]` removed, cut to 32 characters, and only offered when it passes. |
| Invalid (blur or submit) | Unmet rules turn ✗ (`--danger`) and each mark carries visually-hidden "met" / "not met" text, so colour isn't the only cue. One error line under the field (`<p class="ferr" id="password-error">`) shows the first failing rule's message, and the field gets `aria-invalid="true"`. On submit, focus moves to the first invalid field. Only fields that fail are marked (fixes `app.js:1209-1214`). The form-level `[data-hint]` is kept for errors that belong to no field, and is cleared before each new message so a repeat is announced again. |
| Submitting (0–4 s) | The button gets `aria-disabled="true"` (not `disabled`, which drops focus), a 14 px spinner before the label, and the label "Creating account…". Repeat taps do nothing. The form gets `aria-busy="true"` and the inputs become `readonly`, so a mid-request edit can't be silently ignored. The style is `.btn[aria-disabled="true"] { opacity: .72 }`, plus the same for `:disabled`, which also fixes every other screen. With reduced motion the spinner is a static `…`. |
| Slow (> 4 s) | A status line under the button (`role="status"`, in the DOM from the start): "Still working. The app may be waking up, which can take a minute or two. Keep this page open." At 45 s: "Still working. Saving can take a little longer when the app has just woken up." |
| Lost reply | The fetch times out at 130 s (more than Cloud Run's 120 s), or the network fails, or a 502/503/504 comes back. The page then resends the **same** body automatically, up to 3 times (2 s, 5 s, 10 s apart). That's safe because join is idempotent (§6). If all of them fail: "Couldn't reach the app. Nothing you typed is lost. Try again in a moment." The button goes back to "Try again". |
| Success | Button: "Account created". The cookie is already set. `caches?.delete` is guarded (`globalThis.caches?.delete("pages")`), then `location.replace("/")`, with the notice "Welcome, {username}." |
| Error, a field | As under Invalid, with the server's message placed under its field. |
| Error, other | Form hint (`role="alert"`): the server's text, or "Something went wrong on our side. Try again." for a 500. |
| Code already used | Code status (`#code-h`, once the code is checked or on submit): "This invite has already been used. If you made an account with it, sign in. If not, ask whoever sent it for a new link." The words "sign in" link to `/signin`. A code that never existed, or was revoked or expired, keeps today's message. |
| Code from link | "Valid until {date}." as today. Store the code in `sessionStorage['join-code']` before `replaceState` (`app.js:1034`), so a reload keeps it. Clear it on success. |
| In-app browser | See below. The form still works under the banner. |

**In-app browser banner (join, and the site's waking page).** Detect it on the server from the User-Agent in
`join_page` (`main.py:352-356`): `/\bInstagram\b|FBAN\/|FBAV\/|FB_IAB\/|FBIOS|MessengerForiOS/` (tokens: Instagram;
Facebook FBAN/FBAV on iOS, FB_IAB on Android [[detect-inapp]][inapp-ua], [[SO]][so-fb]). Then render a `.notice` at
the top of `main`, so no JS is needed to see it:
> **Opening from Instagram?** Its browser can cover this form and won't keep your sign-in. Tap ••• and choose
> *Open in external browser*, or copy the link and paste it into Safari or Chrome.
> [Copy link]

"Copy link" builds `{origin}/join#{code}` from the code field. The hash is gone from the URL by then. If the
clipboard is refused, it shows the link in a selectable `<input readonly>`: "Press and hold to copy." No
`x-safari-https://` or `intent://` tricks: they're reported broken on iOS 18.5 ([gist][xsafari]), and `intent://`
takes over the `#` the code lives in.

**Invite screen (`invite.html`, `invites()` in `app.js`).**

| State | What shows |
|---|---|
| Idle | As today: "Make an invite link". |
| Making (0–4 s) | The button is `aria-disabled`, with a spinner and "Making a link…". Repeat taps are ignored. Row buttons (Reset, Send a new link, Revoke, Turn off) get the same treatment with their own text: "Making a reset code…", "Making a new link…", "Revoking…", "Turning off…" / "Turning on…". |
| Slow (> 4 s) | `[data-hint]` neutral, `role="status"`: "Still saving. This takes a few seconds when the app has just woken up." |
| Ready | The codebox appears. Focus moves to its label (`tabindex="-1"`), and the status says "Invite link ready. It's shown only once." The button becomes "Share link" / "Copy link" as today. After a copy it reads "Copied" for 2 s and then goes back to "Copy link". |
| Error | `[data-hint]` with the server text. The button goes back to idle. Renew can't fail after succeeding: the guard stops the second tap. |

The link sent becomes `{site}/join/#{code}` (§5) instead of `location.origin` (`app.js:1056`). `site` comes from
`data-site="{{ site.site }}"` on the dock. The note under the code reads
"`registry.coffee/join` · until {date}. Shown only once: send it before you leave this screen."

## 5. Cold start

**Measure first (S).** Log `logs.event("ready", seconds=…)` at the end of `create_app`, timed from process start.
Pull a week of numbers from `run.googleapis.com/container/startup_latencies` and from the request log's
`httpRequest.latency` for the first request after each `started` event. Record p50 and p95 in `RUNNING.md`. Today,
from here: one warm GET of `/health` took 0.56 s and `/join` 0.65 s. A cold one wasn't seen; don't wake the
service on purpose to measure. Where the time goes: start-up reads the users, shots and catalog lake from HF before
the port opens (`cloud_run.tf:59`), and the 15-minute uptime check wakes it from zero each time
(`alerts.tf:63`).

**Waking page on the public site (M).** GitHub Pages is always up. Add `src/coffeedata/templates/join.html`, written
by `site.py` to `/join/index.html` next to `privacy`/`terms` (`site.py:403-405`). Its inline script is allowed by
hash in `with_csp` (`site.py:130-149`). Add the app's origin to `connect-src` for this page only. It uses the site's
brand styles and the same heading, "You're invited":
1. It reads the code from `location.hash` and never sends it anywhere except in the final redirect, which keeps it
   in the fragment.
2. It polls `GET {app}/health` with `fetch(…, {mode: "cors", cache: "no-store"})` and a 10 s `AbortController`, every
   2 s. `/health` (`main.py:275-278`) adds `Access-Control-Allow-Origin: {SITE_URL}` and nothing else.
3. While waiting: "Opening the app…". After 3 s: "The app is waking up. This can take a minute or two after a quiet
   spell." Elapsed time is shown in `--ink-2` ("0:42"). After 150 s: "This is taking longer than usual." with a
   button: "Try again".
4. When `/health` answers 200: `location.replace("{app}/join#{code}")`.
5. It shows the in-app browser banner as on the app's join page (UA test in JS). Copy link copies the **site** link.

Old `app.registry.coffee/join#…` links keep working. For people already signed in it doesn't matter: the service
worker shell opens and the outbox waits. The privacy policy needs no change: the page is part of using the app,
which it already covers.

**`min_instances = 1`: not now.** With request-based billing an idle minimum instance costs about
$0.0000025 per vCPU-second and $0.0000025 per GiB-second ([Cloud Run pricing][run-pricing]). For 1 vCPU and 1 GiB
that's roughly 2 × 2.6 M s × $0.0000025 ≈ **$13 a month**, against a bill now inside the free tier
(`PLAN.md:591`). Revisit if the measured p95 cold start is over 30 s and members, not just invitees, meet it often.
In that case set `min_instance_count = 1` only during waking hours, with a scheduled job. Don't shorten the uptime
check to keep the instance warm: Cloud Run doesn't promise to keep an idle instance.

## 6. Idempotent submit

- **Client:** one request in flight per form (the `aria-disabled` guard). Retries resend the identical body.
- **Join (server):** in `Accounts.join`, if the code matches a *join* invite that is used, and `used_by` is an account
  whose username equals the submitted one and whose password verifies, return that account. The route then sets the
  cookie and answers 200, so a double tap, a lost reply or a retry from another browser all end signed in. This
  needs the code *and* the password, so it's no weaker than sign-in. Count it against the same limiter as sign-in
  (`accounts.py:410-418`). Anything else on a used join code gives the "already used" message (code 409, rule
  `c.used`).
- **Order of writes:** today the account is published before the invite is marked used (`accounts.py:538-541`). If
  the second publish fails, the code stays open and can make a second account. Write both rows, then call **one**
  `lake.publish`. That halves the wait and makes the pair atomic.
- **Invites:** the client guard is enough. A stray extra link shows under "Links not used yet" and can be revoked.
  The [IETF Idempotency-Key draft][idem] is the pattern if that ever changes.

## 7. Test plan

- **Unit (`tests/test_rules.py`, `tests/test_invites.py`):** a table for every rule id and message. `@`, `.`,
  capitals, 2/3/32/33 characters. Join returns every failing field at once, and a username error no longer hides a
  password error. A repeat join with the same details gives 200 and a cookie; with a different password, 409 `c.used`.
  One publish per join (count calls on a fake lake). NFC: `"é"` composed and decomposed verify the same. Existing
  10-character passwords still sign in.
- **1Password fixtures (`tests/fixtures/passwords/`):** a seeded generator script plus its committed output. 60
  Random passwords from `ABCDEFGHJKLMNPQRTUVWXYZabcdefghijkmnopqrstuvwxyz2346789` +
  `!#%)*+,-.:=>?@]^_}~`, with lengths 8, 14, 15, 20, 64 and 100 and each toggle combination. 60 Memorable ones with
  3, 4 and 15 words, each separator (`-`, space, `.`, `,`, `_`, digit, digit-or-symbol, none), capitalised or not,
  from a 200-word sample of the list (not the whole file). Also Safari (`abcdef-ghijkl-mnopqr` style), Chrome (15
  characters), a passphrase with spaces, and emoji. Each one has an expected verdict: all ≥ 15 pass, and shorter ones
  fail with `p.length` only.
- **Parity:** `node tests/browser/rules_parity.js` runs `app.js`'s rule checker over the same fixtures and a list of
  usernames, and pytest compares verdicts **and messages** with `rules.py`.
- **End to end (`tests/browser/app_flow.js`, Playwright):** at widths 320, 360, 390 and 430 (height 640 too, with the
  error line visible above the dock):
  - typing turns rules ✓ with no red; blur shows ✗ and the error line; an `@` username offers the suggestion and
    tapping it fills the field;
  - a pasted 1Password Random password passes;
  - the slow server is `create_app` over a fake users lake whose `publish` sleeps 6 s. Check: `aria-disabled`,
    "Creating account…", read-only inputs, the 4 s status text, and exactly one `POST /api/join` after five taps;
  - a lost reply: `page.route` lets the first `/api/join` reach the server, then aborts the reply. The page retries
    and ends signed in, never "already used";
  - `addInitScript(() => delete window.caches)`: join still lands on `/`;
  - reload after opening `/join#CODE`: the code is still filled in;
  - invite screen: a double tap makes one link; renew tapped twice shows no error;
  - axe on idle, invalid, busy, banner and invite-ready, in light and dark.
- **In-app user agents:** contexts with `…Mobile/15E148 Instagram 390.0.0.27.105 (iPhone15,2; iOS 18_5; …)` and
  `…[FBAN/FBIOS;FBAV/480.0.0.40.104;…]` show the banner; plain iOS Safari and Android Chrome don't.
- **Waking page:** a local server whose `/health` waits 20 s. Check the texts at 0, 3 and 150 s, the redirect keeping
  `#code`, and the CSP meta allowing only that `connect-src`.
- **By hand, once:** iPhone Safari with 1Password, the Instagram app (DM the link to yourself), Android Chrome with
  Google Password Manager, and VoiceOver through join.

## 8. Tasks, in order

1. **S** Quick fixes on their own: guard `caches` (`app.js:1205`, `:1241`), mark only the failing field,
   clear-then-set the hint so a repeat is announced, `:disabled` style, keep the code in `sessionStorage`. These
   alone would have saved the tester.
2. **M** `app/rules.py`, with every rule and message. `accounts.py` uses it. Field-tagged 400s. Dots in usernames.
   NFC. The minimum goes to 15 if the owner agrees. Unit tests.
3. **M** Idempotent join, `c.used` on check and join, one publish per join. Tests.
4. **M** Templates: hints, rule lists, the rules JSON block, `passwordrules`, aria wiring, error lines. On join,
   owner setup, reset and Change password.
5. **L** `app.js`: live checks, the `@` suggestion, the submit states (busy, slow, retry, timeout), focus, and
   `post()` reading field details. CSS: rule list, spinner, `.ferr`, `aria-disabled`.
6. **S** In-app browser banner (UA helper in `web.py`, a template block, Copy link).
7. **S-M** Invite screen busy states and focus; the link points at the site.
8. **S** Measure cold start (`ready` event, query in `RUNNING.md`).
9. **M** The site's `/join/` waking page, CORS on `/health`, site CSP for that page.
10. **L** Fixtures, parity test, Playwright cases, the in-app user agents, the slow and lost-reply servers.
11. **S** `DESIGN.md` copy table, `RUNNING.md` cold-start numbers. Decide on `min_instances` once there's data.

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
