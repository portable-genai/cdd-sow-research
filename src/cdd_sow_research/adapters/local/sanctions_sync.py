"""Build a watchlist snapshot from the published sanctions sources.

Pure parsers (text -> ``WatchlistEntry`` rows) plus a small fetch/merge orchestrator used
by ``scripts/sync_sanctions.py`` to refresh the screening snapshot. The parsers are pure
and unit-tested; the fetch uses ``httpx`` (a core dep) and is the only network-touching
part. The merged snapshot is written as the same JSON the providers read, with a
``version`` (UTC date) so every screening alert is reproducible against a point-in-time
list.

Source coverage:
* **OFAC** — SDN + Consolidated legacy CSV (``sdn.csv`` + ``alt.csv``; same columns for the
  consolidated ``cons_prim.csv`` + ``cons_alt.csv``). Robust, header-less, fixed columns.
* **UN** — the consolidated XML (``CONSOLIDATED_LIST`` → individuals + entities).
* **EU / UK** — a configurable delimited parser (``parse_delimited``); map the published
  columns when wiring real files. The bundled fixture covers all sources for offline use.

Real schemas evolve — verify against the live files before production use.
"""

from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable

from ...domain.models import ListSource, SubjectType, WatchlistEntry

_DOB_RE = re.compile(r"DOB\s+([0-9]{1,2}\s+\w+\s+[0-9]{4}|[0-9]{4}(?:-[0-9]{2}-[0-9]{2})?)")
_EMPTY = {"", "-0-", "-0- "}


def _clean(v: str) -> str:
    v = (v or "").strip()
    return "" if v in _EMPTY else v


def _dob_from_remark(remark: str) -> str | None:
    m = _DOB_RE.search(remark or "")
    return m.group(1) if m else None


def parse_ofac_sdn_csv(
    sdn_csv: str,
    alt_csv: str = "",
    *,
    source: ListSource = ListSource.OFAC_SDN,
) -> list[WatchlistEntry]:
    """Parse OFAC legacy ``sdn.csv`` (+ optional ``alt.csv`` aliases) into entries.

    ``sdn.csv`` columns (header-less): ent_num, name, sdn_type, program, title, call_sign,
    vess_type, tonnage, grt, vess_flag, vess_owner, remarks.
    ``alt.csv`` columns: ent_num, alt_num, alt_type, alt_name, alt_remarks.
    """
    aliases: dict[str, list[str]] = {}
    if alt_csv:
        for row in csv.reader(io.StringIO(alt_csv)):
            if len(row) >= 4:
                name = _clean(row[3])
                if name:
                    aliases.setdefault(row[0].strip(), []).append(name)

    out: list[WatchlistEntry] = []
    for row in csv.reader(io.StringIO(sdn_csv)):
        if len(row) < 12:
            continue
        ent_num = row[0].strip()
        name = _clean(row[1])
        if not name:
            continue
        sdn_type = _clean(row[2]).lower()
        program = _clean(row[3])
        remark = _clean(row[11])
        entity_type = SubjectType.INDIVIDUAL if sdn_type == "individual" else SubjectType.ENTITY
        out.append(
            WatchlistEntry(
                uid=f"OFAC-{ent_num}",
                source=source,
                name=name,
                entity_type=entity_type,
                aliases=tuple(aliases.get(ent_num, ())),
                dob=_dob_from_remark(remark),
                countries=(),
                programs=(program,) if program else (),
                remark=remark,
            )
        )
    return out


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse_un_consolidated_xml(xml_text: str) -> list[WatchlistEntry]:
    """Parse the UN ``CONSOLIDATED_LIST`` XML (individuals + entities)."""
    out: list[WatchlistEntry] = []
    root = ET.fromstring(xml_text)  # noqa: S314 - trusted, fetched from un.org
    for ind in root.findall(".//INDIVIDUALS/INDIVIDUAL"):
        parts = [
            _text(ind.find(f"{tag}"))
            for tag in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")
        ]
        name = " ".join(p for p in parts if p)
        if not name:
            continue
        aliases = tuple(
            _text(a.find("ALIAS_NAME"))
            for a in ind.findall("INDIVIDUAL_ALIAS")
            if _text(a.find("ALIAS_NAME"))
        )
        dob = _text(ind.find(".//INDIVIDUAL_DATE_OF_BIRTH/DATE")) or _text(
            ind.find(".//INDIVIDUAL_DATE_OF_BIRTH/YEAR")
        )
        country = _text(ind.find(".//NATIONALITY/VALUE"))
        out.append(
            WatchlistEntry(
                uid=f"UN-{_text(ind.find('DATAID')) or name}",
                source=ListSource.UN,
                name=name,
                entity_type=SubjectType.INDIVIDUAL,
                aliases=aliases,
                dob=dob or None,
                countries=(country,) if country else (),
                programs=("UNSC",),
                remark=_text(ind.find("COMMENTS1")),
            )
        )
    for ent in root.findall(".//ENTITIES/ENTITY"):
        name = _text(ent.find("FIRST_NAME"))
        if not name:
            continue
        aliases = tuple(
            _text(a.find("ALIAS_NAME"))
            for a in ent.findall("ENTITY_ALIAS")
            if _text(a.find("ALIAS_NAME"))
        )
        out.append(
            WatchlistEntry(
                uid=f"UN-{_text(ent.find('DATAID')) or name}",
                source=ListSource.UN,
                name=name,
                entity_type=SubjectType.ENTITY,
                aliases=aliases,
                programs=("UNSC",),
                remark=_text(ent.find("COMMENTS1")),
            )
        )
    return out


