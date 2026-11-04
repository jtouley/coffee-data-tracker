# The weekly pipeline (docs/ingestion/DESIGN.md, design B) on Cloud Run Jobs and Cloud Scheduler: every request to a
# roaster runs here, not on GitHub Actions. Discovery and probing, one crawl job per connector, the Shopify
# parked-store retry, the weekly merge, post-load enrichment, and the build + gate + publish to the lake. GitHub
# Actions keeps CI, the site deploy (from the served release) and issue forms, none of which contacts a roaster.
# Egress is per connector: crawl-shopify and crawl-retry leave through Cloud NAT with ONE reserved static IP (Direct
# VPC egress, all traffic); every other job uses Cloud Run's default egress. crawl-shopify-default-egress is the
# confirmation run's control arm (default egress, never scheduled). Steps: docs/ingestion/RUNBOOK.md.

variable "ingest_schedule_enabled" {
  description = "Create the weekly Cloud Scheduler entries. Leave false for the confirmation run (jobs run by hand) and until the GitHub weekly workflow is switched off."
  type        = bool
  default     = false
}

variable "ingest_image_tag" {
  description = "Tag of the ingest image (ingest.Dockerfile) in the app's Artifact Registry repository."
  type        = string
  default     = "latest"
}

variable "ingest_image" {
  description = "The full ingest image every job runs, pinned by digest (`<repo>/ingest@sha256:<digest>`, RUNBOOK section 4). Cloud Run resolves a tag only when a job is created or updated, so a rebuilt `:latest` reaches no job until this changes. Empty = the `ingest_image_tag` form, for compatibility."
  type        = string
  default     = ""
}

variable "egress_echo_url" {
  description = "A plain-text \"what is my IP\" endpoint the default-egress jobs ask (cached 60 s) to log their egress IP. Never a store."
  type        = string
  default     = "https://api.ipify.org"
}

variable "verdict_model" {
  description = "COFFEEDATA_VERDICT_MODEL for the build job's verdicts: the same value as the VERDICT_MODEL repository variable. Empty = rules only."
  type        = string
  default     = ""
}

variable "ingest_job_memory" {
  description = "Per-job memory overrides, e.g. { \"crawl-woocommerce\" = \"4Gi\" }: a job named here gets this limit instead of its default in local.ingest_jobs. With 2 vCPU, Cloud Run allows 1Gi to 8Gi."
  type        = map(string)
  default     = {}
  validation {
    # the size in MiB, from 1024 (1Gi) to 8192 (8Gi); anything else (another unit, 16Gi) fails at plan, not apply
    condition = alltrue([
      for mib in [
        for v in values(var.ingest_job_memory) : try(tonumber(regex("^([0-9]+)(Mi|Gi)$", v)[0]) * (endswith(v, "Gi") ? 1024 : 1), 0)
      ] : mib >= 1024 && mib <= 8192
    ])
    error_message = "Each value is a Cloud Run memory size from 1Gi to 8Gi (with 2 vCPU), such as 2Gi or 4096Mi."
  }
}

variable "ingest_subnet_cidr" {
  description = "The Shopify egress subnet (Direct VPC egress needs at least a /26)."
  type        = string
  default     = "10.8.0.0/26"
}

