# Features FAQ

For product, compliance, and delivery teams: what this agent does, what is deterministic vs
LLM, and, importantly, where its responsibilities **stop** and a sibling catalog system
takes over. Cross-references: [`README.md`](../../README.md), [`DEMO.md`](../../DEMO.md),
[`docs/sow-longitudinal-audit-design.md`](../sow-longitudinal-audit-design.md).

### What does `cdd-sow-research` actually produce?

A cited **CDD dossier** and a long-running **Source-of-Wealth case**. From a customer's KYC
pack, corporate registries and adverse media it produces: a source-of-wealth narrative, a
risk rating, adverse-media findings, and a beneficial-ownership/UBO summary, every claim
carrying a source-and-page `Citation`, with a full WORM audit trail. The SoW *case* flow
adds the enhanced-diligence panels: key-individuals CDD roll-up, sanctions/PEP screening, a
risk scorecard + CDD tier, Source of Funds, and ongoing monitoring / periodic review.

### What is deterministic vs done by the LLM?

The consequential math is **deterministic and replayable** (pure stdlib, unit-tested): the
reconciliation and gap engine, the Source-of-Funds reconciliation, the risk scorecard and
CDD tiering, the review cadence and event triggers, name-screening match scoring, and the
maker-checker escalation policy. The LLM only **narrates and drafts** (the SoW narrative,
RFI wording) and **classifies/triages**. An auditor can recompute every decision without the
model. This is by design (the "deterministic domain service" pattern).

### Is anything auto-approved?

No. Every consequential output sets `requires_human_review=True` (maker-checker, P-06); the
agent proposes and a qualified human (analyst / MLRO) disposes. Escalation signals
(sanctions/terrorism hits, HIGH/PROHIBITED band, open screening alerts, overdue reviews)
*raise* the review bar; they never lower it and never auto-execute.

### Which capabilities does this repo own vs integrate from the catalog?

This is one system in a catalog of composable GRC systems. It **owns** the CDD/SoW domain
logic and its outputs. It **integrates** (via the `platform` profile's HTTP adapters)
several cross-cutting concerns that are owned by sibling platform systems, do not rebuild
these in a fork:

| Concern | Owned by (catalog id / repo) | `cdd-sow-research`'s role |
|---|---|---|
| Runtime guardrail: PII redaction, prompt-injection / jailbreak defense | `agent-guardrail-gateway` | consumes it on every assessment (input + output screen) |
| Governed RAG / ACL-aware knowledge base with citations | `enterprise-knowledge-base` | ingests case docs into it, retrieves grounded passages from it |
| Agent registry, versioning, identity, entitlements | `agent-registry` | publishes its A2A AgentCard for discovery |
| AI-quality / eval / model-risk promotion gate | `model-quality-gate` | its eval metrics gate promotion; the offline gate mirrors it |
| Observability + immutable WORM prompt/response audit | `agent-observability` | writes audit events to it; traces spans through it |
| Regulatory Q&A / CDD-AML control checklists | `compliance-advisory` | consumes it for regulatory compliance checks |
| On-prem, CPU-only DLP scrub before egress | `onprem-dlp` | the sovereign-DLP option behind the redaction port |

So the guardrail, knowledge base, audit sink, and eval platform are *dependencies*, not
features of this repo. `cdd-sow-research`'s own screening/scorecard/monitoring sub-services are
case-level diligence logic, distinct from the platform's runtime controls.

### How does this relate to the other financial-crime systems in the catalog?

`cdd-sow-research` is onboarding-time CDD + Source of Wealth. Adjacent (mostly proposed) FCC systems
handle different points in the lifecycle and should not be duplicated here: **G2**
sanctions-screening copilot (hit disposition at scale), **G6** perpetual-KYC re-rating
(event-driven re-assessment of the existing book), **G7** UBO structure unwrap (deep
ownership traversal), and **G1** AML alert triage (transaction-monitoring alerts). `cdd-sow-research`'s
built-in screening and monitoring are the case-level slice; the dedicated FCC systems are
the enterprise-scale versions. Check
[the organization's repository index](https://github.com/portable-genai) before building a
capability that may already have a home.

### Can I use this for a non-KYC document-diligence product?

Yes, that is the point of the kernel/vertical split. The reusable core (citations,
grounding, the reconciliation/gap engine, audit, eval, maker-checker) transfers to credit-
memo review, trade-finance checking, claims triage, ESG due diligence, and similar. You
replace the artifact models and prompts and retune the policy/taxonomy. See
[`docs/ADOPTING.md`](../ADOPTING.md) and [adoption-faq.md](adoption-faq.md).

### How do I see it working?

`make demo` runs the presenter-controlled walkthrough (one command, ten steps, narration on
the terminal). `DEMO.md` documents the offline case demo, the managed-GCP one-shot demo, and
the portability tour. Everything in the walkthrough runs on synthetic, fictional data with
no cloud and no API key.
