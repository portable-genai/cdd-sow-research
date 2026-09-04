"""``cdd-sow`` — the Typer CLI for the B1 CDD + Source-of-Wealth Agent.

This is a thin presentation layer over the domain services. It owns no business logic:
every command builds the wiring from :func:`cdd_sow_research.config.build_container` and the
factory functions in :mod:`cdd_sow_research.api.deps`, invokes one domain service, and
pretty-prints the cited result.

Design constraints honoured here:

* **Import-safe.** Importing this module (e.g. the ``[project.scripts]`` entry point, or
  ``--help``) must never pull in FastAPI, uvicorn, the Google Cloud SDKs, or even the
  domain services. All of those are imported *lazily inside command bodies*, so the
  on-prem/test profile (which installs no Google Cloud SDK) can still load the CLI.
* **Profile-aware.** ``CDD_PROFILE`` selects the adapter stack. The ``onprem`` profile
  binds placeholder adapters that raise ``NotImplementedError``; when a command trips one,
  the CLI fails clearly (exit code 2) with a message that names the migration target.
* **Citations are first-class.** Every artifact is printed with source-and-page
  provenance, because a CDD finding an analyst/MLRO cannot trace is worthless.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import Container
    from ..domain.models import (
        AdverseMediaScreening,
        CDDCase,
        Citation,
        PerpetualKycAssessment,
        SourceOfWealthNarrative,
        UboResolution,
    )

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        "B1 CDD + Source-of-Wealth Agent — cited customer due-diligence dossiers from a "
        "KYC pack, corporate registries and adverse media, on the Gemini Enterprise Agent "
        "Platform (configurable region; default asia-southeast1)."
    ),
)

_PROFILE_EXIT = 2
_RUNTIME_EXIT = 1
_CLI_ACTOR = "cli:operator"


def _container() -> Container:
    from ..config import build_container

    return build_container()


def _deps() -> Any:
    try:
        from ..api import deps  # type: ignore[attr-defined]
    except ImportError as exc:  # pragma: no cover - defensive wiring guard
        _fail(
            f"Service factories (cdd_sow_research.api.deps) are unavailable: {exc}",
            code=_RUNTIME_EXIT,
        )
    return deps


def _fail(message: str, *, code: int = _RUNTIME_EXIT) -> Any:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


def _run(action: str, fn: Any) -> Any:
    """Execute ``fn`` and translate adapter failures into clean CLI errors."""
    from ..config import Settings

    profile = Settings.load().profile
    try:
        return fn()
    except NotImplementedError as exc:
        detail = str(exc) or "method not implemented"
        _fail(
            f"'{action}' is not available under profile '{profile}'. "
            f"This profile uses placeholder adapters (on-prem migration target): {detail}",
            code=_PROFILE_EXIT,
        )
    except KeyError as exc:
        _fail(f"'{action}' has no adapter wired for profile '{profile}': {exc}", code=_PROFILE_EXIT)
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI boundary: no tracebacks to operators
        _fail(f"'{action}' failed: {type(exc).__name__}: {exc}", code=_RUNTIME_EXIT)


# --------------------------------------------------------------------------- #
# Pretty-printing
# --------------------------------------------------------------------------- #
def _fmt_citation(c: Citation) -> str:
    page = f" p.{c.page}" if c.page is not None else ""
    st = c.source_type.value if hasattr(c.source_type, "value") else str(c.source_type)
    url = f" — {c.url}" if c.url else ""
    return f"[{c.source_id}, {st}{page}]{url}"


def _echo_citations(citations: tuple[Citation, ...], indent: str = "  ") -> None:
    if not citations:
        typer.secho(f"{indent}(no citations)", fg=typer.colors.YELLOW)
        return
    typer.secho(f"{indent}Citations:", bold=True)
    for c in citations:
        typer.echo(f"{indent}  - {_fmt_citation(c)}")


def _echo_review_banner(requires_review: bool) -> None:
    if requires_review:
        typer.secho(
            "  [HUMAN REVIEW REQUIRED] maker-checker gate (P-06) — do not act on this "
            "dossier until a qualified reviewer signs off.",
            fg=typer.colors.YELLOW,
            bold=True,
        )


def _print_case(case: CDDCase) -> None:
    typer.secho(f"CDD dossier — {case.subject.name}", bold=True, fg=typer.colors.GREEN)
    _echo_review_banner(case.requires_human_review)
    typer.secho(f"  Risk band: {case.rating.band.value.upper()}", bold=True)
    typer.echo(f"  Rationale: {case.rating.rationale}")
    typer.echo("")
    typer.secho("  Source of wealth:", bold=True)
    typer.echo(f"    {case.sow.narrative}")
    for src in case.sow.sources:
        typer.echo(f"    - [{str(src.kind)}] {src.description} ({src.est_value_band})")
    _echo_citations(case.sow.citations, indent="    ")
    if case.adverse_media is None:
        typer.secho("  Adverse media: not screened", bold=True, fg=typer.colors.YELLOW)
    elif case.adverse_media.findings:
        typer.secho("  Adverse media:", bold=True)
        for f in case.adverse_media.findings:
            typer.echo(f"    - ({f.category.value}/{f.severity.value}) {f.headline}")
    if case.ownership and case.ownership.owners:
        typer.secho("  Beneficial owners:", bold=True)
        for o in case.ownership.owners:
            pep = " [PEP]" if o.is_pep else ""
            typer.echo(f"    - {o.name} {o.pct}%{pep}")


def _print_sow(sow: SourceOfWealthNarrative) -> None:
    typer.secho("Source-of-wealth narrative", bold=True, fg=typer.colors.GREEN)
    _echo_review_banner(sow.requires_human_review)
    typer.echo(f"  {sow.narrative}")
    typer.echo(f"  confidence: {sow.confidence:.2f}")
    for src in sow.sources:
        typer.echo(f"  - [{str(src.kind)}] {src.description} ({src.est_value_band})")
    _echo_citations(sow.citations)


def _print_adverse_media(screening: AdverseMediaScreening | None) -> None:
    if screening is None:
        typer.secho("Adverse media", bold=True, fg=typer.colors.GREEN)
        typer.secho(
            "  NOT SCREENED: no adverse-media backend was reachable on this profile.",
            fg=typer.colors.YELLOW,
        )
        return
    findings = screening.findings
    typer.secho(f"Adverse media ({len(findings)})", bold=True, fg=typer.colors.GREEN)
    if not findings:
        sources = ", ".join(screening.sources) or "the configured sources"
        typer.secho(f"  CLEAR: searched {sources}, no findings.", fg=typer.colors.GREEN)
        return
    for f in findings:
        typer.secho(
            f"  ({f.category.value}/{f.severity.value}) {f.headline} — {f.publisher}", bold=True
        )
        if f.url:
            typer.echo(f"    {f.url}")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@app.command()
def assess(
    subject: str = typer.Argument(..., help="The subject (customer / entity) name to assess."),
    type_: str = typer.Option("entity", "--type", "-t", help="Subject type: individual or entity."),
    jurisdiction: str = typer.Option("", "--jurisdiction", "-j", help="ISO-ish country code."),
) -> None:
    """Build a full cited CDD dossier for a subject."""

    def _do() -> CDDCase:
        from ..domain.models import CaseInput, Subject, SubjectType

        svc = _deps().build_cdd_service(_container())
        sub_type = SubjectType(type_) if type_ in ("individual", "entity") else SubjectType.ENTITY
        subj = Subject(
            id=subject.lower().replace(" ", "-"),
            name=subject,
            type=sub_type,
            jurisdiction=jurisdiction,
        )
        return svc.assess(CaseInput(subject=subj), actor=_CLI_ACTOR)

    case = _run("assess", _do)
    _print_case(case)


@app.command(name="source-of-wealth")
def source_of_wealth(
    subject: str = typer.Argument(..., help="The subject name to build a SoW narrative for."),
    type_: str = typer.Option("entity", "--type", "-t", help="Subject type."),
) -> None:
    """Build a cited source-of-wealth narrative for a subject."""

    def _do() -> SourceOfWealthNarrative:
        from ..domain import _grounded as g
        from ..domain.models import Subject, SubjectType

        container = _container()
        sub_type = SubjectType(type_) if type_ in ("individual", "entity") else SubjectType.ENTITY
        subj = Subject(id=subject.lower().replace(" ", "-"), name=subject, type=sub_type)
        passages = g.retrieve_passages(
            container.knowledge_base,
            f"source of wealth evidence for {subj.name}",
            acl_principals=(f"case:{subj.id}",),
            top_k=container.settings.knowledge_base.top_k,
        )
        return _deps().build_sow_service(container).build(subj, passages, actor=_CLI_ACTOR)

    sow = _run("source-of-wealth", _do)
    _print_sow(sow)


@app.command(name="adverse-media")
def adverse_media(
    subject_name: str = typer.Argument(..., help="The name to scan for adverse media."),
) -> None:
    """Scan public-web adverse media for a subject name."""

    def _do() -> AdverseMediaScreening | None:
        from ..domain.models import Subject

        svc = _deps().build_adverse_media_service(_container())
        return svc.scan(Subject(id="adhoc", name=subject_name), actor=_CLI_ACTOR)

    screening = _run("adverse-media", _do)
    _print_adverse_media(screening)


def _print_perpetual_kyc(assessment: PerpetualKycAssessment) -> None:
    """Print the pKYC outcome: what moved, the arithmetic, and the queue placement."""
    typer.secho(
        f"\nPerpetual KYC - {assessment.subject_name or assessment.subject_id} "
        f"(as at {assessment.as_of})",
        bold=True,
    )
    _echo_review_banner(assessment.requires_human_review)
    typer.echo(
        f"  Score {assessment.baseline_score:.4f} -> {assessment.score:.4f} "
        f"(delta {assessment.score_delta:+.4f}); band "
        f"{assessment.baseline_band.value.upper()} -> {assessment.band.value.upper()}; "
        f"tier {assessment.tier.value.upper()}"
    )
    item = assessment.queue_item
    if item is not None:
        typer.secho(
            f"  Queue: {item.priority.value.upper()}, disposition due {item.sla_due}, "
            f"routed to human-review-console: "
            f"{'yes' if item.routed_to_hrz7 else 'no (retained locally)'}",
            bold=True,
        )
        for reason in item.reasons:
            typer.echo(f"    - {reason}")
    typer.secho("  Signals:", bold=True)
    if not assessment.signals:
        typer.echo("    (none observed)")
    for signal in assessment.signals:
        typer.echo(
            f"    [{signal.change.value}/{signal.severity.value}] "
            f"{signal.source.value}: {signal.summary}"
        )
    typer.secho("  Score arithmetic:", bold=True)
    for uplift in assessment.uplifts:
        typer.echo(f"    {uplift.uplift:+.4f}  {uplift.reason}")
    if assessment.narrative:
        typer.secho("  Narrative (model prose over computed figures):", bold=True)
        typer.echo(f"    {assessment.narrative}")
    _echo_citations(assessment.citations)


@app.command(name="perpetual-kyc")
def perpetual_kyc(
    subject: str = typer.Argument(..., help="The subject name to run a pKYC cycle for."),
    type_: str = typer.Option("entity", "--type", "-t", help="Subject type."),
    jurisdiction: str = typer.Option("", "--jurisdiction", "-j", help="ISO-ish country code."),
    tenant: str = typer.Option("demo-bank", "--tenant", help="Owning tenant for the ACL."),
    as_of: str = typer.Option("", "--as-of", help="ISO date to evaluate for (default today)."),
    last_reviewed: str = typer.Option(
        "", "--last-reviewed", help="ISO date of the last completed periodic review."
    ),
) -> None:
    """Run one perpetual-KYC cycle: detect change, re-score, and queue for human review.

    The operator identity is this CLI's own; in the API the actor and the ACL come from
    the verified Principal instead. The re-score is deterministic and the outcome always
    requires human review: nothing here acts on a relationship.
    """

    def _do() -> PerpetualKycAssessment:
        from datetime import UTC, date, datetime

        from ..domain.models import Subject, SubjectType

        sub_type = SubjectType(type_) if type_ in ("individual", "entity") else SubjectType.ENTITY
        subj = Subject(
            id=subject.lower().replace(" ", "-"),
            name=subject,
            type=sub_type,
            jurisdiction=jurisdiction,
            tenant=tenant,
        )
        when = date.fromisoformat(as_of[:10]) if as_of else datetime.now(UTC).date()
        service = _deps().build_perpetual_kyc_service(_container())
        return service.run(
            subj,
            actor=_CLI_ACTOR,
            principals=("group:cdd-analyst", f"tenant:{tenant}", f"case:{subj.id}"),
            as_of=when,
            last_reviewed=last_reviewed,
        )

    assessment = _run("perpetual-kyc", _do)
    _print_perpetual_kyc(assessment)


def _print_ubo_graph(resolution: UboResolution) -> None:
    """Print the resolution with the MULTIPLICATION behind every effective percentage.

    Showing the working is the point: a beneficial-ownership percentage a reviewer cannot
    check by hand is a number they have to take on faith, which is exactly what a UBO tool
    must not ask of them.
    """
    typer.secho(
        f"\nUBO graph - {resolution.subject_name or resolution.subject_id} "
        f"(as at {resolution.as_of})",
        bold=True,
    )
    _echo_review_banner(resolution.requires_human_review)
    graph = resolution.graph
    if graph is not None:
        typer.echo(
            f"  Structure: {len(graph.nodes)} party(ies), {len(graph.edges)} recorded "
            f"connection(s), depth {graph.depth}, jurisdictions "
            f"{'/'.join(graph.jurisdictions) or 'unknown'}"
            + ("  [TRUNCATED: the picture is incomplete]" if graph.truncated else "")
        )
    typer.secho(
        f"  Control basis: {resolution.control_basis.value.replace('_', ' ').upper()}",
        bold=True,
    )
    typer.echo(f"    {resolution.control_rationale}")
    typer.secho(
        f"  Beneficial owners at or above {resolution.ownership_threshold_pct:.2f}%:", bold=True
    )
    if not resolution.beneficial_owners:
        typer.echo("    (none: the control ladder decided this structure)")
    for finding in resolution.beneficial_owners:
        pep = " [PEP]" if finding.is_pep else ""
        typer.echo(f"    - {finding.name} {finding.effective_pct:.4f}%{pep}")
    typer.secho("  Path arithmetic:", bold=True)
    printed = False
    for finding in resolution.findings:
        for path in finding.paths:
            chain = " -> ".join([*(s.source_name for s in path.steps), path.steps[-1].target_name])
            typer.echo(f"    {finding.name}: {path.arithmetic}")
            typer.echo(f"        via {chain}")
            printed = True
    if not printed:
        typer.echo("    (no equity path resolved)")
    typer.secho(f"  Indicators (opacity {resolution.opacity_score:.4f}):", bold=True)
    if not resolution.flags:
        typer.echo("    (none raised)")
    for flag in resolution.flags:
        typer.echo(f"    [{flag.severity.value}] {flag.kind.value}: {flag.summary}")
    if resolution.narrative:
        typer.secho("  Narrative (model prose over the computed figures):", bold=True)
        typer.echo(f"    {resolution.narrative}")
    _echo_citations(resolution.citations)


@app.command(name="ubo-graph")
def ubo_graph(
    entity: str = typer.Argument(..., help="The corporate entity to resolve."),
    jurisdiction: str = typer.Option("", "--jurisdiction", "-j", help="ISO-ish country code."),
    tenant: str = typer.Option("demo-bank", "--tenant", help="Owning tenant for the ACL."),
    as_of: str = typer.Option("", "--as-of", help="ISO date to evaluate for (default today)."),
) -> None:
    """Resolve an entity's cross-jurisdiction UBO graph and print the working.

    Every percentage is the deterministic product of the cited registry hops; the model
    produces none of them. The outcome always requires human review and is routed to
    human-review-console.
    """

    def _do() -> UboResolution:
        from datetime import UTC, date, datetime

        from ..domain.models import Subject, SubjectType

        subj = Subject(
            id=entity.lower().replace(" ", "-"),
            name=entity,
            type=SubjectType.ENTITY,
            jurisdiction=jurisdiction,
            tenant=tenant,
        )
        when = date.fromisoformat(as_of[:10]) if as_of else datetime.now(UTC).date()
        service = _deps().build_ubo_graph_service(_container())
        return service.resolve(subj, actor=_CLI_ACTOR, as_of=when)

    resolution = _run("ubo-graph", _do)
    _print_ubo_graph(resolution)


@app.command(name="perpetual-kyc-queue")
def perpetual_kyc_queue(
    tenant: str = typer.Option("demo-bank", "--tenant", help="Tenant whose queue to list."),
) -> None:
    """List the tenant's perpetual-KYC review queue, most urgent first."""

    def _do() -> tuple[PerpetualKycAssessment, ...]:
        service = _deps().build_perpetual_kyc_service(_container())
        return service.queue(("group:cdd-analyst", f"tenant:{tenant}"))

    queue = _run("perpetual-kyc-queue", _do)
    if not queue:
        typer.secho("Review queue is empty for this tenant.", fg=typer.colors.YELLOW)
        return
    typer.secho(f"Perpetual-KYC review queue ({len(queue)} item(s))", bold=True)
    for assessment in queue:
        item = assessment.queue_item
        priority = item.priority.value.upper() if item is not None else "STANDARD"
        due = item.sla_due if item is not None else "unset"
        typer.echo(
            f"  [{priority}] {assessment.subject_id} "
            f"score {assessment.baseline_score:.4f} -> {assessment.score:.4f} "
            f"({assessment.band.value}), due {due}"
        )


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address for the API server."),
    port: int = typer.Option(8090, help="TCP port for the API server."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code change (dev only)."),
) -> None:
    """Run the FastAPI app (A2A card, MCP, and REST endpoints) under uvicorn."""

    def _do() -> None:
        import uvicorn

        from ..envread import boolean_setting

        deps = _deps()
        if not hasattr(deps, "create_app"):
            _fail(
                "cdd_sow_research.api.deps does not expose create_app(); cannot start the server.",
                code=_RUNTIME_EXIT,
            )
        settings = _container().settings
        settings.validate_deployment()
        if (
            settings.identity_mode == "local-persona"
            and host not in {"localhost", "127.0.0.1", "::1"}
            and not boolean_setting("CDD_ALLOW_INSECURE_DEMO")
        ):
            _fail(
                "local-persona is loopback-only. Bind localhost, select a secure identity, "
                "or set CDD_ALLOW_INSECURE_DEMO=1 to acknowledge the exposure.",
                code=_PROFILE_EXIT,
            )
        typer.secho(
            f"serving cdd-sow-research on http://{host}:{port} "
            f"(runtime={settings.profile}, identity={settings.identity_mode}, "
            f"channel={settings.channel_mode})",
            fg=typer.colors.GREEN,
        )
        uvicorn.run(
            "cdd_sow_research.api.deps:create_app",
            factory=True,
            host=host,
            port=port,
            reload=reload,
        )

    _run("serve", _do)


