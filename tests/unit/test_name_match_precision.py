"""Screening precision: a partial name overlap is not a certain match.

Organisation suffixes are stripped before comparison, which is right ("Acme Ltd" is
"Acme Limited") but leaves some watchlist names reduced to a single generic industry
word. Scoring only how well the SHORTER name was covered then made every such subset a
1.00 match, so against a real 20,000-entry watchlist any company sharing one common word
drew a certain-match alert. These pin the fix in both directions: the noise is gone, and
the matches screening exists to catch still clear the threshold.
"""

from __future__ import annotations

import pytest

from cdd_sow_research.domain.name_match import name_score, token_set_ratio

#: The configured OFAC-style alerting threshold (``sanctions.match_threshold``).
THRESHOLD = 0.85


@pytest.mark.parametrize(
    ("query", "entry"),
    [
        # The case observed against the real OFAC snapshot: both names reduce to the
        # single shared token "logistics" once suffixes are stripped.
        ("Meridian Logistics Holdings Pte Ltd", "TRUST LOGISTICS LLC"),
        ("Apex Shipping Holdings Ltd", "GLOBAL SHIPPING GROUP"),
        ("Northwind Trading Pte Ltd", "TRADING COMPANY LLC"),
        ("Harbourfront Capital Partners", "CAPITAL GROUP"),
    ],
)
def test_a_shared_generic_word_is_not_an_alert(query: str, entry: str):
    assert name_score(query, entry) < THRESHOLD


@pytest.mark.parametrize(
    ("query", "entry"),
    [
        ("Banco Nacional de Cuba", "BANCO NACIONAL DE CUBA"),  # exact, case aside
        ("Acme Trading Pte Ltd", "ACME TRADING LIMITED"),  # suffix variance only
        ("Jose Garcia Lopez", "JOSE GARCIA-LOPEZ"),  # punctuation
        ("Vladimir Putin", "Vladimir Vladimirovich Putin"),  # extra middle name
        ("Rosneft", "ROSNEFT"),  # a short form, as carried in entry aliases
        ("Sberbank Rossii", "SBERBANK ROSSII PAO"),
    ],
)
def test_the_matches_screening_exists_to_catch_still_alert(query: str, entry: str):
    assert name_score(query, entry) >= THRESHOLD


def test_the_score_is_symmetric():
    """Argument order must not change a screening outcome."""
    a, b = "Meridian Logistics Holdings Pte Ltd", "TRUST LOGISTICS LLC"
    assert token_set_ratio(a, b) == token_set_ratio(b, a)
    assert name_score(a, b) == name_score(b, a)


def test_an_unrelated_name_scores_well_below_the_threshold():
    """Not zero: Jaro-Winkler gives any two ASCII strings a floor from incidental
    character overlap. What matters is the margin to the alerting threshold."""
    assert name_score("Meridian Logistics Holdings", "Zephyr Pharmaceuticals") < 0.7
