"""Whether a screen that never ran says so anywhere an operator can see.

The dossier has always distinguished "not screened" (``screening is None``) from "screened
and clear" (a result with no alerts), and that distinction is right. What it could not do
is say WHY nothing ran, because ``_screen`` swallowed every exception into the same None
and emitted nothing at all -- no log, no metric, no span attribute.

The cost was paid on the deployment. Its watchlist snapshot was unreadable, so every
dossier it produced had screened nobody, and the field on the wire said ``null``, which
reads as "this profile does not screen" rather than "screening is down". It ran that way
until a paired run compared it against a laptop that had screened six lists, and the
divergence, not the outage, is what raised the alarm.

Two events had been collapsing into one None:

* no provider bound under this profile -- a configuration fact, true at boot, unchanging;
* a provider bound and FAILING -- an outage, transient, and the one worth waking someone.

These tests hold them apart, and hold the degradation itself in place: neither case may
raise, because a screening outage must not fail a dossier that the maker-checker gate will
still review.
"""

from __future__ import annotations

import logging

import pytest

from cdd_sow_research.domain.models import (
    ListSource,
    Subject,
    SubjectType,
    WatchlistEntry,
)

_LOGGER_NAME = "cdd_sow_research.domain.cdd_service"

#: A subject nothing on any list matches, so a working screen returns a CLEAR result and
#: the tests distinguish "screened, clear" from "not screened" on the outcome, not on luck.
SUBJECT = Subject(id="subj-meridian", name="Meridian Harbour Holdings Pte Ltd", jurisdiction="SG")


class _UnreadableSnapshot:
    """A wired provider whose snapshot cannot be read: the deployment's actual state."""

    def version(self) -> str:
        raise RuntimeError("404 GET /sanctions/snapshot/current.json: no such object")

    def iter_entries(self):  # type: ignore[no-untyped-def]
        raise RuntimeError("404 GET /sanctions/snapshot/current.json: no such object")


class _WorkingSnapshot:
    def version(self) -> str:
        return "2026-03-01-fixture-v1"

    def iter_entries(self):  # type: ignore[no-untyped-def]
        return iter(
            (
                WatchlistEntry(
                    uid="1",
                    source=ListSource.OFAC_SDN,
                    name="Someone Else Entirely",
                    entity_type=SubjectType.INDIVIDUAL,
                    list_version="2026-03-01-fixture-v1",
                ),
            )
        )


def _service_with(sanctions, cdd_service):  # type: ignore[no-untyped-def]
    """Rebind the assembled service's provider, leaving every other port real."""

    cdd_service._sanctions = sanctions
    return cdd_service


def test_an_unreadable_snapshot_is_logged_as_an_outage(
    cdd_service, caplog: pytest.LogCaptureFixture
) -> None:
    service = _service_with(_UnreadableSnapshot(), cdd_service)

    with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
        result = service._screen(SUBJECT)

    assert result is None, "the dossier must still say NOT SCREENED, not carry a false clear"
    assert caplog.records, "an outage that logs nothing is the defect this test exists for"
    record = caplog.records[-1]
    assert record.levelno == logging.ERROR
    message = record.getMessage()
    assert "NOT SCREENED" in message
    assert "no such object" in message, "the cause must survive into the message"


def test_no_provider_bound_is_not_reported_as_an_outage(
    cdd_service, caplog: pytest.LogCaptureFixture
) -> None:
    """A profile that binds no provider is configured, not broken. ERROR would be noise,
    and noise at ERROR is how a real outage stops being noticed."""

    service = _service_with(None, cdd_service)

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        result = service._screen(SUBJECT)

    assert result is None
    assert caplog.records, "silence here is what made the two cases indistinguishable"
    assert all(r.levelno < logging.ERROR for r in caplog.records)


def test_a_screening_outage_still_degrades_rather_than_failing(cdd_service) -> None:
    """The deliberate half of the behaviour, kept: screening is best-effort by design."""

    service = _service_with(_UnreadableSnapshot(), cdd_service)

    assert service._screen(SUBJECT) is None  # no exception escapes


def test_a_working_provider_screens_and_logs_no_outage(
    cdd_service, caplog: pytest.LogCaptureFixture
) -> None:
    """Proves the ERROR path is reached by failure and not by every call."""

    service = _service_with(_WorkingSnapshot(), cdd_service)

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        result = service._screen(SUBJECT)

    assert result is not None, "a readable snapshot must produce a real screening result"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
