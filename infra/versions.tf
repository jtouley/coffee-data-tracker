terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
  # State lives in a GCS bucket the owner creates once (see docs/app/RUNNING.md):
  #   terraform init -backend-config="bucket=<project>-tfstate"
  backend "gcs" {
    prefix = "registry-coffee"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
