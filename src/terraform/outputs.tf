output "service_url" {
  description = "The run.app address (works before the domain does)."
  value       = google_cloud_run_v2_service.app.uri
}

output "image" {
  description = "Where the deploy workflow pushes the image (GitHub variable GCP_IMAGE)."
  value       = local.image
}

output "workload_identity_provider" {
  description = "GitHub variable GCP_WIF_PROVIDER."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "deploy_service_account" {
  description = "GitHub variable GCP_DEPLOY_SA."
  value       = google_service_account.deploy.email
}

output "dns" {
  description = "The record to add at the DNS host once the domain mapping exists."
  value       = "CNAME ${split(".", var.domain)[0]} -> ghs.googlehosted.com."
}

output "usage_url" {
  description = "The usage collector's run.app address (works before its domain does)."
  value       = one(google_cloud_run_v2_service.usage[*].uri)
}

output "usage_dns" {
  description = "The record to add at the DNS host once the usage collector's domain mapping exists."
  value       = "CNAME ${split(".", var.usage_domain)[0]} -> ghs.googlehosted.com."
}
