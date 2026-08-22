"""The complete case bundle FORMAT: what it guarantees, and what it honestly does not.

These tests drive ``domain/case_bundle.py`` directly, with no store and no API, because
the format's promises have to hold for an archive that arrives from anywhere. The
hostile-input cases are written as archives built by hand rather than by the exporter:
an attacker does not use our writer.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from cdd_sow_research.domain.case_bundle import (
    BUNDLE_SCHEMA_VERSION,
    DOSSIER_NAME,
    MANIFEST_NAME,
    build_case_bundle,
    canonical_json,
    digest_bytes,
    read_case_bundle,
)
from cdd_sow_research.domain.errors import CaseBundleError
from cdd_sow_research.domain.models import DocType, StoredDocument

_DOSSIER = {"subject": {"id": "meridian-logistics", "name": "Meridian Logistics (FICTIONAL)"}}


def _record(document_id: str = "doc-abc123", **overrides) -> StoredDocument:
    base = {
        "id": document_id,
        "filename": "statement.pdf",
        "doc_type": DocType.BANK_STATEMENT,
        "mime_type": "application/pdf",
        "subject_id": "meridian-logistics",
        "acl_tags": ("case:meridian-logistics", "tenant:bank-test"),
        "uploaded_at": "2026-08-05T00:00:00+00:00",
        "pages": 3,
    }
    return StoredDocument(**{**base, **overrides})


def _build(documents=None, dossier=None) -> tuple[bytes, str]:
    content, _manifest, manifest_sha256 = build_case_bundle(
        case_id="meridian-logistics",
        dossier=dossier if dossier is not None else _DOSSIER,
        documents=documents if documents is not None else [(_record(), b"%PDF-1.4 fictional")],
        exported_at="2026-08-05T09:00:00+00:00",
    )
    return content, manifest_sha256


def _rezip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


# --------------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------------- #
def test_a_bundle_round_trips_the_dossier_and_the_document_bytes() -> None:
    content, _ = _build()

    loaded = read_case_bundle(content)

    assert loaded.manifest.schema_version == BUNDLE_SCHEMA_VERSION
    assert loaded.manifest.case_id == "meridian-logistics"
    assert loaded.dossier == _DOSSIER
    # The bytes come back identical: no re-encoding, no normalisation.
    assert loaded.documents == {"doc-abc123": b"%PDF-1.4 fictional"}


def test_the_manifest_carries_the_custody_metadata_a_reviewer_needs() -> None:
    content, _ = _build()

    entry = read_case_bundle(content).manifest.documents[0]

    assert entry.filename == "statement.pdf"
    assert entry.doc_type is DocType.BANK_STATEMENT
    assert entry.mime_type == "application/pdf"
    assert entry.pages == 3
    assert entry.uploaded_at == "2026-08-05T00:00:00+00:00"
    # Provenance, deliberately named so it cannot be mistaken for tags to apply.
    assert entry.source_acl_tags == ("case:meridian-logistics", "tenant:bank-test")


def test_a_bundle_with_no_documents_is_valid_not_an_error() -> None:
    """A case whose evidence has not been uploaded yet still exports."""
    content, _ = _build(documents=[])

    loaded = read_case_bundle(content)

    assert loaded.documents == {}
    assert loaded.dossier == _DOSSIER


def test_exporting_the_same_case_twice_produces_identical_bytes() -> None:
    """Bundles are evidence: two exports of one case must be comparable byte for byte."""
    first, _ = _build()
    second, _ = _build()

    assert first == second


# --------------------------------------------------------------------------- #
# Integrity, and the limit of it
# --------------------------------------------------------------------------- #
def test_a_flipped_document_byte_fails_the_integrity_check() -> None:
    content, _ = _build()
    members = _members(content)
    members["documents/doc-abc123"] = b"%PDF-1.4 tampered!"

    with pytest.raises(CaseBundleError, match="failed its integrity check"):
        read_case_bundle(_rezip(members))


def test_an_edited_dossier_fails_the_integrity_check() -> None:
    content, _ = _build()
    members = _members(content)
    members[DOSSIER_NAME] = canonical_json({"subject": {"id": "someone-else"}})

    with pytest.raises(CaseBundleError, match="dossier failed its integrity check"):
        read_case_bundle(_rezip(members))


def test_a_consistently_rewritten_bundle_passes_without_the_out_of_band_digest() -> None:
    """The honest limit, asserted rather than only documented.

    A party who rewrites a document AND its manifest entry produces a self-consistent
    archive. The in-archive digests cannot see that, and this test exists so nobody
    later claims they can.
    """
    content, _ = _build()
    members = _members(content)
    forged = b"%PDF-1.4 forged evidence"
    members["documents/doc-abc123"] = forged
    manifest = json.loads(members[MANIFEST_NAME])
    manifest["documents"][0]["sha256"] = digest_bytes(forged)
    manifest["documents"][0]["size_bytes"] = len(forged)
    members[MANIFEST_NAME] = canonical_json(manifest)

    loaded = read_case_bundle(_rezip(members))

    assert loaded.documents["doc-abc123"] == forged


def test_the_out_of_band_manifest_digest_catches_that_same_rewrite() -> None:
    """...and this is the control that closes it."""
    content, manifest_sha256 = _build()
    members = _members(content)
    forged = b"%PDF-1.4 forged evidence"
    members["documents/doc-abc123"] = forged
    manifest = json.loads(members[MANIFEST_NAME])
    manifest["documents"][0]["sha256"] = digest_bytes(forged)
    manifest["documents"][0]["size_bytes"] = len(forged)
    members[MANIFEST_NAME] = canonical_json(manifest)

    with pytest.raises(CaseBundleError, match="recorded out of band"):
        read_case_bundle(_rezip(members), expected_manifest_sha256=manifest_sha256)


def test_an_untouched_bundle_matches_its_out_of_band_digest() -> None:
    content, manifest_sha256 = _build()

    loaded = read_case_bundle(content, expected_manifest_sha256=manifest_sha256)

    assert loaded.manifest_sha256 == manifest_sha256


def test_a_document_whose_size_disagrees_with_the_manifest_is_refused() -> None:
    content, _ = _build()
    members = _members(content)
    manifest = json.loads(members[MANIFEST_NAME])
    manifest["documents"][0]["size_bytes"] = 999_999
    members[MANIFEST_NAME] = canonical_json(manifest)

    with pytest.raises(CaseBundleError, match="the manifest declares"):
        read_case_bundle(_rezip(members))


# --------------------------------------------------------------------------- #
# Hostile input
# --------------------------------------------------------------------------- #
def test_a_member_the_manifest_does_not_declare_is_refused() -> None:
    """Unverified content riding along inside evidence is refused, not ignored."""
    content, _ = _build()
    members = _members(content)
    members["documents/doc-smuggled"] = b"never checked"

    with pytest.raises(CaseBundleError, match="undeclared"):
        read_case_bundle(_rezip(members))


def test_a_declared_document_missing_from_the_archive_is_refused() -> None:
    content, _ = _build()
    members = _members(content)
    del members["documents/doc-abc123"]

    with pytest.raises(CaseBundleError, match="missing"):
        read_case_bundle(_rezip(members))


@pytest.mark.parametrize(
    "document_id",
    ["../../etc/passwd", "/absolute", "..", "a/b", ""],
)
def test_a_document_id_that_could_escape_the_archive_is_refused(document_id: str) -> None:
    """Path traversal dies at the id, before any name is ever joined to a path."""
    members = {
        MANIFEST_NAME: canonical_json(
            {
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "case_id": "meridian-logistics",
                "exported_at": "2026-08-05T09:00:00+00:00",
                "dossier": {"path": DOSSIER_NAME, "sha256": digest_bytes(canonical_json(_DOSSIER))},
                "documents": [
                    {
                        "document_id": document_id,
                        "path": f"documents/{document_id}",
                        "sha256": digest_bytes(b"x"),
                        "size_bytes": 1,
                    }
                ],
            }
        ),
        DOSSIER_NAME: canonical_json(_DOSSIER),
    }

    with pytest.raises(CaseBundleError, match="unsafe document id"):
        read_case_bundle(_rezip(members))


def test_a_manifest_pointing_a_document_at_another_member_is_refused() -> None:
    """The path is rebuilt from the id, so a manifest cannot redirect the reader."""
    members = {
        MANIFEST_NAME: canonical_json(
            {
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "case_id": "meridian-logistics",
                "exported_at": "2026-08-05T09:00:00+00:00",
                "dossier": {"path": DOSSIER_NAME, "sha256": digest_bytes(canonical_json(_DOSSIER))},
                "documents": [
                    {
                        "document_id": "doc-abc123",
                        "path": DOSSIER_NAME,
                        "sha256": digest_bytes(b"x"),
                        "size_bytes": 1,
                    }
                ],
            }
        ),
        DOSSIER_NAME: canonical_json(_DOSSIER),
    }

    with pytest.raises(CaseBundleError, match="declares a path that is not"):
        read_case_bundle(_rezip(members))


def test_an_archive_that_expands_past_the_ceiling_is_refused_before_it_is_read() -> None:
    """A compression bomb is small on the wire; the ceiling is on the expanded size."""
    payload = b"\x00" * (1024 * 1024)
    members = {MANIFEST_NAME: b"{}", DOSSIER_NAME: b"{}", "documents/doc-big": payload}

    with pytest.raises(CaseBundleError, match="over the .* byte ceiling"):
        read_case_bundle(_rezip(members), max_total_bytes=1024)


def test_more_documents_than_the_ceiling_allows_is_refused() -> None:
    entries = [
        {
            "document_id": f"doc-{index:04d}",
            "path": f"documents/doc-{index:04d}",
            "sha256": digest_bytes(b"x"),
            "size_bytes": 1,
        }
        for index in range(5)
    ]
    members = {
        MANIFEST_NAME: canonical_json(
            {
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "case_id": "meridian-logistics",
                "exported_at": "",
                "dossier": {"path": DOSSIER_NAME, "sha256": digest_bytes(canonical_json(_DOSSIER))},
                "documents": entries,
            }
        ),
        DOSSIER_NAME: canonical_json(_DOSSIER),
    }

    with pytest.raises(CaseBundleError, match="over the 2 document ceiling"):
        read_case_bundle(_rezip(members), max_documents=2)


def test_a_duplicate_member_is_refused() -> None:
    """Two members with one name: which one was verified is not a question worth having."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MANIFEST_NAME, b"{}")
        archive.writestr(MANIFEST_NAME, b"{}")
        archive.writestr(DOSSIER_NAME, b"{}")

    with pytest.raises(CaseBundleError, match="duplicate members"):
        read_case_bundle(buffer.getvalue())


