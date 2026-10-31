# Email the owner about money, errors, downtime and write volume.

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
  display_name = "registry.coffee /health"
  timeout      = "10s"
  period       = "900s" # every 15 minutes: each check wakes the service from zero, so not more often
  http_check {
    path         = "/health" # not /healthz: Google's front end answers paths ending in "z" itself
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
    display_name = "/health failing"
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

# Each saved shot, bag or equipment change is one Hugging Face commit (app/store.py `_commit` logs a `saved` event
# with `publish_ms`), and HF repos slow down after a few thousand commits. These count the saves and time them, so
# the owner hears before that matters (docs/app/WRITE_PATH.md). A save whose file went up in another request's
# commit still counts, so the count is an upper bound on commits.
locals {
  saved_filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.app.name}\"",
    "jsonPayload.logger=\"app.events\"",
    "jsonPayload.message=\"saved\"",
  ])
}

resource "google_logging_metric" "saves" {
  name   = "${var.service}-saves"
  filter = local.saved_filter
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
  depends_on = [google_project_service.apis]
}

resource "google_logging_metric" "save_publish_ms" {
  name            = "${var.service}-save-publish-ms"
  filter          = local.saved_filter
  value_extractor = "EXTRACT(jsonPayload.publish_ms)"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "ms"
  }
  bucket_options {
    exponential_buckets {
      num_finite_buckets = 40
      growth_factor      = 1.3
      scale              = 10 # 10 ms up to about 36 minutes
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "write_volume" {
  display_name = "registry.coffee: write volume"
  combiner     = "OR"
  conditions {
    display_name = "Over ${var.saves_per_day_alert} saves in 24 h"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.saves.name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.saves_per_day_alert
      duration        = "0s"
      aggregations {
        alignment_period     = "86400s" # a rolling 24 hours
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM" # across revisions
      }
    }
  }
  conditions {
    display_name = "p95 save publish over 5 s in 24 h"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.save_publish_ms.name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 5000
      duration        = "0s"
      aggregations {
        alignment_period     = "86400s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_PERCENTILE_95"
      }
    }
  }
  documentation {
    content   = "One Hugging Face commit per save is reaching its limit. See docs/app/WRITE_PATH.md."
    mime_type = "text/markdown"
  }
  notification_channels = [google_monitoring_notification_channel.owner.id]
}
