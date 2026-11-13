# Running the app

The shot-logging app in `app/`: how to run it locally, how sign-in works, and how it's deployed on Cloud Run. The plan and
design are in `PLAN.md` and `DESIGN.md` (branch `app-plan`).

## Locally

```bash
make app                     # http://127.0.0.1:8000 over ./lake, signed in as the owner, no password
HOST=0.0.0.0 make app        # reachable from a phone on the same network
```

`make app` sets `COFFEEDATA_LOCAL_OWNER=1`, which, without `SESSION_SECRET`, uses a stub that treats every request
as the owner (`COFFEEDATA_OWNER_ID`, default `owner`). Anyone who can reach it can log shots, so keep it on your own
network. The stub runs only when asked for by that variable, and refuses to start on Cloud Run (or a Hugging Face
Space). Started any other way without `SESSION_SECRET` and `AUTH_PEPPER`, the app serves nothing but a "not set up"
notice.

To try real sign-in locally, with accounts in `./lake-users`:

```bash
SESSION_SECRET=$(openssl rand -hex 32) AUTH_PEPPER=$(openssl rand -hex 16) OWNER_SETUP_CODE=pick-one \
  COFFEEDATA_INSECURE_COOKIES=1 make app
```

`COFFEEDATA_INSECURE_COOKIES=1` drops the cookie's `Secure` flag, which plain http on a LAN address needs. Chrome
accepts Secure cookies on `localhost` without it. On Cloud Run (or a Space) the app refuses to start while it is set.

Every screen and API call needs a signed-in session except the few listed in `PUBLIC` (`app/auth.py`): health,
sign-in, join, reset, the offline page and the static files. A new route is private until it is added there, and
`tests/test_auth_gate.py` checks every registered route. Pages and photos for a signed-in person are sent with
`Cache-Control: no-store, private` and `Vary: Cookie`, so no browser HTTP cache or shared cache keeps them. The
app's own service worker is the exception: it keeps the last copy of the screens used at the machine (Today, Log
shot, New bag, Coffees, a coffee, History, Settings), and up to 60 bag photos, so they open offline. It clears them
on sign-out and whenever the sign-in screen opens (except after "couldn't check your sign-in", when the session may
still be good). The password forms post: if app.js didn't wire one, the page comes back with a notice and the
username as typed, and nothing is done with what was sent.

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
  boost, up to 250 requests at once, a 120 s request timeout, and a startup probe that allows about two minutes.
  One instance because shots, accounts, the write lock and the sign-in limits live in memory;
- Secret Manager secrets for the five app secrets (seven once email is on, below), mounted as environment variables,
  readable only by the service's own runtime account;
- Workload Identity Federation, so GitHub Actions on `main` can deploy without a JSON key;
- the domain mapping for `app.registry.coffee`;
- a $20 monthly budget alert, an alert on any ERROR log line or 5xx, and an uptime check on `/health` every
  15 minutes, all emailed to the owner.

`.github/workflows/deploy.yml` builds the image and checks the Terraform on every pull request. On `main` it waits
for `ci` to pass on the push, then pushes the image and deploys it by digest, once the variables below are set. It
deploys only main's newest commit, so an older commit whose `ci` finished late never replaces a newer one, and it
deploys after docs-only merges too (the same app, restarted). Running the workflow by hand from `main` deploys main's
newest commit if `ci` passed on it; with `force` checked, even if it didn't. With the variable `GCP_ADMIN_SERVICE`
set, the same image then goes to the admin service, through the `admin` environment, where a required reviewer can
be added in the repository's settings, as long as the app still runs that image (a later run's deploy carries on
from there), so the app and the admin never stay on different images. A deploy while traffic is pinned (Rolling
back, below) ends with a warning on the run, and leaves the admin where it is.

Once the deploy job has pushed the app's image, the same run does two more things, neither of which deploys
anything, and both whether or not the rollout then succeeded:
- `attest` signs build provenance for the app's image (which workflow run and commit made that digest);
- `ingest-image` builds the ingestion jobs' image from the same commit, checks it starts, pushes it beside the
  app's (`.../ingest:<commit>`) and signs its provenance. The jobs run it only once Terraform pins its digest
  (`docs/ingestion/RUNBOOK.md` §1 and §4). The start check is small: the CLI's help, the packages the jobs
  import, DuckDB's httpfs. It doesn't prove a crawl or a build works.

