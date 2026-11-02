# Ingestion on Cloud Run: runbook

The owner's steps for the confirmation run, the cutover and a rollback. The design is in `DESIGN.md`, and the
infrastructure is in `src/terraform/ingestion.tf`. Every command runs from the repository root unless it says
otherwise.

```sh
export PROJECT=coffee-data-tracker REGION=us-central1
export IMAGE=$REGION-docker.pkg.dev/$PROJECT/registry-coffee/ingest:latest
export WEEK=2026-11-02   # the confirmation run's week (§3)
```

## 0. Before you start

- `gcloud auth login` and `gcloud auth application-default login`, as an account that can apply the app's Terraform.
- Terraform ≥ 1.6. No local Docker is needed: Cloud Build builds the image (§1).
- A checkout of this repository with [`uv`](https://docs.astral.sh/uv/) and `uv sync` run once: the results script
  in §3 runs with `uv run`. `jq` for the network check in §2 (Cloud Shell has it).
- Secret Manager read access for your account on `HF_TOKEN` (`roles/secretmanager.secretAccessor`): §3 reads it with
  `gcloud secrets versions access` to query the lake.
- `HF_TOKEN` and `ANTHROPIC_API_KEY` already hold values in Secret Manager: they are the app's secrets
  (`secrets.tf`). Terraform only references them by name and never sets a value.
- `src/terraform/terraform.tfvars` exists (copy `terraform.tfvars.example`). For verdicts in the build job, add
  `verdict_model = "<the VERDICT_MODEL repository variable's value>"`. Leave it out for rules only.

## 1. Build the image

The jobs reference `$IMAGE` (`.../registry-coffee/ingest:latest`) until `ingest_image` pins a digest (§4), so **the
image must exist before the first apply**: a job whose image is missing fails to create. The app's Artifact Registry repository already exists.

Build with Cloud Build (`gcloud builds submit`), because pushes from Cloud Shell were refused. Its docker step runs
with `DOCKER_BUILDKIT=1`: only BuildKit reads `ingest.Dockerfile.dockerignore`, which keeps dbt/, config/ and
contracts/ in the image (the app's `.dockerignore` leaves them out). The upload follows `.gitignore`, so the local
lake and build/ stay behind.

```sh
cat > /tmp/ingest-build.yaml <<'YAML'
steps:
  - name: gcr.io/cloud-builders/docker
    env: ["DOCKER_BUILDKIT=1"]
    args: ["build", "-f", "ingest.Dockerfile", "-t", "$_IMAGE", "."]
images: ["$_IMAGE"]
YAML
gcloud builds submit --project "$PROJECT" --config /tmp/ingest-build.yaml --substitutions "_IMAGE=$IMAGE" .
gcloud artifacts docker images describe "$IMAGE" --format='value(image_summary.digest)'   # it exists
```

**A build alone changes no job.** Cloud Run resolves an image tag to a digest when a job is created or updated, not
on each execution, so after a rebuild every job keeps running the digest it was last created or updated with. To move
the jobs to a new build, pin it by digest and apply (`-var ingest_image=<repo>/ingest@sha256:<digest>`, §4 step 2):
every job then runs the same image, and the plan shows the change.

## 2. Init, plan, apply

**Already applied in production** (the image existed first). The Shopify egress IP is **34.172.156.113**, and every
job runs `$IMAGE`. This section is for a fresh project, or to check that the live state still matches.

- **In production**, the crawl jobs' `max_retries` is 0 for Shopify's (`crawl-shopify`, its default-egress arm,
  `crawl-retry`) and 1 for the other four since the crawl-hardening PR (section 7); `crawl-generic` has 8Gi. A plan
  that shows anything else on a job, beyond the image: stop.
- **Before applying in production, check the live NAT** against `ingestion.tf` (dynamic ports on,
  endpoint-independent mapping off, 1,024 to 8,192 ports per VM), so a NAT updated by hand without
  `--no-enable-endpoint-independent-mapping` doesn't show up as unexplained drift:

  ```sh
  gcloud compute routers nats describe registry-coffee-shopify-egress --router=registry-coffee-ingest \
    --region=us-central1 \
    --format='value(enableDynamicPortAllocation,enableEndpointIndependentMapping,minPortsPerVm,maxPortsPerVm)'
  ```

  It should print `True False 1024 8192`. Anything else: run the §3 `nats update` command first, then plan again.
- **In a fresh project**, expect the plan below.

```sh
cd src/terraform
terraform init -backend-config="bucket=coffee-data-tracker-tfstate"
terraform plan -var ingest_schedule_enabled=false -out ingest.plan
```

**Expected plan in a fresh project: 38 to add, 0 to change, 0 to destroy.** The app's own resources don't change.

| Resources | Count |
|---|---|
| `google_project_service.ingest_apis` (compute, cloudscheduler, storage) | 3 |
| `google_compute_network.ingest`, `google_compute_subnetwork.shopify_egress`, `google_compute_router.ingest`, `google_compute_address.shopify_egress`, `google_compute_router_nat.shopify_egress` | 5 |
| `google_storage_bucket.ingest_staging` + `google_storage_bucket_iam_member.ingest_staging` | 2 |
| `google_service_account.ingest`, `google_service_account.ingest_scheduler` | 2 |
| `google_secret_manager_secret_iam_member.ingest` (HF_TOKEN, ANTHROPIC_API_KEY) | 2 |
| `google_cloud_run_v2_job.ingest`: discover, probe, crawl-shopify, crawl-shopify-default-egress, crawl-retry, crawl-squarespace, crawl-woocommerce, crawl-square-online, crawl-generic, merge, enrich-week, build | 12 |
| `google_cloud_run_v2_job_iam_member.scheduler` (one per job) | 12 |
| `google_cloud_scheduler_job.ingest` | **0** (the schedule is off) |

Stop if the plan changes or destroys anything else, or creates a secret version.

```sh
terraform apply ingest.plan
terraform output shopify_egress_ip       # the one IP Shopify sees: keep it for Shopify's form and Support
terraform output ingest_jobs
cd ../..
```

Check that only crawl-shopify and crawl-retry leave through the NAT. `gcloud run jobs describe` returns the v1 (Knative)
form of the job, where Direct VPC egress is the `run.googleapis.com/vpc-access-egress` annotation on the execution
template (`spec.template.metadata.annotations`, as in Google's
[Direct VPC egress for jobs](https://cloud.google.com/run/docs/configuring/vpc-direct-vpc) page), not a
`vpcAccess` field:

```sh
for j in crawl-shopify crawl-retry crawl-shopify-default-egress; do
  echo "$j: $(gcloud run jobs describe $j --region $REGION --format=json \
    | jq -r '.spec.template.metadata.annotations["run.googleapis.com/vpc-access-egress"] // "(none)"')"
done   # all-traffic, all-traffic, (none)
```

If the first two print `(none)` as well, look at the whole template before trusting either answer:
`gcloud run jobs describe crawl-shopify --region $REGION --format=yaml | grep -i -A2 vpc`.

## 3. Confirmation run: Shopify, three arms (today)

Ran on 2026-11-01 (salt `confirm-2026-11-01`, week 2026-11-02, image 7419da12). The results and the decision are in
DESIGN.md §10.1. The steps below are kept for a repeat.

Each arm crawls one third of the Shopify roasters, chosen by a salted hash, so the arms' stores don't overlap. From
this commit on, the hash is of the roaster's **host**, so roaster_ids that share one store (24 hosts serve 49 of the
1,070 Shopify roaster_ids) are always in the same arm. An image built before it (79464cd) hashes `roaster_id`: with
`confirm-2026-11a`, 15 shared hosts fall in two arms, a small overlap to note with today's results. Never resume or
re-run one salt's arms on a different image: the thirds differ. Per-store windows can't carry over from one arm to another; only a shared egress IP can. So:
- **arms 1 and 2 run at the same time**: default egress and the NAT IP are different IPs;
- **arm 3 starts 30 min after arm 2 ends**: it shares arm 2's NAT IP (34.172.156.113), and the gap lets an IP-wide
  window from arm 2 reset. Arm 1 may still be running then, on another IP.

All three run today, once the image from §1 is built. The lake already has history (product counts), so v2's
short-page stop works in every arm.

**Before the first arm, turn retries off** for the two jobs (Terraform sets `max_retries = 0` for Shopify's crawl jobs;
this is the same setting, by hand, for an image built earlier):

```sh
for j in crawl-shopify crawl-shopify-default-egress; do
  gcloud run jobs update $j --region $REGION --max-retries 0
done
```

A second attempt would re-ask the stores that already refused, in a new 225-min window, and add a second end row
with the same `detail.arm`.

**Give the NAT enough ports.** On the first try arm 2 crashed reading the hf:// lake ("Could not connect to server"
on a HEAD to huggingface.co): its egress is all traffic through the NAT, DuckDB's parallel range reads of the lake go
through it too, and the default of 64 static ports per VM most likely ran out. Terraform now sets dynamic port
allocation (1,024 to 8,192 ports per VM, endpoint-independent mapping off, which dynamic allocation requires); by hand,
for a NAT applied earlier:

```sh
gcloud compute routers nats update registry-coffee-shopify-egress --router registry-coffee-ingest \
  --region $REGION --enable-dynamic-port-allocation --min-ports-per-vm=1024 --max-ports-per-vm=8192 \
  --no-enable-endpoint-independent-mapping
```

(Check the router and NAT names with `gcloud compute routers list` and `gcloud compute routers nats list --router
<router> --region $REGION`.) From this commit on, a job's first reads of the lake (the registry state, and what this
run already fetched) also retry a dropped connection three times (after 2, 6 and 18 s) before failing.

**Pass `--run-date $WEEK` (2026-11-02), not today's date.** A crawl works on the week of its run date, and skips
every store the lake already has an ok fetch for under that run_date (an exact match on the Monday, not any day of
the week). Today's week (2026-10-26) is already crawled, so without
it an arm would find almost nothing to do. 2026-11-02 has no fetches in the lake yet. Each arm writes only to its own
sub-partition in the staging bucket, `raw/connector=shopify/run_date=2026-11-02/slice=k-of-3/`: concurrent arms never
compact each other's files, and the weekly merge never reads a slice. The arms' `job_manifests` rows go to the lake
under 2026-11-02; Monday's GitHub weekly run doesn't read them, and from this commit on an arm's row (one with
`detail.slice` or `detail.arm`, from any image) never counts as the week's Shopify crawl in `merge` and never feeds
`egress-check`. Don't run merge or crawl-retry during the confirmation run. **Monday's GitHub Actions weekly run
stays as the fallback** (09:17 UTC on 2026-11-02). Nothing enforces that it starts after the arms end, so keep to the
latest start time below.