locals {
  ingest_image = var.ingest_image != "" ? var.ingest_image : "${var.region}-docker.pkg.dev/${var.project_id}/${var.service}/ingest:${var.ingest_image_tag}"
  staging      = "/mnt/staging" # COFFEEDATA_STAGING: raw/ and parked/ persist here (GCS) between jobs

  # name => what it runs, whether it leaves through the NAT IP, whether it needs the Claude key, its memory (default;
  # var.ingest_job_memory overrides it per job) and Cloud Run's retries of a failed task (max_retries).
  # Retries: 1 for every job but Shopify's crawls and fetch-health (below). A crawl that dies (OOM kill, crash) is
  # retried at once, and the retry resumes: it compacts the dead attempt's checkpoints, skips the roasters already
  # done this week, fetches the dead attempt's in-flight roasters one at a time and quarantines one that killed two
  # attempts (crashguard), and it stops inside the first attempt's window (connectors._window_left), so it never
  # overlaps the next slot. A crawl stopped by its deadline exits 0 and is never retried. Shopify's crawls stay at 0:
  # a retry straight after a failure would re-ask the stores that just refused, on the one NAT IP; their next slot
  # (15:17, 21:47) resumes.
  ingest_jobs = {
    # Discovery reads open datasets (Overture, OSM), monthly; probing asks stale roasters' sites which platform they
    # run, weekly before the crawls. Both publish what they recorded.
    "discover" = {
      command = ["sh", "-c"]
      args    = ["coffeedata discover overture; coffeedata discover osm; coffeedata publish --message \"discover\""]
      nat     = false, claude = false, memory = "4Gi", retries = 1
    }
    "probe" = {
      command = ["sh", "-c"]
      args    = ["coffeedata probe --stale-days 28 && coffeedata publish --message \"probe\""]
      nat     = false, claude = false, memory = "2Gi", retries = 1
    }
    "crawl-shopify"                = { command = ["coffeedata"], args = ["crawl", "--connector", "shopify"], nat = true, claude = false, memory = "2Gi", retries = 0 }
    "crawl-shopify-default-egress" = { command = ["coffeedata"], args = ["crawl", "--connector", "shopify"], nat = false, claude = false, memory = "2Gi", retries = 0 }
    "crawl-retry"                  = { command = ["coffeedata"], args = ["crawl-retry", "--connector", "shopify"], nat = true, claude = false, memory = "2Gi", retries = 0 }
    # 8Gi, as crawl-generic: with no sitemap cap (#99), each of its 32 workers may hold a whole sitemap's URL list.
    "crawl-squarespace"   = { command = ["coffeedata"], args = ["crawl", "--connector", "squarespace"], nat = false, claude = false, memory = "8Gi", retries = 1 }
    "crawl-woocommerce"   = { command = ["coffeedata"], args = ["crawl", "--connector", "woocommerce"], nat = false, claude = false, memory = "2Gi", retries = 1 }
    "crawl-square-online" = { command = ["coffeedata"], args = ["crawl", "--connector", "square_online"], nat = false, claude = false, memory = "2Gi", retries = 1 }
    # 8Gi: OOM-killed at 2Gi on 2026-11-02 (about 7 min in); the owner raised it by hand (gcloud run jobs update).
    "crawl-generic" = { command = ["coffeedata"], args = ["crawl", "--connector", "generic"], nat = false, claude = false, memory = "8Gi", retries = 1 }
    # The fetch-health check (fetchhealth.py): did each fetch return a catalog of about the right size? Polled
    # through Monday's crawl window, it runs the gate's run_ok_rate, adapter_ok_rate and suspect rules on the
    # unmerged week once a crawl attempt has ended, and writes its list (failed, empty, short) for the owner (the
    # crawl passes re-fetch it only with COFFEEDATA_DQ_REFETCH, set on no job here: off until measured,
    # eval/run_quality/README.md). It judges fetches, not the data in them. Never serves, records a build or
    # publishes. Its non-zero exit (a systemic fetch failure) fails the execution, which emails the owner (alerts.tf,
    # ingest_job_failed). Retries 0: a retry would find the check done and exit 0, hiding the failure. 8Gi, as the
    # build: dbt builds the models run_completeness reads (dim_coffee, with the classifier).
    "fetch-health" = { command = ["coffeedata"], args = ["fetch-health"], nat = false, claude = false, memory = "8Gi", retries = 0 }
    "merge"        = { command = ["coffeedata"], args = ["merge"], nat = false, claude = false, memory = "4Gi", retries = 1 }
    "enrich-week"  = { command = ["coffeedata"], args = ["enrich-week", "--submit"], nat = false, claude = true, memory = "2Gi", retries = 1 }
    # Verdicts, then the build and its gate; publish either way (a failed build's record too); then the egress
    # check, whose non-zero exit (an IP over 8% refusals two runs in a row) fails the execution, which emails the
    # owner (alerts.tf, ingest_job_failed).
    "build" = {
      command = ["sh", "-c"]
      args    = ["coffeedata classify; coffeedata build; rc=$?; coffeedata publish --message \"weekly build\"; coffeedata egress-check || rc=1; exit $rc"]
      nat     = false, claude = true, memory = "8Gi", retries = 1
    }
  }

  # B's slots, anchored at the weekly baseline (Monday 09:17 UTC, as .github/workflows/weekly.yml): every connector's
  # job at once, each on its own instance (only Shopify's on the NAT), then each one's resume 6 h later; a third pass
  # that evening for every connector (Shopify: crawl-retry, its parked stores, on the NAT; the others: their own crawl
  # job again, which resumes and retries every roaster without an ok fetch, on default egress); after each, fetch-health
  # judges the fetches that landed and lists roasters whose fetch failed, came back empty or short (re-fetch is
  # off); Tuesday merge,
  # enrichment, build. The release is stamped Monday and published
  # Tuesday, at most 8 days old (contracts/public-catalog.odcs.yaml, max_run_age_days).
  ingest_schedule = {
    "discover"                   = { job = "discover", cron = "17 3 1 * *" } # the 1st of each month
    "probe"                      = { job = "probe", cron = "17 7 * * 1" }
    "crawl-shopify"              = { job = "crawl-shopify", cron = "17 9 * * 1" }
    "crawl-shopify-resume"       = { job = "crawl-shopify", cron = "17 15 * * 1" }
    "crawl-retry"                = { job = "crawl-retry", cron = "47 21 * * 1" }
    "crawl-squarespace"          = { job = "crawl-squarespace", cron = "17 9 * * 1" }
    "crawl-squarespace-resume"   = { job = "crawl-squarespace", cron = "17 15 * * 1" }
    "crawl-woocommerce"          = { job = "crawl-woocommerce", cron = "17 9 * * 1" }
    "crawl-woocommerce-resume"   = { job = "crawl-woocommerce", cron = "17 15 * * 1" }
    "crawl-square-online"        = { job = "crawl-square-online", cron = "17 9 * * 1" }
    "crawl-square-online-resume" = { job = "crawl-square-online", cron = "17 15 * * 1" }
    "crawl-generic"              = { job = "crawl-generic", cron = "17 9 * * 1" }
    "crawl-generic-resume"       = { job = "crawl-generic", cron = "17 15 * * 1" }
    "crawl-squarespace-retry"    = { job = "crawl-squarespace", cron = "47 21 * * 1" }
    "crawl-woocommerce-retry"    = { job = "crawl-woocommerce", cron = "47 21 * * 1" }
    "crawl-square-online-retry"  = { job = "crawl-square-online", cron = "47 21 * * 1" }
    "crawl-generic-retry"        = { job = "crawl-generic", cron = "47 21 * * 1" }
    # fetch-health: every 20 minutes over Monday 10:07 (50 minutes into the first pass) to Tuesday 02:47 (after the
    # 21:47 pass's 240-minute limit, before the 03:17 merge). One entry, named after its job as every entry is
    # (alerts.tf names the job from the entry), so its hours also cover Monday 00-02 and Tuesday 10-23, when there is
    # nothing to judge: a poll with no crawl attempt ended since the last check exits in seconds. So each
    # connector's pass is judged within 20 minutes of ending.
    "fetch-health" = { job = "fetch-health", cron = "7,27,47 0-2,10-23 * * 1,2" }
    "merge"        = { job = "merge", cron = "17 3 * * 2" }
    "enrich-week"  = { job = "enrich-week", cron = "17 4 * * 2" }
    "build"        = { job = "build", cron = "17 6 * * 2" }
  }
}

