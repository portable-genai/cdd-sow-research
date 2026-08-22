"""Complete case bundle: the dossier AND the source-document bytes it cites.

The portable dossier envelope (``cdd-dossier/v1``) carries the logical case: the
narrative, the rating, the findings and the citations. What it does NOT carry is the
evidence those citations point at. A citation reading "doc-bank p.11" is only worth
something while the file behind it is still reachable, so a dossier that travels without
its documents is a promise the receiving side cannot check.

This module is the other half of the exit story (P-12): one open, documented archive
holding the dossier, every source document's original bytes, and a manifest that binds
them together by digest. The receiving side needs no vendor tooling to open it: it is a
ZIP with a JSON manifest, readable by ``unzip`` and ``python -m json.tool``.

Layout::

    manifest.json          the envelope (schema version, case, digests)
    dossier.json           the cdd-dossier/v1 payload, byte-for-byte as exported
    documents/<id>         one source document, original bytes, no re-encoding

Integrity, and its honest limits. ``manifest.json`` carries a SHA-256 over
``dossier.json`` and over every document, and :func:`read_case_bundle` re-computes all of
them and refuses the whole bundle when one disagrees, so a truncated transfer or a casual
edit cannot pass as intact. This is a CORRUPTION check, not an authenticity one: the
manifest travels inside the archive it describes, so a party who rewrites a document and
its manifest entry together produces a self-consistent bundle. :func:`build_case_bundle`
therefore also returns the digest OF the manifest, to be carried out of band (the WORM
audit trail, a signature, a transfer receipt); comparing that digest on arrival is what
makes the bundle tamper-EVIDENT rather than merely intact.

Access control is deliberately NOT in this module. ``source_acl_tags`` on each entry is
provenance: it records the tags the exporting deployment used, so an auditor can see what
the evidence was scoped to. It is named ``source_`` precisely because it must never be
applied on the way back in: the restoring side re-derives tags from ITS OWN verified
principal (see :mod:`cdd_sow_research.domain.case_bundle_service`), so a hand-edited bundle
cannot smuggle a foreign tenant tag into the receiving store.

Reading is hostile-input handling, because a bundle arrives from outside: member names
are matched against the exact set the manifest declares (so no path escapes the archive),
the uncompressed size is bounded before any member is read (so a compression bomb is
refused rather than expanded), and duplicate members are rejected outright.

Pure standard library: ``zipfile``, ``hashlib``, ``json``. No cloud SDK, no framework.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import re
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import CaseBundleError
from .models import DocType, StoredDocument

#: The schema identifier written into (and required by) every bundle manifest.
BUNDLE_SCHEMA_VERSION = "cdd-case-bundle/v1"

MANIFEST_NAME = "manifest.json"
DOSSIER_NAME = "dossier.json"
DOCUMENT_PREFIX = "documents/"

#: Ceiling on the total UNCOMPRESSED size of a bundle's members, checked from the
#: archive's own directory before a single byte is decompressed.
DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024
#: Ceiling on how many documents one bundle may carry.
DEFAULT_MAX_DOCUMENTS = 500

#: Document ids are server-minted (``doc-<hex>``). Restricting the character set is what
#: makes ``documents/<id>`` incapable of naming anything outside the archive: no
#: separator, no dot segment, no drive letter can appear in a name that matches.
_DOCUMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: A fixed ZIP timestamp. Bundles are evidence, so exporting the same case twice must
#: produce the same bytes; a wall-clock member time would break that for no benefit
#: (the real export time is a manifest field, which IS covered by the digest).
_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def digest_bytes(payload: bytes) -> str:
    """The canonical digest form used throughout the bundle: ``sha256:<hex>``."""
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def canonical_json(payload: Any) -> bytes:
    """Serialize ``payload`` the one way the digests are computed over.

    Sorted keys and no incidental whitespace, so the same logical dossier digests
    identically no matter which surface serialized it.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


@dataclass(frozen=True, slots=True)
class BundleDocumentEntry:
    """One source document's manifest entry: how to find it and how to check it."""

    document_id: str
    path: str
    sha256: str
    size_bytes: int
    filename: str = ""
    doc_type: DocType = DocType.OTHER
    mime_type: str = ""
    pages: int = 0
    subject_id: str = ""
    uploaded_at: str = ""
    #: The tags the EXPORTING deployment scoped this document to. Provenance only; the
    #: restoring side derives its own (see the module docstring).
    source_acl_tags: tuple[str, ...] = ()

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "filename": self.filename,
            "doc_type": self.doc_type.value,
            "mime_type": self.mime_type,
            "pages": self.pages,
            "subject_id": self.subject_id,
            "uploaded_at": self.uploaded_at,
            "source_acl_tags": list(self.source_acl_tags),
        }


@dataclass(frozen=True, slots=True)
class BundleManifest:
    """The envelope: what this bundle is, and the digests that bind its parts."""

    case_id: str
    exported_at: str
    dossier_sha256: str
    documents: tuple[BundleDocumentEntry, ...] = ()
    schema_version: str = BUNDLE_SCHEMA_VERSION

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "exported_at": self.exported_at,
            "dossier": {"path": DOSSIER_NAME, "sha256": self.dossier_sha256},
            "documents": [entry.to_jsonable() for entry in self.documents],
        }


