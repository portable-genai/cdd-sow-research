"""Gemini adverse-media adapter (AdverseMediaPort).

Scans public-web negative news for a subject via the Gemini API ``google_search``
grounding tool on the Gemini Enterprise Agent Platform. Per the one-built-in-tool-per
agent rule, ``google_search`` is isolated here (and in the agent layer, in its own
sub-agent). The model is prompted to return structured adverse-media hits, which are
mapped onto domain :class:`AdverseMediaFinding` objects with a ``MEDIA`` citation.

All Google GenAI SDK imports are lazy so the on-prem / test profile imports this module
without ``google-genai`` installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...config import Settings
from ...domain._grounded import parse_json_object
from ...domain.models import (
    AdverseMediaCategory,
    AdverseMediaFinding,
    AdverseMediaScreening,
    Citation,
    Severity,
    SourceType,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from google import genai

_CATEGORY_BY_VALUE = {c.value: c for c in AdverseMediaCategory}
_SEVERITY_BY_VALUE = {s.value: s for s in Severity}

_PROMPT = (
    "Search public-web news for adverse media on the subject named below. Return only "
    "credible negative-news hits relevant to financial crime (fraud, corruption, "
    "sanctions, money laundering, terrorism). For each hit give headline, publisher, "
    "url, published_date (ISO), category (one of fraud, corruption, sanctions, "
    "money_laundering, terrorism, other), severity (low, medium, high, critical) and a "
    "short snippet. "
    # Asked without this, the model answered a query about a company with no coverage by
    # returning the industry and the jurisdiction: a real prosecution naming real banks, which
    # the risk policy then turned into a PROHIBITED band for an unrelated subject. The
    # deterministic gate in the domain is what actually enforces this, because a prompt is a
    # request and not a guarantee; the instruction is here so the model is not asked to guess.
    "Every hit MUST be about this specific named subject: the subject's own name must appear "
    "in the article. An article about the same industry, the same country, or a similarly "
    "named organisation is NOT a hit and must be omitted. Reporting nothing is the correct "
    "answer for a subject with no coverage. "
    "If there is no credible adverse media, return an empty list.\n\n"
    'Return strictly JSON: {{"findings": [ ... ]}}.\n\nSubject: {subject_name}'
)


class GeminiAdverseMediaAdapter:
    """Adverse-media scanning via Gemini ``google_search`` grounding."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._models = settings.models
        self._enabled = settings.grounding_enabled
        self._client: Any | None = None

    def _get_client(self) -> genai.Client:
        if self._client is None:
            from google import genai

            self._client = genai.Client(
                vertexai=True,
                project=self._settings.project_id,
                # The MODEL location, not the compute region: see models.location in
                # config/settings.yaml. us-central1 serves no Gemini 3.
                location=self._settings.models.location,
            )
        return self._client

    def search(self, subject_name: str, max_results: int = 10) -> AdverseMediaScreening | None:
        """Return the adverse-media SCREEN for ``subject_name``, or ``None`` if none ran.

        This returned a bare ``list`` and ``[]`` when disabled, which breaks the port in the
        exact way its own docstring warns about, twice over. The caller reads ``.findings`` off
        the result, so a list crashed the whole assessment with
        ``AttributeError: 'list' object has no attribute 'findings'`` -- and had it not crashed,
        an empty list would have meant "screened, nothing found" for a search that never ran, so
        the dossier would have rendered an affirmative "No adverse media found" on the strength
        of a disabled backend.
        """

        if not self._enabled:
            # No search ran. Say so; an empty screening would claim a clean result.
            return None
        from google.genai import types

        client = self._get_client()
        response = client.models.generate_content(
            model=self._models.triage,
            # A single Content, not a one-element list: the SDK accepts both, and the
            # bare form types cleanly (list[Content] is invariant against the union).
            contents=types.Content(
                role="user",
                parts=[types.Part.from_text(text=_PROMPT.format(subject_name=subject_name))],
            ),
            config=types.GenerateContentConfig(
                temperature=0.0,
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        findings = self._parse(getattr(response, "text", "") or "")[:max_results]
        return AdverseMediaScreening(
            subject_name=subject_name,
            findings=tuple(findings),
            sources=("google-search",),
        )

    @staticmethod
    def _parse(text: str) -> list[AdverseMediaFinding]:
        # Tolerant read: a model using the google_search tool cannot also be put in JSON
        # mode, so it answers with a fenced block or a sentence of preamble. Parsing that
        # strictly returns "no adverse media found", which is indistinguishable from a
        # clean subject and is the one wrong answer this scan must never give silently.
        data = parse_json_object(text)
        raw_findings = data.get("findings")
        if not isinstance(raw_findings, list):
            return []
        out: list[AdverseMediaFinding] = []
        for raw in raw_findings:
            if not isinstance(raw, dict):
                continue
            headline = str(raw.get("headline") or "").strip()
            if not headline:
                continue
            url = str(raw.get("url") or "").strip()
            category = _CATEGORY_BY_VALUE.get(
                str(raw.get("category") or "").lower(), AdverseMediaCategory.OTHER
            )
            severity = _SEVERITY_BY_VALUE.get(
                str(raw.get("severity") or "").lower(), Severity.MEDIUM
            )
            snippet = str(raw.get("snippet") or "").strip()
            citation = Citation(
                source_id=f"media:{url or headline}",
                source_type=SourceType.MEDIA,
                title=headline,
                url=url,
                snippet=snippet,
            )
            out.append(
                AdverseMediaFinding(
                    headline=headline,
                    publisher=str(raw.get("publisher") or "").strip(),
                    url=url,
                    published_date=raw.get("published_date"),
                    category=category,
                    severity=severity,
                    snippet=snippet,
                    citation=citation,
                )
            )
        return out
