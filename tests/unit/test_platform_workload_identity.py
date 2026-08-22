from __future__ import annotations

from cdd_sow_research.adapters.platform import _s2s
from cdd_sow_research.config import Settings


def test_managed_platform_headers_mint_audience_bound_id_token(
    monkeypatch,
) -> None:
    monkeypatch.delenv("HRZ_S2S_TOKEN", raising=False)
    audiences: list[str] = []

    def mint(audience: str) -> str:
        audiences.append(audience)
        return "fresh-id-token"

    monkeypatch.setattr(_s2s, "_fetch_id_token", mint)
    headers = _s2s.headers(
        settings=Settings(profile="platform"),
        base_url="https://quality.example.test",
    )

    assert headers["Authorization"] == "Bearer fresh-id-token"
    assert audiences == ["https://quality.example.test"]
