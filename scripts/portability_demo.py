"""Portability-seam tour: prove the current bounded claims on a laptop, fully offline.

Usage (from the repo root; no cloud, no API key, no emulators):

    PYTHONPATH=src python scripts/portability_demo.py

Six acts, mapping to the portability properties this script can execute:

  1. One-line profile swap ..... the SAME assessment runs offline under ``local`` and
                                 fails fast under ``onprem`` (no domain edits, P-02/P-12)
  2. Interface parity .......... all 19 runtime/data ports instantiate + satisfy their
                                 Protocols under both SDK-free profiles
  3. Tamper-evident audit ...... the hash-chained audit store catches a doctored record
                                 (a softened decision) that plain WORM-at-rest would miss
  4. Open-format round-trip .... the audit trail exports to JSON Lines and reloads on a
                                 FRESH store with every field and the chain intact
  5. Identity seam ............. seeded personas resolve offline and the configured
                                 IdentityPort bindings are displayed
  6. Complete case bundle ...... the dossier AND the original bytes of every document it
                                 cites export as one open archive and reload on a FRESH
                                 document store, ids and digests intact, with a forged
                                 document refused

Exits 0 only if every named check passes. It does not prove one UI artifact across host
origins or external issuers; that is the separate Modes 4/5 browser evidence gate.
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import replace
from pathlib import Path

from cdd_sow_research import ports
from cdd_sow_research.adapters.local.audit import LocalAppendOnlyAuditAdapter
from cdd_sow_research.adapters.local.identity import LocalPersonaIdentityAdapter
from cdd_sow_research.api.deps import build_cdd_service
from cdd_sow_research.config import Container, Settings, instantiate
from cdd_sow_research.domain.identity import RequestContext
from cdd_sow_research.domain.models import CaseInput, Subject, SubjectType
from cdd_sow_research.domain.serialization import audit_event_from_jsonable

CONFIG_PATH = "config/settings.yaml"

PORT_PROTOCOLS: dict[str, type] = {
    "extraction": ports.DocumentExtractionPort,
    "knowledge_base": ports.KnowledgeBaseClientPort,
    "document_store": ports.DocumentStorePort,
    "adverse_media": ports.AdverseMediaPort,
    "registry": ports.CorporateRegistryPort,
    "compliance": ports.ComplianceClientPort,
    "llm": ports.LLMPort,
    "guardrail": ports.GuardrailPort,
    "redaction": ports.PIIRedactionPort,
    "audit": ports.AuditSinkPort,
    "tracer": ports.ObservabilityTracerPort,
    "evaluation": ports.EvaluationGatePort,
    "agent_registry": ports.AgentRegistryPort,
    "tool_catalog": ports.ToolCatalogPort,
    "case_store": ports.CaseStorePort,
    "monitoring_store": ports.MonitoringStorePort,
    "sanctions": ports.SanctionsListProviderPort,
    "browser_flow_store": ports.BrowserFlowStorePort,
    "review_router": ports.ReviewRouterPort,
    "ownership_graph": ports.OwnershipGraphPort,
}

CHECKS: list[tuple[str, bool]] = []


def banner(step: str, title: str) -> None:
    print(f"\n{'=' * 74}\n{step}  {title}\n{'=' * 74}")


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok))
    marker = "PASS" if ok else "FAIL"
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{marker}] {name}{suffix}")


def settings_for(
    profile: str,
    audit_path: str = ":memory:",
    browser_flow_path: str = "",
    documents_path: str = ":memory:",
) -> Settings:
    base = Settings.load(CONFIG_PATH)
    return replace(
        base,
        profile=profile,
        local=replace(
            base.local,
            db_path=":memory:",
            audit_path=audit_path,
            browser_flow_path=browser_flow_path,
            documents_path=documents_path,
        ),
    )


def act_1_profile_swap(audit_path: str) -> None:
    banner("[1/5]", "One-line profile swap: same request, local works, onprem fails fast")
    case_input = CaseInput(
        subject=Subject(
            id="acme-holdings",
            name="Acme Holdings Pte Ltd (FICTIONAL)",
            type=SubjectType.ENTITY,
            jurisdiction="SG",
        )
    )

    local_settings = settings_for("local", audit_path)
    case = build_cdd_service(Container(local_settings)).assess(case_input, actor="demo@laptop")
    citations = len(case.sow.citations)
    print(
        f"  local  -> dossier built offline: risk={case.rating.band.value.upper()}, "
        f"{citations} SoW citations, requires_human_review={case.requires_human_review}"
    )
    check("local profile produced a grounded, cited dossier offline", citations > 0)
    check("maker-checker held (requires_human_review)", case.requires_human_review is True)

    try:
        build_cdd_service(Container(settings_for("onprem"))).assess(case_input, actor="demo@laptop")
        check("onprem profile fails fast (sovereign migration placeholder)", False)
    except NotImplementedError as exc:
        print(f"  onprem -> NotImplementedError: {str(exc)[:80]} (CLI maps this to exit 2)")
        check("onprem profile fails fast (sovereign migration placeholder)", True)

    print("\n  The swap is configuration, not code: config/settings.yaml adapters.llm")
    for profile in ("local", "onprem", "gcp"):
        dotted = local_settings.adapters["llm"].get(profile, "(unbound)")
        print(f"    {profile:<7} -> {dotted}")


def act_2_interface_parity(workdir: Path) -> None:
    banner(
        "[2/5]",
        f"Interface parity: {len(PORT_PROTOCOLS)} ports x {{local, onprem}}, no Google Cloud SDK",
    )
    browser_flow_path = str(workdir / "browser-flow.db")
    configured = set(settings_for("local", browser_flow_path=browser_flow_path).adapters)
    declared = set(PORT_PROTOCOLS)
    map_complete = declared == configured
    check(
        "portability map covers every configured port",
        map_complete,
        (
            f"missing={sorted(configured - declared)}, extra={sorted(declared - configured)}"
            if not map_complete
            else f"{len(declared)} ports"
        ),
    )
    all_ok = True
    for port_name in sorted(PORT_PROTOCOLS):
        row = [f"  {port_name:<16}"]
        for profile in ("local", "onprem"):
            settings = settings_for(profile, browser_flow_path=browser_flow_path)
            adapter = instantiate(settings.adapters[port_name][profile], settings)
            ok = isinstance(adapter, PORT_PROTOCOLS[port_name])
            all_ok &= ok
            row.append(f"{profile}: {type(adapter).__name__} {'ok' if ok else 'MISMATCH'}")
        print(" | ".join(row))
    check("every port satisfies its Protocol under both SDK-free profiles", all_ok)


def act_3_tamper_evidence(audit_path: str, workdir: Path) -> None:
    banner("[3/5]", "Tamper-evident audit: a doctored decision breaks the hash chain")
    audit = LocalAppendOnlyAuditAdapter(settings_for("local", audit_path))
    report = audit.verify_chain()
    print(f"  pristine store : {report.entries} entries, chain intact={report.ok}")
    check("hash chain verifies on the pristine store", report.ok and report.chained > 0)

    tampered_path = workdir / "audit-tampered.db"
    shutil.copy(audit_path, tampered_path)
    conn = sqlite3.connect(tampered_path)
    # An attacker with file access can drop the append-only (WORM) triggers; that is
    # exactly why the hash chain (and the optional external head anchor) sit on top.
    conn.execute("DROP TRIGGER IF EXISTS audit_log_no_update")
    conn.execute("DROP TRIGGER IF EXISTS audit_log_no_delete")
    conn.execute(
        "UPDATE audit_log SET event_json = replace(event_json, "
        '\'"decision":"escalated"\', \'"decision":"allowed"\') '
        "WHERE seq = (SELECT MIN(seq) FROM audit_log)"
    )
    conn.commit()
    conn.close()

    tampered = LocalAppendOnlyAuditAdapter(settings_for("local", str(tampered_path)))
    tampered_report = tampered.verify_chain()
    print(f"  doctored store : ESCALATED silently edited to ALLOWED -> {tampered_report.detail}")
    check(
        "in-place edit detected (verify_chain reports the exact record)",
        not tampered_report.ok and tampered_report.first_bad_seq is not None,
    )


def act_4_round_trip(audit_path: str, workdir: Path) -> None:
    banner("[4/5]", "Open-format export / reload: JSON Lines out, fresh store in, chain intact")
    source = LocalAppendOnlyAuditAdapter(settings_for("local", audit_path))
    export_path = workdir / "audit-export.jsonl"
    exported = source.export_jsonl(export_path)
    print(
        f"  exported {exported} records -> {export_path.name} (documented JSONL, no vendor tooling needed)"
    )

    fresh_path = workdir / "audit-restored.db"
    target = LocalAppendOnlyAuditAdapter(settings_for("local", str(fresh_path)))
    restored = target.import_jsonl(export_path)
    report = target.verify_chain()
    same_payloads = target.read_all() == source.read_all()
    print(f"  restored {restored} records into a fresh store; chain intact={report.ok}")
    check("every record reloaded on a different store", restored == exported and same_payloads)
    check("chain re-verified after reload", report.ok)

    first = audit_event_from_jsonable(target.read_all()[0])
    print(
        f"  rehydrated first record as a domain AuditEvent: action={first.action}, "
        f"decision={first.decision.value}, ts={first.timestamp.isoformat()}"
    )
    check("records rehydrate to first-class domain objects", first.action == "assess_cdd")


def act_5_identity() -> None:
    banner("[5/5]", "Identity seam: personas work offline; configured bindings stay visible")
    settings = settings_for("local")
    identity = LocalPersonaIdentityAdapter(settings)

    default = identity.resolve(RequestContext(headers={}))
    approver = identity.resolve(RequestContext(headers={"x-dev-persona": "approver"}))
    print(f"  no IdP needed: default persona resolves to {default.subject} ({default.tenant})")
    print(f"  persona picker: X-Dev-Persona: approver -> {approver.subject} {approver.principals}")
    check(
        "seeded personas resolve offline with per-user entitlements",
        default.subject != approver.subject and "group:cdd-approver" in approver.principals,
    )

    print("\n  The same IdentityPort, three verification regimes (config only):")
    for profile, dotted in sorted(settings.identity.bindings.items()):
        print(f"    {profile:<13} -> {dotted}")


def act_6_case_bundle(workdir: Path) -> None:
    banner(
        "[6/6]",
        "Complete case bundle: dossier plus source-document bytes out, and back in elsewhere",
    )
    from cdd_sow_research.adapters.local.document_store import LocalDocumentStoreAdapter
    from cdd_sow_research.domain import entitlements
    from cdd_sow_research.domain.case_bundle import (
        MANIFEST_NAME,
        canonical_json,
        digest_bytes,
        read_case_bundle,
    )
    from cdd_sow_research.domain.case_bundle_service import export_bundle, restore_bundle
    from cdd_sow_research.domain.errors import CaseBundleError

    def _archive_members(data: bytes) -> dict[str, bytes]:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def _rebuild(members: dict[str, bytes]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, payload in members.items():
                archive.writestr(name, payload)
        return buffer.getvalue()

    def _refused(data: bytes, expected: str = "") -> bool:
        try:
            read_case_bundle(data, expected_manifest_sha256=expected)
        except CaseBundleError as exc:
            print(f"  refused: {exc}")
            return True
        return False

    from cdd_sow_research.domain.models import DocType

    case_id, tenant = "acme-holdings", "bank-test"
    tags = entitlements.case_tags(case_id, tenant)
    evidence = b"%PDF-1.4 FICTIONAL registry extract for Acme Holdings Pte Ltd"

    source = LocalDocumentStoreAdapter(settings_for("local"))
    record = source.put(
        content=evidence,
        filename="registry-extract.pdf",
        doc_type=DocType.REGISTRY_EXTRACT,
        subject_id=case_id,
        acl_tags=tags,
        mime_type="application/pdf",
    )
    exported = export_bundle(
        source,
        case_id=case_id,
        dossier={"subject": {"id": case_id, "name": "Acme Holdings Pte Ltd (FICTIONAL)"}},
        scope=tags,
        exported_at="2026-08-05T09:00:00+00:00",
    )
    bundle_path = workdir / exported.filename
    bundle_path.write_bytes(exported.content)
    print(
        f"  exported {len(exported.manifest.documents)} document(s) + the dossier -> "
        f"{bundle_path.name} (a ZIP with a JSON manifest; unzip reads it)"
    )
    print(f"  out-of-band manifest digest: {exported.manifest_sha256}")

    target = LocalDocumentStoreAdapter(settings_for("local"))
    restored = restore_bundle(
        target,
        exported.content,
        case_id=case_id,
        acl_tags=tags,
        expected_manifest_sha256=exported.manifest_sha256,
    )
    served = target.get(record.id, tags)
    print(
        f"  reloaded on a FRESH document store: id {record.id} kept, "
        f"{len(served)} bytes served back"
    )
    check(
        "case documents reload on a different store with their ids intact",
        [r.id for r in restored.documents] == [record.id],
    )
    check("restored document bytes are identical to the originals", served == evidence)

    # The dossier's citations are only worth something if the file they name comes back.
    check(
        "the dossier travels in the same archive as the evidence it cites",
        restored.dossier["subject"]["id"] == case_id,
    )

    # And the archive is not merely a container. Two forgeries, two controls.
    members = _archive_members(exported.content)
    document_member = f"documents/{record.id}"

    # (a) Swap the evidence and leave the manifest alone: the in-archive digest sees it.
    swapped = dict(members)
    swapped[document_member] = b"%PDF-1.4 FORGED registry extract"
    check(
        "a swapped document is refused by the bundle's own digest",
        _refused(_rebuild(swapped)),
    )

    # (b) Swap the evidence AND its manifest entry: self-consistent, so only the digest
    # recorded out of band can catch it. This is the honest limit, demonstrated.
    forged_bytes = b"%PDF-1.4 FORGED registry extract"
    rewritten = dict(members)
    rewritten[document_member] = forged_bytes
    manifest = json.loads(members[MANIFEST_NAME])
    manifest["documents"][0]["sha256"] = digest_bytes(forged_bytes)
    manifest["documents"][0]["size_bytes"] = len(forged_bytes)
    rewritten[MANIFEST_NAME] = canonical_json(manifest)
    consistent = _rebuild(rewritten)
    print(
        "  a bundle rewritten consistently (document AND manifest) passes its internal "
        "digests, which is why the manifest digest is carried out of band"
    )
    check(
        "a consistently rewritten bundle is caught by the out-of-band manifest digest",
        _refused(consistent, expected=exported.manifest_sha256),
    )


def main() -> int:
    print("Doc1 portability-seam tour: bounded offline proof")
    print("(runtime seam, local identity, audit integrity and open export). No cloud or API key.")
    workdir = Path(tempfile.mkdtemp(prefix="cdd-portability-"))
    audit_path = str(workdir / "audit.db")

    act_1_profile_swap(audit_path)
    act_2_interface_parity(workdir)
    act_3_tamper_evidence(audit_path, workdir)
    act_4_round_trip(audit_path, workdir)
    act_5_identity()
    act_6_case_bundle(workdir)

    banner("DONE", "Scoreboard: the portability properties exercised by this script")
    failures = [name for name, ok in CHECKS if not ok]
    q_map = {
        "Q1 identity seam: local identity resolves through IdentityPort": [
            "seeded personas resolve offline with per-user entitlements",
        ],
        "Q2 runtime seam: local execution plus parity and a fail-closed exit target": [
            "local profile produced a grounded, cited dossier offline",
            "onprem profile fails fast (sovereign migration placeholder)",
            "portability map covers every configured port",
            "every port satisfies its Protocol under both SDK-free profiles",
        ],
        "Q3 audit data: exports in an open format, reloads elsewhere, tamper-evident": [
            "hash chain verifies on the pristine store",
            "in-place edit detected (verify_chain reports the exact record)",
            "every record reloaded on a different store",
            "chain re-verified after reload",
        ],
        "Q4 case data: the dossier AND its source-document bytes leave and reload intact": [
            "case documents reload on a different store with their ids intact",
            "restored document bytes are identical to the originals",
            "the dossier travels in the same archive as the evidence it cites",
            "a swapped document is refused by the bundle's own digest",
            "a consistently rewritten bundle is caught by the out-of-band manifest digest",
        ],
    }
    passed = dict(CHECKS)
    for question, names in q_map.items():
        ok = all(passed.get(n, False) for n in names)
        print(f"  [{'YES' if ok else 'NO '}] {question}")
    print(f"\n  {len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed. Artifacts: {workdir}")
    if failures:
        print("  FAILED: " + "; ".join(failures))
        return 1
    print("  Bounded proof complete. Cross-host and cross-issuer evidence remains a separate gate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
