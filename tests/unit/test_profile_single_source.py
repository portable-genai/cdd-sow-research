"""The profile has ONE source of truth, and it fails closed on an unset variable.

The standing gate for the absence-read-as-consent class, mirroring human-review-console
(``human-review-console/tests/test_profile_single_source.py``). Guarding this fail-open in the
identity adapter alone leaves another module re-deriving the same decision with its own raw
fallback, which is how a write path stays open. A drift guard is therefore part of the defence,
not a nicety: any module that reads
``CDD_PROFILE`` directly can reintroduce the whole class, so only ``config.resolve_profile``
may read it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cdd_sow_research.config import (
    IDENTITY_MODES,
    RUNTIME_PROFILES,
    UNCONSENTED_IDENTITY_MODE,
    IdentitySettings,
    Settings,
    resolve_profile,
)
from cdd_sow_research.envread import ConfiguredEmptyError

_SRC = Path(__file__).resolve().parents[2] / "src" / "cdd_sow_research"
_CONFIG = _SRC / "config.py"


def _python_sources() -> list[Path]:
    return sorted(p for p in _SRC.rglob("*.py") if p != _CONFIG)


def test_only_the_resolver_reads_the_runtime_profile_variable_from_the_environment() -> None:
    offenders = []
    for path in _python_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if re.search(r"(os\.environ|os\.getenv)[^\n]*CDD_PROFILE", line):
                offenders.append(f"{path.relative_to(_SRC)}:{number}: {line.strip()}")
    assert not offenders, (
        "these modules re-derive the profile instead of calling config.resolve_profile, "
        "so an unset CDD_PROFILE can again be read as consent:\n" + "\n".join(offenders)
    )


def test_the_scan_would_actually_fail_on_a_reintroduced_permissive_default() -> None:
    """The guard is only worth having if it fires; prove the pattern it looks for."""
    offending = 'profile = os.environ.get("CDD_PROFILE", "local")'
    assert re.search(r"(os\.environ|os\.getenv)[^\n]*CDD_PROFILE", offending)


def test_the_resolver_treats_only_an_absent_variable_as_no_choice() -> None:
    choice = resolve_profile({})
    assert choice.explicit is False


@pytest.mark.parametrize("value", ["", "   "])
def test_a_configured_empty_profile_refuses_instead_of_inheriting_local(value: str) -> None:
    with pytest.raises(ConfiguredEmptyError, match="CDD_PROFILE"):
        resolve_profile({"CDD_PROFILE": value})


def test_a_deliberate_profile_is_carried_through_unchanged() -> None:
    for name in sorted(RUNTIME_PROFILES):
        choice = resolve_profile({"CDD_PROFILE": name})
        assert (choice.profile, choice.explicit) == (name, True)


def test_the_settings_file_key_counts_as_a_deliberate_choice() -> None:
    choice = resolve_profile({}, file_profile="gcp")
    assert (choice.profile, choice.explicit) == ("gcp", True)
    # The environment still wins over the file when both name a profile.
    assert resolve_profile({"CDD_PROFILE": "local"}, file_profile="gcp").profile == "local"


@pytest.mark.parametrize("value", ["bogus", "Local", "GCP", "LOCAL", "local "])
def test_an_unknown_or_mis_capitalised_profile_is_refused_at_resolution(value: str) -> None:
    """A typo must be a boot failure, not a profile that matches no posture at all."""
    if value.strip() in RUNTIME_PROFILES:
        pytest.skip("whitespace-only difference is normalised, not a typo")
    with pytest.raises(ValueError, match="CDD_PROFILE"):
        resolve_profile({"CDD_PROFILE": value})


def test_an_unconsented_run_is_not_local_persona_for_any_relaxation() -> None:
    unconsented = Settings(profile="local", profile_explicit=False)
    assert unconsented.identity_mode == "local-persona"
    assert unconsented.exposure_identity_mode == UNCONSENTED_IDENTITY_MODE
    assert UNCONSENTED_IDENTITY_MODE not in IDENTITY_MODES


def test_an_unconsented_run_still_looks_local_to_the_bind_restriction() -> None:
    """The two decisions fail closed in OPPOSITE directions, so one string cannot serve both."""
    from cdd_sow_research.api.app import _security_profile

    assert _security_profile(Settings(profile="local", profile_explicit=False)) == "local"


def test_a_deliberate_local_run_keeps_every_relaxation() -> None:
    chosen = Settings(profile="local", profile_explicit=True)
    assert chosen.exposure_identity_mode == "local-persona"


def test_a_deliberately_named_identity_mode_is_a_choice_even_without_a_profile() -> None:
    named = Settings(
        profile="local",
        profile_explicit=False,
        identity=IdentitySettings(mode="local-persona"),
    )
    assert named.identity_mode_explicit is True
    assert named.exposure_identity_mode == "local-persona"
