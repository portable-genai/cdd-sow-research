# outputs.tf — Values the app/operators need to wire settings.yaml after apply.
#
# These map 1:1 onto config/settings.yaml / config.py fields so a deploy is just
# "apply, then export these into the runtime environment". Each description names the
# CDD_* environment variable the app reads (settings.yaml interpolates ${CDD_*} tokens).

output "project_id" {
  description = "The deployment project id."
  value       = var.project_id
}

output "region" {
  description = "The selected deploy region (validated against var.allowed_regions). App env: CDD_REGION."
  value       = var.region
}

# ------------------------------ Document AI --------------------------------- #
output "documentai_processor_id" {
  description = "Document AI processor id (settings.yaml document_ai.processor_id). App env: CDD_DOCAI_PROCESSOR."
  value       = google_document_ai_processor.kyc.id
}

output "docai_location" {
  description = "Where the processor was created: var.docai_location, the deploy region or a named multi-region (a stated residency deviation until single-region access lands)."
  value       = google_document_ai_processor.kyc.location
}

# --------------------------------- KMS -------------------------------------- #
output "kms_key" {
  description = "Regional CMEK crypto key id (settings.yaml kms_key). App env: CDD_KMS_KEY."
  value       = google_kms_crypto_key.cdd.id
}

output "embed_signing_key_version" {
  description = "Non-exportable Mode 5 signing key version when enable_embed_signing_key is true; otherwise null."
  value       = one(google_kms_crypto_key_version.embed_signing[*].name)
}

# ------------------------------- WORM logging ------------------------------- #
output "log_bucket" {
  description = "WORM audit log bucket id (settings.yaml logging.bucket; locked when worm_locked = true). App env: CDD_LOG_BUCKET."
  value       = one(google_logging_project_bucket_config.worm_audit[*].id)
}

output "audit_log_name" {
  description = "Audit log name the app writes to (settings.yaml logging.log_name); the WORM sink routes it. App env: CDD_LOG_NAME."
  value       = local.audit_log_name
}

output "worm_locked" {
  description = "Whether the audit bucket is irreversibly locked for the retention window (true is the compliant production posture)."
  value       = var.worm_locked
}

output "audit_sink_writer_identity" {
  description = "Sink writer identity (grant it bucket access if cross-project)."
  value       = one(google_logging_project_sink.audit_to_worm[*].writer_identity)
}

# ------------------------- Model Armor / DLP -------------------------------- #
output "model_armor_template" {
  description = "Model Armor template id (settings.yaml model_armor.template_id). App env: CDD_MODEL_ARMOR_TEMPLATE."
  value       = one(google_model_armor_template.cdd_guardrail[*].template_id)
}

output "dlp_inspect_template" {
  description = "DLP inspect template (settings.yaml dlp.inspect_template). App env: CDD_DLP_INSPECT_TEMPLATE."
  value       = one(google_data_loss_prevention_inspect_template.cdd[*].id)
}

output "dlp_deidentify_template" {
  description = "DLP deidentify template (settings.yaml dlp.deidentify_template). App env: CDD_DLP_DEIDENTIFY_TEMPLATE."
  value       = one(google_data_loss_prevention_deidentify_template.cdd[*].id)
}

# ----------------------------- Service accounts ----------------------------- #
output "app_service_account" {
  description = "Serving/API service account email."
  value       = google_service_account.app.email
}

output "agent_runtime_service_account" {
  description = "Agent Runtime (reasoningEngine) service account email."
  value       = google_service_account.agent_runtime.email
}

output "agent_staging_bucket" {
  description = "Agent Runtime staging bucket for the SDK-based deploy."
  value       = google_storage_bucket.agent_staging.name
}

output "sanctions_bucket" {
  description = "Regional CMEK bucket holding the synced sanctions/PEP watchlist snapshot. App env: CDD_SANCTIONS_BUCKET."
  value       = google_storage_bucket.sanctions.name
}
