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

# --- The weekly ingestion pipeline (ingestion.tf) ------------------------------------------------------------------
# Following Google's cloud-run-alert-configuration skill (references/jobs.md): alert at once on any failed execution of
# a job. `run.googleapis.com/job/completed_execution_count` (DELTA, resource cloud_run_job with job_name and location,
# metric label `result`) counts completed executions; in PromQL it is
# run_googleapis_com:job_completed_execution_count. An execution fails only after its retries (max_retries: 0 for the
# crawls, 1 otherwise) and on the 240-minute timeout; a crawl that hits its deadline exits 0 and isn't a failure.

# One policy per job, so the email names the job and its first command.
resource "google_monitoring_alert_policy" "ingest_job_failed" {
  for_each     = local.ingest_jobs
  display_name = "registry.coffee: ingest job ${each.key} failed"
  combiner     = "OR"
  conditions {
    display_name = "${each.key}: an execution ended failed"
    condition_prometheus_query_language {
      query    = <<-EOT
        sum(
          increase(run_googleapis_com:job_completed_execution_count{result="failed", job_name="${google_cloud_run_v2_job.ingest[each.key].name}", location="${var.region}"}[15m])
        ) by (job_name, location) > 0
      EOT
      duration = "0s" # page on the first failed execution
    }
  }
  documentation {
    mime_type = "text/markdown"
    subject   = "registry.coffee: ingest job ${each.key} failed"
    content   = <<-EOT
      An execution of the Cloud Run job `${each.key}` ended **failed** (non-zero exit after its retries, or the 240-minute timeout). First:

          gcloud run jobs executions list --job ${each.key} --region ${var.region} --limit 5

      Then the failed execution's logs (its name is in the list):

          gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="${each.key}" AND labels."run.googleapis.com/execution_name"="<execution>"' --limit 200 --freshness 1d

      A failed `build`: if the gate failed, no new release this week and Tuesday's site run fails without exporting. If only egress-check failed (an IP refused on over 8% of first contacts), the release is already published: check the egress IPs, not the data. A failed `publish` or `classify` exits 0 and is not caught here. Steps: docs/ingestion/RUNBOOK.md, section 5.
    EOT
  }
  user_labels = {
    job = each.key
  }
  alert_strategy {
    auto_close = "604800s"
  }
  notification_channels = [google_monitoring_notification_channel.owner.id]
}

# Cloud Scheduler logs each attempt; one that couldn't start its job (a 4xx/5xx from the Cloud Run Admin API, a
# permission or deadline error) is logged at ERROR on the cloud_scheduler_job resource. Only this pipeline's entries.
resource "google_monitoring_alert_policy" "ingest_schedule_failed" {
  display_name = "registry.coffee: ingest schedule failed to start a job"
  combiner     = "OR"
  conditions {
    display_name = "Cloud Scheduler ERROR on ${var.service}-*"
    condition_matched_log {
      filter = join(" AND ", [
        "resource.type=\"cloud_scheduler_job\"",
        "resource.labels.job_id=~\"^${var.service}-\"",
        "severity>=ERROR",
      ])
      label_extractors = {
        job_id = "EXTRACT(resource.labels.job_id)"
      }
    }
  }
  documentation {
    mime_type = "text/markdown"
    subject   = "registry.coffee: Cloud Scheduler failed to start $${log.extracted_label.job_id}"
    content   = <<-EOT
      The Cloud Scheduler entry `$${log.extracted_label.job_id}` failed to start its Cloud Run job, so that slot didn't run. First:

          gcloud scheduler jobs describe $${log.extracted_label.job_id} --location ${var.region}

      `status` holds the error. The attempt's log: `gcloud logging read 'resource.type="cloud_scheduler_job" AND resource.labels.job_id="$${log.extracted_label.job_id}"' --limit 5 --freshness 1d`. To run the slot by hand: `gcloud run jobs execute <job> --region ${var.region}` (the job is the entry's name without `${var.service}-` and `-resume`). Steps: docs/ingestion/RUNBOOK.md, section 5.
    EOT
  }
  alert_strategy {
    notification_rate_limit {
      period = "300s" # each job_id has its own alert; Google caps new alerts at 2 a minute and 20 notifications a day
    }
    auto_close = "1800s"
  }
  notification_channels = [google_monitoring_notification_channel.owner.id]
}

# No "missing release" policy (no `build` success between Tue 06:17 and 12:00 UTC). Google documents no reliable way
# to alert on the absence of a weekly event:
# - a metric-absence condition (or a threshold one with EVALUATION_MISSING_DATA_ACTIVE) waits at most 23.5 hours, and
#   has no time-of-week window: on a weekly job it would be met six days out of seven;
# - a log-match condition fires on an entry that arrives, never on one that doesn't;
# - a PromQL condition gated on day_of_week()/hour() over a DELTA metric that has no points between executions isn't
#   a pattern Google documents, and an empty series would read as "no success" every Tuesday.
# What covers it instead: ingest_job_failed (a failed or timed-out build, or a failed merge/enrich-week before it),
# ingest_schedule_failed (a build slot that didn't start), and site.yml's Tuesday 11:17 run, which fails red when the
# served release isn't this week's (GitHub emails a failed scheduled run). The gap left: a paused or deleted
# Scheduler entry, which only the red Tuesday run shows.
