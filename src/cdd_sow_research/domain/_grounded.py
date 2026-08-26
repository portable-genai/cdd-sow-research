"""Shared grounded retrieve-reason-cite routine (private to the domain layer).

The CDD sub-services (source-of-wealth synthesis, risk rating) share the same
skeleton: render the case's retrieved passages into the prompt context, call the LLM
with a structured-output schema, defensively parse the JSON, and map the model's
``used_source_ids`` back to the retrieved passages' ``Citation`` objects (preserving
page provenance).

This module factors out that machinery (plus the severity/band/category coercions and
the LlmRequest builder) so each service keeps the exact constructor and method
signature mandated by SPEC §5 while sharing one well-tested core. It is ``_``-prefixed
and not part of the public domain API.

Pure domain code: talks only to ports and models, no Google Cloud / ADK imports.
"""

from __future__ import annotations

import json
from typing import Any

from .models import (
    AdverseMediaCategory,
    Citation,
    LlmMessage,
    LlmRequest,
    LlmResponse,
    RetrievalQuery,
    RetrievedPassage,
    RiskBand,
    Severity,
    ThinkingLevel,
)
from .prompts import PASSAGE_BLOCK

#: Severity rank for picking the "highest" severity across findings.
_SEVERITY_RANK: dict[Severity, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}

_SEVERITY_BY_VALUE: dict[str, Severity] = {s.value: s for s in Severity}
_RISK_BAND_BY_VALUE: dict[str, RiskBand] = {b.value: b for b in RiskBand}
_MEDIA_CATEGORY_BY_VALUE: dict[str, AdverseMediaCategory] = {
    c.value: c for c in AdverseMediaCategory
}

#: Ordered low -> high so a band can be "raised" by a downstream signal.
_RISK_BAND_RANK: dict[RiskBand, int] = {
    RiskBand.LOW: 0,
    RiskBand.MEDIUM: 1,
    RiskBand.HIGH: 2,
    RiskBand.PROHIBITED: 3,
}


def coerce_severity(value: Any, default: Severity = Severity.MEDIUM) -> Severity:
    """Map a model-emitted severity string to the ``Severity`` enum defensively."""
    if isinstance(value, Severity):
        return value
    if isinstance(value, str):
        return _SEVERITY_BY_VALUE.get(value.strip().lower(), default)
    return default


def coerce_risk_band(value: Any, default: RiskBand = RiskBand.MEDIUM) -> RiskBand:
    """Map a model-emitted band string to the ``RiskBand`` enum defensively."""
    if isinstance(value, RiskBand):
        return value
    if isinstance(value, str):
        return _RISK_BAND_BY_VALUE.get(value.strip().lower(), default)
    return default


def coerce_media_category(
    value: Any, default: AdverseMediaCategory = AdverseMediaCategory.OTHER
) -> AdverseMediaCategory:
    """Map a model-emitted adverse-media category to the enum defensively."""
    if isinstance(value, AdverseMediaCategory):
        return value
    if isinstance(value, str):
        return _MEDIA_CATEGORY_BY_VALUE.get(value.strip().lower(), default)
    return default


def highest_severity(severities: list[Severity]) -> Severity | None:
    """Return the most severe entry, or None for an empty list."""
    if not severities:
        return None
    return max(severities, key=lambda s: _SEVERITY_RANK[s])


def max_band(bands: list[RiskBand]) -> RiskBand | None:
    """Return the highest risk band, or None for an empty list."""
    if not bands:
        return None
    return max(bands, key=lambda b: _RISK_BAND_RANK[b])


def render_passages(passages: list[RetrievedPassage]) -> str:
    """Render retrieved passages into the numbered evidence block for the prompt.

    Each block is keyed by ``source_id`` and page so the model can echo
    ``[source_id p.N]`` citations exactly. Page is rendered as ``?`` when unknown so
    the model emits ``[source_id]`` rather than inventing a page.
    """
    if not passages:
        return "(no evidence was retrieved)"
    blocks: list[str] = []
    for p in passages:
        c = p.citation
        page = str(c.page) if c.page is not None else "?"
        blocks.append(
            PASSAGE_BLOCK.format(
                source_id=c.source_id,
                page=page,
                source_type=c.source_type.value,
                title=c.title,
                text=p.text.strip(),
            )
        )
    return "\n".join(blocks)


