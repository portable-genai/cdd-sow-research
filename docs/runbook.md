# Runbook: `cdd-sow-research` CDD + Source-of-Wealth Agent

Operational notes for deploying and running `cdd-sow-research` on the Gemini Enterprise Agent Platform in
`asia-southeast1`. This is a reference build; adapt to your own change-management and
model-risk sign-off before any live use.

## 1. Deploy

```bash
# 1. From the repository root, validate the separated deployment inputs, bind the exact
#    Secret Manager versions, and produce a remote-state plan. Review the plan; the WORM
#    bucket lock is irreversible when worm_locked = true, the default.
make deploy-preflight
make deploy-verify-secrets
make tf-plan
# Apply only in an approved change window, through the same reviewed runner:
# Saved-plan apply is rejected; review the apply-generated plan interactively.
python3.12 scripts/deployment_env.py run -- terraform -chdir=infra/terraform apply

# 2. Export the outputs into the runtime environment (settings.yaml resolves them).
cd infra/terraform
export CDD_REGION="$(terraform output -raw region)"
export CDD_DOCAI_PROCESSOR="$(terraform output -raw documentai_processor_id)"
export CDD_KMS_KEY="$(terraform output -raw kms_key)"
export CDD_DLP_INSPECT_TEMPLATE="$(terraform output -raw dlp_inspect_template)"
export CDD_DLP_DEIDENTIFY_TEMPLATE="$(terraform output -raw dlp_deidentify_template)"
export CDD_MODEL_ARMOR_TEMPLATE="$(terraform output -raw model_armor_template)"
export CDD_LOG_NAME="$(terraform output -raw audit_log_name)"
export CDD_LOG_BUCKET="$(terraform output -raw log_bucket)"
export CDD_SANCTIONS_BUCKET="$(terraform output -raw sanctions_bucket)"

# 3. Install the managed stack and run the API.
pip install -e ".[gcp,dev]"
export GOOGLE_CLOUD_PROJECT=your-sg-project CDD_PROFILE=gcp
gcloud auth application-default login
make run-api          # FastAPI on :8090
```

The reviewed runner accepts Terraform only when `-chdir` resolves exactly to this repository's
`infra/terraform` directory. A different directory, including one reached through a symlink, is
rejected before Terraform starts.

Mode 5 uses two explicit stages. First set
`DOC1_DEPLOYMENT_STAGE=mode5-key-bootstrap`, plan and apply the HSM signing key with the edge
disabled, and record the resulting exact key-version resource name in both
`DOC1_EMBED_SIGNING_KEY_VERSION` and a new reviewed runtime settings secret version. Then set
`DOC1_DEPLOYMENT_STAGE=production-edge`, review the complete edge plan, and apply interactively.
The second apply fails if the reviewed value does not equal the actual Terraform key-version
resource. Mode 4 starts directly with `production-edge`.

Prerequisites and the two deploy paths: the default posture (full sovereign) expects an
Access Context Manager policy id (`access_policy_id`) and `roles/orgpolicy.policyAdmin`
on the project. For a quick project-scoped evaluation WITHOUT those org-level
prerequisites, set `enable_vpc_sc = false`, `enable_org_policies = false` and
`worm_locked = false` in `terraform.tfvars` (everything stays deletable; not compliant
for production). See `infra/terraform/terraform.tfvars.example` for both scenarios and
the second-instance (`name_prefix`) scenario.

The ADK agent is deployed to Agent Runtime separately via the Agent Platform SDK; see the
docstring in `src/cdd_sow_research/agent/root_agent.py`. Record the resulting `reasoningEngine`
resource name in `settings.agent_engine.resource_name` (or `CDD_AGENT_ENGINE`).

### 1.1 Modes 4/5 production enablement

The repository's full synthetic channel/identity gate passes, but it is not production
deployment evidence. Before enabling a named Modes 4/5 installation, provision and verify:

