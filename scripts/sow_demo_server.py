"""Live, click-through demo server for the long-running SoW flow (stdlib only).

Holds a real :class:`SowCaseService` over the in-memory case store and advances the
*actual* case one step per click — open + declaration -> ingest Round 0 -> analyse ->
client responds -> ... -> MLRO approves -> sealed snapshot — rendering the audit-first
UI at each step. No Google Cloud, no API key, no extra dependencies.

    PYTHONPATH=src python scripts/sow_demo_server.py [--port 8099]

Then open http://localhost:8099 and click "Next ▶", or drive it with
``scripts/sow_demo_playwright.py`` for a presenter-controlled walkthrough.
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import related_party_demo as rp  # sibling script: reuse the enhanced-diligence builders
import render_sow_ui as r  # sibling script: reuse the exact audit-first rendering
import sow_demo as demo  # sibling script: reuse the synthetic Acme scenario

from cdd_sow_research.adapters.local.case_store import InMemoryCaseStore
from cdd_sow_research.domain.serialization import to_jsonable
from cdd_sow_research.domain.sow_case_service import SowCaseService

# The scripted demo steps. Each "Next" performs the action described by ``next``.
STEPS = [
    {
        "key": "opened",
        "label": "Case opened (client SoW declaration captured)",
        "next": "Ingest Round 0 (first document pack) and analyse",
    },
    {
        "key": "round0",
        "label": "Round 0 analysed: gaps found, RFIs raised",
        "next": "Client responds weeks later (ingest Round 1 and re-analyse)",
    },
    {
        "key": "round1",
        "label": "Round 1 analysed: fewer gaps",
        "next": "Client responds again (ingest Round 2 and re-analyse)",
    },
    {
        "key": "round2",
        "label": "Round 2 analysed: clean, ready for review",
        "next": "Run key-individuals CDD + SoW roll-up (UBOs and directors)",
    },
    {
        "key": "related_parties",
        "label": "Key individuals assessed (UBOs + directors; PEP escalation)",
        "next": "Screen every party against the sanctions / PEP / watchlist snapshot",
    },
    {
        "key": "screening",
        "label": "Sanctions / PEP / watchlist screening done",
        "next": "Compute the risk-based scorecard and CDD tier (SDD/CDD/EDD)",
    },
    {
        "key": "scorecard",
        "label": "Risk scorecard + CDD tier computed",
        "next": "Reconcile Source of Funds (declared inflows vs evidenced)",
    },
    {
        "key": "source_of_funds",
        "label": "Source of Funds reconciled: gaps found",
        "next": "Derive the ongoing-monitoring / periodic-review outcome",
    },
    {
        "key": "monitoring",
        "label": "Ongoing monitoring assessed: re-review triggers found",
        "next": "MLRO approves (four-eyes) and seals the snapshot",
    },
    {"key": "approved", "label": "Approved: immutable snapshot sealed", "next": None},
]

_CONTROL_CSS = """
.democtl{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:12px;
  margin:-24px -18px 16px;padding:12px 18px;background:#0b101a;color:#fff}
.democtl .lbl{font-size:13px}.democtl .lbl b{color:#90b2ff}
.democtl .spacer{flex:1}
.democtl form{margin:0}
.democtl button{font:inherit;font-size:13px;font-weight:600;border:0;border-radius:7px;
  padding:7px 14px;cursor:pointer}
.democtl .next{background:#3a60f0;color:#fff}.democtl .next:disabled{opacity:.4;cursor:default}
.democtl .restart{background:transparent;color:#a6b6cc;border:1px solid #33445b}
.democtl .pct{font-variant-numeric:tabular-nums;color:#cdd7e4;font-size:12px}
"""


class DemoSession:
    """Drives the real SoW case forward one scripted step at a time."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        from cdd_sow_research.config import Settings

        self.store = InMemoryCaseStore()
        # Bank-owned policy from settings drives the deterministic engines (SoW gap
        # tolerances, scorecard, SoF, monitoring) throughout the walkthrough.
        self._policy = Settings.load().policy
        self.svc = SowCaseService.from_policy(self.store, self._policy)
        self.idx = 0
        self.subject = demo.Subject(
            id=demo.CASE_ID,
            name="Acme Holdings Pte Ltd (FICTIONAL)",
            type=demo.SubjectType.ENTITY,
            jurisdiction="SG",
        )
        self.svc.open(demo.CASE_ID, self.subject, demo._declaration(), actor=demo.RM)
        self.data: dict = {
            "case_id": demo.CASE_ID,
            "subject": to_jsonable(self.subject),
            "iterations": [],
            "final_status": "",
            "snapshot": None,
        }
        self.evidence_by_id: dict[str, dict] = {}
        # Enhanced-diligence artifacts carried between the later steps.
        self._review = None
        self._screening = None
        self._scorecard = None
        self._sof = None

    @property
    def at_end(self) -> bool:
        return self.idx >= len(STEPS) - 1

    def goto(self, target: int) -> None:
        """Reset and replay to step ``target`` (deterministic, in-memory, instant)."""
        target = max(0, min(target, len(STEPS) - 1))
        self.reset()
        while self.idx < target:
            self.advance()

    def state(self) -> dict:
        """Machine-readable state for the walkthrough's per-step assertions."""
        view = self.data["iterations"][-1]["audit_view"] if self.data["iterations"] else None
        return {
            "step": self.idx,
            "key": STEPS[self.idx]["key"],
            "label": STEPS[self.idx]["label"],
            "total_steps": len(STEPS),
            "at_end": self.at_end,
            "coverage_pct": (round(view["reconciliation"]["coverage_pct"] * 100) if view else None),
            "gaps": len(view["gaps"]) if view else None,
            "rfis": len(view["rfis"]) if view else None,
            "final_status": self.data["final_status"],
        }

    def advance(self) -> None:
        if self.at_end:
            return
        self.idx += 1
        key = STEPS[self.idx]["key"]
        if key.startswith("round"):
            self._advance_round(int(key[-1]))
        elif key == "related_parties":
            self._review = rp.build_related_party_review(self.svc)
            self._attach(
                self.svc.attach_related_parties(
                    demo.CASE_ID, demo.PRINCIPALS, self._review, actor=demo.RM
                )
            )
        elif key == "screening":
            self._screening = rp.build_screening(self._review)
            self._attach(
                self.svc.attach_screening(
                    demo.CASE_ID, demo.PRINCIPALS, self._screening, actor=demo.RM
                )
            )
        elif key == "scorecard":
            self._scorecard = rp.build_scorecard(self._screening, self._policy)
            self._attach(
                self.svc.attach_scorecard(
                    demo.CASE_ID, demo.PRINCIPALS, self._scorecard, actor=demo.RM
                )
            )
        elif key == "source_of_funds":
            self._sof = rp.build_source_of_funds(self._policy)
            self._attach(
                self.svc.attach_source_of_funds(
                    demo.CASE_ID, demo.PRINCIPALS, self._sof, actor=demo.RM
                )
            )
        elif key == "monitoring":
            monitoring = rp.build_monitoring(
                self._scorecard, self._screening, self._sof, self._policy
            )
            self._attach(
                self.svc.attach_monitoring(demo.CASE_ID, demo.PRINCIPALS, monitoring, actor=demo.RM)
            )
        elif key == "approved":
            final = self.svc.review(demo.CASE_ID, demo.PRINCIPALS, approve=True, checker=demo.MLRO)
            self.data["final_status"] = final.status.value
            if self.data["iterations"]:
                self.data["iterations"][-1]["status_after"] = final.status.value
            snap = self.store.get_snapshot(final.id, final.version)
            self.data["snapshot"] = to_jsonable(snap)

    def _advance_round(self, n: int) -> None:
        as_of, items = demo.ROUNDS[n]
        self.svc.add_evidence(demo.CASE_ID, demo.PRINCIPALS, items, actor=demo.RM)
        case = self.svc.analyze(demo.CASE_ID, demo.PRINCIPALS, actor=demo.RM, as_of=as_of)
        for it in items:
            self.evidence_by_id[it.id] = {
                "id": it.id,
                "doc_type": str(it.document.doc_type),
                "evidenced_band": it.evidenced_band,
                "doc_date": it.doc_date,
                "provided_by": it.provided_by,
                "round": case.iterations[-1].no,
            }
        self.data["iterations"].append(
            {
                "no": case.iterations[-1].no,
                "as_of": as_of.date().isoformat(),
                "status_after": case.status.value,
                "added_evidence": [self.evidence_by_id[it.id] for it in items],
                "audit_view": to_jsonable(case.current),
            }
        )

    def _attach(self, case: object) -> None:
        """Refresh the latest rendered iteration with the case's updated audit view.

        The enhanced-diligence steps mutate the SAME (final) round's audit view rather than
        adding a round, so the panels appear on the current page as each is attached.
        """
        if self.data["iterations"]:
            self.data["iterations"][-1]["audit_view"] = to_jsonable(case.current)  # type: ignore[attr-defined]
            self.data["iterations"][-1]["status_after"] = case.status.value  # type: ignore[attr-defined]

    # -- rendering --------------------------------------------------------- #
    def render(self) -> str:
        if self.idx == 0:
            body = self._declaration_body()
            html = r.page("SoW demo — case opened", body)
        elif self.data["iterations"]:
            it = self.data["iterations"][-1]
            html = r.render_iteration(self.data, it, self.evidence_by_id)
        else:  # pragma: no cover - defensive
            html = r.page("SoW demo", "<p>Nothing to show.</p>")
        return self._inject_controls(html)

    def _declaration_body(self) -> str:
        decl = demo._declaration()
        rows = "".join(
            f"<li><span class='tag'>{r.esc(str(s.kind))}</span> {r.esc(s.description)} "
            f"<span class='muted'>declared {r.esc(s.declared_band)}</span></li>"
            for s in decl.sources
        )
        return (
            f"<h1>Source of Wealth — {r.esc(self.subject.name)}</h1>"
            f"<p class='sub'>Long-running case <b class='mono'>{r.esc(demo.CASE_ID)}</b> · SG · "
            "a new case, opened with the client's self-declaration. No evidence yet.</p>"
            + r.steps_bar(0, "")
            + "<section class='panel'><h2>Client declaration (claimed Source of Wealth)"
            f"<span class='statuspill' style='background:#e6ebf2;color:#546b8b'>DRAFT</span></h2>"
            f"<div class='body'><ul class='sources' style='list-style:none;padding:0;margin:0'>{rows}</ul>"
            f"<p class='muted' style='margin-top:10px'>Declared net worth: "
            f"<b>{r.esc(decl.declared_net_worth_band)}</b> — the figure the evidence must reconcile to."
            "</p></div></section>"
            "<p class='foot'>Audit-first SoW view · synthetic fictional data · "
            "deterministic reconciliation/gap engine</p>"
        )

    def _inject_controls(self, html: str) -> str:
        step = STEPS[self.idx]
        nxt = step["next"]
        cov = ""
        if self.data["iterations"]:
            pct = round(
                self.data["iterations"][-1]["audit_view"]["reconciliation"]["coverage_pct"] * 100
            )
            cov = f"<span class='pct'>coverage {pct}%</span>"
        next_btn = (
            f"<form method='post' action='/advance'><button class='next' type='submit'>"
            f"Next ▶ &nbsp;·&nbsp; {r.esc(nxt)}</button></form>"
            if nxt
            else "<button class='next' disabled>Demo complete ✓</button>"
        )
        bar = (
            "<div class='democtl'>"
            f"<span class='lbl'>Step {self.idx + 1}/{len(STEPS)} — <b>{r.esc(step['label'])}</b></span>"
            f"{cov}<span class='spacer'></span>{next_btn}"
            "<form method='post' action='/restart'><button class='restart' type='submit'>Restart</button></form>"
            "</div>"
        )
        html = html.replace("</style>", _CONTROL_CSS + "</style>", 1)
        return html.replace("<div class='wrap'>", "<div class='wrap'>" + bar, 1)


class Handler(BaseHTTPRequestHandler):
    session: DemoSession  # set on the server instance below

    def _send(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, obj: object, status: int = 200) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, to: str = "/") -> None:
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    @property
    def _sess(self) -> DemoSession:
        return self.server.session  # type: ignore[attr-defined]

    def _query(self) -> dict[str, str]:
        from urllib.parse import parse_qs, urlsplit

        return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        with self.server.lock:  # type: ignore[attr-defined]
            if path == "/":
                self._send(self._sess.render())
            elif path == "/timeline":
                self._send(r.render_timeline(self._sess.data))
            elif path == "/state":
                self._send_json(self._sess.state())
            elif path == "/restart":
                # Allowed over GET so the walkthrough can reset with a plain navigation.
                self._sess.reset()
                self._redirect("/")
            elif path == "/goto":
                # Deterministic replay: reset and advance to the requested step. Lets the
                # presenter jump back to any earlier step instantly (it is all in-memory).
                try:
                    target = int(self._query().get("step", "0"))
                except ValueError:
                    target = 0
                self._sess.goto(target)
                self._redirect("/")
            else:
                self._send("<h1>404</h1>", 404)

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        with self.server.lock:  # type: ignore[attr-defined]
            if path == "/advance":
                self._sess.advance()
            elif path == "/restart":
                self._sess.reset()
        self._redirect("/")

    def log_message(self, *args: object) -> None:  # quiet console
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Live SoW demo server")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.session = DemoSession()  # type: ignore[attr-defined]
    server.lock = threading.Lock()  # type: ignore[attr-defined]
    print(f"SoW demo server on http://{args.host}:{args.port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
