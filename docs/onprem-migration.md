# On-prem migration (exit / portability): General Principle P-12

Doc1 makes the sovereign exit **contract demonstrable**, not the completed migration.
`CDD_PROFILE=onprem` selects fail-fast placeholders and proves the domain boundary. A working
sovereign deployment still requires implementing, validating, and operating every adapter
below. The domain core, services, API, CLI, and agent wiring are designed not to change.

## What "onprem" gives you today

Setting `CDD_PROFILE=onprem` rebinds every port to a placeholder adapter under
`src/cdd_sow_research/adapters/onprem/`. Those adapters:

- construct cleanly with **no Google Cloud SDK installed** (the contract test proves it),
- structurally satisfy the same `Protocol` as the managed GCP adapter, and
- raise `NotImplementedError` from every method that must not silently no-op (guardrail,
  redaction, audit, extraction, knowledge base, registry, compliance, LLM, eval), while a
  couple of non-essential ports return safe defaults (the tracer is a no-op, adverse-media
  returns no findings, modelling an air-gapped perimeter with no public-web egress).

This is what makes the contract test `tests/contract/test_port_parity.py` meaningful: it
imports and constructs each on-prem placeholder and asserts interface parity.

## The migration checklist

To run Doc1 on a sovereign / on-premise platform, these adapter bodies are the primary
application-code surface to implement. The domain and services stay unchanged. Deployment
also requires sovereign packaging and dependencies, settings and secrets, IdP and browser-flow-store
configuration, infrastructure and operations, and credentialed integration tests:

| Port | On-prem file | What to implement |
|------|--------------|-------------------|
| `DocumentExtractionPort` | `onprem/extraction.py` | An on-prem document-extraction service |
| `KnowledgeBaseClientPort` | `onprem/knowledge_base.py` | An on-prem governed RAG store with case ACLs |
| `DocumentStorePort` | `onprem/document_store.py` | Sovereign evidence-byte custody with ACLs, including `restore` (write a document back under its original id, refusing a differing document already held) so a complete case bundle reloads on-prem |
| `AdverseMediaPort` | `onprem/adverse_media.py` | An internal news/sanctions index (or keep air-gapped) |
| `CorporateRegistryPort` | `onprem/registry.py` | An on-prem corporate-registry / UBO source |
| `OwnershipGraphPort` | `onprem/ownership_graph.py` | An on-prem source for ONE cited registry hop (the engine owns the traversal) |
| `ComplianceClientPort` | `onprem/compliance.py` | The Rsk1 compliance service on-prem |
| `LLMPort` | `onprem/llm.py` | An on-prem model-serving endpoint |
| `GuardrailPort` | `onprem/guardrail.py` | An on-prem prompt/response screening backend (R1) |
| `PIIRedactionPort` | `onprem/redaction.py` | An on-prem PII de-identifier (R1, P-04) |
| `AuditSinkPort` | `onprem/audit.py` | An on-prem immutable (WORM) audit store (R2) |
| `ReviewRouterPort` | `onprem/review_router.py` | On-prem maker-checker routing |
| `ObservabilityTracerPort` | `onprem/tracer.py` | Sovereign telemetry export or reviewed no-op |
| `EvaluationGatePort` | `onprem/evaluation.py` | An on-prem eval backend (R5) |
| `AgentRegistryPort` | `onprem/registry_agent.py` | An on-prem agent catalog |
| `ToolCatalogPort` | `onprem/tool_catalog.py` | An on-prem MCP tool catalog |
| `CaseStorePort` | `onprem/case_store.py` | Durable longitudinal cases and sealed snapshots |
| `MonitoringStorePort` | `onprem/monitoring_store.py` | Perpetual-KYC baselines and the ACL-scoped review queue |
| `SanctionsListProviderPort` | `onprem/sanctions_provider.py` | Versioned local sanctions/PEP source |
| `BrowserFlowStorePort` | `onprem/browser_flow_store.py` | Shared atomic citation/grant state and security-event outbox |
| `IdentityPort` | `onprem/identity.py` | Client IdP verification and principal mapping |

Nothing under `src/cdd_sow_research/domain/` changes. The dossier pipeline, the maker-checker
policy, the citation mapping, the serialization, and the prompts are all profile-agnostic.

## Why this matters for a regulated buyer

A private bank's financial-crime function cannot accept a workload it cannot exit. Because
the domain depends only on Protocols, the migration surface is enumerated and contract-tested.
Equivalent sovereign controls for redaction, WORM audit, identity, authorization, and
maker-checker still have to be implemented and evidenced before claiming those properties
survive the move. The passing Modes 4/5 synthetic channel/identity gate does not prove any
of these sovereign runtime, storage, key-custody, deployment, or operational properties.
