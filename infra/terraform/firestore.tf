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

# The Firestore service agent must be able to use the regional CMEK key.
resource "google_kms_crypto_key_iam_member" "firestore" {
  crypto_key_id = google_kms_crypto_key.cdd.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-firestore.iam.gserviceaccount.com"
}

resource "google_firestore_database" "sow_cases" {
  project     = var.project_id
  name        = var.case_database
  location_id = var.region # us-central1 (P-03) : case documents stay in-country
  type        = "FIRESTORE_NATIVE"

  # Regional CMEK end to end (P-09), delete protection on (a case store is model-risk
  # evidence), and point-in-time recovery for the audit window.
  cmek_config {
    kms_key_name = google_kms_crypto_key.cdd.id
  }
  delete_protection_state           = "DELETE_PROTECTION_ENABLED"
  point_in_time_recovery_enablement = "POINT_IN_TIME_RECOVERY_ENABLED"

  depends_on = [
    google_project_service.firestore,
    google_kms_crypto_key_iam_member.firestore,
  ]
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
