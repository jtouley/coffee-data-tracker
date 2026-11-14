# The admin's grants (docs/design/admin-registry.md §2.4, M4): it may start the named pipeline jobs and nothing
# else, it reaches its own database only, and it can't write the lake or its staging. Against a mock Google provider:
# no credentials, no project, nothing real planned or applied. Run with `terraform test` in src/terraform.

mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      number = "123456789"
    }
  }
  mock_resource "google_service_account" {
    defaults = {
      email = "other-account@coffee-test-1.iam.gserviceaccount.com"
      name  = "projects/coffee-test-1/serviceAccounts/other-account@coffee-test-1.iam.gserviceaccount.com"
    }
  }
  mock_resource "google_cloud_run_v2_job" {
    defaults = {
      name = "a-job"
    }
  }
}

# Each account its own email, so a grant to the wrong one shows.
override_resource {
  target = google_service_account.admin
  values = {
    email = "registry-coffee-admin-run@coffee-test-1.iam.gserviceaccount.com"
    name  = "projects/coffee-test-1/serviceAccounts/registry-coffee-admin-run@coffee-test-1.iam.gserviceaccount.com"
  }
}

override_resource {
  target = google_service_account.ingest
  values = {
    email = "registry-coffee-ingest@coffee-test-1.iam.gserviceaccount.com"
    name  = "projects/coffee-test-1/serviceAccounts/registry-coffee-ingest@coffee-test-1.iam.gserviceaccount.com"
  }
}

variables {
  project_id      = "coffee-test-1"
  billing_account = "000000-000000-000000"
  owner_email     = "owner@example.com"
  verdict_model   = "claude-haiku-5-5"
}

run "admin_starts_only_the_named_jobs" {
  command = plan

  assert {
    condition     = toset([for k, m in google_cloud_run_v2_job_iam_member.admin : trimsuffix(trimsuffix(k, "-executor"), "-viewer")]) == local.admin_jobs
    error_message = "The admin's job grants must be exactly local.admin_jobs."
  }
  assert {
    condition = alltrue([for m in google_cloud_run_v2_job_iam_member.admin :
    contains(["roles/run.jobsExecutorWithOverrides", "roles/run.viewer"], m.role)])
    error_message = "On a job the admin gets only run-with-overrides/cancel and read, never update or IAM roles."
  }
  assert {
    condition     = length(google_cloud_run_v2_job_iam_member.admin) == 2 * length(local.admin_jobs)
    error_message = "Each named job gets both roles, and no other job gets any."
  }
  assert {
    condition     = !contains(keys(local.ingest_jobs), "discover") || !contains(local.admin_jobs, "discover")
    error_message = "discover and probe stay off the admin's list."
  }
}

run "admin_database_is_iam_only_and_scoped" {
  command = plan

  assert {
    condition     = google_sql_user.admin.type == "CLOUD_IAM_SERVICE_ACCOUNT" && google_sql_user.admin.password == null
    error_message = "The admin's database user is its IAM identity, with no password."
  }
  assert {
    condition = alltrue([for m in google_project_iam_member.admin_sql :
    contains(["roles/cloudsql.client", "roles/cloudsql.instanceUser"], m.role) && length(m.condition) == 1])
    error_message = "The admin's Cloud SQL roles are client and instanceUser, each conditioned on its instance."
  }
  assert {
    condition = anytrue([for f in google_sql_database_instance.admin.settings[0].database_flags :
    f.name == "cloudsql.iam_authentication" && f.value == "on"])
    error_message = "IAM database authentication must be on."
  }
  assert {
    condition     = length(google_sql_database_instance.admin.settings[0].ip_configuration[0].authorized_networks) == 0
    error_message = "No authorized networks: connect only through the connector."
  }
  assert {
    condition     = google_sql_database_instance.admin.settings[0].tier == "db-f1-micro" && google_sql_database_instance.admin.deletion_protection
    error_message = "The smallest tier, protected from deletion."
  }
}

run "admin_never_writes_the_lake_or_staging" {
  # apply against the mock provider: the members are known only then (nothing real is created)
  command = apply

  assert {
    condition = alltrue([for m in google_storage_bucket_iam_member.admin_snapshots :
    contains(["roles/storage.objectCreator", "roles/storage.objectViewer"], m.role)])
    error_message = "On its snapshot bucket the admin creates and reads; it never overwrites or deletes."
  }
  assert {
    condition     = google_storage_bucket_iam_member.ingest_staging.member != "serviceAccount:registry-coffee-admin-run@coffee-test-1.iam.gserviceaccount.com"
    error_message = "The staging bucket (raw lake files between jobs) is the jobs', never the admin's."
  }
  assert {
    condition = alltrue([for m in google_cloud_run_v2_job_iam_member.admin :
    m.member == "serviceAccount:registry-coffee-admin-run@coffee-test-1.iam.gserviceaccount.com"])
    error_message = "The job grants go to the admin's account."
  }
  assert {
    condition     = google_storage_bucket_iam_member.ingest_snapshots.role == "roles/storage.objectViewer"
    error_message = "The build job only reads the snapshots."
  }
}
