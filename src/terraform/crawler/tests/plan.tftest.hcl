# `terraform test` from src/terraform/crawler (after `terraform init -backend=false`): plans against a mock provider,
# so it needs no credentials and creates nothing.
mock_provider "google" {}

run "plan" {
  command = plan

  assert {
    condition     = google_compute_firewall.iap_ssh.source_ranges == toset(["35.235.240.0/20"])
    error_message = "Only IAP may reach the VM."
  }
  assert {
    condition     = length(google_compute_firewall.iap_ssh.allow) == 1 && google_compute_firewall.iap_ssh.direction == "INGRESS"
    error_message = "One ingress rule, SSH only."
  }
  assert {
    condition     = google_compute_instance.crawler.metadata["enable-oslogin"] == "TRUE" && google_compute_instance.crawler.metadata["block-project-ssh-keys"] == "TRUE"
    error_message = "SSH by OS Login only."
  }
  assert {
    condition     = google_compute_instance.crawler.metadata["crawler-allowed-events"] == "workflow_dispatch schedule"
    error_message = "The runner accepts dispatched and scheduled jobs only."
  }
  assert {
    condition     = google_compute_instance.crawler.metadata["crawler-allowed-refs"] == "refs/heads/main"
    error_message = "The runner accepts jobs on main only."
  }
  assert {
    condition     = google_compute_network.crawler.auto_create_subnetworks == false
    error_message = "Its own network, not default."
  }
}

run "refuses_pull_request_events" {
  command = plan
  variables {
    allowed_events = ["workflow_dispatch", "pull_request"]
  }
  expect_failures = [var.allowed_events]
}
