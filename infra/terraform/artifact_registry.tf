# artifact_registry.tf — The registry this stack's own images are promoted into.
#
# General Principle map:
#   P-02 (no lock-in): Terraform is the only place infrastructure is described. The registry
#         was originally created by hand during the 2026-08-24 reference deployment and is
#         declared here instead, so the stack has no resource that exists only because
#         somebody once ran a gcloud command.
#   P-03 (residency): regional, pinned to var.region like every other resource.
#   P-09 (CMEK explicit): image layers are customer material — a container image carries the
#         application and its configuration — so the repository is bound to the same key as
#         the rest of the stack. CMEK does not cascade, hence the explicit grant below.

# The Artifact Registry service agent does not exist until it is asked for, and a CMEK
# repository cannot be created before it holds the key grant. Creating the identity is what
# makes the ordering explicit rather than a race: gcloud fails this outright with "The
# Artifact Registry service account might not exist" (observed 2026-08-24).
resource "google_project_service_identity" "artifactregistry" {
  provider = google-beta
  project  = var.project_id
  service  = "artifactregistry.googleapis.com"

  depends_on = [google_project_service.required]
}

resource "google_kms_crypto_key_iam_member" "artifactregistry" {
  crypto_key_id = google_kms_crypto_key.cdd.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_project_service_identity.artifactregistry.email}"
}

resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = var.name_prefix
  description   = "Promoted ${var.name_prefix} API and UI images, CMEK-encrypted."
  format        = "DOCKER"

  kms_key_name = google_kms_crypto_key.cdd.id

  # Immutable tags: a promoted release tag must always name the same bytes. Without this a
  # digest-pinned deployment can still be undermined by the tag that produced it being moved
  # under a reviewer who checked the tag rather than the digest.
  docker_config {
    immutable_tags = true
  }

  depends_on = [
    google_project_service.required,
    google_kms_crypto_key_iam_member.artifactregistry,
  ]
}

output "image_repository" {
  description = "Image prefix for scripts/promote_production_images.sh --image-prefix."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}
