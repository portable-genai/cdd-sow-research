"""Exporting and reloading a case bundle through a real document store.

Drives the real ``LocalDocumentStoreAdapter`` rather than a fake, so the ACL assertions
mean something: the cross-tenant cases fail here for the same reason they would fail
against the managed bucket, not because a stub said so.

Two properties carry the security weight of this feature, and each has a test that is
RED without its check:

* an export cannot contain evidence the exporter could not already read;
* a reload files evidence under the RESTORING side's tags, never the bundle's own.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from cdd_sow_research.adapters.local.document_store import LocalDocumentStoreAdapter
from cdd_sow_research.config import LocalSettings, Settings
from cdd_sow_research.domain import entitlements
from cdd_sow_research.domain.case_bundle import canonical_json, read_case_bundle
from cdd_sow_research.domain.case_bundle_service import export_bundle, restore_bundle
from cdd_sow_research.domain.errors import (
    CaseBundleError,
    DocumentConflictError,
    DocumentNotFoundError,
)
from cdd_sow_research.domain.models import DocType

_CASE = "meridian-logistics"
_TENANT = "bank-test"
_OTHER_TENANT = "other-bank"
_PDF = b"%PDF-1.4 fictional bank statement"
_DOSSIER = {"subject": {"id": _CASE, "name": "Meridian Logistics (FICTIONAL)"}}


@pytest.fixture()
def store() -> LocalDocumentStoreAdapter:
    return LocalDocumentStoreAdapter(
        replace(Settings.load(), profile="local", local=LocalSettings(documents_path=":memory:"))
    )


def _scope(case_id: str = _CASE, tenant: str = _TENANT) -> tuple[str, ...]:
    return entitlements.case_tags(case_id, tenant)


def _put(store, content: bytes = _PDF, *, case_id: str = _CASE, tenant: str = _TENANT, **kw):
    return store.put(
        content=content,
        filename=kw.get("filename", "statement.pdf"),
        doc_type=kw.get("doc_type", DocType.BANK_STATEMENT),
        subject_id=case_id,
        acl_tags=entitlements.case_tags(case_id, tenant),
        mime_type="application/pdf",
    )


def _export(store, case_id: str = _CASE, tenant: str = _TENANT):
    return export_bundle(
        store,
        case_id=case_id,
        dossier=_DOSSIER,
        scope=_scope(case_id, tenant),
        exported_at="2026-08-05T09:00:00+00:00",
    )


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def test_an_export_carries_the_dossier_and_every_readable_document(store) -> None:
    first = _put(store, filename="statement.pdf")
    second = _put(store, b"%PDF-1.4 registry extract", filename="registry.pdf")

    exported = _export(store)

    loaded = read_case_bundle(exported.content)
    assert loaded.dossier == _DOSSIER
    assert set(loaded.documents) == {first.id, second.id}
    assert loaded.documents[first.id] == _PDF
    assert loaded.documents[second.id] == b"%PDF-1.4 registry extract"


def test_an_export_cannot_contain_another_tenants_evidence(store) -> None:
    """RED the moment the export scope is widened past the caller's own.

    Two independent gates keep the foreign document out (the listing and the byte
    fetch), so this stays green under a mutation to either one alone and only fails
    when the scope itself is wrong. That is the property worth asserting: the bound is
    the scope, not one call site.
    """
    mine = _put(store)
    _put(store, b"%PDF-1.4 not yours", tenant=_OTHER_TENANT)

    exported = _export(store)

    assert set(read_case_bundle(exported.content).documents) == {mine.id}


def test_an_export_carries_only_the_named_cases_documents(store) -> None:
    mine = _put(store)
    _put(store, b"%PDF-1.4 different case", case_id="other-case")

    exported = _export(store)

    assert set(read_case_bundle(exported.content).documents) == {mine.id}


def test_the_out_of_band_digest_is_returned_with_the_archive(store) -> None:
    _put(store)

    exported = _export(store)

    assert exported.manifest_sha256.startswith("sha256:")
    assert (
        read_case_bundle(
            exported.content, expected_manifest_sha256=exported.manifest_sha256
        ).manifest.case_id
        == _CASE
    )


def test_the_archive_filename_is_filesystem_safe(store) -> None:
    exported = export_bundle(
        store,
        case_id="../weird id",
        dossier=_DOSSIER,
        scope=_scope("../weird id"),
        exported_at="",
    )

    assert exported.filename == "case-bundle----weird-id.zip"


# --------------------------------------------------------------------------- #
# Restore
# --------------------------------------------------------------------------- #
def test_a_reload_into_a_fresh_store_returns_the_same_bytes_under_the_same_id(store) -> None:
    """The point of the whole feature: the dossier's citations still resolve after a move."""
    original = _put(store)
    exported = _export(store)

    target = LocalDocumentStoreAdapter(
        replace(Settings.load(), profile="local", local=LocalSettings(documents_path=":memory:"))
    )
    restored = restore_bundle(
        target, exported.content, case_id=_CASE, acl_tags=entitlements.case_tags(_CASE, _TENANT)
    )

    assert [r.id for r in restored.documents] == [original.id]
    # The id survived, so a citation naming it still opens the right file.
    assert target.get(original.id, _scope()) == _PDF
    assert restored.dossier == _DOSSIER


