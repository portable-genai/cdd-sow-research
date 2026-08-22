"""The case-document REST surface: upload, list, serve, delete.

Drives the real local document store and the seeded dev personas, so the cross-tenant
assertions are not vacuous: the ``other-tenant`` persona is a genuine authenticated user
who simply does not hold the document's tenant tag.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from tests.fixtures.pdfs import BANK_STATEMENT_PAGES, build_pdf

from cdd_sow_research.api import deps
from cdd_sow_research.api.app import app
from cdd_sow_research.config import LocalSettings, Settings, build_container

_CASE = "meridian-logistics"
_PDF = build_pdf(BANK_STATEMENT_PAGES)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    # Real settings (so every port is bound as configured), with ephemeral stores.
    container = build_container(
        replace(
            Settings.load(),
            profile="local",
            local=LocalSettings(
                db_path=":memory:", audit_path=":memory:", documents_path=":memory:"
            ),
        )
    )
    deps.get_container.cache_clear()
    app.dependency_overrides[deps.get_container] = lambda: container
    original = deps.get_container
    deps.get_container = lambda: container  # type: ignore[assignment]
    yield TestClient(app, client=("127.0.0.1", 50000))
    deps.get_container = original  # type: ignore[assignment]
    app.dependency_overrides.clear()
    deps.get_container.cache_clear()


def _upload(
    client: TestClient,
    persona: str = "analyst",
    content: bytes = _PDF,
    filename: str = "statement.pdf",
    content_type: str = "application/pdf",
    doc_type: str = "bank_statement",
):
    return client.post(
        f"/v1/cases/{_CASE}/documents",
        files={"file": (filename, content, content_type)},
        data={"doc_type": doc_type},
        headers={"X-Dev-Persona": persona},
    )


def test_upload_returns_the_stored_record(client: TestClient):
    response = _upload(client)

    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "statement.pdf"
    assert body["doc_type"] == "bank_statement"
    assert body["size_bytes"] == len(_PDF)
    assert body["subject_id"] == _CASE
    assert body["uri"] == f"/v1/cases/{_CASE}/documents/{body['id']}"


def test_uploaded_bytes_are_served_back_unchanged(client: TestClient):
    document = _upload(client).json()

    served = client.get(document["uri"], headers={"X-Dev-Persona": "analyst"})

    assert served.status_code == 200
    assert served.content == _PDF
    assert served.headers["content-type"].startswith("application/pdf")
    # Inline, so following a citation lands the reviewer in the document itself.
    assert served.headers["content-disposition"] == 'inline; filename="statement.pdf"'


def test_list_returns_the_case_documents(client: TestClient):
    first = _upload(client, filename="statement.pdf").json()
    second = _upload(client, filename="registry.pdf", doc_type="registry_extract").json()

    listed = client.get(f"/v1/cases/{_CASE}/documents", headers={"X-Dev-Persona": "analyst"}).json()

    assert {d["id"] for d in listed["documents"]} == {first["id"], second["id"]}


def test_another_tenant_cannot_read_or_list_the_document(client: TestClient):
    document = _upload(client).json()

    served = client.get(document["uri"], headers={"X-Dev-Persona": "other-tenant"})
    listed = client.get(f"/v1/cases/{_CASE}/documents", headers={"X-Dev-Persona": "other-tenant"})

    # 404, not 403: a cross-tenant caller learns nothing about what exists.
    assert served.status_code == 404
    assert listed.json()["documents"] == []


def test_unknown_document_id_is_a_404(client: TestClient):
    response = client.get(
        f"/v1/cases/{_CASE}/documents/doc-nope", headers={"X-Dev-Persona": "analyst"}
    )
    assert response.status_code == 404


def test_delete_removes_the_document(client: TestClient):
    document = _upload(client).json()

    deleted = client.delete(document["uri"], headers={"X-Dev-Persona": "analyst"})
    served = client.get(document["uri"], headers={"X-Dev-Persona": "analyst"})

    assert deleted.status_code == 200
    assert served.status_code == 404


def test_another_tenant_cannot_delete_the_document(client: TestClient):
    document = _upload(client).json()

    deleted = client.delete(document["uri"], headers={"X-Dev-Persona": "other-tenant"})

    assert deleted.status_code == 404
    assert client.get(document["uri"], headers={"X-Dev-Persona": "analyst"}).status_code == 200


def test_an_unreadable_media_type_is_refused_with_the_supported_list(client: TestClient):
    response = _upload(
        client, content=b"MZ\x90\x00", filename="tool.exe", content_type="application/x-msdownload"
    )

    assert response.status_code == 415
    assert "application/pdf" in response.json()["detail"]


def test_an_empty_upload_is_refused(client: TestClient):
    response = _upload(client, content=b"")
    assert response.status_code == 400


def test_an_oversized_document_is_refused(client: TestClient, monkeypatch):
    container = deps.get_container()
    limits = container.settings.document_store
    monkeypatch.setattr(limits.__class__, "max_upload_bytes", property(lambda _self: 10))

    response = _upload(client)

    assert response.status_code == 413
    assert "per-document limit" in response.json()["detail"]


def test_an_oversized_document_is_not_buffered_whole_before_refusal(
    client: TestClient, monkeypatch
):
    """The request-level cap is read from Content-Length, which a chunked upload does
    not send, so the per-document ceiling has to stop reading by itself."""
    container = deps.get_container()
    limits = container.settings.document_store
    monkeypatch.setattr(limits.__class__, "max_upload_bytes", property(lambda _self: 4096))

    oversized = b"%PDF-1.4\n" + b"x" * (512 * 1024)
    response = client.post(
        f"/v1/cases/{_CASE}/documents",
        files={"file": ("huge.pdf", oversized, "application/pdf")},
        data={"doc_type": "other"},
        headers={"X-Dev-Persona": "analyst"},
    )

    assert response.status_code == 413
    # Nothing was placed in custody.
    listed = client.get(f"/v1/cases/{_CASE}/documents", headers={"X-Dev-Persona": "analyst"}).json()
    assert listed["documents"] == []


def test_media_type_is_guessed_when_the_browser_does_not_say(client: TestClient):
    document = _upload(client, content_type="application/octet-stream").json()
    assert document["mime_type"] == "application/pdf"
