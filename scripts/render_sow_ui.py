"""Render the proposed audit-first SoW UI from the demo JSON into static HTML pages.

Server-side, dependency-free rendering of ``SowAuditView`` (one page per iteration plus
a case timeline), faithful to the design in ``docs/sow-longitudinal-audit-design.md`` §6
and to the existing console palette (ink / regblue, Panel, citation chips). Used to
produce screenshots with the synthetic testing data.

    PYTHONPATH=src python scripts/render_sow_ui.py sow_demo.json out_dir/
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

KIND_LABEL = {
    "business_ownership": "Business ownership",
    "employment": "Employment",
    "investments": "Investments",
    "inheritance": "Inheritance",
    "asset_sale": "Asset sale",
    "other": "Other",
}
SEV_COLOR = {
    "low": ("#eef2f7", "#546b8b"),
    "medium": ("#fef3c7", "#92400e"),
    "high": ("#ffedd5", "#c2410c"),
    "critical": ("#fee2e2", "#b91c1c"),
}
STATUS_LABEL = {
    "rfi_pending": "RFI pending (awaiting client)",
    "ready_for_review": "Ready for maker-checker review",
    "approved": "Approved",
}

CSS = """
:root{--ink-50:#f5f7fa;--ink-100:#e6ebf2;--ink-200:#cdd7e4;--ink-300:#a6b6cc;
--ink-400:#7790ae;--ink-500:#546b8b;--ink-600:#3f5470;--ink-700:#33445b;--ink-800:#1f2a3a;
--regblue-50:#eef4ff;--regblue-100:#dbe7ff;--regblue-600:#2945d6;--regblue-700:#2237ad;
--ok:#059669;--ok-bg:#ecfdf5;--warn:#d97706;--warn-bg:#fffbeb;
--shadow:0 1px 2px rgba(11,16,26,.06),0 8px 24px rgba(11,16,26,.06);}
*{box-sizing:border-box}
body{margin:0;background:var(--ink-50);color:var(--ink-800);
font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
font-size:14px;line-height:1.5;padding:24px 18px}
.wrap{max-width:880px;margin:0 auto}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
h1{font-size:18px;margin:0 0 2px}
.sub{color:var(--ink-500);font-size:13px;margin:0 0 16px}
.sub b{color:var(--ink-800)}
.steps{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 16px}
.step{font-size:12px;padding:4px 10px;border-radius:999px;border:1px solid var(--ink-200);background:#fff;color:var(--ink-500)}
.step.done{background:var(--regblue-50);border-color:var(--regblue-100);color:var(--regblue-700)}
.step.cur{background:var(--regblue-600);border-color:var(--regblue-600);color:#fff;font-weight:600}
.panel{border:1px solid var(--ink-200);background:#fff;border-radius:10px;box-shadow:var(--shadow);margin-bottom:16px}
.panel>h2{border-bottom:1px solid var(--ink-100);padding:11px 16px;margin:0;font-size:13px;font-weight:600;color:var(--ink-800);
display:flex;justify-content:space-between;align-items:center}
.panel>.body{padding:16px}
.statuspill{font-size:11px;font-weight:600;padding:2px 9px;border-radius:999px}
.review{border:1px solid #fcd34d;background:var(--warn-bg);color:#92400e;border-radius:8px;padding:8px 12px;font-size:12px;font-weight:600;margin-bottom:14px}
/* reconciliation */
.recon{display:flex;gap:24px;align-items:center;flex-wrap:wrap}
.recon .fig{font-size:13px;color:var(--ink-600)}
.recon .fig b{display:block;font-size:18px;color:var(--ink-800);font-variant-numeric:tabular-nums}
.cov{flex:1;min-width:220px}
.bar{height:14px;border-radius:8px;background:var(--ink-100);overflow:hidden;border:1px solid var(--ink-200)}
.bar>span{display:block;height:100%;background:linear-gradient(90deg,#3a60f0,#2945d6)}
.cov .lbl{display:flex;justify-content:space-between;font-size:12px;color:var(--ink-500);margin-bottom:4px}
.notes{margin:12px 0 0;padding:0;list-style:none;font-size:12px;color:var(--ink-500)}
.notes li{margin-top:3px}
/* groups */
.group{border:1px solid var(--ink-200);border-radius:9px;margin-bottom:10px;overflow:hidden}
.group>.gh{display:flex;align-items:center;gap:10px;padding:10px 12px;background:var(--ink-50);cursor:default}
.dot{width:9px;height:9px;border-radius:50%;flex:none}
.dot.ok{background:var(--ok)} .dot.warn{background:var(--warn)}
.gh .name{font-weight:600;font-size:13px}
.gh .vals{margin-left:auto;font-size:12px;color:var(--ink-500);text-align:right}
.gh .vals b{color:var(--ink-800)}
.minicov{width:90px;height:8px;border-radius:6px;background:var(--ink-100);border:1px solid var(--ink-200);overflow:hidden;margin-top:3px}
.minicov>span{display:block;height:100%;background:#2945d6}
.ev{padding:9px 12px 4px;border-top:1px dashed var(--ink-200)}
.ev .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
.tag{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--regblue-700);background:var(--regblue-50);border:1px solid var(--regblue-100);border-radius:5px;padding:1px 6px}
.muted{color:var(--ink-400);font-size:12px}
.chip{display:inline-flex;align-items:center;gap:4px;border:1px solid var(--ink-200);background:var(--ink-50);border-radius:6px;padding:1px 7px;font-size:11px;color:var(--ink-700)}
.chip .ty{font-family:ui-monospace,Menlo,monospace;font-weight:600;color:var(--regblue-700)}
.chip .sr{font-family:ui-monospace,Menlo,monospace}
/* gaps + rfi */
.item{display:flex;gap:10px;align-items:flex-start;padding:9px 0;border-bottom:1px solid var(--ink-100)}
.item:last-child{border-bottom:0}
.sev{font-size:10px;font-weight:700;text-transform:uppercase;padding:2px 7px;border-radius:5px;flex:none;margin-top:1px}
.item .txt{font-size:13px}
.item .txt .d{color:var(--ink-500);font-size:12px;margin-top:2px}
.gapk{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--ink-500)}
.docpills{margin-top:4px;display:flex;gap:4px;flex-wrap:wrap}
.docpill{font-size:10px;background:var(--ink-100);color:var(--ink-600);border-radius:5px;padding:1px 6px}
.empty{color:var(--ok);font-size:13px;background:var(--ok-bg);border:1px solid #a7f3d0;border-radius:8px;padding:10px 12px}
.foot{font-size:11px;color:var(--ink-400);text-align:center;margin-top:18px}
/* timeline */
table.tl{width:100%;border-collapse:collapse;font-size:13px}
table.tl th,table.tl td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--ink-100)}
table.tl th{font-size:11px;text-transform:uppercase;color:var(--ink-400);font-weight:600}
table.tl td.num{font-variant-numeric:tabular-nums}
"""


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def sev_pill(sev: str) -> str:
    bg, fg = SEV_COLOR.get(sev, SEV_COLOR["low"])
    return f'<span class="sev" style="background:{bg};color:{fg}">{esc(sev)}</span>'


def chip(c: dict) -> str:
    ty = {"document": "DOC", "registry": "REG", "media": "MEDIA", "regulation": "REGUL"}.get(
        c.get("source_type", ""), c.get("source_type", "")
    )
    page = f" p.{c['page']}" if c.get("page") is not None else ""
    return f'<span class="chip"><span class="ty">{esc(ty)}</span><span class="sr">{esc(c.get("source_id"))}{esc(page)}</span></span>'


def steps_bar(cur: int, final_status: str) -> str:
    labels = ["Round 0", "Round 1", "Round 2", "Approved"]
    out = []
    for i, lab in enumerate(labels):
        cls = "step"
        if i == 3:
            if final_status == "approved" and cur == 3:
                cls += " cur"
            elif final_status == "approved":
                cls += " done"
        else:
            if i < cur:
                cls += " done"
            elif i == cur:
                cls += " cur"
        out.append(f'<span class="{cls}">{esc(lab)}</span>')
    return '<div class="steps">' + "".join(out) + "</div>"


def render_recon(recon: dict) -> str:
    cov = round(recon.get("coverage_pct", 0) * 100)
    notes = "".join(f"<li>• {esc(n)}</li>" for n in recon.get("consistency_notes", []))
    return f"""
    <section class="panel" data-panel="reconciliation"><h2>Reconciliation: declared vs evidenced</h2><div class="body">
      <div class="recon">
        <div class="fig">Declared net worth<b>{esc(recon.get("declared_total_band") or "-")}</b></div>
        <div class="fig">Evidenced<b>{esc(recon.get("evidenced_total_band") or "-")}</b></div>
        <div class="cov">
          <div class="lbl"><span>Coverage</span><span>{cov}%</span></div>
          <div class="bar"><span style="width:{cov}%"></span></div>
        </div>
      </div>
      {f'<ul class="notes">{notes}</ul>' if notes else ""}
    </div></section>"""


def render_groups(view: dict, evidence_by_id: dict) -> str:
    rows = []
    recon_lines = {ln["kind"]: ln for ln in view["reconciliation"].get("lines", [])}
    for g in view.get("groups", []):
        kind = g["kind"]
        line = recon_lines.get(kind, {})
        cov = round(line.get("coverage_pct", 0) * 100)
        dot = "ok" if g.get("corroborated") else "warn"
        ev_html = []
        for eid in g.get("evidence_ids", []):
            ev = evidence_by_id.get(eid)
            if not ev:
                ev_html.append(
                    f'<div class="ev"><div class="row"><span class="mono muted">{esc(eid)}</span>'
                    f'<span class="muted">(submitted in an earlier round)</span></div></div>'
                )
                continue
            cites = "".join(
                chip({"source_type": "document", "source_id": eid, "page": None}) for _ in [0]
            )
            ev_html.append(
                f"""<div class="ev"><div class="row">
                  <span class="tag">{esc(ev["doc_type"])}</span>
                  <span>{esc(eid)}</span>
                  <span class="muted">· {esc(ev["evidenced_band"])} · dated {esc(ev["doc_date"])} · from {esc(ev["provided_by"])} · round {esc(ev.get("round", "?"))}</span>
                  {cites}
                </div></div>"""
            )
        rows.append(
            f"""<div class="group">
              <div class="gh">
                <span class="dot {dot}"></span>
                <span class="name">{esc(KIND_LABEL.get(kind, kind))}</span>
                <span class="vals">declared <b>{esc(g.get("declared_band") or "-")}</b> · evidenced <b>{esc(g.get("evidenced_band") or "-")}</b>
                  <div class="minicov" style="margin-left:auto"><span style="width:{cov}%"></span></div>
                </span>
              </div>
              {"".join(ev_html)}
            </div>"""
        )
    return (
        '<section class="panel" data-panel="groups"><h2>Source groups: at a glance, with proofs</h2>'
        f'<div class="body">{"".join(rows)}</div></section>'
    )


def render_gaps(view: dict) -> str:
    gaps = view.get("gaps", [])
    if not gaps:
        return (
            '<section class="panel" data-panel="gaps"><h2>Gaps</h2><div class="body">'
            '<div class="empty">✓ No outstanding gaps: declared net worth reconciles within tolerance '
            "and every declared source is corroborated. Ready for maker-checker review.</div></div></section>"
        )
    items = "".join(
        f"""<div class="item">{sev_pill(g["severity"])}
          <div class="txt">{esc(g["summary"])} <span class="gapk">[{esc(g["kind"])}]</span>
            <div class="d">{esc(g["detail"])}</div></div></div>"""
        for g in gaps
    )
    return f'<section class="panel" data-panel="gaps"><h2>Gaps the system found <span class="muted">{len(gaps)}</span></h2><div class="body">{items}</div></section>'


def render_rfis(view: dict) -> str:
    rfis = view.get("rfis", [])
    if not rfis:
        return ""
    items = []
    for r in rfis:
        docs = "".join(
            f'<span class="docpill">{esc(d.replace("_", " "))}</span>'
            for d in r.get("suggested_doc_types", [])
        )
        items.append(
            f"""<div class="item">{sev_pill(r["priority"])}
              <div class="txt">{esc(r["ask"])}
                <div class="d">closes <span class="mono">{esc(r["gap_id"])}</span></div>
                {f'<div class="docpills">{docs}</div>' if docs else ""}
              </div></div>"""
        )
    return f'<section class="panel" data-panel="rfis"><h2>Information to request from the client <span class="muted">{len(rfis)}</span></h2><div class="body">{"".join(items)}</div></section>'


ROLE_LABEL = {
    "beneficial_owner": "Beneficial owner",
    "director": "Director",
    "controller": "Controller",
    "authorised_signatory": "Authorised signatory",
    "shareholder": "Shareholder",
    "other": "Other",
}


def render_related_parties(rp: dict | None) -> str:
    """Render the key-individuals (CDD + SoW) roll-up panel, if present."""
    if not rp:
        return ""
    outcomes = rp.get("outcomes", [])
    in_scope = [o for o in outcomes if o.get("in_scope")]
    badge = (
        '<span class="statuspill" style="background:#fef3c7;color:#92400e">ENHANCED REVIEW</span>'
        if rp.get("escalated")
        else '<span class="statuspill" style="background:#ecfdf5;color:#059669">CLEARED</span>'
    )
    rows = []
    for o in outcomes:
        party = o.get("party", {})
        subj = party.get("subject", {})
        scr = o.get("screening", {})
        if not o.get("in_scope"):
            dot = "out"
            color = "#a6b6cc"
        elif o.get("escalates"):
            dot, color = "warn", "var(--warn)"
        else:
            dot, color = "ok", "var(--ok)"
        flags = []
        if scr.get("is_pep"):
            flags.append(
                '<span class="docpill" style="background:#fee2e2;color:#b91c1c">PEP</span>'
            )
        if scr.get("sanctions_hit"):
            flags.append(
                '<span class="docpill" style="background:#fee2e2;color:#b91c1c">SANCTIONS</span>'
            )
        if scr.get("adverse_media"):
            flags.append(
                f'<span class="docpill" style="background:#ffedd5;color:#c2410c">'
                f"adverse media: {esc(scr['adverse_media'])}</span>"
            )
        flags.append(
            '<span class="docpill">ID verified</span>'
            if scr.get("identity_verified")
            else '<span class="docpill" style="background:#fee2e2;color:#b91c1c">ID missing</span>'
        )
        sow = ""
        if party.get("source_of_funds"):
            cov = round(o.get("sow_coverage_pct", 0) * 100)
            st = o.get("sow_status") or "not started"
            sow = (
                f"<span class='muted'> · SoW {esc(STATUS_LABEL.get(st, st))} "
                f"({cov}% coverage)</span>"
            )
        scope_lbl = "" if o.get("in_scope") else "<span class='muted'> · out of scope</span>"
        reasons = (
            f"<div class='d'>{esc('; '.join(o.get('reasons', [])))}</div>"
            if o.get("reasons")
            else ""
        )
        rows.append(
            f"""<div class="item">
              <span class="dot {dot}" style="margin-top:5px;background:{color}"></span>
              <div class="txt">
                <b>{esc(subj.get("name"))}</b>
                <span class="muted">· {esc(ROLE_LABEL.get(party.get("role"), party.get("role")))}</span>
                {f'<span class="muted">· {party.get("pct")}%</span>' if party.get("pct") else ""}
                {sow}{scope_lbl}
                <div class="docpills">{"".join(flags)}</div>
                {reasons}
              </div></div>"""
        )
    return (
        f'<section class="panel" data-panel="related_parties"><h2>Key individuals — CDD + SoW ({len(in_scope)} in scope) {badge}</h2>'
        f"<div class='body'><p class='muted' style='margin:0 0 8px'>{esc(rp.get('summary', ''))}</p>"
        f"{''.join(rows)}</div></section>"
    )


SRC_LABEL = {
    "ofac_sdn": "OFAC SDN",
    "ofac_consolidated": "OFAC Consolidated",
    "un": "UN",
    "eu": "EU",
    "uk_hmt": "UK HMT",
    "pep": "PEP register",
}
STATUS_LABEL_SCR = {
    "pending": ("PENDING", "#fef3c7", "#92400e"),
    "true_positive": ("TRUE POSITIVE", "#fee2e2", "#b91c1c"),
    "false_positive": ("false positive", "#eef2f7", "#546b8b"),
}


def render_screening(scr: dict | None) -> str:
    """Render the sanctions/PEP/watchlist screening panel, if present."""
    if not scr:
        return ""
    alerts = scr.get("alerts", [])
    open_alerts = [a for a in alerts if a.get("status") != "false_positive"]
    badge = (
        '<span class="statuspill" style="background:#fef3c7;color:#92400e">ENHANCED REVIEW</span>'
        if open_alerts
        else '<span class="statuspill" style="background:#ecfdf5;color:#059669">NO OPEN HITS</span>'
    )
    rows = []
    for a in alerts:
        m = a.get("match", {})
        entry = m.get("entry", {})
        label, bg, fg = STATUS_LABEL_SCR.get(
            a.get("status", "pending"), STATUS_LABEL_SCR["pending"]
        )
        score = round(m.get("score", 0) * 100)
        src = SRC_LABEL.get(entry.get("source"), entry.get("source"))
        feats = " · ".join(m.get("features", []))
        rows.append(
            f"""<div class="item">
              <span class="sev" style="background:{bg};color:{fg}">{label}</span>
              <div class="txt">
                <b>{esc(entry.get("name"))}</b>
                <span class="tag">{esc(src)}</span>
                <span class="muted">· match {score}% · screened: {esc(a.get("subject_id"))}</span>
                <div class="d">{esc(feats)}{(" · programs: " + esc(", ".join(entry.get("programs", [])))) if entry.get("programs") else ""}</div>
              </div></div>"""
        )
    body = (
        f"<p class='muted' style='margin:0 0 8px'>Screened against snapshot "
        f"<span class='mono'>{esc(scr.get('lists_version'))}</span> · sources: "
        f"{esc(', '.join(SRC_LABEL.get(s, s) for s in scr.get('sources', [])))}</p>"
        + ("".join(rows) if rows else "<div class='empty'>✓ No watchlist hits.</div>")
    )
    return (
        f'<section class="panel" data-panel="screening"><h2>Sanctions / PEP / watchlist screening '
        f'({len(open_alerts)} open) {badge}</h2><div class="body">{body}</div></section>'
    )


_BAND_COLOR = {
    "low": ("#ecfdf5", "#059669"),
    "medium": ("#fef3c7", "#92400e"),
    "high": ("#ffedd5", "#c2410c"),
    "prohibited": ("#fee2e2", "#b91c1c"),
}
_TIER_LABEL = {"sdd": "SDD (simplified)", "cdd": "CDD (standard)", "edd": "EDD (enhanced)"}


def render_scorecard(sc: dict | None) -> str:
    """Render the risk-based scorecard + CDD tier panel, if present."""
    if not sc:
        return ""
    band = sc.get("band", "low")
    tier = sc.get("tier", "cdd")
    bg, fg = _BAND_COLOR.get(band, _BAND_COLOR["low"])
    pct = round(sc.get("score", 0) * 100)
    factors = "".join(
        f"<li><span class='mono'>{esc(f.get('name'))}</span> · weight {f.get('weight')} · "
        f"score {round(f.get('score', 0) * 100)}%"
        f"{(' (' + esc(f.get('detail')) + ')') if f.get('detail') else ''}</li>"
        for f in sc.get("factors", [])
    )
    hard = sc.get("hard_signals", [])
    hard_html = (
        f"<p class='muted' style='margin-top:8px'>Hard signals: {esc(', '.join(hard))}</p>"
        if hard
        else ""
    )
    return (
        f'<section class="panel" data-panel="scorecard"><h2>Risk scorecard: CDD tier '
        f'<span class="statuspill" style="background:{bg};color:{fg}">'
        f"{esc(band.upper())} · {esc(_TIER_LABEL.get(tier, tier))}</span></h2>"
        f"<div class='body'>"
        f"<div class='cov'><div class='lbl'><span>Weighted risk score</span><span>{pct}%</span></div>"
        f"<div class='bar'><span style='width:{pct}%'></span></div></div>"
        f"<ul class='sources' style='list-style:none;padding:0;margin:10px 0 0;font-size:13px;color:var(--ink-700)'>{factors}</ul>"
        f"{hard_html}</div></section>"
    )


_FUNDS_KIND_LABEL = {
    "salary": "Salary",
    "business_income": "Business income",
    "asset_sale": "Asset sale",
    "investment_income": "Investment income",
    "loan": "Loan",
    "gift": "Gift",
    "inheritance": "Inheritance",
    "other": "Other",
}


def render_source_of_funds(sof: dict | None) -> str:
    """Render the Source-of-Funds reconciliation panel (distinct from SoW), if present."""
    if not sof:
        return ""
    gaps = sof.get("gaps", [])
    badge = (
        '<span class="statuspill" style="background:#fef3c7;color:#92400e">ENHANCED REVIEW</span>'
        if gaps
        else '<span class="statuspill" style="background:#ecfdf5;color:#059669">RECONCILED</span>'
    )
    cov = round(sof.get("coverage_pct", 0) * 100)
    line_rows = []
    for ln in sof.get("lines", []):
        lcov = round(ln.get("coverage_pct", 0) * 100)
        dot = "ok" if ln.get("corroborated") else "warn"
        color = "var(--ok)" if ln.get("corroborated") else "var(--warn)"
        line_rows.append(
            f"""<div class="group"><div class="gh">
              <span class="dot {dot}" style="background:{color}"></span>
              <span class="name">{esc(_FUNDS_KIND_LABEL.get(ln.get("kind"), ln.get("kind")))}</span>
              <span class="vals">expected <b>{esc(ln.get("declared_band") or "-")}</b> · evidenced <b>{esc(ln.get("evidenced_band") or "-")}</b>
                <div class="minicov" style="margin-left:auto"><span style="width:{lcov}%"></span></div>
              </span></div></div>"""
        )
    gap_html = (
        "".join(
            f"""<div class="item">{sev_pill(g["severity"])}
              <div class="txt">{esc(g["summary"])} <span class="gapk">[{esc(g["kind"])}]</span>
                <div class="d">{esc(g.get("detail", ""))}</div></div></div>"""
            for g in gaps
        )
        if gaps
        else "<div class='empty'>✓ Inflows reconcile with the declared funding profile.</div>"
    )
    activity = (
        f"<p class='muted' style='margin:0 0 8px'>Expected activity: {esc(sof['expected_activity'])}</p>"
        if sof.get("expected_activity")
        else ""
    )
    notes = "".join(f"<li>• {esc(n)}</li>" for n in sof.get("consistency_notes", []))
    notes_html = f'<ul class="notes">{notes}</ul>' if notes else ""
    return (
        f'<section class="panel" data-panel="source_of_funds"><h2>Source of Funds: declared inflows vs evidenced '
        f"({cov}% coverage) {badge}</h2><div class='body'>"
        f"{activity}"
        f"<div class='recon'><div class='fig'>Declared inflows<b>{esc(sof.get('declared_inflow_band') or '-')}</b></div>"
        f"<div class='fig'>Evidenced<b>{esc(sof.get('evidenced_inflow_band') or '-')}</b></div>"
        f"<div class='cov'><div class='lbl'><span>Coverage</span><span>{cov}%</span></div>"
        f"<div class='bar'><span style='width:{cov}%'></span></div></div></div>"
        f"<div style='margin-top:12px'>{''.join(line_rows)}</div>"
        f"<div style='margin-top:6px'>{gap_html}</div>"
        f"{notes_html}"
        f"</div></section>"
    )


_REVIEW_STATUS = {
    "current": ("CURRENT", "#ecfdf5", "#059669"),
    "due_soon": ("DUE SOON", "#fffbeb", "#d97706"),
    "overdue": ("OVERDUE", "#fee2e2", "#b91c1c"),
    "triggered": ("RE-REVIEW TRIGGERED", "#ffedd5", "#c2410c"),
}
_TRIGGER_LABEL = {
    "periodic_due": "Periodic review due",
    "sanctions_hit": "Sanctions / watchlist hit",
    "pep_status": "PEP exposure",
    "adverse_media": "Adverse media",
    "ownership_change": "Ownership change",
    "unusual_activity": "Unusual activity",
    "material_change": "Material change",
    "document_expiry": "Document expiry",
}


def render_monitoring(mon: dict | None) -> str:
    """Render the ongoing-monitoring / periodic-review panel, if present."""
    if not mon:
        return ""
    status = mon.get("review_status", "current")
    label, bg, fg = _REVIEW_STATUS.get(status, _REVIEW_STATUS["current"])
    days = mon.get("days_until_due", 0)
    when = f"overdue by {abs(days)} day(s)" if days < 0 else f"due in {days} day(s)"
    triggers = mon.get("triggers", [])
    trig_html = (
        "".join(
            f"""<div class="item">{sev_pill(t.get("severity", "low"))}
              <div class="txt">{esc(t.get("summary", ""))}
                <span class="gapk">[{esc(_TRIGGER_LABEL.get(t.get("kind"), t.get("kind")))}]</span>
                <div class="d">{esc(t.get("detail", ""))}</div></div></div>"""
            for t in triggers
        )
        if triggers
        else "<div class='empty'>✓ No re-review triggers (relationship within risk-based cadence).</div>"
    )
    return (
        f'<section class="panel" data-panel="monitoring"><h2>Ongoing monitoring: periodic review '
        f'<span class="statuspill" style="background:{bg};color:{fg}">{label}</span></h2>'
        f"<div class='body'>"
        f"<div class='recon'>"
        f"<div class='fig'>CDD tier<b>{esc((mon.get('tier') or 'cdd').upper())}</b></div>"
        f"<div class='fig'>Cadence<b>{esc(mon.get('cadence_months'))} months</b></div>"
        f"<div class='fig'>Last reviewed<b>{esc(mon.get('last_reviewed') or '-')}</b></div>"
        f"<div class='fig'>Next review due<b>{esc(mon.get('next_review_due') or '-')} "
        f"<span class='muted' style='font-size:12px;font-weight:400'>({esc(when)})</span></b></div>"
        f"</div>"
        f"<div style='margin-top:10px'>{trig_html}</div>"
        f"</div></section>"
    )


def page(title: str, body: str) -> str:
    return f"<!doctype html><html><head><meta charset='utf-8'><title>{esc(title)}</title><style>{CSS}</style></head><body><div class='wrap'>{body}</div></body></html>"


def render_iteration(data: dict, it: dict, evidence_by_id: dict) -> str:
    view = it["audit_view"]
    subj = data["subject"]
    status = it["status_after"]
    bg, fg = (
        SEV_COLOR["medium"]
        if status == "rfi_pending"
        else ("#dbeafe", "#1e40af")
        if status == "ready_for_review"
        else SEV_COLOR["low"]
    )
    header = (
        f"<h1>Source of Wealth: {esc(subj['name'])}</h1>"
        f"<p class='sub'>Long-running case <b class='mono'>{esc(data['case_id'])}</b> · {esc(subj['jurisdiction'])} · "
        f"round {esc(it['no'])} as of <b>{esc(it['as_of'])}</b></p>"
        + steps_bar(it["no"], data.get("final_status", ""))
        + (
            '<div class="review">HUMAN REVIEW REQUIRED: maker-checker gate (P-06). '
            "The agent proposes; a qualified checker disposes.</div>"
            if status != "approved"
            else ""
        )
    )
    statuspanel = (
        f'<section class="panel" data-panel="status"><h2>Case status'
        f'<span class="statuspill" style="background:{bg};color:{fg}">{esc(STATUS_LABEL.get(status, status))}</span>'
        f"</h2><div class='body'><span class='muted'>Added this round: </span>"
        + ", ".join(f"<span class='mono'>{esc(e['id'])}</span>" for e in it["added_evidence"])
        + "</div></section>"
    )
    body = (
        header
        + statuspanel
        + render_recon(view["reconciliation"])
        + render_groups(view, evidence_by_id)
        + render_gaps(view)
        + render_rfis(view)
        + render_source_of_funds(view.get("source_of_funds"))
        + render_monitoring(view.get("monitoring"))
        + render_scorecard(view.get("scorecard"))
        + render_screening(view.get("screening"))
        + render_related_parties(view.get("related_parties"))
        + "<p class='foot'>Audit-first SoW view · synthetic fictional data · deterministic reconciliation/gap engine</p>"
    )
    return page(f"SoW round {it['no']}", body)


def render_timeline(data: dict) -> str:
    rows = []
    for it in data["iterations"]:
        v = it["audit_view"]
        recon = v["reconciliation"]
        cov = round(recon.get("coverage_pct", 0) * 100)
        rows.append(
            f"<tr><td>Round {esc(it['no'])}</td><td>{esc(it['as_of'])}</td>"
            f"<td>{esc(STATUS_LABEL.get(it['status_after'], it['status_after']))}</td>"
            f"<td class='num'>{esc(recon.get('evidenced_total_band'))} / {esc(recon.get('declared_total_band'))}</td>"
            f"<td class='num'>{cov}%</td><td class='num'>{len(v.get('gaps', []))}</td>"
            f"<td class='num'>{len(v.get('rfis', []))}</td></tr>"
        )
    snap = data.get("snapshot")
    snaprow = (
        f"<tr><td>Approved</td><td>{esc(snap['sealed_at'][:10])}</td><td>Sealed snapshot v{esc(snap['version'])} by {esc(snap['approved_by'])}</td><td>-</td><td>-</td><td class='num'>0</td><td class='num'>0</td></tr>"
        if snap
        else ""
    )
    body = (
        f"<h1>Case timeline: {esc(data['subject']['name'])}</h1>"
        f"<p class='sub'>How the case moved over weeks as the client closed gaps. Final status: <b>{esc(data.get('final_status'))}</b>.</p>"
        '<section class="panel"><h2>Iterations</h2><div class="body">'
        "<table class='tl'><tr><th>Round</th><th>As of</th><th>Status</th><th>Evidenced / Declared</th><th>Coverage</th><th>Gaps</th><th>RFIs</th></tr>"
        + "".join(rows)
        + snaprow
        + "</table></div></section>"
        "<p class='foot'>Audit-first SoW view · synthetic fictional data</p>"
    )
    return page("SoW timeline", body)


def main(json_path: str, out_dir: str) -> None:
    data = json.loads(Path(json_path).read_text())
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Evidence index across all rounds (so earlier-round items can still render).
    evidence_by_id: dict[str, dict] = {}
    for it in data["iterations"]:
        for e in it["added_evidence"]:
            evidence_by_id[e["id"]] = {**e, "round": it["no"]}

    written = []
    for it in data["iterations"]:
        p = out / f"sow-iter-{it['no']}.html"
        p.write_text(render_iteration(data, it, evidence_by_id))
        written.append(p)
    tl = out / "sow-timeline.html"
    tl.write_text(render_timeline(data))
    written.append(tl)
    for p in written:
        print("wrote", p)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
