# Coffee Data Tracker — Repo Review (Oct 2026)

> **Update:** the project's direction has changed to a combined data play: a daily roaster catalog pulled from Shopify `/products.json`, a shot log, and dbt + DuckDB, all on GitHub Actions. That design replaces the GCP stack reviewed below. [`MVP_PROMPT.md`](./MVP_PROMPT.md) now builds the new design. The review below is kept for history.

## TL;DR

The repo has the right idea: a web form, a Cloud Function, BigQuery, and Terraform. But **no data reaches BigQuery today, on `main` or on any branch.** On `main` the function writes to a hardcoded bucket called `"bucketname"`, the form posts to the literal string `"YOUR_CLOUD_FUNCTION_URL"`, and nothing connects GCS to BigQuery. The `feature/pubsub-dataflow-setup` branch gets closer, but it adds a hardcoded `/Users/...` path, a Pub/Sub schema that is never attached to the topic, and a 90-day partition expiration that would delete your history.

The quickest way to a working MVP is to **simplify, not add**. Use one Gen2 Cloud Function that validates the JSON and calls BigQuery `insert_rows_json`. Serve the form from that same function, so there is no CORS. Let Terraform zip the source and enable the APIs. Drop GCS, Pub/Sub, Avro, and the committed zip. A ready-to-paste prompt that does all of this is in [`MVP_PROMPT.md`](./MVP_PROMPT.md).

---

## 1. What's in the repo

| Path | What it does | State |
|---|---|---|
| `src/web/coffeetracker.html` + 3 JS files + CSS | Espresso shot form: roaster, roast, origin, grind, dose, yield, time, rating, latte, notes | Works as a page. Submit is broken. |
| `src/cloud_function/main.py` | HTTP function. Handles CORS and writes the raw JSON to GCS | Broken (hardcoded bucket) |
| `src/cloud_function/coffee-data-function.zip` | Hand-built deploy artifact | Stale (dated 2024-03-29), committed to git |
| `src/terraform/*.tf` | BQ dataset/table, 2 GCS buckets, Gen1 function, public invoker | Doesn't apply cleanly from a fresh clone |
| `docs/README.md` | Project story; "Getting Started" is a placeholder | Incomplete |
| branch `feature/pubsub-dataflow-setup` | Pub/Sub topic, BQ subscription, Avro schema | Unmerged, has blocking bugs (§4) |

## 2. Open issues and PRs

- **#7 — Public access for the Cloud Function not applied (bug).** The code has `google_cloudfunctions_function_iam_member` with `allUsers`, but it is on provider `~> 3.5` (2020-era) with no `project`/`region` on the resource. The usual causes are a provider/region mismatch, or an org policy (`iam.allowedPolicyMemberDomains`, "Domain restricted sharing") silently blocking `allUsers`. **Fix:** move to Gen2 (`google_cloudfunctions2_function`) and grant `roles/run.invoker` to `allUsers` on the underlying Cloud Run service with `google_cloud_run_service_iam_member`, with `project` and `location` set explicitly. If the project sits under an org, check that policy. The MVP prompt covers this.
- **#6 — Automate Cloud Function packaging (enhancement).** The bash + md5 tfvars plan in the issue works, but it isn't needed. Terraform's `data "archive_file"` zips `src/cloud_function/` at plan time. Naming the GCS object `function-${data.archive_file.src.output_md5}.zip` makes every code change redeploy automatically. Delete the committed zip and the script idea. The MVP prompt covers this.
- **PRs #1–#5:** all closed. The merge commits are on `main`. **PR #5's description says the function checks `Origin` against an allowed domain. The code doesn't:** it returns `Access-Control-Allow-Origin: *` unconditionally.
- **#8:** closed, unrelated.

## 3. Bugs on `main` (by severity)

**Blockers: data never lands**
1. `main.py:27`: `client.bucket("bucketname")` is hardcoded. Terraform passes `GCS_BUCKET`, but the code never reads it.
2. `formscript.js:24`: `fetch("YOUR_CLOUD_FUNCTION_URL", …)` is a placeholder.
3. `formscript.js:31`: `response.json()` on a plain-text `"Success"` body throws, so every successful submit ends up in `.catch` as an "Error". The user also gets no on-screen feedback either way.
4. Nothing moves data from GCS into BigQuery. No load job, no external table, no trigger.
5. `gcs_buckets.tf:12`: `source = "./coffee-data-tracker/src/..."` is relative to the wrong directory. It fails from `src/terraform/`.
6. Schema mismatch between the form and the BQ table:
   - `submitted_on` is `REQUIRED`, but the form never sends it. The form sends `created_at`/`received_at`.
   - `grinder_setting` is `INTEGER`, but the form sends values like `"15.25"`.
   - The table has no `is_latte` column.
   - All values arrive as strings (`"18.0"`, `"true"`).

