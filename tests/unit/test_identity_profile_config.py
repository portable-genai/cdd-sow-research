"""Priority 1: independent runtime, identity, and browser-channel configuration."""

from __future__ import annotations

from dataclasses import replace

import pytest

from cdd_sow_research.config import (
    ChannelSettings,
    IssuerSettings,
    Settings,
)

CONFIG = "config/settings.yaml"
_SELECTORS = (
    "CDD_PROFILE",
    "CDD_IDENTITY_PROFILE",
    "CDD_CHANNEL_PROFILE",
    "CDD_STANDALONE",
)


@pytest.fixture(autouse=True)
def _clean_selectors(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SELECTORS:
        monkeypatch.delenv(name, raising=False)


def test_local_compatibility_default_keeps_offline_demo_working() -> None:
    settings = Settings.load(CONFIG)

    assert settings.profile == "local"
    assert settings.identity_mode == "local-persona"
    assert settings.channel_mode == "standalone"
    settings.validate_deployment()


def test_local_compute_can_use_oidc_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    base = Settings.load(CONFIG)
    issuer = IssuerSettings(
        issuer="https://idp.test.example",
        tenant="demo-bank",
        client_id="doc1",
        client_secret_env="TEST_CLIENT_SECRET",
    )
    settings = replace(
        base,
        identity=replace(base.identity, mode="oidc-session", trusted_issuers=(issuer,)),
        channel=ChannelSettings(
            mode="standalone",
            public_origin="https://agent.test.example",
        ),
    )

    settings.validate_deployment()
    assert settings.profile == "local"
    assert settings.identity_mode == "oidc-session"


def test_live_never_infers_local_or_managed_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDD_PROFILE", "live")
    settings = Settings.load(CONFIG)

    with pytest.raises(ValueError, match="CDD_IDENTITY_PROFILE is required"):
        _ = settings.identity_mode


def test_historical_oidc_pseudo_runtime_has_exact_migration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile validation belongs at boot, not at the first request.

    Validating in ``validate_deployment``, which the API runs lazily, lets a process choose
    its CORS, persona-header and bind postures from a string nothing binds. Validation is
    part of resolving the profile, so ``load`` refuses.
    """
    monkeypatch.setenv("CDD_PROFILE", "oidc-session")

    with pytest.raises(
        ValueError,
        match=r"CDD_PROFILE=<local\|live\|gcp\|platform\|onprem>.*CDD_IDENTITY_PROFILE=oidc-session",
    ):
        Settings.load(CONFIG)


def test_unknown_identity_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDD_IDENTITY_PROFILE", "magic-header")
    settings = Settings.load(CONFIG)

    with pytest.raises(ValueError, match="unknown CDD_IDENTITY_PROFILE"):
        settings.validate_deployment()


def test_secure_deployment_requires_explicit_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDD_PROFILE", "gcp")
    settings = Settings.load(CONFIG)

    with pytest.raises(ValueError, match="CDD_CHANNEL_PROFILE is required"):
        settings.validate_deployment()


def test_reviewed_matrix_rejects_oidc_in_a_native_channel() -> None:
    base = Settings.load(CONFIG)
    settings = replace(
        base,
        identity=replace(base.identity, mode="oidc-session"),
        channel=replace(base.channel, mode="native"),
    )

    with pytest.raises(ValueError, match="unreviewed channel/identity combination"):
        settings.validate_deployment()


def test_access_token_mode_without_reviewed_policy_does_not_fall_back_to_iap() -> None:
    base = Settings.load(CONFIG)
    settings = replace(
        base,
        identity=replace(base.identity, mode="oauth-access-token"),
        channel=replace(base.channel, mode="native"),
    )

    with pytest.raises(ValueError, match="reviewed access-token issuer policy"):
        settings.validate_deployment()


def test_deprecated_control_ownership_cannot_disable_application_oidc() -> None:
    base = Settings.load(CONFIG)
    issuer = IssuerSettings(
        issuer="https://idp.test.example",
        tenant="demo-bank",
        client_id="doc1",
        client_secret_env="TEST_CLIENT_SECRET",
    )
    settings = replace(
        base,
        identity=replace(base.identity, mode="oidc-session", trusted_issuers=(issuer,)),
        channel=ChannelSettings(
            mode="standalone",
            public_origin="https://agent.test.example",
        ),
        deployment=replace(base.deployment, standalone=False),
    )

    with pytest.raises(ValueError, match="assigns identity control to the platform"):
        settings.validate_deployment()


def test_safe_configuration_hash_changes_with_selector_not_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = Settings.load(CONFIG)
    monkeypatch.setenv("CDD_SESSION_SIGNING_KEY", "must-never-be-hashed-or-returned")
    second = Settings.load(CONFIG)
    native = replace(second, channel=replace(second.channel, mode="native"))

    assert first.configuration_hash() == second.configuration_hash()
    assert second.configuration_hash() != native.configuration_hash()
    assert len(second.configuration_hash()) == 64


def test_identity_binding_is_separate_from_all_runtime_ports() -> None:
    settings = Settings.load(CONFIG)

    assert "identity" not in settings.adapters
    assert len(settings.adapters) == 20
    for bindings in settings.adapters.values():
        assert set(bindings) == {"local", "live", "gcp", "platform", "onprem"}
    assert set(settings.identity.bindings) == {
        "local-persona",
        "iap",
        "oidc-session",
        "oauth-access-token",
        "embedded-grant",
        "onprem",
    }


def test_validation_rejects_a_deleted_runtime_port_even_when_remaining_maps_are_complete() -> None:
    settings = Settings.load(CONFIG)
    incomplete = replace(
        settings,
        adapters={
            name: bindings for name, bindings in settings.adapters.items() if name != "audit"
        },
    )

    with pytest.raises(ValueError, match=r"exact required port set; missing: audit"):
        incomplete.validate_deployment()
