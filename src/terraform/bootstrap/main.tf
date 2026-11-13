# Two accounts, each behind its own workload identity pool. Not the main root's `github` pool (deploy.tf): every
# identity in that pool may impersonate the deploy account, so a provider added there would hand it to PR tokens.
#
# The conditions read GitHub's OIDC token claims. workflow_ref is "<owner>/<repo>/<path>@<ref>" of the top-level
# workflow; a pull request's is ".../terraform-plan.yml@refs/pull/<n>/merge". `environment` is in the token only for a
# job that passed that environment's protection rules, so the apply condition ties GCP access to the Approve.
# Fork pull requests get no OIDC token from GitHub at all; pull_request_target is excluded by the event_name pins.

locals {
  repo_claims = join(" && ", [
    "assertion.repository_id == '${var.github_repository_id}'",
    "assertion.repository_owner_id == '${var.github_repository_owner_id}'",
    "assertion.repository == '${var.github_repo}'",
    "assertion.runner_environment == 'github-hosted'",
  ])
  workflows      = "${var.github_repo}/.github/workflows"
  on_main        = "assertion.ref == 'refs/heads/main' && (assertion.event_name == 'push' || assertion.event_name == 'workflow_dispatch')"
  apply_workflow = "${local.workflows}/terraform-apply.yml@refs/heads/main"

  # job_workflow_ref as well as workflow_ref: a job of main's file that called a reusable workflow (at any ref) would
  # otherwise get the token for code that isn't that file.
  apply_file = "assertion.workflow_ref == '${local.apply_workflow}' && assertion.job_workflow_ref == '${local.apply_workflow}'"

  # The plan account's runs, exactly these two: a pull request's terraform-plan.yml, and the plan job of
  # terraform-apply.yml on main (a push, or a run by hand).
  plan_runs = [
    "(assertion.event_name == 'pull_request' && assertion.ref.startsWith('refs/pull/') && assertion.workflow_ref.startsWith('${local.workflows}/terraform-plan.yml@refs/pull/') && assertion.job_workflow_ref == assertion.workflow_ref)",
    "(${local.on_main} && ${local.apply_file})",
  ]
}

# --- Plan: read only. This repository's pull requests and the apply workflow on main; never a fork or other file --

resource "google_iam_workload_identity_pool" "tf_plan" {
  workload_identity_pool_id = "github-tf-plan"
  display_name              = "GitHub Terraform plan"
  description               = "Read-only Terraform plans from this repository's PRs and main. Managed in src/terraform/bootstrap."
}

resource "google_iam_workload_identity_pool_provider" "tf_plan" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.tf_plan.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub"
  attribute_mapping = {
    "google.subject"          = "assertion.sub"
    "attribute.repository_id" = "assertion.repository_id"
  }
  attribute_condition = join(" && ", [local.repo_claims, "(${join(" || ", local.plan_runs)})"])
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "tf_plan" {
  account_id   = "${var.service}-tf-plan"
  display_name = "Terraform plan (GitHub Actions, read only)"
}

resource "google_service_account_iam_member" "tf_plan_from_github" {
  service_account_id = google_service_account.tf_plan.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.tf_plan.name}/attribute.repository_id/${var.github_repository_id}"
}

# --- Apply: write. Only terraform-apply.yml on main, only in a job running in the terraform-apply environment --------

resource "google_iam_workload_identity_pool" "tf_apply" {
  workload_identity_pool_id = "github-tf-apply"
  display_name              = "GitHub Terraform apply"
  description               = "Terraform apply from main after the environment's Approve. Managed in src/terraform/bootstrap."
}

resource "google_iam_workload_identity_pool_provider" "tf_apply" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.tf_apply.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub"
  attribute_mapping = {
    "google.subject"          = "assertion.sub"
    "attribute.repository_id" = "assertion.repository_id"
    "attribute.environment"   = "assertion.environment"
  }
  attribute_condition = join(" && ", [
    local.repo_claims,
    local.on_main,
    local.apply_file,
    "assertion.environment == '${var.apply_environment}'",
  ])
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "tf_apply" {
  account_id   = "${var.service}-tf-apply"
  display_name = "Terraform apply (GitHub Actions, after Approve)"
}

resource "google_service_account_iam_member" "tf_apply_from_github" {
  service_account_id = google_service_account.tf_apply.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.tf_apply.name}/attribute.environment/${var.apply_environment}"
}

# --- Where the apply workflow's saved plans wait for the Approve ---------------------------------------------------
# A saved plan holds every variable in clear, so it never goes to a GitHub artifact (public on a public repository).
# The plan account may only create objects here (no read, no overwrite); the apply account may only read them.

resource "google_storage_bucket" "tf_plans" {
  name                        = "${var.project_id}-tf-plans"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  # The apply job deletes its plan once applied; anything left goes after a day (a plan older than that is refused
  # anyway), with no soft-delete copy kept: a plan holds every variable in clear.
  soft_delete_policy {
    retention_duration_seconds = 0
  }
  lifecycle_rule {
    condition {
      age = 1
    }
    action {
      type = "Delete"
    }
  }
}
