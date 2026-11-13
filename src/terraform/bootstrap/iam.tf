# What each account may do, read from what src/terraform/ manages:
#   APIs (google_project_service); the deploy pool and provider; four service accounts and IAM on two of them; the
#   image repository and its IAM; seven empty secrets and their IAM; the Cloud Run service, its IAM and its domain
#   mapping; the ingestion network (network, two subnets, router, two NATs, two static addresses); the staging bucket
#   and its IAM; the Cloud Run jobs and their IAM; Cloud Scheduler jobs; a notification channel, alert policies and an
#   uptime check; the billing budget; `data.google_project`.
# Specific roles throughout: no owner, editor, projectIamAdmin, workloadIdentityPoolAdmin, roleAdmin or
# serviceAccountKeyAdmin. A missing permission fails closed, with a 403 that names it.
#
# What neither account can do: write a workload identity pool or provider, a custom role, the project's IAM policy,
# IAM on any service account (so who may act as the deploy, plan or apply account never changes through the
# pipeline: the guard refuses those changes too), these two buckets' IAM or the billing account's; delete a service
# account.
#
# What the apply account CAN do once an apply is approved, because the main root manages it: set IAM on the secrets
# (so grant itself or anyone a secret's value), the image repository, the Cloud Run service and jobs, and the staging
# bucket (so reach raw ingestion data); and run any image as the runtime, ingest or scheduler account, which hold
# secret access and the staging bucket. The approval, the guard and the checks in terraform-apply.yml are the gate
# on all of that, not these roles. The plan account can read every resource's settings and IAM, and the state (the
# billing account id, the owner's email, the egress IPs), from any same-repository pull request.

locals {
  plan_member  = "serviceAccount:${google_service_account.tf_plan.email}"
  apply_member = "serviceAccount:${google_service_account.tf_apply.email}"

  # The main root's runtime accounts (secrets.tf, ingestion.tf), by the names it gives them.
  managed_account = {
    for k, id in {
      runtime          = "${var.service}-run"
      ingest           = "${var.service}-ingest"
      ingest_scheduler = "${var.service}-ingest-sched"
    } : k => "projects/${var.project_id}/serviceAccounts/${id}@${var.project_id}.iam.gserviceaccount.com"
  }

  # The main root's state objects only: not this root's state (registry-coffee-bootstrap/), which the trailing slash
  # excludes. Listing is checked on the bucket itself, so the bucket is matched as well (object names only).
  state_condition = "resource.type == 'storage.googleapis.com/Bucket' || resource.name.startsWith('projects/_/buckets/${var.state_bucket}/objects/${var.state_prefix}/')"

  image_repository = "projects/${var.project_id}/locations/${var.region}/repositories/${var.service}"
  staging_bucket   = "${var.project_id}-ingest-staging"
}

# --- Custom roles: where a predefined role would give more than the root needs ---------------------------------------

# Read the staging bucket's settings (lifecycle, access mode). Not its objects: those are raw ingestion data.
resource "google_project_iam_custom_role" "tf_bucket_reader" {
  role_id     = "tfPlanBucketReader"
  title       = "Terraform plan: read a bucket's settings"
  permissions = ["storage.buckets.get"]
}

# Change the staging bucket's settings and IAM (lifecycle rule, the ingest account's objectUser), never its objects.
resource "google_project_iam_custom_role" "tf_bucket_admin" {
  role_id     = "tfApplyBucketAdmin"
  title       = "Terraform apply: a bucket's settings and IAM"
  permissions = ["storage.buckets.get", "storage.buckets.getIamPolicy", "storage.buckets.setIamPolicy", "storage.buckets.update"]
}

# Create and update the main root's service accounts. Not delete (the guard refuses deletes; that is a change by
# hand), not setIamPolicy (project-wide it would reach these two accounts' own bindings), not keys.
resource "google_project_iam_custom_role" "tf_service_accounts" {
  role_id     = "tfApplyServiceAccounts"
  title       = "Terraform apply: create and update service accounts"
  permissions = ["iam.serviceAccounts.create", "iam.serviceAccounts.get", "iam.serviceAccounts.list", "iam.serviceAccounts.update"]
}

# The image repository's settings and IAM (cleanup policies, the deploy account's writer role), on that repository
# only: not roles/artifactregistry.admin, which can also delete images, the pinned one included.
resource "google_project_iam_custom_role" "tf_repository" {
  role_id = "tfApplyRepository"
  title   = "Terraform apply: a repository's settings and IAM, no images"
  permissions = [
    "artifactregistry.repositories.get",
    "artifactregistry.repositories.getIamPolicy",
    "artifactregistry.repositories.setIamPolicy",
    "artifactregistry.repositories.update",
  ]
}

# Read the saved plan it was approved for (by name and generation), and delete it after the apply. Not list or write.
resource "google_project_iam_custom_role" "tf_plan_object" {
  role_id     = "tfApplyPlanObject"
  title       = "Terraform apply: read and delete a saved plan"
  permissions = ["storage.objects.delete", "storage.objects.get"]
}

# Create the empty secrets and set who may read them. Not secretmanager.versions.access (roles/secretmanager.admin
# has it): the account never reads a value, and Terraform never holds one.
resource "google_project_iam_custom_role" "tf_secrets" {
  role_id = "tfApplySecrets"
  title   = "Terraform apply: secrets' metadata and IAM, no values"
  permissions = [
    "secretmanager.secrets.create",
    "secretmanager.secrets.get",
    "secretmanager.secrets.getIamPolicy",
    "secretmanager.secrets.list",
    "secretmanager.secrets.setIamPolicy",
    "secretmanager.secrets.update",
  ]
}

# --- Plan: read every resource the main root manages ----------------------------------------------------------------