@app.command()
def eval(  # noqa: A001 - "eval" is the documented command name
    dataset: str | None = typer.Option(
        None,
        "--dataset",
        "-d",
        help="Path to the golden eval dataset (defaults to the bundled set).",
    ),
    mode: str = typer.Option(
        "smoke",
        "--mode",
        help=(
            "smoke (offline pre-merge check) | gate (model-quality-gate promotion verdict; needs "
            "platform|gcp)."
        ),
    ),
) -> None:
    """Run the eval: --mode smoke (offline pre-merge) or gate (model-quality-gate promotion
    authority).
    """

    def _do() -> int:
        import subprocess
        import sys
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[3]
        script = repo_root / "eval" / "run_eval.py"
        if not script.exists():
            _fail(f"eval gate script not found at {script}", code=_RUNTIME_EXIT)
        cmd = [sys.executable, str(script), "--mode", mode]
        if dataset:
            cmd += ["--dataset", dataset]
        typer.secho(f"running eval gate: {script}", fg=typer.colors.CYAN)
        completed = subprocess.run(cmd, check=False)  # noqa: S603 - trusted local script
        return completed.returncode

    code = _run("eval", _do)
    if code == 0:
        typer.secho("eval gate: PASS", fg=typer.colors.GREEN, bold=True)
    else:
        typer.secho("eval gate: FAIL", fg=typer.colors.RED, bold=True)
    raise typer.Exit(int(code))


