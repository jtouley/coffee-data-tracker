# The admin's actions (docs/admin/README.md, docs/design/admin-registry.md §2-3, §5.3): its control-plane database,
# the snapshot bucket drafts are built from, and the right to start the named pipeline jobs. The admin's service
# account (admin.tf) gets, beyond what admin.tf gives it:
#   - Cloud SQL: connect to this one instance as an IAM database user (no password anywhere);
#   - the snapshot bucket: create and read objects, never overwrite or delete them;
#   - on each job in local.admin_jobs only: run it with argument overrides, cancel, and read its executions.
# Nothing here grants it the lake (a Hugging Face dataset, written only by the jobs), the staging bucket, any other
# job, job updates, IAM or Terraform: infra stays on the CLI.

locals {
  # The jobs the admin may start (admin/jobs.py JOB_NAMES; tests/test_admin_terraform.py checks the two agree).
  admin_jobs = toset([
    "crawl-shopify", "crawl-squarespace", "crawl-woocommerce", "crawl-square-online", "crawl-generic",
    "crawl-retry", "fetch-health", "merge", "enrich-week", "build", "publish", "pull",
  ])
  # The Cloud SQL user of a service account is its email without ".gserviceaccount.com".
  admin_db_user = trimsuffix(google_service_account.admin.email, ".gserviceaccount.com")
}

resource "google_project_service" "admin_sqladmin" {
  service            = "sqladmin.googleapis.com"
  disable_on_destroy = false
}

# The control plane: the smallest Cloud SQL for PostgreSQL (shared core, zonal, 10 GB SSD), with IAM database
# authentication and a public IP that no network is authorized on, so the only way in is the Cloud SQL connector
# with an IAM identity and a client certificate. Daily backups, kept 7; point-in-time recovery off (the audit trail
# is append-only and small; turn it on if a day's loss matters).
resource "google_sql_database_instance" "admin" {
  name                = "${var.service}-admin"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = true

  settings {
    edition                     = "ENTERPRISE" # shared-core tiers exist only in Enterprise; PostgreSQL 16 defaults to Plus
    deletion_protection_enabled = true         # also refuses a delete from the console or gcloud, not only Terraform
    tier                        = "db-f1-micro"
    availability_type           = "ZONAL"
    disk_type                   = "PD_SSD"
    disk_size                   = 10
    disk_autoresize             = false

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }

    ip_configuration {
      ipv4_enabled = true
      ssl_mode     = "TRUSTED_CLIENT_CERTIFICATE_REQUIRED" # the connector's short-lived certificates only
    }

    backup_configuration {
      enabled                        = true
      start_time                     = "02:00" # UTC
      point_in_time_recovery_enabled = false
      backup_retention_settings {
        retained_backups = 7
      }
    }

    maintenance_window {
      day  = 4 # Thursday, away from Monday's crawls and Tuesday's release
      hour = 8
    }
  }

  depends_on = [google_project_service.admin_sqladmin]
}

resource "google_sql_database" "admin" {
  name     = "admin"
  instance = google_sql_database_instance.admin.name
}

resource "google_sql_user" "admin" {
  name     = local.admin_db_user
  instance = google_sql_database_instance.admin.name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}

# Connect (cloudsql.client) and log in as its IAM user (cloudsql.instanceUser), on this instance only.
resource "google_project_iam_member" "admin_sql" {
  for_each = toset(["roles/cloudsql.client", "roles/cloudsql.instanceUser"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.admin.email}"
  condition {
    title       = "only the admin's instance"
    description = "The admin's control-plane database, nothing else."
    expression  = "resource.name == \"projects/${var.project_id}/instances/${google_sql_database_instance.admin.name}\""
  }
}

# Drafts' decisions snapshots: the admin creates them, the build job reads them (mounted read-only), nobody rewrites
# them. Creating an object that exists needs delete permission, which no one here has.
resource "google_storage_bucket" "admin_snapshots" {
  name                        = "${var.project_id}-admin-snapshots"
  location                    = upper(var.region)
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning {
    enabled = true
  }
  depends_on = [google_project_service.admin_storage]
}

resource "google_storage_bucket_iam_member" "admin_snapshots" {
  for_each = toset(["roles/storage.objectCreator", "roles/storage.objectViewer"])
  bucket   = google_storage_bucket.admin_snapshots.name
  role     = each.value
  member   = "serviceAccount:${google_service_account.admin.email}"
}

resource "google_storage_bucket_iam_member" "ingest_snapshots" {
  bucket = google_storage_bucket.admin_snapshots.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.ingest.email}"
}

# Start the named jobs with argument overrides and cancel their executions (jobsExecutorWithOverrides: run.jobs.run,
# run.jobs.runWithOverrides, run.executions.cancel), and read them and their executions for the Jobs page's status
# (run.viewer: it has no run, update or IAM permission). Both on each job, never on the project.
resource "google_cloud_run_v2_job_iam_member" "admin" {
  for_each = {
    for pair in setproduct(sort(local.admin_jobs), ["roles/run.jobsExecutorWithOverrides", "roles/run.viewer"]) :
    "${pair[0]} ${pair[1]}" => { job = pair[0], role = pair[1] }
  }
  name     = google_cloud_run_v2_job.ingest[each.value.job].name
  location = var.region
  role     = each.value.role
  member   = "serviceAccount:${google_service_account.admin.email}"
}

# An email on every admin write: a job started or cancelled, a decision or undo, a draft, Publish, Roll back,
# Discard (admin/writes.py logs event admin_write for each). None should surprise.
resource "google_monitoring_alert_policy" "admin_writes" {
  display_name = "admin.registry.coffee: a write"
  combiner     = "OR"
  conditions {
    display_name = "admin_write logged"
    condition_matched_log {
      filter = join(" AND ", [
        "resource.type=\"cloud_run_revision\"",
        "resource.labels.service_name=\"${google_cloud_run_v2_service.admin.name}\"",
        "jsonPayload.event=\"admin_write\"",
      ])
    }
  }
  alert_strategy {
    notification_rate_limit {
      period = "300s"
    }
    auto_close = "1800s"
  }
  notification_channels = [google_monitoring_notification_channel.owner.id]
}

output "admin_db_instance" {
  description = "The control plane's Cloud SQL instance connection name (ADMIN_DB_INSTANCE)."
  value       = google_sql_database_instance.admin.connection_name
}

output "admin_db_user" {
  description = "The admin's IAM database user: grant it CREATE on the admin database once (docs/admin/README.md)."
  value       = local.admin_db_user
}

output "admin_snapshots_bucket" {
  description = "Where the admin writes drafts' decisions snapshots; the build job mounts it read-only."
  value       = google_storage_bucket.admin_snapshots.name
}
