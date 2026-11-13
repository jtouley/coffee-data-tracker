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
  sensitive   = true
}

variable "owner_email" {
  description = "Where budget, error and uptime alerts go."
  type        = string
  sensitive   = true
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

variable "admin_domain" {
  description = "The admin's own hostname (admin.tf): its sign-in origin and passkeys' relying party."
  type        = string
  default     = "admin.registry.coffee"
}

variable "admin_cold_start_min_samples" {
  description = "Container starts an hour needs before the admin's cold-start alert can fire, so one slow start never pages (docs/design/admin-registry.md §7.1)."
  type        = number
  default     = 3
}

variable "email" {
  description = "Email recovery (docs/app/signup-ux-spec.md §5): mounts the EMAIL_KEY and RESEND_API_KEY secrets on the app and adds the \"email not sent\" alert. The secrets themselves always exist; turning this off only unmounts them. Turn on after the Resend domain is verified and both secrets have values (RUNNING.md, \"Email\")."
  type        = bool
  default     = false
}
