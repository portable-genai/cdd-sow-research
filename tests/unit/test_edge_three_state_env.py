"""Every security-relevant edge variable resolves in THREE states, never two.

Unset, set-and-empty, and set-and-valid are three different answers. Collapsing the first
two is how a fail-closed control fails open: the operator who blanked a variable expressed
an intent, and inheriting the unset default silently ignores it.

Two edge variables are covered here.

``CDD_FRAME_ANCESTORS`` is the clickjacking control. Blanked, a two-state read builds an empty
``frame_ancestors`` tuple, and the middleware then emits the header ``Content-Security-Policy:
frame-ancestors`` with no directive value. Browsers treat a valueless directive as a parse error and
discard it, and the ``== ("'self'",)`` branch that sets ``X-Frame-Options`` is skipped at the same
time, so BOTH clickjacking headers vanish at once and the page becomes framable by anyone.
cdd-sow-research refuses the whole boot instead: the value is validated inside
``WebSecuritySettings``, the type the middleware reads from, so no code path can construct a service
whose CSP is empty.

``CDD_API_HOST`` is the bind guard. hex-service-kit makes a present-but-empty value
raise ``ConfiguredEmptyError`` rather than inherit the profile default, which on a secure
profile meant binding every interface on a value nobody chose. ``main()`` must convert that
to a SystemExit carrying the guard's message, the same way it already handles the loopback
refusal, so an operator sees the explanation and not a traceback.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hex_service_kit.netdefaults import ConfiguredEmptyError

from cdd_sow_research.api import app as app_module
from cdd_sow_research.config import Settings, WebSecuritySettings

_BLANK_VALUES = ("", "   ", "\n", "\t ")


@pytest.mark.parametrize("blank", _BLANK_VALUES)
def test_a_blanked_frame_ancestors_refuses_the_boot(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """Set-and-empty must refuse, not build a service with an empty CSP directive."""
    monkeypatch.setenv("CDD_PROFILE", "local")
    monkeypatch.setenv("CDD_FRAME_ANCESTORS", blank)
    with pytest.raises(ValueError, match="frame_ancestors must not be empty"):
        Settings.load(Path("config/settings.yaml"))


def test_an_unset_frame_ancestors_keeps_the_shipped_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset expresses no intent, so the shipped restrictive default stands."""
    monkeypatch.setenv("CDD_PROFILE", "local")
    monkeypatch.delenv("CDD_FRAME_ANCESTORS", raising=False)
    settings = Settings.load(Path("config/settings.yaml"))
    assert settings.web.frame_ancestors == ("'self'",)


def test_a_named_parent_origin_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set-and-valid is used verbatim, space separated as CSP spells it."""
    monkeypatch.setenv("CDD_PROFILE", "local")
    monkeypatch.setenv("CDD_FRAME_ANCESTORS", "https://parent.example https://portal.example")
    settings = Settings.load(Path("config/settings.yaml"))
    assert settings.web.frame_ancestors == ("https://parent.example", "https://portal.example")


def test_the_empty_csp_directive_is_unconstructable() -> None:
    """The refusal lives in the type the middleware reads, so no caller can route around it.

    Guarding only the environment read would leave the defect reachable through any other
    way of building settings; the middleware formats whatever tuple it is handed.
    """
    with pytest.raises(ValueError, match="frame_ancestors must not be empty"):
        WebSecuritySettings(frame_ancestors=())


@pytest.mark.parametrize(
    "entry",
    ["*", "'*'", "null", "*.*", "https://*.example", "*.example", "https://*"],
)
def test_a_wildcard_origin_refuses_the_boot_in_either_list(entry: str) -> None:
    """Every spelling, on both lists, because a membership test only catches the bare one.

    A check of ``"*" in self.cors_origins`` is a membership test over a tuple, so it sees an
    entry that IS an asterisk and not one that CONTAINS one. ``https://*.example`` therefore
    reaches the origin validator, which finds a well-formed https origin with an authority and
    no path and accepts it. CSP honours that host-source form, so every subdomain could frame
    the console, including one obtained by takeover or serving user content.

    The other spellings would be refused incidentally, by the origin validator rather than by
    the wildcard rule. Incidental coverage moves when the validator does, so they are pinned
    here against the rule that is supposed to own them.
    """
    with pytest.raises(ValueError, match="wildcard"):
        WebSecuritySettings(frame_ancestors=(entry,))
    with pytest.raises(ValueError, match="wildcard"):
        WebSecuritySettings(cors_origins=(entry,))


def test_the_wildcard_refusal_leaves_a_legitimate_allowlist_alone() -> None:
    """A refusal that also turns away valid configuration is an outage, not a control."""
    settings = WebSecuritySettings(
        frame_ancestors=("https://portal.demo-bank.example",),
        cors_origins=("https://console.demo-bank.example",),
    )
    assert settings.frame_ancestors == ("https://portal.demo-bank.example",)
    assert settings.cors_origins == ("https://console.demo-bank.example",)
    assert WebSecuritySettings().frame_ancestors == ("'self'",)


def test_a_populated_frame_ancestors_still_emits_both_clickjacking_headers() -> None:
    """Green side: the default posture sets a valued CSP directive AND X-Frame-Options."""

    class _Response:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

    settings = Settings.load(Path("config/settings.yaml"))
    response = _Response()
    app_module._DeploymentSecurityMiddleware._security_headers(response, settings)  # type: ignore[arg-type]
    assert response.headers["Content-Security-Policy"] == "frame-ancestors 'self'"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"


@pytest.mark.parametrize("blank", _BLANK_VALUES)
def test_a_blanked_api_host_exits_with_the_guard_message(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """A present-but-empty bind host is an operator error, reported as such, not a traceback."""
    monkeypatch.setenv("CDD_PROFILE", "local")
    monkeypatch.setenv("CDD_API_HOST", blank)

    def _never_runs(*args: object, **kwargs: object) -> None:  # pragma: no cover - must not run
        raise AssertionError("uvicorn must not start when the bind host is refused")

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", _never_runs)
    with pytest.raises(SystemExit) as exit_info:
        app_module.main()
    assert "CDD_API_HOST" in str(exit_info.value)


def test_the_bind_guard_refusal_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red before the widened except: ConfiguredEmptyError escaped main() uncaught."""
    monkeypatch.setenv("CDD_API_HOST", "")
    from hex_service_kit.netdefaults import resolve_bind_host

    with pytest.raises(ConfiguredEmptyError):
        resolve_bind_host(
            "local",
            host_env="CDD_API_HOST",
            insecure_demo_env="CDD_ALLOW_INSECURE_DEMO",
        )
