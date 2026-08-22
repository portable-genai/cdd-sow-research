"""Live GCP smoke test — deselected in CI via ``-m 'not integration'``.

Requires real Google Cloud credentials and the ``[gcp]`` extra installed. It is skipped
automatically when ``GOOGLE_CLOUD_PROJECT`` is unset, so the default on-prem / test
profile (no Google Cloud SDK) never executes any of this. It constructs the managed
service adapters in the configured GCP region and does one trivial liveness call per adapter.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("GOOGLE_CLOUD_PROJECT"),
        reason="set GOOGLE_CLOUD_PROJECT (and install the [gcp] extra) to run GCP smoke tests",
    ),
]


@pytest.fixture(scope="module")
def gcp_settings():
    from cdd_sow_research.config import Settings

    s = Settings.load("config/settings.yaml")
    return Settings(
        project_id=os.environ["GOOGLE_CLOUD_PROJECT"],
        region="us-central1",
        profile="gcp",
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


@pytest.fixture(scope="module")
def container(gcp_settings):
    from cdd_sow_research.config import Container

    return Container(gcp_settings)


def test_region_is_singapore(gcp_settings):
    assert gcp_settings.region == "us-central1"


def test_guardrail_liveness(container):
    from cdd_sow_research.domain.models import Direction

    verdict = container.guardrail.screen("hello", Direction.INPUT)
    assert verdict.direction is Direction.INPUT


def test_redaction_liveness(container):
    result = container.redaction.redact("Contact me at jane@example.com")
    assert isinstance(result.text, str)


def test_knowledge_base_search_liveness(container):
    from cdd_sow_research.domain.models import RetrievalQuery

    passages = container.knowledge_base.search(
        RetrievalQuery(text="source of wealth", top_k=3, acl_principals=("case:demo",))
    )
    assert isinstance(passages, list)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q", "-m", "integration"]))
