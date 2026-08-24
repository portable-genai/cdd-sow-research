# org_policy.tf — Project-level Org Policy guardrails (defence in depth).
#
# General Principle map:
#   P-03 (residency): constraints/gcp.resourceLocations hard-restricts where resources may
#         be created to the SELECTED region's location group. Combined with the per-resource
#         region pin and the VPC-SC perimeter, this makes an out-of-jurisdiction resource
#         impossible to create, not merely avoided by convention.
#   P-06 (least privilege / key hygiene): disable service-account key creation (prefer
#         Workload Identity / attached SAs over exportable keys) and require uniform
#         bucket-level access (no legacy ACLs). Optional domain-restricted sharing.
#
# Applied at the PROJECT level (parent = projects/<id>) so this stack does not require
# org-admin beyond the target project. Requires orgpolicy.googleapis.com (apis.tf) and that
# the caller holds roles/orgpolicy.policyAdmin on the project.
#
# Gating: every policy here is created only when var.enable_org_policies = true (the
# default). Set it false for a quick project-scoped evaluation deploy by a caller without
# roles/orgpolicy.policyAdmin; the per-resource region pins still apply, but this
# defence-in-depth layer is skipped (NOT compliant for production).
#
# verify: https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/org_policy_policy

# Residency: only allow resource creation in the selected region's location group.
resource "google_org_policy_policy" "resource_locations" {
  count = var.enable_org_policies ? 1 : 0

  name   = "projects/${var.project_id}/policies/gcp.resourceLocations"
  parent = "projects/${var.project_id}"

  spec {
    rules {
      values {
        # Default: "in:<region>-locations" — the region plus its zones/sub-locations, and
        # nothing else. var.resource_location_values overrides it only where a required
        # service has no single-region presence; see that variable for why widening is a
        # jurisdiction statement rather than an exception list.
        allowed_values = length(var.resource_location_values) > 0 ? var.resource_location_values : ["in:${var.region}-locations"]
      }
    }
  }

  depends_on = [google_project_service.required]
}

# Key hygiene: forbid creating exportable service-account keys (P-06). Use attached SAs /
# Workload Identity instead. The SAs in iam.tf never need exported keys.
resource "google_org_policy_policy" "disable_sa_keys" {
  count = var.enable_org_policies ? 1 : 0

  name   = "projects/${var.project_id}/policies/iam.disableServiceAccountKeyCreation"
  parent = "projects/${var.project_id}"

  spec {
    rules {
      enforce = "TRUE"
    }
  }

  depends_on = [google_project_service.required]
}

# Require uniform bucket-level access on every bucket (no legacy object ACLs).
resource "google_org_policy_policy" "uniform_bucket_access" {
  count = var.enable_org_policies ? 1 : 0

  name   = "projects/${var.project_id}/policies/storage.uniformBucketLevelAccess"
  parent = "projects/${var.project_id}"

  spec {
    rules {
      enforce = "TRUE"
    }
  }

  depends_on = [google_project_service.required]
}

# Optional domain-restricted sharing: only principals from the listed customer/directory
# ids may be granted IAM. Created only when org policies are enabled AND
# var.allowed_policy_member_domains is non-empty.
resource "google_org_policy_policy" "allowed_member_domains" {
  count = var.enable_org_policies && length(var.allowed_policy_member_domains) > 0 ? 1 : 0

  name   = "projects/${var.project_id}/policies/iam.allowedPolicyMemberDomains"
  parent = "projects/${var.project_id}"

  spec {
    rules {
      values {
        allowed_values = var.allowed_policy_member_domains
      }
    }
  }

  depends_on = [google_project_service.required]
}
