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
- Usernames are shown to the owner (`invite.html:18`; `accounts.py:43`). Recovery emails are shown only to the owner
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
   - Heading "Reset your password"; field "Email on your account" (`type="email"`, `autocomplete="email"`); button
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

## 5. Recovery email

**Sign-up.** Between the username and the password, add the field **"Email, for account recovery only"**.
- Input: `type="email"`, `autocomplete="email"`, `autocapitalize="none"`, `spellcheck="false"`.
- Hint: "For resetting your password and the odd account notice. Never for signing in. Only the owner can see it."
  Marketing is a separate checkbox, unticked by default (§7).
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

**Setup → Account:** a "Recovery email" row.
- It shows "{email} · verified" or "No recovery email", with **Change** and **Remove**.
- Removing asks for a second tap. It deletes the record's files (see Storage) and says "Email removed. The owner can
  still make you a reset link."
- A changed email isn't used until the new one is verified, and the old address gets a notice: "The recovery email
  on your registry.coffee account was changed." That follows OWASP-FP's advice not to trust a just-changed recovery
  detail.

**Existing members.** After sign-in, if there's no verified email, Today shows one dismissible `.notice`: "Add an
email so you can reset your password yourself. [Add email] [Not now]". "Not now" sets a `nudge-email` cookie for 90
days, and the row in Setup is always there. The first time, the same notice links "What changed" to the privacy
page.

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
  account deletion all delete **every** file of the record in one commit, as named-invite notes do (§8). Then
  `HfApi.super_squash_history` on the users dataset drops the old commits. The users dataset is private and small,
  so that's cheap. With encryption that step is defence in depth, not the only protection. Never log an email: logs
  carry the user id only (`privacy.html:39`).
- **Secrets:** `EMAIL_KEY` (32 random bytes, base64) and `RESEND_API_KEY` in Secret Manager, added to `local.secrets`
  (`src/terraform/provider.tf:26-28`), so they're mounted like the rest (`cloud_run.tf:47-57`). Without them, email
  features are hidden. The app still starts: don't add them to `missing_secrets()` (`auth.py:102-110`).

**Privacy page: change it before any email is collected.** This changes today's promise of "your username and a
hash of your password" (`privacy.html:33`). The new page must be deployed, with `LEGAL_UPDATED` (`site.py:73`)
bumped, **before** the email field ships.
- The full diff, covering email, consents, recommendations and insights, is in §7.4.
- The named-invite line from §8 goes in the same release.
- **Tell existing members** with the notice in §7.4. The owner also tells current members directly, since they have
  no emails on file to send to.

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
- **Sees:** every member's username, role, status (active or turned off) and shot count. Their recovery email and
  whether it's verified (decrypted in the app, for helping someone back in). Every invite with its For note and
  status (§8). The owner can also read the datasets directly, as the join page already says (`join.html:20-23`).
- **Does:**
  - makes invite links and reset links (§4);
  - turns accounts off and on, which signs them out (`accounts.py:578-589`);
  - removes a member's recovery email;
  - erases an account and all its data with `coffeedata delete-user` (`erase.py`; the account must be turned off
    first).
- **Never:** passwords, which are peppered and hashed with Argon2id (`accounts.py:292-304`). Reset, verify and invite
  codes, of which only hashes are stored. Session cookies. And no "sign in as this member": recovery goes through a
  reset link the member opens, so the owner never learns a member's password.
- **Out of scope:** the separate `admin.registry.coffee` control plane. This spec covers only what the app's own
  owner screens show.

## 7. Consent and data use

> **Legal review is required before launch.** Email collection, marketing email, and any sale of insights all need
> it. This spec isn't legal advice. Its author isn't a lawyer, and the statute summaries below are for that review,
> not a substitute for it. In particular, whether CCPA/CPRA applies to registry.coffee at all is for counsel: it has
> revenue and volume thresholds, `1798.140(d)`.

**One consent record per member.** It's a new `consents` table in the users dataset, one file per change, latest
wins.
- **Row:** `user_id`, `logged_at`, `terms_version`, `privacy_version`, `terms_at`, `terms_source` (`signup` /
  `reaccept`), `marketing` (`none` / `pending` / `confirmed` / `withdrawn`), `marketing_at`,
  `personalization` (bool, default true), `insights` (`unset` / `in` / `out`), `insights_at`, `wording_version`
  (the `LEGAL_UPDATED` date of the text the person saw), and `source` (`signup` / `settings` / `unsubscribe` /
  `notice`).
