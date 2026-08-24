# monitoring.tf — Security alerting: log-based metrics + alert policies.
#
# General Principle map:
#   P-07 / P-08 (detect, not just record): DATA_READ logging (logging_worm.tf) records
#         reads, but recording is not detection. These log-based metrics + alert policies
#         SURFACE security-relevant events so an operator is notified, rather than the signal
#         sitting unread in the WORM bucket.
#
# Signals covered:
#   - guardrail_blocks : a guardrail BLOCKED decision in the app audit log (R1 working).
#   - sa_key_creation  : an exportable SA key was created (org policy should forbid it; P-06).
#   - vpc_sc_denials   : a VPC Service Controls violation (perimeter working / probing).
#   - cmek_changes     : a CMEK key destroy/update (P-09 key material change).
#
# Alert policies are always created; var.alert_notification_channels attaches channels (an
# empty list still creates the policy, just with nowhere to notify — wire a channel in prod).
#
# verify: https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/logging_metric
# verify: https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/monitoring_alert_policy

locals {
  # Any monitored resource a security signal could plausibly arrive on. See the long note on
  # the alert filter below for why this is a generous union and not one exact type per metric.
  alert_resource_types = "resource.type=one_of(\"audited_resource\",\"global\",\"project\",\"cloud_run_revision\",\"cloud_run_job\",\"service_account\",\"cloudkms_cryptokey\",\"cloudkms_keyring\",\"gce_instance\",\"k8s_container\",\"generic_task\",\"generic_node\",\"gcs_bucket\")"

  security_metrics = {
    guardrail_blocks = {
      description = "Guardrail BLOCKED decision in the app audit log"
      filter      = "logName=\"projects/${var.project_id}/logs/${local.audit_log_name}\" AND jsonPayload.decision=\"blocked\""
    }
    sa_key_creation = {
      description = "Service-account key created (org policy should forbid this)"
      filter      = "protoPayload.methodName=\"google.iam.admin.v1.CreateServiceAccountKey\""
    }
    vpc_sc_denials = {
      description = "VPC Service Controls violation"
      filter      = "protoPayload.metadata.@type=\"type.googleapis.com/google.cloud.audit.VpcServiceControlAuditMetadata\""
    }
    cmek_changes = {
      description = "CMEK key destroy/update operation"
      filter      = "protoPayload.serviceName=\"cloudkms.googleapis.com\" AND (protoPayload.methodName:\"DestroyCryptoKeyVersion\" OR protoPayload.methodName:\"UpdateCryptoKey\")"
    }
  }
}

resource "google_logging_metric" "security" {
  for_each = local.security_metrics

  project     = var.project_id
  name        = "${local.metric_prefix}_${each.key}"
  description = each.value.description
  filter      = each.value.filter

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }

  depends_on = [google_project_service.required]
}

resource "google_monitoring_alert_policy" "security" {
  for_each = local.security_metrics

  project      = var.project_id
  display_name = "${var.name_prefix} security: ${each.key}"
  combiner     = "OR"

  conditions {
    display_name = each.value.description

    condition_threshold {
      # Cloud Monitoring REJECTS an alert filter with no resource.type restriction
      # (400: "must specify a restriction on resource.type"), so one is required here.
      # Only equality, one_of() and starts_with() are accepted: "!=", regex, and an empty
      # starts_with("") argument are all rejected by the API, so one_of is the only form
      # that can express "any of these".
      #
      # The list is deliberately a GENEROUS UNION rather than one exact type per metric.
      # These four signals arrive on different monitored resources — the app's own audit log
      # on cloud_run_revision (or global off Cloud Run), CreateServiceAccountKey on
      # service_account, VPC-SC denials on audited_resource, KMS changes on
      # cloudkms_cryptokey — and a log-based metric inherits the resource of the entry that
      # produced it. That resource type cannot be discovered before the fact: a user
      # log-based metric has no Monitoring descriptor until a matching entry first appears.
      #
      # Naming one type per metric would therefore be a guess that READS as precision and
      # silently matches nothing the day a signal lands on a resource we guessed wrong. A
      # security alert that never fires is worse than a broad one and is invisible, because
      # a healthy system and a dead alert look identical. The metric's own log filter above
      # already scopes what gets counted; this restriction exists only to satisfy the API,
      # so it must not narrow anything. Add to the list rather than trimming it.
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.security[each.key].name}\" AND ${local.alert_resource_types}"
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = var.alert_notification_channels

  documentation {
    content   = "Security signal '${each.key}' fired for the B1 CDD/SoW agent. Investigate the matching entries in Cloud Logging and the WORM audit bucket."
    mime_type = "text/markdown"
  }

  depends_on = [google_project_service.required]
}
