# Where the deploy workflow pushes the app's image and the ingestion jobs' (<repository>/app, <repository>/ingest).

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
  # deploy.yml pushes an ingest image for every commit it deploys, so the image the Cloud Run jobs are pinned to
  # (`ingest_image`) is soon neither recent nor, after 30 days, young. The owner tags each image they pin
  # `pinned-<commit>` (docs/ingestion/RUNBOOK.md §1 "At merge" and §4), and an image tagged like that is never
  # deleted, until someone removes the tag. `latest` is kept too: only the emergency build by hand pushes it now.
  # The 5 most recent are counted for each image name (app, ingest) on its own.
  cleanup_policies {
    id     = "keep-pinned"
    action = "KEEP"
    condition {
      tag_state    = "TAGGED"
      tag_prefixes = ["pinned", "latest"]
    }
  }
  depends_on = [google_project_service.apis]
}
