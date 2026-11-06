# Sign-up and invite UX: spec

Status: ready to build. Owner decisions recorded 2026-11-06, with email recovery added the same day. Scope:
`/join`, owner setup on `/signin`, `/reset`, the new `/forgot` and `/verify`, Setup → Account, `/invite` (people,
links, reset links, the invite list), recovery emails, and the first visit after the app has scaled to zero. Changes
are subtle: same layout, type, colours and dock. What's new is rule text, inline feedback, busy states, named
invites, email recovery, and a way through in-app browsers and cold starts.

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

**Username: the sign-in, and never an email. Allow dots.** (Owner decision.) You sign in with a username and a
password. An email can be added, but only for recovery (§5), and it never works as a sign-in name.
- Rule: `^[a-z0-9._-]{3,32}$` after trim and lower-case, so `first.last` and the tester's `name.com` edit both work.
  Typing `@` shows its own message and a one-tap suggestion: the part before `@`, cleaned up. Never strip characters
  as he types (better-accessibility `forms.md`: "Never block typing or filter characters").
- Capitals are accepted and shown as they'll be stored ("You'll sign in as maya"). The server already lower-cases
  (`accounts.py:528`). Existing usernames aren't affected: sign-in doesn't re-check the pattern (`accounts.py:406-431`).
- Usernames are shown to the owner (`invite.html:18`; `accounts.py:43`). Members' emails are shown only to the owner
  (§6).

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

**Cold start: a waking page on the public site, not `min_instances = 1`.** Details in §10.

## 3. Rules: one source, the same words on both sides

New `app/rules.py` holds the rules as data: `id`, `field`, the text shown under the field, and the error message.
`check_username(name)` and `check_password(pw, username)` return **every** failing rule, not just the first.
`accounts.py` calls them everywhere it checks today (`:384-386`, `:533-537`, `:438`, `:567`). The page gets the same
data as `<script type="application/json" id="rules">{{ rules | tojson }}</script>`. JSON data blocks don't run, so
`web.py`'s `script-src 'self'` allows them (comment at `app/web.py:17-18`). `app.js` evaluates them. The common
list (47 entries) ships too. The server answers 400 with
`{"detail": [{"field": "password", "rule": "length", "message": "…"}]}`. `post()` keeps a plain-string `detail`
working and places field errors inline. A parity test (§12) holds the two sides to the same verdicts and words.

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

## 4. Account recovery: email first, the owner's reset link as the fallback

**What exists today, which stays as the fallback:**
- The owner makes a reset code for one member: `Accounts.invite(by, kind="reset", for_user_id=…)` (`accounts.py:470`,
  via `POST /api/invites` with `kind: "reset"`, `main.py:435-440`). A new code revokes any earlier open one
  (`accounts.py:479-482`). It lasts 7 days, works once, and only its SHA-256 is stored.
- The code is shown once, on the owner's screen, and the owner sends it however they like.
- `/reset` sets the new password and signs out every device (`accounts.py:544-576`).
- The owner recovers their own account with `OWNER_SETUP_CODE` (`accounts.py:551-559`).

**Email recovery (new), following OWASP's Forgot Password sheet ([OWASP-FP][owasp-fp]):**
1. **Asking:** sign-in's "Forgot your password?" (`signin.html:31`) goes to the new `/forgot` page.
   - Heading "Reset your password"; field "Email" (`type="email"`, `autocomplete="email"`); button
     "Send reset link"; busy text "Sending…".
   - Under the button: "No email on your account? Ask the person who invited you for a reset link."
2. **Answering:** the reply is always the same and takes the same time.
   - The page says: "If that email is on an account, we've sent a link. It works for 30 minutes. Check your spam
     folder if it isn't there in a few minutes."
   - `POST /api/forgot` answers 200 at once; the lookup and the send run in a background task. So neither the text
     nor the timing shows whether an account exists (OWASP-FP "consistent message", "uniform response times";
     [OWASP Authentication][owasp-auth], "Authentication and Error Messages").
   - Only a *verified* email can get a link.
