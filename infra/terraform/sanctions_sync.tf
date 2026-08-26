# sanctions_sync.tf — scheduled sync of the sanctions/PEP watchlist snapshot.
#
# General Principle map:
#   P-03 (residency): the snapshot bucket and the Cloud Run job are in var.region.
#   P-09 (CMEK explicit): the bucket is CMEK-encrypted (storage SA key binding below).
#   Reproducible screening: a scheduled Cloud Run job runs scripts/sync_sanctions.py to
#         pull OFAC SLS (SDN + Consolidated) + UN/EU/UK, diff, and write a *versioned*
#         snapshot object that the gcp SanctionsListProviderPort reads. Screening never
#         calls the publishers directly — it reads this point-in-time cached copy, so
#         alerts are reproducible and stay in-region with no per-request egress.
#
# Gating: the job, its scheduler and their IAM are created ONLY when
# var.sanctions_sync_image is set. A Cloud Run job cannot be created with an empty image,
# so an ungated job made the default `terraform apply` FAIL; it is guarded so the default
# deploy succeeds. The bucket, the GCS CMEK binding, the sync SA and its bucket IAM stay
# unconditional: the app reads the bucket either way. With no image, upload the snapshot
# out-of-band (e.g. scripts/sync_sanctions.py --gcs <bucket>/snapshot/current.json).
#
# NOTE: requires egress from the job to the publisher domains. If the project runs under a
# strict VPC-SC perimeter / no public egress, run the job in a network that allows the
# publisher hosts (or mirror the files into an allowed bucket first).

# Regional, CMEK-encrypted bucket holding the current (and versioned) watchlist snapshot.
resource "google_storage_bucket" "sanctions" {
  project                     = var.project_id
  name                        = local.sanctions_bucket_name
  location                    = var.region # selected, allowlisted region (P-03)
  uniform_bucket_level_access = true
  force_destroy               = false

  versioning {
    enabled = true # keep prior snapshots for audit / rollback
  }

  encryption {
    default_kms_key_name = google_kms_crypto_key.cdd.id # CMEK (P-09)
  }

  depends_on = [
    google_project_service.required,
    google_kms_crypto_key_iam_member.storage_sanctions,
  ]
}

# Let the Cloud Storage service agent use the CMEK key (P-09 — CMEK does not cascade).
data "google_storage_project_service_account" "gcs" {
  project = var.project_id
}

resource "google_kms_crypto_key_iam_member" "storage_sanctions" {
  crypto_key_id = google_kms_crypto_key.cdd.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${data.google_storage_project_service_account.gcs.email_address}"
}

# Dedicated, least-privilege identity for the sync job (kept even when the job is skipped,
# so out-of-band uploads can impersonate it rather than a personal identity).
resource "google_service_account" "sanctions_sync" {
  project      = var.project_id
  account_id   = local.sanctions_sync_sa_id
  display_name = "B1 sanctions watchlist sync job"
}

# The job needs to write the snapshot object into the bucket.
resource "google_storage_bucket_iam_member" "sync_writer" {
  bucket = google_storage_bucket.sanctions.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.sanctions_sync.email}"
}

# ...and something has to READ it. Only the writer was ever granted, so the bucket existed, the
# snapshot existed, and every screening call from the serving identity was refused. The dossier
# then reported `screening: null`, which reads as "this profile does not screen" rather than as an
# outage, and no call anywhere failed loudly. Found on 2026-08-26 by comparing a deployment run
# against a laptop run: the laptop screened six lists and the deployment screened none.
#
# Read-only on purpose. The application never writes the snapshot; only the sync job does, and
# granting the serving identity write access here would let a compromised request rewrite the
# watchlist it is about to be screened against.
resource "google_storage_bucket_iam_member" "sanctions_app_reader" {
  bucket = google_storage_bucket.sanctions.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.app.email}"
}

# The embedding host's runtime identity, for the same reason documents.tf carries the equivalent
# grant: a portal that mounts this app same-origin runs it under an identity of the PORTAL's
# making, which this stack cannot know, so the serving-identity grant above misses it entirely.
# That reasoning was applied to case documents and not to the watchlist, which is precisely how
# the deployment came to screen nobody. The variable is named for its first use; the identities
# are the same serving identities.
resource "google_storage_bucket_iam_member" "sanctions_embedded_readers" {
  for_each = toset(var.document_writer_service_accounts)
  bucket   = google_storage_bucket.sanctions.name
  role     = "roles/storage.objectViewer"
  member   = "serviceAccount:${each.value}"
}

# Cloud Run Job that runs scripts/sync_sanctions.py --gcs <bucket>/snapshot/current.json.
# Created only when var.sanctions_sync_image is supplied (see gating note above).
resource "google_cloud_run_v2_job" "sanctions_sync" {
  count = var.sanctions_sync_image == "" ? 0 : 1

  project  = var.project_id
  name     = local.sanctions_job_name
  location = var.region

  template {
    template {
      service_account = google_service_account.sanctions_sync.email
      max_retries     = 2
      timeout         = "1800s"
      containers {
        image   = var.sanctions_sync_image # the app image; runs the sync entrypoint
        command = ["python", "scripts/sync_sanctions.py"]
        args    = ["--gcs", "${google_storage_bucket.sanctions.name}/snapshot/current.json"]
        env {
          name  = "CDD_PROFILE"
          value = "gcp"
        }
      }
    }
  }

  depends_on = [google_project_service.required]
}

# Identity Cloud Scheduler uses to trigger the job (Run Jobs invoker).
resource "google_service_account" "sanctions_scheduler" {
  count = var.sanctions_sync_image == "" ? 0 : 1

  project      = var.project_id
  account_id   = local.sanctions_sched_sa_id
  display_name = "B1 sanctions sync scheduler"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  count = var.sanctions_sync_image == "" ? 0 : 1

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.sanctions_sync[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.sanctions_scheduler[0].email}"
}

# Daily poll-and-diff (OFAC has no fixed publish schedule; poll, then diff).
resource "google_cloud_scheduler_job" "sanctions_sync" {
  count = var.sanctions_sync_image == "" ? 0 : 1

  project   = var.project_id
  name      = local.sanctions_schedule_name
  region    = var.region
  schedule  = "0 2 * * *" # 02:00 daily
  time_zone = var.scheduler_time_zone

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${one(google_cloud_run_v2_job.sanctions_sync[*].name)}:run"
    oauth_token {
      service_account_email = one(google_service_account.sanctions_scheduler[*].email)
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_invoker]
}
