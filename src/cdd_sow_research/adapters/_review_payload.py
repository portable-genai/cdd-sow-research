"""Shared conversion from an escalated CDD dossier to an ``review-kit`` Review payload.

Lives in the adapter layer (not the pure domain) because it depends on the kit. Redacts the subject
descriptor and summary before they leave the process (R1 / P-04 boundary), using the same
jurisdiction pattern set the redaction adapter uses (``domain/pii_patterns``), so no raw customer
identifier reaches human-review-console over the wire; human-review-console redacts again before its
own audit write (defense in depth). The maker (the agent/analyst who originated the dossier) and the
tenant are asserted here and trusted by human-review-console because this is an authenticated S2S
caller (per-hop OBO is the deferred next layer).
"""

from __future__ import annotations

import re

from review_kit import Citation as KitCitation
from review_kit import Review

from ..domain.models import (
    CDDCase,
    Citation,
    PerpetualKycAssessment,
    QueuePriority,
    RiskBand,
    UboResolution,
)
from ..domain.pii_patterns import all_patterns
from ..domain.policy import UboGraphPolicy

#: The repository name prefixes every source_key: it is the dedup key the
#: human-review-console stores, so it names the producer.
_REPOSITORY = "cdd-sow-research"

# Cap the citations carried on the wire: enough to let a reviewer trace the dossier without
# copying the entire evidence set into the review console.
_MAX_CITATIONS = 8

# A dual-control gate for the highest-risk band; the checker count is policy, not code, but a
# HIGH-risk dossier warranting four-eyes is the conservative default.
_APPROVALS_BY_BAND: dict[RiskBand, int] = {
    RiskBand.LOW: 1,
    RiskBand.MEDIUM: 1,
    RiskBand.HIGH: 2,
    RiskBand.PROHIBITED: 2,
}


def _redact(text: str) -> str:
    """Mask every jurisdiction's national identifiers plus email/phone before the wire.

    Uses the full pattern set (not just the deployment's configured jurisdictions) because the
    review console is a shared sink: a dossier for an SG subject may still quote an HK id, and the
    payload must never carry a raw identifier regardless of which market configured this producer.
    """
    redacted = text
    for info_type, pattern in all_patterns():
        redacted = pattern.sub(f"[{info_type}]", redacted)
    return re.sub(r"\s+", " ", redacted).strip()


def _kit_citations(case: CDDCase) -> tuple[KitCitation, ...]:
    return _limited_citations(tuple(_case_citations(case)))


def _case_citations(case: CDDCase) -> list[Citation]:
    out: list[Citation] = []
    out.extend(case.sow.citations)
    out.extend(case.rating.citations)
    for finding in case.adverse_media.findings if case.adverse_media is not None else ():
        if finding.citation is not None:
            out.append(finding.citation)
    if case.ownership is not None:
        out.extend(case.ownership.citations)
    return out


#: human-review-console severity for a queued perpetual-KYC item. Priority is the pKYC vocabulary;
#: the
#: console speaks the shared severity scale, so the mapping is declared once here.
_SEVERITY_BY_PRIORITY: dict[QueuePriority, str] = {
    QueuePriority.URGENT: "critical",
    QueuePriority.HIGH: "high",
    QueuePriority.STANDARD: "medium",
    QueuePriority.LOW: "low",
}

#: Dual control for the two priorities that can change how a relationship is managed.
_APPROVALS_BY_PRIORITY: dict[QueuePriority, int] = {
    QueuePriority.URGENT: 2,
    QueuePriority.HIGH: 2,
    QueuePriority.STANDARD: 1,
    QueuePriority.LOW: 1,
}


def assessment_to_review(assessment: PerpetualKycAssessment, *, maker: str) -> Review:
    """Build the review a producer submits to human-review-console for a perpetual-KYC re-score.

    Mirrors :func:`case_to_review`: the subject descriptor and summary are redacted with
    the full jurisdiction pattern set before they leave the process, the citations behind
    the queued reasons travel with the item so a checker can trace it, and the source key
    is deterministic per subject and run date so a retry is idempotent.
    """
    item = assessment.queue_item
    priority = item.priority if item is not None else QueuePriority.STANDARD
    descriptor = (
        f"Perpetual-KYC re-score for {assessment.subject_name or assessment.subject_id} "
        f"(id={assessment.subject_id}, as at {assessment.as_of})"
    )
    summary = (
        f"priority={priority.value}; band={assessment.baseline_band.value}"
        f"->{assessment.band.value}; score={assessment.baseline_score:.4f}"
        f"->{assessment.score:.4f}; new_signals={len(assessment.new_signals)}; "
        f"cleared_signals={len(assessment.cleared_signals)}; "
        f"sla_due={item.sla_due if item is not None else 'unset'}"
    )
    citations = _limited_citations(
        item.citations if item is not None else assessment.citations,
    )
    return Review(
        action="perpetual_kyc_rescore",
        subject=_redact(descriptor),
        maker=maker,
        tenant=assessment.tenant,
        summary=_redact(summary),
        severity=_SEVERITY_BY_PRIORITY.get(priority, "medium"),
        required_approvals=_APPROVALS_BY_PRIORITY.get(priority, 1),
        sod_group="cdd-maker-checker",
        case_ref=item.id if item is not None else assessment.subject_id,
        source_key=f"{_REPOSITORY}:{assessment.tenant}:{assessment.subject_id}:pkyc:{assessment.as_of}",
        citations=citations,
    )


