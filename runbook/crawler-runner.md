# The crawler runner: a fixed egress IP (proof of concept)

GitHub-hosted runners share Azure addresses with other scrapers. On those runners, 29% of Shopify stores answered
the crawl with a 429 [25.0, 33.8]. From another IP, 5–8% did (the 2026-10-31 live test). `src/terraform/crawler/`
(its own Terraform state) describes one small GCP VM with a reserved static IP. The VM runs a GitHub Actions
self-hosted runner with the label `crawler`, so the crawl leaves from one identifiable address. It is not for
rotating IPs. Nothing is created until you apply it.

`terraform plan` from scratch is **11 to add**:
- `google_project_service.apis` ×3 (compute, secretmanager, iap);
- `google_compute_network.crawler` and `google_compute_subnetwork.crawler`: its own VPC, not `default`;
- `google_compute_firewall.iap_ssh`: tcp/22 from IAP's `35.235.240.0/20`, the only ingress;
- `google_compute_address.egress`: the static IP;
- `google_service_account.crawler`: no project roles;
- `google_secret_manager_secret.runner_token` and `google_secret_manager_secret_iam_member.crawler`;
- `google_compute_instance.crawler`: `crawler-1`, `e2-micro`, Ubuntu 24.04, Shielded VM, OS Login.

**Cost:** about $3.65 a month for the IP, with the VM and disk inside the free tier (one e2-micro and 30 GB of
standard disk per billing account, [free tier](https://cloud.google.com/free/docs/free-cloud-features); IP
[pricing](https://cloud.google.com/vpc/network-pricing) $0.005/h). `e2-micro` (1 GB plus 2 GB swap) is enough for
the live test's crawl, which peaks at about 370 MB. Before `weekly.yml` moves here, set `machine_type = "e2-small"`
(about $12 a month more): its dbt build alone peaked at 0.7–1.0 GB on a copy of the prod lake.

## Security model (public repo)

- **The VM refuses other jobs.** The pre-job hook `scripts/job-started.sh` fails every job before its first step
  unless all of these hold:
  - it is `workflow_dispatch` or `schedule`;
  - it runs on `refs/heads/main`;
  - its workflow is `.github/workflows/live-test.yml`;
  - it comes from `jtouley/coffee-data-tracker`.

  Fork PRs (approved or not), `pull_request_target`, pushes and other branches are all refused. The allow list is
  hard-coded in the hook.
- **The hook can't be emptied or switched off.**
  - It is installed by writing a temp file and renaming it into place, and only if it is complete: non-empty, with
    both markers, and it parses.
  - The runner's unit won't start without a good hook.
  - Root owns `/opt/actions-runner` (install, `.env`, credentials) and the hook. The unit has
    `ReadOnlyPaths=/opt/actions-runner`, and only `_work` and `_diag` are writable.
  - Self-update is off (`--disableupdate`).
- **Jobs are unprivileged.**
  - They run as `runner`: no sudo, no docker.
  - They can reach the metadata server only for DNS (port 53), so they can't use the VM's service account.
  - `KillMode=control-group` stops every process a job leaves behind.
- **CI** (`tests/test_crawler_runner.py`) fails if a workflow that targets the label has any trigger other than
  dispatch or schedule, or if the hook's allow list doesn't match those workflows. It also runs the hook against
  the cases it must refuse.
- **Repository settings: set these first** (Settings → Actions → General):
  - *Approval for running fork pull request workflows from contributors*: **Require approval for all external
    contributors**;
  - *Workflow permissions*: read-only.

  A personal-account repo has no runner groups, so the hook is what restricts which workflows can use the runner.

**Runner updates are manual.** Bump `runner_version` and `runner_sha256` (from the release notes), then rebuild the
VM: `terraform apply -replace=google_compute_instance.crawler`, with a fresh token (step 3). GitHub stops sending jobs
to a runner more than 30 days behind a release.

## Owner's steps (GCP Cloud Shell)

The project and the state bucket already exist (`docs/app/RUNNING.md`, steps 1–2).

1. Set the repository settings above.
2. Clone the repo, check out `poc/crawler-egress-vm` (or `main`), then:
   ```bash
   cd src/terraform/crawler
   terraform init -backend-config="bucket=coffee-data-tracker-tfstate"
   terraform apply -target=google_project_service.apis -target=google_secret_manager_secret.runner_token   # 4 to add
   ```
3. **Token** (valid for one hour, so do step 4 straight away). Go to Settings → Actions → Runners → New self-hosted
   runner and copy the value after `--token`. Paste it, then press Ctrl-D twice, so it never lands in the shell
   history:
   `gcloud secrets versions add crawler-runner-registration-token --data-file=-`
4. `terraform plan` (7 to add), `terraform apply`, then `terraform output egress_ip`.
5. Within a few minutes, `crawler-1` shows as *Idle* under Settings → Actions → Runners. If it doesn't:
   `gcloud compute ssh crawler-1 --zone us-central1-a --tunnel-through-iap -- sudo journalctl -u google-startup-scripts -n 50`.
   An expired token says so in that log. Add a fresh one (step 3), then run
   `sudo google_metadata_script_runner startup` on the VM.
6. Destroy the spent token:
   ```bash
   gcloud secrets versions list crawler-runner-registration-token --filter=state=enabled --format='value(name)' |
     xargs -r -n1 gcloud secrets versions destroy --secret=crawler-runner-registration-token --quiet
   ```
7. To tear it down: `terraform destroy`, then remove the runner under Settings → Actions → Runners.

## Running `live-test.yml`

It is `workflow_dispatch` only. GitHub offers dispatch only for workflows on the default branch, so
`live-test.yml` and `src/coffeedata/livetest.py` must reach `main` first. Then go to Actions → **live-test** → Run
workflow, or run `gh workflow run live-test.yml --ref main`.

It runs the 2026-10-31 experiment on the same 200 stores:
- phase A: 100 stores at 1 request/s;
- phase B: the other 100 at 3 requests/s;
- phase C: A's 429 stores again.

At most 700 requests in all. Publishing is off:
- the job has no secrets or HF token;
- the lake is a throwaway folder;
- Hugging Face calls raise;
- nothing is uploaded.

The log prints aggregates only, with the same definitions as `live.md`'s table, so the numbers compare directly.
