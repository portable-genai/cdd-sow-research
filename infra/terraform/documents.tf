# Custody of the case documents an analyst uploads.
#
# The gcp document-store adapter writes here and the app requires it in the managed profile, but
# the stack provisioned no such bucket: the sanctions snapshot bucket and the agent staging bucket
# were the only two, so the FIRST upload on the deployed console failed with a 500 whose cause was
# "The specified bucket does not exist" from the Cloud Storage client. Nothing offline could
# notice, because every offline profile writes to a local path.
#
# Same posture as the sanctions bucket: regional in the allowlisted region, uniform access, CMEK
# (which does not cascade, hence the explicit service-agent grant it depends on), versioned so a
# replaced document is still auditable, and never force-destroyed.
resource "google_storage_bucket" "documents" {
  project                     = var.project_id
  name                        = local.documents_bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  versioning {
    enabled = true
  }

  encryption {
    default_kms_key_name = google_kms_crypto_key.cdd.id
  }

  depends_on = [
    google_project_service.required,
    google_kms_crypto_key_iam_member.storage_sanctions,
  ]
}

# The serving identity reads and writes case documents; nothing else in the project does.
resource "google_storage_bucket_iam_member" "documents_app" {
  bucket = google_storage_bucket.documents.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.app.email}"
}

# Additional writers: the runtime identities an EMBEDDING HOST creates for this app.
#
# A portal that mounts this app same-origin runs it under a service account of the portal's own
# making (journey-portal creates one per embedded app), so the grant above -- which names this stack's own
# serving identity -- does not cover it. The first upload through the portal was refused with
# "does not have storage.objects.list access" naming an account this stack has never heard of.
# Naming them here keeps the bucket's access list in ONE place, next to the bucket, rather than
# letting another stack grant itself access to this app's data store.
resource "google_storage_bucket_iam_member" "documents_embedded" {
  for_each = toset(var.document_writer_service_accounts)
  bucket   = google_storage_bucket.documents.name
  role     = "roles/storage.objectAdmin"
  member   = "serviceAccount:${each.value}"
}

output "documents_bucket" {
  description = "Case-document custody bucket; the serving profile's CDD_DOCUMENT_BUCKET."
  value       = google_storage_bucket.documents.name
}