def parse_delimited(
    text: str,
    source: ListSource,
    *,
    name_col: str,
    uid_col: str | None = None,
    alias_col: str | None = None,
    dob_col: str | None = None,
    country_col: str | None = None,
    program_col: str | None = None,
    delimiter: str = ",",
) -> list[WatchlistEntry]:
    """Generic header-row delimited parser for EU/UK-style published list files."""
    out: list[WatchlistEntry] = []
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    for i, row in enumerate(reader):
        name = _clean(row.get(name_col, ""))
        if not name:
            continue
        aliases: tuple[str, ...] = ()
        if alias_col and _clean(row.get(alias_col, "")):
            aliases = tuple(a.strip() for a in row[alias_col].split(";") if a.strip())
        out.append(
            WatchlistEntry(
                uid=f"{source.value.upper()}-{_clean(row.get(uid_col, '')) if uid_col else i}",
                source=source,
                name=name,
                aliases=aliases,
                dob=_clean(row.get(dob_col, "")) or None if dob_col else None,
                countries=(
                    (_clean(row.get(country_col, "")),)
                    if country_col and _clean(row.get(country_col, ""))
                    else ()
                ),
                programs=(
                    (_clean(row.get(program_col, "")),)
                    if program_col and _clean(row.get(program_col, ""))
                    else ()
                ),
            )
        )
    return out


def build_snapshot(entries: Iterable[WatchlistEntry], version: str) -> dict:
    """Assemble the snapshot dict the providers read (same schema as the fixture)."""
    rows = []
    for e in entries:
        rows.append(
            {
                "uid": e.uid,
                "source": e.source.value,
                "name": e.name,
                "entity_type": e.entity_type.value,
                "aliases": list(e.aliases),
                "dob": e.dob,
                "countries": list(e.countries),
                "programs": list(e.programs),
                "remark": e.remark,
            }
        )
    return {"version": version, "entries": rows}


def diff_counts(old: dict | None, new: dict) -> tuple[int, int]:
    """Return (added, removed) entry counts between two snapshots, keyed by uid."""
    old_uids = {e["uid"] for e in (old or {}).get("entries", [])}
    new_uids = {e["uid"] for e in new.get("entries", [])}
    return len(new_uids - old_uids), len(old_uids - new_uids)
