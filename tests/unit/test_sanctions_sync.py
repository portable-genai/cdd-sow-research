"""Unit tests for the sanctions snapshot parsers (OFAC CSV, UN XML, delimited)."""

from __future__ import annotations

from cdd_sow_research.adapters.local import sanctions_sync as sx
from cdd_sow_research.domain.models import ListSource, SubjectType

# Header-less OFAC legacy sdn.csv rows (12 columns).
_SDN = (
    '101,"DOE, John",individual,CYBER,-0-,-0-,-0-,-0-,-0-,-0-,-0-,"DOB 12 Apr 1968; nationality SG."\n'  # noqa: E501
    '102,"NORTHWIND TRADING FZE",-0-,NS-ISA,-0-,-0-,-0-,-0-,-0-,-0-,-0-,"Free zone establishment."\n'  # noqa: E501
)
_ALT = '101,1,aka,"DOE, Johnny",-0-\n101,2,aka,"J. DOE",-0-\n'


def test_parse_ofac_sdn_csv_individual_with_aliases_and_dob() -> None:
    entries = sx.parse_ofac_sdn_csv(_SDN, _ALT)
    assert len(entries) == 2
    john = entries[0]
    assert john.uid == "OFAC-101"
    assert john.source is ListSource.OFAC_SDN
    assert john.entity_type is SubjectType.INDIVIDUAL
    assert set(john.aliases) == {"DOE, Johnny", "J. DOE"}
    assert john.dob == "12 Apr 1968"
    assert john.programs == ("CYBER",)


def test_parse_ofac_entity_type_and_consolidated_source() -> None:
    entries = sx.parse_ofac_sdn_csv(_SDN, source=ListSource.OFAC_CONSOLIDATED)
    fze = entries[1]
    assert fze.entity_type is SubjectType.ENTITY
    assert fze.source is ListSource.OFAC_CONSOLIDATED
    assert fze.aliases == ()  # no alt file passed


def test_parse_ofac_skips_blank_names_and_short_rows() -> None:
    assert sx.parse_ofac_sdn_csv("bad,row\n") == []
    assert sx.parse_ofac_sdn_csv("1,-0-,individual,P,-0-,-0-,-0-,-0-,-0-,-0-,-0-,-0-\n") == []


_UN_XML = """<?xml version="1.0"?>
<CONSOLIDATED_LIST>
  <INDIVIDUALS>
    <INDIVIDUAL>
      <DATAID>6908555</DATAID>
      <FIRST_NAME>Abdul</FIRST_NAME>
      <SECOND_NAME>Rahman</SECOND_NAME>
      <THIRD_NAME>Khan</THIRD_NAME>
      <NATIONALITY><VALUE>AF</VALUE></NATIONALITY>
      <INDIVIDUAL_ALIAS><ALIAS_NAME>Abd al-Rahman Khan</ALIAS_NAME></INDIVIDUAL_ALIAS>
      <INDIVIDUAL_DATE_OF_BIRTH><DATE>1980-01-01</DATE></INDIVIDUAL_DATE_OF_BIRTH>
      <COMMENTS1>Test entry.</COMMENTS1>
    </INDIVIDUAL>
  </INDIVIDUALS>
  <ENTITIES>
    <ENTITY><DATAID>99</DATAID><FIRST_NAME>Helios Maritime</FIRST_NAME></ENTITY>
  </ENTITIES>
</CONSOLIDATED_LIST>"""


def test_parse_un_individuals_and_entities() -> None:
    entries = sx.parse_un_consolidated_xml(_UN_XML)
    assert len(entries) == 2
    ind = entries[0]
    assert ind.name == "Abdul Rahman Khan"
    assert ind.aliases == ("Abd al-Rahman Khan",)
    assert ind.dob == "1980-01-01"
    assert ind.countries == ("AF",)
    assert ind.source is ListSource.UN
    ent = entries[1]
    assert ent.entity_type is SubjectType.ENTITY
    assert ent.name == "Helios Maritime"


def test_parse_delimited_generic() -> None:
    text = "Name,Aliases,Country\nMaria Goncalves,Maria Gonsalves;M Goncalves,BR\n"
    entries = sx.parse_delimited(
        text, ListSource.UK_HMT, name_col="Name", alias_col="Aliases", country_col="Country"
    )
    assert len(entries) == 1
    e = entries[0]
    assert e.name == "Maria Goncalves"
    assert e.aliases == ("Maria Gonsalves", "M Goncalves")
    assert e.countries == ("BR",)
    assert e.source is ListSource.UK_HMT


def test_build_snapshot_and_diff() -> None:
    entries = sx.parse_un_consolidated_xml(_UN_XML)
    snap = sx.build_snapshot(entries, version="2026-03-01")
    assert snap["version"] == "2026-03-01"
    assert len(snap["entries"]) == 2
    added, removed = sx.diff_counts(None, snap)
    assert (added, removed) == (2, 0)
    smaller = sx.build_snapshot(entries[:1], version="2026-03-02")
    a2, r2 = sx.diff_counts(snap, smaller)
    assert (a2, r2) == (0, 1)
