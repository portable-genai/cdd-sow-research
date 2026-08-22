"""Contract tests: the ``onprem`` and ``local`` adapters are structural parity of the ports.

For every port the catalog declares, this iterates the adapter map and, for both the
``onprem`` and ``local`` profiles, imports + constructs the bound class (which must build
cleanly with **no Google Cloud SDK** installed), then asserts:

  1. the constructed instance satisfies its runtime_checkable Protocol (isinstance), and
  2. every method/property the Protocol declares actually exists on the instance.

It additionally proves the two profiles' distinct contracts:

* ``onprem`` is the fail-fast Google Distributed Cloud migration target: consequential
  operations raise ``NotImplementedError`` (proven on a representative port), while
  explicitly reviewed optional ports may return safe defaults, and
* ``local`` is a WORKING offline stack: the same ports construct and answer in-process.

This is the proof of the ports-and-adapters / no-lock-in promise (P-02): the on-prem
migration target and the offline local stack implement the exact same interface as the
managed GCP stack.
"""

from __future__ import annotations

import importlib
from typing import Protocol, get_type_hints

import pytest

from cdd_sow_research import ports
from cdd_sow_research.config import LocalSettings, Settings, instantiate

CONFIG_PATH = "config/settings.yaml"

# Every port name in settings.adapters mapped to its Protocol.
PORT_PROTOCOLS: dict[str, type] = {
    "extraction": ports.DocumentExtractionPort,
    "knowledge_base": ports.KnowledgeBaseClientPort,
    "document_store": ports.DocumentStorePort,
    "adverse_media": ports.AdverseMediaPort,
    "registry": ports.CorporateRegistryPort,
    "ownership_graph": ports.OwnershipGraphPort,
    "compliance": ports.ComplianceClientPort,
    "llm": ports.LLMPort,
    "guardrail": ports.GuardrailPort,
    "redaction": ports.PIIRedactionPort,
    "audit": ports.AuditSinkPort,
    "tracer": ports.ObservabilityTracerPort,
    "evaluation": ports.EvaluationGatePort,
    "agent_registry": ports.AgentRegistryPort,
    "tool_catalog": ports.ToolCatalogPort,
    "case_store": ports.CaseStorePort,
    "monitoring_store": ports.MonitoringStorePort,
    "sanctions": ports.SanctionsListProviderPort,
    "review_router": ports.ReviewRouterPort,
    "browser_flow_store": ports.BrowserFlowStorePort,
}

# Profiles whose adapters must construct + satisfy the Protocols with no GCP SDK.
SDK_FREE_PROFILES = ("onprem", "local")


def _settings(profile: str) -> Settings:
    base = Settings.load(CONFIG_PATH)
    # Point the local stores at in-memory SQLite so the contract test stays ephemeral.
    return Settings(
        project_id=base.project_id,
        region=base.region,
        profile=profile,
        kms_key=base.kms_key,
        grounding_enabled=base.grounding_enabled,
        models=base.models,
        document_ai=base.document_ai,
        knowledge_base=base.knowledge_base,
        model_armor=base.model_armor,
        dlp=base.dlp,
        logging=base.logging,
        agent_engine=base.agent_engine,
        local=LocalSettings(
            db_path=":memory:",
            audit_path=":memory:",
            documents_path=":memory:",
            browser_flow_path=".pytest_cache/browser-flow-contract.sqlite3",
        ),
        adapters=base.adapters,
    )


def _protocol_members(protocol: type) -> set[str]:
    """The attribute names a Protocol declares (methods + properties), no dunders."""
    members = set(getattr(protocol, "__protocol_attrs__", set()))
    if not members:
        # Fallback for older typing internals: union of annotations + callables.
        members |= set(get_type_hints(protocol).keys())
        for name in dir(protocol):
            if name.startswith("_"):
                continue
            members.add(name)
    return {m for m in members if not m.startswith("_")}


def test_port_protocols_matches_settings_adapters():
    """The hand-maintained PORT_PROTOCOLS map must equal the ports bound in settings.

    Without this, a fork that adds a port Protocol and binds it in config/settings.yaml but
    forgets the PORT_PROTOCOLS entry gets ZERO parity/constructor/onprem-binding enforcement
    with a green CI (silent drift). Set-equality here fails loudly on either omission.
    """
    settings = Settings.load(CONFIG_PATH)
    bound = set(settings.adapters)
    declared = set(PORT_PROTOCOLS)
    missing_from_map = bound - declared
    missing_from_settings = declared - bound
    assert not missing_from_map, (
        f"ports bound in settings.adapters but absent from PORT_PROTOCOLS "
        f"(so untested): {sorted(missing_from_map)}. Add them to the parity map."
    )
    assert not missing_from_settings, (
        f"ports in PORT_PROTOCOLS with no settings.adapters binding: "
        f"{sorted(missing_from_settings)}."
    )


def test_every_port_has_onprem_and_local_bindings():
    settings = Settings.load(CONFIG_PATH)
    for port_name in PORT_PROTOCOLS:
        binding = settings.adapters.get(port_name, {})
        assert "onprem" in binding, f"port '{port_name}' has no on-prem adapter binding"
        assert "local" in binding, f"port '{port_name}' has no local adapter binding"


