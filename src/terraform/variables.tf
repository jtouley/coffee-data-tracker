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

variable "email" {
  description = "Email recovery (docs/app/signup-ux-spec.md §5): mounts the EMAIL_KEY and RESEND_API_KEY secrets on the app and adds the \"email not sent\" alert. The secrets themselves always exist; turning this off only unmounts them. Turn on after the Resend domain is verified and both secrets have values (RUNNING.md, \"Email\")."
  type        = bool
  default     = false
}

variable "usage_domain" {
  description = "The usage collector's hostname (usage.tf): its own subdomain, so nothing of the app's is same-origin with it."
  type        = string
  default     = "e.registry.coffee"
}

variable "usage_dataset" {
  description = "owner/name of the private Hugging Face dataset the usage collector appends to; never published."
  type        = string
  default     = "jtouley/coffee-usage"
}

variable "usage_enabled" {
  description = "Creates the usage collector's service, its public invoker, its domain mapping and its request-log exclusion (usage.tf). Turn on in ci.tfvars once the apply account can act as registry-coffee-usage and write log exclusions (RUNNING.md, \"Usage\"); before that an apply would fail partway."
  type        = bool
  default     = false
}

variable "usage_token" {
  description = "Mounts the USAGE_HF_TOKEN secret on the usage collector as HF_TOKEN. The secret always exists; turn this on once it has a value (RUNNING.md, \"Usage\")."
  type        = bool
  default     = false
}

variable "usage_image" {
  description = "The image the usage collector runs: an app image deploy.yml built from main, by digest (`<region>-docker.pkg.dev/<project>/<service>/app@sha256:<64 hex>`), set in ci.tfvars. Empty (the default) runs Google's sample image. Never a tag: a tag moves, a digest doesn't."
  type        = string
  default     = ""
  validation {
    condition     = var.usage_image == "" || can(regex("^[a-z0-9-]+-docker\\.pkg\\.dev/[a-z0-9-]+/[a-z0-9-]+/app@sha256:[0-9a-f]{64}$", var.usage_image))
    error_message = "usage_image must be the app's image pinned by digest: <region>-docker.pkg.dev/<project>/<repository>/app@sha256:<64 hex>, from deploy.yml's run on main (its attest job signs it). A tag such as :latest is refused."
  }
}
