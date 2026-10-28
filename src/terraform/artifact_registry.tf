# Where the deploy workflow pushes the app's image.

resource "google_artifact_registry_repository" "app" {
  repository_id = var.service
  location      = var.region
  format        = "DOCKER"
  # A KEEP policy alone deletes nothing: delete images older than 30 days, except the 5 most recent.
  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "2592000s"
    }
  }
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }
  depends_on = [google_project_service.apis]
}