def test_a_reload_preserves_the_custody_metadata_not_the_moment_of_copying(store) -> None:
    original = _put(store, filename="registry.pdf", doc_type=DocType.REGISTRY_EXTRACT)
    store.set_pages(original.id, 7)
    exported = _export(store)

    target = LocalDocumentStoreAdapter(
        replace(Settings.load(), profile="local", local=LocalSettings(documents_path=":memory:"))
    )
    restored = restore_bundle(
        target, exported.content, case_id=_CASE, acl_tags=entitlements.case_tags(_CASE, _TENANT)
    )

    record = restored.documents[0]
    assert record.filename == "registry.pdf"
    assert record.doc_type is DocType.REGISTRY_EXTRACT
    assert record.pages == 7
    # When the bank received the evidence, not when the archive was unpacked.
    assert record.uploaded_at == original.uploaded_at


def test_a_reload_files_evidence_under_the_restoring_sides_tags_not_the_bundles(store) -> None:
    """RED without re-derivation: a hand-edited bundle must not choose its own ACL.

    The bundle is built by a deployment whose documents are tagged for ``other-bank``.
    Reloading it here must produce documents readable by THIS deployment's tenant and by
    nobody else, whatever the archive says.
    """
    foreign = _put(store, tenant=_OTHER_TENANT)
    exported = export_bundle(
        store,
        case_id=_CASE,
        dossier=_DOSSIER,
        scope=_scope(_CASE, _OTHER_TENANT),
        exported_at="",
    )
    assert read_case_bundle(exported.content).manifest.documents[0].source_acl_tags == (
        f"case:{_CASE}",
        f"tenant:{_OTHER_TENANT}",
    )

    target = LocalDocumentStoreAdapter(
        replace(Settings.load(), profile="local", local=LocalSettings(documents_path=":memory:"))
    )
    restored = restore_bundle(
        target, exported.content, case_id=_CASE, acl_tags=entitlements.case_tags(_CASE, _TENANT)
    )

    assert restored.documents[0].acl_tags == (f"case:{_CASE}", f"tenant:{_TENANT}")
    assert target.get(foreign.id, _scope(_CASE, _TENANT)) == _PDF
    # And the tenant the bundle named gains nothing by having been named.
    with pytest.raises(DocumentNotFoundError):
        target.get(foreign.id, _scope(_CASE, _OTHER_TENANT))


def test_a_bundle_for_a_different_case_is_refused_not_relabelled(store) -> None:
    _put(store)
    exported = _export(store)

    with pytest.raises(CaseBundleError, match="describes case"):
        restore_bundle(
            store,
            exported.content,
            case_id="some-other-case",
            acl_tags=entitlements.case_tags("some-other-case", _TENANT),
        )


