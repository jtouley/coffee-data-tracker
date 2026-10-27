# registry.coffee on Cloud Run. Terraform makes the empty secrets; the owner adds their values (RUNNING.md).

locals {
  # The app's secrets, mounted as environment variables. Values are never in Terraform or its state.
  secrets = ["HF_TOKEN", "ANTHROPIC_API_KEY", "AUTH_PEPPER", "SESSION_SECRET", "OWNER_SETUP_CODE"]
  image   = "${var.region}-docker.pkg.dev/${var.project_id}/${var.service}/app"
}

resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    "billingbudgets.googleapis.com",
    "cloudresourcemanager.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "app" {
  repository_id = var.service
  location      = var.region
  format        = "DOCKER"
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }
  depends_on = [google_project_service.apis]
}

# --- the service and what it may read ----------------------------------------------------------------------------

resource "google_service_account" "runtime" {
  account_id   = "${var.service}-run"
  display_name = "registry.coffee app (runtime)"
}

resource "google_secret_manager_secret" "app" {
  for_each  = toset(local.secrets)
  secret_id = each.value
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# Least privilege: the runtime account can read these five secrets and nothing else in the project.
resource "google_secret_manager_secret_iam_member" "runtime" {
  for_each  = google_secret_manager_secret.app
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_cloud_run_v2_service" "app" {
  name                = var.service
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = google_service_account.runtime.email
    scaling {
      min_instance_count = 0 # scale to zero between uses
      # One instance only. Shots, the write lock and the sign-in limits live in the process's memory, so a second
      # instance would serve stale shots and double the guess limits.
      max_instance_count = 1
    }
    containers {
      # The first apply runs Google's sample image; the deploy workflow puts the app's image in.
      image = "us-docker.pkg.dev/cloudrun/container/hello"
      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi" # staged lake files are on the in-memory filesystem
        }
        cpu_idle          = true # CPU only during requests
        startup_cpu_boost = true
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id # for the trace field in the JSON logs
      }
      dynamic "env" {
        for_each = google_secret_manager_secret.app
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value.secret_id
              version = "latest"
            }
          }
        }
      }
      startup_probe {
        http_get {
          path = "/healthz"
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].containers[0].image, client, client_version] # set by each deploy
  }
  depends_on = [google_secret_manager_secret_iam_member.runtime]
}

# The app does its own sign-in: every page but sign-in, join and reset needs an account, and every write needs
# the signed-in account (tests/test_m8_deploy.py). So the service itself is public.
resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.app.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# Before this applies: the applying account is a verified owner of the domain in Search Console, and the domain's
# CAA record allows pki.goog (RUNNING.md). Afterwards: the CNAME app -> ghs.googlehosted.com.
resource "google_cloud_run_domain_mapping" "app" {
  name     = var.domain
  location = var.region
  metadata {
    namespace = var.project_id
  }
  spec {
    route_name = google_cloud_run_v2_service.app.name
  }
}
