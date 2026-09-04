# Compliance FAQ

For compliance, MLRO, and model-risk teams assessing the repo's regulatory posture.
Cross-references: [`COMPLIANCE.md`](../../COMPLIANCE.md) (the full principle→control map and
the MAS 626 crosswalk appendix), [`SPEC.md`](../../SPEC.md).

### Is this making regulatory decisions autonomously?

No. It is a **decision-support** agent (P-05): every consequential output requires human
review (maker-checker, P-06). The deterministic engines produce a documented, replayable
assessment; a qualified human (analyst / MLRO) disposes. Sanctions/terrorism hits, a
HIGH/PROHIBITED band, open screening alerts, or an overdue review escalate to enhanced
review, never to auto-execution.

### How is customer PII handled?

Redact-before-everything (P-04, R1): the orchestrator redacts case inputs before any model,
index, registry or audit call. National-identifier detection is **jurisdiction-driven**
(`pii.jurisdictions` in `config/settings.yaml`, `domain/pii_patterns.py`) so a non-Singapore
deployment scrubs, and gates on, its own identifiers (PAN, Aadhaar, NINO, NIK, ...), not
just the SG NRIC. The runtime guardrail/DLP itself is the sibling `agent-guardrail-gateway`; this repo
consumes it rather than re-implementing it.

### How is the work auditable / reproducible?

Every assessment writes an immutable, already-redacted WORM `AuditEvent` with the decision
and the citation set (P-07). Every dossier statement carries a source-and-page `Citation`
(P-10). The consequential math is deterministic, so an auditor can recompute any figure or
decision from the same inputs. The enterprise WORM audit system is `agent-observability`; the in-repo
hash-chained store is the offline/local stand-in (see
[security-faq.md](security-faq.md) for its exact tamper-evidence limits).

### What is the model-risk story?

An offline eval gate (`eval/run_eval.py`) scores groundedness, risk-band accuracy, citation
accuracy, and PII safety against a golden set, failing the build below threshold (P-08). The
enterprise promotion gate and model documentation / red-team harness are the sibling `model-quality-gate`
system; this repo's gate mirrors its thresholds so merges are guarded locally. A fork must
rebuild the golden set for its own vertical, or the gate measures the wrong thing.

### Which regulators does this map to?

`COMPLIANCE.md` maps the internal P-01..P-12 / R1..R6 controls to concrete code, plus an
**adopter-owned regulator crosswalk appendix** with the MAS (Singapore) reference mapping as
the template. To add FCA / RBI / OJK / HKMA / APRA, copy the appendix table, swap the
regulator-reference column, and re-review with local counsel, the `cdd-sow-research`-control column is
stable across regulators. At scale, the sibling **the cloud control-mapping toolkit control-mapping toolkit** and **`compliance-advisory`** generate and maintain these crosswalks; a large estate should
integrate them rather than hand-maintain the table.

### Is data residency enforced?

Yes, at deploy time: a single approved region (default `asia-southeast1`),
validated to fail fast, with regional endpoints, a `gcp.resourceLocations` Org Policy
allowlist, CMEK, and a VPC-SC perimeter (P-03, P-09). The residency-violation CI gate is the
sibling **the data-residency validator residency validator**; the exit/concentration-risk plan is **the exit-and-portability planner**. This repo
enforces residency in its own infra and is one of the systems those tools reason about.

### Can we run it against real customer data today?

Not without your own legal, security, and model-risk sign-off. Every fixture and the bundled
sanctions snapshot are obviously-fictional, and the docs state throughout that this is a
reference build. The adoption checklist (`docs/ADOPTING.md` §6) lists the steps, replace
reference data, own the risk policy, wire your IdP, rebuild the eval golden set, that must
precede any live-data use.

### Which CDD/AML lifecycle stages does it cover, and which does it not?

It covers onboarding-time CDD + Source of Wealth, with case-level screening, scorecard, and
ongoing-monitoring sub-services. Enterprise-scale sanctions hit disposition (**G2**),
event-driven perpetual-KYC re-rating of the existing book (**G6**), deep UBO structure
unwrap (**G7**), and transaction-monitoring alert triage (**G1**) are adjacent catalog
systems, not this repo's job. See [features-faq.md](features-faq.md) for the boundary.
