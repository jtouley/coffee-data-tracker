# What each account may do, read from what src/terraform/ manages:
#   APIs (google_project_service); the deploy pool and provider; six service accounts and IAM on two of them; the
#   image repository and its IAM; eight empty secrets and their IAM; the Cloud Run service, its IAM and its domain
#   mapping; the usage collector's service and its IAM, and one log exclusion (usage.tf); the ingestion network
#   (network, two subnets, router, two NATs, two static addresses); the staging bucket and its IAM; the Cloud Run jobs
#   and their IAM; Cloud Scheduler jobs; a notification channel, alert policies (the log-based ones with a Logging
#   notification rule underneath) and an uptime check; the billing budget; `data.google_project`. With the admin
#   (admin.tf, admin_control.tf): its service account, three secrets, two buckets (`-admin-state`, `-admin-snapshots`)
#   and their IAM, a Cloud SQL instance, database and IAM user, IAM on the jobs it may start, and its service, domain
#   mapping and alerts. The admin account's two Cloud SQL project grants are this root's (admin_sql, below): the
#   pipeline never writes the project's IAM policy.
# Specific roles throughout: no owner, editor, projectIamAdmin, workloadIdentityPoolAdmin, roleAdmin or
# serviceAccountKeyAdmin. A missing permission fails closed, with a 403 that names it.
#
# What neither account can do: write a workload identity pool or provider, a custom role, the project's IAM policy,
# IAM on any service account (so who may act as the deploy, plan or apply account never changes through the
# pipeline: the guard refuses those changes too), the state and plans buckets' IAM or the billing account's; delete a
# service account.
#
# What the apply account CAN do once an apply is approved. It holds create, read, list and update (no delete beyond
# the saved plan's) for the ordinary types of every service the main root runs, so the next resource of a kind it
# already runs needs no change here: Cloud Run services, jobs and domain mappings (run.admin), Cloud Scheduler,
# Compute networking (networks, subnets, routers, NATs, addresses), Secret Manager secrets, Cloud Storage buckets,
# Artifact Registry repositories, Monitoring (alert policies, channels, uptime checks, dashboards, groups, services,
# SLOs, snoozes), Logging (exclusions, log-based metrics, notification rules) and Cloud SQL (instances, databases,
# users). So it can: set IAM on the secrets (so grant itself or anyone a secret's value), the image repository, the
# Cloud Run services and jobs, and every "<project>-" bucket but the state and plans buckets (the staging bucket's raw
# ingestion data, the admin's sign-in state and snapshots); create buckets and repositories; deploy from any
# repository's images (the guard pins the images it may run); change any Cloud SQL instance and create or change its
# databases and users (the guard refuses an authorized network, an ssl_mode weaker than ENCRYPTED_ONLY, IAM
# authentication off, a root password, a replica, clone or restore, and a database user that isn't an IAM one or
# carries a password: cloud-sql-access); run any image as the runtime, ingest, scheduler, usage or admin account,
# which hold secret access, the staging bucket and (the admin's) its database, buckets and the named jobs; and create
# log-based metrics, alert policies and notification channels, so a policy whose label extractors or documentation
# copy log fields could send them to a channel: a narrow, reviewable path for log content, not a sink. The guard keeps
# channels to email (notification-channels), nothing public but a Cloud Run invoker (public-access), every IAM member
# known at plan (iam-members-known), and Cloud SQL's database flags to an allowlist (cloud-sql-access).
#
# The hard line (tests/test_bootstrap_roles.py, test_the_hard_line_holds): never the project's IAM
# (project-iam-policy refuses it too), IAM on a service account, a custom role, a key, a workload identity pool or
# provider, billing beyond the budget, or the state and plans buckets' IAM; no basic role, no predefined admin role
# beyond today's; setIamPolicy only in the three roles that held it before (secrets, the one repository, buckets).
# The approval, the guard and the checks in terraform-apply.yml are the gate on all of that, not these roles. The plan
# account can read every resource's settings and IAM, and the state (the billing account id, the owner's email, the
# egress IPs), from any same-repository pull request.