def _profile_label() -> str:
    from ..config import Settings

    return Settings.load().profile


# --------------------------------------------------------------------------- #
# Audit trail: tamper-evident verify / open-format export / restore (P-08, P-12)
# --------------------------------------------------------------------------- #
audit_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        "The local hash-chained audit trail: verify integrity, export to JSON Lines, "
        "restore an export into a fresh store. (Under the gcp profile the WORM log "
        "bucket plus Cloud Logging's export APIs give the managed equivalent.)"
    ),
)
app.add_typer(audit_app, name="audit")

_DB_OPTION = typer.Option(
    None,
    "--db",
    help=(
        "Path to the local audit store. Defaults to CDD_LOCAL_AUDIT or "
        "~/.cdd_sow_research/audit.db (the store the local profile writes)."
    ),
)


def _local_audit(db_path: str | None) -> Any:
    """Open the hash-chained local audit store (a maintenance tool, profile-independent)."""
    from dataclasses import replace

    from ..adapters.local.audit import LocalAppendOnlyAuditAdapter
    from ..config import Settings

    settings = Settings.load()
    if db_path:
        settings = replace(settings, local=replace(settings.local, audit_path=db_path))
    return LocalAppendOnlyAuditAdapter(settings)


@audit_app.command()
def verify(db: str | None = _DB_OPTION) -> None:
    """Verify the SHA-256 hash chain over the append-only audit store."""
    report = _run("audit verify", lambda: _local_audit(db).verify_chain())
    status = "INTACT" if report.ok else "BROKEN"
    colour = typer.colors.GREEN if report.ok else typer.colors.RED
    typer.secho(
        f"audit chain {status}: {report.entries} entries "
        f"({report.chained} chained, {report.legacy} legacy)",
        fg=colour,
        bold=True,
    )
    if not report.ok:
        typer.secho(f"  {report.detail}", fg=typer.colors.RED)
        raise typer.Exit(_RUNTIME_EXIT)


