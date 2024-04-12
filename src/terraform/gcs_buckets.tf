resource "google_storage_bucket" "cloud_functions_bucket" {
  name = "cf-source-${random_id.bucket_suffix.hex}"
  location = var.region

  lifecycle {
    ignore_changes = [location]
  }
}

resource "google_storage_bucket" "coffee_data_bucket" {
  name = "coffee-data-${random_id.bucket_suffix.hex}"
  location = var.region
  
  lifecycle {
    ignore_changes = [location]
  }
}

resource "google_storage_bucket_object" "function_source" {
  name   = "coffee-data-function.zip"
  bucket = google_storage_bucket.cloud_functions_bucket.name
  source = "/Users/jasontouleyrou/Projects/coffee-data-tracker/src/cloud_function/coffee-data-function.zip" //may need to replace with full root path
}

resource "random_id" "bucket_suffix" {
  byte_length = 2
}