3. **The link:** `{APP}/reset#{code}`.
   - The base URL is the fixed `APP` (`site.py:71`), never the request's Host header (OWASP-FP "Host Header
     Injection").
   - The code is `new_code()`: 80 random bits (`accounts.py:141-144`), above NIST's 64-bit floor for recovery codes
     ([NIST][nist] §4.2.1). It's stored as an `Invite` with `kind="reset"`, `created_by="email"` and a **30-minute**
     expiry. NIST allows up to 24 h for email; the owner asked for 15–30 minutes.
   - Only its SHA-256 is stored, it works once, and a new request or a successful reset revokes any other open reset
     code for that person (`accounts.py:479-482`).
   - The code rides in the `#` fragment, so it never reaches server logs or a `Referer`. `/reset` and `/forgot` also
     send `Referrer-Policy: no-referrer`.
4. **Rate limits** (the `Limiter`, `accounts.py:178-243`), before any lookup:
   - 3 requests per email per hour, keyed by the email's HMAC (§5);
   - 10 per IP per hour;
   - 90 sends per day in total, under the provider's 100-a-day free cap.

   Past a limit, the reply text is the same, and nothing is sent. Wrong reset codes already count against the IP
   (`accounts.py:514-525`). Accounts are never locked by a reset request (OWASP-FP).
5. **Resetting:** `/reset` fills the code from the hash, as join does (`app.js:1009-1037`). The new password follows
   the §3 rules.
   - It bumps `session_epoch`, which signs out every device (as today), and revokes the other reset codes.
   - Afterwards, a notice email goes to the verified address: "Your registry.coffee password was changed." NIST
     §4.2: "account recovery SHALL cause a notification".
   - OWASP-FP advises against signing in automatically. So the email path ends on `/signin`, with the username
     filled in and the notice "Password changed. Sign in with the new one." The owner-link path keeps today's
     behaviour.

**Owner fallback, made visible:**
- The member list action reads "Make reset link" (`invite.html:20`).
  - First tap: `confirmTap`, "Tap again to make a reset link for {username}. Any earlier one stops working."
  - Busy: "Making a reset link…".
  - Then a codebox labelled "Reset link for {username}", with Share link / Copy link → "Copied", and the note
    "Send it to {username}. It works once, until {date}, and signs them out everywhere."
- The `/reset` intro: "Use the link from your email, or ask the person who invited you for a reset link. It works
  once, and signs you out everywhere else."

## 5. Email

**Sign-up.** Between the username and the password, add one optional field labelled just **"Email"**. It's the
account's only email: used for account recovery and, if the marketing box is ticked (§7.1), for marketing.
- Input: `type="email"`, `autocomplete="email"`, `autocapitalize="none"`, `spellcheck="false"`.
- Hint: "Optional. For resetting your password and account notices, and for news if you tick the box below. Never
  for signing in. Only the owner can see it." Marketing is a separate checkbox, ticked by default (§7.1).
