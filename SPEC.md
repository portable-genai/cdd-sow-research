# SPEC: Doc1 CDD + Source-of-Wealth Agent

The authoritative build specification: locked decisions, the pinned stack, the adapter
convention, the assessment pipeline, and the HTTP contracts Doc1 consumes and defines. The
README and ARCHITECTURE describe how the pieces fit; this file is the contract.

---

## 1. Scope and catalog identity

Doc1 is a grounded-research agent that turns a customer's KYC pack, corporate registries and
adverse media into a cited **CDD dossier**: a source-of-wealth narrative, a risk rating,
adverse-media findings, and a beneficial-ownership / UBO summary, with a full audit trail.

- Repo: `cdd-sow-research` (public, Apache-2.0). Package: `cdd_sow_research`.
- Catalog identity: **Doc1**, group `doc` (document-heavy verticals), priority **P1**, buyer
  **Financial Crime / Private Bank**.
- CLI entry point: `cdd-sow = cdd_sow_research.cli.main:app`.
- Profile env var: `CDD_PROFILE` (`gcp` | `local` | `live` | `platform` | `onprem`).
  Channel and identity are selected independently by `CDD_CHANNEL_PROFILE` and
  `CDD_IDENTITY_PROFILE`; no selector infers or falls back to another axis.
  `config/settings.yaml` defaults to `local`; the production Dockerfile sets `gcp`
  explicitly. Service port default: **8090**.

### Deployment profiles

| Profile | Backends | Use |
| --- | --- | --- |
| `gcp` | Managed Google Cloud (Agent Search, Gemini, Model Armor, DLP, Document AI, Cloud Logging WORM, Cloud Trace, Gen AI eval); lazy SDK imports | Production on the Gemini Enterprise Agent Platform |
| `local` | SDK-free, deterministic, in-process: SQLite FTS5 knowledge base, schema-driven LLM, heuristic guardrail, regex DLP, hash-chained append-only SQLite audit (JSONL export/restore via `cdd-sow audit`), no-op tracer, local doc parser, offline eval gate | A WORKING laptop run, no Google Cloud, no API key, no emulators |
| `live` | Local document custody and local model inference, with selected cloud web sources receiving subject names only | Higher-fidelity operator run; secure identity must be selected explicitly for any non-loopback deployment |
| `platform` | Sibling-service HTTP clients where contracts exist, plus explicit managed adapters for vertical-owned capabilities | The full deployed platform |
| `onprem` | Fail-fast placeholders: consequential operations raise `NotImplementedError`; explicitly reviewed optional ports may return safe defaults | The Google Distributed Cloud / sovereign migration target |

Under `local`, the platform-client ports (Hrz2 knowledge base, Hrz1 guardrail/DLP, Hrz3 registry,
Hrz5 audit, Hrz4 eval, Rsk1 compliance) use **in-process** local implementations, not HTTP to
siblings: a laptop runs one app, not the whole platform. The default `local` path imports
no google-cloud package. Optional higher-fidelity local runs route the in-process stores to
Google's official emulators when `FIRESTORE_EMULATOR_HOST` (or `PUBSUB_EMULATOR_HOST` /
`STORAGE_EMULATOR_HOST`) is set AND the `[gcp]` client lib imports; the google client is
imported lazily, only on that branch. There is no emulator for Agent Search, Gemini, Model
Armor, DLP or Document AI, so those stay on the SDK-free workaround.

Doc1 handles **customer PII** (KYC), so rule **R1** applies: the full Hrz1 guardrail plus DLP
redaction pipeline is mandatory. The dossier is consequential output, so P-06 (maker
checker) applies: the dossier always requires human review.

---

## 2. Locked decisions

- **Hexagonal ports-and-adapters.** The domain core (`domain/`) is pure standard library:
  no Google Cloud, ADK, FastAPI, httpx or pydantic imports. Everything external is a port
  (`ports/`, a `@runtime_checkable typing.Protocol`).
- **Region selected at deploy** from a residency allowlist (`var.allowed_regions`), defaulting
  to the selected deployment region (default `asia-southeast1`) for data residency. An unapproved region
  fails fast at `terraform plan`; the app reads the same region via `CDD_REGION`.
