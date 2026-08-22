from __future__ import annotations

import json

from fastapi.responses import JSONResponse
from tests.fixtures import sample_cases

from cdd_sow_research.api.app import (
    export_portable_dossier,
    import_portable_dossier,
)
from cdd_sow_research.api.schemas import CddCaseResponse, PortableDossierArtifact
from cdd_sow_research.domain.identity import Principal


def _principal() -> Principal:
    return Principal(
        subject="analyst@bank.test",
        principals=("group:cdd-analyst",),
        tenant="bank-test",
        assurance="local",
        source="test",
    )


def test_portable_dossier_exports_and_reloads_with_integrity(cdd_service) -> None:
    case = cdd_service.assess(
        sample_cases.SAMPLE_CASE_INPUT,
        actor="analyst@bank.test",
    )
    dossier = CddCaseResponse.from_domain(case)

    artifact = export_portable_dossier(dossier, _principal())
    assert isinstance(artifact, PortableDossierArtifact)
    assert artifact.schema_version == "cdd-dossier/v1"
    assert artifact.sha256.startswith("sha256:")
    assert json.loads(artifact.model_dump_json())["dossier"]["id"] == case.id

    reloaded = import_portable_dossier(artifact, _principal())
    assert isinstance(reloaded, CddCaseResponse)
    assert reloaded == dossier


def test_portable_dossier_rejects_digest_tampering(cdd_service) -> None:
    case = cdd_service.assess(
        sample_cases.SAMPLE_CASE_INPUT,
        actor="analyst@bank.test",
    )
    dossier = CddCaseResponse.from_domain(case)
    artifact = export_portable_dossier(dossier, _principal())
    assert isinstance(artifact, PortableDossierArtifact)

    tampered = artifact.model_copy(update={"sha256": "sha256:" + ("0" * 64)})
    response = import_portable_dossier(tampered, _principal())
    assert isinstance(response, JSONResponse)
    assert response.status_code == 422
