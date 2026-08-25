# iam.tf — Least-privilege, case-scoped service account for the B1 agent.
#
# General Principle map:
#   P-06 (least privilege / separation of duties): a single serving identity that gets
#         only the roles it needs (extract, ingest to A2, call models + guardrail + DLP,
#         write audit + traces). No shared "kitchen-sink" SA.
#   P-03 (residency): the identity is project-scoped; data access is to in-region services.
#   P-09 (CMEK explicit): the SA that touches CMEK-encrypted data gets its own cryptoKey
#         use binding.
#   R3 (case-scoped ACL): case documents are ingested into A2 with case:<subject_id> ACL
#         tags; the app reads only case-scoped principals. ACL enforcement lives in A2, but
#         the agent SA carries only the discoveryengine viewer/editor roles it needs.

resource "google_service_account" "app" {
  account_id   = local.app_sa_id
  display_name = "B1 CDD + Source-of-Wealth Agent (serving / API)"
  project      = var.project_id

  depends_on = [google_project_service.required]
}

locals {
  # Serving path: extract KYC docs (Document AI), ingest + query the A2 governed RAG store,
  # call models + DLP, write audit + traces, run evals, read secrets. No org-wide writes.
  app_roles = [
    "roles/aiplatform.user",        # Gemini reasoning + Gen AI evals
    "roles/documentai.apiUser",     # process KYC documents
    "roles/discoveryengine.editor", # ingest case docs into A2 (case-scoped ACL)
    "roles/dlp.user",               # deidentifyContent (P-04, R1)
    # sanitizeUserPrompt / sanitizeModelResponse against the guardrail template. Never needed
    # before because the gcp guardrail binding could not even import its SDK, so the whole
    # adapter was unreachable and its missing role invisible.
    "roles/modelarmor.user",
    "roles/logging.logWriter", # write redacted audit events to WORM sink (R2)
    "roles/cloudtrace.agent",  # OpenTelemetry spans (content OFF)
    "roles/secretmanager.secretAccessor",
    "roles/run.invoker",
    # Calls to DLP are billed against this project and refused with
    # "Permission 'serviceusage.services.use' denied on //dlp.googleapis.com" without it. It
    # reads like a quota-project problem and is a missing role.
    "roles/serviceusage.serviceUsageConsumer",
  ]
}

resource "google_project_iam_member" "app" {
  for_each = toset(local.app_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.app.email}"
}

# The SAME role set for any additional serving identity, so a second one is not a second
# security decision.
#
# An embedding host runs this app under a runtime identity of the host's making, which this
# stack has never heard of. Every grant above therefore misses it, and the app fails one API at
# a time in the order it happens to call them: first Cloud Storage on the document upload, then
# Cloud Trace on the first traced request, then DLP on the first redaction. Each failure reads
# as a different problem; all three are the same missing identity.
#
# Granting the identical list, rather than a hand-picked subset, is deliberate: the app needs
# what the app needs, and a subset assembled by whoever hit the first 403 is how an identity
# ends up with exactly the permissions the last outage required.
resource "google_project_iam_member" "additional_serving" {
  for_each = {
    for pair in setproduct(var.additional_serving_service_accounts, local.app_roles) :
    "${pair[0]}|${pair[1]}" => { member = pair[0], role = pair[1] }
  }
  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${each.value.member}"
}

resource "google_kms_crypto_key_iam_member" "additional_serving" {
  for_each      = toset(var.additional_serving_service_accounts)
  crypto_key_id = google_kms_crypto_key.cdd.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${each.value}"
}

# App uses the CMEK for envelope ops it performs directly.
resource "google_kms_crypto_key_iam_member" "app" {
  crypto_key_id = google_kms_crypto_key.cdd.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_service_account.app.email}"
}

# ------------------------- Agent Runtime identity --------------------------- #
resource "google_service_account" "agent_runtime" {
  account_id   = local.runtime_sa_id
  display_name = "B1 Agent Runtime (reasoningEngine) identity"
  project      = var.project_id

  depends_on = [google_project_service.required]
}

resource "google_project_iam_member" "agent_runtime" {
  for_each = toset(["roles/aiplatform.user", "roles/logging.logWriter", "roles/cloudtrace.agent"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.agent_runtime.email}"
}