Choose a salt and keep it with the results. `arms.sh` takes it from the environment or as its first argument
(`export SALT=confirm-2026-11-01`, or `sh arms.sh confirm-2026-11-01`), never a hardcoded default, so a re-run with a
new salt can't reuse the old one by accident.

| Arm | Starts | Job (network) | Slice | Rate |
|---|---|---|---|---|
| 1 | with arm 2 | `crawl-shopify-default-egress` (default egress) | 0/3 | full (`SHOPIFY_RPS`) |
| 2 | with arm 1 | `crawl-shopify` (NAT, 34.172.156.113) | 1/3 | full |
| 3 | 30 min after arm 2 ends | `crawl-shopify` (NAT, 34.172.156.113) | 2/3 | **low rate**: 0.5 req/s |

Arm 3 tests a **low rate**, not a crawl spread over hours. The 225-min window (`deadline_min`, the default) only caps
how long the job may start stores; the main pass starts each store as soon as the rate allows, so at 0.5 req/s a third
of the stores (about 1,400 requests) takes roughly 45–60 min plus backoffs. Only the deferred rounds are spread out.
A main pass spread over the window is a follow-up (DESIGN.md, Next), worth building only if the low rate wins.

**Run the sequence detached, and start it by 00:45 UTC on Monday 2026-11-02.** In the worst case arm 2 runs its full
240 min, then the 30-min gap, then arm 3 its 240 min: 8.5 h, which must end before the 09:17 UTC GitHub run. Cloud
Shell ends idle sessions, so don't leave the sequence in an interactive shell: put it in a script and run it under
`tmux` (or `nohup`), which survive a disconnect:

