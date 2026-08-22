"""Sensitive Data Protection (DLP) redaction adapter (PIIRedactionPort, A1, R1).

Implements :class:`PIIRedactionPort` against **Sensitive Data Protection / DLP** of the
Gemini Enterprise Agent Platform. Because B1 processes customer KYC, every prompt, model
input, index payload and audit record is de-identified at the boundary first, so PII is
minimised to the model (P-04). The call is regional
(``projects/{project}/locations/{region}``) to keep inspection inside Singapore.

If inspect/de-identify templates are configured, they are used as-is. Otherwise the
adapter builds an inline configuration that masks the info types most relevant to APAC
KYC: names, emails, phone numbers, passport numbers, card numbers, and a custom Singapore
NRIC/FIN detector.

The ``google.cloud.dlp_v2`` import is lazy so on-prem and test profiles load this module
with no GCP SDK installed.
"""

from __future__ import annotations

from typing import Any

from ...config import Settings
from ...domain.models import RedactionFinding, RedactionResult

_DEFAULT_INFO_TYPES: tuple[str, ...] = (
    "PERSON_NAME",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "PASSPORT",
    "CREDIT_CARD_NUMBER",
    "IBAN_CODE",
)

_SG_NRIC_INFO_TYPE = "SG_NRIC_FIN"
_SG_NRIC_REGEX = r"[STFGM]\d{7}[A-Z]"

_MASKING_CHAR = "#"


class DlpRedactionAdapter:
    """De-identify PII via DLP ``deidentify_content`` (templates or inline config)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._dlp = settings.dlp
        self._parent = f"projects/{settings.project_id}/locations/{settings.region}"
        self._client: Any | None = None

    def redact(self, text: str) -> RedactionResult:
        """Return de-identified text plus per-info-type finding counts."""
        if not text:
            return RedactionResult(text=text, findings=())
        client = self._service_client()
        request = self._build_request(text)
        response = client.deidentify_content(request=request)
        redacted_text: str = response.item.value
        return RedactionResult(text=redacted_text, findings=self._summarise(response))

    def _service_client(self) -> Any:
        from google.cloud import dlp_v2  # lazy

        if self._client is None:
            self._client = dlp_v2.DlpServiceClient()
        return self._client

    def _build_request(self, text: str) -> dict[str, Any]:
        request: dict[str, Any] = {"parent": self._parent, "item": {"value": text}}
        if self._dlp.deidentify_template:
            request["deidentify_template_name"] = self._dlp.deidentify_template
        else:
            request["deidentify_config"] = self._inline_deidentify_config()
        if self._dlp.inspect_template:
            request["inspect_template_name"] = self._dlp.inspect_template
        elif not self._dlp.deidentify_template:
            request["inspect_config"] = self._inline_inspect_config()
        return request

    def _inline_inspect_config(self) -> dict[str, Any]:
        # verify: https://cloud.google.com/dlp/docs/reference/rest/v2/InspectConfig
        return {
            "info_types": [{"name": name} for name in _DEFAULT_INFO_TYPES],
            "custom_info_types": [
                {
                    "info_type": {"name": _SG_NRIC_INFO_TYPE},
                    "regex": {"pattern": _SG_NRIC_REGEX},
                    "likelihood": "POSSIBLE",
                }
            ],
            "min_likelihood": "POSSIBLE",
            "include_quote": False,
        }

    def _inline_deidentify_config(self) -> dict[str, Any]:
        # verify: https://cloud.google.com/dlp/docs/reference/rest/v2/DeidentifyConfig
        all_info_types = [{"name": name} for name in _DEFAULT_INFO_TYPES] + [
            {"name": _SG_NRIC_INFO_TYPE}
        ]
        return {
            "info_type_transformations": {
                "transformations": [
                    {
                        "info_types": all_info_types,
                        "primitive_transformation": {
                            "character_mask_config": {"masking_character": _MASKING_CHAR}
                        },
                    }
                ]
            }
        }

    def _summarise(self, response: Any) -> tuple[RedactionFinding, ...]:
        overview = getattr(response, "overview", None)
        summaries = getattr(overview, "transformation_summaries", None) or []
        findings: list[RedactionFinding] = []
        for summary in summaries:
            info_type = getattr(getattr(summary, "info_type", None), "name", "")
            if not info_type:
                continue
            findings.append(
                RedactionFinding(info_type=info_type, count=self._transformed_count(summary))
            )
        return tuple(findings)

    @staticmethod
    def _transformed_count(summary: Any) -> int:
        total = 0
        for result in getattr(summary, "results", None) or []:
            code = getattr(result, "code", None)
            code_name = getattr(code, "name", str(code))
            if code_name == "SUCCESS":
                total += int(getattr(result, "count", 0) or 0)
        return total or 1
