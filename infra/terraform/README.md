# Terraform: Doc1 CDD + Source-of-Wealth Agent infrastructure

In-region infrastructure for the Doc1 agent, built to deploy quickly and repeatedly to
multiple separate enterprises. The deploy **region is selected at deploy time**
(`var.region`, default `us-central1`) and validated against a residency
allowlist (`var.allowed_regions`) so an unapproved region fails fast at `terraform plan`
(P-03). Every resource name derives from **`var.name_prefix`** (`naming.tf`), so a second
instance (same project, or a second enterprise) deploys cleanly with a new prefix.
Residency is enforced in depth: per-resource region pin, a `gcp.resourceLocations` Org
Policy (`org_policy.tf`, gated by `enable_org_policies`), and the VPC-SC perimeter
(`vpc_sc.tf`, gated by `enable_vpc_sc`).

> Retrieval and storage infrastructure (the governed RAG store) lives in **Hrz2**
> (`enterprise-knowledge-base`), not here. Doc1 ingests case documents into Hrz2 and
> retrieves via Hrz2; this stack provisions only what Doc1 owns: document extraction, PII
> redaction, the guardrail, the WORM audit trail, CMEK, IAM and the perimeter.

## What this provisions

| File | Resource | Principle |
|------|----------|-----------|
| `apis.tf` | Enables only the managed services Doc1 uses | P-01 |
| `naming.tf` | Locals deriving every resource name from `name_prefix` | n/a |
| `document_ai.tf` | Document AI form-parser processor (KYC extraction) | P-03 |
| `dlp.tf` | DLP inspect + deidentify templates (incl. SG NRIC/FIN, passport) | P-04, R1 |
| `model_armor.tf` | Model Armor guardrail template (both directions) | R1 |
| `kms.tf` | Regional CMEK key ring + per-service-agent key bindings | P-09, P-03 |
| `firestore.tf` | Regional CMEK Firestore, point-in-time recovery and browser-flow/JTI TTL | P-03, P-07, P-09 |
| `production_edge.tf` | Optional immutable UI/API Cloud Run services and embed-compatible HTTPS edge | P-03, P-06 |
| `logging_worm.tf` | WORM audit bucket (lock via `worm_locked`) + sink + DATA_READ audit | P-08, R2 |
| `iam.tf` | Least-privilege app + Agent Runtime service accounts | P-06 |
| `agent_runtime.tf` | CMEK-encrypted Agent Runtime staging bucket | P-01, P-03 |
| `sanctions_sync.tf` | Snapshot bucket (always) + sync job/scheduler (image-gated) | P-03, P-09 |
| `org_policy.tf` | Project-level Org Policy guardrails (gated by `enable_org_policies`) | P-03, P-06 |
| `vpc_sc.tf` | VPC Service Controls perimeter (gated by `enable_vpc_sc`) | P-03 |
| `monitoring.tf` | Log-based security metrics + alert policies | P-07, P-08 |
| `outputs.tf` | Values to export into the runtime env after apply | n/a |

## Inputs

