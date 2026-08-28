# apis.tf — Enable exactly the managed services B1 depends on.
#
# General Principle map:
#   P-01 (managed-first / minimal surface): only the services the pinned stack (SPEC §3)
#         actually uses are enabled — nothing speculative.
#   P-03 (residency): enabling these APIs is a prerequisite for the regional, CMEK
#         protected resources defined in the sibling files.
#
# Note: retrieval/storage infra (Agent Search data stores) lives in A2, not here. B1
# enables Document AI (extraction), DLP (redaction), and the audit/trace/eval surface.
#
# disable_on_destroy = false so a `terraform destroy` of this stack does not yank platform
# APIs out from under other workloads in a shared project.

locals {
  required_services = [
    "aiplatform.googleapis.com",           # Gemini Enterprise Agent Platform / Agent Runtime / evals
    "documentai.googleapis.com",           # Document AI (KYC document extraction)
    "discoveryengine.googleapis.com",      # Agent Search (the A2 governed RAG store backend)
    "dlp.googleapis.com",                  # Sensitive Data Protection / DLP (PII redaction, R1)
    "modelarmor.googleapis.com",           # Model Armor guardrail (R1)
    "logging.googleapis.com",              # Cloud Logging (WORM locked bucket + audit, R2)
    "monitoring.googleapis.com",           # Cloud Monitoring (log-based metrics + security alerts)
    "cloudtrace.googleapis.com",           # Cloud Trace (OpenTelemetry spans)
    "run.googleapis.com",                  # Cloud Run job / app host
    "secretmanager.googleapis.com",        # App secrets (no secrets in code, P-04)
    "cloudkms.googleapis.com",             # Regional CMEK key ring (P-09)
    "accesscontextmanager.googleapis.com", # VPC Service Controls perimeter (P-03)
    "artifactregistry.googleapis.com",     # Promoted image registry (artifact_registry.tf)
    "cloudscheduler.googleapis.com",       # Sanctions snapshot refresh schedule (sanctions_sync.tf)
    # Supporting services the above transitively require.
    "compute.googleapis.com",   # VPC
    "iam.googleapis.com",       # Service accounts / least-privilege IAM
    "orgpolicy.googleapis.com", # Org Policy residency constraints (P-03)
    "storage.googleapis.com",   # GCS: case documents, sanctions snapshot, agent staging buckets
  ]
}

resource "google_project_service" "required" {
  for_each = toset(local.required_services)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}
