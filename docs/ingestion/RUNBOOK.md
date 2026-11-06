# Ingestion on Cloud Run: runbook

The owner's steps for the confirmation run, the cutover and a rollback. The design is in `DESIGN.md`, and the
infrastructure is in `src/terraform/ingestion.tf`. Every command runs from the repository root unless it says
otherwise.

```sh
export PROJECT=coffee-data-tracker REGION=us-central1
export IMAGE=$REGION-docker.pkg.dev/$PROJECT/registry-coffee/ingest:latest
export WEEK=2026-11-02   # the week's Monday: the confirmation run's week (§3), and the week §5 repairs
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
- `src/terraform/terraform.tfvars` exists (copy `terraform.tfvars.example`), with
  `verdict_model = "claude-haiku-5-5"` (the VERDICT_MODEL repository variable's value). **It is required:** a plan
  without it stops at the variable's validation, and a build job without it fails (§5.5).

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
job runs `$IMAGE`. This section is for a fresh project, or to check that the live state still matches. The second
egress IP, for the WooCommerce and generic crawls, is applied on its own: §2.1.

- **In production**, the crawl jobs' `max_retries` is 0 for Shopify's (`crawl-shopify`, its default-egress arm,
  `crawl-retry`) and 1 for the other four since the crawl-hardening PR (section 7), until §2.1 sets crawl-woocommerce's
  and crawl-generic's to 0; `crawl-generic` has 8Gi, and
  `crawl-squarespace` 8Gi since #99 (a plan shows that change once, until it is applied). A plan that shows anything
  else on a job, beyond the image: stop.
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

**Expected plan in a fresh project: 41 to add, 0 to change, 0 to destroy.** The app's own resources don't change.

| Resources | Count |
|---|---|
| `google_project_service.ingest_apis` (compute, cloudscheduler, storage) | 3 |
| `google_compute_network.ingest`, `google_compute_subnetwork.shopify_egress`, `google_compute_router.ingest`, `google_compute_address.shopify_egress`, `google_compute_router_nat.shopify_egress` | 5 |
| `google_compute_subnetwork.woo_generic_egress`, `google_compute_address.woo_generic_egress`, `google_compute_router_nat.woo_generic_egress` | 3 |
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
terraform output woo_generic_egress_ip   # the one IP the WooCommerce and generic stores see
terraform output ingest_jobs
cd ../..
```

