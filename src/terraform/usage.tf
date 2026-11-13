# The usage collector (app/usage.py): its own small service on e.registry.coffee, not the app. The app is one
# instance holding every account and is same-site with registry.coffee, so a beacon sent to it would carry the
# session cookie; this service sets no cookie, reads none, and holds only a write token for the private usage
# dataset. Same image as the app; COFFEEDATA_APP picks the module (app/Dockerfile).

resource "google_service_account" "usage" {
  account_id   = "${var.service}-usage"
  display_name = "registry.coffee usage collector (runtime)"
}

# A Hugging Face token that can write only to the usage dataset (a fine-grained token scoped to that one repository).
# Terraform makes the empty secret; the owner adds its value, then sets usage_token = true (RUNNING.md, "Usage").
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

resource "google_cloud_run_v2_service" "usage" {
  name                = "${var.service}-usage"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = google_service_account.usage.email
    # A beacon is answered at once (204) and costs microseconds; the default concurrency is plenty.
    max_instance_request_concurrency = 80
    timeout                          = "10s"
    scaling {
      min_instance_count = 0 # scale to zero between uses
      max_instance_count = 2 # the cost ceiling; the per-prefix limits are per instance
    }
    containers {
      # The first apply runs Google's sample image; a deploy puts the app's image in (ignore_changes below).
      image = "us-docker.pkg.dev/cloudrun/container/hello"
      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi" # events wait in memory between flushes; staged files are on the in-memory filesystem
        }
        cpu_idle          = true # CPU only during requests; the request path and shutdown flush too
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
    # As the app's service (cloud_run.tf): deploys set the image; the service-level scaling block reads back as zeros.
    ignore_changes = [template[0].containers[0].image, client, client_version, scaling]
  }
  depends_on = [google_secret_manager_secret_iam_member.usage_token]
}

# Pages on registry.coffee and app.registry.coffee post to it without credentials, so it is public. It answers 204
# to anything, and drops foreign origins, oversized bodies and bursts (app/usage.py).
resource "google_cloud_run_v2_service_iam_member" "usage_public" {
  name     = google_cloud_run_v2_service.usage.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# The domain is verified already (app.registry.coffee). Afterwards: the CNAME e -> ghs.googlehosted.com.
resource "google_cloud_run_domain_mapping" "usage" {
  name     = var.usage_domain
  location = var.region
  metadata {
    namespace = var.project_id
  }
  spec {
    route_name = google_cloud_run_v2_service.usage.name
  }
}

# Cloud Run's request log line holds the caller's full IP and user agent: drop the collector's from the _Default
# sink, so they are never stored. Its own log (counts per flush) is kept; so are the other services' request logs.
resource "google_logging_project_exclusion" "usage_requests" {
  name        = "${var.service}-usage-requests"
  description = "The usage collector's request logs: they hold full IPs and user agents (app/usage.py)."
  filter      = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.usage.name}\"",
    "logName=\"projects/${var.project_id}/logs/run.googleapis.com%2Frequests\"",
  ])
}