locals {
  plan_member  = "serviceAccount:${google_service_account.tf_plan.email}"
  apply_member = "serviceAccount:${google_service_account.tf_apply.email}"

  # The main root's runtime accounts (secrets.tf, ingestion.tf, usage.tf, admin.tf), by the names it gives them. Each
  # is made by the main root's apply before this root grants on it: the usage collector's by the first apply after
  # usage.tf merged, the admin's by #199's (RUNNING.md, "Usage" and "The apply account's grants for the admin").
  # tests/test_terraform_accounts.py checks that the main root creates every account named here.
  managed_account = {
    for k, id in {
      runtime          = "${var.service}-run"
      ingest           = "${var.service}-ingest"
      ingest_scheduler = "${var.service}-ingest-sched"
      usage            = "${var.service}-usage"
      admin            = "${var.service}-admin-run"
    } : k => "projects/${var.project_id}/serviceAccounts/${id}@${var.project_id}.iam.gserviceaccount.com"
  }

  # The main root's state objects only: not this root's state (registry-coffee-bootstrap/), which the trailing slash
  # excludes. Listing is checked on the bucket itself, so the bucket is matched as well (object names only).
  state_condition = "resource.type == 'storage.googleapis.com/Bucket' || resource.name.startsWith('projects/_/buckets/${var.state_bucket}/objects/${var.state_prefix}/')"

  image_repository = "projects/${var.project_id}/locations/${var.region}/repositories/${var.service}"
  staging_bucket   = "${var.project_id}-ingest-staging"

  # The project's buckets the apply account may manage (settings and IAM): every bucket whose name starts with
  # "<project>-", by the main root's naming (the staging bucket, the admin's two, the next one), except the state and
  # plans buckets, whose IAM is the owner's only. Cloud Build's "<project>_cloudbuild" doesn't match (an underscore).
  # Project grants with this condition, since a new bucket doesn't exist when this root applies. IAM conditions on
  # Cloud Storage need uniform bucket-level access, which every bucket the main root makes has.
  managed_buckets_condition = join(" && ", [
    "resource.type == 'storage.googleapis.com/Bucket'",
    "resource.name.startsWith('projects/_/buckets/${var.project_id}-')",
    "resource.name != 'projects/_/buckets/${var.state_bucket}'",
    "resource.name != 'projects/_/buckets/${google_storage_bucket.tf_plans.name}'",
  ])
}

# --- Custom roles: where a predefined role would give more than the root needs ---------------------------------------

# Read the staging bucket's settings (lifecycle, access mode). Not its objects: those are raw ingestion data.
resource "google_project_iam_custom_role" "tf_bucket_reader" {
  role_id     = "tfPlanBucketReader"
  title       = "Terraform plan: read a bucket's settings"
  permissions = ["storage.buckets.get"]
}

# Change a bucket's settings and IAM (lifecycle rule, versioning, who reads or writes its objects), never its objects:
# on the staging bucket (tf_apply_staging) and, through tf_apply_buckets, on every "<project>-" bucket but the state
# and plans buckets. This role held setIamPolicy before; what is new is its reach: the next bucket of the main root's
# needs no bootstrap change, and an approved apply can set IAM on any such bucket, the admin's sign-in state included.
resource "google_project_iam_custom_role" "tf_bucket_admin" {
  role_id     = "tfApplyBucketAdmin"
  title       = "Terraform apply: a bucket's settings and IAM"
  permissions = ["storage.buckets.get", "storage.buckets.getIamPolicy", "storage.buckets.setIamPolicy", "storage.buckets.update"]
}

