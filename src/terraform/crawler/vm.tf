# The VM, the account it runs as, and the secret it registers the runner with.

# No project roles at all. It can read one secret (below); the runner's jobs can't reach the metadata server, so
# they can't act as this account either (scripts/startup.sh).
resource "google_service_account" "crawler" {
  account_id   = "crawler-runner"
  display_name = "crawler VM (GitHub Actions self-hosted runner)"
}

# A GitHub runner registration token: valid for one hour and only for registering a runner. The owner adds it as a
# version right before the VM first boots (runbook/crawler-runner.md); its value is never in Terraform or its state.
resource "google_secret_manager_secret" "runner_token" {
  secret_id = "crawler-runner-registration-token"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "crawler" {
  secret_id = google_secret_manager_secret.runner_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.crawler.email}"
}

resource "google_compute_instance" "crawler" {
  name         = var.runner_name
  machine_type = var.machine_type
  zone         = var.zone
  tags         = ["crawler"]
  labels       = { role = "crawler" }

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = var.disk_gb
      type  = "pd-standard"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.crawler.id
    access_config {
      nat_ip       = google_compute_address.egress.address
      network_tier = "PREMIUM"
    }
  }

  service_account {
    email  = google_service_account.crawler.email
    scopes = ["cloud-platform"] # IAM decides; the account holds one secret accessor binding
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  metadata = {
    enable-oslogin         = "TRUE" # SSH by IAM identity through IAP, no keys in metadata
    block-project-ssh-keys = "TRUE"
    serial-port-enable     = "FALSE"
    startup-script         = file("${path.module}/scripts/startup.sh")
    crawler-job-started    = file("${path.module}/scripts/job-started.sh")
    crawler-repo           = var.github_repo
    crawler-runner-name    = var.runner_name
    crawler-runner-version = var.runner_version
    crawler-runner-sha256  = var.runner_sha256
    crawler-token-secret   = google_secret_manager_secret.runner_token.secret_id
  }

  # The startup script reads the token secret on first boot, so the binding must exist first.
  depends_on = [google_secret_manager_secret_iam_member.crawler]
}