```sh
cat > arms.sh <<'SH'
#!/bin/sh
set -u
SALT=${1:-${SALT:?pass the salt as the first argument or set SALT}}
WEEK=${WEEK:-2026-11-02} REGION=${REGION:-us-central1}
echo "salt=$SALT week=$WEEK"
date -u   # the start: no later than 00:45 UTC on 2026-11-02

# Arm 1: default egress, full rate (in the background)
gcloud run jobs execute crawl-shopify-default-egress --region $REGION --wait \
  --args="crawl,--connector,shopify,--run-date,$WEEK,--slice,0/3,--salt,$SALT" \
  --update-env-vars="COFFEEDATA_ARM=1-default-full" > arm1.log 2>&1 &

# Arm 2: the dedicated NAT IP, full rate, at the same time
gcloud run jobs execute crawl-shopify --region $REGION --wait \
  --args="crawl,--connector,shopify,--run-date,$WEEK,--slice,1/3,--salt,$SALT" \
  --update-env-vars="COFFEEDATA_ARM=2-dedicated-full" > arm2.log 2>&1
date -u   # arm 2 has ended

# Arm 3: the same NAT IP at a low rate, 30 min after arm 2 ended
sleep 1800
gcloud run jobs execute crawl-shopify --region $REGION --wait \
  --args="crawl,--connector,shopify,--run-date,$WEEK,--slice,2/3,--salt,$SALT,--rps,0.5" \
  --update-env-vars="COFFEEDATA_ARM=3-dedicated-low" > arm3.log 2>&1
date -u   # arm 3 has ended
wait   # for arm 1
SH
tmux new-session -d -s arms "sh arms.sh $SALT > arms.log 2>&1"   # or: nohup sh arms.sh "$SALT" > arms.log 2>&1 &
tmux attach -t arms   # to watch; detach with Ctrl-b d, and the sequence keeps running
```

Each `gcloud run jobs execute --wait` also shows up in `gcloud run jobs executions list --region $REGION`, so you can
check progress from anywhere.

An arm that reaches its deadline is a finished arm: from this commit on it exits 0 and prints `crawl shopify:
deadline reached, N roasters left` to stderr. Its `job_manifests` end row says `deadline`, with `skipped` = N, and its
results count. (The 79464cd image still exits 1 on a deadline; with `--max-retries 0` nothing is retried, and the
execution just shows as failed.)

