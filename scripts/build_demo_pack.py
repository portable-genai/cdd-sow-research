"""Build the live-demo evidence packs from genuinely public records.

The live walkthrough uploads real documents about real subjects, so this script
prepares two small KYC-style packs, each rendered to a text-layer PDF from public,
redistribution-safe sources (nothing third-party is committed to the repo; the packs
land under the gitignored ``scripts/out/``):

* **clean** (the true negative): a business and financial profile of a large listed
  company, built from its SEC EDGAR submissions and XBRL company facts. EDGAR filings
  are US government works in the public domain, fetched from ``data.sec.gov`` with the
  source URLs cited inside the document.
* **flagged** (the true positive): the designation record of a real OFAC-designated
  entity, rendered from the synced sanctions snapshot (``scripts/sync_sanctions.py``)
  with the US Treasury source named inside the document.

Every generated page carries a PROVENANCE line naming its public source and the fetch
date, so a reviewer reading the uploaded evidence can see exactly where each fact came
from. A ``manifest.json`` describes both packs for the portal walkthrough to consume.

    PYTHONPATH=src python scripts/build_demo_pack.py
    PYTHONPATH=src python scripts/build_demo_pack.py --out scripts/out/live-demo

SEC fair-access policy asks automated clients to identify themselves: set
``SEC_EDGAR_CONTACT`` to your contact email before a real run.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# The sibling cache helper lives next to this script; make it importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fetch_cache import cached_fetch  # noqa: E402

from cdd_sow_research.envread import setting_or_default  # noqa: E402

# --------------------------------------------------------------------------- #
# Defaults: one large listed issuer (public-domain EDGAR filings) and one real
# OFAC-designated entity present in any synced snapshot.
# --------------------------------------------------------------------------- #
CLEAN_NAME = "Apple Inc."
CLEAN_CIK = "0000320193"
FLAGGED_NAME = "BANCO NACIONAL DE CUBA"
DEFAULT_SNAPSHOT = "scripts/out/sanctions/current.json"

EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
EDGAR_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
OFAC_SEARCH = "https://sanctionssearch.ofac.treas.gov/"
OFAC_SDN_LIST = "https://www.treasury.gov/ofac/downloads/sdn.csv"


# --------------------------------------------------------------------------- #
# Minimal text-layer PDF writer (stdlib only): Helvetica, one column, real pages.
# The point is honest machine-readable text the extraction adapter can cite by page,
# not typography.
# --------------------------------------------------------------------------- #
_PAGE_W, _PAGE_H = 612, 792  # US Letter, points
_MARGIN, _LEADING, _SIZE = 54, 14, 10
_LINES_PER_PAGE = int((_PAGE_H - 2 * _MARGIN) / _LEADING)
_WRAP = 92


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _wrap_line(line: str) -> list[str]:
    if len(line) <= _WRAP:
        return [line]
    out: list[str] = []
    words = line.split(" ")
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > _WRAP:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        out.append(cur)
    return out


def write_pdf(path: Path, lines: list[str]) -> int:
    """Write ``lines`` as a simple multi-page Helvetica PDF; return the page count."""
    wrapped: list[str] = []
    for line in lines:
        wrapped.extend(_wrap_line(line))
    pages = [wrapped[i : i + _LINES_PER_PAGE] for i in range(0, len(wrapped), _LINES_PER_PAGE)] or [
        [""]
    ]

    objects: list[bytes] = []  # 1-indexed PDF objects, in id order

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    content_ids: list[int] = []
    for page in pages:
        parts = [f"BT /F1 {_SIZE} Tf {_MARGIN} {_PAGE_H - _MARGIN} Td {_LEADING} TL"]
        for line in page:
            parts.append(f"({_escape(line)}) Tj T*")
        parts.append("ET")
        stream = "\n".join(parts).encode("latin-1", "replace")
        content_ids.append(
            add(
                b"<< /Length "
                + str(len(stream)).encode()
                + b" >>\nstream\n"
                + stream
                + b"\nendstream"
            )
        )
    pages_id = len(objects) + len(pages) + 1  # the /Pages node is written after the pages
    for content_id in content_ids:
        page_ids.append(
            add(
                (
                    f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {_PAGE_W} {_PAGE_H}] "
                    f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>"
                ).encode()
            )
        )
    kids = " ".join(f"{i} 0 R" for i in page_ids)
    real_pages_id = add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode())
    assert real_pages_id == pages_id, "pages node id drifted"
    catalog_id = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    path.write_bytes(bytes(out))
    return len(pages)


# --------------------------------------------------------------------------- #
# Public-record fetch + rendering
# --------------------------------------------------------------------------- #
def _http_get_text(url: str) -> str:
    import httpx  # core dep; imported here so --help works without network

    contact = setting_or_default("SEC_EDGAR_CONTACT", "unset")
    headers = {"User-Agent": f"cdd-sow-research-demo/1.0 (contact: {contact})"}
    resp = httpx.get(url, headers=headers, timeout=60.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def _fetch_json(url: str, *, refresh: bool = False) -> dict:
    """Fetch an EDGAR JSON document, reusing a fresh on-disk copy when one exists."""
    return json.loads(cached_fetch(url, _http_get_text, refresh=refresh))


def _fact_series(facts: dict, tag: str) -> list[tuple[str, float]]:
    """Annual (10-K) USD values for an us-gaap tag, most recent first."""
    node = facts.get("facts", {}).get("us-gaap", {}).get(tag, {})
    rows = node.get("units", {}).get("USD", [])
    annual = [
        r for r in rows if r.get("form") == "10-K" and r.get("fp") == "FY" and "frame" not in r
    ]
    seen: dict[str, float] = {}
    for r in annual:  # later filings restate earlier years; keep the latest value per year
        end = r.get("end", "")
        if end:
            seen[end] = float(r.get("val", 0.0))
    return sorted(seen.items(), reverse=True)[:3]


def _usd(v: float) -> str:
    return f"USD {v / 1e9:,.1f} bn" if abs(v) >= 1e9 else f"USD {v / 1e6:,.1f} m"


def build_clean_pack(
    out_dir: Path, name: str, cik: str, today: str, *, refresh: bool = False
) -> dict:
    cik = cik.zfill(10)
    submissions_url = EDGAR_SUBMISSIONS.format(cik=cik)
    facts_url = EDGAR_FACTS.format(cik=cik)
    sub = _fetch_json(submissions_url, refresh=refresh)
    facts = _fetch_json(facts_url, refresh=refresh)

    addresses = sub.get("addresses", {}).get("business", {})
    tickers = ", ".join(sub.get("tickers", [])[:3]) or "n/a"
    exchanges = ", ".join(x for x in sub.get("exchanges", []) if x) or "n/a"
    recent = sub.get("filings", {}).get("recent", {})
    latest_10k = ""
    for form, date in zip(recent.get("form", []), recent.get("filingDate", []), strict=False):
        if form == "10-K":
            latest_10k = date
            break

    lines = [
        f"BUSINESS PROFILE AND FINANCIAL SUMMARY: {sub.get('name', name)}",
        "",
        f"PROVENANCE: compiled {today} from SEC EDGAR public filings (US government",
        "works, public domain). Sources:",
        f"  {submissions_url}",
        f"  {facts_url}",
        "",
        "REGISTRY IDENTIFIERS",
        f"  SEC CIK: {int(cik)}",
        f"  Entity type: {sub.get('entityType', 'operating company')}",
        f"  SIC: {sub.get('sic', 'n/a')} {sub.get('sicDescription', '')}".rstrip(),
        f"  State of incorporation: {sub.get('stateOfIncorporation', 'n/a')}",
        f"  Fiscal year end: {sub.get('fiscalYearEnd', 'n/a')}",
        f"  Tickers: {tickers} ({exchanges})",
        f"  Latest 10-K filed: {latest_10k or 'n/a'}",
        "",
        "PRINCIPAL BUSINESS ADDRESS",
        f"  {addresses.get('street1', '')}",
        f"  {addresses.get('city', '')}, {addresses.get('stateOrCountry', '')} "
        f"{addresses.get('zipCode', '')}",
        "",
        "AUDITED ANNUAL FINANCIALS (from XBRL company facts, form 10-K)",
    ]
    for label, tag in (
        ("Total revenue", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("Net income", "NetIncomeLoss"),
        ("Total assets", "Assets"),
        ("Stockholders' equity", "StockholdersEquity"),
    ):
        series = _fact_series(facts, tag) or _fact_series(facts, "Revenues")
        if not series:
            continue
        vals = "; ".join(f"FY ending {end}: {_usd(v)}" for end, v in series)
        lines.append(f"  {label}: {vals}")
    lines += [
        "",
        "SOURCE OF WEALTH INDICATORS",
        "  The company's wealth derives from its operating business as evidenced by the",
        "  audited revenues and retained earnings in its SEC filings above. Ownership is",
        "  dispersed public-market shareholding via the listed exchanges named above.",
        "",
        f"Document prepared for a CDD demonstration on {today}. All facts above are",
        "quoted from the cited public filings; verify against the source URLs.",
    ]
    path = out_dir / "clean-subject-profile.pdf"
    pages = write_pdf(path, lines)
    return {
        "subject_name": sub.get("name", name),
        "subject_type": "entity",
        "jurisdiction": "US",
        "file": path.name,
        "pages": pages,
        "sources": [submissions_url, facts_url],
    }


def build_flagged_pack(out_dir: Path, name: str, snapshot_path: Path, today: str) -> dict:
    snapshot = json.loads(snapshot_path.read_text())
    if "fixture" in snapshot.get("version", ""):
        raise SystemExit(
            f"{snapshot_path} is the FICTIONAL bundled fixture; run scripts/sync_sanctions.py "
            f"--out {snapshot_path} first so the flagged pack renders a real designation"
        )
    wanted = name.upper()
    entries = [
        e
        for e in snapshot.get("entries", [])
        if e.get("name", "").upper() == wanted
        or any(a.upper() == wanted for a in e.get("aliases", []))
    ]
    if not entries:
        raise SystemExit(f"no entry named {name!r} in {snapshot_path}")
    entry = entries[0]

    lines = [
        f"WATCHLIST DESIGNATION RECORD: {entry['name']}",
        "",
        f"PROVENANCE: rendered {today} from the synced OFAC/UN sanctions snapshot",
        f"version {snapshot['version']} (content sha256 "
        f"{snapshot.get('sync_meta', {}).get('content_sha256', 'n/a')[:16]}...).",
        "Published sources:",
        f"  {OFAC_SDN_LIST}",
        f"  {OFAC_SEARCH} (interactive lookup)",
        "",
        "DESIGNATION",
        f"  Listed name: {entry['name']}",
        f"  Source list: {entry['source']}",
        f"  Entry uid: {entry['uid']}",
        f"  Entity type: {entry.get('entity_type', 'entity')}",
        f"  Programs: {', '.join(entry.get('programs', [])) or 'n/a'}",
        f"  Countries: {', '.join(entry.get('countries', [])) or 'n/a'}",
    ]
    aliases = entry.get("aliases", [])
    if aliases:
        lines.append("  Known aliases:")
        lines += [f"    - {a}" for a in aliases[:12]]
    if entry.get("remark"):
        lines += ["", "REMARKS", f"  {entry['remark']}"]
    lines += [
        "",
        "CDD RELEVANCE",
        "  The subject appears on the sanctions list named above. Any customer",
        "  relationship requires enhanced due diligence and escalation; screening",
        "  alerts must be dispositioned by a qualified checker (maker-checker).",
        "",
        f"Document prepared for a CDD demonstration on {today}. The designation is",
        "quoted from the cited published list; verify against the source URLs.",
    ]
    path = out_dir / "flagged-subject-designation.pdf"
    pages = write_pdf(path, lines)
    return {
        "subject_name": entry["name"].title(),
        "subject_type": "entity" if entry.get("entity_type") == "entity" else "individual",
        "jurisdiction": (entry.get("countries") or ["n/a"])[0],
        "file": path.name,
        "pages": pages,
        "sources": [OFAC_SDN_LIST, OFAC_SEARCH],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the live-demo evidence packs")
    ap.add_argument("--out", default="scripts/out/live-demo", help="output directory")
    ap.add_argument("--clean-name", default=CLEAN_NAME)
    ap.add_argument("--clean-cik", default=CLEAN_CIK, help="SEC CIK of the clean subject")
    ap.add_argument("--flagged-name", default=FLAGGED_NAME)
    ap.add_argument("--snapshot", default=DEFAULT_SNAPSHOT, help="synced sanctions snapshot")
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="bypass the on-disk EDGAR cache and re-download",
    )
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).date().isoformat()

    clean = build_clean_pack(out_dir, args.clean_name, args.clean_cik, today, refresh=args.refresh)
    flagged = build_flagged_pack(out_dir, args.flagged_name, Path(args.snapshot), today)

    manifest = {
        "built_at": today,
        "note": (
            "Live-demo evidence packs rendered from public records; see each PDF's "
            "PROVENANCE line. The flagged subject is a real listed designation, used "
            "to demonstrate a true-positive screening alert."
        ),
        "clean": clean,
        "flagged": flagged,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote {out_dir}/manifest.json")
    for pack in (clean, flagged):
        print(f"  {pack['subject_name']}: {pack['file']} ({pack['pages']} pages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
