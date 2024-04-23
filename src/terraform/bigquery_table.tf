resource "google_bigquery_table" "coffee_table" {
  dataset_id          = google_bigquery_dataset.coffee_dataset.dataset_id
  table_id            = "coffee_tracker_results"
  deletion_protection = false

  description = "Stores detailed records of coffee brewing sessions including roaster, origin, brew parameters, and tasting notes."

  time_partitioning {
    type                      = "DAY"
    expiration_ms             = 7776000000 # 90 days in milliseconds
    field                     = "received_at"
    require_partition_filter  = false
  }

  schema = <<EOF
[
  {
    "name": "coffee_roaster",
    "type": "STRING",
    "mode": "REQUIRED",
    "description": "Name of the roastery where the coffee beans were roasted."
  },
  {
    "name": "coffee_roast",
    "type": "STRING",
    "mode": "REQUIRED",
    "description": "The roast level of the coffee beans, e.g., Light, Medium, Dark."
  },
  {
    "name": "coffee_origin",
    "type": "STRING",
    "mode": "REQUIRED",
    "description": "The geographic origin of the coffee beans."
  },
  {
    "name": "grinder_setting",
    "type": "FLOAT",
    "mode": "REQUIRED",
    "description": "The grinder setting used to grind the coffee beans."
  },
  {
    "name": "coffee_dose",
    "type": "FLOAT",
    "mode": "REQUIRED",
    "description": "The amount of coffee used for the brew, typically measured in grams."
  },
  {
    "name": "coffee_yield",
    "type": "FLOAT",
    "mode": "REQUIRED",
    "description": "The amount of coffee extracted, typically measured in grams."
  },
  {
    "name": "shot_duration",
    "type": "FLOAT",
    "mode": "REQUIRED",
    "description": "The time taken to pull the espresso shot, typically measured in seconds."
  },
  {
    "name": "coffee_rating",
    "type": "INTEGER",
    "mode": "REQUIRED",
    "description": "A subjective rating of the coffee's quality, on a scale of 1 to 5."
  },
  {
    "name": "is_latte",
    "type": "BOOLEAN",
    "mode": "NULLABLE",
    "description": "A flag indicating whether the coffee is a latte."
  },
  {
    "name": "notes",
    "type": "STRING",
    "mode": "NULLABLE",
    "description": "Additional notes about the coffee, brewing method, or tasting notes."
  },
  {
    "name": "created_at",
    "type": "TIMESTAMP",
    "mode": "REQUIRED",
    "description": "The timestamp when the coffee data was created."
  },
  {
    "name": "received_at",
    "type": "TIMESTAMP",
    "mode": "REQUIRED",
    "description": "The timestamp when the coffee data was received and stored."
  },
  {
    "name": "message_id",
    "type": "STRING",
    "mode": "NULLABLE",
    "description": "Unique identifier for each message."
  },
  {
    "name": "subscription_name",
    "type": "STRING",
    "mode": "NULLABLE",
    "description": "Name of the Pub/Sub subscription."
  },
  {
    "name": "publish_time",
    "type": "TIMESTAMP",
    "mode": "NULLABLE",
    "description": "The time at which the message was published."
  },
  {
    "name": "attributes",
    "type": "STRING",
    "mode": "NULLABLE",
    "description": "JSON string of the message attributes."
  },
  {
    "name": "data",
    "type": "STRING",
    "mode": "NULLABLE",
    "description": "The raw message data as a string."
  }
]
EOF
}