# Create a bucket (new: the admin's `-admin-state` and `-admin-snapshots`, and any later one). Checked on the project,
# before the bucket exists, so not limited by name; it reaches no existing bucket (a name in use fails). New: an
# approved apply can create any number of buckets (a cost). A bucket named outside "<project>-" can't be managed
# afterwards (tf_apply_buckets), so it fails closed at its first settings read.
resource "google_project_iam_custom_role" "tf_bucket_create" {
  role_id     = "tfApplyBucketCreate"
  title       = "Terraform apply: create a bucket"
  permissions = ["storage.buckets.create"]
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
# And read its images: Cloud Run resolves a job's or the usage service's image as the account that deploys it, so
# without downloadArtifacts every google_cloud_run_v2_job update failed with Docker-GetManifest permission_denied.
# Cloud Run's "Create jobs" page (Required roles) asks the deployer for Artifact Registry Reader on the image's
# repository, so this holds the read permissions roles/artifactregistry.reader has at repository scope (IAM roles
# reference, Artifact Registry). Left out: its project-level ones (locations, projectconfigs, projectsettings,
# resourcemanager.projects), repositories.list (checked on the location, the repositories.list method's reference
# says, so no effect granted here), exportArtifacts (copies images out), and the Maven, npm and Python reads (a Docker
# repository). No upload, tag, delete or other write permission; update and setIamPolicy are its settings and IAM.
resource "google_project_iam_custom_role" "tf_repository" {
  role_id = "tfApplyRepository"
  title   = "Terraform apply: a repository's settings and IAM, read images"
  permissions = [
    "artifactregistry.attachments.get",
    "artifactregistry.attachments.list",
    "artifactregistry.dockerimages.get",
    "artifactregistry.dockerimages.list",
    "artifactregistry.files.download",
    "artifactregistry.files.get",
    "artifactregistry.files.list",
    "artifactregistry.packages.get",
    "artifactregistry.packages.list",
    "artifactregistry.repositories.downloadArtifacts",
    "artifactregistry.repositories.get",
    "artifactregistry.repositories.getIamPolicy",
    "artifactregistry.repositories.listEffectiveTags",
    "artifactregistry.repositories.listTagBindings",
    "artifactregistry.repositories.readViaVirtualRepository",
    "artifactregistry.repositories.setIamPolicy",
    "artifactregistry.repositories.update",
    "artifactregistry.rules.get",
    "artifactregistry.rules.list",
    "artifactregistry.tags.get",
    "artifactregistry.tags.list",
    "artifactregistry.versions.get",
    "artifactregistry.versions.list",
  ]
}

# Any Artifact Registry repository in the project: create one, read and change its settings (cleanup policies, the
# format's settings), and read its images, which Cloud Run checks as whoever deploys (the same read set as
# tfApplyRepository). New: an approved apply can create a repository and deploy from any repository's images; the
# guard's image pins (ingest-image-pinned, app-image-pinned) still allow only this project's `<service>/ingest` and
# `<service>/app` images by digest. Not setIamPolicy (that stays on the one repository, tfApplyRepository), and no
# upload, tag write or delete.
resource "google_project_iam_custom_role" "tf_registry" {
  role_id = "tfApplyRegistry"
  title   = "Terraform apply: create repositories, their settings, read images"
  permissions = [
    "artifactregistry.attachments.get",
    "artifactregistry.attachments.list",
    "artifactregistry.dockerimages.get",
    "artifactregistry.dockerimages.list",
    "artifactregistry.files.download",
    "artifactregistry.files.get",
    "artifactregistry.files.list",
    "artifactregistry.packages.get",
    "artifactregistry.packages.list",
    "artifactregistry.repositories.create",
    "artifactregistry.repositories.downloadArtifacts",
    "artifactregistry.repositories.get",
    "artifactregistry.repositories.list",
    "artifactregistry.repositories.listEffectiveTags",
    "artifactregistry.repositories.listTagBindings",
    "artifactregistry.repositories.readViaVirtualRepository",
    "artifactregistry.repositories.update",
    "artifactregistry.rules.get",
    "artifactregistry.rules.list",
    "artifactregistry.tags.get",
    "artifactregistry.tags.list",
    "artifactregistry.versions.get",
    "artifactregistry.versions.list",
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

# The usage collector's request-log exclusion (usage.tf), and the Logging notification rule under each log-based
# alert policy (condition_matched_log: alerts.tf, usage.tf). Creating one calls Monitoring's CreateAlertPolicy, which
# needs logging.notificationRules.create on the project as well ("Configure log-based alerting policies"; Logging's
# access-control page, "Permissions for log-based alerting policies"); without it usage_dropped failed with
# "Permission 'logging.notificationRules.create' denied". update for a changed filter, get and list to read the rule
# back. Not roles/logging.configWriter, which can also write sinks (export every log anywhere) and buckets: exclusions
# and notification rules only, and no delete of either (the guard refuses deletes; that is by hand). Residual risk,
# which the approval covers: with alertPolicyEditor and notificationChannelEditor this does let an approved apply
# create a log-based alert whose label extractors or documentation copy fields of matching log entries into a
# notification, on a channel it creates. The guard allows email channels only (notification-channels) and doesn't
# check extractors; RUNNING.md, "What an approved apply can do", says what to read before approving. The role id stays (changing it
# would replace the role and its grant); the title says what it covers now.
# And log-based metrics (logging.logMetrics create, get, list, update; new): an approved apply can count matching log
# entries into a metric, which the alert policies it can already write can then use. A metric holds counts and
# labels, not entries; its label extractors can copy a field's values into labels, readable by anyone with
# monitoring.viewer in the project (no one outside it). Still no sinks (they export entries anywhere), log buckets
# or views (retention, who reads logs), and no delete.
resource "google_project_iam_custom_role" "tf_log_exclusions" {
  role_id = "tfApplyLogExclusions"
  title   = "Terraform apply: log exclusions, log-based metrics, log-based alerts' rules"
  permissions = [
    "logging.exclusions.create",
    "logging.exclusions.get",
    "logging.exclusions.list",
    "logging.exclusions.update",
    "logging.logMetrics.create",
    "logging.logMetrics.get",
    "logging.logMetrics.list",
    "logging.logMetrics.update",
    "logging.notificationRules.create",
    "logging.notificationRules.get",
    "logging.notificationRules.list",
    "logging.notificationRules.update",
  ]
}

# Cloud SQL, project-wide: create, read and change instances, databases and users (the admin's control plane in
# admin_control.tf, and the next). Not roles/cloudsql.admin: no delete (the guard refuses deletes), export, import,
# clone (a separate permission), connect or login. Unconditioned: Google's "IAM Conditions" page for Cloud SQL,
# Limitations, says conditions don't support the users (cloudsql.users.*) or databases (cloudsql.databases.*)
# resources, and it documents no condition for instance creation (it lists "creating instances with specific prefixes
# or suffixes" only as a use case). New, beyond the admin's set: instances.list, instances.update on any instance (not
# only the admin's), and databases.list, databases.update, users.update. What an approved apply can do with it is
# bounded by the guard (cloud-sql-access) on every instance and user in a plan: no authorized network, encrypted
# connections, IAM sign-in on, no root password, no replica, clone or restore, users CLOUD_IAM_* with no password (so
# users.update can't set one through Terraform). Logging in needs cloudsql.instances.login, which no pipeline grant
# gives. Operations are read through instances.get.
resource "google_project_iam_custom_role" "tf_cloud_sql" {
  role_id = "tfApplyCloudSql"
  title   = "Terraform apply: Cloud SQL instances, databases and users"
  permissions = [
    "cloudsql.databases.create",
    "cloudsql.databases.get",
    "cloudsql.databases.list",
    "cloudsql.databases.update",
    "cloudsql.instances.create",
    "cloudsql.instances.get",
    "cloudsql.instances.list",
    "cloudsql.instances.update",
    "cloudsql.users.create",
    "cloudsql.users.get",
    "cloudsql.users.list",
    "cloudsql.users.update",
  ]
}

# Monitoring's other ordinary types (alert policies, channels and uptime checks are the predefined editors' below):
# dashboards, groups, services and their SLOs, snoozes; create, get, list, update, no delete. New: an approved apply
# can add or change dashboards and SLOs, and snooze an alert policy for a while (it can already edit or disable one
# with alertPolicyEditor, so a snooze adds no new way to silence an alert).
resource "google_project_iam_custom_role" "tf_monitoring" {
  role_id = "tfApplyMonitoring"
  title   = "Terraform apply: dashboards, groups, services, SLOs, snoozes"
  permissions = [
    "monitoring.dashboards.create",
    "monitoring.dashboards.get",
    "monitoring.dashboards.list",
    "monitoring.dashboards.update",
    "monitoring.groups.create",
    "monitoring.groups.get",
    "monitoring.groups.list",
    "monitoring.groups.update",
    "monitoring.services.create",
    "monitoring.services.get",
    "monitoring.services.list",
    "monitoring.services.update",
    "monitoring.slos.create",
    "monitoring.slos.get",
    "monitoring.slos.list",
    "monitoring.slos.update",
    "monitoring.snoozes.create",
    "monitoring.snoozes.get",
    "monitoring.snoozes.list",
    "monitoring.snoozes.update",
  ]
}

# The plan account's reads of what the apply account's wider set can create: any repository's settings and log-based
# metrics. Settings only: no image download (it never deploys).
resource "google_project_iam_custom_role" "tf_plan_reader" {
  role_id = "tfPlanReader"
  title   = "Terraform plan: repositories' settings, log-based metrics"
  permissions = [
    "artifactregistry.repositories.get",
    "artifactregistry.repositories.list",
    "logging.logMetrics.get",
    "logging.logMetrics.list",
  ]
}

resource "google_project_iam_custom_role" "tf_log_exclusions_reader" {
  role_id     = "tfPlanLogExclusions"
  title       = "Terraform plan: read log exclusions"
  permissions = ["logging.exclusions.get", "logging.exclusions.list"]
}

# --- Plan: read every resource the main root manages ----------------------------------------------------------------

resource "google_project_iam_member" "tf_plan" {
  for_each = toset([
    "roles/browser",                           # resourcemanager.projects.get: data.google_project, the provider
    "roles/serviceusage.serviceUsageConsumer", # read enabled APIs; use the project as quota project (user_project_override)
    "roles/iam.workloadIdentityPoolViewer",    # the deploy pool and provider
    "roles/iam.serviceAccountViewer",          # the five service accounts
    "roles/iam.securityReviewer",              # read (never set) IAM policies: accounts, secrets, repository, service, jobs, bucket
    "roles/run.viewer",                        # the service, its domain mapping, the jobs
    "roles/secretmanager.viewer",              # secrets' metadata; never a value (no versions.access)
    "roles/compute.networkViewer",             # network, subnets, router, NATs, static addresses
    "roles/cloudscheduler.viewer",             # the weekly schedule
    "roles/monitoring.viewer",                 # notification channel, alert policies, uptime check
    "roles/cloudsql.viewer",                   # the admin's Cloud SQL instance, database and IAM user
  ])
  project = var.project_id
  role    = each.value
  member  = local.plan_member
}

# The custom role, by a static key: its id is unknown until the role exists, and for_each keys must be known at plan.
resource "google_project_iam_member" "tf_plan_custom" {
  for_each = {
    log_exclusions = google_project_iam_custom_role.tf_log_exclusions_reader.id
    reader         = google_project_iam_custom_role.tf_plan_reader.id
  }
  project = var.project_id
  role    = each.value
  member  = local.plan_member
}

resource "google_artifact_registry_repository_iam_member" "tf_plan" {
  project    = var.project_id
  location   = var.region
  repository = local.image_repository
  role       = "roles/artifactregistry.reader" # the repository's settings (cleanup policies); it never deploys a job
  member     = local.plan_member
}

resource "google_storage_bucket_iam_member" "tf_plan_staging" {
  bucket = local.staging_bucket
  role   = google_project_iam_custom_role.tf_bucket_reader.id
  member = local.plan_member
}

# The managed buckets' settings (local.managed_buckets_condition), as for the staging bucket; their IAM through
# securityReviewer.
resource "google_project_iam_member" "tf_plan_buckets" {
  project = var.project_id
  role    = google_project_iam_custom_role.tf_bucket_reader.id
  member  = local.plan_member
  condition {
    title      = "managed-buckets-only"
    expression = local.managed_buckets_condition
  }
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
  ])
  project = var.project_id
  role    = each.value
  member  = local.apply_member
}

# The custom roles, by static keys: their ids are unknown until the roles exist, and for_each keys must be known
# at plan.
resource "google_project_iam_member" "tf_apply_custom" {
  for_each = {
    service_accounts = google_project_iam_custom_role.tf_service_accounts.id
    secrets          = google_project_iam_custom_role.tf_secrets.id
    log_exclusions   = google_project_iam_custom_role.tf_log_exclusions.id
    bucket_create    = google_project_iam_custom_role.tf_bucket_create.id
    cloud_sql        = google_project_iam_custom_role.tf_cloud_sql.id
    monitoring       = google_project_iam_custom_role.tf_monitoring.id
    registry         = google_project_iam_custom_role.tf_registry.id
  }
  project = var.project_id
  role    = each.value
  member  = local.apply_member
}

# The managed buckets' settings and IAM (local.managed_buckets_condition): the admin's two (versioning, the admin's
# creator and viewer, the ingest account's viewer on the snapshots) and the next; never the state or plans bucket.
resource "google_project_iam_member" "tf_apply_buckets" {
  project = var.project_id
  role    = google_project_iam_custom_role.tf_bucket_admin.id
  member  = local.apply_member
  condition {
    title      = "managed-buckets-only"
    expression = local.managed_buckets_condition
  }
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
# scheduler account, the usage collector and the admin service (admin.tf, `-admin-run`) as their own. No other
# account, and never IAM on any account: deploy.tf's two account bindings (who may act as the deploy account, and the
# deploy account acting as the runtime one) stay changes by hand, and the guard refuses them. The deploy account's
# grants for deploying the admin (run.developer on the admin service, serviceAccountUser on `-admin-run` only) are
# hand changes too (RUNNING.md, "The apply account's grants for the admin").
resource "google_service_account_iam_member" "tf_apply_acts_as" {
  for_each           = toset(["runtime", "ingest", "ingest_scheduler", "usage", "admin"])
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

# --- The admin's Cloud SQL grants: the owner's, here, not the pipeline's --------------------------------------------

# Connect (cloudsql.client) and log in as its IAM database user (cloudsql.instanceUser), on the admin's instance only.
# Cloud SQL has no instance-level IAM policy, so these are project grants narrowed by a condition, and project IAM is
# never the pipeline's: they are applied by the owner with this root, after the main root has made the account (#199).
# Same keys, member, roles and condition as the admin stack's google_project_iam_member.admin_sql had in the main root.
resource "google_project_iam_member" "admin_sql" {
  for_each = { client = "roles/cloudsql.client", instance_user = "roles/cloudsql.instanceUser" }
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${var.service}-admin-run@${var.project_id}.iam.gserviceaccount.com"
  condition {
    title       = "only the admin's instance"
    description = "The admin's control-plane database, nothing else."
    expression  = "resource.name == \"projects/${var.project_id}/instances/${var.service}-admin\""
  }
}
