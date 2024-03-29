resource "google_storage_bucket" "cloud_functions_bucket" {
  name = "cf-source-${random_id.bucket_suffix.hex}"
}

resource "google_storage_bucket" "coffee_data_bucket" {
  name = "coffee-data-${random_id.bucket_suffix.hex}"
}

resource "google_storage_bucket_object" "function_source" {
  name   = "coffee-data-function.zip"
  bucket = google_storage_bucket.cloud_functions_bucket.name
  source = "src/cloud_function/coffee-data-function.zip"
}

resource "random_id" "bucket_suffix" {
  byte_length = 2
}