- **Recommendation: optional, but encouraged. Not required.**
  - A required email makes the account wait on mail delivery and a cold start, the same fragile path that failed the
    tester.
  - A typo or a slow inbox shouldn't block an invited person.
  - The owner's reset link already covers anyone without one.
  - It's encouraged in three places: the hint, a gentle nudge on submit when it's empty ("Add an email so you can
    reset your password yourself? [Add email] [Skip]"; asked once), and the reminder for members in point 4 below.
- Email syntax check, live: has one `@`, a dot in the domain, and is at most 254 characters. Error message: "That
  doesn't look like an email address. Check for typos."
- The server lower-cases the domain and trims. One verified email per account. An email already verified on another
  account gives the same "verify" screen with no error, and that account's owner gets nothing (no enumeration).

**Verification.** An email counts only once verified.
- On join, or when an email is added or changed in Setup, send a link: `{APP}/verify#{token}`. The token has 32
  random bytes, is stored as SHA-256, works once, and lasts 24 h (NIST §4.2.1.2's limit for email).
- `/verify` posts the token. Success: "Email verified. You can now reset your password by email." Expired or used:
  "That link has expired or was already used. Send a new one from Setup." with a link to `/setup#account`.
- Until it's verified, Setup shows "{email} · not verified yet", with "Send again" (3 per hour) and "Change".

**Setup → Account:** an "Email" row.
- It shows "{email} · verified" or "No email", with **Change** and **Remove**.
- Removing asks for a second tap. It deletes the record's files (see Storage) and says "Email removed. The owner can
  still make you a reset link."
- A changed email isn't used until the new one is verified.

**Existing members.** After sign-in, if there's no verified email, Today shows one dismissible `.notice`: "Add an
email so you can reset your password yourself. [Add email] [Not now]". "Not now" sets a `nudge-email` cookie for 90
days, and the row in Setup is always there.

**Storage.** Recommendation: a separate `contacts` table in the users dataset, with an encrypted email, an HMAC for
lookup, and real erasure.
- **Why not a column on `users`:** the users table is append-only on Hugging Face, one file per change, with
  version history (`accounts.py:308-328`, `privacy.html:83-86`). Every later users row (a password change, a
  sign-out everywhere) would copy the email into another file.
- **Row** (one file per change, `contacts/{user_id}__{ts}.parquet`): `user_id`, `logged_at`, `email_hmac`,
  `email_enc`, `verified_at`, `verify_sha256`, `verify_expires`.
- **`email_hmac`:** HMAC-SHA256 of the normalised email, under a key derived from a new `EMAIL_KEY` secret (HKDF,
  label "lookup"). Forgot-password finds the account with it without decrypting anything. A keyed hash, not plain
  SHA-256, because emails are guessable (OWASP Password Storage, on peppers: [OWASP-PS][owasp-ps]).
- **`email_enc`:** AES-256-GCM (the `cryptography` package) under an HKDF "encrypt" key from `EMAIL_KEY`, with
  `user_id` as associated data. The owner's screen and the mail sender decrypt it in the app. Anyone reading the
  dataset, or its history, sees only ciphertext.
- **Erasing and changing:** add `("contacts", "user_id", ("user_id",))` to `erase.py:28`'s `USERS`. Remove, change and
  account deletion all delete **every** file of the record in one commit, the way `erase.py` deletes records. Then
  `HfApi.super_squash_history` on the users dataset drops the old commits. The users dataset is private and small,
  so that's cheap. With encryption that step is defence in depth, not the only protection. Never log an email: logs
  carry the user id only (`privacy.html:39`).
- **Secrets:** `EMAIL_KEY` (32 random bytes, base64) and `RESEND_API_KEY` in Secret Manager, added to `local.secrets`
  (`src/terraform/provider.tf:26-28`), so they're mounted like the rest (`cloud_run.tf:47-57`). Without them, email
  features are hidden. The app still starts: don't add them to `missing_secrets()` (`auth.py:102-110`).

**Privacy page:** it currently promises only "your username and a hash of your password" (`privacy.html:33`), so it
has to be updated before the email field ships (§7.4).

**Sending.** Recommendation: **Resend**.

| Provider | Free tier | Paid entry | Domain setup | Notes |
|---|---|---|---|---|
| Resend | 3,000/month, 100/day, 3 domains, 30-day logs ([pricing][resend]) | $20/mo for 50k | DKIM TXT, SPF + MX on a `send.` subdomain, DMARC | A simple HTTP API with official Python SDK; enough for an invite-only app |
| Postmark | 100/month ([pricing][postmark]) | $15/mo for 10k | DKIM, Return-Path CNAME, DMARC | Best known for transactional deliverability; the free tier is for testing only |
| Amazon SES | No SES free tier for new accounts, only general AWS credits ([pricing][ses]) | $0.10 per 1,000 | DKIM CNAMEs, custom MAIL FROM, DMARC | Cheapest at volume; starts in a sandbox; adds an AWS account and IAM |
| Mailgun | 100/day, 1-day logs ([comparison][mg-cmp]) | about $15/mo | DKIM, SPF, tracking CNAME, DMARC | Fine, but no advantage here |

- **Why Resend:** the free tier covers this app many times over, it's one HTTPS call with one secret, and logs last
  30 days for debugging deliverability. Postmark is the swap-in if deliverability ever disappoints.
- **DNS** (by hand, as for the app's CNAME in `RUNNING.md`):
  - Resend's DKIM `TXT` at `resend._domainkey`, plus the SPF `TXT` and `MX` on `send.registry.coffee`;
  - `_dmarc` `TXT` `v=DMARC1; p=none; rua=mailto:{SUPPORT_EMAIL}`, raised to `p=quarantine` once the reports are
    clean. Gmail asks every sender for SPF or DKIM, and bulk senders for DMARC too ([Google][gmail-guidelines]).
  - Send from `registry.coffee <hello@send.registry.coffee>`, with replies to `SUPPORT_EMAIL`.
- **Templates:** `app/templates/email/{verify,reset,changed}.{txt,html}`, each with a plain-text part and an HTML
  part. The HTML has inline styles only, the app's paper and ink colours, Newsreader for the heading with a serif
  fallback, one accent button, and no images or tracking pixels. Turn off Resend's open and click tracking. The copy
  keeps the coffee subtle:
  - **Verify.** Subject "Confirm your email for registry.coffee". Body: "One quick step before your first shot: confirm
    this is your email, so you can reset your password yourself. [Confirm email] The link works for 24 hours. If you
    didn't sign up, ignore this."
  - **Reset.** Subject "Reset your registry.coffee password". Body: "Someone, hopefully you, asked to reset the
    password for {username}. [Choose a new password] The link works once, for 30 minutes. If it wasn't you, ignore
    this; your password stays as it is."
  - **Changed.** Subject "Your registry.coffee password was changed". Body: "The password for {username} was just
    changed, and every device was signed out. If that wasn't you, reply to this email."
- **Sender interface:** a `Mailer` protocol (`send(to, subject, text, html)`), with `ResendMailer` and a `FakeMailer`
  for tests, so the tests never call out.

## 6. What the owner can see and do

The owner has full admin access over accounts, through the app (`/invite`, which becomes the "People" screen) and the
CLI, but not into secrets.
- **Sees:** every member's username, role, status (active or turned off) and shot count. Their email and
  whether it's verified (decrypted in the app, for helping someone back in). Every invite with its For note and
  status (§8). The owner can also read the datasets directly, as the join page already says (`join.html:20-23`).
- **Does:**
  - makes invite links and reset links (§4);
  - turns accounts off and on, which signs them out (`accounts.py:578-589`);
  - removes a member's email;
  - erases an account and all its data with `coffeedata delete-user` (`erase.py`; the account must be turned off
    first).
- **Never:** passwords, which are peppered and hashed with Argon2id (`accounts.py:292-304`). Reset, verify and invite
  codes, of which only hashes are stored. Session cookies. And no "sign in as this member": recovery goes through a
  reset link the member opens, so the owner never learns a member's password.
- **Out of scope:** the separate `admin.registry.coffee` control plane. This spec covers only what the app's own
  owner screens show.

## 7. Consent and data use

Scope here is what the owner asked for: a required terms checkbox, a separate pre-ticked marketing checkbox, a record
of acceptance, re-acceptance when the terms change, recommendations from logged bags, and aggregated insights for roasters later. Anything that may be
legally required beyond that is a "confirm with counsel" line under task 0 (§13), not a feature in this spec.

**Consent record.** A new `consents` table in the users dataset, one file per change, latest wins.
- Row: `user_id`, `logged_at`, `terms_version`, `privacy_version` (each page's `LEGAL_UPDATED`, `site.py:73`; today
  they share one value), `terms_at`, `marketing` (bool), `marketing_at`, `marketing_version`, and `source` (`signup` / `settings` / `reaccept`).
- Sends read the latest row at the time of sending. Add `("consents", "user_id", ("user_id",))` to `erase.py:28`'s
  `USERS`, so it goes with the account.

**0. Terms checkbox (required at sign-up).** It replaces the passive "By joining you agree…" line (`join.html:20-23`)
on join and on owner setup.
- A native checkbox, **unticked**, just above the button:
  `<input type="checkbox" id="agree" name="agree" required aria-describedby="agree-error">`, labelled
  `<label for="agree">I've read and agree to the <a href="{site}/terms/" target="_blank" rel="noopener">Terms</a> and
  <a href="{site}/privacy/" target="_blank" rel="noopener">Privacy Policy</a></label>`. A link opens in a new tab only
  when it's clicked. Nothing opens by itself, and ticking the box opens nothing: the links sit inside the label, so
  their clicks don't toggle the box.
- "Create account" stays enabled. Submitting unticked shows `<p class="ferr" id="agree-error">`: "Tick the box to
  agree to the Terms and Privacy Policy before creating your account." It sets `aria-invalid` and focuses the first
  problem, as for any field (§9).
- **Server:** `/api/join` and owner setup need `agree: true`, or they answer 400 with that message (rule `terms`, in
  `rules.py`). The check runs before the invite is used.
- **Recorded:** the first `consents` row is written with the account, in the same publish (§11):
  `terms_version`, `privacy_version`, `terms_at`, `source = signup`.
- **When the terms change:** if a member's recorded `terms_version` is older than the current `LEGAL_UPDATED`, the next
  sign-in lands on `/agree` (checked after `sign_in` in `main.py:298-313`, and in `screen_user`, `main.py:131-136`,
  for sessions already open).
  - `/agree` reuses the existing page shell (`base.html`, same layout as `/reset`). It has the heading "We've updated
    our Terms and Privacy Policy", one line with the date, the two links, and the same checkbox.
  - Buttons: **Accept** (needs the tick, with the same error) and **Sign out**.
  - Accept writes a new `consents` row (`source = reaccept`, with the new versions and `terms_at`) and continues to
    `/`.
  - Until then, writes answer 403. The phone's outbox keeps them (`app.js:61`, `Held`) and sends them after Accept.

**1. Marketing checkbox (opt-out).** Directly under the terms checkbox, separate from it, and **ticked by default**:
"Send me occasional emails about new coffees and features". The member can untick it.
- Sign-up works either way. It's also in Setup → Account.
- The choice is recorded at sign-up and on every change: `marketing` (true or false), `marketing_at`, and
  `marketing_version` (the `LEGAL_UPDATED` the member saw). Marketing goes only to members with `marketing = true` and
  a verified email (§5), via Resend, from `news@send.registry.coffee`.
- Recovery, verification and password-changed emails are separate templates and don't depend on this box.

**2. Recommendations from logged bags and shots.** Suggestions are computed inside the app, from the member's own
bags, ratings and shots, matched against the public catalog. The privacy page lists this use (point 4).

**3. Aggregated insights for roasters (future).** Build only after task 0.
- Only aggregates leave the app: counts, medians and distributions by coffee, roaster, origin, process or region.
  Never rows, user ids, usernames, emails, photos or notes.
- **Minimum group size: k = 11.** Suppress any cell with fewer than 11 distinct users, plus any cell that would let
  a suppressed one be worked out from totals. This follows CMS's cell-size suppression rule (cells of 1 to 10 are
  suppressed; [CMS][cms-cells], [CHHS][chhs]).

**4. Privacy page changes.** `privacy.html` currently says the app keeps "your username and a hash of your password"
(`:33`) and uses aggregated data "to run and improve the service and to publish statistics" (`:60-62`). Before
email collection ships, update the page and bump `LEGAL_UPDATED` (`site.py:73`):
- Collected (`:33`): add "an email address, if you add one, used for account recovery and, if you tick the box, marketing" and "whether you agreed to the Terms and opted
  into marketing, and when".
- Uses (`:55-62`): password resets and account notices by email; marketing email unless you untick the box; coffee
  recommendations from the bags and shots you log; aggregated insights provided to roasters.
- Who processes it (`:66-72`): add "**Resend:** sends the app's emails."
- "We don't use third-party tracking or analytics" (`:79`) stays true and stays.
- Exact wording comes after counsel's review (task 0).

## 8. Named invites (owner requirement)

- **What:** an optional **For** note on each invite, so the owner can track links: free text, at most 60 characters
  after trimming and collapsing whitespace, with no control characters. It shows only on the owner's invite screen.
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

  Open rows keep "Send a new link" and "Revoke invite". Every row gets "Edit note". Expired and revoked rows older
  than 90 days are collapsed under "Older links ({n})".
- **Edit and clear:** "Edit note" opens the input in place (Save / Cancel, busy state "Saving…"), calling
  `POST /api/invites/{id}/label` with `{for_label}`. An empty value clears the note. That writes a new version of the
  invite; latest wins. Invites go with the account they made (`erase.py:28`).

## 9. Target behaviour by state

Guidance used: Owl-Listener `form-design` (labels, errors under the field, a check mark where correctness isn't
obvious), `error-handling-ux` (what happened, why, what to do; keep the input), `loading-states` (100 ms–1 s subtle,
1–10 s clear, over 10 s explain), `feedback-patterns` (acknowledge every action), and `accessibility-audit`;
jakubkrehel `better-accessibility` (`forms.md` "Submit behavior", `semantics-and-aria.md` "Disabled states", live
regions) and `interface-review` (every new component has its idle, loading, error, disabled and narrow states).
Paths are in `SKILLS-INDEX.md`.

**Join form (`join.html`). Owner setup and reset reuse the same pieces.** The order is code, username, Email (optional, §5), password, then, just above the button, the required terms checkbox (unticked) and the marketing checkbox (ticked by default) (§7.0, §7.1). Email gets the same live syntax check and the same busy, read-only and error states.

| State | What shows |
|---|---|
| Idle | Today's form. Each field has its hint and, for username and password, a rule list with neutral bullets (`○`, `--ink-3`). Button "Create account". |
| Typing | Each rule turns ✓ (`--balanced`) as it's met. Unmet rules stay neutral while typing. Red only appears on blur or submit (`form-design`: validate on blur), so the live checks read as progress. Nothing is announced per keystroke. Capitals in the username: the hint becomes "You'll sign in as {lower}." Typing `@` shows the `u.email` line at once, with a text button "Use {suggestion}". The suggestion is the part before `@`, lower-cased, with characters outside `[a-z0-9._-]` removed, cut to 32 characters, and only offered when it passes. |
| Invalid (blur or submit) | Unmet rules turn ✗ (`--danger`), with visually-hidden "met" / "not met" text. One error line under the field (`<p class="ferr" id="password-error">`) shows the first failing rule's message, and the field gets `aria-invalid="true"`. On submit, focus moves to the first invalid field. Only fields that fail are marked. The form-level `[data-hint]` is kept for errors that belong to no field, and is cleared before each new message so a repeat is announced again. |
| Submitting (0–4 s) | The button gets `aria-disabled="true"` (not `disabled`, which drops focus), a 14 px ring before the label (pure CSS, `.btn[aria-disabled="true"]::before`, turning; standing still under reduced motion), and the label "Creating account…". It keeps its full colour: no opacity fade, which would drop the label below 4.5:1, and no press scale. Repeat taps do nothing. The inputs become `readonly`. No `aria-busy` on the form, so the slow line below is still announced. |
| Slow (> 4 s) | A status line under the button (`<p class="meta center" role="status">`, in the dock, in the DOM from the start, empty until then; not the error line): "Still working. The app may be waking up, which can take a minute or two. Keep this page open." At 45 s: "Still working. Saving can take a little longer when the app has just woken up." |
| Lost reply | The fetch times out at 130 s, or the network fails, or a 502/503/504 comes back. The page then resends the **same** body up to 3 times (2 s, 5 s, 10 s apart). That's safe: join is idempotent (§11). If all of them fail: "Couldn't reach the app. Nothing you typed is lost. Try again in a moment." The button reads "Try again". |
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

Links point to `{site}/join/#{code}` (§10) instead of `location.origin` (`app.js:1056`), with
`data-site="{{ site.site }}"` on the dock. Reset links stay on the app, because the member already has it open.

## 10. Cold start

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

## 11. Idempotent submit

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

## 12. Test plan

- **Unit (`tests/test_rules.py`, `tests/test_invites.py`):**
  - every rule id and message; `@`, `.`, capitals, 2/3/32/33 characters;
  - 9/10/256/257-character passwords;
  - every failing field comes back at once;
  - a repeat join with the same details gives 200 and a cookie; a different password gives 409;
  - one publish per join;
  - NFC: composed and decomposed `é` verify the same.
- **Named invites:**
  - a note of 60 characters is accepted, and 61 is refused with the message;
  - the note isn't in `/api/invites/check` or `/join`;
  - only the owner can set it (a member gets 404/400);
  - it survives a restart and a renew;
  - the list shows each status (open, used by `maya`, expired, revoked) with its dates;
  - edit and clear show the new value after a restart;
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
  - Invite screen: a double tap makes one link; a note is shown; edit and clear show their busy state.
  - axe on every state above, light and dark.
- **In-app user agents:** `…Mobile/15E148 Instagram 390.0.0.27.105 (iPhone15,2; iOS 18_5; …)` and
  `…[FBAN/FBIOS;FBAV/480.0.0.40.104;…]` show the banner; plain Safari and Chrome don't.
- **Waking page:** a server whose `/health` waits 20 s. Check:
  - steps at 0, 4 and 8 s, three polite announcements, and "Ready" before the redirect;
  - no animation under reduced motion;
  - the error text at 150 s, and Try again restarting the polling;
  - `#code` kept through the redirect;
  - the CSP allowing only that `connect-src`.
- **Email recovery** (with a `FakeMailer` and a fake clock):
  - `/api/forgot` gives the same body and status for a verified email, an unverified email, an unknown email and a
    rate-limited request; response times are within 50 ms of each other (the send happens in the background);
  - only a verified email gets a mail;
  - a reset code from email expires at 30 minutes, works once, and revokes the other open codes; reset bumps
    `session_epoch` and sends the "changed" notice;
  - limits: the 4th request in an hour for one email and the 11th for one IP send nothing, and the daily cap holds;
  - links use `APP` even with a forged `Host` header;
  - the verify link works once and expires after 24 h; an unverified email can't receive a reset;
  - `contacts` rows hold no plaintext email (search the parquet bytes), and the HMAC lookup finds them;
  - remove, change and `delete-user` leave no file of the record, and the squash is called;
  - the owner sees members' emails, and a member's pages never show anyone else's.
- **Consent:**
  - `/api/join` and owner setup without `agree: true` give 400 `terms`; the invite stays open and no account is made;
  - submitting unticked shows the error on the checkbox and focuses it; both links carry `target="_blank"
    rel="noopener"`, and ticking the box opens no tab;
  - the marketing box renders ticked, and is independent of the terms. Unticking it stores `marketing = false`;
    leaving it stores `true`. Both record `marketing_at` and `marketing_version`;
  - the sign-up row carries `terms_version`, `privacy_version` (equal to `LEGAL_UPDATED`) and `terms_at`;
  - marketing sends skip members with `marketing = false` or no verified email;
  - the insights job drops every cell under 11 users, with complementary suppression;
  - after a `LEGAL_UPDATED` bump, an older member's sign-in lands on `/agree`, and writes get 403; Accept records a
    `reaccept` row and clears the gate, and Sign out signs out;
  - `delete-user` removes the `consents` record.
- **By hand, once:** iPhone Safari with 1Password, the Instagram app, Android Chrome, and VoiceOver through join and
  reset.

## 13. Tasks, in order

0. **Legal review required before launch: email collection, marketing and insights sale.** Nothing that collects
   email or consent ships before it, and the insights sale waits for its own sign-off. Confirm with counsel:
   - the privacy and terms wording (§7.4), and whether and how to tell existing members;
   - what to do on a material terms change (re-acceptance or notice);
   - confirm the pre-checked opt-out with counsel (allowed under US CAN-SPAM; not valid consent under GDPR if EU
     users sign up);
   - CAN-SPAM for marketing email (it requires an unsubscribe in every message, a postal address, and opt-outs
     honoured within 10 business days; [FTC][ftc-canspam]), and whether to add `List-Unsubscribe` headers;
   - whether CCPA/CPRA applies; if so, what de-identified data needs (`1798.140(m)`: a public commitment and recipient
     contracts against re-identification, [statute][ccpa-140]), and whether a "Do Not Sell or Share" opt-out is needed
     (`1798.120`, [statute][ccpa-120]);
   - whether data logged under the current privacy page can be used for insights without new consent, and whether
     `terms.html:31-33`'s aggregated-use licence covers it;
   - whether the named-invite note (a third party's name) needs a privacy-page line.
1. **S** Quick fixes: guard `caches` (`app.js:1205`, `:1241`), mark only the failing field, clear-then-set the hint,
   `:disabled` / `aria-disabled` styles, keep the code in `sessionStorage`.
2. **M** `app/rules.py`: every rule and message, with the 10 minimum. `accounts.py` uses it. Field-tagged 400s, dots
   in usernames, NFC. Unit tests.
3. **M** Idempotent join, `c.used` on check and join, one publish per join. Tests.
4. **M** Templates: hints, rule lists, the rules JSON block, `passwordrules` (minlength 10), aria wiring, error
   lines. On join, owner setup, reset and Change password.
5. **L** `app.js`: live checks, the `@` suggestion, the submit states (busy, slow, retry, timeout), focus, and
   `post()` reading field details. CSS: rule list, spinner, `.ferr`.
6. **S** Owner reset links: `/reset#code`, the copy, "Make reset link" with its confirm, busy and copy states.
7. **S–M** Named invites: `for_label` and `created_at`, the For input, `invite_log()` and the list, edit and clear.
   Tests.
8. **S–M** Invite screen busy states and focus for every action; the invite link points at the site.
9. **S** In-app browser banner.
10. **S** Measure cold start.
11. **M** The site's `/join/` "Brewing…" waking page, CORS on `/health`, site CSP.
12. **S** Privacy page changes (§7.4) and `LEGAL_UPDATED`, deployed **before** 14–17. Wording after task 0.
13. **S** Resend account, DNS (DKIM, SPF, DMARC `p=none`), `RESEND_API_KEY` and `EMAIL_KEY` in Secret Manager, and
    the `Mailer` protocol with `FakeMailer`.
14. **M** The `contacts` table: HMAC lookup, AES-GCM, the verify flow (`/verify`), Setup → Account email row, erase
    and squash. Tests.
15. **M** Email recovery: `/forgot`, `/api/forgot` with background send, rate limits, 30-minute codes, the
    password-changed notice, and the email templates. Tests.
16. **S** The sign-up email field and nudge, and the existing-member prompt on Today.
17. **M** `consents`: the required terms checkbox (sign-up and owner setup; links open in a new tab on click), the
    server `agree` check, the acceptance record, the pre-ticked marketing checkbox (sign-up and Setup) with its
    version and timestamp, honoured by sends, and the `/agree` re-acceptance screen (Accept or Sign out). Erase covers
    it. Tests.
18. **L** Tests: fixtures, parity, Playwright (join, invite, forgot and verify, consent screens), in-app user agents,
    slow and lost-reply servers, waking page.
19. **S** `DESIGN.md` copy table, `RUNNING.md` cold-start numbers and email operations. Decide on `min_instances`
    once there's data.
20. **M, later, after task 0's insights sign-off** The insights export job: aggregates only, k = 11 with
    complementary suppression, and a release log.

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
[owasp-fp]: https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html
[owasp-auth]: https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html
[resend]: https://resend.com/pricing
[postmark]: https://postmarkapp.com/pricing
[ses]: https://aws.amazon.com/ses/pricing/
[mg-cmp]: https://devtoolpicks.com/blog/resend-vs-postmark-vs-mailgun-solo-developers-2026
[gmail-guidelines]: https://support.google.com/a/answer/81126
[ftc-canspam]: https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business
[cms-cells]: https://www2.ccwdata.org/documents/10280/19002246/ccw-output-review-dos-donts.pdf
[chhs]: https://kb.data.chhs.ca.gov/ddg-1/6.-justification-of-thresholds-identified
[ccpa-140]: https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=CIV&sectionNum=1798.140
[ccpa-120]: https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=CIV&sectionNum=1798.120
[owasp-ps]: https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