- **Profile-switchable.** The `Container` (`config.py`) binds each runtime/data port from
  one exact `CDD_PROFILE` map and binds identity from one exact
  `CDD_IDENTITY_PROFILE` map. Channel compatibility is checked independently. Missing,
  unknown, disabled, or unsafe combinations fail startup; there is no generic `gcp`
  fallback. One construction convention: `Adapter(settings: Settings)`.
- **GCP imports are lazy.** Every `google-cloud-*` / `google-genai` / `google-adk` import
  lives inside a method/`__init__` or under `TYPE_CHECKING`. The local/onprem/test profile
  imports every module with no Google Cloud SDK installed.
- **A WORKING offline profile.** `CDD_PROFILE=local` runs the full assessment pipeline end
  to end with no Google Cloud, no API key and no emulators: SQLite FTS5 retrieval over a
  self-seeding synthetic case corpus, a deterministic schema-driven LLM, a heuristic
  guardrail, regex DLP, and a hash-chained append-only SQLite audit (tamper-evident,
  exportable to JSON Lines and restorable via `cdd-sow audit`). It is the default dev/test/CI
  profile and is the same code the unit suite drives.
- **Maker-checker.** A CDD dossier always sets `requires_human_review=True`. A HIGH or
  PROHIBITED risk band, or any sanctions/terrorism adverse-media hit, escalates.
- **Governed RAG store is Hrz2.** Doc1 ingests the case's KYC documents into Hrz2 (with case ACL
  tags) and retrieves via Hrz2. It does not build a separate retrieval backend (that is Hrz2's
  job, rule R3).

---

## 3. Pinned stack (current GA, mid-2026)

- Python 3.12+, `from __future__ import annotations`, full type hints, ruff line-length 100,
  ruff lint `["E","F","I","UP","B","SIM"]`, target py312, build backend hatchling.
- The product is Gemini Enterprise Agent Platform; the API host is still
  `aiplatform.googleapis.com`. Region pinned `asia-southeast1`.
- Models: reasoning `gemini-3.5-flash` (thinking=high), triage `gemini-3.5-flash`.
  Never a floating default or `gemini-2.0-flash`.
- Unified SDK `google-genai`. ADK `google-adk==2.7.1`. A2A v1.0 + MCP 2026-07-28. One
  built-in tool per agent: `google_search` lives in its own adverse-media sub-agent.
- Document extraction: **Document AI**. Audit: Cloud Logging locked WORM bucket, retention
  180 days by default (six months), with a longer reviewed period supported. The WORM lock
  remains irreversible, and existing locked deployments must preserve or increase their
  current retention. Tracing: Cloud Trace via OpenTelemetry, message-content capture OFF. Eval: Gen
  AI evaluation service plus the Hrz4 gate.
- `[gcp]` extra holds all google-cloud-* / google-adk / google-genai (documentai,
  discoveryengine, dlp, aiplatform, logging, opentelemetry, a2a-sdk, mcp). Core deps are
  framework-light (pydantic, pyyaml, httpx, tenacity, typer, fastapi, uvicorn,
  python-dateutil). `[dev]`: pytest, pytest-asyncio, pytest-cov, ruff, mypy, types-PyYAML,
  respx.

---

## 4. Layers

- `domain/` (pure): `models.py` (the dossier artifacts, citations, safety/audit/eval/llm
  models), `prompts.py` (pure strings), `serialization.py` (`to_jsonable`), `errors.py`,
  `review_policy.py` (maker-checker), `_grounded.py` (shared retrieve-reason-cite helper),
  and the services (`cdd_service`, `sow_service`, `risk_service`, `adverse_media_service`,
  `ownership_service`, re-exported by `services.py`).
- `ports/`: 21 Protocols, re-exported from `ports/__init__.py`, including
  `DocumentStorePort`, `ReviewRouterPort`, `CaseStorePort`,
  `SanctionsListProviderPort`, `BrowserFlowStorePort`, `OwnershipGraphPort`, and
  `IdentityPort`.
- `adapters/{gcp,live,local,platform,onprem}/`: primary managed-service adapters (lazy SDK
  imports), the SDK-free offline `local` family, the intentional `live` hybrid, thin
  `httpx` clients to sibling services, and on-prem placeholder stubs.
- `api/`: FastAPI app (import-safe), pydantic schemas with `from_domain`, the dossier
  endpoints + `/healthz` + `/.well-known/agent-card.json`.
