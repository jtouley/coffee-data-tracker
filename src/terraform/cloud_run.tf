# The service, who may call it, and its domain.

resource "google_cloud_run_v2_service" "app" {
  name                = var.service
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = google_service_account.runtime.email
    # One instance takes bursts of cheap requests (a scanner's 404s) rather than Cloud Run queueing them; the
    # default is 80. Sign-ins hash a few at a time (accounts.HASHES_AT_ONCE), so this doesn't multiply memory.
    max_instance_request_concurrency = 250
    # Default 300s. Room for a publish's retries (lake.py, up to ~30s of backoff) and a label read.
    timeout = "120s"
    scaling {
      min_instance_count = 0 # scale to zero between uses
      # One instance only. Shots, accounts, the write lock and the sign-in limits live in the process's memory, so a
      # second instance would serve stale shots, double the guess limits and miss sign-outs made on the other one.
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
      # Start-up reads the lake from Hugging Face before the port opens, so allow up to ~2 minutes, not ~30s.
      startup_probe {
        period_seconds    = 5
        timeout_seconds   = 3
        failure_threshold = 24
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