Check an image's provenance (it needs `gh` signed in, and read access to the registry, which is private:
`gcloud auth configure-docker us-central1-docker.pkg.dev`):

```bash
gh attestation verify oci://<image>@sha256:<digest> --repo jtouley/coffee-data-tracker \
  --signer-workflow jtouley/coffee-data-tracker/.github/workflows/deploy.yml --source-ref refs/heads/main
```

It proves the digest was built by `deploy.yml` on main, and from which commit. It doesn't prove ci passed on that
commit: a manual deploy with `force` builds and signs too.

**A red run after a deploy.** If `deploy` (and `deploy-admin`) are green and only `attest` or `ingest-image` is
red, production was updated and is fine; what is missing is a signature or the ingest image.
- Use **Re-run failed jobs**. `attest` signs the same digest again. `ingest-image` builds, pushes and signs again;
  the commit's tag then points at the new, signed image.
- Don't use **Re-run all jobs** for this: it runs the deploy again. That is harmless when main has moved on (the
  run skips), and otherwise builds and deploys a new image of the same commit.
- An app image whose deploy step failed is still signed, so a revision deployed from it by hand later verifies,
  and the ingest image for that commit is still built.
  An ingest image whose signing failed stays in the registry unsigned; RUNBOOK §1's script refuses it until the
  job is re-run.

**Which jobs can act as the deploy account.** The Workload Identity condition (`src/terraform/deploy.tf`) accepts
a token from any job of `deploy.yml` running on `main` that has `id-token: write`. `deploy`, `deploy-admin` and
`ingest-image` need that: they push or deploy. `attest` has `id-token: write` only to get a signing certificate
and never signs in to Google Cloud, but the condition would let it. To close that, give the three jobs that need
the cloud a GitHub environment each (`deploy-admin` has `admin`), map `attribute.environment` to
`assertion.environment` in the provider, and add `assertion.environment in ['<those names>']` to its condition.
Apply that and merge the workflow change together: a job with no environment is refused once the condition is
applied, and that includes the deploy itself.

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
   paste it, then press Ctrl-D twice (no Enter, which would add a newline):

   ```bash
   gcloud secrets versions add HF_TOKEN --data-file=-
   gcloud secrets versions add ANTHROPIC_API_KEY --data-file=-   # without a real key, New bag falls back to typing
   ```

   or generate it in place:

   ```bash
   openssl rand -hex 16 | tr -d '\n' | gcloud secrets versions add AUTH_PEPPER --data-file=-
   openssl rand -hex 32 | tr -d '\n' | gcloud secrets versions add SESSION_SECRET --data-file=-
   openssl rand -hex 8 | tr -d '\n' | tee /dev/tty | gcloud secrets versions add OWNER_SETUP_CODE --data-file=-   # note it for step 8
   ```

   (The app strips surrounding whitespace from every secret anyway, so a stray newline does no harm.) Then
   everything else: `terraform apply`.
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
| `EMAIL_KEY` | Email only (`email = true`). 32 random bytes, base64 (`openssl rand -base64 32`). Seals members' email addresses and keys their lookup hash. Losing it makes every stored email unreadable; rotate it with `app.rekey` (Email, below), never by just replacing it. Missing or malformed: the email features are hidden and the app still starts |
| `RESEND_API_KEY` | Email only. A Resend API key with "Sending access" to the sending domain only. Without it, on Cloud Run, the email features are hidden |

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

### Rolling back

Every deploy makes a new revision, and each revision keeps the image digest it was deployed with.

