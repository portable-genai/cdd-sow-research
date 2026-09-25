"""Compliance client that asks `compliance-advisory` over HTTP.

`cdd-sow-research` checks each dossier's rating against regulatory CDD/AML expectations by
asking `compliance-advisory`, the grounded compliance assistant. This adapter implements
:class:`ComplianceClientPort` by POSTing to its ``/ask`` endpoint and projecting the answer onto
a domain :class:`ComplianceAnswer`, citations included.

It is bound under ``gcp``, ``live`` and ``platform``: every profile other than the offline gate
asks the real service. ``RSK_COMPLIANCE_URL`` names that service and is read in three states
with no default. Under ``gcp`` and ``platform`` unset and emptied both refuse at construction,
which the API binds at boot: a managed profile names the service it asks rather than inheriting
a localhost guess, and a deployment that cannot check compliance does not start.

**The laptop run starts without it** (owner rule, 2026-09-23: the laptop never refuses to start
because a sibling is down). Under a deliberately named ``live`` profile an UNSET address builds
a client that asks nobody: every dossier then records the compliance leg as NOT CONFIGURED, and
one whose sibling is down records it as NO ANSWER, both as a typed state on the dossier rather
than a canned answer. An EMPTIED address still refuses, because it is an expressed choice that
names nothing and the three states stay distinct; so does a malformed one.

**The deployed call goes through the portal's IAP edge, not to the sibling service.** Every
route a deployed `compliance-advisory` answers on is an embedded app behind `journey-portal`:
its API takes internal traffic only, only the portal's own service account may invoke it, this
service has no VPC egress, and its managed identity accepts an IAP assertion and nothing else.
So a direct service-to-service call cannot succeed in any of four independent ways, and
``RSK_COMPLIANCE_URL`` is the portal edge path for that app,
``https://<rm-domain>/apps/compliance-advisory/api``. The base URL therefore carries a path
prefix, which survives validation, and ``/ask`` is appended inside the mount.

IAP accepts one bearer audience: the deployment's **IAP OAuth client id**. It is named by
``RSK_COMPLIANCE_IAP_AUDIENCE``, required under ``gcp`` and refused when emptied, because a
token minted for the edge's origin, or for the backend-service path IAP compares its own
assertion against, is rejected at the edge with nothing in this process able to tell why. This
is the same audience `journey-portal`'s ``make e2e-gcp`` and an app's ``make verify-deployed``
present as the `portal-e2e@` service account; only the minting differs (``gcloud`` impersonation
there, this service's own workload identity here).

``platform`` is unchanged by any of that: it still means a thin delegate to a sibling contract
reached directly, so it mints for the origin like the five platform adapters beside it, and the
audience is optional there. ``live`` calls a `compliance-advisory` the launcher started on
loopback, which runs no IAP, so it sends no token at all.
"""

from __future__ import annotations

import logging

import httpx

from ...config import Settings, iap_audience_or_refuse
from ...domain.errors import CddError, ComplianceNotConfiguredError
from ...domain.models import Citation, ComplianceAnswer, SourceType
from ...envread import optional_setting, required_setting, setting_or_default
from . import _s2s

#: The one environment variable that names the compliance-advisory base URL. Under ``gcp`` it is
#: the portal edge path for the embedded app, so it carries a path prefix.
URL_ENV = "RSK_COMPLIANCE_URL"
#: The one environment variable that names the audience the edge accepts: the IAP OAuth client id.
AUDIENCE_ENV = "RSK_COMPLIANCE_IAP_AUDIENCE"
#: The profile whose receiver is behind IAP, and so the profile the audience is required under.
_IAP_PROFILE = "gcp"
#: How long the answer may take, in seconds. Read in three states: unset takes the default,
#: emptied or non-positive refuses at construction, a value wins.
TIMEOUT_ENV = "RSK_COMPLIANCE_TIMEOUT_SECONDS"
#: The default read budget. The first deployed call, on 2026-09-22, found compliance-advisory
#: scaled to zero: its instance started when the question arrived, spent about ten seconds
#: starting, then about sixty retrieving and generating, and answered 200 some seventy seconds in.
#: The client had given up at thirty, so the dossier reported NOT CHECKED for an answer that
#: arrived. A scale-to-zero deployment makes a cold start part of every first call, so the budget
#: is the cold path with margin. The answer is advisory and a timeout is recorded rather than
#: failed, so a generous budget costs a slower dossier, never a wrong one.
_DEFAULT_READ_SECONDS = 120.0
_CONNECT_SECONDS = 5.0

_LOG = logging.getLogger(__name__)