**Infra / config**

7. Provider `hashicorp/google ~> 3.5` is about five major versions old. Current is 6.x/7.x.
8. `runtime = "python39"`: Python 3.9 reached end of life in Oct 2025, and GCF is deprecating it. Use `python312` or newer.
9. Gen1 Cloud Functions: Google steers all new work to Gen2 (Cloud Run functions).
10. No `google_project_service` resources, so a fresh project fails until you hand-enable the APIs (cloudfunctions, run, cloudbuild, artifactregistry, bigquery, storage).
11. `variables.tf` declares `region`, `runtime`, and others that are **never used**. `project` is hardcoded in `provider.tf`.
12. Buckets have no `location` (defaults to US multi-region) and no `uniform_bucket_level_access`.
13. `requirements.txt` pulls `pandas` and `pyarrow`, which are unused and add about 100MB and slow cold starts. It pins `flask==2.1.2`/`werkzeug==2.2.2`, which can conflict with the runtime. It omits `functions-framework` and `google-cloud-bigquery`.

**Form / JS** (confirmed by running the loops in Node)

14. `doseselector.js`: floating-point drift. `17.5 += 0.1` fifty times ends the list at **22.4**, never 22.5. Loop over integers instead (`for (i = 175; i <= 225; i++) i/10`).
15. `doseselector.js:24` and `grindersetting.js:27`: `removeChild(select.firstChild)` removes the **whitespace text node**, not the placeholder `<option>`. Use `select.querySelector('option[value=""]').remove()`.
16. `grindersetting.js:24`: `select.value = 15.00` assigns `"15"`, which matches no option (`"15.00"`), so nothing is preselected.
17. Grinder dropdown has **401 options** (0–100 in 0.25 steps). Use `<input type="number" step="0.25">`.
18. `coffee_yield` is `type="text"`. Make it `type="number" step="0.1"`. Add `min`/`max` for `shot_duration`.
19. The radio labels all use `for="coffee_roast"`/`for="coffee_rating"`, which point at a `<div>`. That's invalid HTML and an a11y problem, but harmless because the inputs are nested.
20. The roaster and origin lists are long hardcoded `<option>` blocks with no "Other" option. A `<datalist>` gives you suggestions plus free text.
21. Client-side `received_at` is wrong by definition: the server should stamp it.

**Repo hygiene**

22. `.gitignore` has `*.json`, which ignores *every* JSON file (schemas, `package.json`, fixtures). The `!src/cloud/...` negations point at a directory that doesn't exist. Ignore `*.tfvars`, `*-key.json`, and `credentials*.json` instead.
23. `.DS_Store` is committed (`src/.DS_Store`, plus root on the branch). Add `.DS_Store` to `.gitignore` and `git rm --cached` them.
24. The built `.zip` is committed.
25. No tests, no CI, no `terraform fmt`/`validate`, no LICENSE. The README "Getting Started" is a placeholder.

**Security**

26. Unauthenticated public endpoint with `Access-Control-Allow-Origin: *` and **no validation**. Anyone can write arbitrary JSON of any size into your bucket. For a personal tracker:
    - validate types and ranges server-side
    - cap the body size
    - restrict CORS to your own origin(s) via an env var
    - add a simple shared secret (`SUBMIT_TOKEN`) checked in a header or hidden field

    It isn't real auth, but it stops drive-by spam. Also set `max_instance_count` low (e.g. 2) to cap cost.

## 4. The unmerged `feature/pubsub-dataflow-setup` branch

The direction is good: typed schema, column descriptions, partitioning. Don't merge it as-is:
- `gcs_buckets.tf`: `source = "/Users/jasontouleyrou/Projects/..."` is an absolute local path.
- `time_partitioning.expiration_ms = 7776000000` **auto-deletes rows after 90 days.** That's wrong for a personal history log.
- `use_topic_schema = true`, but the topic has no `schema_settings { schema = … encoding = "JSON" }`, so the BQ subscription won't map fields as intended. If you use a topic schema, string-typed form values also fail Avro validation.
- `data.avro` (a binary test file) and `.DS_Store` are committed.
- A plain `coffee-data-subscription` pull subscription with no consumer just accumulates messages.
- The code still dual-writes to GCS.