- Every send, every recommendation and every insights export reads the **latest** row at the moment of use. Nothing
  caches a consent past one request or job.
- Add `("consents", "user_id", ("user_id",))` to `erase.py:28`'s `USERS`, so it goes with the account.
- The append-only history is the audit trail of what was agreed and when.

**0. Agreeing to the Terms and Privacy Policy (required at sign-up).** This replaces the passive line "By joining you
agree to the Terms and Privacy Policy" (`join.html:20-23`). The same applies to owner setup (`signin.html`).
- **Placement:** a native checkbox, **unticked**, just above the dock button, with the marketing checkbox
  (point 1) directly under it. Both are visible next to "Create account", not tucked away.
  - Markup: `<input type="checkbox" id="agree" name="agree" required aria-describedby="agree-error">` with
    `<label for="agree">I've read and agree to the <a href="{site}/terms/" target="_blank" rel="noopener">Terms</a>
    and <a href="{site}/privacy/" target="_blank" rel="noopener">Privacy Policy</a></label>`.
  - Each link carries visually-hidden text "(opens in a new tab)".
  - The whole label row is the hit target, at least 44 px tall (better-accessibility "Label and type every
    control").
  - Today's short data note ("Your shots, bags and photos are kept in the owner's private Hugging Face datasets…")
    stays as plain text above it.
- **Not silently disabled:** "Create account" stays enabled (better-accessibility: "Validate on submit, never by
  disabling submit"). Submitting without the box ticked:
  - shows `<p class="ferr" id="agree-error">` under the checkbox: "Tick the box to agree to the Terms and Privacy
    Policy before creating your account.";
  - sets `aria-invalid="true"` on the checkbox and moves focus to the **first** problem in form order. That's the
    checkbox if every field above it is fine, so a screen reader reads its label, the unticked state and the error.
  - Until the box is ticked, a quiet line under the button (`--ink-2`, part of the button's `aria-describedby`)
    says "Agree to the Terms and Privacy Policy to continue."
  - Ticking the box clears the error and the line.
- **Server:** `/api/join` and owner setup (`/api/signin` with `setup_code`) need `agree: true`. Without it they answer
  400 `{"field": "agree", "rule": "terms", "message": "Tick the box to agree to the Terms and Privacy Policy before
  creating your account."}`. That's the same text as the page, from `rules.py` (§3).
  - The check runs **before** the invite is used. So a missing tick never uses up a code, and the idempotent retry
    (§11) still needs it.
- **Recorded:** the first `consents` row is written in the same publish as the account (§11, one commit). It holds
  `terms_version` and `privacy_version` (each page's `LEGAL_UPDATED`, `site.py:73`; today both pages share it),
  `terms_at`, and `terms_source = signup`. The marketing column records its own choice in the same row; agreeing
  to the terms never sets it.

**0b. When the Terms or Privacy Policy change.**
- `site.py` gains `LEGAL_CHANGES`: a list of `{version, material: bool, summary: [lines]}`, newest first. Bump
  `LEGAL_UPDATED` and add an entry with every change to `privacy.html` or `terms.html`. The app reads the same module,
  as it already reads `site.links` (`main.py:34`).
- **Material change** (`material: true`): for example adding email collection (§5), marketing, or the insights use
  (§7.3).
  - At the next page load after sign-in, a member whose `terms_version` is older than the newest material version
    is sent to `/agree`, enforced in `screen_user` (`main.py:131-136`).
  - Writes then answer 403 "Accept the updated terms to keep saving." The phone's outbox keeps them (`app.js:61`,
    `Held`) and sends them after acceptance, so nothing logged at the machine is lost.
  - `/agree` shows the heading "We've updated our Terms and Privacy Policy", the effective date, and the `summary`
    lines as a list. It links to the full pages (new tab) and to the change history on GitHub
    (`{REPO}/commits/main/src/coffeedata/templates/privacy.html` and `…/terms.html`).
  - The same unticked checkbox, and the buttons **Accept and continue**, **Sign out** and **Delete my account**.
    Accept needs the tick, with the same error.
  - Delete asks for a second tap ("Tap again to turn off your account and ask the owner to delete it"). It turns the
    account off at once (`set_disabled`, signing it out everywhere) and records a deletion request for the owner, who
    erases it with `coffeedata delete-user` (§6).
  - Accepting writes a new `consents` row (`terms_source = reaccept`) and lands where the member was going.
- **Minor change** (`material: false`, wording or contact details): no gate. A dismissible `.notice` once: "We've
  made small updates to the Terms and Privacy Policy. [What changed]". The member's recorded versions don't change.
- **Accessibility:** `/agree` is a normal page, not a modal. Focus starts on the `<h1>`, and the summary is a real
  list. The error and focus rules match sign-up.

**1. Marketing email (opt-in).**
- At sign-up, directly under the terms checkbox and next to the button, add a separate optional checkbox,
  **unticked by default**: "Send me occasional emails about new coffees and features". It's never bundled with the
  terms, because consent has to be freely given; sign-up works either way. It's also in Setup → Account. It's only enabled once an email is entered; the hint is
  "You can stop them any time; one click in any email does it."
- **Double opt-in:** ticking it records `pending` and sends "Confirm you'd like coffee news from registry.coffee
  [Yes, send them]". Only a click from a **verified** address makes it `confirmed`. Verifying the email and
  confirming marketing can be one click when they happen together; the confirmation text says both.
- **Every marketing email:**
  - a visible "Unsubscribe" link at the foot;
  - `List-Unsubscribe: <{APP}/u/{token}>, <mailto:unsubscribe@send.registry.coffee>` and
    `List-Unsubscribe-Post: List-Unsubscribe=One-Click` ([RFC 8058][rfc8058]; Gmail asks for one-click unsubscribe
    from bulk senders, [Google][gmail-guidelines]);
  - the postal address CAN-SPAM requires ([FTC][ftc-canspam]).
  - The `{token}` is an HMAC of `user_id` and a purpose, so it needs no sign-in and lasts indefinitely. CAN-SPAM
    needs at least 30 days.
- A `POST` to `/u/{token}` writes `withdrawn` at once (CAN-SPAM allows 10 business days). The confirmation page
  reads "You're unsubscribed. You'll still get password and account emails." It has no second step.
- **Recovery, verification, password-changed and service notices are separate:** a different template set, no
  opt-in needed, and no promotional content mixed in, so they stay transactional under CAN-SPAM.
- Marketing goes through Resend's broadcast or batch API, from `news@send.registry.coffee`, under the same domain
  set-up as §5.

**2. Personalized recommendations from logged bags and shots.**
- These are made inside registry.coffee, from the member's own bags, ratings and shots, matched against the public
  catalog. No person's data, or anything derived from it, is ever shared per person.
- **At the point of use:** each recommendation carries its reason in `--ink-2`, for example "Recommended because you
  rated Ethiopia Guji (washed) highly." It links to "Why am I seeing this?", which goes to the Setup toggle.
- **Setup → Privacy:** "Personalized recommendations" (default on, disclosed at sign-up). Its hint: "Suggest
  coffees from the bags and shots you've logged. Off: no suggestions, and nothing is computed from your logs for
  them." When it's off, the recommendation surfaces aren't rendered, and the recommender skips the member.
- On by default, because it's a feature of the service the member is using, disclosed up front. Whether that's
  enough consent where they live is part of the legal review.

**3. Selling anonymized usage insights to roasters (future; not to be built until counsel signs off).**
- **What may leave:** aggregates only, such as counts, medians and distributions by coffee, roaster, origin, process
  or region, computed by a reviewed job. Never rows, user ids, usernames, emails, photos, free-text notes, or
  anything at a single-bag or single-shot level.
- **Minimum group size: k = 11.** Suppress any cell with fewer than **11 distinct users** (1 to 10), following CMS's
  long-standing cell-size suppression rule for released health data ([CMS][cms-cells]; [CHHS guidelines][chhs]).
  - Also suppress complementary cells, so a suppressed cell can't be recovered by subtraction from totals.
  - No cell may have one user contributing more than 50% of its rows.
  - Regions are coarsened (state, or a metro of over 100k people).
- **Small markets:** where a slice has fewer than 50 users, add calibrated noise, a differential-privacy mechanism
  following NIST SP 800-226 ([NIST DP][nist-dp]), or don't release it. The privacy budget is set and recorded per
  release.
- **California (CCPA/CPRA), for counsel to confirm:**
  - *Deidentified* information (`1798.140(m)`) requires three things: reasonable technical measures against
    re-identification, a **public commitment** to keep it deidentified and not re-identify it, and **contracts**
    obliging every recipient to do the same and not re-identify ([statute][ccpa-140]; summary: [NLR][nlr-deid]).
  - So the privacy page gains that public commitment, and every roaster agreement carries no-re-identification,
    no-onward-transfer and audit clauses.
  - If any released data were *personal information* (`1798.140(v)`), disclosing it for money is a *sale*
    (`1798.140(ad)`). That needs notice and a "**Do Not Sell or Share My Personal Information**" link and opt-out
    (`1798.120`, `1798.135`, [statute][ccpa-120]). The spec avoids releasing personal information, but the link is
    budgeted in case counsel says it's needed.
- **Data already collected:** data collected under the current promise **must not** be used for selling insights
  without new consent. Today the privacy page says aggregated data is used "to run and improve the service and to
  publish statistics" (`privacy.html:60-62`) and "We don't sell or rent your personal data" (`privacy.html:77`).
  The new terms apply only to data logged **after** their effective date, unless an existing member explicitly opts
  in (`insights = in`, `source = notice`).
  - Every insights job filters each member's rows by `logged_at` ≥ the effective date, unless they opted in.
  - `terms.html:31-33` already has an aggregated-use licence. Whether that covers a sale is a question for counsel,
    and this spec doesn't rely on it.
- **Opt-out for everyone:** Setup → Privacy, "Don't include my data in aggregated insights". Recommended even where
  it isn't required. It's honoured from the next export, and excluded members' rows never enter the job.
- **Erasure:** an erased member's rows leave every future release. Releases already delivered were aggregates and
  aren't recalled; the privacy page says so.

**4. Privacy page: changes as a diff.** The page ships, with `LEGAL_UPDATED` (`site.py:73`) set to the effective
date, **before** any of this collects or uses data.
- `privacy.html:33`, the account item: "your username, a hash of your password and, if you add one, a recovery email
  address (encrypted when stored)."
- After `:35`, a new item: "**Your choices:** whether you want marketing email, personalized recommendations and
  aggregated insights, with when you chose."
- "Why we use it" (`:55-59`), add:
  - "**Your email:** password resets, confirming the address, and notices about your account or the service.
    Marketing email only if you opt in and confirm."
  - "**Recommendations:** suggesting coffees from the bags and shots you log, inside registry.coffee. You can turn
    this off in Setup."
- Replace `:60-62` with: "We may use **aggregated, de-identified** data to run and improve the service, to publish
  statistics, and, from {effective date}, to provide insights to roasters, for which they may pay. These are
  aggregates of at least 11 people and never individual records. We commit to keeping that data de-identified and
  not trying to re-identify it, and we require anyone who receives it to commit to the same. Data from before
  {effective date} is included only if you opt in."
- "Who processes it" (`:66-72`), add "**Resend:** sends the app's emails."
- "What we don't do" (`:76-80`): keep "We don't use third-party tracking or analytics." It stays true, because the
  recommender runs in the app and Resend's tracking is off. Change "We don't sell or rent your personal data" to "We
  don't sell or rent your personal data. Aggregated insights, described above, aren't personal data."
- "Your choices and rights" (`:88-96`), add:
  - "In Setup → Account and Privacy: remove your email; stop marketing (or use the link in any marketing email);
    turn off recommendations; leave your data out of aggregated insights."
  - The "Do Not Sell or Share" link, if counsel requires it.
- "How long we keep it": "Your email and choices are deleted with your account."
- **Notice to existing members:** a dismissible in-app notice on next sign-in, worded by counsel, with the effective
  date. Plus an email to those with a verified address, at least 30 days before the effective date if counsel agrees.
  It offers, without pre-selecting anything: "Include my earlier logs in aggregated insights" and "Send me occasional
  emails about new coffees and features".

## 8. Named invites (owner requirement)

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

## 9. Target behaviour by state

Guidance used: Owl-Listener `form-design` (labels, errors under the field, a check mark where correctness isn't
obvious), `error-handling-ux` (what happened, why, what to do; keep the input), `loading-states` (100 ms–1 s subtle,
1–10 s clear, over 10 s explain), `feedback-patterns` (acknowledge every action), and `accessibility-audit`;
jakubkrehel `better-accessibility` (`forms.md` "Submit behavior", `semantics-and-aria.md` "Disabled states", live
regions) and `interface-review` (every new component has its idle, loading, error, disabled and narrow states).
Paths are in `SKILLS-INDEX.md`.

**Join form (`join.html`). Owner setup and reset reuse the same pieces.** The order is code, username, recovery email (optional, §5), password, then, just above the button, the required terms checkbox and the optional marketing checkbox, both unticked (§7.0, §7.1). Email gets the same live syntax check and the same busy, read-only and error states.

| State | What shows |
|---|---|
| Idle | Today's form. Each field has its hint and, for username and password, a rule list with neutral bullets (`○`, `--ink-3`). Button "Create account". |
| Typing | Each rule turns ✓ (`--balanced`) as it's met. Unmet rules stay neutral while typing. Red only appears on blur or submit (`form-design`: validate on blur), so the live checks read as progress. Nothing is announced per keystroke. Capitals in the username: the hint becomes "You'll sign in as {lower}." Typing `@` shows the `u.email` line at once, with a text button "Use {suggestion}". The suggestion is the part before `@`, lower-cased, with characters outside `[a-z0-9._-]` removed, cut to 32 characters, and only offered when it passes. |
| Invalid (blur or submit) | Unmet rules turn ✗ (`--danger`), with visually-hidden "met" / "not met" text. One error line under the field (`<p class="ferr" id="password-error">`) shows the first failing rule's message, and the field gets `aria-invalid="true"`. On submit, focus moves to the first invalid field. Only fields that fail are marked. The form-level `[data-hint]` is kept for errors that belong to no field, and is cleared before each new message so a repeat is announced again. |
| Submitting (0–4 s) | The button gets `aria-disabled="true"` (not `disabled`, which drops focus), a 14 px spinner before the label, and the label "Creating account…". Repeat taps do nothing. The form gets `aria-busy="true"`; the inputs become `readonly`. The style is `.btn[aria-disabled="true"], .btn:disabled { opacity: .72 }`. With reduced motion the spinner is a static `…`. |
| Slow (> 4 s) | A status line under the button (`role="status"`, in the DOM from the start): "Still working. The app may be waking up, which can take a minute or two. Keep this page open." At 45 s: "Still working. Saving can take a little longer when the app has just woken up." |
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
- **Email recovery** (with a `FakeMailer` and a fake clock):
  - `/api/forgot` gives the same body and status for a verified email, an unverified email, an unknown email and a
    rate-limited request; response times are within 50 ms of each other (the send happens in the background);
  - only a verified email gets a mail;
  - a reset code from email expires at 30 minutes, works once, and revokes the other open codes; reset bumps
    `session_epoch` and sends the "changed" notice;
  - limits: the 4th request in an hour for one email and the 11th for one IP send nothing, and the daily cap holds;
  - links use `APP` even with a forged `Host` header;
  - the verify link works once and expires after 24 h; an unverified email can't receive a reset; changing the email
    notifies the old address;
  - `contacts` rows hold no plaintext email (search the parquet bytes), and the HMAC lookup finds them;
  - remove, change and `delete-user` leave no file of the record, and the squash is called;
  - the owner sees the decrypted email, and a member's pages never show anyone else's.
- **Consent:**
  - `/api/join` and owner setup without `agree: true` give 400 `terms`; the invite stays open and no account or
    consent row is written;
  - the page keeps "Create account" enabled; submitting unticked shows the error, sets `aria-invalid`, and focuses
    the checkbox (Playwright, and axe on that state);
  - marketing is independent of the terms: agreeing alone stores marketing `none`; marketing ticked with agreement
    gives `pending`; marketing never stands in for agreement;
  - the sign-up row carries `terms_version`, `privacy_version` (equal to `LEGAL_UPDATED`), `terms_at` and
    `terms_source = signup`;
  - after a material `LEGAL_CHANGES` entry and a `LEGAL_UPDATED` bump, an older member is sent to `/agree` and writes
    get 403 (the outbox keeps them); Accept writes a `reaccept` row, clears the gate, and the outbox then syncs;
    a minor entry shows only the notice;
  - Delete my account on `/agree` turns the account off and signs it out;
  - the marketing box renders unticked, and an untouched sign-up stores `none`;
  - nothing goes to a `pending`, `withdrawn`, unverified or `none` member; only `confirmed` and verified gets mail;
  - one-click `POST /u/{token}` withdraws, and the headers are present on every marketing mail and absent on
    transactional ones;
  - `wording_version` and timestamps are recorded;
  - turning personalization off removes the recommendation markup and skips the member in the recommender;
  - the insights job drops opted-out members, rows from before the effective date for members who didn't opt in,
    and every cell under 11 users, with complementary suppression;
  - `delete-user` removes the `consents` and `contacts` records;
  - the privacy page's built HTML still contains "We don't use third-party tracking or analytics".
- **By hand, once:** iPhone Safari with 1Password, the Instagram app, Android Chrome, and VoiceOver through join and
  reset.

## 13. Tasks, in order

0. **Legal review required before launch: email collection, marketing and insights sale.** Counsel reviews §5 and
   §7, the privacy and terms text, whether CCPA/CPRA and CAN-SPAM apply and how, the roaster contract terms, and the
   member notice. Nothing that collects email or consent ships before this. The insights sale also waits for its
   own sign-off.
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
7. **S–M** Named invites: `for_label` and `created_at`, the For input, `invite_log()` and the list, edit, clear and
   delete (removing old files). Tests.
8. **S–M** Invite screen busy states and focus for every action; the invite link points at the site.
9. **S** In-app browser banner.
10. **S** Measure cold start.
11. **M** The site's `/join/` "Brewing…" waking page, CORS on `/health`, site CSP.
12. **M** Privacy and terms changes (§7.4, plus the named-invite line), `LEGAL_UPDATED` and its first `LEGAL_CHANGES`
    entry, marked material, and the member notice.
    Deployed **before** 14–17. Wording after task 0.
13. **S** Resend account, DNS (DKIM, SPF, DMARC `p=none`), `RESEND_API_KEY` and `EMAIL_KEY` in Secret Manager, and
    the `Mailer` protocol with `FakeMailer`.
14. **M** The `contacts` table: HMAC lookup, AES-GCM, the verify flow (`/verify`), Setup → Account email row, erase
    and squash. Tests.
15. **M** Email recovery: `/forgot`, `/api/forgot` with background send, rate limits, 30-minute codes, the
    password-changed notice, and the email templates. Tests.
16. **S** The sign-up email field and nudge, and the existing-member prompt on Today.
17. **L** `consents`: the required terms checkbox (sign-up and owner setup, server `agree` check, the acceptance
    fields), `LEGAL_CHANGES` with the `/agree` re-acceptance gate (Accept, Sign out, Delete my account) and the
    minor-change notice; the marketing checkbox, double opt-in, unsubscribe with `List-Unsubscribe` and one-click POST,
    the personalization toggle and "Recommended because…", and the insights opt-out. Erase covers them. Tests.
18. **L** Tests: fixtures, parity, Playwright (join, invite, forgot and verify, consent screens), in-app user agents,
    slow and lost-reply servers, waking page.
19. **S** `DESIGN.md` copy table, `RUNNING.md` cold-start numbers and email operations. Decide on `min_instances`
    once there's data.
20. **L, later, behind task 0's insights sign-off** The insights export job: k = 11, complementary suppression,
    noise for small markets, effective-date and opt-out filters, and a release log. Plus the roaster contract
    template, from counsel.

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
[owasp-ps]: https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
[resend]: https://resend.com/pricing
[postmark]: https://postmarkapp.com/pricing
[ses]: https://aws.amazon.com/ses/pricing/
[mg-cmp]: https://devtoolpicks.com/blog/resend-vs-postmark-vs-mailgun-solo-developers-2026
[gmail-guidelines]: https://support.google.com/a/answer/81126
[rfc8058]: https://www.rfc-editor.org/rfc/rfc8058
[ftc-canspam]: https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business
[cms-cells]: https://www2.ccwdata.org/documents/10280/19002246/ccw-output-review-dos-donts.pdf
[chhs]: https://kb.data.chhs.ca.gov/ddg-1/6.-justification-of-thresholds-identified
[nist-dp]: https://csrc.nist.gov/pubs/sp/800/226/final
[ccpa-140]: https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=CIV&sectionNum=1798.140
[ccpa-120]: https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=CIV&sectionNum=1798.120
[nlr-deid]: https://natlawreview.com/article/what-de-identified-data