def address_named() -> bool:
    """Has this run named a compliance-advisory address? Emptied refuses, as it does at boot."""
    return optional_setting(URL_ENV) is not None


class RemoteComplianceError(CddError):
    """Raised when compliance-advisory cannot be reached or answers with a non-2xx status."""


class RemoteComplianceAdapter:
    """HTTP client for the `compliance-advisory` ``/ask`` endpoint."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        configured = optional_setting(URL_ENV) if settings.laptop_run else required_setting(URL_ENV)
        #: ``None`` only on a laptop run that named no service: it asks nobody and says so.
        self._base_url: str | None = (
            None
            if configured is None
            else _s2s.validate_base_url(configured, service=type(self).__name__)
        )
        if self._base_url is None:
            _LOG.warning(
                "%s is not set: this %s run starts without compliance-advisory, and every "
                "dossier will report the compliance check as NOT CONFIGURED",
                URL_ENV,
                settings.profile,
            )
        self._audience = self._resolve_audience(settings)
        self._timeout = httpx.Timeout(_resolve_read_seconds(), connect=_CONNECT_SECONDS)

    @staticmethod
    def _resolve_audience(settings: Settings) -> str:
        """Resolve the bearer audience in three states, required only where IAP is the receiver.

        Under ``gcp`` the receiver is the portal's IAP edge, so an unnamed audience cannot be
        guessed and an emptied one is an expressed intent that names nothing: both refuse at
        construction and name the variable. Everywhere else the audience is absent and
        :func:`._s2s.headers` falls back to the receiver's own origin, which is what a direct
        Cloud Run call accepts.
        """
        if settings.profile == _IAP_PROFILE:
            return _audience_or_refuse(required_setting(AUDIENCE_ENV))
        configured = optional_setting(AUDIENCE_ENV)
        return _audience_or_refuse(configured) if configured else ""

    def check(self, question: str, actor: str) -> ComplianceAnswer:
        """Ask compliance-advisory a regulatory CDD/AML question and return its cited answer.

        ``actor`` is not sent. compliance-advisory resolves its principal from the verified
        caller and ignores any actor in the body, so putting one on the wire would only suggest
        that the receiver trusts it.
        """
        if self._base_url is None:
            raise ComplianceNotConfiguredError(
                f"{URL_ENV} is not set, so this run has no compliance-advisory to ask"
            )
        url = f"{self._base_url}/ask"
        payload = {"question": question, "filters": None}
        try:
            response = httpx.post(
                url,
                json=payload,
                timeout=self._timeout,
                headers=_s2s.headers(
                    settings=self._settings,
                    base_url=self._base_url,
                    audience=self._audience,
                ),
            )
        except httpx.HTTPError as exc:
            raise RemoteComplianceError(f"compliance request to {url} failed: {exc}") from exc
        if response.status_code // 100 != 2:
            raise RemoteComplianceError(
                f"compliance {url} returned {response.status_code}: {response.text[:500]}"
            )
        return self._parse(question, response.json())

    @staticmethod
    def _parse(question: str, body: dict) -> ComplianceAnswer:
        citations = tuple(
            Citation(
                source_id=str(item.get("source_id", "")),
                source_type=SourceType.REGULATION,
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                page=item.get("page"),
                snippet=str(item.get("snippet", "")),
                score=item.get("score"),
            )
            for item in (body.get("citations") or ())
        )
        return ComplianceAnswer(
            question=str(body.get("question", question)),
            answer=str(body.get("answer", "")),
            citations=citations,
            requires_human_review=bool(body.get("requires_human_review", True)),
            confidence=float(body.get("confidence", 0.0) or 0.0),
        )


def _audience_or_refuse(value: str) -> str:
    """Refuse a backend-service path pasted where the IAP OAuth client id belongs.

    The rule is shared with the review router's audience, so it lives in
    :func:`cdd_sow_research.config.iap_audience_or_refuse`; this names this adapter's variable.
    """
    return iap_audience_or_refuse(AUDIENCE_ENV, value)


def _resolve_read_seconds() -> float:
    """Resolve the read budget: unset takes the default, emptied or unusable refuses by name.

    Refused at construction, which the API binds at boot, so a deployment that named a budget
    nobody could meet fails to start and says which variable, rather than timing out every call.
    """
    raw = setting_or_default(TIMEOUT_ENV, str(_DEFAULT_READ_SECONDS))
    try:
        seconds = float(raw)
    except ValueError as exc:
        raise ValueError(f"{TIMEOUT_ENV} must be a number of seconds, got {raw!r}") from exc
    if not seconds > 0 or seconds == float("inf"):
        raise ValueError(f"{TIMEOUT_ENV} must be a positive, finite number of seconds, got {raw!r}")
    return seconds
