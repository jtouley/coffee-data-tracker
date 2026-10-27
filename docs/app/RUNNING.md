# Running the app

The shot-logging app in `app/`: how to run it locally, how sign-in works, and how it's deployed on Cloud Run. The plan and
design are in `PLAN.md` and `DESIGN.md` (branch `app-plan`).

## Locally

```bash
make app                     # http://127.0.0.1:8000 over ./lake, signed in as the owner, no password
HOST=0.0.0.0 make app        # reachable from a phone on the same network
```

Without `SESSION_SECRET` the app uses a stub that treats every request as the owner (`COFFEEDATA_OWNER_ID`,
default `owner`). Anyone who can reach it can log shots, so keep it on your own network. The stub refuses to start on
Cloud Run (or a Hugging Face Space).

To try real sign-in locally, with accounts in `./lake-users`:

```bash
SESSION_SECRET=$(openssl rand -hex 32) AUTH_PEPPER=$(openssl rand -hex 16) OWNER_SETUP_CODE=pick-one \
  COFFEEDATA_INSECURE_COOKIES=1 make app
```

`COFFEEDATA_INSECURE_COOKIES=1` drops the cookie's `Secure` flag, which plain http on a LAN address needs. Chrome
accepts Secure cookies on `localhost` without it.

## Sign-in

- **Accounts** live in their own private dataset, `COFFEEDATA_USERS` (`users/` and `invites/`, one Parquet file
  per change, latest wins). dbt, the MCP server and the weekly run never read it.
- **Passwords** are HMAC-ed with `AUTH_PEPPER`, then hashed with Argon2id (64 MiB, 3 passes, 4 lanes). They need
  at least 10 characters, and common passwords and the username are refused.
- **The owner** is the first account. Creating it needs `OWNER_SETUP_CODE`. Each value of the code works once: it
  creates the owner, or later, typed on the reset screen, resets the owner's password. To use it again, put a new value
  in the secret. The owner keeps the id that shots from before accounts belong to.
- **Sessions** are a signed cookie (`SESSION_SECRET`): HttpOnly, Secure, SameSite=Lax, 90 days, renewed after a
  day of use. "Sign out on all devices" and a password change sign out every other phone.
- **Lockouts:** five failed sign-ins for a username from one address, or twenty from one address, lock that for 15
  minutes. Across addresses a username is never locked, only slowed: after ten failures each try waits up to 30 s.
  Usernames aren't shown to anyone signed out.
- **Invites and reset codes:** the owner makes a one-time invite link (valid a week) or a reset code under Setup →
  Invite someone. Codes are 80-bit, shown once, and only their SHA-256 is stored.

Every read and write is scoped to the user in the cookie, never to a `user_id` in the request.

## On Cloud Run

