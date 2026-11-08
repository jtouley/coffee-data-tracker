# registry.coffee on Cloud Run, in the original GCP project (archive/v0-gcp had a Cloud Function, GCS and BigQuery
# here; those stay on that branch). Terraform makes the empty secrets; the owner adds their values (RUNNING.md).
terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0" # was ~> 3.5: 3.x has no Cloud Run v2, Workload Identity Federation or current Artifact Registry
    }
  }
  # State lives in a GCS bucket the owner creates once (see docs/app/RUNNING.md):
  #   terraform init -backend-config="bucket=coffee-data-tracker-tfstate"
  backend "gcs" {
    prefix = "registry-coffee"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  # The billing budget API needs a quota project; with application-default credentials there's none unless set.
  user_project_override = true
  billing_project       = var.project_id
}

locals {
  # The app's secrets, as environment variables. Values are never in Terraform or its state.
  # The two email secrets always exist, whatever var.email says, so turning email off never destroys EMAIL_KEY or
  # its versions (review H2): var.email decides only whether the service mounts them (cloud_run.tf), and so whether
  # the app's email features show (docs/app/RUNNING.md, "Email").
  email_secrets = ["EMAIL_KEY", "RESEND_API_KEY"]
  secrets       = concat(["HF_TOKEN", "ANTHROPIC_API_KEY", "AUTH_PEPPER", "SESSION_SECRET", "OWNER_SETUP_CODE"], local.email_secrets)
  image         = "${var.region}-docker.pkg.dev/${var.project_id}/${var.service}/app"
}

# The APIs the resources below need.
resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    "billingbudgets.googleapis.com",
    "cloudresourcemanager.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}