resource "google_project_service" "ingest_apis" {
  for_each           = toset(["compute.googleapis.com", "cloudscheduler.googleapis.com", "storage.googleapis.com"])
  service            = each.value
  disable_on_destroy = false
}

# --- Shopify's dedicated egress: a subnet for Direct VPC egress, a router, and Cloud NAT with one static IP ---------

resource "google_compute_network" "ingest" {
  name                    = "${var.service}-ingest"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.ingest_apis]
}

resource "google_compute_subnetwork" "shopify_egress" {
  name          = "${var.service}-shopify-egress"
  network       = google_compute_network.ingest.id
  region        = var.region
  ip_cidr_range = var.ingest_subnet_cidr
}

resource "google_compute_router" "ingest" {
  name    = "${var.service}-ingest"
  network = google_compute_network.ingest.id
  region  = var.region
}

# The one IP Shopify sees from us: a stable identity, never rotated to dodge limits.
resource "google_compute_address" "shopify_egress" {
  name         = "${var.service}-shopify-egress"
  region       = var.region
  address_type = "EXTERNAL"
  depends_on   = [google_project_service.ingest_apis]
}

resource "google_compute_router_nat" "shopify_egress" {
  name                               = "${var.service}-shopify-egress"
  router                             = google_compute_router.ingest.name
  region                             = var.region
  nat_ip_allocate_option             = "MANUAL_ONLY"
  nat_ips                            = [google_compute_address.shopify_egress.self_link]
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"
  # The default (64 ports per VM, static) most likely ran out during arm 2: DuckDB's parallel range reads of the hf:// lake go
  # through this NAT too (Direct VPC egress, all traffic), and the job crashed with "Could not connect to server".
  # Dynamic allocation needs endpoint-independent mapping off. Mirrors the RUNBOOK's (section 3) command,
  # `gcloud compute routers nats update registry-coffee-shopify-egress --router registry-coffee-ingest
  # --region us-central1 --enable-dynamic-port-allocation --min-ports-per-vm=1024 --max-ports-per-vm=8192
  # --no-enable-endpoint-independent-mapping`, so an apply doesn't revert it. Before applying, check that the live
  # NAT has these values (RUNBOOK section 2): any drift shows in the plan.
  enable_dynamic_port_allocation      = true
  enable_endpoint_independent_mapping = false
  min_ports_per_vm                    = 1024
  max_ports_per_vm                    = 8192
  subnetwork {
    name                    = google_compute_subnetwork.shopify_egress.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }
  log_config {
    enable = true
    filter = "ERRORS_ONLY" # port exhaustion and dropped connections
  }
}

