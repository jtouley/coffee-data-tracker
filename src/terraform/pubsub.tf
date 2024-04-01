resource "google_pubsub_topic" "coffee_data_topic" {
  name = "coffee-data-topic"
}

resource "google_pubsub_subscription" "coffee_data_subscription" {
  name  = "coffee-data-subscription"
  topic = google_pubsub_topic.coffee_data_topic.name

  message_retention_duration = "7200s"
  ack_deadline_seconds       = 20
}