| Variable | Type | Default | When you need to set it |
|----------|------|---------|-------------------------|
| `project_id` | string | (required) | Always: the target project. |
| `name_prefix` | string | `"cdd-sow"` | Second instance in one project, or redeploy after a destroy (the KMS key ring is indestructible; a fresh prefix avoids the collision). Default reproduces the historical names. 3-19 chars (`^[a-z][a-z0-9-]{2,18}$`); keep <= 14 chars when enabling the sanctions job (30-char SA id cap, see `naming.tf`). |
| `region` | string | `"us-central1"` | Deploying to another approved jurisdiction. Must be in `allowed_regions` (fails fast). |
| `allowed_regions` | list(string) | SG, HK, Tokyo, Sydney, London | Approving a new region after residency review. |
| `scheduler_time_zone` | string | `"Asia/Singapore"` | Pair with `region` (e.g. `europe-west2` + `Europe/London`). |
| `retention_days` | number | `180` (six months) | Longer retention obligations (>= 180 enforced). |
| `existing_locked_retention_days` | number | `0` | Set to the current locked value before planning an existing stack; requested retention cannot be lower. |
| `worm_locked` | bool | `true` | Set `false` ONLY for evaluation/demo stacks that must stay deletable. Locking is IRREVERSIBLE; `true` is required for compliant production. |
| `enable_org_policies` | bool | `true` | Set `false` for a quick project-scoped evaluation without `roles/orgpolicy.policyAdmin`. |
| `enable_vpc_sc` | bool | `true` | Set `false` for a quick project-scoped evaluation without an Access Context Manager policy. |
| `access_policy_id` | string | `""` | Required when `enable_vpc_sc = true` (cross-validated at plan time). `gcloud access-context-manager policies create --organization=ORG_ID --title="sg-residency"` |
| `vpc_sc_enforce` | bool | `false` | Flip to `true` after a dry-run soak with operators added. |
| `operator_members` | list(string) | `[]` | Identities allowed through the perimeter from outside. |
| `allowed_policy_member_domains` | list(string) | `[]` | Domain-restricted sharing ids; empty skips that policy. |
| `alert_notification_channels` | list(string) | `[]` | Where the security alerts notify; empty still creates the policies. |
| `sanctions_sync_image` | string | `""` | Empty SKIPS the sync job + scheduler (bucket is still created; upload snapshots out-of-band). Set the app image to enable the daily sync. |
| `production_edge_enabled` | bool | `false` | Enable only after the named dossier supplies immutable images, domain, manifest and settings secrets. |
| `api_image`, `ui_image` | string | `""` | Required digest-pinned images when the production edge is enabled. |
| `agent_domain`, `dns_managed_zone` | string | `""` | Dedicated origin and optional existing DNS zone. |
| `installation_manifest_secret_id` / `_version`, `runtime_settings_secret_id` / `_version` | string | `""` | Existing reviewed secrets and immutable numeric versions mounted into the serving services. |
| `additional_secret_env` | map(object) | `{}` | Extra API secret environment bindings, each pinned to an existing secret id and numeric version. |
| `production_identity_mode` | string | `"oauth-access-token"` | Exact Mode 4 or Mode 5 identity selector. |
| `production_manifest_sha256`, `production_settings_sha256` | string | `""` | Exact reviewed payload digests, required for the production edge and checked again by the applications. |
| `enable_embed_signing_key` | bool | `false` | Enable only for Mode 5 after key custody is approved; creates an irreversible asymmetric key. |
| `embed_signing_protection_level` | string | `"HSM"` | HSM is the named-production default; software is for an explicitly approved non-production posture. |
| `edge_min_instances` | number | `1` | Named production preflight maps a reviewed value of at least 2. |
| `edge_per_source_rate_limit_per_minute` | number | `120` | Cloud Armor API ceiling per source IP; tune from observed host traffic. |
| `embed_shared_rate_limit_max` | number | `600` | Shared Firestore per-installation/client backstop per minute; validated at >= 5x the per-source edge ceiling. |

## Two deploy paths

**Quick evaluation** (project-scoped; no org-level roles; everything deletable; NOT
compliant for production):

```hcl
project_id          = "your-sandbox-project"
enable_vpc_sc       = false
enable_org_policies = false
worm_locked         = false
```

**Full sovereign** (the default posture; org-policy guardrails, perimeter, locked WORM):

```hcl
project_id       = "your-project"
access_policy_id = "111111111111" # required because enable_vpc_sc defaults to true
# region / allowed_regions / operator_members / alert_notification_channels /
# allowed_policy_member_domains / retention_days / sanctions_sync_image as needed
```

See `terraform.tfvars.example` for both, plus the second-instance scenario.

The optional production edge is reusable infrastructure, not production evidence. Complete
[`../../docs/named-production-deployment-dossier.md`](../../docs/named-production-deployment-dossier.md),
promote signed images with `scripts/promote_production_images.sh`, save and approve the plan, then
follow [`../../docs/named-production-runbook.md`](../../docs/named-production-runbook.md). Direct
Cloud Run ingress is restricted to internal/load-balancer traffic. The sandboxed Modes 4/5 edge
must remain reachable by the host loader and iframe, so application OAuth/grant validation is the
identity boundary; IAP belongs on the separately deployed standalone Mode 6 edge.

Mode 5 uses a deliberate two-stage apply. Set
`DOC1_DEPLOYMENT_STAGE=mode5-key-bootstrap` to provision the opt-in, HSM-backed,
non-exportable signing key while the edge is disabled. Place the resulting exact key-version
resource name into `DOC1_EMBED_SIGNING_KEY_VERSION` and a newly reviewed runtime-settings
secret version, then set `DOC1_DEPLOYMENT_STAGE=production-edge` and plan the edge. Terraform
fails the apply unless that reviewed value equals the actual bootstrap resource. This removes
any guessed-key bootstrap and keeps private material out of Terraform and environment
variables. Mode 4 starts directly with `production-edge`.

## Naming and the `name_prefix` mechanism

