# variables.tf — The only knobs. Everything else is a concrete, prefix-derived value.
#
# General Principle map:
#   P-03 (residency): `region` is SELECTED AT DEPLOY TIME and validated against an
#         allowlist (var.allowed_regions) so a caller fails fast rather than deploying to
#         an unvetted, out-of-jurisdiction region. The default is us-central1, which is
#         the UNITED STATES and satisfies no Asia-Pacific residency regime. An adopter
#         under MAS, HKMA or APRA must set `region` and `allowed_regions` to its own
#         in-country region (asia-southeast1 for Singapore, which is what the rest of
#         the catalog defaults to) before any deployment. The default is a starting
#         point for evaluation, never a jurisdiction claim.
#   P-08 (auditability/retention): `retention_days` is a Terraform variable (the WORM
#         bucket lock is irreversible, so retention must be deliberate).
#
# The knob set supports two deploy paths and repeatable multi-enterprise rollout:
#   - QUICK EVALUATION (project-scoped, no org-level roles): project_id plus
#     enable_vpc_sc = false, enable_org_policies = false, worm_locked = false.
#     Everything stays deletable; NOT compliant for production.
#   - FULL SOVEREIGN (default posture): org-policy guardrails, VPC-SC perimeter
#     (access_policy_id required), locked WORM bucket.
#   - name_prefix keys every resource name, so a second instance (same project or a
#     second enterprise) deploys cleanly and never collides with an indestructible
#     KMS key ring left by an earlier stack.
# Per the build contract, only project_id, the region/residency values, the posture
# toggles and a few per-tenant values (perimeter, alert channels, sanctions image) are
# variables. Service identifiers derive from name_prefix (naming.tf). Storage/RAG infra
# lives in Hrz2.

variable "project_id" {
  description = "Target GCP project id (required). Single-tenant, in-region."
  type        = string
}

variable "name_prefix" {
  description = <<-EOT
    Prefix for every named resource this stack creates (key ring, buckets, SAs, templates,
    metrics, perimeter). Two reasons to change it: (a) two instances can coexist in one
    project, and (b) a destroy + redeploy does not collide with the indestructible KMS key
    ring left by the previous stack (key rings can never be deleted; a fresh prefix gives a
    fresh ring). The default reproduces the historical resource names (cdd-sow-agent-ring,
    cdd-sow-agent-worm, cdd-sow-guardrail, ...). Keep it to 14 chars or fewer when the
    sanctions job is enabled (see naming.tf: service-account id length limit).
  EOT
  type        = string
  default     = "cdd-sow"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,18}$", var.name_prefix))
    error_message = "name_prefix must match ^[a-z][a-z0-9-]{2,18}$ (lowercase letter first, then lowercase letters/digits/hyphens, 3-19 chars total)."
  }
}

variable "allowed_regions" {
  description = <<-EOT
    Residency allowlist: the regions this regulated stack may be deployed to. The region is
    chosen at deploy time (var.region) and validated against this list to FAIL FAST (P-03),
    so an operator cannot accidentally deploy to an unvetted region. Extend this list only
    after confirming the full stack (Document AI, DLP, Model Armor, Vertex/Agent Platform,
    CMEK, Logging) and your residency obligations are satisfied in that region.
  EOT
  type        = list(string)
  default     = ["us-central1"]

  validation {
    condition     = length(var.allowed_regions) > 0
    error_message = "allowed_regions must list at least one residency-approved region."
  }
}

variable "region" {
  description = <<-EOT
    Deployment region, SELECTED AT DEPLOY TIME. Defaults to us-central1
    but is overridable. Validated against var.allowed_regions so an unapproved region fails
    fast at `terraform plan` rather than deploying data out of jurisdiction (P-03).
  EOT
  type        = string
  default     = "us-central1"

  validation {
    # Cross-variable validation (Terraform >= 1.9). Fails at plan time = setup time.
    condition     = contains(var.allowed_regions, var.region)
    error_message = "region must be one of var.allowed_regions (residency allowlist). Add it there first if that region is approved for this workload (P-03)."
  }
}

variable "scheduler_time_zone" {
  description = "IANA time zone for the scheduled sanctions-sync job. Set to match var.region."
  type        = string
  default     = "UTC"
}