**Recommendation:** for one user and a few shots a day, Pub/Sub adds moving parts without real benefit. A direct `insert_rows_json` from the function is simpler and has fewer failure points. Keep the branch's schema descriptions and partitioning (without expiration) and close the branch. Revisit Pub/Sub only if you add more producers, such as a smart scale or a Decent/Gaggiuino integration.

## 5. Similar open-source projects, and what to borrow

| Project | What it is | Ideas worth stealing |
|---|---|---|
| **Beanconqueror** (`graphefruit/Beanconqueror`) | The largest FOSS coffee app (Ionic/Angular; iOS/Android) | Tracks **beans as entities** (roaster, roast date, bag) separately from **brews**. Days-off-roast. Bluetooth scale support. Import/export. Visualizer sync. |
| **Visualizer** (`miharekar/visualizer`, visualizer.coffee) | Rails app for sharing and comparing espresso shots, started for Decent `.shot` files | Shot comparison. Public share links. A brew-ratio display front and center. An open upload API you could push shots to. |
| **b{rew}log** (`jnsgruk/brewlog`, 2025) | Self-hosted Rust coffee log | Roasters → roasts → bags → brews → equipment data model. Built specifically so the data is *queryable and backed up*, which is the same motivation as this repo. |
| **Coffee Log** (F-Droid, GPLv3) | Offline Android espresso log | A field set almost identical to yours, plus **water temperature**. JSON export/import. |
| **Gaggiuino** + Gaggiuino MCP / **Decent DE1** "Describe Your Espresso" | Machine firmware with shot telemetry | Later: ingest pressure/flow curves automatically instead of typing them. |

The fields you're missing, by consensus of those apps:
- `brew_ratio` (compute `yield/dose` in BigQuery)
- `roast_date` → `days_off_roast`
- `bean_name`/`lot`
- `water_temp_c`
- `brew_method` (espresso, pour-over, …)
- `grinder` and `machine` (equipment)
- tasting descriptors (sour, bitter, balanced)

That last one is what makes dial-in analysis possible.

What this repo can offer that those apps don't is **your data in BigQuery and SQL**: Looker Studio dashboards, grind-vs-rating regressions, "best settings per roaster". Lean into that.

## 6. Quick wins, in order

1. **Make the pipeline work end-to-end** (the MVP prompt): Gen2 function → BigQuery direct insert, form served from the function, `archive_file` packaging, API enablement, `roles/run.invoker` for `allUsers`, typed schema. This closes #6 and #7.
2. Fix the form bugs (§3, items 14–18) and add success/error toasts.
3. Server-side validation, a CORS allowlist, an optional `SUBMIT_TOKEN`, and `max_instance_count`.
4. Repo hygiene: fix `.gitignore`, remove `.DS_Store` and the zip, add a LICENSE, write a real README quickstart, add a `Makefile`/`deploy.sh`.
5. CI: a GitHub Action with `terraform fmt -check`, `terraform validate`, `ruff`, and `pytest` for the validator.
6. Analytics: a `v_shots` view with `brew_ratio`, plus a Looker Studio dashboard linked from the README.
7. Data model v2: add `roast_date`, `water_temp_c`, `brew_method`, and `bean_name`, and turn roaster/origin into datalists.
8. Later: remote Terraform state in GCS, Workload Identity Federation for CI deploys, Visualizer/Beanconqueror import.

## Sources
- [Beanconqueror on Home-Barista](https://www.home-barista.com/knockbox/beanconqueror-app-t68236-60.html), [Kaffeemacher overview](https://kaffeemacher.de/en/blogs/kaffeewissen/beanconqueror), [Decent: Beanconqueror and the Decent Scale](https://decentespresso.com/blog/beanconqueror_and_the_decent_scale)
- [Decent: Upload to Visualizer extension](https://decentespresso.com/blog/how_to_use_the_upload_to_visualizer_extension_in_the_de1_app), [Decent: Describe Your Espresso](https://decentespresso.com/blog/how_to_use_the_describe_your_espresso_extension_in_your_de1_app), [Decent .shot Visualizer](https://public.3.basecamp.com/p/3bpv4ybEVekQVXy7Ud9vW6n1)
- [b{rew}log: coffee and agents](https://jnsgr.uk/2026/03/brewlog-coffee-and-agents/)
- [Coffee Log on F-Droid](https://f-droid.org/packages/io.github.adrian2414745.coffeelog/)
- [Gaggiuino MCP](https://mcpservers.org/servers/AndrewKlement/gaggiuino-mcp), [coffee-machine topic on ecosyste.ms](https://repos.ecosyste.ms/topics/coffee-machine)
