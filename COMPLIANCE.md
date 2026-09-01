# COMPLIANCE: Doc1 CDD + Source-of-Wealth Agent

This maps every General Principle (P-01..P-12) and dependency rule (R1..R6, R8) to a concrete
control in **this** repo. Where a principle does not apply to Doc1, it is marked **n/a** with
the reason. Doc1 handles customer KYC data, so the data-protection and audit controls are
load-bearing.

> The synthetic KYC data in `tests/` and `eval/` is **fictional**. This build is a
> reference piece and is **not** intended for live customer data without your own legal,
> security and model-risk sign-off.

---

## General Principles

| # | Principle | How Doc1 implements it | Evidence |
|---|-----------|----------------------|----------|
| **P-01** | Managed-first, minimal surface | Only the managed services the pinned stack uses are enabled; the agent is hosted on Agent Runtime | `infra/terraform/apis.tf`, `agent/root_agent.py` |
| **P-02** | No vendor lock-in (ports and adapters) | Domain depends only on `Protocol` ports; a profile switch rebinds adapters with no domain change. The `local` family proves the same domain runs entirely off-cloud (SQLite FTS5, deterministic LLM, no Google Cloud SDK) | `ports/`, `config.py`, `adapters/local/*`, `adapters/onprem/*` |
| **P-03** | Data residency (in-country) | Region selected at deploy from a residency allowlist (defaults `asia-southeast1`), validated to fail fast; regional endpoints; `gcp.resourceLocations` Org Policy; VPC-SC perimeter | `config/settings.yaml`, `infra/terraform/variables.tf`, `org_policy.tf`, `vpc_sc.tf` |
| **P-04** | Minimise PII to the model | `redaction.redact` runs before any model/index/registry/audit call; spans capture no content | `domain/cdd_service.py`, `adapters/gcp/dlp_redaction.py`, `agent/callbacks.py` |
| **P-05** | Human oversight of automation | Decision-support only; the agent proposes, a human disposes; never an autonomous approver | `domain/review_policy.py`, `agent/root_agent.py` instruction |
| **P-06** | Maker-checker | A CDD dossier always `requires_human_review=True`; HIGH/PROHIBITED or sanctions/terrorism escalate; a perpetual-KYC re-score is consequential in the same way and is likewise always flagged and queued, never acted on; every escalation is ROUTED to the Hrz7 maker-checker console (rule R8), not left as a boolean | `domain/review_policy.py`, `domain/cdd_service.py`, `domain/perpetual_kyc_service.py`, `ports/review_router.py`, `adapters/*/review_router.py` |
| **P-07** | Audited everything | Every assessment writes a WORM `AuditEvent` (already redacted) with the decision and citations. Browser-flow transitions write a sanitized atomic outbox for idempotent audit delivery | `domain/cdd_service.py`, `adapters/gcp/cloud_logging_audit.py`, `adapters/gcp/firestore_browser_flow_store.py` |
| **P-08** | Quality / model-risk gate | Offline eval gate scores groundedness, risk-band accuracy, citation accuracy, PII safety and perpetual-KYC queue placement; `pkyc_priority` is scored against the golden set's own expectation (an independent oracle) and is proven able to go red per change kind; Hrz4 at promotion | `eval/run_eval.py`, `eval/rubrics/*.yaml`, `tests/unit/test_eval_perpetual_kyc_can_go_red.py`, the hosted Cloud Build check |
| **P-09** | CMEK does not cascade | One regional CMEK key has an explicit IAM binding per service agent. A separate non-exportable asymmetric KMS key signs Mode 5 tokens, with HSM as the named-production default | `infra/terraform/kms.tf`, `adapters/gcp/kms_embed_token.py` |
| **P-10** | Provenance on every claim | Every dossier statement carries a source-and-page `Citation`; the model only cites retrieved/derived sources; every perpetual-KYC signal and queued reason carries the citation behind it | `domain/models.py` (`Citation`), `domain/_grounded.py`, `domain/perpetual_kyc.py` |
| **P-11** | Defense in depth | Domain pipeline screens and redacts; the ADK model-boundary callbacks screen, redact and audit again | `agent/callbacks.py` |
| **P-12** | Exit / portability | The `local` adapters run the whole pipeline off-cloud today (the working proof of portability), and the `onprem` placeholder adapters satisfy the same Protocols as the fail-fast sovereign migration target; the contract test proves interface parity for both. The DATA exit is executable in two open formats: the audit trail as JSON Lines with the hash chain re-verified on reload, and the complete `cdd-case-bundle/v1` archive carrying the dossier plus every source document's original bytes, reloading into a different deployment with ids and digests intact | `adapters/local/*`, `adapters/onprem/*`, `domain/case_bundle.py`, `domain/case_bundle_service.py`, `tests/contract/test_port_parity.py`, `tests/contract/test_behavioral_parity.py`, `scripts/portability_demo.py`, `docs/onprem-migration.md` |

