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

variable "site_url" {
  description = "The public site the app links to; match the SITE_URL repository variable."
  type        = string
  default     = "https://registry.coffee"
}

variable "support_email" {
  description = "Where the app's beta-invite requests go; match the SUPPORT_EMAIL repository variable."
  type        = string
  default     = "support@registry.coffee"
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
  description = "Monthly budget; alerts at 50%, 90% and 100%. The runbook estimates $9-14 a month (app + ingestion jobs + two Cloud NAT gateways and their static IPs, docs/ingestion/RUNBOOK.md, Cost), so the first email ($10) can fire in a normal month; raise this if it is noise."
  type        = number
  default     = 20
}

variable "service" {
  description = "Name of the Cloud Run service and its Artifact Registry repository."
  type        = string
  default     = "registry-coffee"
}