**Re-running after a failed arm.** Keep all of one salt's arms on one image digest: a different image can draw
different thirds and runs different code. Record the digest each arm ran (`gcloud run jobs executions describe <execution>
--region $REGION --format='value(spec.template.spec.containers[0].image)'`; a job keeps the digest of its last create or
update, so don't update a job between arms), and check that
every row of the salt has the same `code_version` in the results query. Then either:
- **re-run only the failed arm(s)** on the same digest, with the same salt and `WEEK`. The finished arms' rows stay
  valid, and the results query counts each arm's latest successful attempt. The arms no longer ran at the same time:
  say so with the results, since time of day then confounds the egress comparison; or
- **re-run all three arms with a new salt**, if the image has changed (a fix was built) or its digest is unknown.
  First clear the slices and parked progress (the `gcloud storage rm` at the end of this section), so the new arms
  don't skip the old run's ok stores or compact its rows in, then `sh arms.sh <new salt>`.

**A repeat on another day needs a week of its own.** A crawl skips every store with an ok fetch under its run_date,
and Monday's GitHub run writes 2026-11-02 fetches, so repeating with `WEEK=2026-11-02` would crawl almost nothing,
and a later real week would collide with that week's crawl. Pass `--run-date` a Monday with **no fetches in the lake
and no real crawl ahead of it**: a Monday before the lake's first run. Check with:

```sh
uv run python -c "from coffeedata.lake import Lake; l = Lake(); print(l.connect().sql(f\"select min(run_date) from {l.scan('fetches')}\"))"
```

and use a Monday at least one week before that date (as `WEEK`). An arm's rows never land or merge, so an old
run_date is safe. Use a new salt for a repeat on an image built from this commit (the thirds are drawn by host now):
each arm gets a fresh random third, which balances stores across arms as well as rotating the old thirds would.

### Reading the results

Each arm writes:
- one `job_manifests` row at its start and one at its end. These are published to the lake, and the end row holds
  counts, `egress_ips`, `refusals` per IP, and `detail` (arm, egress, rps, slice, salt);
- its fetch rows, including `blocks`, in its GCS sub-partition `raw/connector=shopify/run_date=$WEEK/slice=k-of-3/`.

```sh
mkdir -p build/confirm && gcloud storage cp -r \
  "gs://$PROJECT-ingest-staging/raw/connector=shopify" build/confirm/
export COFFEEDATA_LAKE=hf://datasets/jtouley/coffee-data
export HF_TOKEN="$(gcloud secrets versions access latest --secret HF_TOKEN)"   # read access; never echo it
export SALT WEEK   # the salt and week of the arms to read
uv run python - <<'EOF'
import os

from coffeedata.lake import Lake

SALT, WEEK = os.environ["SALT"], os.environ["WEEK"]
lake = Lake()
con = lake.connect()
# Wilson 95% bounds for k of n (s = -1 lower, +1 upper)
con.sql("create macro wilson(k, n, s) as "
        "(k/n + 1.9208/n + s * 1.96 * sqrt(k/n * (1 - k/n) / n + 0.9604 / n^2)) / (1 + 3.8416/n)")
print(con.sql(f"""
  select json_extract_string(detail, '$.arm') as arm, attempt_id, started_at, status, code_version,
         json_extract_string(detail, '$.egress') as egress, r.ip, ended_at - started_at as wall,
         todo, ok, errors, parked, skipped, requests, http_429, http_430,
         round(ok / todo, 3) as ok_share,
         round(wilson(ok, todo, -1), 3) as ok_lo, round(wilson(ok, todo, 1), 3) as ok_hi,
         r.refused, r.stores, round(r.refused / r.stores, 3) as refused_share,
         round(wilson(r.refused, r.stores, -1), 3) as lo, round(wilson(r.refused, r.stores, 1), 3) as hi
  from (select *, unnest(refusals) as r from {lake.scan('job_manifests')}
        where job = 'crawl' and connector = 'shopify' and status in ('done', 'deadline')
          and json_extract_string(detail, '$.salt') = '{SALT}' and run_date = date '{WEEK}')
  where json_extract_string(detail, '$.arm') is not null
  -- each arm's latest successful attempt of this salt (all its egress-IP rows)
  qualify dense_rank() over (partition by json_extract_string(detail, '$.arm')
                             order by started_at desc, attempt_id desc) = 1
  order by arm, r.ip"""))
print(con.sql(f"""
  select b.egress_ip, b.status, count(*) as blocks,
         count(*) filter (where b.cf_mitigated = 'challenge') as challenges,
         count(*) filter (where b.content_type like 'text/html%') as html,
         count(*) filter (where b.retry_after is not null) as with_retry_after
  from (select unnest(blocks) as b from 'build/confirm/connector=shopify/run_date={WEEK}/slice=*/fetches.parquet')
  group by all order by all"""))
EOF
```

- One row per arm and egress IP, from each arm's **latest successful attempt of this salt** (`done` or `deadline`,
  filtered on `detail.salt` and `run_date`). An earlier attempt of the same arm (a crash, a re-run) is left out. All
  rows should show one `code_version`; if not, the salt's arms ran on different images (see "Re-running after a
  failed arm").
- `ok_share` with `ok_lo`–`ok_hi` is the arm's ok stores over the stores in its slice (`todo`), with Wilson 95%. The
  slices differ in size (about 357 ± 15), so compare shares, never counts.