---

## Dependency rules

Doc1 exercises the **whole platform**. Each rule is satisfied by consuming the sibling service
through a `platform` adapter (with an on-prem stub), never by re-implementing the concern.

| Rule | Requirement | How Doc1 satisfies it | Evidence |
|------|-------------|---------------------|----------|
| **R1** | Customer PII handling: Hrz1 guardrail + DLP redaction mandatory | The full safety pipeline runs on every assessment: redact, screen INPUT, screen OUTPUT | `domain/cdd_service.py`, `ports/safety.py`, `adapters/{gcp,platform}/*guardrail*`, `*redaction*` |
| **R2** | Audit to Hrz5 | Every assessment writes an immutable WORM `AuditEvent`; the `platform` adapter posts to Hrz5 `/v1/audit` | `adapters/gcp/cloud_logging_audit.py`, `adapters/platform/remote_audit.py` |
| **R3** | Governed RAG via Hrz2 | KYC documents are ingested into Hrz2 with `case:<subject_id>` ACL tags and retrieved via Hrz2 governed search | `ports/knowledge_base.py`, `adapters/platform/remote_knowledge_base.py` |
| **R4** | Register in Hrz3 | The A2A AgentCard is published and resolvable via Hrz3 (`platform` registry adapter) | `agent/agent_card.py`, `adapters/platform/remote_registry.py` |
| **R5** | Hrz4 promotion gate | `EvaluationGatePort.gate` checks the Hrz4 thresholds before promotion; the offline gate guards merges | `ports/observability.py`, `adapters/platform/remote_evaluation.py`, `eval/run_eval.py` |
| **R6** | Validated by Rsk3 at intake | As a new project, Doc1 is validated by the Rsk3 intake validator. n/a in-repo (Rsk3 is the validator, not a Doc1 dependency at runtime); Doc1 consumes **Rsk1** for regulatory CDD/AML checks | `adapters/platform/remote_compliance.py` (Rsk1), intake handled by Rsk3 externally |
| **R8** | Route `requires_human_review` to Hrz7 | Every escalated dossier, and every perpetual-KYC re-score, is submitted to the Hrz7 Human-Review & Maker-Checker Console through the shared `review-kit` client (redact-before-wire, idempotent per run); `local` enqueues to a transactional outbox so the routing path runs offline, `gcp`/`platform` submit over S2S to Hrz7's service intake | `ports/review_router.py` (`route`, `route_monitoring`), `adapters/{local,gcp,onprem}/review_router.py`, `adapters/_review_payload.py` |

---

## Specific data-protection emphasis (R1, customer KYC)

- **Redact before model, index, registry and audit (P-04).** The orchestrator redacts the
  case inputs before any outbound call. The DLP template scrubs names, emails, phone
  numbers, passport numbers, card numbers and IBANs; the national-identifier detectors are
  **jurisdiction-driven** (`pii.jurisdictions` in `config/settings.yaml`,
  `domain/pii_patterns.py`), defaulting to the Singapore NRIC/FIN and extensible per
  adopter so a non-SG deployment scrubs (and gates on) its own identifiers.
