resource "google_bigquery_table" "coffee_table" {
  dataset_id = google_bigquery_dataset.coffee_dataset.dataset_id
  table_id   = "coffee_tracker_results"

  schema = <<EOF
[
  {
    "name": "submitted_on",
    "type": "TIMESTAMP",
    "mode": "REQUIRED"
  },
  {
    "name": "coffee_roaster",
    "type": "STRING",
    "mode": "NULLABLE"
  },
  {
    "name": "coffee_roast",
    "type": "STRING",
    "mode": "NULLABLE"
  },
  {
    "name": "coffee_origin",
    "type": "STRING",
    "mode": "NULLABLE"
  },
  {
    "name": "grinder_setting",
    "type": "INTEGER",
    "mode": "NULLABLE"
  },
  {
    "name": "coffee_dose",
    "type": "FLOAT",
    "mode": "NULLABLE"
  },
  {
    "name": "coffee_yield",
    "type": "FLOAT",
    "mode": "NULLABLE"
  },
  {
    "name": "shot_duration",
    "type": "INTEGER",
    "mode": "NULLABLE"
  },
  {
    "name": "coffee_rating",
    "type": "INTEGER",
    "mode": "NULLABLE"
  },
  {
    "name": "notes",
    "type": "STRING",
    "mode": "NULLABLE"
  }
]
EOF
}
