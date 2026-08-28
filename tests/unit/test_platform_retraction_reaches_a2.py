"""The platform profile retracts through A2 instead of refusing by name.

`RemoteKnowledgeBaseAdapter.retract` raised `NotImplementedError` because A2 exposed no
retraction endpoint, and refusing was the right call at the time: returning False would have
told a caller withdrawing evidence that nothing was indexed while the passage stayed citable.

A2 now serves `POST /v1/documents/{id}/retract`, entitled separately from reading and from the
pipeline ingest path, so the refusal is no longer honest. It is now a client for that route.

The mapping from A2's answers back onto the port contract is the part worth pinning:

* 200 means the passages went, so True, matching the local adapter;
* 404 means nothing was indexed under that id, so False, and a repair run twice stays as safe
  as a repair run once;
* 403 is A2 refusing the retraction entitlement, and it must surface as `PermissionError`, the
  same way the local adapter refuses a caller who cannot read the document. It must NOT become
  a False, because "you may not remove this" and "there was nothing to remove" are the two
  answers this port exists to keep apart.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from cdd_sow_research.adapters.platform import remote_knowledge_base as rkb
from cdd_sow_research.adapters.platform.remote_knowledge_base import (
    RemoteKnowledgeBaseAdapter,
    RemoteKnowledgeBaseError,
)


class _Settings:
    """The adapter reads only a base URL and the service-to-service header inputs."""

    def __init__(self) -> None:
        self.kb_url = "http://a2.test"


def _adapter(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> Any:
    seen: dict[str, Any] = {}

    def _post(url: str, **kwargs: Any) -> httpx.Response:
        seen["url"] = url
        seen["json"] = kwargs.get("json")
        return response

    monkeypatch.setattr(rkb.httpx, "post", _post)
    monkeypatch.setattr(rkb._s2s, "headers", lambda **_: {})
    monkeypatch.setattr(rkb, "setting_or_default", lambda *a, **k: "http://a2.test")
    adapter = RemoteKnowledgeBaseAdapter.__new__(RemoteKnowledgeBaseAdapter)
    adapter._base_url = "http://a2.test"
    adapter._settings = _Settings()
    return adapter, seen


def _response(status: int, body: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=body if body is not None else {},
        request=httpx.Request("POST", "http://a2.test"),
    )


def test_a_successful_retraction_returns_true_and_calls_the_governed_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, seen = _adapter(monkeypatch, _response(200, {"status": "retracted"}))

    assert adapter.retract("doc-1", ("case:abc",)) is True
    assert seen["url"] == "http://a2.test/v1/documents/doc-1/retract"


def test_nothing_indexed_is_false_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, _ = _adapter(monkeypatch, _response(404, {"detail": "unknown document"}))

    assert adapter.retract("doc-missing", ("case:abc",)) is False


def test_a_refused_entitlement_raises_rather_than_reporting_nothing_was_indexed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distinction this port exists to keep: refused is not absent."""
    adapter, _ = _adapter(monkeypatch, _response(403, {"detail": "no retraction entitlement"}))

    with pytest.raises(PermissionError):
        adapter.retract("doc-1", ("case:abc",))


def test_any_other_failure_is_still_a_loud_error(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, _ = _adapter(monkeypatch, _response(500, {"detail": "boom"}))

    with pytest.raises(RemoteKnowledgeBaseError):
        adapter.retract("doc-1", ("case:abc",))