resource "google_project_iam_member" "tf_plan" {
  for_each = toset([
    "roles/browser",                           # resourcemanager.projects.get: data.google_project, the provider
    "roles/serviceusage.serviceUsageConsumer", # read enabled APIs; use the project as quota project (user_project_override)
    "roles/iam.workloadIdentityPoolViewer",    # the deploy pool and provider
    "roles/iam.serviceAccountViewer",          # the four service accounts
    "roles/iam.securityReviewer",              # read (never set) IAM policies: accounts, secrets, repository, service, jobs, bucket
    "roles/run.viewer",                        # the service, its domain mapping, the jobs
    "roles/secretmanager.viewer",              # secrets' metadata; never a value (no versions.access)
    "roles/compute.networkViewer",             # network, subnets, router, NATs, static addresses
    "roles/cloudscheduler.viewer",             # the weekly schedule
    "roles/monitoring.viewer",                 # notification channel, alert policies, uptime check
  ])
  project = var.project_id
  role    = each.value
  member  = local.plan_member
}

resource "google_artifact_registry_repository_iam_member" "tf_plan" {
  project    = var.project_id
  location   = var.region
  repository = local.image_repository
  role       = "roles/artifactregistry.reader" # the repository's settings (cleanup policies)
  member     = local.plan_member
}

resource "google_storage_bucket_iam_member" "tf_plan_staging" {
  bucket = local.staging_bucket
  role   = google_project_iam_custom_role.tf_bucket_reader.id
  member = local.plan_member
}

# The monthly budget, read through the project: Google's budget docs give this project-scoped pair for a budget
# limited to one project, without any billing-account role (billing.viewer would show every project's costs). If the
# Budget API refuses it, the first plan fails closed with a 403, and billing.viewer on the account is the fallback.
resource "google_project_iam_custom_role" "tf_budget_reader" {
  role_id     = "tfPlanBudgetReader"
  title       = "Terraform plan: this project's budgets"
  permissions = ["billing.resourcebudgets.read", "resourcemanager.projects.get"]
}

resource "google_project_iam_member" "tf_plan_budget" {
  project = var.project_id
  role    = google_project_iam_custom_role.tf_budget_reader.id
  member  = local.plan_member
}

resource "google_storage_bucket_iam_member" "tf_plan_state" {
  bucket = var.state_bucket
  role   = "roles/storage.objectViewer" # read the main root's state; plans run with -lock=false
  member = local.plan_member
  condition {
    title      = "main-root-state-only"
    expression = local.state_condition
  }
}

resource "google_storage_bucket_iam_member" "tf_plan_plans" {
  bucket = google_storage_bucket.tf_plans.name
  role   = "roles/storage.objectCreator" # write a saved plan once; cannot read, list or overwrite one
  member = local.plan_member
}

# --- Apply: change every resource the main root manages --------------------------------------------------------------

resource "google_project_iam_member" "tf_apply" {
  for_each = toset([
    "roles/browser",                              # resourcemanager.projects.get
    "roles/serviceusage.serviceUsageAdmin",       # enable the APIs in google_project_service (never disabled: disable_on_destroy = false)
    "roles/iam.workloadIdentityPoolViewer",       # read only: the deploy pool is changed by hand
    "roles/iam.securityReviewer",                 # read IAM policies, for the fresh plan the apply job compares (drift)
    "roles/run.admin",                            # the service, its IAM and domain mapping; the jobs and their IAM
    "roles/compute.networkAdmin",                 # network, subnets, router, NATs, static addresses (no instances, no firewall)
    "roles/cloudscheduler.admin",                 # the weekly schedule
    "roles/monitoring.alertPolicyEditor",         # alert policies
    "roles/monitoring.notificationChannelEditor", # the owner's email channel
    "roles/monitoring.uptimeCheckConfigEditor",   # the /healthz uptime check
    google_project_iam_custom_role.tf_service_accounts.id,
    google_project_iam_custom_role.tf_secrets.id,
  ])
  project = var.project_id
  role    = each.value
  member  = local.apply_member
}

resource "google_artifact_registry_repository_iam_member" "tf_apply" {
  project    = var.project_id
  location   = var.region
  repository = local.image_repository
  role       = google_project_iam_custom_role.tf_repository.id
  member     = local.apply_member
}

resource "google_storage_bucket_iam_member" "tf_apply_staging" {
  bucket = local.staging_bucket
  role   = google_project_iam_custom_role.tf_bucket_admin.id
  member = local.apply_member
}

# actAs: the service runs as the runtime account, the jobs as the ingest account, the schedule calls them as the
# scheduler account. No other account, and never IAM on any account: deploy.tf's two account bindings (who may act as
# the deploy account, and the deploy account acting as the runtime one) stay changes by hand, and the guard refuses them.
resource "google_service_account_iam_member" "tf_apply_acts_as" {
  for_each           = toset(["runtime", "ingest", "ingest_scheduler"])
  service_account_id = local.managed_account[each.value]
  role               = "roles/iam.serviceAccountUser"
  member             = local.apply_member
}

resource "google_billing_account_iam_member" "tf_apply" {
  billing_account_id = var.billing_account
  role               = "roles/billing.costsManager" # the monthly budget; no billing IAM, no payments
  member             = local.apply_member
}

resource "google_storage_bucket_iam_member" "tf_apply_state" {
  bucket = var.state_bucket
  role   = "roles/storage.objectAdmin" # read and write the main root's state and its lock
  member = local.apply_member
  condition {
    title      = "main-root-state-only"
    expression = local.state_condition
  }
}

resource "google_storage_bucket_iam_member" "tf_apply_plans" {
  bucket = google_storage_bucket.tf_plans.name
  role   = google_project_iam_custom_role.tf_plan_object.id
  member = local.apply_member
}
