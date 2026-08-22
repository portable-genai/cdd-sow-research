# providers.tf — Provider pinning for the B1 CDD + Source-of-Wealth Agent.
#
# General Principle map:
#   P-03 (data residency / in-country): every provider call is pinned to var.region, the
#         region SELECTED AT DEPLOY TIME and validated against an allowlist (variables.tf).
#         There is no global/multi-region default; it defaults to us-central1.
#   P-02 (no lock-in): Terraform is the only place infra is described; the app itself
#         talks to ports, not these resources.
#
# google-beta is required because several sovereignty resources (Model Armor templates,
# some Access Context Manager fields) are only exposed on the beta surface as of the
# pinned provider line.

terraform {
  required_version = ">= 1.9.0"

  # Named production must initialize this partial backend through deployment_env.py,
  # which supplies the reviewed bucket and per-installation prefix. Keeping both values
  # out of source makes the module reusable; keeping the backend declaration active makes
  # accidental local state impossible in the production runner.
  backend "gcs" {}

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0" # 6.x line — current GA surface (mid-2026)
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 6.0"
    }
  }
}

# Primary (GA) provider — every resource defaults to the selected region.
provider "google" {
  project = var.project_id
  region  = var.region # the selected, allowlisted region — pinned, never global
}

# Beta provider — same project/region, used only where a resource needs it.
provider "google-beta" {
  project = var.project_id
  region  = var.region
}