1. external Mode 4 IdP and Mode 5 BFF registrations;
2. approve and apply the reusable DNS, TLS, ingress, and immutable UI/API edge;
3. verify the regional Firestore BrowserFlow and JTI replay stores under multi-replica failure;
4. approve the Cloud KMS signing-key custody, rotation, accepted-key windows, and revocation;
5. reviewed installation origins plus client `script-src` and `frame-src` policy;
6. Chromium, Firefox, and WebKit evidence in the target hosting environment; and
7. a separately deployed Mode 6 fallback with its own OIDC registration and restricted
   discovery/JWKS egress.

Keep unsupported bindings disabled so startup fails closed. The owned production procedure,
incident response, backup/restore, and evidence retention must be completed with the client.
See the production gate in
[`embedding-implementation-plan.md`](embedding-implementation-plan.md) and the normative
integration contract in [`embedding-and-identity.md`](embedding-and-identity.md). Use
[`named-production-deployment-dossier.md`](named-production-deployment-dossier.md) and
[`named-production-runbook.md`](named-production-runbook.md) for the named apply.

## 2. Region selection and fail-fast

The Terraform `region` is selected at deploy time and validated against the residency
allowlist `allowed_regions` (default member: `asia-southeast1`); an apply against a region
not in that list fails immediately at `terraform plan`, before anything is created. Set
`CDD_REGION` for the app and `scheduler_time_zone` to match. DLP, Model Armor and the WORM
bucket are created in the selected region, and a `gcp.resourceLocations` Org Policy
hard-restricts resource creation to the approved width. Document AI is the stated exception:
it is created at `docai_location` (default the `us` multi-region, a disclosed residency
deviation until Google grants single-region access; see `infra/terraform/variables.tf`).
Confirm `docai_location` in the outputs equals the value you decided, not the deploy region.

## 3. Key rotation

The CMEK crypto key (`kms.tf`) rotates every 90 days. Rotation is transparent to the app;
no restart is needed. The key has `prevent_destroy = true` so it cannot be torn down while
data depends on it.

## 4. Retention and the WORM lock

The audit bucket retention is `retention_days` (default 180 days, six months) and the bucket is
locked by default (`worm_locked = true`), which is **irreversible**. To trial without
locking, set `worm_locked = false` in `terraform.tfvars` (not compliant for production).
Only redacted prompts/responses are ever written to the audit log (P-04, R1).

For a new stack, set `DOC1_EXISTING_LOCKED_RETENTION_DAYS=0`. Before upgrading an existing
stack, read its current locked retention and set that exact value in
`DOC1_EXISTING_LOCKED_RETENTION_DAYS`. If the existing stack still uses the former 2557-day
default, also keep `DOC1_AUDIT_RETENTION_DAYS=2557` or choose a larger approved value. Both the
preflight and Terraform reject any attempted reduction. Do not rely on the new 180-day default
when planning an existing locked bucket.

Set `DOC1_STACK_LIFECYCLE=new` only for a genuinely new prefix. The reviewed plan runner proves
the derived WORM bucket does not exist. Set it to `existing` for an upgrade; the runner reads the
bucket's live lock and retention metadata and rejects a mismatch. An operator-entered zero is
never sufficient evidence that a stack is new.

## 4a. Perpetual-KYC operations

Perpetual KYC keeps two kinds of state and they are operationally different:

- **Baselines** (`pkyc_baselines`) define what "unchanged" means. Losing them is not a data
  loss so much as an alert storm: the next cycle sees every standing fact as new. Restore
  from the Firestore backup rather than letting a cycle re-baseline against a changed
  picture, and never delete a baseline to "reset" a noisy subject.
- **Queue items** (`pkyc_assessments`) are one live entry per subject; a re-run supersedes
  the previous entry, and the superseded run remains in the WORM audit trail. The queue is
  a working surface, not the record of what happened.

Retuning the control is a configuration change, not a deploy of new logic: the uplifts,
the score ceiling, the priority thresholds and the SLA days all live under
`policy.perpetual_kyc` in `config/settings.yaml`. Because the settings hash is part of the
deployment selector, a retune is visible in `/healthz` and in the audit trail.