def test_every_runtime_port_has_an_exact_binding_for_every_runtime():
    settings = Settings.load(CONFIG_PATH)
    expected = {"local", "live", "gcp", "platform", "onprem"}
    assert len(PORT_PROTOCOLS) == 20
    for port_name in PORT_PROTOCOLS:
        assert set(settings.adapters[port_name]) == expected, (
            f"runtime port {port_name!r} must bind every runtime exactly"
        )


def test_identity_bindings_are_separate_and_exact():
    settings = Settings.load(CONFIG_PATH)
    assert "identity" not in settings.adapters
    assert set(settings.identity.bindings) == {
        "local-persona",
        "iap",
        "oidc-session",
        "oauth-access-token",
        "embedded-grant",
        "onprem",
    }
    assert settings.identity.bindings["local-persona"].endswith("LocalPersonaIdentityAdapter")
    assert settings.identity.bindings["onprem"].endswith("OnPremIdentityAdapter")


@pytest.mark.parametrize("profile", SDK_FREE_PROFILES)
@pytest.mark.parametrize("port_name", sorted(PORT_PROTOCOLS))
def test_adapter_satisfies_protocol(profile: str, port_name: str):
    settings = _settings(profile)
    protocol = PORT_PROTOCOLS[port_name]
    dotted = settings.adapters[port_name][profile]

    # Import + construct with only Settings (the adapter convention), no GCP SDK.
    adapter = instantiate(dotted, settings)

    # 1. Structural conformance via runtime_checkable Protocol.
    assert isinstance(adapter, protocol), (
        f"{dotted} does not structurally satisfy {protocol.__name__}"
    )

    # 2. Every declared Protocol member exists. Check on the *class* (via the MRO), not
    #    the instance: a placeholder property getter may raise, so ``hasattr`` would
    #    wrongly report it missing. Looking the name up on the type tests for declaration
    #    without invoking the getter.
    members = _protocol_members(protocol)
    declared = set().union(*(vars(klass) for klass in type(adapter).__mro__))
    for member in members:
        assert member in declared, (
            f"{dotted} is missing port method/attr '{member}' of {protocol.__name__}"
        )


@pytest.mark.parametrize("profile", SDK_FREE_PROFILES)
@pytest.mark.parametrize("port_name", sorted(PORT_PROTOCOLS))
def test_adapter_constructs_with_single_settings_arg(profile: str, port_name: str):
    """The build contract: every adapter is ``Adapter(settings: Settings)``."""
    settings = _settings(profile)
    dotted = settings.adapters[port_name][profile]
    module_path, _, class_name = dotted.partition(":")

    cls = getattr(importlib.import_module(module_path), class_name)
    # Must accept exactly one positional Settings argument and build cleanly.
    instance = cls(settings)
    assert instance is not None


def test_onprem_redaction_fails_fast():
    """The on-prem stubs are fail-fast: a representative port raises NotImplementedError."""
    settings = _settings("onprem")
    adapter = instantiate(settings.adapters["redaction"]["onprem"], settings)

    with pytest.raises(NotImplementedError):
        adapter.redact("anything")


def test_local_knowledge_base_returns_real_passages():
    """The local stack is WORKING: the KB returns real, page-cited passages offline."""
    settings = _settings("local")
    adapter = instantiate(settings.adapters["knowledge_base"]["local"], settings)
    from cdd_sow_research.domain.models import RetrievalQuery

    passages = adapter.search(
        RetrievalQuery(text="source of wealth ownership logistics business", top_k=5)
    )
    assert passages, "local FTS5 knowledge base returned nothing for the seeded corpus"
    assert all(p.citation.page is not None for p in passages), "page-level citation required"


def test_all_protocols_are_runtime_checkable():
    for protocol in PORT_PROTOCOLS.values():
        assert issubclass(protocol, Protocol)  # type: ignore[arg-type]
        assert getattr(protocol, "_is_runtime_protocol", False), (
            f"{protocol.__name__} must be @runtime_checkable"
        )


def test_shared_contracts_are_the_commons_objects_not_local_copies():
    """The shared ports and value types must be the SAME objects as the commons ones.

    ``isinstance`` against a ``runtime_checkable`` Protocol is satisfied by a hand-copied
    look-alike, which is exactly how sixteen repositories drifted apart while every structural
    test stayed green. Object identity (``is``) is the only assertion that can tell a re-export
    from a redeclaration, so it is the one made here.
    """
    import agent_eval_kit
    import hex_service_kit.identity
    import hex_service_kit.observability

    from cdd_sow_research.domain import kernel, models

    assert ports.ObservabilityTracerPort is hex_service_kit.observability.ObservabilityTracerPort
    assert ports.TokenUsage is hex_service_kit.observability.TokenUsage
    assert ports.EvaluationGatePort is agent_eval_kit.EvaluationGatePort
    assert ports.IdentityPort is hex_service_kit.identity.IdentityPort

    assert models.TokenUsage is hex_service_kit.observability.TokenUsage
    assert kernel.TokenUsage is hex_service_kit.observability.TokenUsage
    assert models.EvalReport is agent_eval_kit.EvalReport
    assert kernel.EvalReport is agent_eval_kit.EvalReport
    assert models.EvalMetricResult is agent_eval_kit.EvalMetricResult
    assert kernel.EvalMetricResult is agent_eval_kit.EvalMetricResult


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
