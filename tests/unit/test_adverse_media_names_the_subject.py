"""Adverse media must be ABOUT the subject, not merely near it.

Found on 2026-08-26 by the paired demonstration. Asked for adverse media on a fictional company,
the deployment's grounded web search returned a real money-laundering prosecution naming real
banks, marked it ``critical``, and the risk policy turned that into a PROHIBITED band. The
laptop, which does not search the public web, said medium. The pair caught it through
``rating.band``, which is the reason the band is compared and the media itself is not.

Two things were wrong and only one is about search quality. The model decided an outcome: a
returned article carried its severity straight into the most consequential field in the dossier,
with nothing deterministic in between. This system does not allow that anywhere else, and it does
not allow it here now.

The gate is deliberately conservative. Dropping an unverifiable hit costs a finding. Keeping one
costs a subject the most severe band the system can assign, on evidence about somebody else.
"""

from __future__ import annotations

import pytest

from cdd_sow_research.domain.adverse_media_service import finding_names_subject
from cdd_sow_research.domain.models import AdverseMediaCategory, AdverseMediaFinding, Severity

_SUBJECT = "Meridian Harbour Holdings Pte Ltd"


def _finding(headline: str, snippet: str = "") -> AdverseMediaFinding:
    return AdverseMediaFinding(
        headline=headline,
        publisher="Example Wire",
        url="https://news.example/story",
        category=AdverseMediaCategory.MONEY_LAUNDERING,
        severity=Severity.CRITICAL,
        snippet=snippet,
    )


def test_the_real_hit_that_forced_a_prohibited_band_is_refused() -> None:
    """The exact article the deployment returned, headline and snippet as received.

    It names six real banks and a real prosecution. It does not name the subject anywhere, and
    the only thing it shares with the subject is a jurisdiction.
    """

    finding = _finding(
        "Singapore's banking giants entangled in US$740 million money-laundering scandal",
        "Some of the biggest local and international banks in Singapore are becoming embroiled "
        "in one of the city state's largest money laundering cases, involving over S$1 billion "
        "of assets.",
    )

    assert finding_names_subject(_SUBJECT, finding) is False


def test_a_hit_that_names_the_subject_in_full_is_kept() -> None:
    finding = _finding("Meridian Harbour Holdings Pte Ltd charged with fraud")

    assert finding_names_subject(_SUBJECT, finding) is True


def test_a_hit_that_names_the_subject_without_its_legal_form_is_kept() -> None:
    """A publisher writes 'Meridian Harbour'; the register writes the full legal name."""

    finding = _finding("Regulator opens probe into Meridian Harbour")

    assert finding_names_subject(_SUBJECT, finding) is True


def test_the_subject_named_only_in_the_snippet_is_kept() -> None:
    """The headline is not the whole finding; the snippet is evidence too."""

    finding = _finding(
        "Three firms named in probe",
        "Investigators confirmed that Meridian Harbour Holdings is among those under review.",
    )

    assert finding_names_subject(_SUBJECT, finding) is True


@pytest.mark.parametrize(
    "headline",
    [
        "Harbour Freight recalls power tools over safety defect",
        "Meridian Energy reports record quarterly profit",
        "Holdings across the sector fell sharply",
    ],
    ids=["one-token-harbour", "one-token-meridian", "one-token-holdings"],
)
def test_a_single_shared_word_is_not_a_hit(headline: str) -> None:
    """The failure mode this exists to stop: a common word standing in for an identity."""

    assert finding_names_subject(_SUBJECT, _finding(headline)) is False


def test_a_subject_with_no_distinctive_name_admits_nothing() -> None:
    """A name that is only a legal form has nothing to match on, so it refuses.

    Admitting everything would be the permissive default, and this is a fail-closed system.
    """

    assert finding_names_subject("Pte Ltd", _finding("Some company charged with fraud")) is False


def test_matching_ignores_case_and_punctuation() -> None:
    finding = _finding("MERIDIAN HARBOUR HOLDINGS, PTE. LTD. under investigation")

    assert finding_names_subject(_SUBJECT, finding) is True
