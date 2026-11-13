# admin.registry.coffee: the owner's command center (docs/admin/README.md, docs/design/admin-registry.md §5.3).
# The app's image, run as its own service, under its own service account. That account holds only:
#   - read access to its three secrets (values are never in Terraform or its state; the owner adds them);
#   - create and read, never overwrite or delete, on the bucket that keeps its sign-in state (passkeys, sessions,
#     recovery codes, the spent enrollment record), so the one-shot records it writes can't be rewritten by it;
#   - nothing on the lake in GCP: the lake is a Hugging Face dataset, read with ADMIN_HF_TOKEN, a fine-grained token
#     the owner makes with read access to the lake dataset only.
# Sign-in is in code (passkeys), as for the app, so the service itself is public. Every route but /health and the
# sign-in routes needs a session, and the service answers 503 to everything when its settings are incomplete.

locals {
  # Secret Manager id => the environment variable the admin reads it as.
  admin_secrets = {
    ADMIN_SESSION_SECRET = "ADMIN_SESSION_SECRET" # 32+ random characters: signs the session cookie
    ADMIN_ENROLL_TOKEN   = "ADMIN_ENROLL_TOKEN"   # 32+ random characters: enrolls the first passkey, once
    ADMIN_HF_TOKEN       = "HF_TOKEN"             # read-only on the lake dataset: never the app's HF_TOKEN
  }
}

resource "google_project_service" "admin_storage" {
  service            = "storage.googleapis.com"
  disable_on_destroy = false
}

resource "google_service_account" "admin" {
  account_id   = "${var.service}-admin-run"
  display_name = "admin.registry.coffee (runtime)"
}

resource "google_secret_manager_secret" "admin" {
  for_each  = local.admin_secrets
  secret_id = each.key
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "admin" {
  for_each  = google_secret_manager_secret.admin
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.admin.email}"
}

# The sign-in state. Versioned, private, one region. The admin may create and read objects; creating an object
# that exists needs delete permission, which it doesn't have, so every record is written once.
resource "google_storage_bucket" "admin_state" {
  name                        = "${var.project_id}-admin-state"
  location                    = upper(var.region)
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning {
    enabled = true
  }
  depends_on = [google_project_service.admin_storage]
}

resource "google_storage_bucket_iam_member" "admin_state" {
  # Plain keys: the plan guard (scripts/tf_plan_guard.py) prints only addresses whose keys are plain words.
  for_each = { creator = "roles/storage.objectCreator", viewer = "roles/storage.objectViewer" }
  bucket   = google_storage_bucket.admin_state.name
  role     = each.value
  member   = "serviceAccount:${google_service_account.admin.email}"
}

resource "google_cloud_run_v2_service" "admin" {
  name                = "${var.service}-admin"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account                  = google_service_account.admin.email
    max_instance_request_concurrency = 40
    timeout                          = "60s" # a page's lake read gives up at 25 s
    scaling {
      min_instance_count = 0
      # One instance: sessions are cached and sign-in attempts limited in memory (the records are in the bucket).
      max_instance_count = 1
    }
    containers {
      # The first apply runs Google's sample image; the owner then deploys the app's image (docs/admin/README.md).
      image   = "us-docker.pkg.dev/cloudrun/container/hello"
      command = ["sh", "-c", "exec uvicorn --factory admin.main:create_app --host 0.0.0.0 --port \"$PORT\" --no-proxy-headers"]
      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi" # the Catalog's per-build DuckDB files are on the in-memory filesystem
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }
      env {
        name  = "ADMIN_ORIGIN"
        value = "https://${var.admin_domain}"
      }
      env {
        name  = "ADMIN_STATE_BUCKET"
        value = google_storage_bucket.admin_state.name
      }
      env {
        name  = "ADMIN_CACHE_DIR"
        value = "/tmp/admin-cache"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      dynamic "env" {
        for_each = google_secret_manager_secret.admin
        content {
          name = local.admin_secrets[env.key]
          value_source {
            secret_key_ref {
              secret  = env.value.secret_id
              version = "latest"
            }
          }
        }
      }
      # /health answers 503 while the settings are incomplete, so a misconfigured revision never takes traffic.
      startup_probe {
        period_seconds    = 5
        timeout_seconds   = 3
        failure_threshold = 12
        http_get {
          path = "/health"
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].containers[0].image, client, client_version, scaling]
  }
  depends_on = [google_secret_manager_secret_iam_member.admin, google_storage_bucket_iam_member.admin_state]
}

