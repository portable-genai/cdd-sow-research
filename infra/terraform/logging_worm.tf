# logging_worm.tf — WORM audit trail: locked Cloud Logging bucket + sink + audit config.
#
# General Principle map:
#   P-08 (immutable audit / WORM, rule R2): the audit log is routed to a Cloud Logging
#         bucket whose retention is var.retention_days (180 days by default) and whose lock
#         (var.worm_locked) makes it Write-Once-Read-Many. The audit adapter
#         (cloud_logging_audit) writes already-redacted AuditEvents here.
#   P-03 (residency): bucket location is var.region.
#   P-09 (CMEK explicit): the bucket is CMEK-encrypted (logging SA key binding in kms.tf).
#   P-04 (no raw PII in logs): only redacted prompts/responses are written (enforced in the
#         app); DATA_READ audit logging is enabled to record every read.
#
# ############################################################################ #
# # WARNING: LOCKING IS IRREVERSIBLE.                                         # #
# # The lock is variable-controlled (var.worm_locked, DEFAULT TRUE). Locking  # #
# # permanently prevents reducing retention or deleting this bucket for the   # #
# # full retention window. You CANNOT undo it, not even with project-owner    # #
# # rights. Confirm retention_days before apply. worm_locked = true is        # #
# # REQUIRED for a compliant production deploy; set worm_locked = false only  # #
# # for evaluation/demo stacks that must stay deletable (NOT compliant).      # #
# ############################################################################ #

resource "google_logging_project_bucket_config" "worm_audit" {
  count          = var.standalone ? 1 : 0
  project        = var.project_id
  location       = var.region # selected, allowlisted region (P-03)
  bucket_id      = local.worm_bucket_id
  description    = "WORM audit bucket for the Doc1 CDD + Source-of-Wealth Agent (six-month default retention)."
  retention_days = var.retention_days # 180 days by default

  # IRREVERSIBLE when true (the default): see WARNING banner above.
  locked = var.worm_locked

  # CMEK on the log bucket (P-09) — explicit, does not cascade.
  cmek_settings {
    kms_key_name = google_kms_crypto_key.cdd.id
  }

  depends_on = [
    google_project_service.required,
    google_kms_crypto_key_iam_member.logging,
  ]
}

# Route the audit log stream into the locked WORM bucket.
resource "google_logging_project_sink" "audit_to_worm" {
  count       = var.standalone ? 1 : 0
  project     = var.project_id
  name        = local.audit_sink_name
  description = "Routes the ${local.audit_log_name} log to the WORM bucket."

  destination = "logging.googleapis.com/${google_logging_project_bucket_config.worm_audit[0].id}"

  # Capture this app's audit log + all Cloud Audit Logs (admin/data access).
  # The app writes to local.audit_log_name (exported as output audit_log_name; the app
  # reads it via CDD_LOG_NAME), so the sink filter must use the same derived name.
  filter = <<-EOT
    logName="projects/${var.project_id}/logs/${local.audit_log_name}"
    OR logName:"cloudaudit.googleapis.com"
  EOT

  unique_writer_identity = true
}

# --------------------------------------------------------------------------- #
# Enable Data Access audit logs (DATA_READ) so every read of the case evidence,
# the dossier and the audit store itself is itself audited (P-08). ADMIN_READ and
# DATA_WRITE are on by default; we add DATA_READ explicitly.
# --------------------------------------------------------------------------- #
resource "google_project_iam_audit_config" "data_access" {
  project = var.project_id
  service = "allServices"

  audit_log_config {
    log_type = "DATA_READ"
  }
  audit_log_config {
    log_type = "DATA_WRITE"
  }
  audit_log_config {
    log_type = "ADMIN_READ"
  }
}
