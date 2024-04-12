resource "google_pubsub_topic" "coffee_data_topic" {
  name = "coffee-data-topic"
}

resource "google_pubsub_subscription" "coffee_data_subscription" {
  name  = "coffee-data-subscription"
  topic = google_pubsub_topic.coffee_data_topic.name

  message_retention_duration = "7200s"
  ack_deadline_seconds       = 20
}

resource "google_pubsub_schema" "coffee_data_schema" {
  name       = "coffee_data_schema"
  project    = var.project_id
  type       = "AVRO"
  definition = <<EOF
{
  "type": "record",
  "name": "CoffeeData",
  "fields": [
    {"name": "coffee_roaster", "type": "string"},
    {"name": "coffee_roast", "type": "string"},
    {"name": "coffee_origin", "type": "string"},
    {"name": "grinder_setting", "type": "string"},
    {"name": "coffee_dose", "type": "string"},
    {"name": "coffee_yield", "type": "string"},
    {"name": "shot_duration", "type": "string"},
    {"name": "coffee_rating", "type": "string"},
    {"name": "is_latte", "type": "boolean"},
    {"name": "notes", "type": ["null", "string"], "default": null},
    {"name": "created_at", "type": "string"},
    {"name": "received_at", "type": "string"}
  ]
}
EOF
}

resource "google_pubsub_subscription" "coffee_data_bigquery_subscription" {
  name  = "coffee-data-bigquery-subscription"
  topic = google_pubsub_topic.coffee_data_topic.id

  bigquery_config {
    table              = "${google_bigquery_table.coffee_table.project}.${google_bigquery_table.coffee_table.dataset_id}.${google_bigquery_table.coffee_table.table_id}"
    use_topic_schema   = true
    write_metadata     = true
    drop_unknown_fields = true
  }
}



