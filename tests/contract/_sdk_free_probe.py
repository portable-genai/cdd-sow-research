"""Construct every adapter of one profile in a FRESH interpreter with the cloud SDKs blocked.

Run as ``python -m tests.contract._sdk_free_probe <profile>`` from the repo root (with ``src``
on ``PYTHONPATH``); exits 0 when every port binding of that profile imported and constructed,
non-zero naming the offence otherwise. ``--self-test`` proves the blocker itself still refuses,
because a probe whose blocker quietly stopped blocking would pass on any machine and prove
nothing.

Why a subprocess rather than a fixture that unloads modules in place: reloading rebinds the
adapter classes, so every already-imported test module would keep stale class objects and
``isinstance`` checks elsewhere in the suite would start failing for reasons that have nothing
to do with the code under test. A fresh process also proves the stronger claim: a whole
interpreter in which the SDK was never importable, which is the difference between "no SDK
installed on this machine" and "cannot be imported".

The profile list and the ephemeral-settings shape are IMPORTED from the parity suite rather
than restated here. Both files must build the same settings for the two proofs to be about the
same thing, and a second copy of a bespoke constructor is a copy that drifts.
"""

from __future__ import annotations

import importlib
import sys

#: Import roots the SDK-free profiles must build without. ``google`` covers every
#: ``google-cloud-*`` distribution plus ``google-genai`` and ``google-adk``; ``vertexai``
#: ships as its own root.
BLOCKED_ROOTS = ("google", "vertexai")


class _BlockedSdkFinder:
    """A meta-path finder that refuses the blocked roots, whatever is installed."""

    def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
        if any(fullname == root or fullname.startswith(root + ".") for root in BLOCKED_ROOTS):
            raise ModuleNotFoundError(
                f"{fullname} is blocked: this profile must construct with no cloud SDK"
            )
        return None


def _install_blocker() -> None:
    evicted = [
        name
        for name in sys.modules
        if any(name == root or name.startswith(root + ".") for root in BLOCKED_ROOTS)
    ]
    for name in evicted:
        del sys.modules[name]
    sys.meta_path.insert(0, _BlockedSdkFinder())  # type: ignore[arg-type]


def _self_test() -> int:
    """The blocker must refuse, and refuse for the right reason."""
    _install_blocker()
    try:
        importlib.import_module("google.auth")
    except ModuleNotFoundError as exc:
        if "blocked" in str(exc):
            print("blocker refused google.auth")
            return 0
        print(f"google.auth failed, but not because of the blocker: {exc}", file=sys.stderr)
        return 1
    print("google.auth imported despite the blocker", file=sys.stderr)
    return 1


def main(profile: str) -> int:
    _install_blocker()

    # Imported AFTER the blocker is installed, so an eager SDK import anywhere on the
    # construction path, config.py included, is caught rather than arriving pre-loaded.
    from tests.contract.test_port_parity import _settings, instantiate

    settings = _settings(profile)
    constructed = 0
    for port_name in sorted(settings.adapters):
        dotted = settings.adapters[port_name].get(profile)
        if dotted is None:
            print(f"port {port_name} has no {profile} binding", file=sys.stderr)
            return 1
        instantiate(dotted, settings)
        constructed += 1
    if not constructed:
        print(
            "settings.adapters bound no ports; a build of nothing proves nothing",
            file=sys.stderr,
        )
        return 1
    print(f"{profile}: constructed {constructed} adapters with no cloud SDK importable")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(
            "usage: python -m tests.contract._sdk_free_probe <profile>|--self-test",
            file=sys.stderr,
        )
        raise SystemExit(2)
    raise SystemExit(_self_test() if sys.argv[1] == "--self-test" else main(sys.argv[1]))