@audit_app.command()
def export(
    out: str = typer.Argument(..., help="Output path for the JSON Lines export."),
    db: str | None = _DB_OPTION,
) -> None:
    """Export the audit trail to JSON Lines (open format, chain hashes included)."""
    count = _run("audit export", lambda: _local_audit(db).export_jsonl(out))
    typer.secho(f"exported {count} audit records to {out}", fg=typer.colors.GREEN)


@audit_app.command()
def restore(
    src: str = typer.Argument(..., help="A JSON Lines export produced by 'audit export'."),
    db: str | None = _DB_OPTION,
) -> None:
    """Restore an export into a FRESH store, re-verifying the chain line by line."""
    count = _run("audit restore", lambda: _local_audit(db).import_jsonl(src))
    typer.secho(
        f"restored {count} audit records (chain verified) from {src}", fg=typer.colors.GREEN
    )


# --------------------------------------------------------------------------- #
# Complete case bundles: dossier + source-document bytes, out and back (P-12)
# --------------------------------------------------------------------------- #
bundle_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        "Complete case bundles: export a case's dossier together with the original "
        "bytes of every document it cites, and reload that archive into a fresh "
        "deployment. A ZIP with a JSON manifest, openable without this product."
    ),
)
app.add_typer(bundle_app, name="bundle")