resource "google_cloud_run_v2_service_iam_member" "admin_public" {
  name     = google_cloud_run_v2_service.admin.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# Before this applies: the domain is verified for the applying account and its CAA record allows pki.goog, as for
# the app (docs/app/RUNNING.md). Afterwards: the CNAME admin -> ghs.googlehosted.com.
resource "google_cloud_run_domain_mapping" "admin" {
  name     = var.admin_domain
  location = var.region
  metadata {
    namespace = var.project_id
  }
  spec {
    route_name = google_cloud_run_v2_service.admin.name
  }
}

# Any ERROR line or 5xx: a lake read that failed, the sign-in state unreadable, or the service switched off.
resource "google_monitoring_alert_policy" "admin_errors" {
  display_name = "admin.registry.coffee: errors"
  combiner     = "OR"
  conditions {
    display_name = "ERROR log or 5xx"
    condition_matched_log {
      filter = join(" AND ", [
        "resource.type=\"cloud_run_revision\"",
        "resource.labels.service_name=\"${google_cloud_run_v2_service.admin.name}\"",
        "(severity>=ERROR OR httpRequest.status>=500)",
      ])
    }
  }
  alert_strategy {
    notification_rate_limit {
      period = "300s"
    }
    auto_close = "1800s"
  }
  notification_channels = [google_monitoring_notification_channel.owner.id]
}

# An email whenever the passkeys change: enrollment, a passkey added, a recovery code used. None should surprise.
resource "google_monitoring_alert_policy" "admin_passkeys" {
  display_name = "admin.registry.coffee: passkeys changed"
  combiner     = "OR"
  conditions {
    display_name = "enroll, add_passkey or recover succeeded"
    condition_matched_log {
      filter = join(" AND ", [
        "resource.type=\"cloud_run_revision\"",
        "resource.labels.service_name=\"${google_cloud_run_v2_service.admin.name}\"",
        "jsonPayload.event=\"admin_auth\"",
        "jsonPayload.outcome=\"ok\"",
        "jsonPayload.action=(\"enroll\" OR \"add_passkey\" OR \"recover\")",
      ])
    }
  }
  alert_strategy {
    notification_rate_limit {
      period = "300s"
    }
    auto_close = "1800s"
  }
  notification_channels = [google_monitoring_notification_channel.owner.id]
}

# The language decision's revisit trigger (docs/design/admin-registry.md §7.1): container startup p95 above 2 s over
# an hour, with at least admin_cold_start_min_samples starts, means revisit Go against Python.
# - The metric is run.googleapis.com/container/startup_latencies (BETA; a DELTA distribution in ms, sampled every
#   60 s), on cloud_run_revision among other resources, so the PromQL needs monitored_resource. It measures the
#   container's start, not the first request's own work.
# - PromQL names (Cloud Monitoring's mapping: the first / becomes :, other special characters _, and a distribution
#   is a histogram with _bucket, _count and _sum): run_googleapis_com:container_startup_latencies_bucket and _count.
# - condition_prometheus_query_language is in hashicorp/google 6.x (checked in the 6.50.0 schema). A threshold
#   condition couldn't also require a minimum number of starts.
resource "google_monitoring_alert_policy" "admin_cold_start" {
  display_name = "admin.registry.coffee: cold start p95 > 2 s"
  combiner     = "OR"
  conditions {
    display_name = "container startup p95 over 1 h, at least ${var.admin_cold_start_min_samples} starts"
    condition_prometheus_query_language {
      query    = <<-EOT
        histogram_quantile(0.95, sum by (le) (increase(run_googleapis_com:container_startup_latencies_bucket{monitored_resource="cloud_run_revision",service_name="${google_cloud_run_v2_service.admin.name}"}[1h]))) > 2000
        and on() sum(increase(run_googleapis_com:container_startup_latencies_count{monitored_resource="cloud_run_revision",service_name="${google_cloud_run_v2_service.admin.name}"}[1h])) >= ${var.admin_cold_start_min_samples}
      EOT
      duration = "0s"
    }
  }
  documentation {
    content   = "admin cold start p95 > 2 s: revisit the Go vs Python decision (docs/design/admin-registry.md §7.1)."
    mime_type = "text/markdown"
  }
  alert_strategy {
    auto_close = "86400s"
  }
  notification_channels = [google_monitoring_notification_channel.owner.id]
}

output "admin_url" {
  description = "The admin service's run.app URL (admin.registry.coffee once the domain mapping is live)."
  value       = google_cloud_run_v2_service.admin.uri
}

output "admin_state_bucket" {
  value = google_storage_bucket.admin_state.name
}