def resolution_to_review(
    resolution: UboResolution, *, maker: str, policy: UboGraphPolicy
) -> Review:
    """Build the review a producer submits to human-review-console for a UBO-graph resolution.

    Mirrors :func:`case_to_review` and :func:`assessment_to_review` in every respect that
    matters on the wire (redaction with the full jurisdiction pattern set, capped
    citations, a deterministic source key so a retry is idempotent) and differs in exactly
    the two places a resolution genuinely differs: the severity comes from the opacity
    score rather than a band or a priority, and the summary names the beneficial owners
    and the control basis, which is what a checker is actually being asked to confirm.

    The owners' NAMES are in the summary on purpose: a checker cannot verify a beneficial
    owner they cannot see. They go through the same ``_redact`` pass as every other
    producer's payload, so national identifiers, emails and phone numbers never travel
    even when a registry embedded one in a recorded name.
    """
    graph = resolution.graph
    owners = resolution.beneficial_owners
    descriptor = (
        f"UBO-graph resolution for {resolution.subject_name or resolution.subject_id} "
        f"(id={resolution.subject_id}, as at {resolution.as_of})"
    )
    named = "; ".join(f"{f.name} {f.effective_pct:.2f}%" for f in owners) or "none at threshold"
    summary = (
        f"control_basis={resolution.control_basis.value}; "
        f"opacity={resolution.opacity_score:.4f}; "
        f"layers={graph.depth if graph is not None else 0}; "
        f"parties={len(graph.nodes) if graph is not None else 0}; "
        f"jurisdictions={'/'.join(graph.jurisdictions) if graph is not None else ''}; "
        f"truncated={str(bool(graph is not None and graph.truncated)).lower()}; "
        f"indicators={','.join(resolution.flag_kinds) or 'none'}; "
        f"beneficial_owners={named}"
    )
    return Review(
        action="ubo_graph_resolution",
        subject=_redact(descriptor),
        maker=maker,
        tenant=resolution.tenant,
        summary=_redact(summary),
        severity=_severity_for(resolution.opacity_score, policy.opacity_severity_bands),
        required_approvals=(
            2 if resolution.opacity_score >= policy.dual_control_opacity or not owners else 1
        ),
        sod_group="cdd-maker-checker",
        case_ref=f"ubo-{resolution.subject_id}-{resolution.as_of}",
        source_key=f"{_REPOSITORY}:{resolution.tenant}:{resolution.subject_id}:ubo:{resolution.as_of}",
        citations=_limited_citations(resolution.citations),
    )


def _severity_for(opacity: float, bands: tuple[tuple[float, str], ...]) -> str:
    """Band the opacity score to a review severity using the bank-owned ladder."""
    for floor, severity in bands:
        if opacity >= floor:
            return severity
    return "low"


def _limited_citations(citations: tuple[Citation, ...]) -> tuple[KitCitation, ...]:
    """De-duplicated, capped, redacted kit citations (the shared wire rule)."""
    seen: set[str] = set()
    out: list[KitCitation] = []
    for c in citations:
        if c.source_id in seen:
            continue
        seen.add(c.source_id)
        out.append(KitCitation(source_id=c.source_id, title=c.title, snippet=_redact(c.snippet)))
        if len(out) >= _MAX_CITATIONS:
            break
    return tuple(out)


def case_to_review(case: CDDCase, *, maker: str) -> Review:
    """Build the review a producer submits to human-review-console when a CDD dossier escalates."""
    subject = case.subject
    descriptor = (
        f"CDD dossier for {subject.name} (id={subject.id}, {subject.type.value}, "
        f"jurisdiction={subject.jurisdiction or 'unknown'})"
    )
    media = (
        "not screened"
        if case.adverse_media is None
        else f"{len(case.adverse_media.findings)} findings"
    )
    summary = (
        f"risk={case.rating.band.value}; sow_sources={len(case.sow.sources)}; "
        f"adverse_media={media}; "
        f"owners={len(case.ownership.owners) if case.ownership else 0}"
    )
    return Review(
        action=f"cdd_dossier:{subject.type.value}",
        subject=_redact(descriptor),
        maker=maker,
        tenant=subject.tenant,
        summary=_redact(summary),
        severity=case.rating.band.value,
        required_approvals=_APPROVALS_BY_BAND.get(case.rating.band, 1),
        sod_group="cdd-maker-checker",
        case_ref=case.id,
        source_key=f"{_REPOSITORY}:{subject.tenant}:{case.id}:cdd_dossier",
        citations=_kit_citations(case),
    )
