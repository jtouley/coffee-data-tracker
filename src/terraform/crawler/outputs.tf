output "egress_ip" {
  description = "The crawl's fixed egress address: what the stores see. Publish it next to the User-Agent."
  value       = google_compute_address.egress.address
}

output "ssh" {
  description = "SSH through IAP (no public SSH port)."
  value       = "gcloud compute ssh ${google_compute_instance.crawler.name} --zone ${var.zone} --tunnel-through-iap"
}

output "runner_token_secret" {
  description = "Add the registration token here (one version) before the VM's first boot."
  value       = google_secret_manager_secret.runner_token.secret_id
}

output "service_account" {
  description = "The VM's service account: no project roles, one secret accessor binding."
  value       = google_service_account.crawler.email
}
