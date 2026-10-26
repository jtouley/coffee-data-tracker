# One-shot MVP prompt

Paste everything inside the fence below into Claude Code, run from the root of this repo. It is self-contained: it describes the target state, the exact files, and the acceptance checks. When it finishes you run three commands (see "After it finishes").

````text
You are working in the coffee-data-tracker repo (GCP + Terraform espresso shot logger).
Goal: make it a working MVP in ONE pass. A user on their phone opens a URL, fills the
shot form, taps Submit, sees a success message, and the row is queryable in BigQuery.
Read docs/REVIEW.md first; it lists every known bug. Replace the current broken
GCS-only path with this simpler architecture. Do not use Pub/Sub, Avro, or GCS for data.

ARCHITECTURE
- One Cloud Functions **Gen2** HTTP function (Python 3.12, `google_cloudfunctions2_function`, entry point `app`).
  - GET  /         -> serves the form (index.html with inline CSS+JS), so it's same-origin, no CORS needed.
  - POST /api/shots -> validates JSON, stamps `received_at` + `shot_id` (uuid4) server-side,
                       inserts with `bigquery.Client().insert_rows_json(TABLE, [row], row_ids=[shot_id])`.
                       Returns JSON `{"ok": true, "shot_id": ...}` (201) or `{"ok": false, "errors": {...}}` (400).
  - OPTIONS        -> CORS preflight, but only for origins in env `ALLOWED_ORIGINS` (comma-separated,
                       default empty) so the form can still be embedded on Squarespace later.
  - Optional env `SUBMIT_TOKEN`: if set, POST must carry header `X-Submit-Token` equal to it (401 otherwise).
    The form reads the token from `?t=` in the page URL, stores it in localStorage, and sends it.
  - Reject bodies > 10 KB (413). Log errors with `logging`, never echo stack traces.
- BigQuery dataset `coffee_data`, table `shots`, DAY-partitioned on `brewed_at`, **no partition expiration**,
  clustered by `coffee_roaster`. Plus a view `v_shots` adding `brew_ratio = SAFE_DIVIDE(coffee_yield_g, coffee_dose_g)`
  and `days_off_roast = DATE_DIFF(DATE(brewed_at), roast_date, DAY)`.

SCHEMA (single source of truth: `src/cloud_function/schema.py`, mirrored in `src/terraform/schema/shots.json`;
add a test asserting they match)
  shot_id STRING REQUIRED | brewed_at TIMESTAMP REQUIRED (client sends; default now) |
  received_at TIMESTAMP REQUIRED (server) | coffee_roaster STRING REQUIRED | bean_name STRING |
  coffee_origin STRING | coffee_roast STRING REQUIRED (Light|Medium|Dark|Decaf) | roast_date DATE |
  brew_method STRING REQUIRED default "Espresso" | grinder_setting FLOAT REQUIRED (0–100) |
  coffee_dose_g FLOAT REQUIRED (5–30) | coffee_yield_g FLOAT REQUIRED (5–500) |
  shot_duration_s FLOAT REQUIRED (1–600) | water_temp_c FLOAT (70–100) |
  coffee_rating INTEGER REQUIRED (1–5) | is_milk_drink BOOLEAN REQUIRED | notes STRING (≤2000 chars)
Validation coerces strings ("18.0", "true", "on") to the right types and rejects unknown fields.