- `refused_share` with `lo`–`hi` is the first-contact refusal share per egress IP, with Wilson 95%.
- The second table says whether blocks were challenges (`cf-mitigated: challenge`, HTML, often no Retry-After) or
  rate limits (JSON, `retry-after: 60`).

**Decision** (the same rules as DESIGN.md §10):
- **Dedicated egress stays** if arm 2's first-contact refusal share is below arm 1's with Wilson intervals that don't
  overlap, or if they overlap while arm 1's blocks are challenges and arm 2's aren't.
- **The low rate becomes the config** (`rps` in `connectors.py`) if arm 3's ok share is above arm 2's with Wilson
  intervals that don't overlap, and arm 3 ended inside 240 min.
- **Otherwise full rate stays.** Overlapping ok-share intervals are no evidence for either rate.
- **Revisit C** only if the low rate wins and can't fit one job's 240-min limit for a whole week's Shopify crawl.

Afterwards, clear the confirmation run's partition and parked progress (the arms' `job_manifests` rows stay, and are
never read as a crawl):

```sh
gcloud storage rm -r "gs://$PROJECT-ingest-staging/raw/connector=shopify/run_date=${WEEK:?set WEEK}" \
  "gs://$PROJECT-ingest-staging/parked/run_date=${WEEK:?set WEEK}"
```

## 4. Cutover: Monday's ship moves every connector at once

Do this only after the confirmation run passes, and finish it by Sunday so Monday's 07:17 probe is the first
scheduled job.

1. **Merge the cutover PR.** It does two things:
   - `.github/workflows/weekly.yml`: remove the `schedule:` block and keep `workflow_dispatch`, with its inputs
     (`sample`, `rollback_to`, `snowflake_migrate`, `accept_volume_change`), as the one-week fallback;
   - `.github/workflows/site.yml`: add `schedule: - cron: "17 11 * * 2"` (Tuesday, after the 06:17 build), plus the
     facts-only public export and the Snowflake load from the **served** release (moved from `weekly.yml`). The
     site deploy stays on Actions and never contacts a roaster.
2. **Turn the schedule on only when both hold:** the cutover PR has merged, so `weekly.yml` on main has no
   `schedule:` (otherwise GitHub and Cloud Scheduler both crawl Monday at 09:17), and no confirmation arm is running
   (one would share the NAT IP and the week with `crawl-shopify`). Check both first:
   ```sh
   gh api repos/jtouley/coffee-data-tracker/contents/.github/workflows/weekly.yml --jq .content | base64 -d \
     | grep -c '^ *schedule:'   # must print 0
   for j in crawl-shopify crawl-shopify-default-egress; do
     gcloud run jobs executions list --job $j --region $REGION --limit 3 \
       --format='table(name,status.startTime,status.completionTime)'   # every row has a completion time
   done
   ```
   Then build the image from the merged main with Cloud Build (§1), read its digest from the build output, and pin
   every job to it while turning the schedule on:
   ```sh
   git checkout main && git pull
   DIGEST=$(gcloud builds submit --project "$PROJECT" --config /tmp/ingest-build.yaml \
     --substitutions "_IMAGE=$IMAGE" --suppress-logs --format='value(results.images[0].digest)' .)
   PINNED="${IMAGE%:*}@${DIGEST:?the build printed no digest}"; echo "$PINNED"
   gcloud artifacts docker images describe "$PINNED" --format='value(image_summary.digest)'   # the same digest
   cd src/terraform
   terraform plan -var ingest_image="$PINNED" -var ingest_schedule_enabled=true -out ingest.plan
   # 16 to add (google_cloud_scheduler_job.ingest), 12 to change (each job's image, to $PINNED), 0 to destroy
   terraform apply ingest.plan
   for j in $(terraform output -json ingest_jobs | jq -r '.[]'); do   # every job's template runs the pinned digest
     img=$(gcloud run jobs describe "$j" --region $REGION \
       --format='value(spec.template.spec.template.spec.containers[0].image)')
     [ "$img" = "$PINNED" ] && echo "ok $j" || echo "MISMATCH $j: $img"
   done
   gcloud scheduler jobs list --location $REGION --filter="name~registry-coffee-" --format="table(name,schedule,state)"
   ```
   Any `MISMATCH`: stop and pause the schedule (§5). Set `ingest_schedule_enabled = true` and
   `ingest_image = "<the pinned image>"` in `terraform.tfvars`, so a later plan doesn't revert either.