@dataclass(frozen=True, slots=True)
class LoadedBundle:
    """A bundle that has been read AND fully verified.

    Holding one of these is the proof that every digest matched; nothing in this module
    hands back a partially-checked bundle.
    """

    manifest: BundleManifest
    dossier: dict[str, Any]
    #: ``document_id -> original bytes``, exactly as exported.
    documents: dict[str, bytes]
    #: Digest of the canonical manifest, to compare against an out-of-band record.
    manifest_sha256: str


def build_case_bundle(
    *,
    case_id: str,
    dossier: Mapping[str, Any],
    documents: Sequence[tuple[StoredDocument, bytes]],
    exported_at: str,
) -> tuple[bytes, BundleManifest, str]:
    """Assemble one complete case bundle.

    Returns the archive bytes, the manifest that describes them, and the digest OF that
    manifest (the value to record out of band; see the module docstring on why the
    in-archive digests alone cannot prove authenticity).

    ``documents`` pairs each record with the bytes the store returned for it. The record's
    stored ``sha256`` is NOT copied into the manifest: the digest is recomputed from the
    bytes actually being written, so a store whose metadata has drifted from its blob is
    caught here rather than exported as a bundle that fails verification on arrival.
    """
    dossier_payload = canonical_json(dossier)
    entries: list[BundleDocumentEntry] = []
    for record, content in documents:
        if not _DOCUMENT_ID_RE.match(record.id):
            raise CaseBundleError(f"document id {record.id!r} is not a safe archive member name")
        entries.append(
            BundleDocumentEntry(
                document_id=record.id,
                path=f"{DOCUMENT_PREFIX}{record.id}",
                sha256=digest_bytes(content),
                size_bytes=len(content),
                filename=record.filename,
                doc_type=record.doc_type,
                mime_type=record.mime_type,
                pages=record.pages,
                subject_id=record.subject_id,
                uploaded_at=record.uploaded_at,
                source_acl_tags=tuple(record.acl_tags),
            )
        )
    entries.sort(key=lambda e: e.document_id)
    if len({e.document_id for e in entries}) != len(entries):
        raise CaseBundleError("the same document id was supplied twice")

    manifest = BundleManifest(
        case_id=case_id,
        exported_at=exported_at,
        dossier_sha256=digest_bytes(dossier_payload),
        documents=tuple(entries),
    )
    manifest_payload = canonical_json(manifest.to_jsonable())

    by_id = {record.id: content for record, content in documents}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_member(archive, MANIFEST_NAME, manifest_payload)
        _write_member(archive, DOSSIER_NAME, dossier_payload)
        for entry in entries:
            _write_member(archive, entry.path, by_id[entry.document_id])
    return buffer.getvalue(), manifest, digest_bytes(manifest_payload)


