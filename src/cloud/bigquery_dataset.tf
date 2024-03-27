resource "google_bigquery_dataset" "coffee_dataset" {
  dataset_id  = "coffee_data"
  location    = "US"
  description = "Dataset containing coffee brewing data."
}