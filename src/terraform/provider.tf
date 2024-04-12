terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0.0"
    }
  }
}

provider "google" {
  project = "coffee-data-tracker"
  region  = "us-central1"
}

data "google_project" "project" {
  project_id = var.project_id
}