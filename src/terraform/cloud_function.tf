resource "google_cloudfunctions_function" "coffee_data_function" {
  name        = "coffee-data-function"
  description = "Processes coffee data submissions and stores them in GCS"
  runtime     = "python39"

  available_memory_mb   = 128
  source_archive_bucket = google_storage_bucket.cloud_functions_bucket.name
  source_archive_object = google_storage_bucket_object.function_source.name
  trigger_http          = true
  entry_point           = "process_coffee_data"

  environment_variables = {
    GCS_BUCKET = google_storage_bucket.coffee_data_bucket.name
  }

  labels = {
    env = "production"
  }
}

output "cloud_function_url" {
  value = google_cloudfunctions_function.coffee_data_function.https_trigger_url
}

// IAM policy for unauthenticated access - needed to be added due to CORS issue
resource "google_cloudfunctions_function_iam_member" "public_invoker" {
  cloud_function = google_cloudfunctions_function.coffee_data_function.name

  role   = "roles/cloudfunctions.invoker"
  member = "allUsers"
}