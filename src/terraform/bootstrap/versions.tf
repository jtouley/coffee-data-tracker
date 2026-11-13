# The identities GitHub Actions uses to plan and apply src/terraform/ (.github/workflows/terraform-apply.yml), and the
# private bucket the saved plans go to. A root of its own, applied only by the owner from Cloud Shell
# (docs/ingestion/final-shell-run.sh, then docs/app/RUNNING.md "Changing the infrastructure"): the gate is never
# managed by what it gates. Neither identity can change anything here: no role below writes a workload identity pool,
# a provider, a custom role, the project's IAM policy, these accounts' IAM policies or these buckets' IAM policies.
terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0" # the main root's constraint; .terraform.lock.hcl is a copy of its lock file
    }
  }
  # The main root's state bucket, under its own prefix:
  #   terraform init -backend-config="bucket=coffee-data-tracker-tfstate"
  backend "gcs" {
    prefix = "registry-coffee-bootstrap"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  # Billing account IAM needs a quota project with application-default credentials, as in the main root.
  user_project_override = true
  billing_project       = var.project_id
}
