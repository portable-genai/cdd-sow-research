"""JSON-safe serialization for domain objects.

``to_jsonable(obj)`` converts dataclasses, enums, datetimes and nested containers
into plain JSON-serializable Python (``dict`` / ``list`` / ``str`` / ``int`` /
``float`` / ``bool`` / ``None``). Used by remote-platform clients (to build the
horizontal-platform / C1 HTTP payloads) and by the API layer to render the CDD
dossier.

Rules (mirrors SPEC §5 / §6):
* ``enum.Enum``  -> ``.value``
* ``datetime``   -> ``.isoformat()``
* dataclass      -> ``{field: to_jsonable(value)}`` (recursively)
* tuple / list   -> ``[to_jsonable(x), ...]`` (tuples become lists for JSON)
* dict           -> ``{to_key(k): to_jsonable(v)}`` (enum keys -> ``.value``)

``audit_event_from_jsonable`` is the deliberate inverse for the one record type that
must round-trip through an open, documented format: the audit trail. Exported audit
events reload as first-class :class:`AuditEvent` objects, so the exit story (P-12) is
"copy the JSONL file", not "migrate a product".

**Derived answers.** The dataclass rule above walks ``dataclasses.fields``, so a value a
model computes with an ``@property`` never reaches the wire.
:attr:`~cdd_sow_research.domain.models.UboResolution.beneficial_owners` is exactly that: the
natural persons the graph placed at or above the policy threshold, derived from
``findings`` by ``meets_threshold``. It is a frozen key of the ``resolve_ubo_graph`` A2A
contract (``docs/ubo-graph-contract.md``), whose
section 3 tells a consumer that ``findings`` is NOT the owner list and to read
``beneficial_owners`` instead. A consumer doing exactly that off the bare walk reads
nothing, and concludes no beneficial owner was identified for an entity where the graph
found one. :func:`ubo_resolution_jsonable` puts the derived answer back after the field
walk, and the A2A skill serializes through it, so the skill body and the
``UboGraphResponse`` the REST route returns stay the same shape by construction, as the
contract claims they are.

The walk itself stays a pure field walk. It is the persistence and rehydration encoding:
``_dataclass_from_jsonable`` reconstructs by field hints and the managed case store relies
on ``to_jsonable(sow_case_from_jsonable(p)) == p``, so a derived key must never enter it.

Pure standard library; no Google Cloud, ADK, or framework imports.
"""

from __future__ import annotations

import dataclasses
import enum
import types
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Union, get_args, get_origin, get_type_hints

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .models import (
        AuditEvent,
        PerpetualKycAssessment,
        PerpetualKycBaseline,
        SowCase,
        SowSnapshot,
        UboResolution,
    )


def _jsonable_key(key: Any) -> str:
    """Coerce a mapping key into a JSON object key (always a string)."""
    if isinstance(key, enum.Enum):
        return str(key.value)
    if isinstance(key, (str, int, float, bool)) or key is None:
        return str(key)
    return str(key)


def to_jsonable(obj: Any) -> Any:
    """Recursively convert ``obj`` into JSON-serializable Python.

    Handles dataclass instances, enums, datetimes/dates, tuples, lists, dicts and
    scalars. Unknown objects fall back to ``str(obj)`` so serialization never raises
    on an unexpected type at an audit/serialization boundary.
    """
    # Enums -> their value (which is itself made jsonable, e.g. for IntEnum). Checked
    # BEFORE the scalar branch: StrEnum/IntEnum members are also str/int instances, and
    # the wire format must stay the plain value, never the member object.
    if isinstance(obj, enum.Enum):
        return to_jsonable(obj.value)

    # Scalars that are already JSON-safe.
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj

    # Datetimes / dates -> ISO 8601 strings.
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()

    # Bytes -> best-effort UTF-8 (audit payloads should already be text).
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).decode("utf-8", errors="replace")

    # Dataclass *instances* (not the class itself) -> ordered field dict.
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}

    # Mappings.
    if isinstance(obj, dict):
        return {_jsonable_key(k): to_jsonable(v) for k, v in obj.items()}

    # Sequences / sets (tuples become lists for JSON).
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(x) for x in obj]

    # Last resort: stringify rather than raise at a serialization boundary.
    return str(obj)


def citation_to_dict(citation: Any) -> dict[str, Any]:
    """Serialize a ``Citation`` (or any citation-like dataclass) to a plain dict.

    Convenience wrapper that guarantees a ``dict`` result for the citation arrays in
    audit events and the dossier artifacts (SPEC §6 ``AuditEvent.citations``). Accepts
    any dataclass/enum-bearing object and delegates to :func:`to_jsonable`.
    """
    result = to_jsonable(citation)
    if isinstance(result, dict):
        return result
    return {"value": result}


def ubo_resolution_jsonable(resolution: UboResolution) -> dict[str, Any]:
    """One :class:`UboResolution` as the frozen ``resolve_ubo_graph`` body.

    ``beneficial_owners`` is a computed property, so the field walk alone would omit it and
    a consumer reading the key the contract tells it to read would see no owner at all. The
    key is taken off the domain object after the walk, which is also how
    ``UboGraphResponse.from_domain`` builds it for the REST route: one derivation, two
    surfaces, one shape.
    """
    data: dict[str, Any] = to_jsonable(resolution)
    data["beneficial_owners"] = to_jsonable(resolution.beneficial_owners)
    return data