variable "retention_days" {
  description = <<-EOT
    WORM audit-log retention in days. Default 180 (six months); the lock is irreversible.

    The 180-day compliance floor (P-08) binds whenever worm_locked = true, which is the
    production posture and the default. It is NOT applied to an unlocked stack, where the
    policy is removable by a project owner anyway and therefore evidences routing and
    coverage rather than immutability. That lets an evaluation or reference deployment run a
    deliberately short, destroyable retention (e.g. 3 days) without weakening the control for
    anyone deploying for real: turning the lock on re-imposes the floor at plan time.
  EOT
  type        = number
  default     = 180 # Six months; mirrors config/settings.yaml logging.retention_days

  validation {
    condition     = var.worm_locked ? var.retention_days >= 180 : var.retention_days >= 1
    error_message = "A LOCKED stack must retain at least 180 days (six months) (P-08); an unlocked stack must still retain at least 1 day."
  }
}

variable "existing_locked_retention_days" {
  description = "Existing locked bucket retention in days, or 0 for a new stack. A plan may never request a lower value."
  type        = number
  default     = 0

  validation {
    condition     = var.existing_locked_retention_days == 0 || var.existing_locked_retention_days >= 180
    error_message = "existing_locked_retention_days must be 0 for a new stack or at least 180."
  }

  validation {
    condition     = var.existing_locked_retention_days == 0 || var.retention_days >= var.existing_locked_retention_days
    error_message = "retention_days cannot be lower than the existing locked retention. Existing stacks must preserve or increase their locked value."
  }
}

variable "worm_locked" {
  description = <<-EOT
    Lock the WORM audit bucket (P-08, R2).
    #########################################################################
    # WARNING: LOCKING IS IRREVERSIBLE. With true, the bucket and its       #
    # retention window can NEVER be reduced or deleted until every entry    #
    # ages out (180 days by default), not even with project-owner rights.  #
    #########################################################################
    true (the default) is REQUIRED for a compliant production deploy: the audit trail is
    Write-Once-Read-Many only when locked. Set false ONLY for evaluation/demo stacks that
    must remain deletable (terraform destroy works); that posture is NOT compliant.
  EOT
  type        = bool
  default     = true
}

variable "enable_org_policies" {
  description = <<-EOT
    Create the project-level Org Policy guardrails (resourceLocations residency pin,
    disable SA key creation, uniform bucket-level access). Needs the caller to hold
    roles/orgpolicy.policyAdmin on the project. Set false for a quick project-scoped
    evaluation deploy without that role; the per-resource region pins still apply, but
    the defence-in-depth policy layer is skipped (NOT compliant for production).
  EOT
  type        = bool
  default     = true
}

variable "access_policy_id" {
  description = <<-EOT
    Existing Access Context Manager policy id (numeric, no prefix) for the org.
    Required when enable_vpc_sc = true; the service perimeter is created under it.
    Create once per org with:
      gcloud access-context-manager policies create \
        --organization=ORG_ID --title="sg-residency"
  EOT
  type        = string
  default     = ""

  validation {
    condition     = !var.enable_vpc_sc || length(var.access_policy_id) > 0
    error_message = "enable_vpc_sc = true requires access_policy_id. Either supply your org's Access Context Manager policy id (create one with: gcloud access-context-manager policies create --organization=ORG_ID --title='residency-policy') or set enable_vpc_sc = false for a project-scoped quick deploy."
  }
}

variable "enable_vpc_sc" {
  description = "Create the VPC Service Controls perimeter around the AI/data APIs (P-03)."
  type        = bool
  default     = true
}

variable "vpc_sc_enforce" {
  description = <<-EOT
    Enforce the VPC-SC perimeter (true) or run it in DRY-RUN / audit mode (false). Good
    practice is to apply with false first, watch the dry-run violation logs, add your
    operator/CI identities to var.operator_members, then flip to true to enforce (P-03).
  EOT
  type        = bool
  default     = false
}

variable "operator_members" {
  description = <<-EOT
    Identities (e.g. "user:you@example.com", "serviceAccount:ci@PROJECT.iam.gserviceaccount.com")
    allowed to reach the perimeter-restricted APIs from outside the perimeter, via an Access
    Context Manager access level. Empty list means no access level is created.
  EOT
  type        = list(string)
  default     = []
}