_TENANT_OPTION = typer.Option(
    "",
    "--tenant",
    help=(
        "The tenant to scope the operation to. On restore this is what the documents "
        "are filed under; the bundle's own tags are never applied."
    ),
)


def _cli_scope(case_id: str, tenant: str) -> tuple[str, ...]:
    """The ACL scope a CLI operator acts under: the case, plus the tenant when given.

    The CLI has no IdP, so this is the local-profile analogue of the API's server-derived
    scope, not a way to widen one: it grants exactly the case named on the command line.
    """
    from ..domain import entitlements

    return entitlements.case_tags(case_id, tenant)


@bundle_app.command(name="export")
def bundle_export(
    case_id: str = typer.Argument(..., help="The case (subject id) to export."),
    out: str = typer.Argument(..., help="Output path for the bundle archive (.zip)."),
    tenant: str = _TENANT_OPTION,
) -> None:
    """Export a case's dossier and every readable source document as one archive."""

    def _do() -> tuple[str, int]:
        import json
        from datetime import UTC, datetime
        from pathlib import Path

        from ..domain.case_bundle_service import export_bundle

        container = _container()
        scope = _cli_scope(case_id, tenant)
        # The CLI exports the evidence and whatever dossier JSON sits beside it; a
        # freshly computed dossier would be a different case than the one whose
        # documents are in custody, so the manifest names the case and nothing more.
        dossier = {"subject": {"id": case_id}, "exported_by": "cli"}
        exported = export_bundle(
            container.document_store,
            case_id=case_id,
            dossier=dossier,
            scope=scope,
            exported_at=datetime.now(UTC).isoformat(),
        )
        Path(out).write_bytes(exported.content)
        Path(out + ".manifest.json").write_text(
            json.dumps(
                {"manifest_sha256": exported.manifest_sha256, "bundle": out},
                indent=2,
            )
            + "\n"
        )
        return exported.manifest_sha256, len(exported.manifest.documents)

    manifest_sha256, count = _run("bundle export", _do)
    typer.secho(f"exported case {case_id} with {count} documents to {out}", fg=typer.colors.GREEN)
    typer.secho(
        f"  manifest digest {manifest_sha256}\n"
        f"  recorded beside the archive; carry it separately to make the reload "
        f"tamper-evident rather than merely intact.",
        fg=typer.colors.CYAN,
    )


