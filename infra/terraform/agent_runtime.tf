# agent_runtime.tf — placeholder for the Agent Runtime (reasoningEngine) deploy.
#
# General Principle map:
#   P-01 (managed-first): the ADK agent is hosted on Agent Runtime (ex-Agent Engine), a
#         reasoningEngine resource, rather than self-managed infra.
#   P-03 (residency): the reasoningEngine is created in us-central1.
#
# The reasoningEngine itself is created by the Agent Platform SDK at deploy time (see
# src/cdd_sow_research/agent/root_agent.py docstring), not by Terraform, because the build
# artifact is a packaged Python agent. This file reserves the staging bucket and records
# the runtime identity so settings.agent_engine.resource_name can be set after deploy.
# verify: https://docs.cloud.google.com/vertex-ai/generative-ai/docs/agent-engine/deploy

resource "google_storage_bucket" "agent_staging" {
  name                        = local.staging_bucket_name
  project                     = var.project_id
  location                    = var.region # in-country staging (P-03)
  uniform_bucket_level_access = true
  force_destroy               = false

  encryption {
    default_kms_key_name = google_kms_crypto_key.cdd.id # CMEK (P-09)
  }

  depends_on = [
    google_project_service.required,
    google_kms_crypto_key_iam_member.aiplatform,
  ]
}
