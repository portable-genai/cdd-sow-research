# document_ai.tf -- Document AI processor for KYC document extraction.
#
# General Principle map:
#   P-03 (residency): PARTIAL, and stated rather than absorbed. The processor is created at
#         var.docai_location, which defaults to the `us` MULTI-REGION -- so KYC document
#         bytes are extracted in the United States while the rest of the stack stays in
#         region. Document AI serves asia-southeast1 only once Google grants single-region
#         access; set var.docai_location (and the runtime's CDD_DOCAI_LOCATION) to
#         asia-southeast1 the day it lands, and in-country extraction follows.
#   P-01 (managed-first): a single managed Document AI processor replaces a bespoke
#         document-parsing service; the DocumentExtractionPort binds to it.
#   P-09 (CMEK explicit): the processor is bound to the regional CMEK key so document
#         bytes are customer-key-encrypted end to end, not just under a Google-managed key.
#         CMEK does not cascade, so this binding is explicit (and the Document AI service
#         agent's key permission is granted in kms.tf).
#
# verify: https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/document_ai_processor

# CMEK must be reachable from the processor's own location. When the processor is out of
# region (the default: `us` multi-region), the stack key cannot bind to it — the API
# answers NOT_FOUND for a key in another location, observed 2026-08-29 on the
# asia-southeast1 deployment. What the API actually accepts, proved by the prior live
# deployment and re-proved here, is a key in a REGION inside the processor's multi-region
# (us-central1 for `us`); a key created in the `us` multi-region KMS location itself is
# refused with the same NOT_FOUND. So the dedicated ring lives at var.docai_kms_location,
# and the CMEK claim holds without pretending the extraction is in region (P-03 already
# discloses that it is not).
locals {
  docai_out_of_region = var.docai_location != var.region
}

resource "google_kms_key_ring" "docai" {
  count    = local.docai_out_of_region ? 1 : 0
  project  = var.project_id
  name     = "${var.name_prefix}-docai-ring"
  location = var.docai_kms_location

  depends_on = [google_project_service.required]
}

resource "google_kms_crypto_key" "docai" {
  count           = local.docai_out_of_region ? 1 : 0
  name            = "${var.name_prefix}-docai-cmek"
  key_ring        = google_kms_key_ring.docai[0].id
  rotation_period = "7776000s" # 90 days, matching the stack key in kms.tf

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_kms_crypto_key_iam_member" "documentai_colocated" {
  count         = local.docai_out_of_region ? 1 : 0
  crypto_key_id = google_kms_crypto_key.docai[0].id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_project_service_identity.documentai.email}"
}

resource "google_document_ai_processor" "kyc" {
  project      = var.project_id
  location     = var.docai_location # NOT var.region: Document AI serves neither every region nor, yet, ours in-country
  display_name = local.docai_display

  # A form parser extracts the key/value fields and full text from KYC documents
  # (passports, financial statements, registry extracts, bank statements).
  type = "FORM_PARSER_PROCESSOR"

  # Explicit CMEK on the processor (P-09 -- does not cascade), from the key that shares the
  # processor's location.
  kms_key_name = local.docai_out_of_region ? google_kms_crypto_key.docai[0].id : google_kms_crypto_key.cdd.id

  depends_on = [
    google_project_service.required,
    google_kms_crypto_key_iam_member.documentai,
    google_kms_crypto_key_iam_member.documentai_colocated,
  ]
}
