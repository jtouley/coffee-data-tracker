# GitHub Actions deploys with Workload Identity Federation: no JSON keys. Only deploy.yml, run from main of
# var.github_repo, may. workflow_ref is "<repo>/<path>@<ref>" of the workflow that runs (GitHub's OIDC reference);
# deploy.yml's runs from main, on workflow_run or workflow_dispatch, are ".github/workflows/deploy.yml@refs/heads/main"
# (workflow_run runs the default branch's file, at the default branch's ref). No other workflow can use this provider.

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  depends_on                = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub"
  attribute_mapping = {
    "google.subject"         = "assertion.sub"
    "attribute.repository"   = "assertion.repository"
    "attribute.ref"          = "assertion.ref"
    "attribute.workflow_ref" = "assertion.workflow_ref"
  }
  attribute_condition = join(" && ", [
    "assertion.repository == '${var.github_repo}'",
    "assertion.ref == 'refs/heads/main'",
    "assertion.workflow_ref == '${var.github_repo}/.github/workflows/deploy.yml@refs/heads/main'",
  ])
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "deploy" {
  account_id   = "${var.service}-deploy"
  display_name = "registry.coffee deploys (GitHub Actions)"
}

resource "google_service_account_iam_member" "deploy_from_github" {
  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repo}"
}

# Push images to this one repository, update this one service, and run it as the runtime account.
resource "google_artifact_registry_repository_iam_member" "deploy" {
  repository = google_artifact_registry_repository.app.name
  location   = var.region
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_cloud_run_v2_service_iam_member" "deploy" {
  name     = google_cloud_run_v2_service.app.name
  location = var.region
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_service_account_iam_member" "deploy_acts_as_runtime" {
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deploy.email}"
}
