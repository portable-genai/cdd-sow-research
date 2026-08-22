"""Unit tests for the deterministic name matcher and sanctions screening service."""

from __future__ import annotations

from cdd_sow_research.domain import name_match as nm
from cdd_sow_research.domain.models import (
    HitStatus,
    ListSource,
    Subject,
    SubjectType,
    WatchlistEntry,
)
from cdd_sow_research.domain.screening import ScreeningPolicy, ScreeningService


# --- name matching -------------------------------------------------------- #
def test_normalize_strips_accents_punctuation_case() -> None:
    assert nm.normalize("José  L. Gonçalves-Núñez!") == "jose l goncalves nunez"


def test_jaro_winkler_bounds_and_prefix() -> None:
    assert nm.jaro_winkler("martha", "martha") == 1.0
    assert nm.jaro_winkler("", "x") == 0.0
    assert nm.jaro_winkler("martha", "marhta") > 0.9  # transposition, high score


def test_token_set_order_independent() -> None:
    assert nm.name_score("Tan Wei Ming", "Wei Ming Tan") > 0.9


def test_org_suffixes_ignored() -> None:
    assert nm.name_score("Helios Maritime Ltd", "Helios Maritime Limited") > 0.95


def test_name_score_distinct_names_low() -> None:
    assert nm.name_score("Wei Zhang", "Tan Wei Ming") < 0.85


def test_dob_agreement() -> None:
    assert nm.dob_agreement("1968-04-12", "1968-04-12") == 1.0
    assert nm.dob_agreement("1968-04-12", "1968-09-01") == 0.5  # same year
    assert nm.dob_agreement("1968-04-12", "1975-01-01") == 0.0  # conflict
    assert nm.dob_agreement(None, "1968-04-12") is None


# --- screening service ---------------------------------------------------- #
class _FakeProvider:
    """Minimal in-memory SanctionsListProviderPort for tests."""

    def __init__(self, entries: list[WatchlistEntry], version: str = "v-test") -> None:
        self._entries = entries
        self._version = version

    def version(self) -> str:
        return self._version

    def iter_entries(self):
        return iter(self._entries)


def _entry(uid, name, source=ListSource.OFAC_SDN, aliases=(), dob=None, countries=()):
    return WatchlistEntry(
        uid=uid,
        source=source,
        name=name,
        entity_type=SubjectType.INDIVIDUAL,
        aliases=tuple(aliases),
        dob=dob,
        countries=tuple(countries),
    )


def _subject(name, dob=None):
    return Subject(id=name.lower().replace(" ", "-"), name=name, dob_or_incorp=dob)


def test_exact_match_raises_pending_alert_with_version() -> None:
    prov = _FakeProvider([_entry("1", "Tan Wei Ming", dob="1968-04-12")], version="2026-03-01")
    res = ScreeningService().screen_subject(_subject("Tan Wei Ming", "1968-04-12"), prov)
    assert len(res.alerts) == 1
    a = res.alerts[0]
    assert a.status is HitStatus.PENDING
    assert a.match.score == 1.0
    assert res.lists_version == "2026-03-01"
    assert res.escalates is True
    assert "dob exact" in a.match.features


def test_clean_name_no_alert() -> None:
    prov = _FakeProvider([_entry("1", "Tan Wei Ming")])
    res = ScreeningService().screen_subject(_subject("Jonathan Clean"), prov)
    assert res.alerts == ()
    assert res.escalates is False


def test_alias_match() -> None:
    prov = _FakeProvider([_entry("1", "Maria Goncalves", aliases=["Maria Gonsalves"])])
    res = ScreeningService().screen_subject(_subject("Maria Gonsalves"), prov)
    assert len(res.alerts) == 1
    assert res.alerts[0].match.matched_name == "Maria Gonsalves"


def test_dob_conflict_discounts_below_threshold() -> None:
    # Same name but a clearly different DOB discounts the score under the default 0.85.
    prov = _FakeProvider([_entry("1", "John A Smith", dob="1950-01-01")])
    strict = ScreeningService(threshold=0.95)
    res = strict.screen_subject(_subject("John A Smith", "1990-12-31"), prov)
    assert res.alerts == ()  # 1.0 name * 0.85 dob-conflict = 0.85 < 0.95


def test_threshold_configurable() -> None:
    prov = _FakeProvider([_entry("1", "Robert Smith")])
    loose = ScreeningService(threshold=0.5).screen_subject(_subject("Bob Smithe"), prov)
    strict = ScreeningService(threshold=0.99).screen_subject(_subject("Bob Smithe"), prov)
    assert len(loose.alerts) >= len(strict.alerts)


def test_sources_recorded_and_sorted_by_score() -> None:
    prov = _FakeProvider(
        [
            _entry("1", "Acme Holdings", source=ListSource.EU),
            _entry("2", "Acme Holding", source=ListSource.UN),
        ]
    )
    res = ScreeningService(threshold=0.7).screen_subject(_subject("Acme Holdings"), prov)
    scores = [a.match.score for a in res.alerts]
    assert scores == sorted(scores, reverse=True)
    assert set(res.sources) <= {ListSource.EU, ListSource.UN}


def test_open_alert_and_false_positive_disposition() -> None:
    from dataclasses import replace

    prov = _FakeProvider([_entry("1", "Tan Wei Ming")])
    res = ScreeningService().screen_subject(_subject("Tan Wei Ming"), prov)
    alert = res.alerts[0]
    assert alert.open is True
    fp = replace(alert, status=HitStatus.FALSE_POSITIVE)
    assert fp.open is False
    cleared = replace(res, alerts=(fp,))
    assert cleared.escalates is False  # all alerts dispositioned false positive


def test_policy_soft() -> None:
    pol = ScreeningPolicy()
    assert pol.requires_enhanced_review(None) is False
    prov = _FakeProvider([_entry("1", "Tan Wei Ming")])
    res = ScreeningService().screen_subject(_subject("Tan Wei Ming"), prov)
    assert pol.requires_enhanced_review(res) is True


def test_deterministic() -> None:
    prov = _FakeProvider([_entry("1", "Tan Wei Ming", dob="1968-04-12")])
    s = ScreeningService()
    a = s.screen_subject(_subject("Tan Wei Ming", "1968-04-12"), prov)
    b = s.screen_subject(_subject("Tan Wei Ming", "1968-04-12"), prov)
    assert [x.match.score for x in a.alerts] == [x.match.score for x in b.alerts]