@bundle_app.command(name="restore")
def bundle_restore(
    case_id: str = typer.Argument(..., help="The case to file the restored evidence under."),
    src: str = typer.Argument(..., help="A bundle archive produced by 'bundle export'."),
    tenant: str = _TENANT_OPTION,
    manifest_sha256: str = typer.Option(
        "",
        "--manifest-sha256",
        help="The digest recorded out of band at export; checked when supplied.",
    ),
) -> None:
    """Verify a bundle and put its documents back in custody under this deployment."""

    def _do() -> tuple[int, tuple[str, ...]]:
        from pathlib import Path

        from ..domain.case_bundle_service import restore_bundle

        container = _container()
        limits = container.settings.document_store
        restored = restore_bundle(
            container.document_store,
            Path(src).read_bytes(),
            case_id=case_id,
            acl_tags=_cli_scope(case_id, tenant),
            expected_manifest_sha256=manifest_sha256,
            max_total_bytes=limits.max_bundle_uncompressed_bytes,
            max_documents=limits.max_bundle_documents,
        )
        return len(restored.documents), restored.retained_existing

    count, retained = _run("bundle restore", _do)
    typer.secho(f"restored {count} documents into case {case_id} from {src}", fg=typer.colors.GREEN)
    if retained:
        typer.secho(
            f"  {len(retained)} already in custody under other tags, kept as they were: "
            f"{', '.join(retained)}",
            fg=typer.colors.YELLOW,
        )


if __name__ == "__main__":  # pragma: no cover
    app()
