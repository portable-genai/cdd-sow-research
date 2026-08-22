# naming.tf — Every resource name, derived from var.name_prefix in one place.
#
# With the default prefix ("cdd-sow") these locals equal the historical hard-coded names,
# so an existing deployment keeps its resources (exceptions, renamed once: the sanctions
# SAs/job/scheduler and the VPC-SC perimeter/access-level, see README "Renames").
# A non-default prefix lets a second instance coexist in the same project and lets a
# destroy + redeploy sidestep the indestructible KMS key ring from a previous stack.
#
# Length note: GCP service-account ids must be 6-30 chars. The longest derived id is
# <prefix>-sanctions-sched (prefix + 16 chars) and the always-created <prefix>-sanctions-sync
# is prefix + 15, so keep the prefix at 14 chars or fewer when the sanctions job is enabled
# (15 or fewer without it). The name_prefix regex allows up to 19 chars for the other
# resource kinds; over-long SA ids fail at plan/apply on the SA resource.

locals {
  prefix   = var.name_prefix
  prefix_u = replace(var.name_prefix, "-", "_")

  # KMS (kms.tf). Key rings are indestructible: a new prefix means a new ring.
  kms_ring_name = "${var.name_prefix}-agent-ring" # was cdd-sow-agent-ring
  kms_key_name  = "${var.name_prefix}-agent-cmek" # was cdd-sow-agent-cmek

  # WORM audit trail (logging_worm.tf, monitoring.tf).
  worm_bucket_id  = "${var.name_prefix}-agent-worm"  # was cdd-sow-agent-worm (app: CDD_LOG_BUCKET)
  audit_log_name  = "${var.name_prefix}-agent-audit" # was cdd-sow-research-audit (app: CDD_LOG_NAME)
  audit_sink_name = "${var.name_prefix}-audit-to-worm"

  # Service accounts (iam.tf, sanctions_sync.tf).
  app_sa_id     = "${var.name_prefix}-app"     # was cdd-sow-app
  runtime_sa_id = "${var.name_prefix}-runtime" # was cdd-sow-runtime

  # Guardrail + extraction + redaction templates (model_armor.tf, document_ai.tf, dlp.tf).
  guardrail_template_id  = "${var.name_prefix}-guardrail" # was cdd-sow-guardrail (app: CDD_MODEL_ARMOR_TEMPLATE)
  docai_display          = "${var.name_prefix}-kyc-extractor"
  dlp_inspect_display    = "${var.name_prefix}-inspect"
  dlp_deidentify_display = "${var.name_prefix}-deidentify"

  # Buckets (agent_runtime.tf, sanctions_sync.tf). Project-qualified: bucket names are global.
  staging_bucket_name   = "${var.project_id}-${var.name_prefix}-agent-staging"
  sanctions_bucket_name = "${var.project_id}-${var.name_prefix}-agent-sanctions"

  # Sanctions sync job (sanctions_sync.tf).
  sanctions_sync_sa_id    = "${var.name_prefix}-sanctions-sync"  # renamed from cdd-sanctions-sync
  sanctions_sched_sa_id   = "${var.name_prefix}-sanctions-sched" # renamed from cdd-sanctions-sched
  sanctions_job_name      = "${var.name_prefix}-sanctions-sync"
  sanctions_schedule_name = "${var.name_prefix}-sanctions-sync-daily"

  # VPC-SC (vpc_sc.tf): perimeter/access-level names allow letters/digits/underscores only.
  perimeter_name    = "${local.prefix_u}_perimeter" # renamed from cdd_sow_sg (stale _sg suffix dropped)
  access_level_name = "${local.prefix_u}_operators" # renamed from cdd_operators

  # Log-based metrics (monitoring.tf) were cdd_sow_<key>.
  metric_prefix = local.prefix_u
}