- `cli/`: Typer CLI (import-safe, lazy heavy imports), `NotImplementedError` maps to exit 2.
- `agent/`: ADK root agent + agent_card + tools + the `google_search` adverse-media
  sub-agent + the redact/guardrail/audit callbacks; all ADK imports lazy.

---

## 5. The assessment pipeline (R1 full safety; each step in `tracer.span`; audited)

`CddService.assess(case_input, actor)`:

1. `redaction.redact(case inputs)` (P-04) before any model/index/registry/audit call.
2. `guardrail.screen(INPUT)`. Blocked: audit BLOCKED and raise `GuardrailBlockedError`.
3. For each KYC document: `extraction.extract` then `knowledge_base.ingest` (case ACL).
4. `knowledge_base.search` (retrieve grounding). Empty: raise `RetrievalEmptyError`.
5. `adverse_media.scan` and `registry.resolve` (UBO).
6. `SourceOfWealthService.build` (LLM synthesis + self-critique groundedness pass).
7. `RiskRatingService.rate` (LLM, then deterministic band raise on hard signals).
8. `compliance.check` against Rsk1 (regulatory CDD/AML expectations), best-effort.
9. Assemble `CDDCase`.
10. `guardrail.screen(OUTPUT)` on the narrative + rationale. Blocked: audit BLOCKED + raise.
11. Review policy: always `requires_human_review=True`; escalate on hard signals.
12. `audit.record` (already-redacted prompt + a redacted response summary), decision
    ESCALATED.

Sub-service signatures (fixed): `SourceOfWealthService.build(subject, passages, actor)`,
`RiskRatingService.rate(subject, sow, adverse_media, ownership, passages, actor)`,
`AdverseMediaService.scan(subject, actor)`, `OwnershipService.resolve(subject, actor)`.

---

## 6. HTTP contracts

### 6.1 Doc1 defines (consumed by the UI/CLI and peers)

- `POST /v1/cdd {subject, documents[], actor}` -> `CDDCase`.
- `POST /v1/source-of-wealth {subject, actor}` -> `SourceOfWealthNarrative`.
- `POST /v1/adverse-media {subject_name, actor}` -> `AdverseMediaFinding[]`.
- `POST /v1/perpetual-kyc {subject, as_of?, last_reviewed?}` -> `PerpetualKycAssessment`.
  Runs one perpetual-KYC cycle. The tenant is stamped from the verified Principal and the
  monitoring record's ACL is derived server-side, so a cross-tenant caller gets 403.
- `GET /v1/perpetual-kyc/queue` -> `{items: PerpetualKycAssessment[]}`, the caller's
  tenant-scoped review queue, most urgent first.
- `POST /v1/cases/{case_id}/bundle/export` (body: `CDDCase`) -> `application/zip`, the
  complete case bundle: `manifest.json`, `dossier.json` and `documents/<id>` holding the
  original bytes of every source document the caller may read. The manifest digest is
  returned in `X-Bundle-Manifest-Sha256` for out-of-band custody. Requires both
  `cdd.read` and `documents.read`.
