#!/usr/bin/env python3
"""Presenter-paced walkthrough of the real standalone laptop UI.

The runner attaches to an existing UI/API stack or starts isolated local processes itself.
Presenter notes stay in this terminal; the browser shows only the application.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui"
DEFAULT_BASE_URL = "http://127.0.0.1:3000/agent/"
DEFAULT_API_URL = "http://127.0.0.1:8090"

PageAction = Callable[[Any, str], None]
_artifact_path: Path | None = None


@dataclass(frozen=True, slots=True)
class Step:
    id: str
    title: str
    presenter_notes: str
    action: PageAction


def _open(page: Any, base_url: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded")
    page.get_by_role("heading", name="Deployment capabilities").wait_for()
    page.get_by_text("Functional laptop demo", exact=True).wait_for()
    if page.get_by_text("Unavailable here", exact=True).count() < 3:
        raise AssertionError("managed capability limitations are not visibly disclosed")


def _ensure_dossier(page: Any, base_url: str) -> None:
    if not page.url.startswith(base_url.rstrip("/")):
        _open(page, base_url)
    subject_heading = page.get_by_role("heading", name="Subject — Acme Holdings Pte Ltd")
    if subject_heading.count() and subject_heading.is_visible():
        return
    page.get_by_placeholder("Legal name of the company or person").fill("Acme Holdings Pte Ltd")
    page.get_by_role("button", name="Build CDD dossier").click()
    subject_heading.wait_for(timeout=30_000)
    page.get_by_text("HUMAN REVIEW REQUIRED", exact=False).wait_for()


def _build_dossier(page: Any, base_url: str) -> None:
    _ensure_dossier(page, base_url)
    page.get_by_role("heading", name="Source of wealth").scroll_into_view_if_needed()


def _export_reload(page: Any, base_url: str) -> None:
    global _artifact_path
    _ensure_dossier(page, base_url)
    with page.expect_download() as pending:
        page.get_by_role("button", name="Export open dossier").click()
    download = pending.value
    destination = Path(tempfile.gettempdir()) / download.suggested_filename
    download.save_as(destination)
    _artifact_path = destination

    page.get_by_placeholder("Legal name of the company or person").fill("Awaiting portable reload")
    page.locator('input[type="file"][accept*="json"]').set_input_files(str(destination))
    page.get_by_role("heading", name="Subject — Acme Holdings Pte Ltd").wait_for()
    page.get_by_text("cdd-dossier/v1", exact=False).wait_for()


STEPS: tuple[Step, ...] = (
    Step(
        id="capabilities",
        title="Open the honest laptop deployment",
        presenter_notes=(
            "The compliance analyst opens the due-diligence workspace on a laptop and sees "
            "exactly which controls are operating in this profile. The core workflow and local "
            "evaluation are functional, while managed audit, observability, and Model Armor are "
            "clearly unavailable and the deployment is not presented as production attested."
        ),
        action=_open,
    ),
    Step(
        id="dossier",
        title="Build a cited dossier offline",
        presenter_notes=(
            "The analyst names the customer and builds a complete customer due-diligence dossier "
            "from the evidence available to the local profile. The workspace shows source of "
            "wealth, risk, screening, ownership, citations, and a maker-checker warning, so the "
            "business workflow remains useful without relying on managed cloud assurance."
        ),
        action=_build_dossier,
    ),
    Step(
        id="portable-record",
        title="Export and reload the governed record",
        presenter_notes=(
            "The analyst exports the completed dossier in a versioned open JSON envelope and "
            "reloads it through the application integrity check. This bounded demonstration shows "
            "that the logical case record can move between deployments while its structure, cited "
            "evidence, human-review status, and SHA-256 identity remain stable."
        ),
        action=_export_reload,
    ),
)


def selected_steps(from_step: str | None = None) -> tuple[Step, ...]:
    if from_step is None:
        return STEPS
    for index, step in enumerate(STEPS):
        if step.id == from_step:
            return STEPS[index:]
    choices = ", ".join(step.id for step in STEPS)
    raise ValueError(f"unknown step {from_step!r}; choose one of: {choices}")


def print_script(steps: Sequence[Step]) -> None:
    for number, step in enumerate(steps, start=1):
        print(f"{number:02d}. {step.id}: {step.title}")
        print(f"    Notes: {step.presenter_notes}")


def _ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _wait_ready(url: str, process: subprocess.Popen[Any], name: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _ready(url):
            return
        if process.poll() is not None:
            raise RuntimeError(f"{name} exited before readiness")
        time.sleep(0.2)
    raise RuntimeError(f"{name} did not become ready at {url}")


def _terminate(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _start_stack(
    base_url: str, api_url: str, state_dir: Path
) -> tuple[subprocess.Popen[Any] | None, subprocess.Popen[Any] | None]:
    api_process: subprocess.Popen[Any] | None = None
    ui_process: subprocess.Popen[Any] | None = None
    api_health = f"{api_url.rstrip('/')}/healthz"
    if not _ready(api_health):
        api = urlsplit(api_url)
        api_env = {
            **os.environ,
            "CDD_PROFILE": "local",
            "CDD_IDENTITY_PROFILE": "local-persona",
            "CDD_CHANNEL_PROFILE": "standalone",
            "CDD_PUBLIC_ORIGIN": f"{urlsplit(base_url).scheme}://{urlsplit(base_url).netloc}",
            "CDD_LOCAL_DB": str(state_dir / "knowledge.db"),
            "CDD_LOCAL_AUDIT": str(state_dir / "audit.db"),
            "CDD_LOCAL_DOCUMENTS": str(state_dir / "documents.db"),
        }
        api_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "cdd_sow_research.api.app:app",
                "--host",
                api.hostname or "127.0.0.1",
                "--port",
                str(api.port or 8090),
            ],
            cwd=ROOT,
            env=api_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_ready(api_health, api_process, "Doc1 API")

    if not _ready(base_url):
        parsed = urlsplit(base_url)
        ui_env = {**os.environ, "CDD_API_INTERNAL_ORIGIN": api_url}
        ui_process = subprocess.Popen(
            [
                "npm",
                "run",
                "dev",
                "--",
                "--hostname",
                parsed.hostname or "127.0.0.1",
                "--port",
                str(parsed.port or 3000),
            ],
            cwd=UI,
            env=ui_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_ready(base_url, ui_process, "Doc1 UI")
    return api_process, ui_process


def run(
    steps: Sequence[Step],
    *,
    base_url: str,
    api_url: str,
    slow_mo: int,
    pause: bool,
    screenshots: Path | None,
    headless: bool,
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "install the demo extra and browser: pip install -e '.[demo]' && "
            "playwright install chromium"
        ) from error

    if screenshots is not None:
        screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="doc1-laptop-demo-") as directory:
        api_process: subprocess.Popen[Any] | None = None
        ui_process: subprocess.Popen[Any] | None = None
        try:
            api_process, ui_process = _start_stack(base_url, api_url, Path(directory))
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=headless,
                    slow_mo=slow_mo,
                )
                context = browser.new_context(
                    viewport={"width": 1440, "height": 1000},
                    accept_downloads=True,
                )
                try:
                    page = context.new_page()
                    page.set_default_timeout(30_000)
                    for number, step in enumerate(steps, start=1):
                        print(f"\n{'=' * 72}\nSTEP {number:02d}: {step.title}\nID: {step.id}\n")
                        print(
                            f"PRESENTER NOTES: {step.presenter_notes}",
                            flush=True,
                        )
                        step.action(page, base_url)
                        if screenshots is not None:
                            page.screenshot(
                                path=str(screenshots / f"{number:02d}-{step.id}.png"),
                                full_page=True,
                            )
                        if pause:
                            input("Enter for next step...")
                finally:
                    context.close()
                    browser.close()
        finally:
            _terminate(ui_process)
            _terminate(api_process)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--base-url", default=DEFAULT_BASE_URL)
    result.add_argument("--api-url", default=DEFAULT_API_URL)
    result.add_argument("--from", dest="from_step", metavar="STEP-ID")
    result.add_argument("--slow-mo", type=int, default=0, metavar="MS")
    result.add_argument("--list", action="store_true")
    result.add_argument("--no-pause", action="store_true")
    result.add_argument("--headless", action="store_true")
    result.add_argument("--screenshots", type=Path, metavar="DIR")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        steps = selected_steps(args.from_step)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    if args.slow_mo < 0:
        print("--slow-mo must be non-negative", file=sys.stderr)
        return 2
    if args.list:
        print_script(steps)
        return 0
    try:
        run(
            steps,
            base_url=args.base_url,
            api_url=args.api_url,
            slow_mo=args.slow_mo,
            pause=not args.no_pause,
            screenshots=args.screenshots,
            headless=args.headless,
        )
    except (RuntimeError, AssertionError, KeyboardInterrupt) as error:
        print(f"laptop demo failed: {error}", file=sys.stderr)
        return 1
    finally:
        if _artifact_path is not None:
            _artifact_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
