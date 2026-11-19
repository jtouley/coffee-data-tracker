# The repository variables the Terraform workflows read (docs/app/RUNNING.md, "Changing the infrastructure").
# None is a secret.

output "github_variables" {
  description = "Repository variables for terraform-plan.yml and terraform-apply.yml: name => value."
  value = {
    GCP_TF_PLAN_PROVIDER  = google_iam_workload_identity_pool_provider.tf_plan.name
    GCP_TF_PLAN_SA        = google_service_account.tf_plan.email
    GCP_TF_APPLY_PROVIDER = google_iam_workload_identity_pool_provider.tf_apply.name
    GCP_TF_APPLY_SA       = google_service_account.tf_apply.email
    TF_STATE_BUCKET       = var.state_bucket
    TF_PLANS_BUCKET       = google_storage_bucket.tf_plans.name
  }
}