variable "allowed_policy_member_domains" {
  description = <<-EOT
    Customer/directory ids (e.g. "C0xxxxxxx") permitted by the domain-restricted-sharing
    org policy (constraints/iam.allowedPolicyMemberDomains). Empty list disables that policy
    (leave empty if you are not org admin or do not want to manage it here). Only applied
    when enable_org_policies = true.
  EOT
  type        = list(string)
  default     = []
}

variable "alert_notification_channels" {
  description = <<-EOT
    Cloud Monitoring notification channel ids for the security alert policies (guardrail
    blocks, SA-key creation, VPC-SC denials, CMEK changes). Empty list still creates the
    alert policies and log-based metrics, just with no channel attached.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition = !var.production_edge_enabled || (
      length(var.alert_notification_channels) > 0 &&
      alltrue([
        for channel in var.alert_notification_channels :
        can(regex("^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/notificationChannels/[0-9]+$", channel))
      ])
    )
    error_message = "production_edge_enabled requires at least one valid Cloud Monitoring notification channel."
  }
}

variable "sanctions_sync_image" {
  description = <<-EOT
    Container image for the scheduled sanctions-list sync Cloud Run job (the app image;
    its entrypoint runs scripts/sync_sanctions.py). Empty (the default) SKIPS creating the
    sync job and its scheduler; supply the app image to enable them. The snapshot bucket is
    created either way (upload out-of-band while empty, e.g. scripts/sync_sanctions.py --gcs).
    e.g. us-central1-docker.pkg.dev/PROJECT/cdd/cdd-sow-research:TAG
  EOT
  type        = string
  default     = ""
}

variable "standalone" {
  type        = bool
  description = "Standalone deploy (default): provision this repo's own guardrail (Model Armor), DLP templates and WORM audit bucket+sink. Set false for a platform deploy where Hrz1/Hrz5 front those. Immutable per deployment (a locked WORM bucket blocks a true->false toggle). validate ignores variable-driven count, so a second `validate -var standalone=false` is a wiring smoke, not proof the count=0 path is destroy-safe."
  default     = true
}

variable "enable_embed_signing_key" {
  description = "Create the irreversible asymmetric Mode 5 signing key and initial version. Enable only for an approved embedded-grant deployment."
  type        = bool
  default     = false
}

variable "embed_signing_protection_level" {
  description = "Cloud KMS protection for the Mode 5 asymmetric signing key. HSM is the named-production default."
  type        = string
  default     = "HSM"

  validation {
    condition     = contains(["HSM", "SOFTWARE"], var.embed_signing_protection_level)
    error_message = "embed_signing_protection_level must be HSM or SOFTWARE."
  }

  validation {
    condition     = !var.production_edge_enabled || var.embed_signing_protection_level == "HSM"
    error_message = "Named production requires HSM embed signing-key protection."
  }
}

variable "embed_signing_key_version" {
  description = "Exact Mode 5 KMS key-version resource name recorded from the reviewed bootstrap output."
  type        = string
  default     = ""
}

variable "browser_flow_records_collection" {
  description = "Firestore collection with short-lived browser-flow records."
  type        = string
  default     = "browser_flows"
}

variable "browser_flow_aliases_collection" {
  description = "Firestore collection mapping opaque hashes to browser-flow records."
  type        = string
  default     = "browser_flow_aliases"
}

variable "browser_flow_outbox_collection" {
  description = "Firestore collection with sanitized browser-flow transition outbox events."
  type        = string
  default     = "browser_flow_outbox"
}

variable "browser_flow_citations_collection" {
  description = "Firestore collection with citations emitted to verified actors."
  type        = string
  default     = "cdd_citation_ledger"
}

variable "browser_flow_replay_collection" {
  description = "Firestore collection for consumed private_key_jwt identifiers."
  type        = string
  default     = "client_assertion_replay"
}

variable "embed_rate_limits_collection" {
  description = "Firestore collection for shared multi-replica Mode 5 fixed-window counters."
  type        = string
  default     = "embed_rate_limits"
}

