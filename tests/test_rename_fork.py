"""Guard the fork-rename script's distribution / resource split.

``scripts/rename_fork.py`` is adopter tooling rather than shipped source, so it is not
importable as a package module; it is loaded here by path, the way an adopter runs it.
"""

from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path

_SCRIPT = Path(__file__).parents[1] / "scripts" / "rename_fork.py"
_SPEC = importlib.util.spec_from_file_location("rename_fork", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_a_distribution_name_differing_from_the_resource_leaves_the_resource_alone() -> None:
    """They are the same token, so only the anchored form can tell them apart.

    Unanchored, the distribution replacement consumes every occurrence and the resource
    name silently becomes the distribution name. This proves that is absent, rather than
    believed.
    """
    args = Namespace(
        package="acme_kyc_agent",
        cli="acme-kyc",
        env_prefix="ACME",
        resource="acme-kyc-agent",
        dist="acme-fork-dist",
    )

    rewritten, _ = _MODULE._rewrite_text(
        f'{_MODULE._OLD_RESOURCE} name = "{_MODULE._OLD_DIST}"',
        _MODULE._replacements(args),
    )

    assert rewritten == 'acme-kyc-agent name = "acme-fork-dist"'