3. **The first week to watch** (all UTC; DESIGN.md section 7 has the table): Mon 07:17 probe; Mon 09:17 all five
   crawls at once (Shopify on the NAT IP); Mon 15:17 their resumes; Mon 21:47 the third pass (crawl-retry for
   Shopify, each other connector's crawl again); Tue 03:17 merge;
   Tue 04:17 enrich-week; Tue 06:17 build; Tue 11:17 the site deploy. Monday's release is published Tuesday.
   - each execution's exit code (`gcloud run jobs executions list --region $REGION`);
   - the `job_manifests` rows: an attempt with only a `running` row died (section 7);
   - Tuesday's merge `detail.missing`: it should be empty;
   - the build's gate;
   - `egress-check`: the build job's execution fails if an IP stays above 8% for two runs in a row, and the owner
     is emailed (§6);
   - Tuesday's site deploy.
4. **After one good week**, a follow-up PR removes `weekly.yml`'s crawl. Its rollback and Snowflake-migrate inputs
   move to a small manual workflow, or to `coffeedata rollback` run from the build image.

## 5. Rollback

- **Stop the Cloud Run schedule** (the jobs and data stay):
  ```sh
  cd src/terraform && terraform apply -var ingest_schedule_enabled=false
  ```
  For an urgent stop of a single entry: `gcloud scheduler jobs pause registry-coffee-<entry> --location $REGION`.
- **Run the old weekly pipeline from Actions** (kept for one week):
  `gh workflow run weekly`. To restore its Monday schedule, revert the cutover PR.
- **Serve the previous release** if a bad one went out: `gh workflow run weekly -f rollback_to=previous`.
- **A half-done week in GCS:** merge what landed (`gcloud run jobs execute merge --region $REGION --wait`). Or, if
  the old workflow crawls that week instead, delete it as in §3 so the two don't overlap.
- **Shopify only, back to default egress:** run `crawl-shopify-default-egress` instead of `crawl-shopify`. To make it
  permanent, set `nat = false` for crawl-shopify and crawl-retry in `ingestion.tf` and apply. The static IP stays
  reserved until the address resource is removed.

## 6. Alerts

`src/terraform/alerts.tf` emails the owner (the `owner` notification channel, `owner_email`) about the pipeline too.
Each email names what failed and the first command to run.

| Policy | Watches | Fires |
|---|---|---|
| `registry.coffee: ingest job <job> failed` (one per job in `ingestion.tf`: 12, and 13 with `crawl-dq`) | `run.googleapis.com/job/completed_execution_count` with `result="failed"` for that job, as Google's Cloud Run jobs guidance (PromQL, 15-minute window, `duration = 0s`) | at the first failed execution: a non-zero exit after its retries, or the 240-minute timeout. It counts executions, not attempts: a crawl attempt that dies and a retry that succeeds is one succeeded execution and doesn't fire (section 7). A crawl that hits its deadline exits 0 and doesn't fire |
| `registry.coffee: ingest schedule failed to start a job` | Cloud Scheduler logs, `resource.type="cloud_scheduler_job"`, `job_id` starting `registry-coffee-`, `severity>=ERROR` | when an entry's attempt to start its job fails; one alert per Scheduler entry; Google caps new alerts at 2 a minute and 20 notifications a day per log-based policy, so five failures at 09:17 send about two emails at first |

**No "missing release" policy.** "No `build` success between Tue 06:17 and 12:00 UTC" has no reliable form in Cloud
Monitoring: a metric-absence condition waits at most 23.5 hours and has no time-of-week window, so on a weekly job
it would be met six days out of seven; a log-match condition fires only on an entry that arrives; and a PromQL
condition gated on the day and hour, over a metric with no points between executions, isn't something Google
documents, and an empty series would read as "missing" every Tuesday. A failed or timed-out build (or a failed
merge or enrich-week before it) fires the job alert; a build slot that doesn't start fires the Scheduler alert; and
`site.yml`'s Tuesday 11:17 run fails red, without exporting, when the served release isn't this week's (GitHub
emails a failed scheduled run). Left uncovered by email from Cloud Monitoring: a paused or deleted Scheduler entry.

**Apply.** With `terraform.tfvars` as after the cutover (`ingest_schedule_enabled = true`, `ingest_image` pinned):

```sh
cd src/terraform
terraform plan -out alerts.plan
terraform apply alerts.plan
```

**Expected plan: 13 to add, 1 to change, 0 to destroy.**

| Resources | Count |
|---|---|
| `google_monitoring_alert_policy.ingest_job_failed`: discover, probe, crawl-shopify, crawl-shopify-default-egress, crawl-retry, crawl-squarespace, crawl-woocommerce, crawl-square-online, crawl-generic, merge, enrich-week, build | 12 to add |
| `google_monitoring_alert_policy.ingest_schedule_failed` | 1 to add |
| `google_billing_budget.monthly`: `amount.specified_amount.units` "5" → "20" (in place) | 1 to change |

Stop if it touches the app service (`google_cloud_run_v2_service.app`), any `google_cloud_run_v2_job.ingest`, any
Scheduler entry, or anything else. If `terraform.tfvars` sets `budget_usd`, set it to 20 there, or the budget shows
no change.

**Test an alert** (after the apply):

- Job failure: run a job that fails at once, without touching a roaster or the lake, e.g. the probe with a bad
  argument: `gcloud run jobs execute probe --region $REGION --args="exit 1" --wait` (the job runs `sh -c`; the override
  applies to this execution only, and it fails twice: one retry). Expect the `ingest job probe failed` email within about 5 minutes. Don't use a crawl job
  or `build`.
