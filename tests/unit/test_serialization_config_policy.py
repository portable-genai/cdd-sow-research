"""Unit tests for serialization, Settings.load, Container wiring, and the review policy.

* domain/serialization.to_jsonable round-trips enums (-> .value) and datetimes.
* Settings.load parses config/settings.yaml.
* Container under profile=onprem binds the on-prem placeholder adapters, and each
  bound adapter satisfies its runtime_checkable Protocol (structural parity).
* CddReviewPolicy: always-review plus the escalation rules.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.fixtures import sample_cases

from cdd_sow_research import ports
from cdd_sow_research.config import Container, Settings
from cdd_sow_research.domain.models import (
    AdverseMediaCategory,
    AdverseMediaFinding,
    AuditEvent,
    CDDCase,
    Decision,
    RiskBand,
    RiskRating,
    Severity,
    SourceOfWealthNarrative,
)
from cdd_sow_research.domain.review_policy import CddReviewPolicy

CONFIG_PATH = "config/settings.yaml"

PORT_PROTOCOLS = {
    "extraction": ports.DocumentExtractionPort,
    "knowledge_base": ports.KnowledgeBaseClientPort,
    "adverse_media": ports.AdverseMediaPort,
    "registry": ports.CorporateRegistryPort,
    "compliance": ports.ComplianceClientPort,
    "llm": ports.LLMPort,
    "guardrail": ports.GuardrailPort,
    "redaction": ports.PIIRedactionPort,
    "audit": ports.AuditSinkPort,
    "tracer": ports.ObservabilityTracerPort,
    "evaluation": ports.EvaluationGatePort,
    "agent_registry": ports.AgentRegistryPort,
    "tool_catalog": ports.ToolCatalogPort,
}

# port name -> Container attribute (most match; compliance/registry differ by concern).
PORT_TO_ATTR = {
    "extraction": "extraction",
    "knowledge_base": "knowledge_base",
    "adverse_media": "adverse_media",
    "registry": "registry",
    "compliance": "compliance",
    "llm": "llm",
    "guardrail": "guardrail",
    "redaction": "redaction",
    "audit": "audit",
    "tracer": "tracer",
    "evaluation": "evaluation",
    "agent_registry": "agent_registry",
    "tool_catalog": "tool_catalog",
}


# --------------------------------------------------------------------------- #
# to_jsonable
# --------------------------------------------------------------------------- #
def _to_jsonable():
    from cdd_sow_research.domain.serialization import to_jsonable

    return to_jsonable


def test_to_jsonable_enum_becomes_value():
    to_jsonable = _to_jsonable()
    assert to_jsonable(RiskBand.PROHIBITED) == "prohibited"
    assert to_jsonable(Severity.HIGH) == "high"
    assert to_jsonable(Decision.BLOCKED) == "blocked"


def test_to_jsonable_datetime_is_json_safe_string():
    to_jsonable = _to_jsonable()
    dt = datetime(2026, 6, 20, 8, 30, tzinfo=UTC)
    out = to_jsonable(dt)
    assert isinstance(out, str)
    assert json.loads(json.dumps(out)) == out
    assert "2026-06-20" in out


def test_to_jsonable_cddcase_roundtrips_through_json():
    to_jsonable = _to_jsonable()
    case = CDDCase(
        id="cdd-x",
        subject=sample_cases.ENTITY_SUBJECT,
        sow=SourceOfWealthNarrative(
            subject_id="subj-x",
            narrative="n",
            sources=(),
            citations=(sample_cases.PRIMARY_PASSAGE.citation,),
            confidence=0.8,
        ),
        rating=RiskRating(band=RiskBand.MEDIUM, score=0.4),
    )
    out = to_jsonable(case)
    text = json.dumps(out)  # must not raise
    reloaded = json.loads(text)
    assert reloaded["rating"]["band"] == "medium"
    assert reloaded["requires_human_review"] is True
    assert reloaded["sow"]["citations"][0]["source_type"] == "document"


def test_to_jsonable_audit_event_is_worm_serialisable():
    to_jsonable = _to_jsonable()
    event = AuditEvent(
        action="assess_cdd",
        actor="analyst",
        decision=Decision.ESCALATED,
        redacted_prompt="[NRIC]",
        redacted_response="risk=medium",
        citations=(sample_cases.PRIMARY_PASSAGE.citation,),
    )
    reloaded = json.loads(json.dumps(to_jsonable(event)))
    assert reloaded["decision"] == "escalated"
    assert reloaded["action"] == "assess_cdd"
    assert reloaded["resource"] == "cdd-sow-research"


# --------------------------------------------------------------------------- #
# Settings.load
# --------------------------------------------------------------------------- #
def test_settings_load_parses_yaml():
    settings = Settings.load(CONFIG_PATH)
    assert settings.region == "us-central1"


def test_gcp_region_is_configurable_from_one_selector(monkeypatch):
    monkeypatch.setenv("GCP_REGION", "europe-west4")
    settings = Settings.load(CONFIG_PATH)
    assert settings.region == "europe-west4"
    assert settings.document_ai.location == "europe-west4"
    # The knowledge base is DELIBERATELY not on this selector. Discovery Engine serves `global`,
    # `us` and `eu` and no Cloud region, so tracking GCP_REGION produced
    # `europe-west4-discoveryengine.googleapis.com` -- a hostname that does not exist -- and
    # grounded retrieval failed with a 501 that blames the api_endpoint configuration. One
    # selector per FACT, and "which Cloud region do we deploy in" and "which Discovery Engine
    # location holds the corpus" are two facts with two answer sets.
    assert settings.knowledge_base.location == "us"
    assert settings.model_armor.host == "modelarmor.europe-west4.rep.googleapis.com"
    assert settings.logging.retention_days == 180
    # The model id moved to gemini-3.7-flash on 2026-08-27; what this line is really
    # asserting is that ONE selector drives the region without dragging the model
    # location with it, which is why models.location stays `us` here.
    assert settings.models.reasoning == "gemini-3.7-flash"
    assert settings.models.location == "us"
    assert settings.models.triage == "gemini-3.1-flash-lite"
    assert settings.knowledge_base.top_k == 10
    assert set(PORT_PROTOCOLS) <= set(settings.adapters)


def test_settings_allows_reviewed_longer_audit_retention(monkeypatch):
    monkeypatch.setenv("CDD_AUDIT_RETENTION_DAYS", "365")

    settings = Settings.load(CONFIG_PATH)

    assert settings.logging.retention_days == 365


def test_settings_file_is_bound_to_reviewed_digest(monkeypatch):
    exact_bytes = Path(CONFIG_PATH).read_bytes()
    monkeypatch.setenv("CDD_EXPECTED_SETTINGS_SHA256", hashlib.sha256(exact_bytes).hexdigest())
    assert Settings.load(CONFIG_PATH).region == "us-central1"

    monkeypatch.setenv("CDD_EXPECTED_SETTINGS_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="settings digest does not match"):
        Settings.load(CONFIG_PATH)


def test_production_settings_require_both_reviewed_digests(monkeypatch):
    exact_bytes = Path(CONFIG_PATH).read_bytes()
    monkeypatch.setenv("CDD_PRODUCTION", "true")

    with pytest.raises(ValueError, match="CDD_EXPECTED_SETTINGS_SHA256"):
        Settings.load(CONFIG_PATH)

    monkeypatch.setenv("CDD_EXPECTED_SETTINGS_SHA256", hashlib.sha256(exact_bytes).hexdigest())
    with pytest.raises(ValueError, match="CDD_EXPECTED_MANIFEST_SHA256"):
        Settings.load(CONFIG_PATH)


def test_settings_pins_models_to_allowed_ids():
    settings = Settings.load(CONFIG_PATH)
    assert settings.models.reasoning != "gemini-2.0-flash"
    assert settings.models.triage != "gemini-2.0-flash"
    assert settings.models.reasoning.startswith("gemini-3")


# --------------------------------------------------------------------------- #
# Container binds on-prem adapters with structural parity.
# --------------------------------------------------------------------------- #
def _onprem_settings() -> Settings:
    s = Settings.load(CONFIG_PATH)
    return Settings(
        project_id=s.project_id,
        region=s.region,
        profile="onprem",
        kms_key=s.kms_key,
        grounding_enabled=s.grounding_enabled,
        models=s.models,
        document_ai=s.document_ai,
        knowledge_base=s.knowledge_base,
        model_armor=s.model_armor,
        dlp=s.dlp,
        logging=s.logging,
        agent_engine=s.agent_engine,
        adapters=s.adapters,
    )


def test_container_binds_onprem_adapters_with_protocol_parity():
    container = Container(_onprem_settings())
    for port_name, protocol in PORT_PROTOCOLS.items():
        adapter = getattr(container, PORT_TO_ATTR[port_name])
        assert isinstance(adapter, protocol), (
            f"on-prem adapter for '{port_name}' is not structurally a {protocol.__name__}"
        )


def test_container_never_falls_back_to_a_different_runtime():
    settings = _onprem_settings()
    broken = dict(settings.adapters)
    broken["guardrail"] = {
        key: value for key, value in broken["guardrail"].items() if key != "onprem"
    }
    settings = Settings(**{**settings.__dict__, "adapters": broken})

    with pytest.raises(KeyError, match="No adapter configured"):
        _ = Container(settings).guardrail


# --------------------------------------------------------------------------- #
# CddReviewPolicy
# --------------------------------------------------------------------------- #
def test_dossier_always_requires_review():
    assert CddReviewPolicy().requires_review() is True


def test_high_band_escalates():
    policy = CddReviewPolicy()
    assert policy.escalates(RiskBand.HIGH, ()) is True
    assert policy.escalates(RiskBand.PROHIBITED, ()) is True
    assert policy.escalates(RiskBand.LOW, ()) is False


def test_sanctions_media_escalates_regardless_of_band():
    policy = CddReviewPolicy()
    media = (
        AdverseMediaFinding(
            headline="h",
            publisher="p",
            url="u",
            category=AdverseMediaCategory.SANCTIONS,
            severity=Severity.HIGH,
        ),
    )
    assert policy.escalates(RiskBand.LOW, media) is True


def test_benign_media_does_not_escalate_low_band():
    policy = CddReviewPolicy()
    media = (
        AdverseMediaFinding(
            headline="h",
            publisher="p",
            url="u",
            category=AdverseMediaCategory.OTHER,
            severity=Severity.LOW,
        ),
    )
    assert policy.escalates(RiskBand.LOW, media) is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_the_knowledge_base_location_has_its_own_selector(monkeypatch):
    """It must be settable for an EU deployment without moving the whole region."""

    monkeypatch.setenv("CDD_KB_LOCATION", "eu")
    assert Settings.load(CONFIG_PATH).knowledge_base.location == "eu"


def test_the_knowledge_base_location_default_is_one_discovery_engine_serves(monkeypatch):
    monkeypatch.delenv("CDD_KB_LOCATION", raising=False)
    monkeypatch.setenv("GCP_REGION", "asia-southeast1")
    assert Settings.load(CONFIG_PATH).knowledge_base.location in {"global", "us", "eu"}
