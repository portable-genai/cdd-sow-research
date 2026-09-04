# GCP compliance validation: `cdd-sow-research` CDD + Source-of-Wealth Agent

This document validates the **GCP setup** (`infra/terraform/*` plus the `adapters/gcp/*`
runtime behaviour) against the project's own compliance principles (P-01..P-12, R1..R6 in
[`COMPLIANCE.md`](../COMPLIANCE.md)) and against common cloud good practice for a regulated,
in-region KYC workload (MAS by default, plus HKMA/APRA/FSA/FCA-style residency expectations).

Scope: the managed `gcp` profile only. The `local`, `platform`, and `onprem` profiles are
out of scope here. Some controls are intentionally delegated to sibling platform services
(`enterprise-knowledge-base` store, `agent-observability`, `compliance-advisory` compliance) and are noted as boundary items.

Status legend: **Met** (control present and correct), **Accepted** (a hardening was
considered and a deliberate choice was made to leave it), **Boundary** (delegated to a
sibling service by design).

> The data in `tests/` and `eval/` is fictional. This is a reference build, not certified
> for live customer data without independent legal, security, and model-risk sign-off.

## 1. Summary verdict

The GCP setup **meets every in-repo compliance principle** that applies to the managed
stack, with four defence-in-depth hardenings plus a deploy-time region selection and
fail-fast validation.

The controls behind that verdict:

- **Region selectable at deploy, fail fast (P-03).** The region is not hard-pinned. It is
  chosen at deploy time (`var.region`, default `asia-southeast1`) and
  validated against a residency allowlist (`var.allowed_regions`). An unapproved region fails
  at `terraform plan`, before anything is created. The app reads the same region via
  `CDD_REGION`, and the Model Armor host plus Document AI / KB locations track it.
- **Org Policy residency lock (`org_policy.tf`).** `gcp.resourceLocations` restricts resource
  creation to the selected region's location group; service-account key creation is disabled;
  uniform bucket-level access is required; optional domain-restricted sharing.
- **Explicit CMEK on Document AI (`document_ai.tf`).** The processor is bound to the regional
  CMEK key, so KYC document bytes are customer-key-encrypted end to end.
- **VPC-SC dry-run first, then enforce (`vpc_sc.tf`).** A `vpc_sc_enforce` toggle runs the
  perimeter in audit mode first; an optional access level admits named operator/CI identities.
- **Security alerting (`monitoring.tf`).** Log-based metrics + alert policies for guardrail
  blocks, service-account key creation, VPC-SC denials, and CMEK changes.

One hardening was considered and **deliberately not taken**: HSM-backed CMEK. The keys remain
`SOFTWARE` (still CMEK, still regional, still customer-managed) by decision; HSM
(`protection_level = "HSM"`) remains a low-effort future option if assurance requirements rise.

## 2. Compliance controls that are met

