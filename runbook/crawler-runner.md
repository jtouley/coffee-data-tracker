# The crawler runner: a fixed egress IP (proof of concept)

GitHub-hosted runners share Azure addresses with other scrapers. On 2026-10-30, 29% of Shopify stores answered the
weekly run with a 429 [25.0, 33.8]. On 2026-10-31, from a different IP, 5–8% did (`build/crawl-plan/live.md`, a local note not in
git). This page sets up one small GCP VM with a reserved static address. It runs a GitHub Actions self-hosted runner
with the label `crawler`, so the crawl leaves from one address that can be identified and published. It is not for
rotating IPs. The VM is described in `src/terraform/crawler/`, with its own Terraform state. Nothing has been
created: the owner applies it.

## What it creates

`terraform plan` from scratch: **11 to add, 0 to change, 0 to destroy**.

| Resource | What |
|---|---|
| `google_project_service.apis` (3) | `compute`, `secretmanager`, `iap` (the app's APIs are left alone) |
| `google_compute_network.crawler`, `google_compute_subnetwork.crawler` | Its own VPC, `10.80.0.0/28`. Not `default`, which usually allows SSH and RDP from anywhere |
| `google_compute_firewall.iap_ssh` | The only ingress rule: tcp/22 from IAP's range `35.235.240.0/20`. Everything else coming in is denied |
| `google_compute_address.egress` | The reserved static external IPv4 address, Premium tier. Output `egress_ip` |
| `google_service_account.crawler` | `crawler-runner@…`: **no project roles** |
| `google_secret_manager_secret.runner_token` | `crawler-runner-registration-token`: empty. You add the value |
| `google_secret_manager_secret_iam_member.crawler` | The VM's account may read that one secret |
| `google_compute_instance.crawler` | `crawler-1`: `e2-small`, Ubuntu 24.04, 20 GB standard disk, Shielded VM, OS Login, no project SSH keys, no serial port |

**Sizing.** The crawl peaks at 306–366 MB RSS (`build/crawl-plan/pools.md`) and uses about 12% of one core. A real
`coffeedata build` over a copy of the prod lake peaked at **1,009 MB** (856 MB with DuckDB capped at 1 GB, 724 MB
capped at 400 MB, measured 2026-10-31). `weekly` holds the crawl's memory while dbt runs, so with the OS and the runner
the total is about 1.5–1.8 GB. **`e2-small` (2 GB)** plus a 2 GB swap file fits that, so it's the default.
`e2-micro` (1 GB, free tier) fits `live-test.yml`, which is the crawl alone, but not the weekly run's dbt build: set
`machine_type = "e2-micro"` while only the live test runs. If `weekly` later swaps on `e2-small` (the
`live-test` "Machine" step prints memory and swap), use `e2-medium`.

## Security model (public repo)

A self-hosted runner on a public repository can be handed code from anyone's fork. These layers keep that code off
the VM:

1. **The VM refuses it, whatever the workflow says.** A root-owned pre-job hook
   (`scripts/job-started.sh`, set via `ACTIONS_RUNNER_HOOK_JOB_STARTED` in a root-owned systemd unit) fails every
   job before its first step unless all of these hold:
   - the event is `workflow_dispatch` or `schedule`;
   - the ref is `refs/heads/main`;
   - the workflow file is in `allowed_workflows` (now only `live-test.yml`);
   - the repository is `jtouley/coffee-data-tracker`.

   `pull_request` and `pull_request_target` jobs are therefore refused, even approved ones, and so are pushes,
   dispatches on other branches and any other workflow that names the label. The variable `allowed_events` refuses
   PR events at plan time.
2. **CI checks the workflows.** `tests/test_crawler_runner.py` fails if any workflow that targets `crawler` or
   `self-hosted` has a trigger other than `workflow_dispatch` or `schedule`, if a `runs-on` is an expression, or if the
   hook's allow list differs from the workflows that target the label. It also runs the hook against the refused
   cases.
3. **Repository settings: set these before the runner registers.**
   - Settings → Actions → General → *Approval for running fork pull request workflows from contributors*: **Require
     approval for all external contributors**. Never approve a fork PR that touches `.github/` without reading it.
   - Settings → Actions → General → *Workflow permissions*: **Read repository contents and packages permissions**.
     Leave *Allow GitHub Actions to create and approve pull requests* off.
   - A repository on a personal account has no runner groups, so the repo can't restrict which workflows may use
     its runners: layers 1 and 2 do that. If the repo moves to an organization, put the runner in its own runner group
     limited to this repository and, where the plan allows it, to the selected workflows `live-test.yml@refs/heads/main`
     (and `weekly.yml@refs/heads/main` later).
4. **A job that does run is unprivileged.**
   - The runner is the `runner` user: no sudo, no docker group, locked password.
   - The runner user can't reach the metadata server's HTTP API (iptables, owner match), so jobs can't get the VM's
     service account token. DNS still works.
   - That account has no project roles anyway. It can read only the registration-token secret, which has expired
     an hour after you made it.
   - The post-job hook deletes the checkout and the job's temp files, so nothing carries over to the next job.
   - The runner tarball is pinned and its SHA-256 is checked.
5. **Ingress.** Only SSH through IAP, by OS Login (your Google identity). No public SSH port and no other open port.

**What's left.** Jobs that pass the hook run main's own code, so they're trusted like `weekly.yml` on a GitHub
runner. The runner user owns its install folder so the runner can update itself. A compromised main-branch job could
therefore tamper with it, but by then main is compromised, and that is worse on any runner. `terraform destroy` and a
fresh apply rebuild the VM in minutes.

## Owner's steps (GCP Cloud Shell)

The app's project and state bucket already exist (`docs/app/RUNNING.md`, steps 1–2).

1. Set the repository settings in layer 3 above.
2. In Cloud Shell:
   ```bash
   git clone https://github.com/jtouley/coffee-data-tracker && cd coffee-data-tracker
   git checkout poc/crawler-egress-vm          # or main, once merged
   cd src/terraform/crawler
   gcloud config set project coffee-data-tracker
   terraform init -backend-config="bucket=coffee-data-tracker-tfstate"
   ```
3. Create the APIs and the empty secret first. The plan shows **4 to add**: the 3 `apis` and `runner_token`.
   ```bash
   terraform apply -target=google_project_service.apis -target=google_secret_manager_secret.runner_token
   ```
4. **The registration token.** It is valid for one hour, so do step 5 straight after. GitHub → the repo → Settings
   → Actions → Runners → **New self-hosted runner** → Linux. Copy only the value after `--token` in the
   `./config.sh` line. Paste it, then press Ctrl-D twice (no Enter), so it never lands in the shell history:
   ```bash
   gcloud secrets versions add crawler-runner-registration-token --data-file=-
   ```
   Or, with `gh` signed in as the owner:
   `gh api -X POST repos/jtouley/coffee-data-tracker/actions/runners/registration-token --jq .token | tr -d '\n' | gcloud secrets versions add crawler-runner-registration-token --data-file=-`.
   Don't put a PAT or the token in `terraform.tfvars`. Terraform never sees the value.
5. Everything else. The plan shows **7 to add**: network, subnetwork, firewall, address, service account, secret
   IAM member, instance.
   ```bash
   terraform plan
   terraform apply
   terraform output egress_ip
   ```
6. Within about 3 minutes, **Settings → Actions → Runners** shows `crawler-1`, *Idle*, with the labels `self-hosted`,
   `Linux`, `X64` and `crawler`. If it doesn't appear:
   ```bash
   gcloud compute ssh crawler-1 --zone us-central1-a --tunnel-through-iap -- sudo journalctl -u google-startup-scripts -n 50
   ```
   An expired token says so in that log. Add a fresh version (step 4), then run
   `sudo google_metadata_script_runner startup` on the VM.
7. Destroy the spent token, which saves the $0.06 version:
   `gcloud secrets versions destroy 1 --secret=crawler-runner-registration-token`.
8. Optional: the repository variable **`CRAWLER_CONTACT`** (e.g. `support@registry.coffee`). The crawler's
   User-Agent then reads
   `coffee-data/0.1 (+https://github.com/jtouley/coffee-data-tracker; support@registry.coffee)`. Unset, it is
   unchanged.

A VM rebuilt from scratch (destroy, or a changed `machine_type` that recreates it) needs a new token: steps 4–5.
Changing `allowed_workflows` only rewrites metadata. Then run `sudo google_metadata_script_runner startup` on the VM,
or reset it, so the hook's allow list is rewritten. To tear it all down, run `terraform destroy`, then remove the
runner under Settings → Actions → Runners.

## Running `live-test.yml`

`workflow_dispatch` only, and GitHub lists a dispatchable workflow only once it is on the default branch. So the
workflow and `src/coffeedata/livetest.py` must reach `main` first. Then: Actions → **live-test** → Run workflow
(branch `main`), or `gh workflow run live-test.yml --ref main`.

It runs the 2026-10-31 experiment. `coffeedata.livetest` picks the same 200 stores (salt `live429-2026-10-31`) and runs:
- phase A: 100 stores at 1 request/s;
- phase B: the other 100 at 3 requests/s;
- phase C: A's 429 stores again.

At most 700 requests. It uses the crawler's own robots.txt handling, User-Agent and per-host spacing.

Publishing is off:
- the job has no secrets and no HF token, and the script refuses to start if one is set;
- the lake is a folder under `$RUNNER_TEMP`, deleted at the end;
- `huggingface_hub`'s entry points are replaced with ones that raise;
- nothing is uploaded.

The log has aggregates only, per phase: requests, 429 share, stores with a 429 and stores ok (Wilson 95%), 430s,
the limiter's counters, Retry-After values, peak RSS and memory. It never names a store. Compare it with the table
in `live.md`. 5–8% of stores with a 429 would match the other IP, and about 29% would match the GitHub runners.

## Moving `weekly.yml` later (not in this PR)

1. Add `".github/workflows/weekly.yml"` to `allowed_workflows`, run `terraform apply`, then on the VM
   `sudo google_metadata_script_runner startup`.
2. The one-line change, in `weekly.yml`'s `run` job only (the Pages `deploy` job stays on GitHub):
   ```diff
   -    runs-on: ubuntu-latest
   +    runs-on: [self-hosted, crawler]
   ```
   `tests/test_crawler_runner.py` will then require the allow list to include `weekly.yml` (it already does after
   step 1), and `test_the_weekly_run_still_uses_a_github_hosted_runner` is to be deleted with that change.
3. Optionally, pass `COFFEEDATA_CONTACT: ${{ vars.CRAWLER_CONTACT }}` in its `env`.

## Cost per month (us-central1, list prices read 2026-10-31)

| Item | Price | Per month (730 h) |
|---|---|---|
| `e2-small` (default) | $0.016752855/h | **$12.23** |
| `e2-micro` instead | $0.008376428/h | $6.11, or **$0** as the billing account's one free-tier e2-micro |
| Static external IPv4, in use | $0.005/h | **$3.65** (reserved but unused: $0.01/h = $7.30, so don't stop the VM to save money) |
| 20 GB standard persistent disk | $0.000054795/GiB-h (≈ $0.04/GiB-month) | $0.80, or $0 inside the free tier's 30 GB-months |
| Internet egress (Premium) | first 1 GiB free, then $0.12/GiB | ≈ $0: crawl responses are ingress (free); requests and the live test's output are tiny |
| Secret Manager | $0.06 per active version; 6 free per billing account (the app uses 5) | $0, and $0 once the spent version is destroyed (step 7) |
| **Total** | | **≈ $15.90–16.70 with `e2-small`; ≈ $3.65–4.45 with a free-tier `e2-micro`** |

Weekly runs that publish to Hugging Face from the VM add egress at $0.12/GiB past the first GiB. The lake grows by tens
of MB a week, so that's cents. The project's $5 budget (`budget_usd` in `src/terraform/`) will alert with `e2-small`:
raise it to 20 when you apply this.

Sources: [VM pricing, general purpose (E2)](https://cloud.google.com/products/compute/pricing/general-purpose);
[IP address and egress pricing](https://cloud.google.com/vpc/network-pricing);
[disk pricing](https://cloud.google.com/compute/disks-image-pricing);
[free tier](https://cloud.google.com/free/docs/free-cloud-features) (one e2-micro in us-west1, us-central1 or
us-east1, 30 GB-months standard disk, 1 GB egress from North America; it doesn't mention external IPs);
[Secret Manager pricing](https://cloud.google.com/secret-manager/pricing).
