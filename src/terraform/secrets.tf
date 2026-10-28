# The app's secrets and the account the service runs as.

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