| Control | Requirement | Status | Evidence |
|---|---|:--:|---|
| **P-03 Residency (region)** | In-region; fail fast on a non-approved region | Met | `variables.tf` (`region` validated against `allowed_regions`, cross-variable validation at plan time), `providers.tf` (no global default), every resource uses `var.region`, `outputs.tf` echoes the Document AI location |
| **P-03 Residency (hard lock)** | Resource creation cannot escape the region | Met | `org_policy.tf` `gcp.resourceLocations` = `in:${region}-locations` |
| **P-03 Exfiltration** | Boundary around the data plane, rolled out safely | Met | `vpc_sc.tf` perimeter over 11 APIs, `vpc_sc_enforce` dry-run-then-enforce, optional operator access level |
| **P-09 CMEK (regional, explicit)** | One regional key, bound per service | Met | `kms.tf` regional key (90-day rotation, `prevent_destroy`) with explicit bindings for Document AI, Vertex/Agent Runtime, Logging, GCS (staging + sanctions); `document_ai.tf` now sets `kms_key_name` on the processor |
| **P-07 / R2 WORM audit** | Immutable audit, six-month default retention | Met | `logging_worm.tf` locked bucket, `retention_days >= 180` validated, CMEK on the bucket, sink captures the app audit log plus all Cloud Audit Logs |
| **P-08 Data-access logging** | Reads are audited | Met | `logging_worm.tf` `google_project_iam_audit_config` enables DATA_READ, DATA_WRITE, ADMIN_READ |
| **P-08 Detection** | Security events are surfaced, not just stored | Met | `monitoring.tf` log-based metrics + alert policies (guardrail blocks, SA-key creation, VPC-SC denials, CMEK changes) |
| **P-04 / R1 PII redaction** | Scrub PII before model, index, audit, span | Met | `dlp.tf` inspect + deidentify templates (PERSON_NAME, EMAIL, PHONE, PASSPORT, CREDIT_CARD, IBAN, custom SG NRIC/FIN), `include_quote = false` |
| **P-04 Content-free tracing** | No prompt/response text in spans | Met | `adapters/gcp/cloud_trace_tracer.py` sets only structural attributes |
| **R1 Guardrail** | Screen input and output | Met | `model_armor.tf` prompt-injection/jailbreak, malicious URI, RAI filters |
| **P-01 Managed-first / minimal surface** | Enable only what is used | Met | `apis.tf` enables exactly the pinned-stack services (plus Monitoring for the alerts); preview model OFF by default |
| **P-06 Least privilege / key hygiene** | Scoped identities, no exportable keys | Met | `iam.tf` scoped app + runtime SAs; `sanctions_sync.tf` separate sync + scheduler SAs; `org_policy.tf` disables SA key creation |
| **Reproducible screening** | Point-in-time, in-region watchlist | Met | `sanctions_sync.tf` versioned + CMEK bucket, daily poll-and-diff (`scheduler_time_zone` variable), gcp provider reads the cached snapshot |

## 3. Accepted choices and boundary items

- **CMEK protection level = SOFTWARE (Accepted).** Considered HSM; chose to keep SOFTWARE.
  Still regional customer-managed CMEK. `protection_level = "HSM"` in `kms.tf` is the upgrade
  path if HSM assurance becomes a requirement.
- **`enterprise-knowledge-base` store (R3 case-scoped ACL) (Boundary).** Retrieval storage, its CMEK, and
  the `case:<subject_id>` ACL enforcement live in `enterprise-knowledge-base`, not this repo. Verify residency and CMEK
  in `enterprise-knowledge-base`'s own setup.
- **`agent-observability` / `compliance-advisory` compliance / `agent-registry` (Boundary).** Consumed via `platform` adapters;
  their residency and retention are those services' responsibility.
- **Agent Runtime (reasoningEngine) (Boundary).** Created by the Agent Platform SDK at deploy
  time; `agent_runtime.tf` reserves only the CMEK staging bucket and the runtime SA. Confirm
  the reasoningEngine is deployed in the selected region.
- **Sanctions-sync internet egress.** The job's reach to public publisher domains
  (OFAC/UN/EU/UK) is a VPC firewall / Cloud NAT concern, not a VPC-SC egress policy (VPC-SC
  governs Google-API access across perimeters, not arbitrary internet egress). Run the job in
  a subnet whose egress allows those hosts, or mirror the files into an in-perimeter bucket.

## 4. Operator rollout notes

- **Pick the region deliberately.** Set `region` (and `scheduler_time_zone`, and `CDD_REGION`
  for the app) together. If the region is not in `allowed_regions`, `terraform plan` fails;
  extend the allowlist only after confirming stack availability and residency obligations.
- **Roll out VPC-SC in two steps.** Apply with `vpc_sc_enforce = false` (dry-run), watch the
  `vpc_sc_denials` alert, add operators to `operator_members`, then set `vpc_sc_enforce = true`.
- **Wire a notification channel.** Set `alert_notification_channels` so the security alert
  policies actually notify; without it they are created but notify nowhere.
- **Org Policy needs `roles/orgpolicy.policyAdmin`** on the project for the apply identity.

## 5. How to read this against COMPLIANCE.md

[`COMPLIANCE.md`](../COMPLIANCE.md) maps each principle to a control. This document is the
**evidence-level validation** of the GCP slice of that map: it confirms the Terraform and
adapter code implements each managed-stack control, records the four defence-in-depth
hardenings added as a result, and notes the one hardening (HSM keys) deliberately deferred.