The app runs on Google Cloud Run in `us-central1` at `app.registry.coffee`. Terraform describes all of
it, in `src/terraform/` (the original GCP setup's place and style):

- an Artifact Registry repository for the image (`app/Dockerfile`, which is a plain container for any host);
- the Cloud Run service: scales to zero, at most **one** instance, 1 GiB, CPU only during requests, startup CPU
  boost. One instance because shots, the write lock and the sign-in limits live in memory;
- Secret Manager secrets for the five app secrets, mounted as environment variables, readable only by the
  service's own runtime account;
- Workload Identity Federation, so GitHub Actions on `main` can deploy without a JSON key;
- the domain mapping for `app.registry.coffee`;
- a $5 monthly budget alert, an alert on any ERROR log line or 5xx, and an uptime check on `/healthz` every
  15 minutes, all emailed to the owner.

`.github/workflows/deploy.yml` builds the image and checks the Terraform on every pull request. On `main` it pushes
the image and deploys it, once the variables below are set.

### Owner's steps

Nothing here was created for you. Once, in this order:

1. **Project:** the app uses the original `coffee-data-tracker` project. `gcloud projects describe
   coffee-data-tracker`: reuse it if it exists, or create it with that ID (`gcloud projects create
   coffee-data-tracker`). Link a billing account; the free tier still needs one. Then `gcloud auth
   application-default login`. If the old Cloud Function from archive/v0-gcp is still deployed, its `allUsers`
   invoker can be removed by hand (`gcloud functions remove-iam-policy-binding`), or the function deleted.
2. **State bucket:** `gcloud storage buckets create gs://coffee-data-tracker-tfstate --location=us-central1`.
3. **Domain prerequisites** (both before the domain mapping; DNS steps are in `runbook/domain-and-dns.md` from #19):
   - verify `registry.coffee` as a Domain property in Google Search Console, with the account that runs
     Terraform as a verified owner;
   - add a CAA record `0 issue "pki.goog"` next to the existing Let's Encrypt one, or Google's managed certificate
     can't be issued.
4. **Apply, in two parts.** The service reads every secret at version `latest`, so the secrets need values
   before the service can be created. First the APIs and the empty secrets:

   ```bash
   cd src/terraform
   cp terraform.tfvars.example terraform.tfvars   # billing_account, owner_email
   terraform init -backend-config="bucket=coffee-data-tracker-tfstate"
   terraform apply -target=google_project_service.apis -target=google_secret_manager_secret.app
   ```

5. **Secret values**, one version for each of the five (Terraform only creates the empty secrets; values never go
   through it or its state). Don't put a value on the command line, where it stays in the shell history. Either
   paste it, then press Enter and Ctrl-D:

   ```bash
   gcloud secrets versions add HF_TOKEN --data-file=-
   gcloud secrets versions add ANTHROPIC_API_KEY --data-file=-   # without a real key, New bag falls back to typing
   ```

   or generate it in place:

   ```bash
   openssl rand -hex 16 | gcloud secrets versions add AUTH_PEPPER --data-file=-
   openssl rand -hex 32 | gcloud secrets versions add SESSION_SECRET --data-file=-
   openssl rand -hex 8 | tee /dev/tty | gcloud secrets versions add OWNER_SETUP_CODE --data-file=-   # note it for step 8
   ```

   Then everything else: `terraform apply`.
6. **GitHub variables** (Settings → Secrets and variables → Actions → Variables), from `terraform output`:
   `GCP_WIF_PROVIDER`, `GCP_DEPLOY_SA` and `GCP_IMAGE`. Then run the deploy workflow once, or push to main.
7. **DNS:** add `CNAME app -> ghs.googlehosted.com.` at Namecheap (`runbook/check-dns.sh` checks it). The
   certificate follows within about an hour.
8. Open `https://app.registry.coffee`, create the owner's account with the setup code, then add your grinder and
   basket in Setup.

### Secrets

| Secret | What it is |
|---|---|
| `HF_TOKEN` | Writes the lake, the users dataset and the photo dataset |
| `ANTHROPIC_API_KEY` | Reads bag labels (10 reads per person per day). It needs a value for the service to start; with a placeholder instead of a real key, New bag asks you to type the coffee |
| `AUTH_PEPPER` | 16+ random characters mixed into every password hash. Changing it breaks every password |
| `SESSION_SECRET` | 32+ random characters that sign the cookie. Changing it signs everyone out |
| `OWNER_SETUP_CODE` | Creates the owner's account, or resets the owner's password on the reset screen. Each value works once; set a new one to use it again. It needs a value for the service to start, even once the owner exists |

`python -c "import secrets; print(secrets.token_urlsafe(32))"` makes a good random string. Until `AUTH_PEPPER`,
`SESSION_SECRET` and `HF_TOKEN` are set, every page returns 503 and names the missing secrets, never their values.

Sign-in limits key on the phone's address: the `X-Forwarded-For` entry Cloud Run's front end appends
(`COFFEEDATA_PROXY_HOPS=1`, set in the Dockerfile), never what the client sent. Uvicorn runs without
`--proxy-headers`.

The dataset locations are set in the Dockerfile:

- `COFFEEDATA_LAKE`: `hf://datasets/jtouley/coffee-data`
- `COFFEEDATA_USERS`: `hf://datasets/jtouley/coffee-data-users`
- `COFFEEDATA_PHOTOS`: `hf://datasets/jtouley/coffee-data-photos`

### Starting up

The app loads shots from the weekly `marts/fct_shot.parquet` export, then reads the shot files written from a day
before it: one directory listing instead of one request per shot file. Without an export, or if the listing fails,
it reads every shot file.

### Debugging

Logs are JSON lines with `severity` and a trace id, so each request's lines group together in the Logs Explorer.
Each request writes one access line with the method, the route template (`/shot/{shot_id}`, never the ids),
status, `latency_ms` and `user_id`. Saves log the table, the id and how long publishing took. Headers, bodies,
query strings and tokens are never logged.

```bash
S='resource.type="cloud_run_revision" AND resource.labels.service_name="registry-coffee"'
gcloud logging read "$S" --freshness=1h --limit=50                                   # the last hour
gcloud logging read "$S AND severity>=ERROR" --freshness=1d                          # errors, with stack traces
gcloud logging read "$S AND jsonPayload.user_id=\"<user_id>\"" --freshness=1d        # one person's requests
gcloud logging read "$S AND trace=\"projects/<project>/traces/<trace_id>\""          # every line of one request
gcloud logging read "$S AND jsonPayload.message=\"saved\"" --freshness=1d            # writes and publish times
```

A request's trace id is on its access line (`logging.googleapis.com/trace`). Errors also appear in Error
Reporting. Locally, `COFFEEDATA_JSON_LOGS=1 make app` prints the same JSON lines.

### Deleting a person's data

The privacy policy promises deletion within 30 days of a request.

1. In the app, turn the account off: Setup → Invite someone → the person → Turn off account. That ends their
   sessions and refuses their sign-in, so the running app can't write anything for them while their files go.
2. With `HF_TOKEN` and the three dataset variables set (as for the weekly run):

   ```bash
   uv run coffeedata delete-user maya --dry-run   # lists every file that would go
   uv run coffeedata delete-user maya
   ```

3. Deploy again (or restart the service), so the app forgets the turned-off account.

It removes every version of that person's shots, bags, equipment, photos, account and invites, and rewrites the
private exports that carry their rows (`marts/fct_shot.parquet`, `marts/mart_bag.parquet`) without them, so the app
can't load them back when it starts: one commit per dataset. It refuses the owner's own account, and an account that
isn't turned off (`--force` skips that check when the app isn't running). The datasets' version history still holds
the old commits, as the policy says.

## Using it on the phone

- **Install:** open the app's address in Chrome on Android and use "Add to Home screen". It runs full screen and stays
  signed in like a normal tab.
- **Offline:** every write waits in the phone's outbox (IndexedDB) until the server acknowledges it, so a shot saved
  with no signal syncs later. The service worker keeps the last copy of Today, Log shot, New bag, Coffees, a coffee
  and History, so those open offline; other pages show "No connection". A bag photo taken offline waits too and is
  read when it syncs. Signing out deletes the kept pages.
- **Left-handed:** Setup → Steppers → Left moves the − and + buttons to the left edge.
- **Bag photos** are shrunk to 1600 px with their EXIF removed, then kept, one Parquet file each, in the private
  `COFFEEDATA_PHOTOS` dataset. Only their owner can see them.
