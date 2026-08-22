"""Local deployment profile adapters — a WORKING, offline laptop stack.

The ``local`` profile is the third deployment option alongside ``gcp`` (managed Google
Cloud services) and ``onprem`` (fail-fast Google Distributed Cloud migration
placeholders). Unlike ``onprem``, every adapter here is a *real, deterministic*
implementation that runs the whole CDD pipeline end to end with **no Google Cloud, no
API key, and no running emulators by default**:

* Knowledge base / retrieval (A2) -> a ``sqlite3`` **FTS5** index over the case's KYC
  passages (BM25 rank), seedable and ingestable.
* LLM (Gemini) -> a deterministic, schema-driven generator (no model, no network).
* Guardrail (Model Armor) -> a heuristic that blocks prompt-injection / jailbreak text.
* PII redaction (DLP) -> regex de-identification (SG NRIC/FIN, emails, phones).
* Audit (Cloud Logging WORM) -> an append-only local store, read-back supported.
* Tracer (Cloud Trace) -> no-op spans.
* Document extraction (Document AI) -> a local plain-text / pypdf parser.
* Adverse media (google_search) -> disabled / canned (no public-web egress) by default.
* Corporate registry -> a deterministic in-process UBO resolver.
* Compliance client (C1) -> an in-process canned regulatory answer (no HTTP to C1).
* Agent registry (A3) / tool catalog (MCP) -> in-process stores (no HTTP to siblings).
* Evaluation (Gen AI eval / A4) -> delegates to the in-repo offline eval gate.

Everything is **seedable** so the test suite stays deterministic, and the default code
path imports **no google-cloud package at module top level**. Optional higher-fidelity
local runs route to Google's official emulators when the standard ``*_EMULATOR_HOST``
env vars are set (the google client is imported lazily, only on that branch); see
:mod:`cdd_sow_research.adapters.local._emulator`.
"""
