"""The case-bundle REST surface: export the whole case, reload it, refuse the rest.

Drives the real local document store and the seeded dev personas, so the cross-tenant
assertions are genuine: ``other-tenant`` is a real authenticated user who simply does not
hold the case's tenant tag.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from tests.fixtures.pdfs import BANK_STATEMENT_PAGES, build_pdf

from cdd_sow_research.api import deps
from cdd_sow_research.api.app import app
from cdd_sow_research.config import LocalSettings, Settings, build_container
from cdd_sow_research.domain.case_bundle import MANIFEST_NAME, canonical_json, digest_bytes

_CASE = "meridian-logistics"
_PDF = build_pdf(BANK_STATEMENT_PAGES)
_SUBJECT = {
    "id": _CASE,
    "name": "Meridian Logistics (FICTIONAL)",
    "type": "entity",
    "jurisdiction": "SG",
}


def _dossier(client: TestClient) -> dict:
    """A REAL dossier from the assess endpoint, not a hand-written stand-in.

    The export route validates the body against ``CddCaseResponse``, so a fixture that
    drifts from the model would make these tests pass for the wrong reason.
    """
    response = client.post(
        "/v1/cdd", json={"subject": _SUBJECT}, headers={"X-Dev-Persona": "analyst"}
    )
    assert response.status_code == 200, response.text
    return response.json()


#: The real (cached) factory, captured once so the swapping below always restores it.
_REAL_GET_CONTAINER = deps.get_container


def _fresh_container() -> object:
    """A container with every store ephemeral: a deployment holding nothing yet."""
    return build_container(
        replace(
            Settings.load(),
            profile="local",
            local=LocalSettings(
                db_path=":memory:", audit_path=":memory:", documents_path=":memory:"
            ),
        )
    )


@contextmanager
def _serving(container: object) -> Iterator[TestClient]:
    """Serve the app from ``container`` for the duration of the block.

    Both the dependency override and the module-level factory are swapped, because route
    code reaches the container through each of them.
    """
    app.dependency_overrides[_REAL_GET_CONTAINER] = lambda: container
    deps.get_container = lambda: container  # type: ignore[assignment]
    try:
        yield TestClient(app, client=("127.0.0.1", 50000))
    finally:
        deps.get_container = _REAL_GET_CONTAINER  # type: ignore[assignment]
        app.dependency_overrides.clear()
        _REAL_GET_CONTAINER.cache_clear()


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with _serving(_fresh_container()) as test_client:
        yield test_client


def _upload(client: TestClient, persona: str = "analyst", filename: str = "statement.pdf"):
    return client.post(
        f"/v1/cases/{_CASE}/documents",
        files={"file": (filename, _PDF, "application/pdf")},
        data={"doc_type": "bank_statement"},
        headers={"X-Dev-Persona": persona},
    )


def _export(client: TestClient, persona: str = "analyst", case_id: str = _CASE):
    return client.post(
        f"/v1/cases/{case_id}/bundle/export",
        json=_dossier(client),
        headers={"X-Dev-Persona": persona},
    )


def _import(client: TestClient, data: bytes, persona: str = "analyst", **form):
    return client.post(
        f"/v1/cases/{_CASE}/bundle/import",
        files={"file": ("bundle.zip", data, "application/zip")},
        data=form,
        headers={"X-Dev-Persona": persona},
    )


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def test_export_returns_an_archive_holding_the_dossier_and_the_documents(client) -> None:
    document = _upload(client).json()

    response = _export(client)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {
            MANIFEST_NAME,
            "dossier.json",
            f"documents/{document['id']}",
        }
        assert archive.read(f"documents/{document['id']}") == _PDF


def test_export_returns_the_manifest_digest_for_out_of_band_custody(client) -> None:
    _upload(client)

    response = _export(client)

    header = response.headers["x-bundle-manifest-sha256"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert header == digest_bytes(archive.read(MANIFEST_NAME))


def test_export_refuses_a_dossier_describing_a_different_case(client) -> None:
    """A mismatched body would produce an archive whose two halves disagree."""
    dossier = _dossier(client)
    response = client.post(
        f"/v1/cases/{_CASE}/bundle/export",
        json={**dossier, "subject": {**dossier["subject"], "id": "someone-else"}},
        headers={"X-Dev-Persona": "analyst"},
    )

    assert response.status_code == 400


def test_another_tenants_documents_never_reach_the_archive(client) -> None:
    mine = _upload(client).json()
    client.post(
        f"/v1/cases/{_CASE}/documents",
        files={"file": ("theirs.pdf", _PDF, "application/pdf")},
        data={"doc_type": "bank_statement"},
        headers={"X-Dev-Persona": "other-tenant"},
    )

    response = _export(client)

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        documents = [n for n in archive.namelist() if n.startswith("documents/")]
    assert documents == [f"documents/{mine['id']}"]


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #
def test_a_bundle_reloads_into_a_fresh_deployment_and_the_citations_still_resolve(
    client,
) -> None:
    document = _upload(client).json()
    exported = _export(client)

    with _serving(_fresh_container()) as target:
        restored = _import(target, exported.content)
        assert restored.status_code == 200
        body = restored.json()
        assert body["case_id"] == _CASE
        assert [d["id"] for d in body["documents"]] == [document["id"]]
        assert body["dossier"]["subject"]["id"] == _CASE
        # The uri in the dossier's citations is rebuilt from the id, so it resolves.
        served = target.get(document["uri"], headers={"X-Dev-Persona": "analyst"})
        assert served.status_code == 200
        assert served.content == _PDF


def test_a_tampered_bundle_is_refused_with_422(client) -> None:
    _upload(client)
    exported = _export(client)
    broken = bytearray(exported.content)
    broken[-30:] = b"\x00" * 30

    response = _import(client, bytes(broken))

    assert response.status_code == 422
    assert "case bundle rejected" in response.json()["detail"]


def test_a_rewritten_bundle_is_refused_when_the_manifest_digest_is_supplied(client) -> None:
    document = _upload(client).json()
    exported = _export(client)
    members = {}
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        for name in archive.namelist():
            members[name] = archive.read(name)
    forged = b"%PDF-1.4 forged"
    members[f"documents/{document['id']}"] = forged
    manifest = json.loads(members[MANIFEST_NAME])
    manifest["documents"][0]["sha256"] = digest_bytes(forged)
    manifest["documents"][0]["size_bytes"] = len(forged)
    members[MANIFEST_NAME] = canonical_json(manifest)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)

    response = _import(
        client,
        buffer.getvalue(),
        manifest_sha256=exported.headers["x-bundle-manifest-sha256"],
    )

    assert response.status_code == 422
    assert "recorded out of band" in response.json()["detail"]


def test_something_that_is_not_a_bundle_is_refused_not_500(client) -> None:
    response = _import(client, b"not a zip at all")

    assert response.status_code == 422


def test_a_reloaded_document_is_scoped_to_the_restoring_tenant_only(client) -> None:
    document = _upload(client).json()
    exported = _export(client)

    with _serving(_fresh_container()) as target:
        _import(target, exported.content)
        mine = target.get(document["uri"], headers={"X-Dev-Persona": "analyst"})
        theirs = target.get(document["uri"], headers={"X-Dev-Persona": "other-tenant"})

    assert mine.status_code == 200
    assert theirs.status_code == 404