- Scheduler: on a test entry, never a live one: `gcloud scheduler jobs create http registry-coffee-alert-test
  --location $REGION --schedule="0 0 1 1 *" --uri=https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/does-not-exist:run
  --http-method=POST --oauth-service-account-email=registry-coffee-ingest-sched@$PROJECT.iam.gserviceaccount.com`,
  then `gcloud scheduler jobs run registry-coffee-alert-test --location $REGION`. The 404 is logged at ERROR and
  the Scheduler email follows within minutes. Delete it: `gcloud scheduler jobs delete registry-coffee-alert-test
  --location $REGION --quiet`.
- Either way, `gcloud alpha monitoring policies list --filter='displayName~"registry.coffee: ingest"'
  --format='table(displayName,enabled)'` lists the 13 policies.

**Roll back:** revert this PR's commit and plan and apply again (13 to destroy, 1 to change, the budget back to
$5), or, to silence one policy at once, `gcloud alpha monitoring policies update <policy> --no-enabled` (the name is
in the list above). The jobs, the schedule and the app don't change either way.

## 7. When a crawl dies

A crawl attempt that dies (OOM kill, segfault, an uncaught error) leaves a `running` row in `job_manifests` with no
end row (an uncaught Python error writes a `failed` end row first), and its in-flight markers in the staging bucket
(`gs://<project>-ingest-staging/raw/connector=X/run_date=W/_inflight/attempt=<id>/<roaster>.json`). What happens
next, by itself (DESIGN.md section 7.1):

1. **The non-Shopify crawls are retried at once** (`max_retries = 1`). The retry compacts what the dead attempt
   wrote, skips what is done, and fetches the dead attempt's in-flight roasters one at a time beside the others. It
   stops starting roasters 225 minutes after the first attempt started, so it never overlaps 15:17; a suspect
   still waiting in a page backoff then is parked with its pages for crawl-retry, like any other store. If it
   succeeds, the execution succeeds and **no alert is sent**; the dead attempt is in `job_manifests`, and the
   retry's start row lists it (`detail.dead_attempts`, `detail.suspects`). Shopify's crawls aren't retried in place:
   their next slot resumes.
2. **A roaster in flight in two dead attempts is quarantined for the week**: the next attempt writes it a `fetches`
   row with status `crashed` (its `error` names the two attempts) and doesn't ask it; every later pass of the week
   skips it. In that attempt's end row it is in neither `ok` nor `errors`: `detail.quarantined` and
   `detail.by_adapter.<connector>.quarantined` count it. Expect, for a true poison pill: the 09:17 attempt and its
   retry die, the execution fails (one email), and the 15:17 resume quarantines it and crawls the rest.
3. **Every connector gets a third pass at 21:47** (crawl-retry for Shopify, the connector's crawl job for the
   others), and next week the roaster is fetched as usual.

**Look at it:**

```sh
gcloud run jobs executions list --job crawl-generic --region $REGION --limit 5
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="crawl-generic" AND
  (textPayload:"memory limit" OR textPayload:"earlier attempt" OR severity>=ERROR)' --limit 50 --freshness 1d
gcloud storage ls "gs://$PROJECT-ingest-staging/raw/connector=generic/run_date=$WEEK/_inflight/**"
```

and, in the lake (as §3 queries it):

```sql
-- the week's attempts per connector: an attempt whose latest row is `running` died
select connector, attempt_id, started_at, status, json_extract(detail, '$.dead_attempts') as dead,
       json_extract(detail, '$.quarantined') as quarantined
from job_manifests where run_date = '2026-11-02' and job in ('crawl', 'crawl-retry')
qualify row_number() over (partition by attempt_id order by (status <> 'running') desc) = 1 order by started_at;
-- quarantined roasters (the lake has them from Tuesday's merge on)
select roaster_id, adapter, fetched_at, error from fetches where run_date = '2026-11-02' and status = 'crashed';
```

Before the merge, the attempts' rows and the `crashed` rows are in the connector's partition in the staging bucket:
`gcloud storage ls "gs://$PROJECT-ingest-staging/raw/connector=<c>/run_date=$WEEK/**"`.

**Memory:** "The configured memory limit was reached" and signal 9 in the logs is an OOM kill. To change a job's
memory, set it in `terraform.tfvars` and apply (`ingest_job_memory = { "crawl-woocommerce" = "4Gi" }`; 1Gi to 8Gi
with 2 vCPU). `terraform plan` refuses a size outside that range and a key that isn't a job in `ingestion.tf`. A
`gcloud run jobs update <job> --memory` by hand is reverted by the next apply unless `ingestion.tf` or
`ingest_job_memory` says the same.

**Un-quarantine a roaster** (once you know why it crashed, or think it was a bystander): fetch it by hand, alone,
in its connector's job. The quarantine only lasts the week; next Monday it is fetched as usual.

```sh
gcloud run jobs execute crawl-generic --region $REGION --wait   --args=crawl,--connector,generic,--only,<roaster_id>
```