**Before you start.** You need `gcloud` signed in as an account with `roles/run.developer` (or Owner) on the project,
and the project set: `gcloud config set project <project_id>` (the `project_id` in Terraform's variables). With the
repository variables `GCP_SERVICE` or `GCP_REGION` set, use their values instead of `registry-coffee` and
`us-central1` below.

**App and admin together.** They run the same image, so a bad change in shared code (anything outside `admin/`)
is bad in both: roll back both, to revisions with the same image. The admin service (`registry-coffee-admin`, when
`GCP_ADMIN_SERVICE` is set) rolls back the same way, with its own name in place of `registry-coffee`.

**Stored data.** A rollback changes the code, not the data. Before going back past a change to what the app writes
(the users dataset's tables, `USER_TABLES` in `src/coffeedata/lake.py`, or the shot and bag records), check that
the older code can read what the newer one wrote. A new column is ignored by older readers; a changed meaning or
type isn't. Each image is pushed with its commit as its tag, so the revision's commit is:

```bash
image=$(gcloud run revisions describe <previous> --region us-central1 --format='value(spec.containers[0].image)')
gcloud artifacts docker tags list "${image%@*}" --filter="version~${image#*@}" --format='value(tag)'
git log -p <that commit>..main -- src/coffeedata/lake.py app/accounts.py
```

To go back to the last good revision while the fix is made:

```bash
gcloud run revisions list --service registry-coffee --region us-central1    # newest first: pick the last good one
gcloud run services update-traffic registry-coffee --region us-central1 --to-revisions=<previous>=100
```

**Check it worked.** `curl -fsS https://app.registry.coffee/health` answers 200, `gcloud run services describe
registry-coffee --region us-central1` shows 100% on `<previous>`, and the "registry.coffee: errors" alert stops
firing (Monitoring, Alerting; it closes on its own once the errors stop).

> **Pinned traffic stays pinned.** Once traffic is sent to a named revision, every later deploy makes a new revision
> that gets **0%** of the traffic. The deploy job still succeeds, with a warning that traffic is pinned ("all
> subsequent deployments use that traffic split pattern going forward", in Cloud Run's
> [rollouts and rollbacks](https://cloud.google.com/run/docs/rollouts-rollbacks-traffic-migration)). So the fix's own
> deploy changes nothing until traffic is moved back.

Once the fix is on `main` and its deploy has finished, send traffic back to the newest revision, and check it the
same way:

```bash
gcloud run services update-traffic registry-coffee --region us-central1 --to-latest
gcloud run services describe registry-coffee --region us-central1    # 100% to the latest revision
curl -fsS https://app.registry.coffee/health
```

### Changing the infrastructure (Terraform)

Since the last Cloud Shell run (`docs/ingestion/final-shell-run.sh`), a change to `src/terraform/` goes through
GitHub, and applies only after the owner's second account, `jtouleyrou`, approves it:

1. **Open a PR.** `terraform-plan.yml` plans it and comments the guard's verdicts and the list of changes (actions
   and addresses, never values). The PR plan is advisory.
2. **Merge.** `terraform-apply.yml` plans main again as the read-only account, with `src/terraform/ci.tfvars` and
   the `TF_VAR_*` repository secrets and variables. It runs the guard (`scripts/tf_plan_guard.py`), saves the plan
   to the private bucket `coffee-data-tracker-tf-plans`, and writes the change list, with IAM changes first, and the
   plan's SHA-256 to the run's summary. Then it waits on the `terraform-apply` environment. No changes: the run
   ends there. Start it as the main account, never as `jtouleyrou`.
3. **Approve, signed in as `jtouleyrou`.** Open the run (Actions, terraform-apply) and read its summary. If the
   commit touches `.github/workflows/terraform-*.yml` or `scripts/tf_plan_guard.py`, read that diff first: the
   approval page doesn't show it. Then Review deployments, Approve. A run you won't approve: Reject it, so the runs
   behind it can go ahead (GitHub ends an unanswered one after 30 days).
4. **The apply job** refuses, changing nothing, unless:
   - this is the run's first attempt and `jtouleyrou` approved it;
   - the environment's settings read back exactly as set: that reviewer alone, self-review prevented, `main` only,
     no administrator bypass;
   - the plan is under a day old, no newer Terraform commit is on main, and the downloaded plan has the SHA-256 shown
     before the approval;
   - the state hasn't changed since the plan, the guard passes again, and a fresh plan shows the same changes (no
     drift);
   - it isn't Monday or Tuesday (UTC), Sunday from 23:00, or 02:00-05:00 on the 1st, and no ingestion job runs.

   It then applies that saved plan, never a fresh one, checks that every job runs the pinned image, and deletes the
   plan. After a refusal, run the workflow again from `main` once its cause is gone (a re-run of the same run never
   applies); the new plan waits for a new approval.

**If an apply fails partway**, Terraform keeps what it finished in the state and nothing else runs. Only a fixed
message and the affected addresses are shown: Terraform's own messages can quote private values, and the
`TF_VAR_*` repository variables aren't masked. For the details, plan in Cloud Shell. Fix the cause in a PR, or run
the workflow again; the new plan shows what is left. A lock left by a dead runner is cleared from Cloud Shell:
`terraform force-unlock <lock id>`, once you're sure no apply is running.

**What an approved apply can do.** The apply account manages what the main root manages, so once you approve, it can
set IAM on the secrets (and so reach their values), on the image repository, the Cloud Run service and jobs and the
staging bucket (and so raw ingestion data), and run any image as the runtime, ingest or scheduler account. That is
what the approval is for: read the IAM section of the summary first. It cannot write a workload identity pool or
provider, a custom role, the project's IAM, or IAM on any service account; and the guard refuses any change to the
deploy, plan or apply accounts or to grants that name them, to a pool or provider, to Shopify's network path (routes,
firewall rules and peerings on the ingest network included), and any delete, replace or unpinned image.

**Not through GitHub, on purpose:** what the guard refuses, the bootstrap root `src/terraform/bootstrap/` (the plan
and apply identities and the plans bucket: the gate is not managed by what it gates), secret values, DNS. These, and
the break-glass below, are the owner's, from Cloud Shell.

**Residual risk.**
- Google issues the apply token to any job that runs in the `terraform-apply` environment. It can't see who approved.
- An account with admin rights on this repository, including any tool that holds such a credential, could loosen
  the environment's reviewers.
- What stands in the way:
  - the environment's own required reviewer;
  - the workflow's checks, which fail closed: it applies only when it can verify a `jtouleyrou` approval of this
    run's first attempt, bound to the plan's hash;
  - for this project's agent sessions, a proxy that refuses environment and ruleset changes.
- The proper fix: agents stop using an admin credential. Give them their own account or app with write access and
  no administration, and require `jtouleyrou`'s review of `.github/workflows/terraform-*.yml`,
  `scripts/tf_plan_guard.py` and `src/terraform/**`.

**Break-glass** (GitHub down, or the pipeline broken), in Cloud Shell with `main` checked out and your
`terraform.tfvars`:

```bash
cd src/terraform && d=$(mktemp -d)   # the plan and its JSON hold every value in clear: keep them out of the checkout
terraform plan -out="$d/bg.plan" >"$d/plan.log" 2>&1 || less "$d/plan.log"
terraform show -json "$d/bg.plan" >"$d/bg.json" && python3 -I ../../scripts/tf_plan_guard.py check "$d/bg.json"
python3 -I ../../scripts/tf_plan_guard.py render "$d/bg.json"   # read the list
terraform apply "$d/bg.plan"; rm -rf "$d"
```

The state lock is shared, so this is safe beside a waiting run.

**Revoking the pipeline's access**, fastest first:
- stop applies at once: `gcloud iam service-accounts disable registry-coffee-tf-apply@coffee-data-tracker.iam.gserviceaccount.com`
  (and `registry-coffee-tf-plan@...` for plans); `enable` undoes it;
- or turn off the sign-in from GitHub:
  `gcloud iam workload-identity-pools providers disable github --workload-identity-pool=github-tf-apply --location=global`
  (`github-tf-plan` for plans);
- for good: remove the resources from `src/terraform/bootstrap/` and apply it from Cloud Shell, after
  `terraform plan -destroy` shows exactly what goes; then delete the repository variables `GCP_TF_*` and
  `TF_PLANS_BUCKET`.

### Deleting a person's data

The privacy policy promises deletion within 30 days of a request.

1. In the app, turn the account off: Setup → Invite someone → the person → Turn off account. That ends their
   sessions and refuses their sign-in, so the running app can't write anything for them while their files go.
2. With `HF_TOKEN` and the three dataset variables set (as for the weekly run):

   ```bash
   uv run coffeedata delete-user maya --dry-run   # lists every file that would go
   uv run coffeedata delete-user maya
   ```

3. Deploy again (or restart the service) right away, so the app forgets the turned-off account. Until then,
   don't turn the account back on. Don't run `delete-user` while the weekly job is running: it rewrites the
   same exports.

It removes every version of that person's shots, bags, equipment, photos, account and invites, and rewrites the
private exports that carry their rows (`marts/fct_shot.parquet`, `marts/mart_bag.parquet`) without them, so the app
can't load them back when it starts: one commit per dataset. It refuses the owner's own account, and an account that
isn't turned off (`--force` skips that check when the app isn't running). The main and photo datasets' version
history still holds the old commits, as the policy says. The users dataset's history is squashed afterwards, so the
person's email (sealed) goes from its history too.