Check that only crawl-shopify, crawl-retry, crawl-woocommerce and crawl-generic leave through a NAT. `gcloud run jobs describe` returns the v1 (Knative)
form of the job, where Direct VPC egress is the `run.googleapis.com/vpc-access-egress` annotation on the execution
template (`spec.template.metadata.annotations`, as in Google's
[Direct VPC egress for jobs](https://cloud.google.com/run/docs/configuring/vpc-direct-vpc) page), not a
`vpcAccess` field:

```sh
for j in crawl-shopify crawl-retry crawl-woocommerce crawl-generic crawl-shopify-default-egress crawl-squarespace; do
  echo "$j: $(gcloud run jobs describe $j --region $REGION --format=json \
    | jq -r '.spec.template.metadata.annotations | (.["run.googleapis.com/vpc-access-egress"] // "(none)")
             + " " + (.["run.googleapis.com/network-interfaces"] // "" | fromjson? // [] | map(.subnetwork) | join(","))')"
done
# crawl-shopify, crawl-retry:       all-traffic registry-coffee-shopify-egress
# crawl-woocommerce, crawl-generic: all-traffic registry-coffee-woo-generic-egress
# crawl-shopify-default-egress, crawl-squarespace: (none)
```

The subnet decides the IP: each subnet has its own NAT gateway with its own static IP. If the first four print
`(none)` as well, look at the whole template before trusting either answer:
`gcloud run jobs describe crawl-shopify --region $REGION --format=yaml | grep -i -A2 vpc`.

### 2.1 The second egress IP: WooCommerce and generic

**Why.** SiteGround, which hosts many WooCommerce and generic roaster sites, answers Cloud Run's default egress with
an HTTP 202 `sg-captcha` challenge page instead of the catalog. The same image through the existing NAT IP got 200
(2026-11-02, execution `sg-diag-nat-5wb2v`). The owner's decision: crawl-woocommerce and crawl-generic, every pass
(the 09:17 crawl, the 15:17 resume and the 21:47 retry run the same two jobs), leave from a **second** reserved
static IP, never Shopify's. Shopify's IP keeps carrying only Shopify's traffic, so its standing with Shopify, and the
confirmation run's reading of it, don't change.

**How.** Cloud NAT translates by source subnet, so the second IP needs its own subnet and gateway: a second subnet
in the same network (`woo_generic_subnet_cidr`, 10.8.0.64/26 by default, beside Shopify's 10.8.0.0/26), a second
reserved address and a second Cloud NAT gateway on the same router, with the Shopify NAT's port settings (dynamic
allocation, 1,024 to 8,192 ports per VM, endpoint-independent mapping off, error logging). In `ingestion.tf` each
job's `egress` is `default`, `shopify` or `woo_generic`.

**Plan, in production** (Shopify's egress already applied; `terraform.tfvars` as it is now):

```sh
cd src/terraform
terraform plan -out egress.plan
```

**Expected plan: 3 to add, 2 to change, 0 to destroy.**

| Resources | Count |
|---|---|
| `google_compute_subnetwork.woo_generic_egress`, `google_compute_address.woo_generic_egress`, `google_compute_router_nat.woo_generic_egress` | 3 to add |
| `google_cloud_run_v2_job.ingest["crawl-woocommerce"]`, `["crawl-generic"]`, in place: a `vpc_access` block (the new subnet, `ALL_TRAFFIC`), `COFFEEDATA_EGRESS_MODE` `default` → `dedicated`, and `COFFEEDATA_EGRESS_ECHO_URL` → `COFFEEDATA_EGRESS_IP` (the new address, known after apply), and `max_retries` 1 → 0 (one IP: an immediate retry would re-ask the stores that just refused) | 2 to change |

**Nothing existing is replaced.** Stop if the plan shows any `-/+` (replace) or `-` (destroy), any change to
`google_compute_network.ingest`, `google_compute_router.ingest` or any `*.shopify_egress` resource, or a change to
any other job (beyond the image, if `ingest_image` isn't pinned in `terraform.tfvars`, and `crawl-squarespace`'s
memory going to 8Gi until #99's change is applied, both as in §2). Then:

```sh
terraform apply egress.plan
terraform output woo_generic_egress_ip   # the new IP; Shopify's (shopify_egress_ip) is unchanged
gcloud compute routers nats list --router=registry-coffee-ingest --region=us-central1   # two gateways
gcloud compute routers nats describe registry-coffee-woo-generic-egress --router=registry-coffee-ingest \
  --region=us-central1 \
  --format='value(enableDynamicPortAllocation,enableEndpointIndependentMapping,minPortsPerVm,maxPortsPerVm)'
cd ../..
```

The `describe` should print `True False 1024 8192`, as Shopify's does. Run the §2 loop: crawl-woocommerce and
crawl-generic now print `all-traffic registry-coffee-woo-generic-egress`.

**Check one SiteGround store from the new IP** before the next Monday crawl (the 202 came from Cloud Run's default
shared egress pool, and Shopify's NAT IP got 200; the new one is a fresh Google IP, so check it rather than
assume). Outside a crawl window, fetch one WooCommerce roaster that answered `sg-captcha` on default egress
(§7's `--only` run):

```sh
gcloud run jobs execute crawl-woocommerce --region $REGION --wait \
  --args="crawl,--connector,woocommerce,--run-date,$WEEK,--only,<roaster_id>"
```

Its `fetches` row should be `ok`, and its `job_manifests` row should list the new IP in `egress_ips` with
`detail.egress` = `dedicated`. If it's still challenged, put the two jobs back on default egress (below) and
report it with the execution name.

**Then open the SiteGround allowlist ticket** (the owner, once the apply is done and the IP is known; it never
changes, so this is once). In SiteGround's support (or through a roaster whose site SiteGround hosts, if SiteGround
asks for the site owner), ask that the new IP be allowlisted past the anti-bot challenge, with:
- the IP: `terraform output -raw woo_generic_egress_ip`;
- what it is: a weekly catalog crawl of coffee roasters' public product pages, paced per store (`http.py`), that
  honours robots.txt, with our user agent (`http.USER_AGENT`) and contact address;
- the evidence: HTTP 202 with `sg-captcha: challenge` on every request from Cloud Run's shared egress (2026-11-02,
  38 stores), and 200 from a dedicated NAT IP (execution `sg-diag-nat-5wb2v`);
- the window: Mondays 09:17 to about Tue 01:47 UTC.

Record the ticket number and date in the next week's notes. Don't add other IPs to the request, and don't ask for
Shopify's: it carries only Shopify's traffic.

**Back to default egress** (both jobs): set `egress = "default"` for crawl-woocommerce and crawl-generic in
`ingestion.tf` and apply (2 to change). The address stays reserved, and billed, until its resource is removed.

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
     (`sample`, `rollback_to`, `snowflake_migrate`, `accept_volume_change`), as the fallback (its crawl for
     one week; its rollback until a replacement exists, step 4);
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
   crawls at once (Shopify on its NAT IP, WooCommerce and generic on the second); Mon 15:17 their resumes; Mon 21:47 the third pass (crawl-retry for
   Shopify, each other connector's crawl again); Tue 03:17 merge;
   Tue 04:17 enrich-week; Tue 06:17 build; Tue 11:17 the site deploy. Monday's release is published Tuesday.
   - each execution's exit code (`gcloud run jobs executions list --region $REGION`);
   - the `job_manifests` rows: an attempt with only a `running` row died (section 7);
   - Tuesday's merge `detail.missing`: it should be empty;
   - the build's gate;
   - `egress-check`: the build job's execution fails if an IP stays above 8% for two runs in a row, and the owner
     is emailed (§6);
   - Tuesday's site deploy.
4. **After one good week**, a follow-up PR may remove `weekly.yml`'s crawl, and only its crawl. **Keep the workflow
   and its `rollback_to` and `snowflake_migrate` inputs:** it is the one-click rollback (§5). Retire it only after a
   replacement that serves, re-exports, loads Snowflake and deploys the site has merged and been tested.
   `coffeedata rollback` in the build image is not one: that job has no `PUBLIC_EXPORT_URL`, Snowflake credentials
   or Pages deploy.

## 5. Rollback and Tuesday recovery

Owner: jtouley. Commands last checked against `main` 1e89d74 on 2026-11-02; distrust them after later changes to
`ingestion.tf`, `weekly.yml`, `site.yml` or the CLI.

- **Stop the Cloud Run schedule** (the jobs and data stay):
  ```sh
  cd src/terraform && terraform apply -var ingest_schedule_enabled=false
  ```
  For an urgent stop of a single entry: `gcloud scheduler jobs pause registry-coffee-<entry> --location $REGION`.
- **Run the old weekly pipeline from Actions** (its crawl is kept for one week):
  `gh workflow run weekly`. To restore its Monday schedule, revert the cutover PR.
- **Serve the previous release** if a bad one went out: `gh workflow run weekly -f rollback_to=previous` (§5.1 d).
  **`weekly.yml` holds the only one-click rollback.** Don't retire it until a replacement exists (§4 step 4).
- **Don't re-release an earlier week with `coffeedata build --run-date <older run>`.** Once a newer run is in the
  lake, `public_coffee_is_fresh` fails such a build (the public catalog's newest `last_seen` is the newer run), so
  resuming last week after this Monday's crawl has written fetches fails too. Roll back to the previous release
  instead (`coffeedata rollback`, or `rollback_to` above), or build the latest week.
- **A half-done week in GCS:** merge what landed (`gcloud run jobs execute merge --region $REGION --wait`). Or, if
  the old workflow crawls that week instead, delete it as in §3 so the two don't overlap.
- **Recover a merged week from the archive** (within 30 days of its merge; then the bucket's lifecycle rule has
  deleted it). The merge moves each connector's partition, unchanged, to
  `archive/raw/connector=<c>/run_date=<week>/merge=<batch>/` in the staging bucket; `<batch>` is the merge's
  `job_manifests.detail.batch` and the `batch=<id>` of the lake files it wrote. A week merged more than once has a
  folder per merge.
  ```sh
  export BUCKET=gs://$PROJECT-ingest-staging WEEK=2026-11-02   # any merged week's Monday
  gcloud storage ls "$BUCKET/archive/raw/connector=*/run_date=$WEEK/"   # one merge=<batch>/ per connector per merge
  ```
  - *To read or check the rows* (nothing changes): `gcloud storage cp -r "$BUCKET/archive/raw/*/run_date=$WEEK"
    /tmp/recover/` and query the Parquet files locally, e.g. `uv run python -c "import duckdb;
    print(duckdb.sql(\"from '/tmp/recover/**/fetches.parquet'\"))"`. They hold exactly the rows the merge read.
  - *To merge them into the lake again* (rows the dataset lost, or a merge whose lake files never reached HF):
    copy, don't move, one merge folder per connector back to where the merge reads it, then run the merge for that
    week. Restore one `merge=<batch>` folder per connector per run (the files share names), oldest first.
    ```sh
    C=shopify B=<batch>   # from the listing above
    gcloud storage cp "$BUCKET/archive/raw/connector=$C/run_date=$WEEK/merge=$B/*.parquet" \
      "$BUCKET/raw/connector=$C/run_date=$WEEK/"
    gcloud run jobs execute merge --region $REGION --args="merge,--run-date,$WEEK" --wait
    ```
    The merge adds only rows the lake doesn't already have (by `batch_id`, `manifest_id` and `body_hash`), so a
    restored partition is never counted twice, and it archives the partition again under a new `merge=<batch>`
    (another 30 days). The connector must have a finished crawl that week in `job_manifests`, as it had for the
    first merge. Only a merge of that week reads the restored partition: the crawls and the scheduled merge read
    the current week's.
- **Shopify only, back to default egress:** run `crawl-shopify-default-egress` instead of `crawl-shopify`. To make it
  permanent, set `egress = "default"` for crawl-shopify and crawl-retry in `ingestion.tf` and apply. The static IP
  stays reserved until the address resource is removed.
- **WooCommerce and generic, back to default egress:** §2.1, last paragraph.

The Tuesday steps below use this week's run date, `$WEEK` from §0: **this week's Monday**. Set it first, every week:

```sh
export WEEK=$(uv run python -c 'import datetime as d; t=d.datetime.now(d.UTC).date(); print(t-d.timedelta(t.weekday()))')
echo $WEEK   # this Monday, e.g. 2026-11-02
```

**Crawl, merge, enrich-week and build run on Cloud Run only.** The jobs carry the verdict model, the Anthropic key
and the staging bucket, and the build job runs `coffeedata classify` first. A local `uv run coffeedata` crawl,
crawl-retry, merge, enrich-week, build or publish has none of these. Without a verdict model and key, a local
`classify` or `build` fails (§5.5); with `--allow-rules-only` or `COFFEEDATA_ALLOW_RULES_ONLY=1` (which `make` sets
for a local lake) it builds a different catalog, rules only, that has no verdict gate and can still pass the other
checks and become the baseline. Don't use the local forms for a release. Only the
read-only steps (`build-diff`, the Python checks) and `override` also have a local form, which runs with §3's
`COFFEEDATA_LAKE` and `HF_TOKEN` exports (never echo the token). `--args` replaces the job's arguments for one
execution, and gcloud splits it on commas, so keep commas out of it. `build` runs `sh -c`, so its `--args` is one
shell script; the other jobs run `coffeedata`, so theirs is the argument list. To read an execution's output:

```sh
E=$(gcloud run jobs executions list --job build --region $REGION --limit 1 --format='value(name)')
gcloud logging read "resource.type=\"cloud_run_job\" AND labels.\"run.googleapis.com/execution_name\"=\"$E\"" \
  --order asc --limit 500 --format='value(textPayload)'
```

### 5.1 The gate failed on Tuesday

**Checklist:**

1. `echo $WEEK` shows this Monday (§5).
2. Diagnose with `build-diff` (a).
3. Check the failed build judged `$WEEK` (§5.2). If it didn't, follow §5.2 instead.
4. Pick **b** (gate wrong), **c** (crawl bad) or **d** (bad data out). Never crawl a past week (c).
5. Rebuild (b, c or §5.2) or roll back (d). After a Cloud Run rollback, run
   `gh workflow run weekly -f rollback_to=served` (d).
6. Confirm the fix worked (e).
7. If the release was served after 11:17: `gh workflow run weekly -f rollback_to=served` (below).

The build exits 1 and the `ingest job build failed` email arrives. The served release stays last week's: nothing bad
is out, but nothing new is either.

**a. Diagnose.**

```sh
gcloud run jobs execute build --region $REGION --wait --args='coffeedata build-diff'
uv run coffeedata build-diff
```

By default it compares the latest build, passing or not, with the last passing build before it (the CLI's `--help`
says "the last two that passed"; the code does the former). Read `new_passed`, `failing`, `metrics`, `coffees` and
`roasters_most_changed`. First check that the failed build judged `$WEEK`, not last week (§5.2). Then go by
`failing`:

| Failing test | Go to |
|---|---|
| `catalog_volume_vs_last_build` | **b** if build-diff shows a real change; **c** if a crawl lost roasters |
| `run_is_complete`, `run_ok_rate`, `adapter_ok_rate` | **c** |
| `verdicts_cover_catalog` | §5.5: the build had no verdict model, or the verdicts backfill isn't collected yet |
| `latest_run_is_fresh` | only possible if last week's run is missing too (on Tuesday last week's Monday still counts as fresh, §5.2). Fix **this** week's merge (§5.2, or **c** for `$WEEK`): once `$WEEK` is merged, this test passes. **Never crawl a past week** (c) |
| anything else (prices, uniqueness, facts-only, coverage claims) | a code or data bug: leave last week served and fix it in code |

`suspect_runs_need_review` and `build_metrics_in_band` are warnings (`severity='warn'`) and never fail the gate, so
neither appears in `failing`. Settle `suspect_runs_need_review`'s rows with `override` (b.1).

**b. The data is right, the gate is wrong.** Minutes, no re-crawl. A rebuild that fails the gate again runs twice:
the build job has `max_retries = 1`, so `--wait` returns only after the second attempt, and `builds` gets two failed
records.

1. One roaster-week at a time: `accept` (a suspect catalog change is real), `reject` (drop that fetch: the week
   counts as missing for the roaster), or `use_batch` with `--batch <batch_id>` (count another ok fetch of that week).
   The override is uploaded to the lake at once; the next build applies it.

   ```sh
   gcloud run jobs execute build --region $REGION --wait \
     --args="coffeedata override --roaster <roaster_id> --run-date $WEEK --action accept --reason '<why>'"
   uv run coffeedata override --roaster <roaster_id> --run-date "$WEEK" --action accept --reason "<why>"
   ```

2. A real catalog-wide change (a `catalog_volume_vs_last_build` failure): rebuild with `--accept-volume-change`. The
   fixed floors still apply, and this build becomes the next baseline. Leave the flag out after overrides alone.

   ```sh
   gcloud run jobs execute build --region $REGION --wait \
     --args="coffeedata classify; coffeedata build --run-date $WEEK --accept-volume-change; rc=\$?; coffeedata publish --message rebuild; exit \$rc"
   ```

**c. The crawl was bad** (an outage, blocks, roasters not attempted). Up to 4 h per crawl, plus merge and build; a
build that fails the gate runs twice (b).

A crawl asks only roasters **without an `ok` or `unavailable` fetch this week**. It repairs failed, skipped and
parked roasters, not ok fetches with wrong data (a parser bug in the image): those need b.1's `reject`, one
roaster-week at a time. Run it on Tuesday: `--run-date` defaults to today, and `week_of()` maps Tuesday to this
Monday (`$WEEK`, passed explicitly below). Crawl and merge share the staging bucket: run the whole chain on Cloud Run.

**Never crawl a past week** (`--run-date` earlier than this Monday). Nothing refuses it: the crawl fetches today's
catalogs and files them as that week's run. Lake files can't be changed, so undoing it takes a `reject` override for
every roaster it fetched. A past week's missing run doesn't need repair: merging `$WEEK` fixes freshness.

1. Check no execution of the job is running: `gcloud run jobs executions list --job crawl-<c> --region $REGION
   --limit 3`.
2. Crawl the connector (`<c>`: `shopify`, `squarespace`, `woocommerce`, `square_online`, `generic`; the job of
   `square_online` is `crawl-square-online`):

   ```sh
   gcloud run jobs execute crawl-<c> --region $REGION --wait --args=crawl,--connector,<c>,--run-date,$WEEK
   ```

   Shopify's parked stores, after `crawl-shopify` (stores that refused on Monday may refuse again):

   ```sh
   gcloud run jobs execute crawl-retry --region $REGION --wait \
     --args=crawl-retry,--connector,shopify,--run-date,$WEEK
   ```

3. Merge again. It adds only what the lake doesn't have yet:

   ```sh
   gcloud run jobs execute merge --region $REGION --wait --args=merge,--run-date,$WEEK
   ```

4. Enrich, then build:

   ```sh
   gcloud run jobs execute enrich-week --region $REGION --wait --args=enrich-week,--submit,--run-date,$WEEK
   gcloud run jobs execute build --region $REGION --wait \
     --args="coffeedata classify; coffeedata build --run-date $WEEK; rc=\$?; coffeedata publish --message rebuild; exit \$rc"
   ```

**d. Bad data passed the gate and is out.** Roll back:

```sh
gh workflow run weekly -f rollback_to=previous   # or -f rollback_to=<build_id> from build-diff
```

It serves the passing release before the served one, publishes, re-exports the public tables, loads Snowflake and
deploys the site. Any earlier passing build can be served with `rollback_to=<build_id>`. On Cloud Run, always name
the release with `--to <build_id>` (from `build-diff` or `quality.builds`):

```sh
gcloud run jobs execute build --region $REGION --wait \
  --args="coffeedata rollback --to <build_id> --reason '<why>'"   # <why>: no commas
```

Without `--to`, a failed `publish` after the serve makes the job's retry (`max_retries = 1`) run `rollback` again,
which goes back one more release. The job switches the served release and publishes only: it writes the site and
the public export inside the container, and they're thrown away. Use it only when Actions can't run. **After any
Cloud Run rollback**, as soon as Actions runs, re-export the release it served:

```sh
gh workflow run weekly -f rollback_to=served
```

Until then the static site, the public export and Snowflake keep showing the bad data. Then fix the week as in b
or c.

**e. Confirm the fix worked** (after b, c or §5.2). The served release's run date must be this Monday, and the
latest build execution must have succeeded:

```sh
uv run python - <<'EOF'
import os
from coffeedata import quality
from coffeedata.lake import Lake

s = quality.served(Lake())
print("served:", s and (s["build_id"], str(s["run_date"])))
week = os.environ["WEEK"]
print("FIXED" if s and str(s["run_date"]) == week else f"NOT FIXED: the served run date isn't {week}")
EOF
gcloud run jobs executions list --job build --region $REGION --limit 1 \
  --format='table(name,status.completionTime,status.succeededCount,status.failedCount)'   # succeededCount 1
```

After d alone, the served run date is last week's until b or c serves this week: check `build_id` is the one you
rolled back to.

**`site.yml` at 11:17 and the 8-day promise.** At 11:17 the Tuesday run renders and deploys the served release, then
checks that its run is this week's. If not, it fails red and exports nothing (no public export, no Snowflake load).
A manual `gh workflow run site` only renders and deploys. To export after a late fix, run
`gh workflow run weekly -f rollback_to=served`: it serves the current release again, exports, loads Snowflake and
deploys. `serving_log` records that re-serve with the reason "re-export for Snowflake migration (<actor>)", because
`weekly.yml` sets that reason for every `rollback_to=served`. Read it as a recovery export of the same release: no
Snowflake migration ran, and the served release didn't change.

The promise is that served data comes from a run at most 8 days old (`max_run_age_days`). On Tuesday last week's run
(the Monday before `$WEEK`) is exactly 8 days old, so the promise still holds. It breaks at 00:00 UTC Wednesday and
gets one day worse each day until this week's release is served.

| Case | 11:17 | 8-day promise |
|---|---|---|
| a/b, rebuilt before 11:17 (a rebuild that fails the gate runs twice) | green, exports this week's | kept |
| a/b, rebuilt after 11:17 | red, nothing exported; then `rollback_to=served` | kept if served on Tuesday |
| c | red (the crawl alone can take 4 h, and a failing build runs twice); then `rollback_to=served` after the build | kept only if served by 23:59 UTC Tuesday |
| d, before 11:17 | red (the served release is last week's); the rollback already exported | kept Tuesday; broken from Wednesday until b or c serves this week |
| d, after 11:17 | the bad release was green and exported; the rollback re-exports | as above |
| nothing fixed | red | broken from Wednesday |

### 5.2 The build ran before the merge finished

The Tuesday jobs run on fixed clock times, not on completion (issue #84): merge at 03:17 (up to 240 min, and one
retry), enrich-week at 04:17, build at 06:17. `build` never checks for a finished merge. It judges the latest run in
the lake, which before the merge is still last week's. Freshness uses a strict `<`, so last Monday counts as fresh on
Tuesday, and such a build can pass and serve last week's data as a new release. enrich-week before a merge prints
`waiting: no merge this week yet` and exits 0, so nothing alerts.

**How to tell:**

```sh
uv run python - <<'EOF'
import os
from coffeedata import control, quality
from coffeedata.lake import Lake

lake = Lake()
print("latest builds (build_id, run judged, passed):", quality.builds(lake)[-3:])
print("served:", quality.served(lake))
for r in control.rows(lake, os.environ["WEEK"]):
    if r["job"] in ("merge", "enrich"):
        print(r["job"], r["status"], r["started_at"], r["ended_at"])
EOF
for j in merge build; do
  gcloud run jobs executions list --job $j --region $REGION --limit 2 \
    --format='table(name,status.startTime,status.completionTime)'
done
```

The build ran too early if the latest build judged **last week's Monday** instead of `$WEEK`, or started before the
merge's `done` row ended. If there is no `enrich` row, enrich-week waited.

**Recourse:** once the merge's row is `done`, run enrich-week (if it waited) and the build again:

```sh
gcloud run jobs execute enrich-week --region $REGION --wait --args=enrich-week,--submit,--run-date,$WEEK
gcloud run jobs execute build --region $REGION --wait \
  --args="coffeedata classify; coffeedata build --run-date $WEEK; rc=\$?; coffeedata publish --message rebuild; exit \$rc"
```

`--run-date $WEEK` makes it judge this week: run before the merge, it fails as incomplete instead of serving last
week again. The early release has last week's data, so it is a harmless baseline. Confirm the fix (§5.1 e). After
11:17, export with `gh workflow run weekly -f rollback_to=served` (§5.1).

### 5.3 A crawl that ends `deadline`: skipped or partial

A crawl that reaches its deadline exits 0 and sends no email. Its end row in `job_manifests` says `deadline` for one
of two reasons, which need different handling:

- **Skipped**: `skipped` > 0 roasters were never started. They have no `fetches` row this week, so
  `run_is_complete` fails Tuesday's gate until something fetches them.
- **Partial**: `skipped` = 0, and `detail.partial` > 0 catalogs were read only in part (the deadline stopped them
  mid-catalog, a WooCommerce store's count differed from its `X-WP-Total`, a sub-sitemap couldn't be read or
  answered 404, or some of a generic store's product pages failed: `fetches.error` counts them). Each
  one is an **ok** fetch with `fetches.partial = true`, and `fetches.error` says why (for example
  `partial: … (page 55)`). `run_completeness` counts it as ok, so the gate passes, and dbt keeps its unread products
  listed. A partial Shopify catalog is parked at the page it reached: the resume slot and crawl-retry continue from
  there.

An attempt whose latest row is `running` died. Check the week's rows per connector (each connector has one attempt
per slot):

```sh
uv run python - <<'EOF'
import json, os
from coffeedata import control
from coffeedata.lake import Lake

for r in control.rows(Lake(), os.environ["WEEK"]):  # the latest row per attempt
    d = json.loads(r["detail"] or "{}")
    print(r["job"], r["connector"], r["started_at"], r["status"],
          *(f"{k}={r[k]}" for k in ("todo", "ok", "errors", "parked", "skipped")),
          f"partial={d.get('partial')}", f"partial_every_week={d.get('partial_every_week')}")
EOF
```

- **`skipped` > 0** on a connector's **latest** crawl row after its 15:17 resume: re-run its crawl before Tuesday's
  03:17 merge (§5.1 c, step 2). Otherwise `run_is_complete` fails Tuesday's gate.
- **Only partial** (`skipped` = 0): the gate passes. No re-run is needed, and none should be made every week for it.
- **`partial_every_week`** lists the stores that ended partial in each of their last two runs
  (`ingest.PARTIAL_WEEKS`); the crawl's log and stderr warn about them too. Such a store is never read whole inside
  one window. Read its `fetches.error` rows: a deadline stop at a later page each week means the catalog is too big
  for the window, and a count that never matches `X-WP-Total` means the store's listing and its count disagree.
  Raise it with the owner rather than re-running crawls.
- `crawl-retry` (Shopify, 21:47) writes `job_manifests` rows of its own (job `crawl-retry`, which never counts as a
  landed crawl). Its end row says `deadline` when it left stores unstarted or a resumed store stopped partial again,
  with `partial` and `partial_every_week` in its `detail`, as a crawl's. The query above prints it.
- After the merge, its row's `detail` lists `missing` connectors. That list should be empty.

### 5.4 Raising a job's memory by hand

An OOM kill shows as "memory limit" and signal 9 in the execution's log. On 2026-11-02 `crawl-generic` was
OOM-killed at 2Gi:

```sh
gcloud run jobs update crawl-generic --region $REGION --memory 8Gi
gcloud run jobs describe crawl-generic --region $REGION \
  --format='value(spec.template.spec.template.spec.containers[0].resources.limits.memory)'   # 8Gi
```

The next execution uses it; one that is running keeps its old limit. **The next `terraform apply` reverts it**
unless `ingestion.tf` says the same. On main, `crawl-generic` is still 2Gi, and PR #91 sets it to 8Gi. Until that
merges, a plan that shows `crawl-generic` memory going 8Gi → 2Gi must not be applied (§4 step 2's digest pin
included): set it to 8Gi in `ingestion.tf` first.

### 5.5 Verdicts: the gate, a failed classify, and the backfill

Claude's coffee-or-not verdicts (`verdict.py`) decide every product the rules don't exclude outright. Until 2026-11
they never ran: `verdict_model` defaulted to empty, `classify` skipped with exit 0, and every release was the rules
alone (on the sealed set, 10.7% of the products the rules keep are not coffee, against 2.7% with the verdicts).
Now three things stop that from happening silently:

- **`coffeedata classify` exits non-zero** when `COFFEEDATA_VERDICT_MODEL` or the Anthropic key is missing, naming
  which. The build job keeps going (`rc=0; coffeedata classify || rc=1; coffeedata build || rc=1; ...`): the build
  uses the verdicts already stored, and its gate decides whether the release is served. The execution still fails,
  so the `ingest job build failed` email arrives. A batch submit that Claude rejects (a revoked key, an outage)
  fails the same way; a finished batch whose results can't be read yet stays open for the next run. With
  `max_retries = 1` the whole chain then runs a second time, as for a failed gate (§5.1 b).
- **`verdicts_cover_catalog` fails the build** when it has no verdict model at all, or when fewer than 85%
  (`min_verdict_share`, `dbt/dbt_project.yml`) of listed coffees (`dim_coffee.is_listed`) have
  `classified_by = 'claude'`. The build's log prints the share: `verdicts: N of M listed coffees (x%) decided by
  Claude's verdicts (<model> N); the rest by the rules`.
- **`verdict_model` is required in Terraform**, with no empty default.

**The expected share and its lag.** About 95%, not 100%. The build job's classify submits this week's new and
changed product versions and doesn't wait for them, so they get verdicts at next week's build: 2.5% of listed
coffees in the 2026-11-02 run (1,114 of 44,593). An `unsure` verdict is left to the rules too, about 3% on the
validation set. 85% leaves room for a week with nearly 3x the usual new versions. To get this week's versions in
before the build, run classify with a wait (`coffeedata classify --wait 60`) before rebuilding.

**A failed classify** (the log says `Claude's verdicts are expected but can't run: no ...`): check
`verdict_model` in `terraform.tfvars` and the job's env (`gcloud run jobs describe build --region $REGION
--format='value(spec.template.spec.template.spec.containers[0].env)'`; it lists names and secret references, not
the key's value), and that `ANTHROPIC_API_KEY` has a version in Secret Manager. Fix, apply, then rebuild as in §5.2.

**The backfill**, after the first apply with `verdict_model` (and whenever the verdict model or
`coffee_definition.md` changes, which makes every version pending again): about 62,000 product versions are
pending, so the first build after the change fails `verdicts_cover_catalog` until their verdicts are collected,
and last week's release stays served. That is the gate working: don't opt out around it.

1. Submit everything pending (7 batches of 10,000; `--limit` defaults to 100,000), and wait up to 3 hours for them
   inside the job's 240 minutes:

   ```sh
   gcloud run jobs execute build --region $REGION --wait --args="coffeedata classify --wait 180; coffeedata publish --message verdicts"
   ```

2. If batches were still running (its output's `collect_after_wait` is below `submit.submitted`), collect again
   later; batches end within 24 hours. Repeat until a run collects 0 and submits 0:

   ```sh
   gcloud run jobs execute build --region $REGION --wait --args="coffeedata classify; coffeedata publish --message verdicts"
   ```

3. Rebuild (§5.2's form) and read the `verdicts:` line in its log. Expect about 3,600 listed products to drop out
   (95% about 2,400–5,200) and a few hundred to come back: within the 20% volume check, so no
   `--accept-volume-change` should be needed. Check the counts with `coffeedata build-diff`; a count far outside
   that range means the production request differs from the one evaluated (`eval/sealed/`).
4. Add the number of products whose `is_coffee` changed to the running total in `eval/sealed/README.md` (CLAUDE.md
   step 9).

**Rules only on purpose** (a fork without Claude, a local lake, tests): `--allow-rules-only` on `classify`, `build`
and `weekly`, or `COFFEEDATA_ALLOW_RULES_ONLY=1` (the `ALLOW_RULES_ONLY` repository variable for `weekly.yml`).
`make` sets it for a local lake. Never set it on the Cloud Run jobs: a rules-only release keeps about four times the
non-coffee.

### 5.6 Verifying a release: rebuild it from raw and compare hashes

Every build records each release table's row count and content hash in its `builds` record (kind `table_hash`,
`quality.table_hash`): a hash over every row and column except `release_build_id`, independent of row order. The
same lake and the same code give the same hashes. `tests/test_reproducible.py` checks that on every PR, on a fixture
lake. This section checks it for a release that customers actually got. Everything here runs locally, on a
downloaded copy of the lake. Nothing is written to the dataset or GCS, nothing is served, and Claude isn't called.

**When:**
- **After a change to how the build decides** (the dbt models, `classify`/`extract`/`products`, or the pinned dbt,
  DuckDB or Python packages) reaches the build job: verify the first release it serves. Also rebuild the release
  served before the change from its own lake revision, with the new code (step 4 with the new commit). A change
  meant to leave the output alone (a refactor, a version bump) should give `same` for every table. A change that
  isn't should differ only in the tables it meant to change.
- **Monthly:** the served release, on any day after the Tuesday export.
- **In an investigation:** a customer reports wrong data, a release looks wrong, or the lake may have been edited.

**1. Read the build's record** (§3's `COFFEEDATA_LAKE` and `HF_TOKEN` exports; never echo the token):

```sh
export BUILD=<build_id>   # quality.served(Lake())["build_id"] for the served one, or from build-diff
uv run coffeedata build-hashes $BUILD --verify
```

`tables` lists each release table's rows and hash. `--verify` re-hashes the release files in the dataset and fails
(exit 1) if any no longer matches the record: the release was damaged or replaced after its build. `inputs` is what a
rebuild has to match: `run_date`, `git_sha`, `package_versions` and `dbt_vars.verdict_model`. A Cloud Run build has
no git checkout, so its `git_sha` is null. Its code is the image the build job ran: read it from the execution
(`gcloud run jobs executions describe <execution> --region $REGION
--format='value(spec.template.spec.containers[0].image)'`, with the execution found as in §5's log commands, on the
build's day).

**2. Find the dataset revision the build read.** The build job publishes the verdicts it collected, its record and
its release in one commit, after the build. The commit that added the record is the lake as the build read it, plus
the build's own outputs (no model reads those):

```sh
export RUN_DATE=<inputs.run_date>
cat > /tmp/find_rev.py <<'PY'
import os
from huggingface_hub import HfApi

api, repo = HfApi(token=os.environ["HF_TOKEN"]), "jtouley/coffee-data"
path = f"builds/run_date={os.environ['RUN_DATE']}/build={os.environ['BUILD']}.parquet"
for c in reversed(api.list_repo_commits(repo, repo_type="dataset")):  # oldest first
    if api.file_exists(repo, path, repo_type="dataset", revision=c.commit_id):
        print(c.commit_id)
        break
PY
export REV=$(uv run python /tmp/find_rev.py) && echo $REV
```

**3. Download that revision to a scratch folder,** never over a working lake. Releases and marts aren't read by the
build, so leave them out:

```sh
export V=/tmp/verify-$BUILD && mkdir -p $V
uv run hf download jtouley/coffee-data --repo-type dataset --revision $REV --local-dir $V/lake \
  --exclude 'releases/*' --exclude 'marts/*'
```

Give `--exclude` once per pattern: a second pattern after one flag is read as a file to download, and the download
fails.

**4. Rebuild with the build's code**, on the local copy only. Unset `HF_TOKEN` first, so nothing can reach the
dataset. Don't run `classify`: the stored verdicts are part of the input, and asking Claude again would change them.
- `git_sha` set (an Actions build): `git worktree add $V/code <git_sha> && cd $V/code && uv sync --frozen`, then
  ```sh
  env -u HF_TOKEN TZ=UTC COFFEEDATA_LAKE=$V/lake COFFEEDATA_VERDICT_MODEL=<inputs.dbt_vars.verdict_model> \
    uv run coffeedata build --run-date $RUN_DATE --db $V/coffee.duckdb
  ```
- A Cloud Run build: run the same image, with the copy mounted:
  ```sh
  chmod -R a+rwX $V
  docker run --rm -v $V:/verify -e COFFEEDATA_LAKE=/verify/lake -e COFFEEDATA_VERDICT_MODEL=<verdict model> \
    <image from step 1> build --run-date $RUN_DATE --db /verify/coffee.duckdb
  ```

Builds run in UTC: `dbt/profiles.yml` pins it, and Cloud Run and Actions run in UTC anyway. `TZ=UTC` above does the
same for a commit from before the pin. In any other zone, a date cast from a timestamp, or a tie-break over a row's
text, can come out differently.

The rebuild of a release more than 8 days old ends with "failed its checks". That is expected: `latest_run_is_fresh`
is measured against today, and the checks never change a table. Passing or not, the rebuild records its own hashes
in the copy, beside the original's. If its `inputs.package_versions` (`build-hashes` with no id, next step) differ
from the original's, the rebuild didn't run the same code: use the image, or `uv sync --frozen` at the same commit.

**5. Compare** (the rebuild is the latest build in the copy):

```sh
env -u HF_TOKEN COFFEEDATA_LAKE=$V/lake uv run coffeedata build-hashes --against $BUILD
```

Exit 0, with `same` for every table: the release is reproduced from raw. Write that down, with the build id, `REV`
and the code, where the check was asked for (the PR, the issue or the monthly note). A table that differs shows its
row counts and both hashes. To see the rows, compare the original release's table (in the dataset at any later
revision, `releases/run_date=$RUN_DATE/build=$BUILD/<table>.parquet`) with the rebuild's (`$V/coffee.duckdb`), with
`except all` both ways and `release_build_id` left out, as `tests/test_reproducible.py` does. Then find the cause: a
model that depends on the order rows are read in (a tie in a "latest" pick, a list or `string_agg` without an
order), a lake file changed after the build, or different code or versions.

**6. Clean up:** `rm -rf $V /tmp/find_rev.py`, and `git worktree remove $V/code` if you made one.

## 6. Alerts

`src/terraform/alerts.tf` emails the owner (the `owner` notification channel, `owner_email`) about the pipeline too.
Each email names what failed and the first command to run.

| Policy | Watches | Fires |
|---|---|---|
| `registry.coffee: ingest job <job> failed` (one per job in `ingestion.tf`: 12, and 13 with `fetch-health`) | `run.googleapis.com/job/completed_execution_count` with `result="failed"` for that job, as Google's Cloud Run jobs guidance (PromQL, 15-minute window, `duration = 0s`) | at the first failed execution: a non-zero exit after its retries, or the 240-minute timeout. It counts executions, not attempts: a crawl attempt that dies and a retry that succeeds is one succeeded execution and doesn't fire (section 7). A crawl that hits its deadline exits 0 and doesn't fire |
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

1. **The Squarespace and Square Online crawls are retried at once** (`max_retries = 1`). The retry compacts what
   the dead attempt wrote, skips what is done, and fetches the dead attempt's in-flight roasters one at a time
   beside the others. It
   stops starting roasters 225 minutes after the first attempt started, so it never overlaps 15:17; a suspect
   still waiting in a page backoff then is parked with its pages for crawl-retry, like any other store. If it
   succeeds, the execution succeeds and **no alert is sent**; the dead attempt is in `job_manifests`, and the
   retry's start row lists it (`detail.dead_attempts`, `detail.suspects`). Shopify's, WooCommerce's and generic's
   crawls aren't retried in place (each leaves from one static IP): their next slot resumes.
2. **A roaster in flight in two dead attempts is quarantined for the week**: the next attempt writes it a `fetches`
   row with status `crashed` (its `error` names the two attempts) and doesn't ask it; every later pass of the week
   skips it. In that attempt's end row it is in neither `ok` nor `errors`: `detail.quarantined` and
   `detail.by_adapter.<connector>.quarantined` count it. Expect, for a true poison pill: the 09:17 attempt and its
   retry die, the execution fails (one email), and the 15:17 resume quarantines it and crawls the rest.
3. **Every connector gets a third pass at 21:47** (crawl-retry for Shopify, the connector's crawl job for the
   others), and next week the roaster is fetched as usual.

**A store that asks us to wait.** We never wait more than 5 minutes for anything a store asks for (DESIGN.md
section 6, the 5-minute rule): a Retry-After of an hour, or an HTTP date days away, is waited for 300 s, and the
store is asked again in the next deferred round, and in the next pass after that. It is never failed just for
asking, and no store can hold a job past its window: a retry that would start after the deadline isn't scheduled
(`detail.deferred.past_deadline`), and crawl-retry and the weekly fallback stop starting stores at 225 and 150
minutes. If a store owner asks why we came back sooner than their Retry-After, that is the answer: they can contact
us, and we'll wait 5 minutes.

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
run is ok when any attempt is). If it kills the attempt too, the execution fails (crawl-generic's `max_retries` is 0;
Squarespace's and Square Online's crawls retry once, and the retry fetches it again before failing). Nothing else
was in flight: it is the cause.
Run it before Tuesday's 03:17 merge, so the merge picks it up; after the merge, run `merge` again by hand.
To stop a roaster from being suspected at all this week (rarely needed), delete its markers:
`gcloud storage rm "gs://$PROJECT-ingest-staging/raw/connector=<c>/run_date=$WEEK/_inflight/attempt=*/<roaster_id>.json"`.

## 8. Fetch-health check (fetch-health)

`fetch-health` judges the week's fetches shortly after each pass (DESIGN, "Fetch health"): did each fetch return a
catalog of about the right size (failed, empty, or far fewer products than usual), and is a platform or the image
failing as a whole. **It doesn't judge the data**: a fetch with wiped prices, swapped products or a short first
week passes it. It is polled every 20 minutes over Monday 10:07 to Tuesday 02:47 UTC (Scheduler entry
`fetch-health`, `7,27,47 0-2,10-23 * * 1,2`; the hours outside that window find nothing to judge); a poll with
nothing new exits in seconds, as does one that finds another check still running. It never serves or records a
build; it writes its `job_manifests` rows and publishes them to the dataset (a failed publish is a `WARNING` line,
never a different verdict or exit code).

**Ship it** (after this PR merges):
1. Rebuild and push the ingest image, and pin the new digest in `ingest_image` (section 1, and section 4's pinning
   step): the jobs run the `coffeedata fetch-health` command and the re-fetch hooks in `crawl`/`crawl-retry` (off:
   `COFFEEDATA_DQ_REFETCH` unset) only from it.
2. `cd src/terraform && terraform plan -out fh.plan`. **Expected: 4 to add, 12 to change, 0 to destroy**: the job
   `google_cloud_run_v2_job.ingest["fetch-health"]`, its invoker binding
   `google_cloud_run_v2_job_iam_member.scheduler["fetch-health"]`, the Scheduler entry
   `google_cloud_scheduler_job.ingest["fetch-health"]` (only with `ingest_schedule_enabled = true`), and the alert
   `google_monitoring_alert_policy.ingest_job_failed["fetch-health"]`. The 12 existing `ingest_job_failed` policies
   change in place, only their documentation text (it now names `fetch-health`): **12 to change**, and nothing else.
   Stop if the plan touches any other job, the NAT, the bucket or the app.
3. `terraform apply fh.plan`.
4. Try it by hand on the current week, without waiting for Monday: `gcloud run jobs execute fetch-health --region
   $REGION --args="fetch-health,--force" --wait`. It writes its `job_manifests` rows (job `fetch-health`),
   publishes them, and nothing else.

**Read a verdict.** The execution's log ends with the verdict as JSON (`systemic`, `alert`, `repull`, `held_back`,
`unattributed`, `partial`, `quarantined`, `by_connector`, `running`, `tests`). **The log is authoritative.** The same
is the `detail` of the week's latest `fetch-health` row in the dataset, once its publish has gone through (a failed
publish leaves the rows staged for the next one):

```sh
uv run python -c "
from coffeedata import control; from coffeedata.lake import Lake; import json, sys
rows = [r for r in control.rows(Lake(), sys.argv[1]) if r['job'] == 'fetch-health' and r['status'] == 'done']
print(json.dumps(json.loads(rows[-1]['detail']), indent=1) if rows else 'no check yet for this week')" 2026-11-02
```

**When it alerts** (`ingest job fetch-health failed`): the log's `"severity": "ERROR"` line names the scope. Each
finding (a scope on a set of connectors) alerts once a week: a later pass's check that finds it again logs
a `WARNING` line and exits 0; a new or different finding alerts again. A suspect with no adapter is listed under
`unattributed`, for you to look at; no pass re-fetches it.
- `connector`: one platform is below `adapter_ok_rate`'s floor among the roasters its passes reached (a mass 429,
  430 or 503 that outlasted the 5-minute wait cap shows up here). Its roasters are held off the list. Look at that
  connector's `job_manifests` rows (429/430 counts, egress IPs) and its execution logs before its next slot;
  Tuesday's build will fail its gate unless the resume or retry recovers it.
- `image`: two or more platforms at once. Suspect what they share: the image (`code_version` on the rows), a code
  change, or egress. Roll back the image (section 5) before the next slot.
- `run`: the run as a whole is below `run_ok_rate` with no single platform to blame.
- No verdict (`CheckError`, or a traceback): dbt couldn't build or run the check. That's not a fetch finding; the
  list keeps its last value, and the polls don't retry it (one email, not one every 20 minutes). A check that
  **failed** (its end row says `failed`) runs again when the next crawl attempt ends, or with `--force`. A check that
  was **killed** (out of memory, instance lost: only its running row) holds back the polls until it is 30 minutes old
  (`fetchhealth.CHECK_LIMIT`) or a later check has started; then the next poll judges everything that has ended
  since the last finished check. After the last pass of the week (21:47) no attempt ends again, so after a failure
  run it with `--force` once it's fixed. Run `coffeedata fetch-health --force` locally against a copy of the lake to
  see the dbt error.

**The list** (`repull`) is for you to read: each entry is a roaster, its adapter, its `reasons` (`failed`, `empty`,
`suspect`) and the gate `rules` that gave them (`run_ok_rate`, `suspect_runs_need_review`; DESIGN, "Why a roaster is
listed"), and `challenge` (`siteground` or `cloudflare`) when a recorded block names the edge that refused it. A
challenge is an ordinary failed fetch: never held back, no alert of its own, retried by the week's later passes
(persistent blocking: issue #110). Apart from it: `partial` (ok fetches that read only part of the
catalog, with their reason: not failures, and the week's resume passes read them again) and `quarantined` (stores
crashguard quarantined for the week: section 7). **Re-fetching is off**: no crawl pass reads the list, and the
week's output is as without the check. `failed` roasters are asked again by every `crawl` pass anyway. To fetch one
listed roaster again by hand: `crawl --connector X --only <roaster_id>` (section 7).

**Re-fetch (off; don't turn it on yet).** The one setting is the env var `COFFEEDATA_DQ_REFETCH=1` on the crawl jobs,
read by `crawl` and `crawl-retry`; with it, each later pass of a connector fetches its listed `empty` and `suspect`
roasters again (the whole catalog). It changes which batch counts for those roasters, so it needs CLAUDE.md's full
evaluation first (`eval/run_quality/README.md`). Until then leave it unset; to confirm a job doesn't have it:
`gcloud run jobs describe <job> --region $REGION --format='value(spec.template.spec.template.spec.containers[0].env)'`
must not name it.

**Stop it:** `gcloud scheduler jobs pause registry-coffee-fetch-health --location $REGION`.
Nothing else depends on it: with re-fetch off the crawl passes never read its rows.

## Cost

About +$1–2/month for the non-Shopify jobs, and about $4–6/month for each of the two Cloud NAT gateways, mostly
its reserved static IP (about $3.65 a month while in use), plus NAT data processing on what the WooCommerce and
generic crawls fetch. The
staging bucket holds one week of raw/ and the last 30 days of merged partitions under archive/, about five weeks of
raw rows, a few GB at most (cents a month). With the app (inside Cloud Run's free tier), about
$9–14/month in all with the second IP. The monthly budget (`budget_usd`) stays $20, so its first email (50%, $10)
now falls inside the estimate: raise `budget_usd` if that email is noise. Alert policies cost nothing.
