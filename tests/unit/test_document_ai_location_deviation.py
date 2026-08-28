"""Document AI may sit in the deploy region or a named multi-region, and in nothing else.

Until 2026-08-28 the two halves of this one decision disagreed, in the dangerous direction.
`config/settings.yaml` routed the adapter to the `us` multi-region, while
`infra/terraform/document_ai.tf` derived the processor location from `var.region`: with the
default back at `asia-southeast1`, an apply would have created the processor in Singapore and
the adapter would have looked for it in the United States, surfacing as a 404 at request time
rather than at apply. The other four extracting trees closed exactly this split on
2026-08-28; this repository was the one their fix list did not cover.

The runtime carried no check at all, so `CDD_DOCAI_LOCATION=global` reached the adapter
silently and built `global-documentai.googleapis.com`, extracting document bytes somewhere
the residency record cannot name. `global` is precisely what someone reaches for to make a
failing single-region call succeed, which is why refusing it has to happen where the value is
read rather than only where the processor is created.

The rule is the one the other four trees already enforce, on both halves:

* the deploy region is allowed, and is the preferred state;
* a named MULTI-REGION is allowed, because it names one jurisdiction and carries Google's
  ML-processing commitment for that geography;
* everything else is refused, including `global`, which names no jurisdiction, and including
  another single region, which is neither the deploy region nor a multi-region commitment.

Document AI is the only field this binds. `models.location` and `knowledge_base.location`
are separate axes with their own stated deviations, decided on 2026-08-27: a guard that
reached them would break the shipped configuration, because Discovery Engine serves no Cloud
region at all. And unlike the sibling repositories, this deploy region is a runtime input
(`GCP_REGION`), so the guard travels with the region rather than pinning one.
"""

from __future__ import annotations

import pytest

from cdd_sow_research.config import DocumentAiSettings, KnowledgeBaseSettings, Settings

CONFIG_PATH = "config/settings.yaml"


def _settings(location: str) -> Settings:
    return Settings(document_ai=DocumentAiSettings(location=location))


def test_the_region_itself_is_allowed() -> None:
    assert _settings("asia-southeast1").document_ai.location == "asia-southeast1"


@pytest.mark.parametrize("multi_region", ["us", "eu"])
def test_a_named_multi_region_is_allowed_as_a_stated_deviation(multi_region: str) -> None:
    assert _settings(multi_region).document_ai.location == multi_region


def test_global_is_refused_because_it_names_no_jurisdiction() -> None:
    with pytest.raises(ValueError, match="global"):
        _settings("global")


@pytest.mark.parametrize("elsewhere", ["us-central1", "europe-west2", "asia-northeast1"])
def test_another_single_region_is_refused(elsewhere: str) -> None:
    """A different single region is neither the deploy region nor a multi-region commitment."""
    with pytest.raises(ValueError):
        _settings(elsewhere)


def test_an_empty_location_is_refused_rather_than_inheriting_the_region() -> None:
    """Set-and-empty is not unset: it names nothing, so it must not take the documented default."""
    with pytest.raises(ValueError):
        _settings("")


def test_the_guard_travels_with_a_configured_deploy_region() -> None:
    """GCP_REGION is a runtime input here, so "the deploy region" means the one configured."""
    moved = Settings(region="europe-west4", document_ai=DocumentAiSettings(location="europe-west4"))
    assert moved.document_ai.location == "europe-west4"


def test_the_shipped_settings_file_still_loads() -> None:
    """The guard must refuse `global`, not the configuration this repository actually ships."""
    assert Settings.load(CONFIG_PATH).document_ai.location == "us"


def test_the_knowledge_base_keeps_its_own_multi_region_axis() -> None:
    """Retrieval location is its own decided deviation (`us`), not this guard's to police."""
    settings = Settings(knowledge_base=KnowledgeBaseSettings(location="us"))
    assert settings.knowledge_base.location == "us"