- `POST /v1/cases/{case_id}/bundle/import` (multipart: `file`, optional
  `manifest_sha256`) -> `{schema_version, case_id, exported_at, manifest_sha256, dossier,
  documents[], retained_existing[]}`. Verifies every digest, then restores the documents
  under their ORIGINAL ids so the dossier's citations still resolve, filed under ACL tags
  derived from the verified Principal (never the bundle's own). A bundle naming another
  case is refused; any integrity failure is one 422. Requires `cdd.read` and
  `documents.write`.
- `POST /v1/ubo-graph {subject, as_of?}` -> `UboResolution`. Resolves the
  cross-jurisdiction beneficial-ownership structure. Consequential: it always requires
  human review and is routed to Hrz7 (rule R8). The tenant and the record's ACL are
  derived from the verified Principal, so a caller with no case entitlement gets 403.
- `GET /v1/ubo-graph/{subject_id}?name=&jurisdiction=&as_of=` -> `OwnershipGraph`, the
  WALKED STRUCTURE ONLY (layers, edges, citations). No finding, no control basis, no
  indicator, so it is evidence rather than a decision and stays side-effect free. Both
  shapes are frozen against the agent-card version: see
  [`docs/ubo-graph-contract.md`](docs/ubo-graph-contract.md).
- `GET /healthz` -> `{status, profile, region}`.
- `GET /.well-known/agent-card.json` -> AgentCard. Skills: `assess_cdd`,
  `build_source_of_wealth`, `scan_adverse_media`, `resolve_ownership`,
  `resolve_ubo_graph`, `run_perpetual_kyc`, `list_perpetual_kyc_queue`.

All JSON field names mirror the domain dataclasses (enums as strings) via `to_jsonable`.

### 6.2 Channel and identity integration contract

The public UI artifact has one fixed `/agent` base path. Native Hrz9 hosting keeps
`/apps/doc1` only as a compatibility entry to that artifact. Sandboxed Modes 4 and 5 use a
dedicated agent origin, an installation-owned manifest, strict MessagePort bootstrap, and
one authenticated JSON/form/blob transport. Mode 4 verifies a Doc1-audience OAuth access
token; Mode 5 redeems an iframe-owned PKCE verifier and one-time broker grant for a
dedicated Doc1 token. Public citation originals continue through an actor-bound,
single-use Mode 6 flow.

The full production-module synthetic gate passes in Chromium, Firefox, and WebKit with
RSA and EC issuers, key rotation, negative paths, and leak scans. This is channel and
identity conformance only. Named production IdP/BFF registrations, DNS/TLS/hosting,
shared BrowserFlow/JTI stores, key custody and rotation, approved origins/CSP,
target-hosting browser evidence, and a separately deployed Mode 6 fallback remain
required. The normative route, token, state-machine, and completion contracts live in
[`docs/embedding-and-identity.md`](docs/embedding-and-identity.md).

### 6.3 Doc1 consumes (existing platform services)

- **Hrz1 guardrail** (`GUARDRAIL_GATEWAY_URL`): `POST /v1/guardrail/screen {text, direction}`,
  `POST /v1/redact {text}`.
- **Hrz2 enterprise KB** (`KNOWLEDGE_BASE_URL`): `POST /v1/ingest {document, acl_tags, source_meta}`,
  `POST /v1/search {query, top_k, acl_principals[], filters}` -> `{passages:[...]}`.
- **Hrz3 registry** (`AGENT_REGISTRY_URL`): `POST/GET /v1/agents`,
  `GET /.well-known/agent-card.json`.
- **Hrz4 AI quality** (`QUALITY_GATE_URL`):
  `POST /v1/evaluations {target: {model, prompt_version, dataset_id, system}, dataset_id, bundle: "doc1-cdd-sow"}`
  (the top-level `dataset_id` equals `target.dataset_id`; a divergence is a 422) -> report
  parsed from `results[]` (each row carrying its own server-owned threshold), and
  `POST /v1/gate {target, dataset_id, bundle: "doc1-cdd-sow"}` -> `{passed}`. Metric
  selection is by the registered bundle name `doc1-cdd-sow` (Hrz4 owns the metric set and
  per-bundle thresholds); no bare metric names go on the wire, so Hrz4's fail-closed
  unknown-metric rejection is never triggered by this client.
- **Hrz5 observability/audit** (`OBSERVABILITY_URL`): `POST /v1/audit` (202).
- **Rsk1 compliance** (`RSK_COMPLIANCE_URL`): `POST /ask {question, actor, filters}` ->
  AnswerResponse `{question, answer, citations, requires_human_review, confidence}`.

---

## 7. Eval gate (Hrz4 / P-08)

`eval/run_eval.py` runs the real `CddService` against deterministic in-memory fakes over a
synthetic golden case set and scores: `sow_groundedness` (>= 0.80), `risk_band_accuracy`
(>= 0.80), `citation_accuracy` (>= 0.90), `pii_safety` (>= 0.99), `pkyc_priority`
(>= 0.90). Exit non-zero on fail.

`pkyc_priority` is scored against an **independent oracle**: each golden case declares the
change to simulate plus the queue priority and re-score direction that change must produce
(`perpetual_kyc.{change, expected_priority, expected_delta_direction}`). The metric never
re-reads the engine's own opinion of itself, and
`tests/unit/test_eval_perpetual_kyc_can_go_red.py` proves per change kind that a mis-tuned
engine drives it red (`agent_eval_kit.assert_each_can_go_red`).
`--use-gcp` routes through the Gen AI evaluation service / Hrz4. Rubrics in `eval/rubrics/`.

---

## 8. The hard gate (how "done" is judged)

In a fresh venv with only the `[dev]` extra (no `google-cloud-*`, no `google-adk`):

```bash
ruff check src tests            # clean
ruff format --check src tests   # clean
pytest -m 'not integration' -q  # pass (unit + contract)
mypy src                        # clean (best-effort)
python eval/run_eval.py         # pass (exit 0)
```

---

## 9. Long-running, auditable SoW cases (implemented)

The pipeline above is *one-shot*: it assumes all evidence is in hand and returns a single
`CDDCase`. Real Source-of-Wealth clearance is iterative: an RM finds gaps, goes back to the
client for documents, and the case accrues evidence over **weeks** before an MLRO signs off.
[`docs/sow-longitudinal-audit-design.md`](docs/sow-longitudinal-audit-design.md) is the
authoritative design and it is **built** (`domain/sow_case_service.py`, `case_store` in the
parity port map, and the demos in [`DEMO.md`](DEMO.md)): a stateful `SowCase` aggregate
(case state machine, append-only evidence ledger + iterations, optimistic concurrency)
persisted via the `CaseStorePort`, and an **audit-first** `SowAuditView` output (sources
grouped at a glance, each proven by a `Citation`, with a *deterministic* reconciliation/gap
engine and client-ready information requests). It carries the enhanced-diligence panels too
(key-individuals CDD roll-up, sanctions/PEP screening, risk scorecard + CDD tier, Source of
Funds, ongoing monitoring), each a pure deterministic sub-service. It preserves every
invariant in this spec (R1 redaction, R2 WORM audit, R3 case ACL, P-06 maker-checker,
residency) and is additive: the one-shot endpoint is unchanged.

> The managed `CaseStorePort` is implemented as `FirestoreCaseStoreAdapter` and is bound
> for `gcp` and `platform`; the integration test requires a provisioned regional Firestore
> database and credentials. The `local` in-process store is fully working and the
> `onprem` placeholder proves interface parity.

---

## 10. Perpetual KYC (implemented)

Section 9 keeps a case current on a *schedule*. Perpetual KYC keeps it current on
*change*: it asks what moved since the last accepted picture and re-scores the
relationship the moment something does. It is a module inside this lifecycle, not a
bolt-on: it reuses the same subject, the same screening/media/registry ports, the same
periodic-review engine, and the same maker-checker route to Hrz7.

**The monitored edges.** Three, each already a port: sanctions/watchlist screening
(`SanctionsListProviderPort` through the deterministic `ScreeningService`), adverse media
(`AdverseMediaPort`) and corporate-registry ownership (`CorporateRegistryPort`). Each
observed fact becomes a `MonitoringSignal` with a stable content fingerprint
(`perpetual_kyc.signal_key`), so the same real-world fact yields the same key on every run
and the baseline diff is exact rather than fuzzy. Under `local` the sanctions and media
adapters serve **obviously fictional** fixtures; a fixture never asserts anything about a
real party.

**Deterministic re-scoring.** `PerpetualKycEngine` is pure stdlib and takes no clock: the
caller supplies `as_of`, so a run is replayable byte-for-byte. Signals are marked NEW,
PERSISTING or CLEARED against the stored baseline; the new score is
`baseline + capped uplift(NEW) - relief(CLEARED)`, clamped to `[0, 1]`, with one auditable
`SignalUplift` line per signal. Every weight, cap, threshold and SLA is bank-owned policy
(`policy.perpetual_kyc` in `config/settings.yaml`), never a constant in the engine. The
first run for a subject *establishes* the baseline rather than treating a standing picture
as change. **The LLM produces no number here**; it is handed the finished, redacted figures
and returns a schema-validated narrative that is discarded if it does not match.

**The explainable review queue.** The engine derives a `ReviewQueueItem` deterministically:
priority (hard signals first, then the re-scored total), an SLA date from policy, the
reasons that put it there, and the citations behind them. The outcome always sets
`requires_human_review` and is routed to Hrz7 through the `ReviewRouterPort`
(`route_monitoring`, rule R8) with the subject descriptor and summary redacted before the
wire. Nothing is blocked, frozen or downgraded by the agent: a checker disposes.

**Persistence and authorization.** `MonitoringStorePort` holds the baselines and the queue.
Records carry the server-derived `case:<subject id>` + `tenant:<tenant>` tags; a baseline
read requires all of them (403, never a 404-shaped answer), and the queue lists only
records whose tenant tag the caller holds, with an untagged record never listed. The
managed adapter is regional CMEK Firestore inside the perimeter, `local` is a working
in-process store, and `onprem` fails fast so an unimplemented store can never silently
empty the queue.

**Surfaces.** `POST /v1/perpetual-kyc` and `GET /v1/perpetual-kyc/queue` (both protected,
both deriving the actor and the ACL from the verified Principal), the A2A skills
`run_perpetual_kyc` and `list_perpetual_kyc_queue`, the CLI commands `perpetual-kyc` and
`perpetual-kyc-queue`, and the `PerpetualKycPanel` in the console.

## 11. Cross-jurisdiction UBO graph (implemented)

Section 4's `OwnershipSummary` answers "who are the owners of record" in one flat hop,
which is what the dossier consumes. A layered cross-border structure is not answerable
that way: a natural person holding 60% of a Jersey holding company that holds 50% of the
operating entity is a 30% beneficial owner no single extract shows. This module walks that
chain. It is a module inside the existing lifecycle, not a bolt-on: it reuses the same
subject, the same name matcher, the same country-risk lists and the same maker-checker
route to Hrz7, and it CONVERTS DOWN to `OwnershipSummary`, so the dossier and the
related-party derivation are untouched.

**One hop at a time.** `OwnershipGraphPort` returns a single `RegistryHop`: the parties a
registry records directly against one entity, each with its own `Citation`. Traversal is
deliberately NOT in the adapter. An adapter that walked the chain would bury the depth
limit, the visited set and the truncation rule inside a vendor integration where no
auditor can see them and no compliance function can retune them, and a prompt that asks a
model to resolve a whole structure invites it to invent the layers it cannot find. Under
`local` the adapter serves an obviously fictional multi-jurisdiction fixture (a layered
chain, a cross-holding cycle, a nominee director and a shell pass-through).

**Deterministic resolution.** `UboGraphEngine` is pure stdlib and takes no clock: the
caller supplies `as_of` and the hop fetcher, so a run replays byte for byte. It walks
breadth-first with a VISITED SET, so circular and cross-holdings terminate; depth, node
and path limits set a truncation flag rather than presenting a partial picture as whole.
Effective ownership is the sum over SIMPLE paths of the product of the shareholding
percentages along each path, deterministically rounded, and every finding carries the
paths it came from and those paths' citations. Control is an explicit LADDER, tried in
order and stopped at the first rung that holds: effective majority, voting majority, board
majority, contractual control, then the senior-managing-official FALLBACK. An intermediate
holding company is looked THROUGH, never named as the controller: a body corporate
qualifies for the ladder only when nothing is recorded above it. Every threshold, limit and
weight is bank-owned policy (`policy.ubo_graph` in `config/settings.yaml`), so moving the
beneficial-ownership threshold from 25% to 10% is configuration, never a code change.

**Indicators, never conclusions.** The same engine raises nominee signals (a declared
nominee arrangement, a nominee/trustee token in the recorded name, one name recurring
across unrelated entities matched with the EXISTING screening name matcher, a shared
registered-agent address) and shell signals (a single-owner pass-through layer,
incorporation age, dormant filing status), plus secrecy jurisdiction scored through the
EXISTING `country_risk` module. A shell flag needs at least `min_shell_signals` traits
together, because a single-shareholder holding company owning one asset is the most common
lawful corporate structure there is. The flags raise a deterministic opacity score, banded
per distinct kind and clamped to `[0, 1]`, which sets the review severity. **The LLM
produces no node, edge, percentage or verdict here**; it is handed the finished, redacted
resolution and returns a schema-validated narrative that is discarded if it does not match.

**No store port.** A resolution is a pure function of the registry layers plus policy, so
it is recomputable rather than stateful. Persisting it would create a second, staler answer
to a question the engine answers exactly. The durable record is the Hrz7 review item plus
the WORM audit event.

**Surfaces.** `POST /v1/ubo-graph` (consequential, routed) and `GET
/v1/ubo-graph/{subject_id}` (the structure alone, side-effect free), the A2A skill
`resolve_ubo_graph`, the CLI command `ubo-graph` (which prints the path arithmetic line by
line), and the `UboGraphPanel` in the console beside the perpetual-KYC panel. The A2A
output shape is frozen against the agent-card version:
[`docs/ubo-graph-contract.md`](docs/ubo-graph-contract.md).