- **Case-scoped ACL (R3).** Documents are ingested into Hrz2 with `case:<subject_id>` tags;
  retrieval passes the case principals so one analyst's case cannot read another's evidence.
- **Maker-checker on a consequential output (P-06).** The dossier always requires human
  review; sanctions/terrorism hits or a HIGH/PROHIBITED band escalate to enhanced review.
- **WORM audit, page-level citations (P-07, P-10).** Every assessment is recorded immutably
  with already-redacted text and the citation set, so an MLRO can trace each finding.
- **Object-level authorization on monitoring history (R3).** Perpetual-KYC baselines and
  queue items carry the same server-derived `case:<id>` + `tenant:<tenant>` tags. A baseline
  read requires every tag (403, not a 404-shaped answer that leaks existence), and the queue
  lists only records whose tenant tag the caller holds; a record with no tenant tag is never
  listed. Proven by a cross-tenant denial test that is red without the check.
- **Fictional data only.** The synthetic KYC fixtures, and the bundled sanctions and
  adverse-media fixtures the `local` profile serves, use obviously-fake names and ids. A
  fixture hit is never a sanctions determination about a real party and must not be read as
  one. Live customer data requires sign-off before any deployment.

---

## Appendix: regulator crosswalk (adopter-owned)

The `P-*` / `R*` catalog above is this build's internal control language. A regulated
adopter must map those controls onto its own supervisor's requirements. The rows below are
the **MAS (Singapore) reference mapping** for the home jurisdiction; a fork adds a column
(or a sibling table) per additional regulator. This appendix is *adopter-owned* (see
[`docs/ADOPTING.md`](docs/ADOPTING.md)): it is a template, not legal advice, and your
compliance function owns the mapping and any gaps.

| Doc1 control | MAS reference | What a supervisor looks for |
|---|---|---|
| P-04 redact-before-everything; R1 safety pipeline | MAS Notice 626 §8 (CDD), PDPA (data protection) | PII minimised before processing; customer data protected in transit and at rest |
| P-06 maker-checker; P-05 human oversight | MAS Notice 626 §6 (senior-management oversight); MAS FEAT (Accountability) | A qualified human disposes of every consequential output; the AI is decision-support |
| P-07 WORM audit; P-10 provenance | MAS Notice 626 §11 (record-keeping, 5 years); MAS TRM (auditability) | Immutable, reproducible records; every finding traceable to its source |
| Screening sub-service; escalation policy | MAS Notice 626 §7 (sanctions/PEP), MAS AML/CFT guidelines | Sanctions/PEP screening against a versioned list; hits dispositioned under four-eyes |
| Risk scorecard + CDD tier | MAS Notice 626 §6 (risk-based approach) | A documented, replayable customer-risk assessment driving the CDD/EDD tier |
| Ongoing monitoring / periodic review | MAS Notice 626 §9 (ongoing monitoring) | Risk-based review cadence plus event-driven re-review triggers |
| P-03 residency; P-09 CMEK; P-12 exit | MAS Outsourcing / Cloud guidelines, MAS TRM | In-country data residency, customer-managed keys, a demonstrable exit/portability plan |
| P-08 quality/model-risk gate | MAS FEAT (Fairness, Ethics, Accountability, Transparency); MAS model-risk expectations | A promotion gate with groundedness/accuracy/safety metrics and model documentation |

**To add another regulator** (FCA, RBI, OJK, HKMA, APRA, ...): copy this table, replace the
"MAS reference" column with that supervisor's instrument and section numbers, and re-review
the "what a supervisor looks for" column with local counsel. The Doc1-control column is
stable across regulators; only the mapping changes. The sibling **Rsk2 control-mapping
toolkit** and **Rsk1 compliance assistant** exist to generate and maintain these crosswalks
at scale, so a large estate should integrate them rather than hand-maintaining this table.
