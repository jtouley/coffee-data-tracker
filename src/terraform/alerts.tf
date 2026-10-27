# Email the owner about money, errors and downtime.

resource "google_monitoring_notification_channel" "owner" {
  display_name = "registry.coffee owner"
  type         = "email"
  labels = {
    email_address = var.owner_email
  }
  depends_on = [google_project_service.apis]
}

data "google_project" "this" {}

resource "google_billing_budget" "monthly" {
  billing_account = var.billing_account
  display_name    = "registry.coffee monthly"
  budget_filter {
    projects = ["projects/${data.google_project.this.number}"]
  }
  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.budget_usd)
    }
  }
  dynamic "threshold_rules" {
    for_each = [0.5, 0.9, 1.0]
    content {
      threshold_percent = threshold_rules.value
    }
  }
  all_updates_rule {
    monitoring_notification_channels = [google_monitoring_notification_channel.owner.id]
  }
}

# Any ERROR line or 5xx response from the service. The app logs JSON with `severity` and `httpRequest.status`.
resource "google_monitoring_alert_policy" "errors" {
  display_name = "registry.coffee: errors"
  combiner     = "OR"
  conditions {
    display_name = "ERROR log or 5xx"
    condition_matched_log {
      filter = join(" AND ", [
        "resource.type=\"cloud_run_revision\"",
        "resource.labels.service_name=\"${google_cloud_run_v2_service.app.name}\"",
        "(severity>=ERROR OR httpRequest.status>=500)",
      ])
    }
  }
  alert_strategy {
    notification_rate_limit {
      period = "300s" # at most one email per 5 minutes
    }
    auto_close = "1800s"
  }
  notification_channels = [google_monitoring_notification_channel.owner.id]
}

resource "google_monitoring_uptime_check_config" "healthz" {
  display_name = "registry.coffee /healthz"
  timeout      = "10s"
  period       = "900s" # every 15 minutes: each check wakes the service from zero, so not more often
  http_check {
    path         = "/healthz"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }
  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = trimsuffix(trimprefix(google_cloud_run_v2_service.app.uri, "https://"), "/")
    }
  }
}

resource "google_monitoring_alert_policy" "down" {
  display_name = "registry.coffee: down"
  combiner     = "OR"
  conditions {
    display_name = "/healthz failing"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.healthz.uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "1800s"
      aggregations {
        alignment_period     = "1800s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.*"]
      }
    }
  }
  notification_channels = [google_monitoring_notification_channel.owner.id]
}
