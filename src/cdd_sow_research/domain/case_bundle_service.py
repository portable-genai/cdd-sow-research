"""Export and reload a complete case bundle through the DocumentStorePort.

:mod:`cdd_sow_research.domain.case_bundle` owns the FORMAT (what a bundle is, and whether a
given one is intact). This module owns the TRANSACTION: which documents belong in an
export, and what happens to them on the way back into a store.

The two rules that make a reload safe are both enforced here, not in the format:

* **The exported set is the readable set.** Documents come from
  ``list_documents(scope, subject_id=case_id)`` with a scope the caller's side derived
  from its verified principal, so an export can never contain evidence the exporter
  could not already open. There is no "export everything for this case" path.
* **Incoming ACL tags are re-derived, never adopted.** ``restore_bundle`` takes the tags
  to apply as an argument and passes those to the store. The bundle's own
  ``source_acl_tags`` are read for provenance and never for authorization, so a
  hand-edited bundle carrying ``tenant:other-bank`` lands under the receiving
  deployment's tags like any other document.

Pure stdlib; the store arrives as a Protocol, so this works identically against the
managed bucket, the local SQLite blob store and a client's on-prem vault.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .case_bundle import (
    BundleManifest,
    LoadedBundle,
    build_case_bundle,
    read_case_bundle,
)
from .errors import CaseBundleError, DocumentNotFoundError
from .models import StoredDocument

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ports.document_store import DocumentStorePort


@dataclass(frozen=True, slots=True)
class ExportedBundle:
    """A bundle ready to hand over, plus the digest to record out of band."""

    content: bytes
    manifest: BundleManifest
    manifest_sha256: str

    @property
    def filename(self) -> str:
        """A stable, filesystem-safe name for the archive."""
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in self.manifest.case_id)
        return f"case-bundle-{safe or 'case'}.zip"


@dataclass(frozen=True, slots=True)
class RestoredBundle:
    """The outcome of a reload: the dossier, and the evidence now back in custody."""

    dossier: dict[str, Any]
    documents: tuple[StoredDocument, ...]
    manifest: BundleManifest
    manifest_sha256: str
    #: Documents that were already in custody with identical bytes under tags OTHER than
    #: the ones being applied, so the existing custody record was kept rather than
    #: rewritten. Worth surfacing because the reviewer's view of who may open that
    #: document comes from the record that was already there, not from this reload.
    #: Reloading the same bundle into the same deployment leaves this empty: the record
    #: is retained there too, but it is indistinguishable from the one just written, and
    #: reporting an unverifiable difference would be worse than reporting none.
    retained_existing: tuple[str, ...] = ()


def export_bundle(
    store: DocumentStorePort,
    *,
    case_id: str,
    dossier: Mapping[str, Any],
    scope: tuple[str, ...],
    exported_at: str,
) -> ExportedBundle:
    """Package ``case_id``'s dossier together with every document ``scope`` may read.

    A document listed but no longer readable when its bytes are fetched (deleted between
    the two calls, or its tags narrowed) is skipped rather than failing the export: the
    bundle then honestly describes what it contains, which is better than an export that
    intermittently produces nothing.
    """
    pairs: list[tuple[StoredDocument, bytes]] = []
    for record in store.list_documents(scope, subject_id=case_id):
        try:
            content = store.get(record.id, scope)
        except DocumentNotFoundError:
            continue
        pairs.append((record, content))

    content_bytes, manifest, manifest_sha256 = build_case_bundle(
        case_id=case_id,
        dossier=dict(dossier),
        documents=pairs,
        exported_at=exported_at,
    )
    return ExportedBundle(content=content_bytes, manifest=manifest, manifest_sha256=manifest_sha256)


def restore_bundle(
    store: DocumentStorePort,
    data: bytes,
    *,
    case_id: str,
    acl_tags: tuple[str, ...],
    expected_manifest_sha256: str = "",
    max_total_bytes: int | None = None,
    max_documents: int | None = None,
) -> RestoredBundle:
    """Verify a bundle and put its documents back in custody under ``acl_tags``.

    ``acl_tags`` MUST be derived by the caller from its own verified principal (see the
    module docstring). ``case_id`` is likewise the caller's target case: a bundle whose
    manifest names a different case is refused rather than silently relabelled, because
    filing one customer's evidence under another customer's case is the exact failure a
    reviewer would never detect from the dossier alone.

    Nothing is written until every digest in the bundle has been checked, so a corrupt
    archive cannot leave half its documents in the store.
    """
    limits: dict[str, Any] = {}
    if max_total_bytes is not None:
        limits["max_total_bytes"] = max_total_bytes
    if max_documents is not None:
        limits["max_documents"] = max_documents
    loaded: LoadedBundle = read_case_bundle(
        data, expected_manifest_sha256=expected_manifest_sha256, **limits
    )
    if loaded.manifest.case_id != case_id:
        raise CaseBundleError(f"bundle describes case {loaded.manifest.case_id!r}, not {case_id!r}")

    restored: list[StoredDocument] = []
    already: list[str] = []
    for entry in loaded.manifest.documents:
        content = loaded.documents[entry.document_id]
        record = store.restore(
            content=content,
            document_id=entry.document_id,
            filename=entry.filename,
            doc_type=entry.doc_type,
            # The case the receiving side is filing this under, not the one the bundle
            # claims; they are equal by the check above, and stating it this way means a
            # future relabelling path cannot forget to update the custody record.
            subject_id=case_id,
            acl_tags=acl_tags,
            mime_type=entry.mime_type,
            pages=entry.pages,
            uploaded_at=entry.uploaded_at,
        )
        if record.acl_tags != tuple(acl_tags):
            # The store handed back a record it already held (same bytes, different
            # tags) instead of writing ours. Report it: the caller should not assume the
            # document it "restored" is now scoped the way this reload intended.
            already.append(entry.document_id)
        restored.append(record)

    return RestoredBundle(
        dossier=loaded.dossier,
        documents=tuple(restored),
        manifest=loaded.manifest,
        manifest_sha256=loaded.manifest_sha256,
        retained_existing=tuple(already),
    )
