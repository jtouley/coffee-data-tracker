variable "region" {
  description = "The Google Cloud region where resources will be deployed"
  type        = string
  default     = "us-central1"
}

variable "runtime" {
  description = "The runtime environment for the Google Cloud Function"
  type        = string
  default     = "python39"
}

variable "set_trigger_http" {
  description = "Boolean variable to set if the trigger will be HTTP"
  type        = bool
  default     = true
}

variable "environment_variables" {
  description = "Environment variables to be set for the Cloud Function"
  type        = map(string)
  default     = {}
}

variable "project_id" {
  description = "The Google Cloud project ID."
  type        = string
  default     = "coffee-data-tracker"
}