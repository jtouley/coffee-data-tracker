variable "project_id" {
  description = "The GCP project: the same one the app runs in."
  type        = string
  default     = "coffee-data-tracker"
}

variable "region" {
  description = "Region of the VM and its static address. us-central1 is one of the free-tier e2-micro regions."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "Zone of the VM."
  type        = string
  default     = "us-central1-a"
}

variable "github_repo" {
  description = "owner/name of the repository the runner registers with and the only one whose jobs it accepts."
  type        = string
  default     = "jtouley/coffee-data-tracker"
}

variable "machine_type" {
  description = "e2-micro (1 GB, free tier) runs live-test.yml's crawl (~370 MB peak) with the swap file. Use e2-small (2 GB) before weekly.yml moves here: its dbt build alone peaks at 0.7-1.0 GB."
  type        = string
  default     = "e2-micro"
}

variable "disk_gb" {
  description = "Boot disk size (standard persistent disk). Runner, uv, Python and a lake copy fit in 20 GB."
  type        = number
  default     = 20
}

variable "runner_name" {
  description = "Name of the runner and the VM."
  type        = string
  default     = "crawler-1"
}

variable "runner_version" {
  description = "actions/runner release to install (self-update is off: bump this and runner_sha256, then rebuild the VM)."
  type        = string
  default     = "2.338.2"
}

variable "runner_sha256" {
  description = "SHA-256 of actions-runner-linux-x64-<runner_version>.tar.gz, from the release notes."
  type        = string
  default     = "baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40"
}