# --- Where raw/ and parked/ live between jobs (never published to HF; the merge moves raw/ to archive/) ------------

resource "google_storage_bucket" "ingest_staging" {
  name                        = "${var.project_id}-ingest-staging"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  depends_on                  = [google_project_service.ingest_apis]

  # The merge moves each merged connector partition to archive/raw/... (connectors.archive), which nothing reads:
  # kept 30 days to recover a week from (RUNBOOK §5), then deleted. An object's age counts from its creation, and
  # GCSFuse renames by copy and delete, so the 30 days start at the merge. Nothing outside archive/ is touched.
  lifecycle_rule {
    condition {
      age            = 30
      matches_prefix = ["archive/"]
    }
    action {
      type = "Delete"
    }
  }
}

# --- The account the jobs run as, and the one Cloud Scheduler calls them with --------------------------------------

resource "google_service_account" "ingest" {
  account_id   = "${var.service}-ingest"
  display_name = "registry.coffee ingestion jobs (runtime)"
}

resource "google_service_account" "ingest_scheduler" {
  account_id   = "${var.service}-ingest-sched"
  display_name = "registry.coffee ingestion schedule (runs the jobs)"
}

resource "google_storage_bucket_iam_member" "ingest_staging" {
  bucket = google_storage_bucket.ingest_staging.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.ingest.email}"
}

# The existing secrets (secrets.tf), by name; Terraform never holds their values. Least privilege: HF_TOKEN and
# ANTHROPIC_API_KEY only.
resource "google_secret_manager_secret_iam_member" "ingest" {
  for_each  = toset(["HF_TOKEN", "ANTHROPIC_API_KEY"])
  secret_id = google_secret_manager_secret.app[each.value].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.ingest.email}"
}

# --- The jobs ----------------------------------------------------------------------------------------------------

