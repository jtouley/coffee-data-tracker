variable "project_id" {
  description = "The GCP project the app runs in: the original coffee-data-tracker project."
  type        = string
  default     = "coffee-data-tracker"
}

variable "region" {
  description = "Cloud Run region. us-central1 supports domain mappings."
  type        = string
  default     = "us-central1"
}

variable "github_repo" {
  description = "owner/name of the repository whose main branch may deploy (Workload Identity Federation)."
  type        = string
  default     = "jtouley/coffee-data-tracker"
}

variable "domain" {
  description = "The app's own hostname."
  type        = string
  default     = "app.registry.coffee"
}

variable "billing_account" {
  description = "Billing account id (XXXXXX-XXXXXX-XXXXXX) for the monthly budget alert."
  type        = string
}

variable "owner_email" {
  description = "Where budget, error and uptime alerts go."
  type        = string
}

variable "budget_usd" {
  description = "Monthly budget; alerts at 50%, 90% and 100%."
  type        = number
  default     = 5
}

variable "service" {
  description = "Name of the Cloud Run service and its Artifact Registry repository."
  type        = string
  default     = "registry-coffee"
}
