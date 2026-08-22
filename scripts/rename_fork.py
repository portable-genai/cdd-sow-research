"""Rebrand this base repo for your institution / vertical in one pass.

Rewrites the package name, the CLI entry point, the ``CDD_`` env-var prefix, and the
baked-in resource ids across the tree, so a fork does not have to hunt them down by hand.
It is a mechanical text + directory rename, deliberately conservative: it prints a plan,
asks before writing (unless ``--yes``), and by default leaves docs prose alone (pass
``--include-docs`` to sweep Markdown too).

    # See what would change (no writes):
    python scripts/rename_fork.py --package acme_kyc_agent --cli acme-kyc \
        --env-prefix ACME --resource acme-kyc-agent --dry-run

    # Apply it:
    python scripts/rename_fork.py --package acme_kyc_agent --cli acme-kyc \
        --env-prefix ACME --resource acme-kyc-agent --yes

After running: recreate the venv and ``pip install -e ".[dev]"`` (the distribution name
changed), then run the gate. See docs/ADOPTING.md for the full checklist (fixtures,
sanctions snapshot, IdP wiring, region) that this script does NOT do for you.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_OLD_PACKAGE = "cdd_sow_research"
_OLD_CLI = "cdd-sow"
_OLD_ENV_PREFIX = "CDD_"
_OLD_RESOURCE = "cdd-sow-research"
_OLD_DIST = "cdd-sow-research"

# Directories never touched.
_SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    ".next",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
}
# File suffixes considered text (everything else is skipped).
_TEXT_SUFFIXES = {
    ".py",
    ".toml",
    ".yaml",
    ".yml",
    ".md",
    ".json",
    ".cfg",
    ".ini",
    ".txt",
    ".mjs",
    ".ts",
    ".tsx",
    ".lock",
    ".env",
    ".example",
    "",
}
# Docs prose is only swept with --include-docs (keep code deterministic by default).
_DOC_SUFFIXES = {".md"}


def _iter_files(include_docs: bool):
    for path in _ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(_ROOT).parts):
            continue
        if path.suffix not in _TEXT_SUFFIXES:
            continue
        if not include_docs and path.suffix in _DOC_SUFFIXES:
            continue
        yield path


def _replacements(args: argparse.Namespace) -> list[tuple[str, str]]:
    env_prefix = args.env_prefix.rstrip("_").upper() + "_"
    # Order matters: the distribution name contains the resource stem, so replace the
    # longer, more specific strings first.
    # The distribution name is the same token as the resource name, so replacing it bare
    # consumes every occurrence and leaves the entry below doing nothing: a --dist that
    # differs from --resource would silently rewrite the resource name too. Anchoring the
    # distribution on its pyproject declaration keeps the two independently meaningful.
    return [
        (f'name = "{_OLD_DIST}"', f'name = "{args.dist or args.resource}"'),
        (_OLD_PACKAGE, args.package),
        (_OLD_RESOURCE, args.resource),
        (_OLD_CLI, args.cli),
        (_OLD_ENV_PREFIX, env_prefix),
    ]


def _rewrite_text(text: str, repls: list[tuple[str, str]]) -> tuple[str, int]:
    count = 0
    for old, new in repls:
        # For the env prefix, only rewrite ``CDD_`` when it prefixes an UPPER token
        # (CDD_PROFILE), never mid-word.
        if old == _OLD_ENV_PREFIX:
            text, n = re.subn(rf"\b{re.escape(old)}(?=[A-Z0-9])", new, text)
        else:
            n = text.count(old)
            text = text.replace(old, new)
        count += n
    return text, count


def main() -> int:
    ap = argparse.ArgumentParser(description="Rebrand the base repo for a fork.")
    ap.add_argument("--package", required=True, help="new python package name (snake_case)")
    ap.add_argument("--cli", required=True, help="new CLI entry-point name (kebab-case)")
    ap.add_argument("--env-prefix", required=True, help="new env-var prefix, e.g. ACME")
    ap.add_argument("--resource", required=True, help="new resource id stem, e.g. acme-kyc-agent")
    ap.add_argument("--dist", default="", help="new distribution name (default --resource)")
    ap.add_argument("--include-docs", action="store_true", help="also rewrite Markdown prose")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    ap.add_argument("--yes", action="store_true", help="apply without the confirmation prompt")
    args = ap.parse_args()

    if not re.fullmatch(r"[a-z_][a-z0-9_]*", args.package):
        ap.error("--package must be a valid snake_case python identifier")

    # Write only when explicitly applying (--yes and not --dry-run); otherwise preview.
    apply = args.yes and not args.dry_run

    repls = _replacements(args)
    print("Planned replacements (whole-tree):")
    for old, new in repls:
        print(f"  {old!r:28} -> {new!r}")
    print()

    files = list(_iter_files(args.include_docs))
    touched: list[tuple[Path, int]] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new_text, n = _rewrite_text(text, repls)
        if n:
            touched.append((path, n))
            if apply:
                path.write_text(new_text, encoding="utf-8")

    total = sum(n for _, n in touched)
    verb = "Edited" if apply else "Would edit"
    print(f"{verb} {len(touched)} file(s), {total} replacement(s).")

    # Rename the package directory last (after content is rewritten).
    pkg_dir = _ROOT / "src" / _OLD_PACKAGE
    new_pkg_dir = _ROOT / "src" / args.package
    if pkg_dir.exists() and pkg_dir != new_pkg_dir:
        print(f"{'Renaming' if apply else 'Would rename'} {pkg_dir} -> {new_pkg_dir}")
        if apply:
            pkg_dir.rename(new_pkg_dir)

    if not apply:
        print("\nNo files were written. Re-run with --yes to apply (or --dry-run to preview).")
        return 0
    print('\nDone. Next: recreate the venv, `pip install -e ".[dev]"`, and run the gate.')
    print("Then finish the human steps in docs/ADOPTING.md (fixtures, sanctions, IdP, region).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
