"""Runnable demo of the long-running, auditable SoW flow (synthetic, fictional data).

Drives a single case for ``Acme Holdings`` through three rounds — weeks apart — as an RM
closes gaps with the client: open + declaration -> first pack -> analyse -> RFIs ->
client responds -> analyse -> client responds -> analyse (clean) -> maker-checker approve.

Run it::

    PYTHONPATH=src python scripts/sow_demo.py [out.json]

It prints a per-iteration summary and writes the full audit-view JSON (one entry per
iteration, plus the sealed snapshot) for the UI to render. No cloud, no LLM: the
reconciliation/gap math is deterministic and the narrator is the offline default.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

from cdd_sow_research.adapters.local.case_store import InMemoryCaseStore
from cdd_sow_research.domain.models import (
    Citation,
    DeclaredSource,
    DocType,
    EvidenceItem,
    KycDocument,
    SourceType,
    Subject,
    SubjectType,
    WealthDeclaration,
    WealthSourceKind,
)
from cdd_sow_research.domain.serialization import to_jsonable
from cdd_sow_research.domain.sow_case_service import SowCaseService

CASE_ID = "acme-holdings"
PRINCIPALS = ("case:acme-holdings",)
RM = "rm.tan@bank.test"
MLRO = "mlro.lim@bank.test"
K = WealthSourceKind
DOC = DocType


def _doc(doc_id: str, doc_type: DocType) -> KycDocument:
    return KycDocument(id=doc_id, doc_type=doc_type, acl_tags=PRINCIPALS)


def _cite(source_id: str, title: str, page: int) -> Citation:
    return Citation(source_id=source_id, source_type=SourceType.DOCUMENT, title=title, page=page)


def _evidence(
    eid: str,
    doc_type: DocType,
    kind: WealthSourceKind,
    band: str,
    doc_date: str,
    title: str,
    page: int = 1,
) -> EvidenceItem:
    return EvidenceItem(
        id=eid,
        document=_doc(eid, doc_type),
        supports_kinds=(kind,),
        evidenced_band=band,
        doc_date=doc_date,
        provided_by="client",
        idempotency_key=eid,
        citations=(_cite(eid, title, page),),
    )


def _declaration() -> WealthDeclaration:
    return WealthDeclaration(
        sources=(
            DeclaredSource(
                K.BUSINESS_OWNERSHIP, "2019 sale of 60% of Acme Logistics Pte Ltd", "USD 25m-50m"
            ),
            DeclaredSource(K.EMPLOYMENT, "Group CEO compensation, 2008-present", "USD 5m-10m"),
            DeclaredSource(K.INVESTMENTS, "Listed equity & fund portfolio", "USD 10m-25m"),
            DeclaredSource(K.INHERITANCE, "2015 family estate", "USD 2m-5m"),
        ),
        declared_net_worth_band="USD 60m-100m",
    )


# Each round: (as_of, list of new evidence). Weeks apart, as the client responds.
ROUNDS: list[tuple[datetime, list[EvidenceItem]]] = [
    (
        datetime(2026, 1, 15, tzinfo=UTC),
        [
            _evidence(
                "spa-2019",
                DOC.OTHER,
                K.BUSINESS_OWNERSHIP,
                "USD 25m-50m",
                "2019-06-01",
                "Share Purchase Agreement",
                4,
            ),
            _evidence(
                "broker-2023",
                DOC.FIN_STATEMENT,
                K.INVESTMENTS,
                "USD 10m-25m",
                "2023-03-01",
                "Brokerage statement 2023",
                2,
            ),
        ],
    ),
    (
        datetime(2026, 2, 10, tzinfo=UTC),
        [
            _evidence(
                "acra-extract",
                DOC.REGISTRY_EXTRACT,
                K.BUSINESS_OWNERSHIP,
                "USD 25m-50m",
                "2026-01-20",
                "ACRA company extract",
                1,
            ),
            _evidence(
                "emp-letter",
                DOC.FIN_STATEMENT,
                K.EMPLOYMENT,
                "USD 5m-10m",
                "2026-01-15",
                "Employer compensation letter",
                1,
            ),
            _evidence(
                "probate-2015",
                DOC.OTHER,
                K.INHERITANCE,
                "USD 2m-5m",
                "2015-08-01",
                "Grant of probate",
                3,
            ),
        ],
    ),
    (
        datetime(2026, 3, 5, tzinfo=UTC),
        [
            _evidence(
                "broker-2026",
                DOC.FIN_STATEMENT,
                K.INVESTMENTS,
                "USD 15m-30m",
                "2026-02-25",
                "Brokerage statement 2026",
                2,
            ),
        ],
    ),
]


def main(out_path: str) -> None:
    store = InMemoryCaseStore()
    svc = SowCaseService(store=store)
    subject = Subject(
        id=CASE_ID,
        name="Acme Holdings Pte Ltd (FICTIONAL)",
        type=SubjectType.ENTITY,
        jurisdiction="SG",
    )

    svc.open(CASE_ID, subject, _declaration(), actor=RM)
    payload: dict = {"case_id": CASE_ID, "subject": to_jsonable(subject), "iterations": []}

    print(f"Opened case {CASE_ID} for {subject.name}\n")
    for as_of, items in ROUNDS:
        svc.add_evidence(CASE_ID, PRINCIPALS, items, actor=RM)
        case = svc.analyze(CASE_ID, PRINCIPALS, actor=RM, as_of=as_of)
        view = case.current
        recon = view.reconciliation
        print(
            f"-- Iteration {case.iterations[-1].no}  ({as_of.date()})  status={case.status.value}"
        )
        print(f"   added: {', '.join(it.id for it in items)}")
        print(
            f"   coverage: {round(recon.coverage_pct * 100)}%   evidenced {recon.evidenced_total_band} / declared {recon.declared_total_band}"
        )
        print(
            f"   gaps ({len(view.gaps)}): "
            + (", ".join(f"{g.kind.value}[{g.severity.value}]" for g in view.gaps) or "none")
        )
        print(
            f"   RFIs ({len(view.rfis)}): " + (", ".join(r.id for r in view.rfis) or "none") + "\n"
        )
        payload["iterations"].append(
            {
                "no": case.iterations[-1].no,
                "as_of": as_of.date().isoformat(),
                "status_after": case.status.value,
                "added_evidence": [
                    {
                        "id": it.id,
                        "doc_type": it.document.doc_type.value,
                        "supports": [str(k) for k in it.supports_kinds],
                        "evidenced_band": it.evidenced_band,
                        "doc_date": it.doc_date,
                        "provided_by": it.provided_by,
                    }
                    for it in items
                ],
                "audit_view": to_jsonable(view),
            }
        )

    snapshot = None
    final = svc.review(CASE_ID, PRINCIPALS, approve=True, checker=MLRO)
    if final.status.value == "approved":
        snap = store.get_snapshot(final.id, final.version)
        snapshot = to_jsonable(snap)
        print(
            f"Approved by {MLRO} (four-eyes; maker was {RM}); snapshot sealed at v{final.version}."
        )
    payload["final_status"] = final.status.value
    payload["snapshot"] = snapshot

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\nWrote audit-view JSON -> {out_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sow_demo.json")