`--only` fetches that roaster even though it is done (`crashed`) this week; that attempt's rows list no quarantine
(`detail.quarantined` is `[]`, `detail.only` names the roaster). If it succeeds, its `ok` row counts for the run (a
run is ok when any attempt is). If it kills the attempt too, Cloud Run retries it once (`max_retries = 1`), and the
retry fetches it again; if that dies as well, the execution fails. Nothing else was in flight: it is the cause.
Run it before Tuesday's 03:17 merge, so the merge picks it up; after the merge, run `merge` again by hand.
To stop a roaster from being suspected at all this week (rarely needed), delete its markers:
`gcloud storage rm "gs://$PROJECT-ingest-staging/raw/connector=<c>/run_date=$WEEK/_inflight/attempt=*/<roaster_id>.json"`.

## 8. Post-crawl check (crawl-dq)

`crawl-dq` judges the week's crawl output shortly after each pass (DESIGN, "Post-crawl data quality"). It is polled
every 20 minutes over Monday 10:07 to Tuesday 02:47 UTC (Scheduler entry `crawl-dq`, `7,27,47 0-2,10-23 * * 1,2`;
the hours outside that window find nothing to judge); a poll with nothing new exits in seconds. It never serves, records a build or publishes.

**Ship it** (after this PR merges):
1. Rebuild and push the ingest image, and pin the new digest in `ingest_image` (section 1, and section 4's pinning
   step): the jobs run the `coffeedata crawl-dq` command and the repull hooks in `crawl`/`crawl-retry` only from it.
2. `cd src/terraform && terraform plan -out dq.plan`. **Expected: 4 to add, 12 to change, 0 to destroy**: the job
   `google_cloud_run_v2_job.ingest["crawl-dq"]`, its invoker binding
   `google_cloud_run_v2_job_iam_member.scheduler["crawl-dq"]`, the Scheduler entry
   `google_cloud_scheduler_job.ingest["crawl-dq"]` (only with `ingest_schedule_enabled = true`), and the alert `google_monitoring_alert_policy.ingest_job_failed["crawl-dq"]`.
   The 12 existing `ingest_job_failed` policies change in place, only their documentation text (it now names
   `crawl-dq`): **12 to change**, and nothing else. Stop if the plan touches any other job, the NAT, the bucket or the app.
3. `terraform apply dq.plan`.
4. Try it by hand on the current week, without waiting for Monday: `gcloud run jobs execute crawl-dq --region $REGION
   --args="crawl-dq,--force" --wait`. It writes one `job_manifests` row (job `dq`) and nothing else.

**Read a verdict.** The execution's log ends with the verdict as JSON (`systemic`, `repull`, `held_back`,
`by_connector`, `running`, `tests`); the same is the `detail` of the week's latest `dq` row:

```sh
uv run python -c "
from coffeedata import control; from coffeedata.lake import Lake; import json, sys
rows = [r for r in control.rows(Lake(), sys.argv[1]) if r['job'] == 'dq' and r['status'] == 'done']
print(json.dumps(json.loads(rows[-1]['detail']), indent=1))" 2026-11-02
```

**When it alerts** (`ingest job crawl-dq failed`): the log's `"severity": "ERROR"` line names the scope.
- `connector`: one platform is below `adapter_ok_rate`'s floor among the roasters its passes reached. Its roasters are
  held off the repull list. Look at that connector's `job_manifests` rows (429/430 counts, egress IPs) and its
  execution logs before its next slot; Tuesday's build will fail its gate unless the resume or retry recovers it.
- `image`: two or more platforms at once. Suspect what they share: the image (`code_version` on the rows), a code
  change, or egress. Roll back the image (section 5) before the next slot.
- `run`: the run as a whole is below `run_ok_rate` with no single platform to blame.
- No verdict (`CheckError`, or a traceback): dbt couldn't build or run the check. That's not a data finding; the
  repull list keeps its last value, and the polls don't retry it (one email, not one every 20 minutes) until another
  crawl attempt ends or you run it with `--force`. Run `coffeedata crawl-dq --force` locally against a copy of the
  lake to see the dbt error.

**Repulls** need nothing from the owner: the next crawl pass of each connector (`crawl`, `crawl-retry`) fetches
its listed `empty` and `suspect` roasters again; `failed` ones are asked again by every `crawl` pass anyway.
To fetch one roaster by hand instead: `crawl --connector X --only <roaster_id>` (section 7).

**Stop it:** `gcloud scheduler jobs pause registry-coffee-crawl-dq --location $REGION`.
Nothing else depends on it: with no `dq` row, the crawl passes add no repulls.

## Cost

About +$1–2/month for the non-Shopify jobs, and about $4–6/month for Cloud NAT, mostly the reserved static IP. The
staging bucket holds one week of raw/ at most, a few hundred MB. With the app (inside Cloud Run's free tier), about
$5–8/month in all, so the monthly budget (`budget_usd`) is $20: its first email (50%, $10) is above the estimate's
top, and 90%/100% ($18/$20) mean more than twice the estimate. Alert policies cost nothing.
