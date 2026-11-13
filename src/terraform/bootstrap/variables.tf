variable "project_id" {
  description = "The project src/terraform/ manages (its var.project_id)."
  type        = string
  default     = "coffee-data-tracker"
}

variable "region" {
  description = "The main root's region (its var.region)."
  type        = string
  default     = "us-central1"
}

variable "service" {
  description = "The main root's var.service: the prefix of the accounts, the image repository and the jobs it manages."
  type        = string
  default     = "registry-coffee"
}

variable "github_repo" {
  description = "owner/name of the repository whose workflows may plan and apply."
  type        = string
  default     = "jtouley/coffee-data-tracker"
}

# The numeric ids as well as the name: a name can be deleted and registered again by someone else, an id can't.
variable "github_repository_id" {
  description = "GitHub's numeric id of var.github_repo (the token's repository_id claim)."
  type        = string
  default     = "778310280"
}

variable "github_repository_owner_id" {
  description = "GitHub's numeric id of the repository's owner (the token's repository_owner_id claim)."
  type        = string
  default     = "14210040"
}

variable "apply_environment" {
  description = "The GitHub environment the apply job runs in. Its required reviewer is the approval (docs/app/RUNNING.md)."
  type        = string
  default     = "terraform-apply"
}

variable "state_bucket" {
  description = "The GCS bucket that holds both roots' state (the main root under registry-coffee/, this one under registry-coffee-bootstrap/)."
  type        = string
  default     = "coffee-data-tracker-tfstate"
}

variable "state_prefix" {
  description = "The main root's state prefix (its backend block). The two identities reach objects under it only."
  type        = string
  default     = "registry-coffee"
}

variable "billing_account" {
  description = "The billing account the main root's budget lives on (the main root's var.billing_account, XXXXXX-XXXXXX-XXXXXX)."
  type        = string
  sensitive   = true
}