resource "google_cloud_run_v2_job" "ingest" {
  for_each            = local.ingest_jobs
  name                = each.key
  location            = var.region
  deletion_protection = false

  template {
    task_count = 1
    template {
      service_account = google_service_account.ingest.email
      timeout         = "14400s" # 240 min, the limit every design was held to
      # local.ingest_jobs says why: Shopify's crawls 0, everything else 1 (a crawl's deadline exits 0, never retried).
      max_retries = each.value.retries

      dynamic "vpc_access" {
        for_each = each.value.nat ? [1] : []
        content {
          network_interfaces {
            network    = google_compute_network.ingest.id
            subnetwork = google_compute_subnetwork.shopify_egress.id
          }
          egress = "ALL_TRAFFIC" # every request, robots.txt included, leaves from the NAT IP
        }
      }

      volumes {
        name = "staging"
        gcs {
          bucket    = google_storage_bucket.ingest_staging.name
          read_only = false
        }
      }

      containers {
        image   = local.ingest_image
        command = each.value.command
        args    = each.value.args
        resources {
          limits = {
            cpu    = "2"
            memory = lookup(var.ingest_job_memory, each.key, each.value.memory)
          }
        }
        volume_mounts {
          name       = "staging"
          mount_path = local.staging
        }
        env {
          name  = "COFFEEDATA_STAGING"
          value = local.staging
        }
        env {
          name  = "COFFEEDATA_EGRESS_MODE" # recorded in job_manifests.detail.egress
          value = each.value.nat ? "dedicated" : "default"
        }
        # The NAT IP is known in advance; default egress is looked up (and may change between connections).
        env {
          name  = each.value.nat ? "COFFEEDATA_EGRESS_IP" : "COFFEEDATA_EGRESS_ECHO_URL"
          value = each.value.nat ? google_compute_address.shopify_egress.address : var.egress_echo_url
        }
        env {
          name = "HF_TOKEN"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.app["HF_TOKEN"].secret_id
              version = "latest"
            }
          }
        }
        dynamic "env" {
          for_each = each.value.claude && var.verdict_model != "" ? [var.verdict_model] : []
          content {
            name  = "COFFEEDATA_VERDICT_MODEL"
            value = env.value
          }
        }
        dynamic "env" {
          for_each = each.value.claude ? ["ANTHROPIC_API_KEY"] : []
          content {
            name = env.value
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.app[env.value].secret_id
                version = "latest"
              }
            }
          }
        }
      }
    }
  }

  lifecycle {
    # `gcloud run jobs update` (e.g. the hand-set --max-retries 0) stamps these; they are not configuration.
    ignore_changes = [client, client_version]
    # a misspelt job name in var.ingest_job_memory would otherwise be ignored by lookup() above, silently
    precondition {
      condition     = length(setsubtract(keys(var.ingest_job_memory), keys(local.ingest_jobs))) == 0
      error_message = "var.ingest_job_memory names a job that isn't in local.ingest_jobs (a misspelt job name?)."
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.ingest,
    google_storage_bucket_iam_member.ingest_staging,
    google_compute_router_nat.shopify_egress,
  ]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler" {
  for_each = google_cloud_run_v2_job.ingest
  name     = each.value.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.ingest_scheduler.email}"
}

# --- The weekly schedule (off for the confirmation run) ----------------------------------------------------------

resource "google_cloud_scheduler_job" "ingest" {
  for_each         = { for k, v in local.ingest_schedule : k => v if var.ingest_schedule_enabled }
  name             = "${var.service}-${each.key}"
  region           = var.region
  schedule         = each.value.cron
  time_zone        = "Etc/UTC"
  attempt_deadline = "60s"

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.ingest[each.value.job].name}:run"
    oauth_token {
      service_account_email = google_service_account.ingest_scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
  depends_on = [google_cloud_run_v2_job_iam_member.scheduler]
}

# --- Outputs -----------------------------------------------------------------------------------------------------

output "shopify_egress_ip" {
  description = "The one static IP crawl-shopify and crawl-retry leave from (for Shopify's form and Support)."
  value       = google_compute_address.shopify_egress.address
}

output "ingest_jobs" {
  description = "The ingestion Cloud Run job names."
  value       = sort([for j in google_cloud_run_v2_job.ingest : j.name])
}

output "ingest_image" {
  description = "The image every ingest job runs (pinned by digest once `ingest_image` is set)."
  value       = local.ingest_image
}

output "ingest_staging_bucket" {
  description = "The bucket mounted at COFFEEDATA_STAGING (raw/ and parked/ between jobs)."
  value       = google_storage_bucket.ingest_staging.name
}