The module never blocks, freezes or exits a relationship. If an operator asks for
perpetual KYC to take an action automatically, the answer is no by design (P-06): the
outcome is routed to `human-review-console` and a checker disposes.

## 4b. Exporting and reloading a complete case

Handing a case to another institution, another deployment, or a regulator means handing
over the dossier AND the evidence it cites. One archive does both:

```bash
cdd-sow bundle export <case-id> case-bundle.zip --tenant <tenant>
```

This writes the archive plus `case-bundle.zip.manifest.json` holding the manifest digest.
**Record that digest somewhere other than the archive** (the transfer receipt, the WORM
audit trail, a signed note). The digests inside the archive prove it is not corrupt; only
the out-of-band one proves it was not rewritten in transit.

To reload on the far side:

```bash
cdd-sow bundle restore <case-id> case-bundle.zip --tenant <tenant> \
  --manifest-sha256 sha256:<the digest you recorded>
```

Operational notes:

- **Documents keep their original ids.** That is deliberate: the dossier's citations name
  those ids, so a reload that re-minted them would produce a dossier whose links go
  nowhere. A conflicting id (already held, different bytes) is refused rather than
  overwritten, and identical bytes are a no-op, so a retried reload is safe.
- **The bundle's ACL tags are never applied.** Restored documents are filed under tags
  derived from the restoring side's own identity and the `--tenant` you pass. Check that
  the tenant is right before restoring; nothing in the archive can correct it for you.
- **A rejected bundle writes nothing.** Verification completes before the first write, so
  a failed reload leaves the store exactly as it was. Re-run it after fixing the transfer.
- **On-prem:** the `onprem` document-store adapter is still a fail-fast placeholder, so a
  reload there raises `NotImplementedError` (HTTP 501 through the API) until the vault
  adapter is implemented. See [`onprem-migration.md`](onprem-migration.md).
- **Size ceilings** live in `config/settings.yaml` under `document_store`
  (`max_bundle_bytes`, `max_bundle_uncompressed_bytes`, `max_bundle_documents`). Raise
  them deliberately: they are what keeps a compression bomb from being unpacked.

## 5. Kill switch

To stop serving without tearing down state: scale the Cloud Run / Agent Runtime deployment
to zero, or remove the app service account's `roles/aiplatform.user` binding. The audit
trail and case evidence remain intact.

## 6. Common failures

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `NotImplementedError` from a CLI command | `CDD_PROFILE=onprem` with placeholder adapters | Set `CDD_PROFILE=gcp` (or implement the on-prem adapter) |
| `RetrievalEmptyError` on assess | No case evidence in `enterprise-knowledge-base` for the subject | Confirm the KYC documents were ingested into `enterprise-knowledge-base` with the case ACL tag |
| Guardrail block on a benign case | Model Armor template too strict | Tune `model_armor.tf` filter confidence levels |
| VPC-SC denies the apply | Runner identity outside the perimeter | Apply with `enable_vpc_sc = false`, add the identity, re-apply true |
| `POST /v1/perpetual-kyc` returns 403 for a valid analyst | The stored baseline belongs to another tenant, or the caller holds no case-access role | Expected and correct: monitoring history is tenant-isolated. Check the caller's tenant and entitlements, never widen the record ACL |
| The perpetual-KYC queue is empty for everyone | The caller carries no tenant tag (the listing fails closed), or `monitoring_store` is bound to the `onprem` placeholder | Confirm the identity supplies a tenant; confirm `CDD_PROFILE` and the `monitoring_store` binding |
| Every perpetual-KYC signal looks `new` on every run | Baselines are not persisting: the store is not durable across replicas or restarts | On `gcp`/`platform` confirm the Firestore database and the `pkyc_baselines` / `pkyc_assessments` collections; `local` is in-process by design and resets with the process |
| A pKYC assessment shows `routed_to_hrz7: false` | `human-review-console` was unreachable when the cycle ran | The assessment is retained and still requires human review. Restore `CDD_HRZ7_URL` / `CDD_S2S_TOKEN`; the local router flushes its outbox on the next route or restart |
