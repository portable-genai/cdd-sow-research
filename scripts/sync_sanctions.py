"""Sync the sanctions/PEP watchlist snapshot from the published sources.

Fetches OFAC SLS (SDN + Consolidated) plus UN/EU/UK lists, parses them into the unified
``WatchlistEntry`` schema (pure parsers in
``cdd_sow_research.adapters.local.sanctions_sync``), merges, versions by UTC date, and writes
the JSON snapshot the screening providers read. Optionally uploads to the regional CMEK
bucket the ``gcp`` provider reads. This is the script the scheduled Cloud Run job runs
(see ``infra/terraform/sanctions_sync.tf``); it can also be run by hand to refresh the
local fixture.

    PYTHONPATH=src python scripts/sync_sanctions.py --out src/cdd_sow_research/adapters/local/data/sanctions_snapshot.json
    PYTHONPATH=src python scripts/sync_sanctions.py --gcs cdd-sow-research-sanctions/snapshot/current.json

Network egress to the publishers is required for a real run; offline, the bundled fixture
already lets screening work. Verify each source's live schema before production use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# The sibling cache helper lives next to this script; make it importable when the script is
# run directly (python scripts/sync_sanctions.py) regardless of the current directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fetch_cache import cached_fetch  # noqa: E402

from cdd_sow_research.adapters.local import sanctions_sync as sx  # noqa: E402
from cdd_sow_research.domain.models import ListSource, WatchlistEntry  # noqa: E402

# Published download endpoints (verify against the live publisher before production).
OFAC_SDN_CSV = "https://www.treasury.gov/ofac/downloads/sdn.csv"
OFAC_SDN_ALT = "https://www.treasury.gov/ofac/downloads/alt.csv"
OFAC_CONS_CSV = "https://www.treasury.gov/ofac/downloads/consolidated/cons_prim.csv"
OFAC_CONS_ALT = "https://www.treasury.gov/ofac/downloads/consolidated/cons_alt.csv"
UN_CONSOLIDATED_XML = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
# EU / UK distribute via portals that often need a token/redirect; wire your URL + columns.
EU_URL = ""
UK_URL = ""


# Integrity floors: a screening list that silently SHRINKS means missed hits (a
# regulatory event), so the sync fails loudly instead of publishing a suspect snapshot.
MIN_ENTRIES_PER_SOURCE = 100  # any real OFAC/UN list is thousands of rows
MAX_SHRINK_FRACTION = 0.10  # refuse a snapshot >10% smaller than the previous one


def _http_get(url: str, timeout: float = 300.0) -> str:
    import httpx  # core dep; imported here so --help works without network

    if not url.startswith("https://"):
        raise SystemExit(f"refusing non-https watchlist source: {url!r}")

    def _https_only(request) -> None:  # redirects must never downgrade the scheme
        if request.url.scheme != "https":
            raise SystemExit(f"refusing redirect to non-https URL: {request.url}")

    print(f"fetching {url} ...", file=sys.stderr)
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        event_hooks={"request": [_https_only]},
        # Identify the sync honestly; some publishers throttle the bare default client UA.
        headers={"User-Agent": "cdd-sow-research-sanctions-sync/1.0"},
    ) as client:
        resp = client.get(url)
    resp.raise_for_status()
    print(f"  {len(resp.content)} bytes", file=sys.stderr)
    return resp.text


def _fetch(url: str, timeout: float = 300.0, *, refresh: bool = False) -> str:
    """Fetch a watchlist source, reusing a fresh on-disk copy when one exists."""
    return cached_fetch(url, lambda u: _http_get(u, timeout), refresh=refresh)


def collect(*, refresh: bool = False) -> list[WatchlistEntry]:
    groups: dict[str, list[WatchlistEntry]] = {
        "ofac_sdn": sx.parse_ofac_sdn_csv(
            _fetch(OFAC_SDN_CSV, refresh=refresh), _fetch(OFAC_SDN_ALT, refresh=refresh)
        ),
        "ofac_consolidated": sx.parse_ofac_sdn_csv(
            _fetch(OFAC_CONS_CSV, refresh=refresh),
            _fetch(OFAC_CONS_ALT, refresh=refresh),
            source=ListSource.OFAC_CONSOLIDATED,
        ),
        # The UN consolidated XML is tens of MB from a slow origin; 60s read-times-out.
        "un": sx.parse_un_consolidated_xml(
            _fetch(UN_CONSOLIDATED_XML, timeout=300.0, refresh=refresh)
        ),
    }
    if EU_URL:
        groups["eu"] = sx.parse_delimited(
            _fetch(EU_URL, refresh=refresh), ListSource.EU, name_col="NameAlias"
        )
    if UK_URL:
        groups["uk_hmt"] = sx.parse_delimited(
            _fetch(UK_URL, refresh=refresh), ListSource.UK_HMT, name_col="Name 6"
        )
    # A truncated download or a schema drift parses to few/no rows; never publish that.
    for name, group in groups.items():
        if len(group) < MIN_ENTRIES_PER_SOURCE:
            raise SystemExit(
                f"source {name!r} yielded only {len(group)} entries "
                f"(< {MIN_ENTRIES_PER_SOURCE}): truncated download or schema drift; aborting"
            )
    return [e for g in groups.values() for e in g]


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync the sanctions watchlist snapshot")
    ap.add_argument("--out", help="write snapshot JSON to this path")
    ap.add_argument(
        "--gcs", help="upload to gs bucket/object, e.g. my-bucket/snapshot/current.json"
    )
    ap.add_argument("--version", default=datetime.now(UTC).date().isoformat())
    ap.add_argument(
        "--allow-shrink",
        action="store_true",
        help="accept a snapshot >10%% smaller than the previous one (verify publishers first)",
    )
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="bypass the on-disk source cache and re-download every list",
    )
    args = ap.parse_args()

    if not args.out and not args.gcs:
        ap.error("specify --out and/or --gcs")

    entries = collect(refresh=args.refresh)
    snapshot = sx.build_snapshot(entries, version=args.version)
    # Provenance for every published snapshot: when/where it came from and a content
    # hash, so screening results can name the exact list bytes they matched against.
    canonical_entries = json.dumps(snapshot["entries"], sort_keys=True, separators=(",", ":"))
    snapshot["sync_meta"] = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "sources": [
            u for u in (OFAC_SDN_CSV, OFAC_CONS_CSV, UN_CONSOLIDATED_XML, EU_URL, UK_URL) if u
        ],
        "content_sha256": hashlib.sha256(canonical_entries.encode()).hexdigest(),
    }
    print(f"Collected {len(snapshot['entries'])} entries; version {args.version}")

    if args.out:
        path = Path(args.out)
        prev = json.loads(path.read_text()) if path.exists() else None
        added, removed = sx.diff_counts(prev, snapshot)
        # ENFORCED shrink guard (not just printed): a materially smaller list means
        # missed screening hits. Override consciously with --allow-shrink.
        prev_n = len(prev.get("entries", ())) if prev else 0
        new_n = len(snapshot["entries"])
        if prev_n and new_n < prev_n * (1 - MAX_SHRINK_FRACTION) and not args.allow_shrink:
            raise SystemExit(
                f"snapshot shrank {prev_n} -> {new_n} entries "
                f"(> {int(MAX_SHRINK_FRACTION * 100)}%); refusing to overwrite. "
                "Re-run with --allow-shrink after verifying the publishers."
            )
        path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        print(f"Wrote {path}  (+{added} / -{removed} vs previous)")

    if args.gcs:
        from google.cloud import storage  # lazy

        bucket_name, _, obj = args.gcs.partition("/")
        storage.Client().bucket(bucket_name).blob(obj).upload_from_string(
            json.dumps(snapshot), content_type="application/json"
        )
        print(f"Uploaded to gs://{args.gcs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
