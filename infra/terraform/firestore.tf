# firestore.tf : managed SoW case store (Firestore Native), CMEK, regional, delete-protected.
#
# Backs the CaseStorePort gcp/platform adapter (adapters/gcp/firestore_case_store.py). The
# case store is vertical-owned, so this is provisioned in every deployment (NOT gated by
# the `standalone` flag). A project holds only one immutable `(default)` database whose
# mode and location are fixed at creation, so this provisions a NAMED database to avoid a
# clash with a project that may already have a `(default)` one; the app selects it via
# CDD_FIRESTORE_DB (see config/settings.yaml `case_store.database`).

variable "case_database" {
  type        = string
  description = "Firestore database id for the SoW case store (a NAMED db, not the immutable (default))."
  default     = "sow-cases"
}

resource "google_project_service" "firestore" {
  project            = var.project_id
  service            = "firestore.googleapis.com"
  disable_on_destroy = false
}

# The Firestore service agent must be able to use the regional CMEK key. Asked for, not
# spelled out — see the note above the service-agent grants in kms.tf.
resource "google_project_service_identity" "firestore" {
  provider = google-beta
  project  = var.project_id
  service  = "firestore.googleapis.com"

  depends_on = [google_project_service.firestore]
}

resource "google_kms_crypto_key_iam_member" "firestore" {
  crypto_key_id = google_kms_crypto_key.cdd.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_project_service_identity.firestore.email}"
}

resource "google_firestore_database" "sow_cases" {
  project     = var.project_id
  name        = var.case_database
  location_id = var.region # us-central1 (P-03) : case documents stay in-country
  type        = "FIRESTORE_NATIVE"

  # Regional CMEK end to end (P-09), delete protection on (a case store is model-risk
  # evidence), and point-in-time recovery for the audit window.
  #
  # CMEK on Firestore is ALLOWLIST-GATED by Google. Creating a CMEK database in a project
  # without that entitlement fails with a 429 QuotaFailure pointing at a request form, which
  # is an external dependency no code change can satisfy — discovered on the first real apply,
  # 2026-08-24. The toggle exists so a project awaiting the allowlist can still stand the rest
  # of the stack up; it defaults to ON, so a production deploy that has the entitlement is
  # unaffected and a deploy that does not must switch it off DELIBERATELY and disclose it.
  # Every other CMEK binding (logging, storage, Document AI, Vertex) is unconditional.
  dynamic "cmek_config" {
    for_each = var.firestore_cmek_enabled ? [1] : []
    content {
      kms_key_name = google_kms_crypto_key.cdd.id
    }
  }
  delete_protection_state = (
    var.firestore_delete_protection_enabled
    ? "DELETE_PROTECTION_ENABLED"
    : "DELETE_PROTECTION_DISABLED"
  )
  point_in_time_recovery_enablement = (
    var.firestore_pitr_enabled
    ? "POINT_IN_TIME_RECOVERY_ENABLED"
    : "POINT_IN_TIME_RECOVERY_DISABLED"
  )

  depends_on = [
    google_project_service.firestore,
    google_kms_crypto_key_iam_member.firestore,
  ]

  lifecycle {
    precondition {
      condition     = var.firestore_cmek_enabled || !var.worm_locked
      error_message = "A locked (production) stack must keep Firestore CMEK enabled: the case store holds the same customer material as the audit trail, so exempting it while claiming an immutable audit posture would be incoherent."
    }
    # Same shape, same reason: these are declinable because a reference stack is
    # deliberately destroyable, and that argument evaporates the moment the stack
    # locks its trail. A deployment that has committed to keeping evidence for
    # seven years may not also be one whose case store can be dropped by an apply.
    precondition {
      condition     = var.firestore_delete_protection_enabled || !var.worm_locked
      error_message = "A locked (production) stack must keep Firestore delete protection enabled: a stack that cannot delete its audit trail must not be able to delete the cases that trail is about."
    }
    precondition {
      condition     = var.firestore_pitr_enabled || !var.worm_locked
      error_message = "A locked (production) stack must keep Firestore point-in-time recovery enabled: seven-year evidence retention over a case store with no recovery window is a guarantee about the trail alone."
    }
  }
}

# The app service account reads/writes case documents (least privilege: no admin).
resource "google_project_iam_member" "app_datastore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.app.email}"
}

output "case_store_database" {
  description = "Firestore database id the app sets as CDD_FIRESTORE_DB (case_store.database)."
  value       = google_firestore_database.sow_cases.name
}

# Firestore TTL is asynchronous cleanup, not the authorization boundary. Every adapter
# operation compares expiry inside its transaction. TTL bounds retained short-lived state.
resource "google_firestore_field" "browser_flow_ttl" {
  project    = var.project_id
  database   = google_firestore_database.sow_cases.name
  collection = var.browser_flow_records_collection
  field      = "expires_at"

  ttl_config {}
}

resource "google_firestore_field" "browser_flow_alias_ttl" {
  project    = var.project_id
  database   = google_firestore_database.sow_cases.name
  collection = var.browser_flow_aliases_collection
  field      = "expires_at"

  ttl_config {}
}

resource "google_firestore_field" "client_assertion_replay_ttl" {
  project    = var.project_id
  database   = google_firestore_database.sow_cases.name
  collection = var.browser_flow_replay_collection
  field      = "expires_at"

  ttl_config {}
}

resource "google_firestore_field" "embed_rate_limit_ttl" {
  project    = var.project_id
  database   = google_firestore_database.sow_cases.name
  collection = var.embed_rate_limits_collection
  field      = "expires_at"

  ttl_config {}
}

# Required by Firestore for the pending-outbox query:
# delivered_at == null, ordered deterministically by occurred_at then event_id.
resource "google_firestore_index" "browser_flow_pending_outbox" {
  project     = var.project_id
  database    = google_firestore_database.sow_cases.name
  collection  = var.browser_flow_outbox_collection
  query_scope = "COLLECTION"

  fields {
    field_path = "delivered_at"
    order      = "ASCENDING"
  }
  fields {
    field_path = "occurred_at"
    order      = "ASCENDING"
  }
  fields {
    field_path = "event_id"
    order      = "ASCENDING"
  }
}
