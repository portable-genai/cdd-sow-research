# cdd-sow-research

The shared working agreement is [`.github/AGENTS.md`](https://github.com/portable-genai/.github/blob/main/AGENTS.md).
It carries the architecture rules, the gate contract, the fleet invariants, the
falsification discipline, versions and house style, and it holds in every repository
here. Read it first. This file carries only what is specific to this one.

## What this is

**Doc1** (catalog id) is a grounded **CDD + Source-of-Wealth agent** for
financial-crime / KYC work. It turns a customer's KYC pack, corporate registries and
adverse media into a cited **CDD dossier** (`CDDCase`): a source-of-wealth narrative, a
risk rating, adverse-media findings, and a beneficial-ownership/UBO summary, every claim
carrying a source-and-page `Citation`, with a full WORM audit trail. It is a
ports-and-adapters reference build targeting the Gemini Enterprise Agent Platform on GCP,
defaulting to `asia-southeast1` (Singapore) for data residency since 2026-08-27. The earlier
`us-central1` pin rested on the claim that not all services this repo depends on run in
`asia-southeast1`; the availability check falsified it.

It is an engineering portfolio piece, not affiliated with Google.

## Commands

The default dev/test/CI profile is **`local`**: a real, working, SDK-free offline stack.
You almost never need Google Cloud installed.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # core + dev tooling, NO google-cloud-* (those live in the [gcp] extra)

make lint        # ruff check + ruff format --check + mypy src
make test        # CDD_PROFILE=local pytest -m 'not integration' -q   (unit + contract)
make eval        # python eval/run_eval.py: the quality gate; non-zero exit fails
make fmt         # ruff format + ruff check --fix (auto-fix)
make run-api     # uvicorn on :8090 (REST + A2A AgentCard at /.well-known/agent-card.json, OpenAPI at /docs)
make run-ui      # Next.js dev server (ui/)

# Run a single test:
pytest tests/unit/test_cdd_service.py -q
pytest tests/unit/test_cdd_service.py::test_name -q
pytest -k "gap_analysis" -q

# CLI (entry point `cdd-sow = cdd_sow_research.cli.main:app`):
cdd-sow assess "Acme Holdings Pte Ltd (FICTIONAL)" --type entity --jurisdiction SG
cdd-sow source-of-wealth ... | cdd-sow adverse-media ... | cdd-sow serve | cdd-sow eval
```

**The PR gate** (must be green; CI runs the same on `local` via `.github/workflows/ci.yaml`
and `eval-gate.yaml`):
```bash
ruff check src tests && ruff format --check src tests && mypy src && \
  pytest -m 'not integration' -q && python eval/run_eval.py
```

Notes:
- **Ruff is pinned exactly** (`ruff==0.15.18`) because formatter output drifts between
  releases and `ruff format --check` would fail CI. Bump deliberately.
- Tests use `:memory:` SQLite and are deterministic. Integration tests (`@pytest.mark.integration`)
  need live GCP and are deselected by default.
- `make run-api`/CLI honour `PROFILE`/`CDD_PROFILE`. The longitudinal SoW demo:
  `PYTHONPATH=src python scripts/sow_demo.py` (render with `scripts/render_sow_ui.py`).

## Architecture: the hexagon

Pure-domain core speaks only to **ports** (`typing.Protocol`s); four baseline adapter
families implement them, while `live` is an intentional hybrid that reuses local and GCP
adapters. Switching runtime bindings needs no domain edits and provides bounded seam
evidence for P-02.

**Profiles** (`CDD_PROFILE`, or `profile:` in `config/settings.yaml`):
- `local`: the WORKING offline stack. SQLite FTS5 KB (self-seeding), deterministic schema-driven
  LLM, heuristic guardrail, regex DLP, hash-chained append-only SQLite audit (verify/export/
  restore via `cdd-sow audit`), no-op tracer. **No GCP SDK.** Default for dev/test/CI.
- `gcp`: real managed services (Document AI, Agent Search, Gemini, Model Armor, DLP, Cloud
  Logging WORM, Cloud Trace, Gen AI Evals). Production deploys select it explicitly
  (`CDD_PROFILE=gcp` in the `Dockerfile`/runbook). An unset `CDD_PROFILE` is not a choice
  of `local`: it binds the SDK-free adapters so an offline process still starts, but every
  relaxation is withheld (no seeded personas, no localhost CORS fallback, no dev-persona
  header, rate limit on).
- `live`: local document custody and local model inference, with selected managed
  public-source adapters receiving subject names only.
- `platform`: thin HTTP delegates where sibling contracts exist, plus managed adapters
  for vertical-owned capabilities. Priority 1 makes every reuse explicit.
- `onprem`: `NotImplementedError` placeholder stubs that still satisfy every Protocol
  (the sovereign-exit / portability story, P-12); a primary CLI command exits 2.

**Layout** (`src/cdd_sow_research/`):
- `domain/`: pure stdlib, **no cloud/framework imports**. Split kernel-vs-vertical
  (ARCHITECTURE §1.1): `kernel.py` holds the vertical-neutral types any fork reuses
  (citations, LLM envelope, guardrail/redaction, `AuditEvent`, `EvalReport`, `Severity`);
  `models.py` holds the CDD/SoW vertical artifacts (`CDDCase`, `SowCase`, screening,
  scorecard, SoF, monitoring) and re-exports every kernel name for compatibility.
  `policy.py` holds the bank-owned policy dataclasses (tolerances, weights, cadences,
  country lists, escalation bands) populated from the settings `policy:` section; the
  taxonomy enums are `StrEnum`s and the engines are typed on plain `str` kinds, so
  taxonomy/policy changes need no engine edits. `cdd_service.py` is the orchestrator;
  sub-services are one-per-module (`sow_service`, `risk_service`, `adverse_media_service`,
  `ownership_service`, plus the longitudinal-case services: `sow_case_service`,
  `gap_analysis`, `rfi_drafting`, `screening`, `scorecard_service`, the perpetual-KYC
  pair `perpetual_kyc` (the pure engine) + `perpetual_kyc_service` (its orchestrator), and
  the UBO-graph pair `ubo_graph` + `ubo_graph_service` in the same shape).
  `services.py`
  re-exports them as the single import surface for the wiring layers. `prompts.py`,
  `review_policy.py`, `errors.py`.
- `ports/`: 21 `@runtime_checkable` Protocols (the hexagon boundary), re-exported from
  `ports/__init__.py`.
- `adapters/{gcp,live,local,platform,onprem}/`: baseline adapter families plus the `live`
  implementations it needs; `live` intentionally reuses reviewed local/GCP bindings for
  other ports.
- `config.py`: `Settings` (loaded from `config/settings.yaml` with `${ENV_VAR:-default}`
  interpolation) and `Container` (lazy DI: one `cached_property` per port; `_bind` resolves
  the dotted `module:Class` path for the active profile, falling back to `gcp`).
- `agent/` (ADK `LlmAgent` + A2A/MCP wiring, lazy so importing never pulls in ADK),
  `api/` (FastAPI; `deps.py` assembles services from the `Container`), `cli/` (Typer).

**Wiring contract:** services take *explicit port instances* in their constructors (no
service-locator inside the domain). `api/deps.py` `build_cdd_service(container)` is the
single place that knows which ports each service needs; `tests/conftest.py` assembles the
same way using the real `local` adapters wrapped in thin recording subclasses.

**Request pipeline** (`CddService.assess`, all under a tracer span, see `cdd_service.py`):
redact (P-04) → guardrail INPUT → extract+ingest each KYC doc into the KB with `case:<id>`
ACL → retrieve grounding passages (empty ⇒ hard error, never ungrounded) → adverse media +
ownership → SoW narrative (LLM) → risk rating → compliance check → assemble `CDDCase` →
guardrail OUTPUT → review policy (always `requires_human_review=True`) → WORM audit
(already-redacted). Extraction/ingestion/compliance failures degrade gracefully; a blocked
input and an ungrounded case are hard errors.

## Conventions you must follow

These are enforced by the contract test (`tests/contract/test_port_parity.py`) and the
gate. Break them and CI fails:

- **Keep `domain/` pure stdlib.** No `google-cloud-*`, `google-adk`, `google-genai`,
  `fastapi`, `httpx`, or `pydantic` imports under `domain/`. Everything external is a port.
- **GCP imports are lazy.** In `adapters/gcp/*` (and `agent/`), every Google import lives
  inside a method/`__init__` or under `TYPE_CHECKING`, never at module top level. The
  `local`/`onprem` profiles must import every module with no GCP SDK installed.
- **One adapter constructor:** `def __init__(self, settings: Settings)`, exactly one
  positional `Settings`. The dotted path in `config/settings.yaml` under `adapters:` is the
  binding contract.
- **Every port is `@runtime_checkable`**, and every port needs both an `onprem` and a
  `local` binding (the parity test asserts this).
- **Cite every claim** (each dossier statement carries a `Citation` with source + page),
  **redact before everything** (R1: PII removed at the boundary before any model/index/
  registry/audit call), and **maker-checker** (P-06: a dossier always sets
  `requires_human_review=True`).
- **Region pinning:** every REGIONAL service/SDK call targets the one deploy region (default
  `asia-southeast1`); never use a floating/global endpoint or the floating ADK default
  model. Three locations are deliberately NOT the deploy region and have their own selectors,
  because the services do not serve every region: `models.location`, `document_ai.location`
  (the `us` multi-region until the Single Region Request Form is granted) and
  `knowledge_base.location` (Agent Search serves only `global`/`us`/`eu`). Each is a stated
  deviation, not a fallback to take silently. Models are pinned in `config/settings.yaml`
  (`gemini-3.5-flash` reasoning, `gemini-3.1-flash-lite` triage).
- **Markdown:** validate any mermaid diagram before committing.

## Adding an adapter

1. Implement the port `Protocol` in `adapters/{gcp,live,local,platform,onprem}/`, or
   explicitly reuse a reviewed binding for the `live` hybrid.
2. Bind it in `config/settings.yaml` under `adapters:` (dotted `module.path:ClassName`).
3. Keep GCP imports lazy; the `onprem` stub must construct with a single `Settings` arg and
   satisfy the Protocol. The parity test enforces all of this.

## Platform dependencies

This repo is **Doc1**. Its platform dependencies are **Hrz1** guardrail, **Hrz2** enterprise
KB (this agent's governed RAG store), **Hrz3** registry, **Hrz4** AI-quality/eval gate,
**Hrz5** observability/audit, and **Rsk1** compliance assistant, each backed by a
`platform/` HTTP adapter.