variable "embed_shared_rate_limit_max" {
  description = "Shared per-installation/client broker backstop per minute. Cloud Armor provides the per-source abuse boundary."
  type        = number
  default     = 600

  validation {
    condition     = var.embed_shared_rate_limit_max >= 100 && var.embed_shared_rate_limit_max <= 100000
    error_message = "embed_shared_rate_limit_max must be from 100 to 100000."
  }

  validation {
    condition     = var.embed_shared_rate_limit_max >= var.edge_per_source_rate_limit_per_minute * 5
    error_message = "embed_shared_rate_limit_max must be at least five times the per-source edge ceiling so one source cannot exhaust shared capacity."
  }
}

variable "edge_per_source_rate_limit_per_minute" {
  description = "Cloud Armor API request ceiling per source IP per minute before HTTP 429."
  type        = number
  default     = 120

  validation {
    condition     = var.edge_per_source_rate_limit_per_minute >= 10 && var.edge_per_source_rate_limit_per_minute <= 10000
    error_message = "edge_per_source_rate_limit_per_minute must be from 10 to 10000."
  }
}

variable "deployment_stage" {
  description = "Reviewed rollout stage: create only the Mode 5 signing key first, or deploy the complete production edge."
  type        = string
  default     = "disabled"

  validation {
    condition     = contains(["disabled", "mode5-key-bootstrap", "production-edge"], var.deployment_stage)
    error_message = "deployment_stage must be disabled, mode5-key-bootstrap or production-edge."
  }
}

variable "production_edge_enabled" {
  description = "Provision the named Doc1 UI/API edge. Requires immutable images, domain and manifest secret."
  type        = bool
  default     = false
}

variable "api_image" {
  description = "Immutable Doc1 API image reference including @sha256 digest."
  type        = string
  default     = ""

  validation {
    condition     = !var.production_edge_enabled || can(regex("@sha256:[0-9a-f]{64}$", var.api_image))
    error_message = "production_edge_enabled requires api_image pinned by @sha256 digest."
  }
}

variable "ui_image" {
  description = "Immutable Doc1 UI image reference including @sha256 digest."
  type        = string
  default     = ""

  validation {
    condition     = !var.production_edge_enabled || can(regex("@sha256:[0-9a-f]{64}$", var.ui_image))
    error_message = "production_edge_enabled requires ui_image pinned by @sha256 digest."
  }
}

variable "agent_domain" {
  description = "Dedicated DNS name for the production agent origin."
  type        = string
  default     = ""

  validation {
    condition     = !var.production_edge_enabled || can(regex("^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])$", var.agent_domain))
    error_message = "production_edge_enabled requires a valid lowercase agent_domain."
  }
}

variable "dns_managed_zone" {
  description = "Optional existing Cloud DNS managed zone in which to create agent_domain."
  type        = string
  default     = ""
}

variable "installation_manifest_secret_id" {
  description = "Existing Secret Manager secret containing the reviewed installation manifest."
  type        = string
  default     = ""

  validation {
    condition     = !var.production_edge_enabled || length(var.installation_manifest_secret_id) > 0
    error_message = "production_edge_enabled requires installation_manifest_secret_id."
  }
}

variable "installation_manifest_secret_version" {
  description = "Immutable numeric Secret Manager version for the reviewed installation manifest."
  type        = string
  default     = ""

  validation {
    condition     = !var.production_edge_enabled || can(regex("^[1-9][0-9]*$", var.installation_manifest_secret_version))
    error_message = "production_edge_enabled requires a numeric installation_manifest_secret_version, never latest."
  }
}

variable "runtime_settings_secret_id" {
  description = "Existing Secret Manager secret containing the reviewed production settings.yaml."
  type        = string
  default     = ""

  validation {
    condition     = !var.production_edge_enabled || length(var.runtime_settings_secret_id) > 0
    error_message = "production_edge_enabled requires runtime_settings_secret_id."
  }
}

variable "runtime_settings_secret_version" {
  description = "Immutable numeric Secret Manager version for reviewed production settings."
  type        = string
  default     = ""

  validation {
    condition     = !var.production_edge_enabled || can(regex("^[1-9][0-9]*$", var.runtime_settings_secret_version))
    error_message = "production_edge_enabled requires a numeric runtime_settings_secret_version, never latest."
  }
}