def read_case_bundle(
    data: bytes,
    *,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_documents: int = DEFAULT_MAX_DOCUMENTS,
    expected_manifest_sha256: str = "",
) -> LoadedBundle:
    """Open and fully verify a bundle, or raise :class:`CaseBundleError`.

    Every failure raises the same exception type with a specific message: a caller
    deciding whether to accept a transfer wants the reason, and none of these reasons
    leak anything about the receiving deployment.

    Pass ``expected_manifest_sha256`` when the manifest digest was carried out of band.
    Without it the bundle is checked for corruption; with it, for tampering.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise CaseBundleError(f"not a readable bundle archive: {exc}") from exc

    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise CaseBundleError("bundle contains duplicate members")
        total = sum(info.file_size for info in infos)
        if total > max_total_bytes:
            raise CaseBundleError(
                f"bundle declares {total} uncompressed bytes, over the "
                f"{max_total_bytes} byte ceiling"
            )
        name_set = set(names)
        for required in (MANIFEST_NAME, DOSSIER_NAME):
            if required not in name_set:
                raise CaseBundleError(f"bundle is missing {required}")

        manifest_payload = _read_member(archive, MANIFEST_NAME)
        manifest = _parse_manifest(manifest_payload, max_documents=max_documents)
        manifest_sha256 = digest_bytes(manifest_payload)
        if expected_manifest_sha256 and not hmac.compare_digest(
            manifest_sha256, expected_manifest_sha256
        ):
            raise CaseBundleError("bundle manifest does not match the digest recorded out of band")

        # The declared members and the archive's members must agree exactly, in both
        # directions. A member the manifest does not name is unverified content riding
        # along inside evidence, which is refused rather than ignored.
        declared = {MANIFEST_NAME, DOSSIER_NAME} | {e.path for e in manifest.documents}
        if name_set != declared:
            undeclared = sorted(name_set - declared)
            missing = sorted(declared - name_set)
            raise CaseBundleError(
                "bundle members do not match its manifest "
                f"(undeclared: {undeclared}, missing: {missing})"
            )

        dossier_payload = _read_member(archive, DOSSIER_NAME)
        if not hmac.compare_digest(digest_bytes(dossier_payload), manifest.dossier_sha256):
            raise CaseBundleError("bundle dossier failed its integrity check")
        try:
            dossier = json.loads(dossier_payload)
        except json.JSONDecodeError as exc:
            raise CaseBundleError(f"bundle dossier is not valid JSON: {exc}") from exc
        if not isinstance(dossier, dict):
            raise CaseBundleError("bundle dossier must be a JSON object")

        documents: dict[str, bytes] = {}
        for entry in manifest.documents:
            content = _read_member(archive, entry.path)
            if len(content) != entry.size_bytes:
                raise CaseBundleError(
                    f"document {entry.document_id!r} is {len(content)} bytes, "
                    f"the manifest declares {entry.size_bytes}"
                )
            if not hmac.compare_digest(digest_bytes(content), entry.sha256):
                raise CaseBundleError(f"document {entry.document_id!r} failed its integrity check")
            documents[entry.document_id] = content

    return LoadedBundle(
        manifest=manifest,
        dossier=dossier,
        documents=documents,
        manifest_sha256=manifest_sha256,
    )


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _write_member(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    """Write one member with a fixed timestamp, so the archive is reproducible."""
    info = zipfile.ZipInfo(filename=name, date_time=_FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, payload)


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        with archive.open(name) as handle:
            return handle.read()
    except (KeyError, zipfile.BadZipFile, EOFError) as exc:
        raise CaseBundleError(f"bundle member {name!r} could not be read: {exc}") from exc


def _parse_manifest(payload: bytes, *, max_documents: int) -> BundleManifest:
    try:
        root = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CaseBundleError(f"bundle manifest is not valid JSON: {exc}") from exc
    if not isinstance(root, dict):
        raise CaseBundleError("bundle manifest must be a JSON object")

    version = root.get("schema_version")
    if version != BUNDLE_SCHEMA_VERSION:
        raise CaseBundleError(
            f"unsupported bundle schema {version!r}; this build reads {BUNDLE_SCHEMA_VERSION!r}"
        )
    dossier_ref = root.get("dossier")
    if not isinstance(dossier_ref, dict) or not _is_digest(dossier_ref.get("sha256")):
        raise CaseBundleError("bundle manifest carries no valid dossier digest")

    raw_documents = root.get("documents", [])
    if not isinstance(raw_documents, list):
        raise CaseBundleError("bundle manifest documents must be a list")
    if len(raw_documents) > max_documents:
        raise CaseBundleError(
            f"bundle declares {len(raw_documents)} documents, over the "
            f"{max_documents} document ceiling"
        )

    entries: list[BundleDocumentEntry] = []
    seen: set[str] = set()
    for raw in raw_documents:
        entries.append(_parse_entry(raw, seen))
    return BundleManifest(
        case_id=str(root.get("case_id", "")),
        exported_at=str(root.get("exported_at", "")),
        dossier_sha256=str(dossier_ref["sha256"]),
        documents=tuple(entries),
    )


def _parse_entry(raw: Any, seen: set[str]) -> BundleDocumentEntry:
    if not isinstance(raw, dict):
        raise CaseBundleError("each bundle manifest document must be a JSON object")
    document_id = str(raw.get("document_id", ""))
    if not _DOCUMENT_ID_RE.match(document_id):
        raise CaseBundleError(f"bundle declares an unsafe document id {document_id!r}")
    if document_id in seen:
        raise CaseBundleError(f"bundle declares document {document_id!r} twice")
    seen.add(document_id)
    # Rebuilt from the id rather than trusted: a manifest whose path does not follow
    # from its own id cannot point the reader at a different member.
    path = f"{DOCUMENT_PREFIX}{document_id}"
    if str(raw.get("path", path)) != path:
        raise CaseBundleError(
            f"bundle document {document_id!r} declares a path that is not {path!r}"
        )
    if not _is_digest(raw.get("sha256")):
        raise CaseBundleError(f"bundle document {document_id!r} carries no valid digest")
    size = raw.get("size_bytes", 0)
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise CaseBundleError(f"bundle document {document_id!r} declares an invalid size {size!r}")
    try:
        doc_type = DocType(str(raw.get("doc_type", DocType.OTHER.value)))
    except ValueError:
        doc_type = DocType.OTHER
    pages = raw.get("pages", 0)
    return BundleDocumentEntry(
        document_id=document_id,
        path=path,
        sha256=str(raw["sha256"]),
        size_bytes=size,
        filename=str(raw.get("filename", "")),
        doc_type=doc_type,
        mime_type=str(raw.get("mime_type", "")),
        pages=pages if isinstance(pages, int) and not isinstance(pages, bool) else 0,
        subject_id=str(raw.get("subject_id", "")),
        uploaded_at=str(raw.get("uploaded_at", "")),
        source_acl_tags=tuple(
            str(tag) for tag in raw.get("source_acl_tags", []) if isinstance(tag, str)
        ),
    )


def _is_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(c in "0123456789abcdef" for c in value[7:])
    )