def retrieve_passages(
    knowledge_base: Any,
    query_text: str,
    acl_principals: tuple[str, ...] = (),
    top_k: int = 10,
) -> list[RetrievedPassage]:
    """Run a governed retrieval query through the KnowledgeBaseClientPort (A2)."""
    query = RetrievalQuery(text=query_text, top_k=top_k, acl_principals=tuple(acl_principals))
    passages = knowledge_base.search(query)
    return list(passages or [])


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a model's answer into a dict, defensively. Never raises.

    The single tolerant JSON reader for every caller that asks a model for structured
    output, because models do not reliably return bare JSON. In particular, a model
    using a built-in search tool cannot also be put in JSON mode, so a grounded answer
    routinely arrives wrapped in a markdown fence or a sentence of preamble. Strict
    parsing of those replies silently yields nothing, which shows up as a dossier with
    no adverse media and no beneficial owners rather than as an error: exactly the
    failure a reviewer cannot see.

    So: parse as JSON, else recover the first balanced ``{...}`` block (which handles
    fences and preamble alike), else give back an empty dict so callers degrade.
    """
    text = (text or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"items": parsed}
    except (json.JSONDecodeError, ValueError):
        pass

    snippet = _extract_json_object(text)
    if snippet is not None:
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def parse_structured(response: LlmResponse) -> dict[str, Any]:
    """Parse an LLM structured-output response into a dict, defensively.

    The GCP adapter returns the structured JSON as ``LlmResponse.text`` when a
    ``response_schema`` is set; see :func:`parse_json_object` for the reader.
    """
    return parse_json_object(response.text or "")


def _extract_json_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` block in ``text``, or None."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def citations_for_source_ids(
    used_source_ids: list[str],
    passages: list[RetrievedPassage],
) -> tuple[Citation, ...]:
    """Map model-returned ``used_source_ids`` back to retrieved passage Citations.

    Preserves the page-level provenance from retrieval (the model only returns ids,
    never pages). When a source_id was cited by multiple passages, each distinct
    (source_id, page) citation is kept once, in retrieval order. Unknown ids the model
    may have hallucinated are dropped: we only ever cite what we retrieved/derived.
    """
    by_id: dict[str, list[Citation]] = {}
    for p in passages:
        by_id.setdefault(p.citation.source_id, []).append(p.citation)

    wanted = list(used_source_ids or [])
    # If the model returned nothing usable, fall back to all retrieved citations so a
    # claim is never left provenance-less.
    selected_ids = [sid for sid in wanted if sid in by_id]
    if not selected_ids:
        selected_ids = list(by_id.keys())

    out: list[Citation] = []
    seen: set[tuple[str, int | None]] = set()
    for sid in selected_ids:
        for citation in by_id.get(sid, ()):
            key = (citation.source_id, citation.page)
            if key not in seen:
                seen.add(key)
                out.append(citation)
    return tuple(out)


def build_llm_request(
    system_instruction: str,
    user_content: str,
    model: str | None,
    response_schema: dict | None,
    thinking: ThinkingLevel = ThinkingLevel.HIGH,
    temperature: float = 0.0,
    max_output_tokens: int = 4096,
) -> LlmRequest:
    """Assemble an ``LlmRequest`` with a single user message and a system prompt.

    ``model=None`` lets the adapter pick its configured default (the reasoning model,
    ``gemini-3.5-flash``); thinking defaults to HIGH for grounded reasoning per SPEC.

    **Temperature is 0.0 and that is a correctness setting, not a style one.** Every service
    reached through this builder decides a field the paired demonstration COMPARES -- the risk
    band and score, the source-of-wealth sources and confidence, the UBO graph, the
    perpetual-KYC pass. It defaulted to 0.2 until 2026-08-26, and the deployment proved what
    that costs: two runs of the identical case, same subject and same single-document corpus,
    minutes apart, returned ``score`` 0.5 then 0.0, ``confidence`` 0.4 then 1.0, and four
    scorecard factors then none. A dossier the system calls deterministic was sampling.

    Every adapter making its own grounded call had already pinned 0.0 -- adverse media,
    ownership, the registry lookup, extraction -- so this builder was the outlier rather than
    the precedent, and the source-of-wealth self-critique pass passed 0.0 explicitly because
    whoever wrote it knew the scored field needed it.

    This is NOT a claim of determinism. A hosted model can still vary across batching and
    revisions; 0.0 is the strongest thing a caller controls, and it is a precondition for the
    pair being a measurement rather than a sample. Whether the deployment is actually
    reproducible is settled by running it twice, not by this default.
    """
    return LlmRequest(
        messages=(LlmMessage(role="user", content=user_content),),
        system_instruction=system_instruction,
        model=model,
        thinking=thinking,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        response_schema=response_schema,
    )


def maybe_record_usage(tracer: Any, response: Any) -> None:
    """Emit token usage to the tracer for FinOps, defensively (never fatal)."""
    try:
        usage = getattr(response, "usage", None)
        model = getattr(response, "model", "") or ""
        if usage is not None and hasattr(tracer, "record_token_usage"):
            tracer.record_token_usage(usage, model)
    except Exception:  # noqa: BLE001 - metrics must never break a generation path
        return


def as_str_list(value: Any) -> list[str]:
    """Coerce an arbitrary model value into a list of stripped non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def clamp(value: Any) -> float:
    """Clamp a confidence/score into [0.0, 1.0], defaulting non-numerics to 0.0."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))