variable "additional_secret_env" {
  description = "API environment variable name to an immutable existing Secret Manager secret version."
  type = map(object({
    secret_id = string
    version   = string
  }))
  default = {}

  validation {
    condition = alltrue([
      for name, secret in var.additional_secret_env :
      can(regex("^[A-Z][A-Z0-9_]{1,127}$", name)) &&
      length(secret.secret_id) > 0 &&
      can(regex("^[1-9][0-9]*$", secret.version)) &&
      !contains([
        "CDD_PROFILE",
        "GOOGLE_CLOUD_PROJECT",
        "GCP_REGION",
        "CDD_REGION",
        "CDD_FIRESTORE_DB",
        "CDD_KMS_KEY",
        "CDD_DOCAI_PROCESSOR",
        "CDD_IDENTITY_PROFILE",
        "CDD_CHANNEL_PROFILE",
        "CDD_PUBLIC_ORIGIN",
        "CDD_INSTALLATION_MANIFEST",
        "CDD_INSTALLATION_MANIFEST_VERSION",
        "CDD_EXPECTED_MANIFEST_SHA256",
        "CDD_EXPECTED_SETTINGS_SHA256",
        "CDD_SETTINGS",
        "CDD_PRODUCTION",
        "CDD_REPLICA_COUNT",
        "CDD_BROWSER_FLOW_FIRESTORE_DB",
        "CDD_BROWSER_FLOW_COLLECTION",
        "CDD_BROWSER_FLOW_ALIAS_COLLECTION",
        "CDD_BROWSER_FLOW_OUTBOX_COLLECTION",
        "CDD_CITATION_LEDGER_COLLECTION",
        "CDD_CLIENT_ASSERTION_REPLAY_COLLECTION",
        "CDD_EMBED_RATE_LIMIT_COLLECTION",
        "CDD_EMBED_SHARED_RATE_LIMIT_MAX",
        "CDD_EMBED_SHARED_RATE_LIMIT_WINDOW",
      ], name)
    ])
    error_message = "additional_secret_env requires uppercase non-reserved names, non-empty secret ids and numeric versions."
  }
}

variable "production_identity_mode" {
  description = "Exact identity selector for the named edge."
  type        = string
  default     = "oauth-access-token"

  validation {
    condition     = contains(["oauth-access-token", "embedded-grant"], var.production_identity_mode)
    error_message = "production_identity_mode must be oauth-access-token or embedded-grant."
  }

  validation {
    condition     = !var.production_edge_enabled || var.production_identity_mode != "embedded-grant" || var.enable_embed_signing_key
    error_message = "A production embedded-grant edge requires enable_embed_signing_key = true."
  }
}

variable "production_manifest_version" {
  description = "Reviewed installation manifest version deployed to both UI and API."
  type        = string
  default     = ""

  validation {
    condition     = !var.production_edge_enabled || length(var.production_manifest_version) > 0
    error_message = "production_edge_enabled requires production_manifest_version."
  }
}

variable "production_manifest_sha256" {
  description = "SHA-256 of the exact reviewed installation manifest bytes."
  type        = string
  default     = ""

  validation {
    condition     = !var.production_edge_enabled || can(regex("^[0-9a-f]{64}$", var.production_manifest_sha256))
    error_message = "production_edge_enabled requires production_manifest_sha256."
  }
}

variable "production_settings_sha256" {
  description = "SHA-256 of the exact reviewed runtime settings bytes."
  type        = string
  default     = ""

  validation {
    condition     = !var.production_edge_enabled || can(regex("^[0-9a-f]{64}$", var.production_settings_sha256))
    error_message = "production_edge_enabled requires production_settings_sha256."
  }
}

variable "edge_min_instances" {
  description = "Minimum instances for each production service."
  type        = number
  default     = 1

  validation {
    condition     = !var.production_edge_enabled || var.edge_min_instances >= 2
    error_message = "production_edge_enabled requires at least two instances for multi-replica state."
  }
}

variable "edge_max_instances" {
  description = "Maximum instances for each production service."
  type        = number
  default     = 10

  validation {
    condition     = var.edge_max_instances >= var.edge_min_instances
    error_message = "edge_max_instances must be greater than or equal to edge_min_instances."
  }
}
