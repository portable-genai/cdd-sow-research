# document_ai.tf — Document AI processor for KYC document extraction.
#
# General Principle map:
#   P-03 (residency): the processor is created in var.region so KYC document bytes are
#         processed in-country (the selected, allowlisted region).
#   P-01 (managed-first): a single managed Document AI processor replaces a bespoke
#         document-parsing service; the DocumentExtractionPort binds to it.
#   P-09 (CMEK explicit): the processor is bound to the regional CMEK key so document
#         bytes are customer-key-encrypted end to end, not just under a Google-managed key.
#         CMEK does not cascade, so this binding is explicit (and the Document AI service
#         agent's key permission is granted in kms.tf).
#
# verify: https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/document_ai_processor

resource "google_document_ai_processor" "kyc" {
  project      = var.project_id
  location     = var.region # selected, allowlisted region — in-country processing (P-03)
  display_name = local.docai_display

  # A form parser extracts the key/value fields and full text from KYC documents
  # (passports, financial statements, registry extracts, bank statements).
  type = "FORM_PARSER_PROCESSOR"

  # Explicit CMEK on the processor (P-09 — does not cascade).
  kms_key_name = google_kms_crypto_key.cdd.id

  depends_on = [
    google_project_service.required,
    google_kms_crypto_key_iam_member.documentai,
  ]
}
