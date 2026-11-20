# admin.registry.coffee

The owner's command center: read-only pages over what the pipeline wrote to the lake, behind passkey sign-in. It is
the first slice of `docs/design/admin-registry.md` (#112): the Week, Failures, Gate, Changes and Catalog pages, the
service and its sign-in. It writes nothing to the lake, and nothing about the data anywhere: drafts, decisions and
jobs come later. The pages' design is in [DESIGN.md](DESIGN.md).

## Pages

Each is a plain query over named lake files, and says which ones under its answer line.

| Page | Answers | Reads |
|---|---|---|
| Week | Did the week land: each connector's passes, merge, enrich, classify and the build, and the served release | `job_manifests`, `builds`, `serving_log`, `marts/release.parquet` |
| Failures | Which stores failed and why: by HTTP status, challenge (SiteGround, Cloudflare), partial and quarantined, and the trend across weeks | `fetches`, the week's build's ops `run_completeness` |
| Gate | The build's data tests, failing first, and its metrics against the previous passing build | `builds` |
| Changes | What the build's public tables change against the served release, by table, change and reason, `unexplained` marked | ops `release_changes` |
| Catalog | Every product, coffee or not, with search and facet filters | ops `product_decisions` and the release's `dim_coffee` |

The Catalog copies a build's two files into a local DuckDB file once (releases are immutable) and filters there.
Failures, Changes and the full Catalog need a build made after #124 (the ops tables); for older builds the pages say
so, and Failures counts from `fetches` by the same challenge rule as dbt's `stg_fetches`.

The lake is opened with `Lake(read_only=True)`: nothing is created (not even the schema files a writer adds), every
write method raises `ReadOnlyLake`, and a remote lake is read directly, never through a staging folder.
`tests/test_admin.py` and `tests/test_admin_browser.py` check that the lake's files are unchanged after every page.

## Sign-in

Passkeys only (WebAuthn, the `webauthn` package), no password:
- **Enrollment, once.** While no passkey is registered, `/enroll` takes the `ADMIN_ENROLL_TOKEN` secret and
  registers the first passkey. It shows ten recovery codes, once. That registration closes enrollment for good,
  whatever the secret still holds.
- **The second passkey** is added from Account within 10 minutes of signing in, which is the enrollment session in
  practice. After two, another passkey needs a recovery code.
- **Recovery:** a recovery code registers a new passkey, retires every earlier passkey (recovery means they are
  lost) and signs out every session. Each code works once. Then add the second passkey again from Account within 10
  minutes. Lost only one passkey? Recover from a device you control: it retires both, then add the other again.
  New recovery codes come with the write API; Account shows how many are left.
- **Sessions** last 12 hours from sign-in and never renew. Sign out revokes the session on the server; "Sign out
  everywhere" revokes them all.
- **Limits:** failed tokens, codes and passkeys are counted per address and overall, and once over the limit
  nothing more is checked for 15 minutes.
- **Every route but `/health`, the sign-in pages and their API, and static files needs a session**, checked before
  any route runs, so a new route is protected by default. A missing or weak setting switches the whole service off
  (503), and it never falls back to showing data. No setting or environment variable turns sign-in off.

The sign-in state is kept as one JSON record per change, each created once and never replaced: in a Cloud Storage
bucket when hosted, in a local folder on a laptop. It is not lake data.

## Settings

| Variable | What | Hosted |
|---|---|---|
| `ADMIN_ORIGIN` | the admin's origin, e.g. `https://admin.registry.coffee`; the passkeys' relying party | required, https |
| `ADMIN_SESSION_SECRET` | 32+ random characters; signs the session cookie | required (Secret Manager) |
| `ADMIN_ENROLL_TOKEN` | 32+ random characters; enrolls the first passkey, once | required until enrolled (Secret Manager) |
| `ADMIN_STATE_BUCKET` | the bucket that holds the sign-in state | required |
| `ADMIN_STATE_DIR` | a local folder instead, on a laptop only | refused |
| `HF_TOKEN` | a fine-grained Hugging Face token with read access to the lake dataset only (secret `ADMIN_HF_TOKEN`) | required |
| `COFFEEDATA_LAKE` | the lake (the image's default is the production dataset) | |
| `ADMIN_CACHE_DIR` | where the Catalog's per-build files go | `/tmp/admin-cache` |

## Running it locally

```sh
export ADMIN_SESSION_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export ADMIN_ENROLL_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
make admin   # http://localhost:8001 over ./lake (COFFEEDATA_LAKE), state in build/admin-state
```

Open `http://localhost:8001` (passkeys need `localhost` or https), enroll with the token, and save the recovery
codes. Delete `build/admin-state` to start over.

## Tests

- `tests/test_admin.py`: every query on a small synthetic lake, the Catalog's cache and facets, sign-in end to end
  with a software authenticator, the state store, the settings, a walk over every route signed out (only the
  public ones answer), and that no page writes to the lake.
- `tests/test_admin_browser.py` (Node with Playwright, and dbt): builds a three-week lake through
  `coffeedata build` (`tests/admin_fixture.py`), then drives every page and state with WebAuthn virtual
  authenticators, at 390 px and 1,280 px, light and dark, with axe. `ADMIN_SCREENSHOTS=<dir>` keeps the
  screenshots.

## Deploying

Terraform: `src/terraform/admin.tf`, with the service, its own service account, the three empty secrets, the state
bucket, the domain mapping, and three alerts (errors, passkeys changed, and the cold start that revisits the
language, §7.1 of the design). The owner applies a reviewed plan, in the order below.
The service's first apply runs Google's sample image with no command. `deploy-admin` in
`.github/workflows/deploy.yml` then deploys the app's image with the admin's entrypoint
(`sh -c "exec uvicorn --factory admin.main:create_app ..."`), and Terraform leaves the image, command and args to it.

### The first apply, in order

The service reads its three secrets at version `latest`, and Cloud Run refuses to create a service whose secret has
no version. Terraform makes the secrets empty (their values are never in Terraform or its state), so the service
can't come in the same apply. `admin_enabled` (`src/terraform/ci.tfvars`, off by default) holds back the service
and what names it: its public invoker, the domain mapping and the alerts.

1. **The first apply, with `admin_enabled = false`** (as merged). It makes the account `registry-coffee-admin-run`
   (unless it is already there), the three empty secrets with the account's read grant on each, the storage API,
   and the state bucket with its two grants. No service yet.
2. **Add the three secret values**, from your laptop or Cloud Shell, never in a file in the repository:

   ```bash
   python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' | gcloud secrets versions add ADMIN_SESSION_SECRET --data-file=-
   python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' | tee /dev/tty | gcloud secrets versions add ADMIN_ENROLL_TOKEN --data-file=-   # note it: it enrolls the first passkey
   gcloud secrets versions add ADMIN_HF_TOKEN --data-file=-   # paste, then Ctrl-D twice
   ```

   `ADMIN_HF_TOKEN` is a fine-grained Hugging Face token with read access to the lake dataset only, never the
   app's `HF_TOKEN`.
3. **A small PR sets `admin_enabled = true`** in `src/terraform/ci.tfvars`, and nothing else. Its plan adds six
   resources: the service, its invoker, the domain mapping and the three alerts. Before it merges, the domain is
   verified for the account that applies and its CAA record allows `pki.goog`, as for the app
   (docs/app/RUNNING.md, Owner's steps, 3). Never before step 2: Cloud Run refuses the service and the apply stops
   there.
4. **Apply** (that PR's merge). The service starts on the sample image. Then DNS: `CNAME admin ->
   ghs.googlehosted.com.`
5. **The deploy account's two grants, by hand** (the plan guard refuses them in Terraform), each on one resource
   only. Cloud Run Developer on the admin service, and Service Account User on `registry-coffee-admin-run`:

   ```bash
   gcloud run services add-iam-policy-binding registry-coffee-admin --region us-central1 \
     --member "serviceAccount:registry-coffee-deploy@<project>.iam.gserviceaccount.com" --role roles/run.developer
   gcloud iam service-accounts add-iam-policy-binding registry-coffee-admin-run@<project>.iam.gserviceaccount.com \
     --member "serviceAccount:registry-coffee-deploy@<project>.iam.gserviceaccount.com" --role roles/iam.serviceAccountUser
   ```

   Never on the project. Without them `deploy-admin` fails with a 403.
6. **Set the repository variable `GCP_ADMIN_SERVICE`** to `registry-coffee-admin`. The next deploy run puts the
   app's image on the admin with its entrypoint. Then open `https://admin.registry.coffee` and enroll.
