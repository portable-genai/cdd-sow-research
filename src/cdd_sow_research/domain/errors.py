"""Domain exceptions for the CDD + Source-of-Wealth Agent (system B1).

Pure-Python exception hierarchy raised by the orchestration services. The domain
layer never imports Google Cloud, ADK, or any framework: these errors let callers
(API, CLI, the Agent Runtime adapter) react to domain-level failures without
coupling to any vendor SDK error type.
"""

from __future__ import annotations


class CddError(Exception):
    """Base class for all domain-level errors raised by B1 services."""


class GuardrailBlockedError(CddError):
    """Raised/flagged when the A1 guardrail blocks an input or output.

    Because the dossier handles customer PII (rule R1), a blocked unsafe request must
    never yield a partial dossier: the orchestrator raises this rather than assembling
    a CDDCase from screened-out content.
    """


class RetrievalEmptyError(CddError):
    """Raised when the governed RAG store (A2) returns no grounding passages.

    A source-of-wealth narrative must be grounded in the case's own KYC documents; an
    empty retrieval after ingestion is treated as a hard error so a dossier is never
    built on no evidence.
    """


class GroundingDisabledError(CddError):
    """Raised when public-web adverse-media grounding is requested but switched off.

    Adverse-media scanning is gated by ``grounding_enabled`` (SPEC §2). Callers that
    explicitly require web grounding can raise this rather than silently skipping it.
    """


class CaseNotFoundError(CddError):
    """Raised when a SoW case id cannot be loaded for the requesting principals."""


class InvalidTransitionError(CddError):
    """Raised when a SoW case is moved between states the state machine forbids."""


class ConcurrencyError(CddError):
    """Raised when an optimistic case write loses to a concurrent update (stale version)."""


class FourEyesError(CddError):
    """Raised when a checker tries to approve their own analysis (maker-checker, P-06)."""


class DocumentNotFoundError(CddError):
    """Raised when an uploaded case document id is unknown to the document store.

    Deliberately indistinguishable from a document the caller may not read: the store
    raises this for both, so probing ids never reveals which ones exist.
    """


class DocumentTooLargeError(CddError):
    """Raised when an upload exceeds the configured per-document byte ceiling."""


class UnsupportedDocumentTypeError(CddError):
    """Raised when an upload's media type is not one the extractor can read."""


class DocumentConflictError(CddError):
    """Raised when restoring a case bundle onto a document id the store already holds.

    Restoring preserves document ids so the dossier's citations still resolve, which
    means a collision has to be refused rather than resolved: overwriting would let a
    bundle replace evidence already in custody. An id that is already present and
    already identical (same bytes) is not a conflict, so reloading the same bundle twice
    is idempotent rather than an error.
    """


class CaseBundleError(CddError):
    """Raised when a complete case bundle is malformed, oversized or fails integrity.

    One error type for every rejection reason in ``domain/case_bundle.py``: a bundle is
    accepted whole or not at all, so a caller never has to distinguish "bad archive"
    from "bad digest" to know it must not be trusted.
    """


class CaseAccessDeniedError(CddError):
    """Raised when a verified principal is not entitled to a case's evidence.

    The ``case:<id>`` retrieval principal is derived server-side from the verified
    Principal (see ``domain/entitlements.py``), never from a client-supplied field;
    a failed entitlement check maps to HTTP 403 at the API layer.
    """
