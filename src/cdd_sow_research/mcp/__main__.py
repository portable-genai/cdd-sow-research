"""Serve Doc1's governed tool catalog over MCP 2026-07-28 on stdio.

The actor is the audited caller and this transport verifies no end user, so it is read from the
environment and recorded as a SERVICE caller. There is deliberately no default that looks like a
person: an unset variable produces ``svc:unattributed``, which is honest and greppable in the
trail rather than a name that would make an unattributed call look attributed.

The read goes through ``setting_or_default`` so it keeps three states. An operator who EMPTIES
the variable has said something different from one who never set it, and inheriting the default
there would silently attribute their calls to the fallback identity.
"""

from __future__ import annotations

import sys

from hex_service_kit.mcpserve import run_stdio

from ..envread import setting_or_default
from .server import build_server


def main() -> int:
    actor = setting_or_default("CDD_MCP_ACTOR", "svc:unattributed")
    run_stdio(build_server(actor=actor))
    return 0


if __name__ == "__main__":
    sys.exit(main())
