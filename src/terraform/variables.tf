variable "project_id" {
  description = "The ID of the Google Cloud project"
  type        = string
}

variable "region" {
  description = "The Google Cloud region where resources will be deployed"
  type        = string
  default     = "us-central1"
}

variable "function_name" {
  description = "The name of the Google Cloud Function"
  type        = string
}

variable "runtime" {
  description = "The runtime environment for the Google Cloud Function"
  type        = string
  default     = "python39"
}

variable "entry_point" {
  description = "The name of the function (within the code) that will be executed"
  type        = string
}

variable "source_archive_bucket" {
  description = "The name of the Google Cloud Storage bucket where the Cloud Function source code will be stored"
  type        = string
}

variable "source_archive_object" {
  description = "The name of the Cloud Function source code archive file (ZIP format) stored in the Google Cloud Storage bucket"
  type        = string
}

variable "set_trigger_http" {
  description = "Boolean variable to set if the trigger will be HTTP"
  type        = bool
  default     = true
}

variable "cloud_storage_bucket_name" {
  description = "The name of the Google Cloud Storage bucket for storing data"
  type        = string
}

variable "environment_variables" {
  description = "Environment variables to be set for the Cloud Function"
  type        = map(string)
  default     = {}
}