# --------------------------------------------------------------------------- #
# Generic, type-hint-driven rehydration for the SowCase aggregate graph.
#
# The SowCase graph is deep and frozen (dozens of nested dataclasses mixing StrEnum,
# Enum, tz-aware datetimes, tuples and Optionals). Rather than ~40 hand-written
# constructors, a single recursive rehydrator keyed on ``dataclasses.fields`` +
# ``typing.get_type_hints`` reconstructs any of them, so the managed (Firestore) case
# store can round-trip ``to_jsonable(case)`` back to a first-class ``SowCase``. The
# invariant ``to_jsonable(sow_case_from_jsonable(p)) == p`` holds for any ``p`` produced
# by ``to_jsonable`` (tuples come back as tuples, enums as enums, datetimes tz-aware).
# --------------------------------------------------------------------------- #
_NONE_TYPE = type(None)


def _rehydrate(hint: Any, value: Any) -> Any:
    """Reconstruct ``value`` (from :func:`to_jsonable`) into an instance of ``hint``."""
    if value is None:
        return None

    origin = get_origin(hint)

    # Optional / Union (our models use ``X | None``): rehydrate as the first non-None arm.
    if origin is Union or isinstance(hint, types.UnionType):
        arms = [a for a in get_args(hint) if a is not _NONE_TYPE]
        return _rehydrate(arms[0], value) if arms else value

    # Homogeneous sequences: ``tuple[X, ...]`` -> tuple, ``list[X]`` -> list.
    if origin in (tuple, list):
        args = get_args(hint)
        elem = args[0] if args else Any
        seq = [_rehydrate(elem, v) for v in value]
        return tuple(seq) if origin is tuple else seq

    # Mappings: ``dict[K, V]`` (values rehydrated; JSON keys are strings).
    if origin is dict:
        args = get_args(hint)
        val_t = args[1] if len(args) == 2 else Any
        return {k: _rehydrate(val_t, v) for k, v in value.items()}

    if isinstance(hint, type):
        if issubclass(hint, enum.Enum):
            return hint(value)
        if hint is datetime:
            return datetime.fromisoformat(value)
        if hint is date:
            return date.fromisoformat(value)
        if dataclasses.is_dataclass(hint):
            return _dataclass_from_jsonable(hint, value)

    # Scalars (str/int/float/bool) and ``Any``: already JSON-native.
    return value


def _dataclass_from_jsonable(cls: type, payload: dict[str, Any]) -> Any:
    """Rehydrate a dataclass instance from its :func:`to_jsonable` dict, by field hints.

    Only fields present in ``payload`` are set (absent ones fall back to their default),
    so an older serialized case still loads as a forward-compatible aggregate.
    """
    hints = get_type_hints(cls)
    kwargs = {
        f.name: _rehydrate(hints[f.name], payload[f.name])
        for f in dataclasses.fields(cls)
        if f.name in payload
    }
    return cls(**kwargs)


def sow_case_from_jsonable(payload: dict[str, Any]) -> SowCase:
    """Rehydrate a :class:`SowCase` from its :func:`to_jsonable` form (managed case store)."""
    from .models import SowCase

    return _dataclass_from_jsonable(SowCase, payload)


def sow_snapshot_from_jsonable(payload: dict[str, Any]) -> SowSnapshot:
    """Rehydrate a sealed :class:`SowSnapshot` from its :func:`to_jsonable` form."""
    from .models import SowSnapshot

    return _dataclass_from_jsonable(SowSnapshot, payload)


def perpetual_kyc_baseline_from_jsonable(payload: dict[str, Any]) -> PerpetualKycBaseline:
    """Rehydrate a :class:`PerpetualKycBaseline` from its :func:`to_jsonable` form."""
    from .models import PerpetualKycBaseline

    return _dataclass_from_jsonable(PerpetualKycBaseline, payload)


def perpetual_kyc_assessment_from_jsonable(payload: dict[str, Any]) -> PerpetualKycAssessment:
    """Rehydrate a :class:`PerpetualKycAssessment` from its :func:`to_jsonable` form.

    The managed monitoring store round-trips the assessment graph (signals, uplifts, the
    queue item and the nested monitoring assessment), so a queue read returns first-class
    domain objects rather than dicts the API layer would have to interpret.
    """
    from .models import PerpetualKycAssessment

    return _dataclass_from_jsonable(PerpetualKycAssessment, payload)


def audit_event_from_jsonable(payload: dict[str, Any]) -> AuditEvent:
    """Rehydrate an :class:`AuditEvent` from its :func:`to_jsonable` form.

    ``to_jsonable(audit_event_from_jsonable(p)) == p`` for any payload produced by
    :func:`to_jsonable` (tuples come back as tuples, enums as enums, timestamps as
    timezone-aware datetimes). A missing/invalid required field raises ``KeyError`` /
    ``ValueError`` rather than guessing: an audit record must reload exactly or fail.
    """
    from .models import AuditEvent, Citation, Decision, SourceType

    citations = tuple(
        Citation(
            source_id=str(c["source_id"]),
            source_type=SourceType(c["source_type"]),
            title=str(c.get("title", "")),
            url=str(c.get("url", "")),
            page=c.get("page"),
            snippet=str(c.get("snippet", "")),
            score=c.get("score"),
        )
        for c in payload.get("citations") or ()
    )
    return AuditEvent(
        action=str(payload["action"]),
        actor=str(payload["actor"]),
        decision=Decision(payload["decision"]),
        redacted_prompt=str(payload.get("redacted_prompt", "")),
        redacted_response=str(payload.get("redacted_response", "")),
        citations=citations,
        resource=str(payload.get("resource", "cdd-sow-research")),
        trace_id=payload.get("trace_id"),
        span_id=payload.get("span_id"),
        correlation_id=payload.get("correlation_id"),
        run_id=payload.get("run_id"),
        event_id=str(payload.get("event_id", "")),
        schema_version=str(payload.get("schema_version", "audit-event/v2")),
        timestamp=datetime.fromisoformat(payload["timestamp"]),
        metadata={str(k): str(v) for k, v in (payload.get("metadata") or {}).items()},
    )
