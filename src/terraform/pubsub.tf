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
    {"name": "coffee_roaster", "type": "string", "doc": "Name of the roastery where the coffee beans were roasted."},
    {"name": "coffee_roast", "type": "string", "doc": "The roast level of the coffee beans, e.g., Light, Medium, Dark."},
    {"name": "coffee_origin", "type": "string", "doc": "The geographic origin of the coffee beans."},
    {"name": "grinder_setting", "type": "float", "doc": "The grinder setting used to grind the coffee beans."},
    {"name": "coffee_dose", "type": "float", "doc": "The amount of coffee used for the brew, typically measured in grams."},
    {"name": "coffee_yield", "type": "float", "doc": "The amount of coffee extracted, typically measured in grams."},
    {"name": "shot_duration", "type": "float", "doc": "The time taken to pull the espresso shot, typically measured in seconds."},
    {"name": "coffee_rating", "type": "int", "doc": "A subjective rating of the coffee's quality, on a scale of 1 to 5."},
    {"name": "is_latte", "type": "boolean", "doc": "A flag indicating whether the coffee is a latte."},
    {"name": "notes", "type": ["null", "string"], "default": null, "doc": "Additional notes about the coffee, brewing method, or tasting notes."},
    {"name": "created_at", "type": "string", "doc": "The timestamp when the coffee data was created."},
    {"name": "received_at", "type": "string", "doc": "The timestamp when the coffee data was received and stored."}
  ]
}
EOF
}

resource "google_pubsub_subscription" "coffee_data_bigquery_subscription" {
  name  = "coffee-data-bigquery-subscription"
  topic = google_pubsub_topic.coffee_data_topic.id

  bigquery_config {
    table                = "${google_bigquery_table.coffee_table.project}.${google_bigquery_table.coffee_table.dataset_id}.${google_bigquery_table.coffee_table.table_id}"
    use_topic_schema     = true
    write_metadata       = false
    drop_unknown_fields = true
  }

  depends_on = [google_bigquery_table.coffee_table]
}
