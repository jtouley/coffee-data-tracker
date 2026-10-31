# A fixed, identifiable egress IP for the crawl: one small VM that runs a GitHub Actions self-hosted runner (label
# `crawler`), behind a reserved static address. Its own root module and state prefix, so planning or destroying it
# never touches the app's resources in ../. Proof of concept: see runbook/crawler-runner.md before applying.
terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
  # The same state bucket as the app, under its own prefix:
  #   terraform init -backend-config="bucket=coffee-data-tracker-tfstate"
  backend "gcs" {
    prefix = "crawler"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

# compute for the VM and its address, secretmanager for the runner token, iap for SSH through IAP only.
resource "google_project_service" "apis" {
  for_each = toset([
    "compute.googleapis.com",
    "secretmanager.googleapis.com",
    "iap.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}
