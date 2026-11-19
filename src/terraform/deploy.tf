# GitHub Actions deploys with Workload Identity Federation: no JSON keys. Only the jobs of deploy.yml itself, run
# from main of this repository, may. The condition reads GitHub's OIDC token claims, in the bootstrap's style
# (src/terraform/bootstrap/main.tf):
# - the repository by its numeric ids as well as its name, and its owner's: a name can be deleted and registered again
#   by someone else, an id can't. The account's binding keys on the id too;
# - a GitHub-hosted runner;
# - main, on the two events deploy.yml deploys from: workflow_run (ci passed) and workflow_dispatch. workflow_run runs
#   the default branch's file, at the default branch's ref;
# - workflow_ref, "<repo>/<path>@<ref>" of the top-level workflow, and job_workflow_ref, the file whose job asks: a
#   job of a reusable workflow that deploy.yml calls has that file's job_workflow_ref, not deploy.yml's, so it is
#   refused. That is how `attest` is kept off this account: deploy.yml calls it as a reusable workflow (attest.yml).
#   That holds once this condition is applied; the guard refuses any change here, so the owner applies it by hand
#   (docs/app/RUNNING.md, "Changing this provider or its grant").
#
# GitHub's token names no job: every job written in deploy.yml itself has the same claims, so this condition can't
# tell one of its jobs from another. Which of them may sign in is decided in deploy.yml, by `id-token: write`
# (tests/test_deploy_identity.py checks only the jobs that push or deploy hold it).

locals {
  deploy_workflow = "${var.github_repo}/.github/workflows/deploy.yml@refs/heads/main"
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  depends_on                = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub"
  # repository_id is added for the binding below; the other three are kept as they were (unused, harmless), so the
  # mapping only grows. Both the mapping and the condition update the provider in place.
  attribute_mapping = {
    "google.subject"          = "assertion.sub"
    "attribute.repository"    = "assertion.repository"
    "attribute.repository_id" = "assertion.repository_id"
    "attribute.ref"           = "assertion.ref"
    "attribute.workflow_ref"  = "assertion.workflow_ref"
  }
  attribute_condition = join(" && ", [
    "assertion.repository_id == '${var.github_repository_id}'",
    "assertion.repository_owner_id == '${var.github_repository_owner_id}'",
    "assertion.repository == '${var.github_repo}'",
    "assertion.runner_environment == 'github-hosted'",
    "assertion.ref == 'refs/heads/main'",
    "(assertion.event_name == 'workflow_run' || assertion.event_name == 'workflow_dispatch')",
    "assertion.workflow_ref == '${local.deploy_workflow}'",
    "assertion.job_workflow_ref == '${local.deploy_workflow}'",
  ])
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "deploy" {
  account_id   = "${var.service}-deploy"
  display_name = "registry.coffee deploys (GitHub Actions)"
}

# By the repository's immutable id, not its name. Changing the member replaces this grant: the new one is made after
# the provider maps repository_id (depends_on) and before the old one is removed (create_before_destroy), so the
# IAM policy always holds one of them. Token exchanges see the new mapping and grant only once IAM has propagated
# them, which can take minutes: a deploy right after the apply can get a 403, and a re-run succeeds.
# create_before_destroy also applies to what this grant depends on (the provider, the pool, the deploy account) if one
# of them is ever replaced. All three have fixed ids, so such a replace would fail with a conflict rather than half
# apply; the guard refuses those replaces anyway.
resource "google_service_account_iam_member" "deploy_from_github" {
  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository_id/${var.github_repository_id}"
  depends_on         = [google_iam_workload_identity_pool_provider.github]
  lifecycle {
    create_before_destroy = true
  }
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
