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
  description = "e2-small (2 GB) fits the weekly run: crawl ~370 MB plus dbt 0.7-1.0 GB plus the runner. e2-micro (1 GB, free tier) fits live-test.yml's crawl alone, with the swap file."
  type        = string
  default     = "e2-small"
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
  description = "actions/runner release to install. Must match runner_sha256."
  type        = string
  default     = "2.338.2"
}

variable "runner_sha256" {
  description = "SHA-256 of actions-runner-linux-x64-<runner_version>.tar.gz, from the release notes."
  type        = string
  default     = "baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40"
}

# The runner's pre-job hook refuses every job outside these. Keep allowed_workflows equal to the set of workflows
# whose jobs target the `crawler` label (tests/test_crawler_runner.py checks it).
variable "allowed_events" {
  description = "Events whose jobs the runner accepts. Never pull_request or pull_request_target."
  type        = list(string)
  default     = ["workflow_dispatch", "schedule"]
  validation {
    condition     = length(setintersection(var.allowed_events, ["pull_request", "pull_request_target", "pull_request_review", "pull_request_review_comment", "issue_comment", "workflow_run", "push"])) == 0
    error_message = "The crawler runner accepts only workflow_dispatch and schedule jobs."
  }
}

variable "allowed_refs" {
  description = "Git refs the runner accepts jobs for. A dispatch on another branch is refused."
  type        = list(string)
  default     = ["refs/heads/main"]
}

variable "allowed_workflows" {
  description = "Workflow files (repository paths) the runner accepts jobs from."
  type        = list(string)
  default     = [".github/workflows/live-test.yml"]
}