def test_a_bundle_from_an_unknown_schema_is_refused_not_guessed_at() -> None:
    content, _ = _build()
    members = _members(content)
    manifest = json.loads(members[MANIFEST_NAME])
    manifest["schema_version"] = "cdd-case-bundle/v99"
    members[MANIFEST_NAME] = canonical_json(manifest)

    with pytest.raises(CaseBundleError, match="unsupported bundle schema"):
        read_case_bundle(_rezip(members))


def test_something_that_is_not_an_archive_is_refused() -> None:
    with pytest.raises(CaseBundleError, match="not a readable bundle archive"):
        read_case_bundle(b"this is a text file, not a zip")


def test_a_bundle_missing_its_manifest_is_refused() -> None:
    with pytest.raises(CaseBundleError, match=f"missing {MANIFEST_NAME}"):
        read_case_bundle(_rezip({DOSSIER_NAME: b"{}"}))


def test_a_document_id_that_is_not_a_safe_member_name_cannot_be_exported() -> None:
    """The writer refuses the same ids the reader does, so we never emit one."""
    with pytest.raises(CaseBundleError, match="not a safe archive member name"):
        _build(documents=[(_record(document_id="../escape"), b"x")])


def test_the_manifest_digest_is_recomputed_from_the_bytes_not_copied_from_the_record() -> None:
    """A store whose metadata drifted from its blob is caught at export, not on arrival."""
    stale = _record(sha256="0" * 64)

    content, _ = _build(documents=[(stale, b"the real bytes")])

    entry = read_case_bundle(content).manifest.documents[0]
    assert entry.sha256 == digest_bytes(b"the real bytes")
