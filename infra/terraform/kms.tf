# kms.tf — Regional Customer-Managed Encryption Keys (CMEK), in-country.
#
# General Principle map:
#   P-09 (CMEK does NOT cascade): a CMEK on one resource does not automatically protect
#         data that resource hands to another service. Each managed service (Document AI
#         outputs, Agent Runtime, Logging, the Storage buckets) must be told to use this
#         key explicitly. We keep ONE regional key ring + crypto key here and wire it into
#         every resource that supports CMEK in its own file.
#   P-03 (residency): the key ring location is var.region (regional key, never the
#         global/multi-region key). Regional CMEK pins crypto material in-country.
#
# NOTE: key rings are indestructible. A `terraform destroy` cannot remove the ring, so a
# redeploy into the same project must either import it or use a fresh var.name_prefix
# (naming.tf derives the ring/key names from it).

resource "google_kms_key_ring" "cdd" {
  name     = local.kms_ring_name
  location = var.region # regional, in-country key material (P-03)

  depends_on = [google_project_service.required]
}

resource "google_kms_crypto_key" "cdd" {
  name     = local.kms_key_name
  key_ring = google_kms_key_ring.cdd.id

  purpose         = "ENCRYPT_DECRYPT"
  rotation_period = "7776000s" # 90 days — periodic rotation for key hygiene

  version_template {
    algorithm        = "GOOGLE_SYMMETRIC_ENCRYPTION"
    protection_level = "SOFTWARE"
  }

  lifecycle {
    # A destroyed key is unrecoverable and would strand all CMEK-encrypted data.
    prevent_destroy = true
  }
}

# Dedicated asymmetric Mode 5 signing key. This key cannot encrypt data and its private
# material cannot be exported. Rotation creates a new accepted `kid`; emergency revocation
# disables the affected version after the token lifetime and accepted-key window are cleared.
resource "google_kms_crypto_key" "embed_signing" {
  count                         = var.enable_embed_signing_key ? 1 : 0
  name                          = "${local.kms_key_name}-embed-signing"
  key_ring                      = google_kms_key_ring.cdd.id
  purpose                       = "ASYMMETRIC_SIGN"
  skip_initial_version_creation = true

  version_template {
    algorithm        = "RSA_SIGN_PKCS1_2048_SHA256"
    protection_level = var.embed_signing_protection_level
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_kms_crypto_key_version" "embed_signing" {
  count      = var.enable_embed_signing_key ? 1 : 0
  crypto_key = google_kms_crypto_key.embed_signing[0].id
  state      = "ENABLED"

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_kms_crypto_key_iam_member" "embed_signer" {
  count         = var.enable_embed_signing_key ? 1 : 0
  crypto_key_id = google_kms_crypto_key.embed_signing[0].id
  role          = "roles/cloudkms.signerVerifier"
  member        = "serviceAccount:${google_service_account.app.email}"
}

# --------------------------------------------------------------------------- #
# Grant each service agent the right to use the key. CMEK does not cascade
# (P-09): every service that encrypts with this key needs its OWN binding here.
# --------------------------------------------------------------------------- #
data "google_project" "this" {
  project_id = var.project_id
}

# Document AI service agent.
resource "google_kms_crypto_key_iam_member" "documentai" {
  crypto_key_id = google_kms_crypto_key.cdd.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-prod-dai-core.iam.gserviceaccount.com"
}

# Vertex AI / Agent Runtime service agent.
resource "google_kms_crypto_key_iam_member" "aiplatform" {
  crypto_key_id = google_kms_crypto_key.cdd.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-aiplatform.iam.gserviceaccount.com"
}

# Cloud Logging service agent (CMEK on the WORM bucket).
resource "google_kms_crypto_key_iam_member" "logging" {
  crypto_key_id = google_kms_crypto_key.cdd.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-logging.iam.gserviceaccount.com"
}