## Email

_Last verified against the code: 2026-11-08 (PR #134, after the final review). Check it again whenever
`app/mail.py`, `app/accounts.py`'s email section or `src/terraform/` change._

Email recovery (docs/app/signup-ux-spec.md §4, §5): members can add an email, confirm it from a link, and reset a
forgotten password from `/forgot` with their username or email. The app sends four emails, verify, reset,
password changed and email changed, through [Resend](https://resend.com), inside the request that asks for them
(Cloud Run gives an instance no CPU after its reply), with one exception: the reset link from `/forgot`. Its reply
mustn't say whether a link went, so the send starts during the reply's fixed 2 s floor and the reply doesn't wait for
it (`mail.later`, at most 4 at once and 16 in flight; past that, new ones are dropped). A send slower than the floor
carries on after the reply with little CPU: it can arrive late, or be lost if the instance stops first, and the
person can ask again (3 an hour). Every send, all its tries included, is cut off after 10 s: with slow tries that
leaves room for two, while quick failures still get all three. Each way a send fails, is cancelled or is dropped is
logged at WARNING or above, so the "email not sent" alert sees it; a send cut off by the instance stopping logs
nothing and doesn't alert. Until `EMAIL_KEY` and `RESEND_API_KEY` are both set, the
email features are hidden: no email field, no marketing box, and `/forgot` shows only "ask the person who invited
you". The app starts either way.

| What | Where | Value |
|---|---|---|
| `EMAIL_KEY` secret | Secret Manager: always exists, mounted when `email = true` | 32 random bytes, base64. Keep a copy offline (a password manager): without it no stored email can be read |
| `RESEND_API_KEY` secret | Secret Manager: always exists, mounted when `email = true` | A Resend key with "Sending access", limited to the sending domain |
| `email` | `terraform.tfvars` | `true` mounts the two secrets and adds the "email not sent" alert; `false` only unmounts them |
| `EMAIL_FROM` env var (optional) | Cloud Run | Default `registry.coffee <hello@send.registry.coffee>`; set it only if the Resend domain differs |
| `SUPPORT_EMAIL` env var (already set) | Cloud Run | The reply-to address of every email |
| DNS records | the registrar, by hand | Resend's DKIM `TXT`, the SPF `TXT` and `MX` for the sending subdomain, and DMARC (below) |

### Turning email on

1. **Resend.** Make an account and add the domain `send.registry.coffee` (a sending subdomain keeps the root
   domain's reputation apart). In the domain's settings, turn **off** open tracking and click tracking: the emails
   carry no pixels and their links go straight to the app.
2. **DNS**, by hand, like the app's CNAME. Copy each record exactly as Resend shows it:
   - the DKIM `TXT` record (`resend._domainkey…`);
   - the SPF `TXT` record and the `MX` record for the bounce subdomain;
   - DMARC at `_dmarc.registry.coffee`: `TXT` `v=DMARC1; p=none; rua=mailto:<SUPPORT_EMAIL>`. Read the reports for
     two weeks, then raise it to `p=quarantine`. Gmail asks every sender for SPF or DKIM, and bulk senders for
     DMARC too.

   Wait until Resend shows the domain as verified.
3. **The key.** In Resend, API Keys → Create: permission "Sending access", domain `send.registry.coffee`.
4. **Secrets, then Terraform.** The two secrets exist from any apply (empty, unmounted). The service reads every
   secret at version `latest`, so they need values before `email = true` mounts them. Nothing below prints a key:

   ```bash
   terraform apply   # with email = false (the default): creates the two empty secrets if they aren't there yet
   (umask 077; openssl rand -base64 32 | tr -d '\n' > email_key.txt)   # copy it into your password manager now
   gcloud secrets versions add EMAIL_KEY --data-file=email_key.txt && shred -u email_key.txt
   gcloud secrets versions add RESEND_API_KEY --data-file=-   # paste the Resend key, then Ctrl-D
   ```

   Then set `email = true` in `terraform.tfvars` and `terraform apply`: it mounts both and adds the alert, and Cloud
   Run makes a new revision.

5. **Check it.** In the app: Setup → Add email, with your own address. The confirm email arrives (check the spam
   folder too); open its link. Sign out, Forgot your password? → your username: the reset email arrives and its link
   works once. Resend's Emails log shows each as delivered.

The privacy policy's update (with `LEGAL_UPDATED`) ships in the same deploy as the email field. Every member then
accepts the new Terms and Privacy Policy once, on `/agree`.

### Sending limits

- **Resend's free tier:** 100 emails a day and 3,000 a month. The app stops at **90 in any 24 hours**
  (`app/mail.py`, `DAILY_CAP`), counting every kind that was sent (a failed send gives its place back), and logs
  "email not sent: the daily limit is reached" past it. Resend's own quota is the hard stop: Resend answers 429
  past it.
- **`/forgot`:** at most 3 requests an hour for one username or one email, 3 an hour for one account however it's
  named, 10 an hour from one IP address, and 20 "no verified email" answers an hour in all. Past any of them the
  reply is the same as for an unknown account and nothing is sent. Every answer takes at least 2 s
  (`FORGOT_FLOOR`), so the reply's timing doesn't say whether a link went.
- **The owner never resets by email** (owner decision, 2026-11-09): `/forgot` answers for the owner like any account
  that can't get a link, and sends nothing. The owner's email, if added, is for notices; the owner's password resets
  with `OWNER_SETUP_CODE` on `/reset`.
- **Verify links:** 3 an hour per account (Send again, and changing the email).
- **"No email came", and the limits.** Because the limits are shared, someone else can use up a name's 3 an hour,
  or the 20 "no verified email" answers an hour in all. A real member is then told "we've sent a link" (or, with
  no email, isn't told to ask you) and nothing goes. If a member reports that, check the logs for a burst of
  `/api/forgot` and make them a reset link on the People screen.
- **What the limits are worth.** They're counted in memory, per instance, and every cold start of this
  scale-to-zero service starts them again. They stop a quick run of guesses from one place and cap how many
  accounts a scan can confirm in an instance-hour. They don't stop a slow scan spread over many addresses and many
  hours. The "no verified email" answer, which says an account exists, is the owner's choice for an invite-only
  app, made knowing that.
- **Links:** a reset link works once, for 30 minutes; a newer one replaces it. It's signed, not stored, so the
  "newer one replaces it" part is kept in memory: after a restart an older link works again until its 30 minutes
  are up. A verify link works once, for 24 hours.

If the cap is hit by real use, Resend's paid tier ($20 a month for 50,000) lifts it: raise `DAILY_CAP` with it.

### Bounces and complaints (weekly, two minutes)

In Resend's dashboard, Emails, filter on **Bounced** and **Complained** for the last 7 days.

- Resend adds hard-bounced and complaining addresses to its suppression list by itself, so they aren't sent to
  again.
- A bounced verify email is usually a typo: the member's Setup row says "not verified yet", and they can fix it
  there. Nothing to do.
- Keep complaints under 0.1% of sends (Gmail's line is 0.3%) and hard bounces under 4%. Over that, stop and look at
  what's being sent before anything else goes.
- Read the DMARC reports (they go to `SUPPORT_EMAIL`) for anyone sending as the domain who isn't Resend.

The app doesn't receive Resend's webhooks yet. If bounces become routine, add a webhook endpoint that marks the
contact unverified.

### Rotating keys

- **`RESEND_API_KEY`:** make a new key in Resend (as in step 3), add it as a new version, then redeploy (run the
  deploy workflow from `main`, or `gcloud run services update registry-coffee --region us-central1` to start a new
  revision). Once an email goes out, delete the old key in Resend. Do it at once if the key may have leaked.
- **`EMAIL_KEY`:** never just replace it: every stored email is sealed under it. At a quiet time, from a laptop
  with `HF_TOKEN` and `COFFEEDATA_USERS` set. Keys go through files, never the command line or the screen:

  ```bash
  umask 077
  gcloud secrets versions access latest --secret EMAIL_KEY > old_key.txt
  openssl rand -base64 32 | tr -d '\n' > new_key.txt      # copy it into your password manager now
  EMAIL_KEY=$(cat old_key.txt) EMAIL_KEY_NEW=$(cat new_key.txt) uv run python -m app.rekey   # prints counts only
  EMAIL_KEY=$(cat new_key.txt) uv run python -m app.rekey --check   # "N of N emails open with this EMAIL_KEY."
  gcloud secrets versions add EMAIL_KEY --data-file=new_key.txt
  gcloud run services update registry-coffee --region us-central1   # restart on the new key at once
  shred -u old_key.txt new_key.txt
  ```

  Between `app.rekey` and the restart, the running app still holds the old key: an email added in that minute is
  sealed under it. Run `--check` again after the restart.
- **If `--check` reports emails that don't open** (an address added mid-rotation, or the wrong key deployed): the
  app treats each as "no email" and logs `an email doesn't open with this EMAIL_KEY` with the user id. With the key
  it was sealed under, run `app.rekey` again from that key to the current one. If that key is gone, the address
  can't be recovered: ask the member to add it again in Setup (adding replaces the unreadable record).

### When the app is slow and `/api/forgot` traffic is high

Every `/forgot` answer that gets past the limits waits until 2 s have passed (an async wait: it holds no worker
thread, so other routes go on), and its send runs in the thread pool. A flood of requests is answered at once once
past the per-IP limit. If the app is still slow:

1. `gcloud logging read 'resource.type="cloud_run_revision" AND jsonPayload.route="/api/forgot"' --limit 50 --freshness 1h`
   shows the volume and where it's from.
2. Many addresses at a low rate each are what the in-memory limits don't stop. Lower `FORGOT_PER_IP` or
   `NO_EMAIL_PER_HOUR` (app/accounts.py) and deploy, or turn email off (`email = false`) until it passes: members
   can still get a reset link from you.

### When Resend is down or refusing

**Quick checklist** for the "email not sent" alert (severity: a member can't recover their password by email; the
owner's reset link still works, so nobody is locked out):

1. `gcloud logging read 'resource.type="cloud_run_revision" AND jsonPayload.logger="app.mail"' --limit 20 --freshness 1d`
   says which: "the daily limit is reached", or a stack trace ending in Resend's status.
2. Resend's status page and its Emails log.
3. Act on the status: a `429` is Resend's rate limit or its daily quota (wait for tomorrow, or move to the paid
   tier); any other 4xx is the key (rotate it, above) or the domain (DNS changed, the domain no longer verified); a
   5xx or a timeout is Resend itself (each send was tried 3 times).
4. Check it's fixed: request a reset for your own username and see it arrive.

- **What people see meanwhile:** `/forgot` gives its usual reply, but no email arrives; a new member still joins,
  without a confirm email. Every unsent email logs "email not sent" at ERROR (the daily cap at WARNING), with
  Resend's status code and never the address.
- **Meanwhile:** the owner makes reset links on the People screen, as before email. Members can press "Send again"
  in Setup once it's back. Links that weren't sent don't need cleaning up: they expire.
- **For a long outage:** Postmark is the planned swap (spec §5). A `PostmarkMailer` is one class beside
  `ResendMailer` in `app/mail.py`, with its own key and DNS records.
- **To turn email off:** set `email = false` and apply. That unmounts both secrets and the features hide again.
  The secrets and their versions stay in Secret Manager (Terraform never destroys them), so stored emails come
  back when it's on again.

### Members' emails

- Stored only sealed (AES-256-GCM, `app/contacts.py`), in the users dataset's `contacts` table, with a keyed hash
  for lookup. Nobody reading the dataset sees an address. The app shows a member their own email, and the owner
  every member's, on the People screen.
- Removing or changing an email (both need the member's current password, and the old address is told), and
  `delete-user`, delete every file of the record in one commit, then squash the users dataset's history
  (`HfApi.super_squash_history`), so no old revision holds it. A squash is tried 3 times, then logged at ERROR.
- **If a commit fails.** The member is told it failed ("Something went wrong on our side. Try again."), and the
  dataset is as it was. The delete list waits in the staging folder (`deletes.pending.json`) and goes with the next
  commit on that instance. Staging is in the instance's memory, so the list is lost if the instance stops first.
  For that, every start checks the dataset: contact files from a member's older email (a different lookup hash
  from the current record's) are deleted in one commit and the history squashed. The log line is "deleted contact
  files left from changed emails".
- Logs never carry an address or a link: only which email it was and the user id.

**Roaster insights.** No report has been sold yet. The privacy policy lets a member turn off "Include my shots and
bags in roaster insights" (Setup → Account; on by default). The choice is in the latest `consents` row per member (`insights`, `insights_at`,
`insights_version`; null means never changed, which counts as on). There's no export job yet. When there is, it
must leave out every member with `insights = false` (`Accounts.insights_excluded()`), and keep a record of each report
it releases, as the policy says.

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