`naming.tf` derives every resource name from `var.name_prefix`. With the default prefix
the names equal the historical hard-coded ones (`cdd-sow-agent-ring`, `cdd-sow-agent-worm`,
`cdd-sow-research-audit`, `cdd-sow-guardrail`, `cdd-sow-app`, ...), so an existing default
deployment keeps its resources. Change the prefix to (a) run two instances in one project
or (b) redeploy after a `terraform destroy`: KMS key rings can never be deleted, and a
fresh prefix gives a fresh ring instead of an "already exists" failure.

**Rename caveat:** four names changed from their legacy literals even under the default
prefix, so an EXISTING deployment will see replacements planned once (review the plan):

- sanctions sync SA: `cdd-sanctions-sync` -> `cdd-sow-sanctions-sync`
- sanctions scheduler SA: `cdd-sanctions-sched` -> `cdd-sow-sanctions-sched`
- (the sanctions job/scheduler names now also derive from the prefix)
- VPC-SC perimeter: `cdd_sow_sg` -> `cdd_sow_perimeter` (stale `_sg` suffix dropped)
- VPC-SC access level: `cdd_operators` -> `cdd_sow_operators`

## State backend

The module declares a partial GCS backend. Named production must run through
`scripts/deployment_env.py`, which supplies `DOC1_TERRAFORM_STATE_BUCKET` and the
installation-specific `DOC1_TERRAFORM_STATE_PREFIX` during `terraform init`. The runner rejects
local state, inherited Terraform variables, auto-loaded variable files, override files and
caller-supplied `-var`, `-var-file` or `-backend-config` arguments. Offline validation and tests
may use `terraform init -backend=false` in an isolated `TF_DATA_DIR`; that exception does not
apply to a plan or apply.

## Usage

Named production uses the repository-root `.env` and `.env.secrets` contract:

```bash
cp .env.example .env
cp .env.secrets.example .env.secrets
chmod 600 .env.secrets
make deploy-env-check
# Replace placeholders and record approvals before enabling the gate.
make deploy-preflight
make deploy-verify-secrets
make tf-plan
```

`make tf-plan` runs Terraform through `scripts/deployment_env.py`; the loader maps reviewed
non-secret values into `TF_VAR_*` variables and never passes parsed secret values to Terraform.
It refuses to run while placeholders, mutable images, floating secret versions, unapproved
WORM locking, unbound payload digests, local state, or a first-apply VPC-SC enforcement flag
remain.

The direct `terraform.tfvars` path is retained for disposable infrastructure evaluation only:

```bash
cp terraform.tfvars.example terraform.tfvars   # pick a scenario, set project_id
terraform init -backend=false -input=false
terraform plan                                  # review
```

Do NOT run `terraform apply` against a shared project without review. The WORM bucket lock
(`worm_locked = true`, the default) is **irreversible**: confirm `retention_days` before
applying.

## Deploy order with VPC-SC

The perimeter blocks API calls from outside it. Apply once with `enable_vpc_sc = false`,
add your operator/CI identity to an access level, then re-apply with `enable_vpc_sc = true`.
Use VPC-SC dry-run mode (`vpc_sc_enforce = false`) before enforcing.

## After apply: outputs -> env vars

Export the outputs into the runtime environment; `config/settings.yaml` interpolates the
matching `${CDD_*}` tokens (including the `CDD_MODEL_ARMOR_TEMPLATE`, `CDD_LOG_NAME` and
`CDD_LOG_BUCKET` tokens read by `model_armor.template_id`, `logging.log_name` and
`logging.bucket`):

```bash
export CDD_REGION="$(terraform output -raw region)"
export CDD_DOCAI_PROCESSOR="$(terraform output -raw documentai_processor_id)"
export CDD_KMS_KEY="$(terraform output -raw kms_key)"
# Mode 5 only, after enable_embed_signing_key = true:
export CDD_EMBED_SIGNING_KEY_VERSION="$(terraform output -raw embed_signing_key_version)"
export CDD_DLP_INSPECT_TEMPLATE="$(terraform output -raw dlp_inspect_template)"
export CDD_DLP_DEIDENTIFY_TEMPLATE="$(terraform output -raw dlp_deidentify_template)"
export CDD_MODEL_ARMOR_TEMPLATE="$(terraform output -raw model_armor_template)"
export CDD_LOG_NAME="$(terraform output -raw audit_log_name)"
export CDD_LOG_BUCKET="$(terraform output -raw log_bucket)"
export CDD_SANCTIONS_BUCKET="$(terraform output -raw sanctions_bucket)"
```

Every output's description names the env var it feeds (`terraform output` shows them).