FILES TO PRODUCE (delete what's obsolete)
- src/cloud_function/main.py, schema.py, validate.py, index.html, requirements.txt
  (only: functions-framework==3.*, google-cloud-bigquery==3.*), tests/test_validate.py (pytest, no GCP needed;
  cover coercion, ranges, missing required, unknown field, oversize notes), requirements-dev.txt (pytest, ruff).
- index.html: mobile-first, accessible labels, no external JS. Fields as above, using:
  - `<datalist>` for roaster and origin (seed with the existing lists from src/web/coffeetracker.html)
  - `<input type=number step=0.25>` for grind, `step=0.1` for dose/yield
  - a built-in shot timer button (start/stop fills shot_duration_s)
  - a live brew-ratio display ("1:2.1")
  - remember the last roaster/bean/grind/dose in localStorage, wrapped in try/catch
  - an on-screen success/error message; the form resets but keeps the remembered fields
  The JS posts to the relative URL `api/shots` and parses JSON responses.
- Remove src/web/ (folded into index.html), src/cloud_function/coffee-data-function.zip, all .DS_Store files.
- src/terraform/:
  - versions.tf: terraform >= 1.6, google ~> 6.0, archive ~> 2.4
  - providers.tf: project/region from variables
  - variables.tf: project_id (required), region default us-central1, allowed_origins default "", submit_token
    default "" sensitive, max_instances default 2 — every variable actually used
  - apis.tf: google_project_service for cloudfunctions, run, cloudbuild, artifactregistry, bigquery, storage,
    logging, iam; disable_on_destroy=false; other resources depend_on these
  - bigquery.tf: dataset + table from schema/shots.json + v_shots view; deletion_protection=true on the table
  - function.tf:
    - `data "archive_file"` zipping ../cloud_function (excluding tests/, __pycache__)
    - a uniform-access source bucket with a 30-day lifecycle rule
    - object name `function-${md5}.zip` so code changes redeploy automatically (closes issue #6)
    - a dedicated service account with roles/bigquery.dataEditor on the dataset only, plus roles/bigquery.jobUser on the project
    - the gen2 function with env TABLE_ID, ALLOWED_ORIGINS, SUBMIT_TOKEN; max_instance_count=var.max_instances;
      256M memory; ingress ALLOW_ALL
    - `google_cloud_run_service_iam_member` granting roles/run.invoker to allUsers on the function's service, with
      project+location set explicitly (closes issue #7; if apply fails with an org-policy error, note
      iam.allowedPolicyMemberDomains in the README)
  - outputs.tf: function_url, form_url (with `?t=` appended when submit_token is set; mark sensitive), bigquery_table
  - terraform.tfvars.example
- Makefile (or scripts/deploy.sh): `make test` (ruff + pytest), `make deploy` (terraform init/apply
  -var-file=terraform.tfvars), `make local` (functions-framework --target=app --debug with TABLE_ID from env),
  `make smoke URL=...` (curl a valid POST and an invalid POST, assert 201 and 400).
- .gitignore: drop the `*.json` rule; ignore .terraform/, *.tfstate*, *.tfvars (not *.example), *.zip,
  .DS_Store, __pycache__/, .venv/, *-key.json, credentials*.json.
- .github/workflows/ci.yml: on PR and push, run ruff, pytest, terraform fmt -check, terraform init -backend=false, terraform validate.
- README.md at the repo root: a 5-step quickstart
  1. gcloud auth application-default login
  2. create or choose a project with billing enabled
  3. cp terraform.tfvars.example terraform.tfvars
  4. make deploy
  5. open form_url on your phone and add it to the home screen

  Include a sample SQL query against v_shots (best grind per roaster by average rating), and a "costs: ~$0/month
  at personal volume" note. Move the project journey from docs/README.md under a "History" heading.

CONSTRAINTS
- Do not hardcode project IDs, bucket names, absolute paths, or URLs anywhere.
- Keep it small: no frameworks, no Pub/Sub, no GCS for data, no Docker.
- Code style: type hints, small pure functions; validate.py must not import google libs.

ACCEPTANCE (run these yourself before finishing and fix until green)
1. `ruff check src/cloud_function && pytest -q src/cloud_function` passes.
2. `cd src/terraform && terraform fmt -check && terraform init -backend=false && terraform validate` passes.
3. Run the function locally with BigQuery insert monkeypatched (add a `DRY_RUN=1` env that logs the row
   instead of inserting):
   - GET / returns the HTML
   - a valid POST to /api/shots returns 201 + shot_id
   - an invalid POST returns 400 with field errors
   - a POST without the token when SUBMIT_TOKEN is set returns 401
   Exercise the form with Playwright: fill it, submit, assert the success message shows.
4. `git grep -nE "YOUR_CLOUD_FUNCTION_URL|bucketname|/Users/"` returns nothing.
Then commit with a clear message and push to the current branch. In the final message, list the exact
commands I need to run to deploy, plus anything you could not verify without GCP credentials.
````

## After it finishes

```bash
gcloud auth application-default login
cp src/terraform/terraform.tfvars.example src/terraform/terraform.tfvars   # set project_id
make deploy && make smoke URL=$(terraform -chdir=src/terraform output -raw function_url)
```

Then open `terraform -chdir=src/terraform output -raw form_url` on your phone and add it to your home screen.