def test_reloading_the_same_bundle_twice_is_idempotent(store) -> None:
    original = _put(store)
    exported = _export(store)
    target = LocalDocumentStoreAdapter(
        replace(Settings.load(), profile="local", local=LocalSettings(documents_path=":memory:"))
    )
    tags = entitlements.case_tags(_CASE, _TENANT)

    first = restore_bundle(target, exported.content, case_id=_CASE, acl_tags=tags)
    second = restore_bundle(target, exported.content, case_id=_CASE, acl_tags=tags)

    assert [r.id for r in first.documents] == [r.id for r in second.documents] == [original.id]
    assert len(target.list_documents(_scope(), subject_id=_CASE)) == 1
    assert second.retained_existing == ()


def test_a_bundle_cannot_replace_a_document_already_in_custody(store) -> None:
    """Preserving ids means a collision has to be refused, never resolved by overwrite."""
    original = _put(store)
    exported = _export(store)
    target = LocalDocumentStoreAdapter(
        replace(Settings.load(), profile="local", local=LocalSettings(documents_path=":memory:"))
    )
    target.restore(
        content=b"%PDF-1.4 the evidence already held",
        document_id=original.id,
        filename="statement.pdf",
        doc_type=DocType.BANK_STATEMENT,
        subject_id=_CASE,
        acl_tags=entitlements.case_tags(_CASE, _TENANT),
    )

    with pytest.raises(DocumentConflictError, match="already held with different content"):
        restore_bundle(
            target,
            exported.content,
            case_id=_CASE,
            acl_tags=entitlements.case_tags(_CASE, _TENANT),
        )

    assert target.get(original.id, _scope()) == b"%PDF-1.4 the evidence already held"


def test_a_document_already_held_under_other_tags_is_reported_not_silently_kept(store) -> None:
    original = _put(store)
    exported = _export(store)
    target = LocalDocumentStoreAdapter(
        replace(Settings.load(), profile="local", local=LocalSettings(documents_path=":memory:"))
    )
    target.restore(
        content=_PDF,
        document_id=original.id,
        filename="statement.pdf",
        doc_type=DocType.BANK_STATEMENT,
        subject_id=_CASE,
        acl_tags=entitlements.case_tags(_CASE, _OTHER_TENANT),
    )

    restored = restore_bundle(
        target, exported.content, case_id=_CASE, acl_tags=entitlements.case_tags(_CASE, _TENANT)
    )

    assert restored.retained_existing == (original.id,)


def test_a_corrupt_bundle_leaves_the_store_untouched(store) -> None:
    """Verification is complete before the first write, so a reload is all or nothing."""
    _put(store)
    exported = _export(store)
    broken = bytearray(exported.content)
    broken[-40:] = b"\x00" * 40
    target = LocalDocumentStoreAdapter(
        replace(Settings.load(), profile="local", local=LocalSettings(documents_path=":memory:"))
    )

    with pytest.raises(CaseBundleError):
        restore_bundle(
            target, bytes(broken), case_id=_CASE, acl_tags=entitlements.case_tags(_CASE, _TENANT)
        )

    assert target.list_documents(_scope(), subject_id=_CASE) == []


def test_the_out_of_band_digest_is_enforced_on_restore_when_supplied(store) -> None:
    _put(store)
    exported = _export(store)

    with pytest.raises(CaseBundleError, match="recorded out of band"):
        restore_bundle(
            store,
            exported.content,
            case_id=_CASE,
            acl_tags=entitlements.case_tags(_CASE, _TENANT),
            expected_manifest_sha256="sha256:" + ("0" * 64),
        )


def test_a_dossier_field_this_build_does_not_model_survives_the_round_trip(store) -> None:
    """The archive must not be lossy: a newer build's dossier reloads intact."""
    future = {**_DOSSIER, "a_field_from_a_later_build": {"nested": [1, 2, 3]}}
    exported = export_bundle(store, case_id=_CASE, dossier=future, scope=_scope(), exported_at="")

    restored = restore_bundle(
        store, exported.content, case_id=_CASE, acl_tags=entitlements.case_tags(_CASE, _TENANT)
    )

    assert restored.dossier == future
    assert canonical_json(restored.dossier) == canonical_json(future)
