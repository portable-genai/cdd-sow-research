# Adopting this repo as your base

This repository is a **common base** that BFSI institutions (and other regulated
industries) fork to build their own document-diligence agents: CDD/KYC, credit-memo
review, trade-finance checking, insurance-claims triage, ESG due diligence. It ships a
reusable hexagonal core (a pure-stdlib domain, typed ports, swappable adapter profiles, a
green offline gate) plus a fully worked CDD + Source-of-Wealth vertical you can keep,
replace, or learn from.

This guide is the step-by-step for making it yours. It has two halves: a **mechanical
rebrand** (one script) and the **human decisions** the script cannot make for you.

> Related reading: [`ARCHITECTURE.md`](../ARCHITECTURE.md) §1.1 (kernel vs vertical),
> [`CONTRIBUTING.md`](../CONTRIBUTING.md) (adding a port / sub-service), the
> [`faq/`](faq/) directory.

---

## 1. What you keep vs what you rewrite

The domain is split so the boundary is explicit (ARCHITECTURE §1.1):

| Layer | Where | For a new vertical |
|---|---|---|
| **Kernel** (vertical-neutral) | `domain/kernel.py`, `domain/serialization.py`, `domain/value_bands.py`, the engine mechanics in `gap_analysis.py` / `source_of_funds_service.py`, the generic ports (LLM, guardrail, redaction, audit, tracer, eval, identity) | keep untouched |
| **Policy** (your numbers) | `domain/policy.py` populated from the `policy:` section of `config/settings.yaml` | change by config, not code |
| **Vertical** (CDD/SoW artifacts) | `domain/models.py`, the narrating services, `domain/prompts.py`, the local fixtures, the eval golden set, the UI dossier views | rewrite for your artifacts |

If your product is another *document-diligence* vertical, most of the kernel and the
deterministic reconciliation/gap engine transfer directly; you replace the artifact models
and the prompts, and retune the policy and taxonomy.

## 2. Core-vs-adopter-owned files (so upstream merges stay mechanical)

Upstream keeps evolving these; avoid diverging from them so you can pull fixes cleanly:

- **Upstream-owned** (take our changes): `domain/kernel.py`, `ports/`,
  `tests/contract/`, the eval harness (`eval/run_eval.py` mechanics), CI workflows, the
  hexagon wiring (`config.py` `Container`).
- **Adopter-owned** (yours; expect to edit): `config/settings.yaml` *values*, the local
  fixtures and sanctions snapshot, `adapters/onprem/*`, UI theming/branding, the golden
  eval dataset, `COMPLIANCE.md` jurisdiction rows.

Track upstream via git tags; rebase your
adopter-owned changes onto each release rather than merging `main` continuously.

## 3. The mechanical rebrand (one script)

`scripts/rename_fork.py` rewrites the package name, CLI entry point, `CDD_` env prefix, and
resource ids across the tree in one pass. Preview first, then apply:

```bash
# Preview (writes nothing):
python scripts/rename_fork.py --package acme_kyc_agent --cli acme-kyc \
    --env-prefix ACME --resource acme-kyc-agent --dry-run

# Apply:
python scripts/rename_fork.py --package acme_kyc_agent --cli acme-kyc \
    --env-prefix ACME --resource acme-kyc-agent --yes

# Then recreate the environment (the distribution name changed) and prove it is green:
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
make lint test eval
```

Add `--include-docs` to sweep Markdown prose too. The script deliberately does NOT touch
the human decisions below.

## 4. The human decisions (the script can't make these)

1. **Region / residency.** Set `CDD_REGION` (now `<PREFIX>_REGION`) and the Terraform
   `region`/`tfvars` to your in-country region. The build defaults to `asia-southeast1`
   (MAS / Singapore). See [`docs/runbook.md`](runbook.md).
2. **Identity / IdP.** `identity.trusted_issuers` is empty by default (Mode 6 login is
   disabled until you configure an issuer). Wire your OIDC issuer(s) and set the session
   signing key env var(s). Issuers must be https. See
   [`docs/embedding-and-identity.md`](embedding-and-identity.md).
3. **PII / jurisdiction pack.** Set `pii.jurisdictions` (and `CDD_PII_JURISDICTIONS` for
   the eval gate) so redaction and the `pii_safety` metric detect YOUR national
   identifiers, not just the Singapore NRIC. Add a pattern pack to
   `domain/pii_patterns.py` if your jurisdiction is not yet listed.
4. **Risk policy.** Own the numbers under `policy:` in `config/settings.yaml` (scorecard
   weights, gap tolerances, review cadences, FATF country lists, escalation bands). The
   defaults are a reference, not your policy.
5. **Reference data is fictional.** The bundled sanctions snapshot
   (`adapters/local/data/sanctions_snapshot.json`) and every fixture use obviously-fake
   names. Replace the snapshot via `scripts/sync_sanctions.py` and swap the fixtures for
   your own synthetic data. **Do not run against live customer data without your own
   legal, security and model-risk sign-off.**
6. **Eval golden set.** Rebuild `eval/datasets/` and the rubrics for your vertical: a fork
   inherits a green gate that measures the WRONG thing until you do. The gate structure is
   generic; the golden cases are yours.
7. **Deployment posture.** Review the Dockerfile (digest-pinned base, non-root),
   `infra/terraform/` (Org Policy, CMEK, VPC-SC, WORM), and the loopback-by-default
   binding before you expose anything.

## 5. Do not duplicate the platform

This repo is one system in a catalog of composable GRC systems. Several concerns it
*touches* are owned by sibling platform services, and you should integrate rather than
rebuild them (see [`docs/faq/features-faq.md`](faq/features-faq.md) for the full map):
the guardrail gateway (`agent-guardrail-gateway`), the governed knowledge base (`enterprise-knowledge-base`), the agent registry
(`agent-registry`), the AI-quality / eval gate (`model-quality-gate`), observability + WORM audit (`agent-observability`), the
compliance assistant (`compliance-advisory`), and the on-prem DLP gate (`onprem-dlp`). The `platform` profile's
adapters are already thin HTTP clients to those services.

## 6. Adoption checklist

- [ ] Ran `scripts/rename_fork.py`, recreated the venv, `make lint test eval` green.
- [ ] Set region + Terraform tfvars to your in-country region.
- [ ] Wired your OIDC issuer(s) and session signing key(s).
- [ ] Set `pii.jurisdictions` + added a pattern pack if needed; `pii_safety` exercises your ids.
- [ ] Owned the `policy:` numbers with your compliance function.
- [ ] Replaced the sanctions snapshot and every synthetic fixture.
- [ ] Rebuilt the eval golden set + rubrics for your vertical.
- [ ] Reviewed the deploy posture (Dockerfile, Terraform, bind address).
- [ ] Decided which sibling platform services you integrate vs stub.
- [ ] Recorded your baseline upstream tag so you can take future fixes.
