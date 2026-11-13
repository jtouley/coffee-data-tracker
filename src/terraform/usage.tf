# The usage collector (app/usage.py): its own small service at its run.app address, not the app. The app is one
# instance holding every account and is same-site with registry.coffee, so a beacon sent to it would carry the
# session cookie; run.app is a public suffix, so no registry.coffee cookie ever reaches this service, which sets no
# cookie, reads none, and holds only a write token for the private usage dataset. Same image as the app;
# COFFEEDATA_APP picks the module (app/Dockerfile). No custom domain: its address is Cloud Run's deterministic one,
# https://<service>-<project number>.<region>.run.app (output usage_url; config/usage.toml for the page scripts).
#
# Deployed by Terraform, like the ingest jobs, never by the deploy workflow: the service runs `usage_image`, an app
# image deploy.yml built from main and attested, pinned by digest in ci.tfvars. A new version is a PR that bumps the
# digest, then the GitHub apply. deploy.yml tags the pinned digest so the registry's cleanup never deletes it.
#
# Two switches, both in ci.tfvars and both off by default (RUNNING.md, "Usage"):
# - usage_enabled: the service, its public invoker, its request-log exclusion and its alert. The apply account must
#   first be able to act as the collector's account and to write log exclusions (bootstrap/iam.tf; RUNNING.md,
#   "Usage"): until then an apply of these would fail partway, so the default creates only the account and the
#   empty secret, which the apply and plan accounts can already manage.
# - usage_token: mounts the secret once it has a value.

locals {
  usage_service = "${var.service}-usage"
  usage_count   = var.usage_enabled ? 1 : 0
}

resource "google_service_account" "usage" {
  account_id   = local.usage_service
  display_name = "registry.coffee usage collector (runtime)"
}

# A Hugging Face token that can write only to the usage dataset (a fine-grained token scoped to that one repository).
# Terraform makes the empty secret; its value is added in the Cloud Console, never through Terraform.
resource "google_secret_manager_secret" "usage_token" {
  secret_id = "USAGE_HF_TOKEN"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# Least privilege: the collector's account reads this one secret and nothing else.
resource "google_secret_manager_secret_iam_member" "usage_token" {
  secret_id = google_secret_manager_secret.usage_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.usage.email}"
}

# Cloud Run's request log line holds the caller's full IP and user agent: drop the collector's from the _Default
# sink, so they are never stored. Made before the service (depends_on there), so no request is logged before it.
# Its own log (counts per flush) is kept; so are the other services' request logs.
resource "google_logging_project_exclusion" "usage_requests" {
  count = local.usage_count

  name        = "${local.usage_service}-requests"
  description = "The usage collector's request logs: they hold full IPs and user agents (app/usage.py)."
  filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${local.usage_service}\"",
    "logName=\"projects/${var.project_id}/logs/run.googleapis.com%2Frequests\"",
  ])
}

resource "google_cloud_run_v2_service" "usage" {
  count = local.usage_count

  name                = local.usage_service
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = google_service_account.usage.email
    # A beacon is answered at once (204) and costs microseconds; the default concurrency is plenty.
    max_instance_request_concurrency = 80
    timeout                          = "30s" # a flush runs in the request that finds it due (app/usage.py FLUSH_WAIT)
    scaling {
      min_instance_count = 0 # scale to zero between uses
      max_instance_count = 2 # the cost ceiling; the per-prefix limits are per instance
    }
    containers {
      # Google's sample image until ci.tfvars pins one (it ignores COFFEEDATA_APP and answers every path).
      image = var.usage_image != "" ? var.usage_image : "us-docker.pkg.dev/cloudrun/container/hello"
      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi" # events wait in memory between flushes; staged files are on the in-memory filesystem
        }
        # CPU only while a request is open (and at shutdown): request-based billing, nothing while idle. So the
        # collector flushes inside a request, never on a background thread after its reply (app/usage.py).
        cpu_idle          = true
        startup_cpu_boost = true
      }
      env {
        name  = "COFFEEDATA_APP"
        value = "app.usage"
      }
      env {
        name  = "COFFEEDATA_USAGE"
        value = "hf://datasets/${var.usage_dataset}"
      }
      env {
        name  = "COFFEEDATA_USAGE_ORIGINS"
        value = join(",", [var.site_url, "https://www.${trimprefix(var.site_url, "https://")}", "https://${var.domain}"])
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      dynamic "env" {
        # Mounted only once the secret has a value: the service reads it at version latest (RUNNING.md, "Usage").
        for_each = var.usage_token ? { HF_TOKEN = google_secret_manager_secret.usage_token.secret_id } : {}
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }
      startup_probe {
        period_seconds    = 5
        timeout_seconds   = 3
        failure_threshold = 12
        http_get {
          path = "/healthz"
        }
      }
    }
  }

  lifecycle {
    # Unlike the app's service, Terraform owns the image (usage_image). client and client_version: set by gcloud if
    # anyone ever deploys by hand. scaling: the service-level block reads back as zeros (cloud_run.tf).
    ignore_changes = [client, client_version, scaling]
  }
  depends_on = [google_secret_manager_secret_iam_member.usage_token, google_logging_project_exclusion.usage_requests]
}

# Pages on registry.coffee and app.registry.coffee post to it without credentials, so it is public. It answers 204
# to anything, and drops foreign origins, oversized bodies and bursts (app/usage.py).
resource "google_cloud_run_v2_service_iam_member" "usage_public" {
  count = local.usage_count

  name     = google_cloud_run_v2_service.usage[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# Lost or dropped events: the collector's flush line logs at WARNING when a limit dropped events or a publish failed,
# and at ERROR when events were lost (a failed write with no room left, or a shutdown that couldn't publish). Its own
# log holds counts only, so the alert can quote them.
resource "google_monitoring_alert_policy" "usage_dropped" {
  count = local.usage_count

  display_name = "registry.coffee: usage events dropped or lost"
  combiner     = "OR"
  conditions {
    display_name = "app.usage logged at WARNING or above"
    condition_matched_log {
      filter = join(" AND ", [
        "resource.type=\"cloud_run_revision\"",
        "resource.labels.service_name=\"${local.usage_service}\"",
        "jsonPayload.logger=\"app.usage\"",
        "severity>=WARNING",
      ])
    }
  }
  documentation {
    mime_type = "text/markdown"
    subject   = "registry.coffee: usage events dropped or lost"
    content   = <<-EOT
      The usage collector dropped or lost events. Its log line holds only counts: `rate_limited`, `ceiling`, `daily_cap`, `overflow` and `staged_full` are events a limit dropped (traffic, or a flood); `publish_failed` means Hugging Face refused or was unreachable and the events wait in staging; `lost` and `write_failed` are events that will never be stored. Logs Explorer: resource "Cloud Run Revision", service `${local.usage_service}`, logger `app.usage`. Steps: docs/app/RUNNING.md, "Usage".
    EOT
  }
  alert_strategy {
    notification_rate_limit {
      period = "21600s" # at most one every six hours: the counts are directional, a flood would repeat it
    }
    auto_close = "3600s"
  }
  notification_channels = [google_monitoring_notification_channel.owner.id]
}
