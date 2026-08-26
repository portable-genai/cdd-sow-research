#!/usr/bin/env python3
"""Remove pre-fix duplicate copies of a case's documents, through the app's own store.

    PYTHONPATH=src python scripts/repair_case_corpus.py --subject <case-id>
    PYTHONPATH=src python scripts/repair_case_corpus.py --subject <case-id> --apply

``document_id`` derives a document's id from its filing, so the same evidence uploaded twice
overwrites. Ids were minted per upload until 2026-08-25, and fixing the derivation cannot
retract what is already stored: the named deployment held 21 accumulated copies of one synthetic
bank statement, and the paired demonstration's citation counts diverged on that history rather
than on any disagreement between the profiles.

**Why this exists at all, rather than a few API calls.** The duplicates were first removed
directly from the Discovery Engine index, the citation counts agreed, and some runs later the
corpus was back. The custody bucket is the source of truth and the search index is a projection
of it, so deleting the projection is not deletion. This goes through
``container.document_store``, which owns both, and through its ACL-checked ``delete``, so a
repair can never reach a document the caller could not already read.

Dry run by default. It prints what it would remove and exits 0 without touching anything;
``--apply`` is the only thing that deletes.
"""

from __future__ import annotations

import argparse
import sys

from cdd_sow_research.config import Settings, build_container
from cdd_sow_research.domain.corpus_repair import plan_repair


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--subject", required=True, help="the case/subject id to repair")
    parser.add_argument(
        "--principal",
        action="append",
        default=[],
        help="an ACL principal to read as; repeatable. Defaults to the case and tenant tags.",
    )
    parser.add_argument("--tenant", default="", help="tenant id, when the store is partitioned")
    parser.add_argument("--apply", action="store_true", help="actually delete; default is a dry run")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    container = build_container(Settings.load())
    store = container.document_store

    principals = tuple(args.principal) or tuple(
        p for p in (f"case:{args.subject}", f"tenant:{args.tenant}" if args.tenant else "") if p
    )
    documents = store.list_documents(principals, args.subject)
    if not documents:
        print(f"no readable documents for subject {args.subject!r}")
        return 0

    contents: dict[str, bytes] = {}
    for record in documents:
        try:
            contents[record.id] = store.get(record.id, principals)
        except Exception as exc:  # noqa: BLE001 - an unreadable document is skipped, never guessed
            print(f"  SKIP {record.id}: bytes unreadable ({type(exc).__name__}); left in place")

    groups = plan_repair(documents, contents)
    removable = [(g, doc_id) for g in groups for doc_id in g.stale_ids]

    print(f"subject {args.subject!r}: {len(documents)} document(s), {len(groups)} filing(s)")
    for group in groups:
        state = "canonical present" if group.canonical_present else "CANONICAL ABSENT, keeping all"
        print(f"  {group.filename} [{group.doc_type.value}] -> {group.canonical_id} ({state})")
        for doc_id in group.stale_ids:
            print(f"      stale: {doc_id}")

    if not removable:
        print("nothing to repair")
        return 0
    if not args.apply:
        print(f"\nDRY RUN: {len(removable)} document(s) would be removed. Re-run with --apply.")
        return 0

    # Custody and the index are two halves and must move together. The index is retracted
    # FIRST: if custody went first and the retraction then failed, the passage would stay
    # citable with its bytes gone -- a citation pointing at nothing, which is worse than the
    # duplicate this is repairing. Retracting first can at worst leave an uncited blob.
    kb = container.knowledge_base
    removed = 0
    retracted = 0
    pending: list[str] = []
    for _group, doc_id in removable:
        try:
            if kb.retract(doc_id, principals):
                retracted += 1
        except NotImplementedError as exc:
            print(f"\nREFUSED: this profile cannot retract an indexed document.\n  {exc}")
            print("Nothing was deleted; custody and the index would have disagreed.")
            return 2
        except Exception as exc:  # noqa: BLE001 - a not-yet-effective retraction is reportable
            if type(exc).__name__ != "RetractionNotYetEffectiveError":
                raise
            # The delete was ACCEPTED; the serving index has not caught up. Custody still goes,
            # because leaving the bytes behind for an index that will drop the passage anyway
            # is the worse of the two inconsistencies -- and the caller is told, by name.
            pending.append(doc_id)
        if store.delete(doc_id, principals):
            removed += 1
        else:
            print(f"  WARN {doc_id}: custody copy already gone")
    print(f"\nretracted {retracted} and removed {removed} of {len(removable)} stale document(s)")
    if pending:
        print(
            f"\n{len(pending)} retraction(s) ACCEPTED BUT NOT YET EFFECTIVE: the serving index "
            "still discloses them. They are deleted and will stop being citable when it "
            "refreshes; re-run to confirm."
        )
        for doc_id in pending:
            print(f"  pending: {doc_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
