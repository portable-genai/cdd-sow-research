"""EVERY profile builds in an interpreter where the cloud SDKs cannot be imported.

Not "no SDK installed on this machine": ``tests/contract/_sdk_free_probe.py`` installs a
meta-path finder that refuses the ``google`` and ``vertexai`` roots, then constructs every
adapter binding of the profile in a fresh process. Proving the claim by absence would tie it to
the developer machine: one with the SDK installed would pass while hiding an eager import that
breaks the offline gate everywhere else.

Every profile, INCLUDING ``gcp``, and that is the point rather than an overreach. The managed
adapters import their SDK lazily, inside the method that needs it, so they CONSTRUCT with no SDK
present; that laziness is what lets a fork run the offline stack without installing a cloud SDK
at all, and it is listed as a hard constraint in AGENTS.md.

Nothing enforced it. The parity and portability suites construct only the SDK-free profiles, so
they never import the managed adapter modules, and a ``from google.cloud import logging`` hoisted
to module scope in one of them passed the whole suite: every test green, the constraint broken,
and the breakage invisible until an offline machine tried to serve. Constructing the managed
profiles under the same prohibition is what closes that hole, and it was proved by planting
exactly that hoisted import and watching only the ``gcp`` case go red.

The parity suite next door proves the bindings satisfy their Protocols in-process; this file
proves every one of them imports and constructs under prohibition.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.contract.test_port_parity import PORT_PROTOCOLS, SDK_FREE_PROFILES, _settings

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Every profile the settings file binds, discovered rather than restated, so a profile added
#: to the binding table is proved from the moment it exists instead of when somebody remembers
#: this list. A hand-written tuple here is how ``platform`` would go unproved for a release.
ALL_PROFILES = tuple(sorted(set().union(*(_settings("local").adapters[p] for p in PORT_PROTOCOLS))))


def _run_probe(argument: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tests.contract._sdk_free_probe", argument],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": "src"},
        check=False,
        timeout=300,
    )


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_the_profile_constructs_with_the_cloud_sdks_blocked(profile: str) -> None:
    completed = _run_probe(profile)
    assert completed.returncode == 0, (
        f"the {profile} profile could not be built with the cloud SDKs blocked:\n"
        f"{completed.stdout}\n{completed.stderr}"
    )
    assert "no cloud SDK importable" in completed.stdout


def test_the_sdk_free_profiles_are_among_the_profiles_proved() -> None:
    """The discovered set must still cover the ones the parity suite calls SDK-free.

    Discovery is worth more than a hand-written list only while it keeps finding at least what
    the list held. A settings file that lost a binding would otherwise shrink this suite in
    silence, and a suite that quietly proves less is the failure this whole file exists about.
    """
    missing = set(SDK_FREE_PROFILES) - set(ALL_PROFILES)
    assert not missing, f"profiles the parity suite proves SDK-free are unbound here: {missing}"
    assert len(ALL_PROFILES) >= len(SDK_FREE_PROFILES)


def test_the_blocker_still_blocks() -> None:
    """A probe whose blocker quietly stopped blocking would make every proof above vacuous."""
    completed = _run_probe("--self-test")
    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    assert "blocker refused google.auth" in completed.stdout
