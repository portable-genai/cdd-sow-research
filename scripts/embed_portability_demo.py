#!/usr/bin/env python3
"""Presenter-paced executable evidence for cdd-sow-research channel portability.

The runner starts or reuses a production Next build plus two registered host origins, then
executes the same immutable loader and UI through each host. Presenter narration is printed only
to this terminal. The default browser is visible; CI uses HEADLESS=1 and --no-pause.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui"
HOSTS = ROOT / "tests" / "embed_hosts"
BROWSER_FIXTURES = ROOT / "tests" / "browser"
AGENT_ORIGIN = "http://127.0.0.1:3200"
UI_ORIGIN = "http://127.0.0.1:3201"
MODE5_AGENT_ORIGIN = "http://127.0.0.1:3210"
MODE5_UI_ORIGIN = "http://127.0.0.1:3211"
HOST_A = "http://127.0.0.1:4101"
HOST_B = "http://127.0.0.1:4102"
UNREGISTERED_HOST = "http://127.0.0.1:4103"
STANDALONE = "http://127.0.0.1:3300"
LOADER_PATH = "/agent/embed/v1/cdd-agent.js"


@dataclass(frozen=True, slots=True)
class Step:
    id: str
    title: str
    presenter_notes: str
    action: Callable[[Any, RunEvidence], None]


@dataclass(slots=True)
class RunEvidence:
    browser: str
    loader_integrity: str
    loader_sha256: str
    completed_steps: list[str] = field(default_factory=list)
    observations: dict[str, Any] = field(default_factory=dict)


class DimensionFailure(AssertionError):
    def __init__(self, dimension: str, message: str) -> None:
        super().__init__(message)
        self.dimension = dimension


def _agent_frame(page: Any, installation_id: str) -> Any:
    expected = f"/agent/embed/{installation_id}"
    frames = [frame for frame in page.frames if expected in frame.url]
    if len(frames) != 1:
        raise AssertionError(f"expected one {installation_id} agent frame, found {len(frames)}")
    return frames[0]


def _open_registered_host(page: Any, evidence: RunEvidence, origin: str, installation: str) -> None:
    page.goto(origin, wait_until="domcontentloaded")
    page.get_by_role("status").filter(has_text="cdd-sow-research ready").wait_for(state="visible")
    frame = _agent_frame(page, installation)
    frame.get_by_text("Assess a subject", exact=True).wait_for(state="visible")
    visible_digest = page.locator("#artifact-digest").inner_text()
    if visible_digest != evidence.loader_integrity:
        raise AssertionError("host-visible loader integrity does not match fetched artifact")
    evidence.observations[installation] = {
        "parent_origin": origin,
        "frame_origin": urllib.parse.urlsplit(frame.url).netloc,
        "loader_integrity": visible_digest,
    }


def open_host_a(page: Any, evidence: RunEvidence) -> None:
    _open_registered_host(page, evidence, HOST_A, "inst_host_a")


def open_host_b(page: Any, evidence: RunEvidence) -> None:
    _open_registered_host(page, evidence, HOST_B, "inst_host_b")


def compare_artifact(page: Any, evidence: RunEvidence) -> None:
    values: list[str] = []
    page.goto(HOST_A, wait_until="domcontentloaded")
    page.get_by_role("status").filter(has_text="cdd-sow-research ready").wait_for(state="visible")
    _agent_frame(page, "inst_host_a").get_by_text("Assess a subject", exact=True).wait_for(
        state="visible"
    )
    values.append(page.locator("#artifact-digest").inner_text())
    other = page.context.new_page()
    try:
        other.goto(HOST_B, wait_until="domcontentloaded")
        other.get_by_role("status").filter(has_text="cdd-sow-research ready").wait_for(
            state="visible"
        )
        _agent_frame(other, "inst_host_b").get_by_text("Assess a subject", exact=True).wait_for(
            state="visible"
        )
        values.append(other.locator("#artifact-digest").inner_text())
    finally:
        other.close()
        page.bring_to_front()
    if values != [evidence.loader_integrity, evidence.loader_integrity]:
        raise AssertionError("registered hosts did not display the same immutable loader digest")
    page.goto(f"{HOST_A}/comparison.html", wait_until="domcontentloaded")
    page.get_by_role("status").filter(
        has_text="PASS: loader bytes and fixed agent surface match across both hosts"
    ).wait_for(state="visible")
    evidence.observations["same_artifact"] = {
        "sha384_sri": evidence.loader_integrity,
        "sha256": evidence.loader_sha256,
        "host_count": 2,
    }


def reject_global_credential(page: Any, evidence: RunEvidence) -> None:
    page.goto(f"{HOST_A}/invalid-handshake.html", wait_until="domcontentloaded")
    probe = page.frame_locator("#probe")
    probe.get_by_text("Establishing the registered host channel…", exact=True).wait_for(
        state="visible"
    )
    button = page.get_by_role("button", name="Test global credential rejection")
    if button.count() != 1:
        raise AssertionError("expected exactly one channel-boundary action")
    button.click()
    page.get_by_role("status").filter(has_text="PASS: invalid global credential rejected").wait_for(
        state="visible"
    )
    probe.get_by_text("Establishing the registered host channel…", exact=True).wait_for(
        state="visible"
    )
    evidence.observations["global_credential"] = "rejected_without_channel"


def deny_unregistered_parent(page: Any, evidence: RunEvidence) -> None:
    page.goto(UNREGISTERED_HOST, wait_until="domcontentloaded")
    page.get_by_role("status").filter(
        has_text="Embedding denied; registered standalone fallback available"
    ).wait_for(state="visible", timeout=15_000)
    fallback = page.get_by_role("link", name="Open registered standalone channel")
    if fallback.count() != 1:
        raise AssertionError("expected exactly one host-owned fallback action")
    fallback.click()
    page.get_by_role("heading", name="Standalone sign-in boundary").wait_for(state="visible")
    if not page.url.startswith(f"{STANDALONE}/agent"):
        raise AssertionError("fallback did not reach the manifest-owned standalone origin")
    evidence.observations["fallback"] = {
        "denied_parent": UNREGISTERED_HOST,
        "standalone_origin": STANDALONE,
    }


STEPS: tuple[Step, ...] = (
    Step(
        id="host-a",
        title="Open cdd-sow-research in the relationship-manager portal",
        presenter_notes=(
            "The relationship manager opens due-diligence work inside the institution portal, "
            "keeping the customer journey in one familiar channel. cdd-sow-research responds "
            "inside the "
            "registered host while its governed evidence workflow remains on the dedicated agent "
            "origin."
        ),
        action=open_host_a,
    ),
    Step(
        id="host-b",
        title="Move the same console to a second host",
        presenter_notes=(
            "The compliance analyst opens the same due-diligence console from a different "
            "institution application. The host channel changes, while the fixed agent surface "
            "and its evidence-oriented workflow remain unchanged; this visibly proves channel "
            "portability, not portability of every runtime or data component."
        ),
        action=open_host_b,
    ),
    Step(
        id="same-artifact",
        title="Verify one immutable artifact across both hosts",
        presenter_notes=(
            "The platform owner compares the artifact evidence shown by both registered portals "
            "before approving the integration. Both channels load the same versioned asset and "
            "fixed agent surface, giving audit teams a reproducible channel-deployment record "
            "without claiming an infrastructure or model-stack swap."
        ),
        action=compare_artifact,
    ),
    Step(
        id="handshake-boundary",
        title="Reject credentials outside the negotiated channel",
        presenter_notes=(
            "The security reviewer tests whether a host can place a credential in the public "
            "browser message used to start the integration. cdd-sow-research rejects that message "
            "and leaves "
            "the protected channel unopened, preserving the boundary required before verified "
            "institutional identity can be accepted."
        ),
        action=reject_global_credential,
    ),
    Step(
        id="fallback",
        title="Deny an unregistered host and offer the reviewed fallback",
        presenter_notes=(
            "The security reviewer opens cdd-sow-research from a portal that is not registered for "
            "this "
            "installation. Framing is denied, and the host presents the reviewed standalone "
            "destination instead; the visible result proves parent-origin policy and safe channel "
            "fallback, while institutional sign-in remains a separate identity proof."
        ),
        action=deny_unregistered_parent,
    ),
)


def _identity_action_requires_harness(_page: Any, _evidence: RunEvidence) -> None:
    raise RuntimeError("identity evidence step was not bound to its production harness")


IDENTITY_STEPS: tuple[Step, ...] = (
    Step(
        id="identity-mode4",
        title="Verify direct institutional access-token identity",
        presenter_notes=(
            "The identity reviewer validates direct institutional access for both registered "
            "portals using short-lived tokens from independently configured RSA and EC issuers. "
            "The same protected transport carries structured, multipart, and document responses, "
            "while rotation succeeds and tenant, installation, origin, and token-type confusion "
            "are rejected without exposing credentials."
        ),
        action=_identity_action_requires_harness,
    ),
    Step(
        id="identity-mode5",
        title="Verify the BFF-authorized embedded identity grant",
        presenter_notes=(
            "The host records an explicit user intent, then its BFF authorizes one registered "
            "embedded instance through a session-bound, anti-forgery protected exchange. The "
            "embedded console redeems a one-time code and performs a protected case-document "
            "request, while sibling origins, subject or instance mismatches, duplicate approval, "
            "wrong verifier, and replay attempts fail closed."
        ),
        action=_identity_action_requires_harness,
    ),
)


def _steps_for_scope(scope: str) -> tuple[Step, ...]:
    return STEPS if scope == "channel" else (*STEPS, *IDENTITY_STEPS)


def selected_steps(from_step: str | None, scope: str = "channel") -> tuple[Step, ...]:
    available = _steps_for_scope(scope)
    if from_step is None:
        return available
    for index, step in enumerate(available):
        if step.id == from_step:
            return available[index:]
    choices = ", ".join(step.id for step in available)
    raise ValueError(f"unknown step {from_step!r}; choose one of: {choices}")


def bind_identity_steps(
    steps: Sequence[Step],
    harness: Any,
    browser_name: str,
) -> tuple[Step, ...]:
    actions = {
        "identity-mode4": partial(
            prove_mode4,
            harness=harness,
            browser_name=browser_name,
        ),
        "identity-mode5": partial(
            prove_mode5,
            harness=harness,
            browser_name=browser_name,
        ),
    }
    return tuple(
        Step(
            id=step.id,
            title=step.title,
            presenter_notes=step.presenter_notes,
            action=actions.get(step.id, step.action),
        )
        for step in steps
    )


def print_script(steps: Sequence[Step]) -> None:
    for number, step in enumerate(steps, start=1):
        print(f"{number:02d}. {step.id}: {step.title}")
        print(f"    Notes: {step.presenter_notes}")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


class HostStaticHandler(QuietHandler):
    server: HostStaticServer

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/bff" or self.path.startswith("/bff/"):
            self._proxy_bff()
        else:
            super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/bff" or self.path.startswith("/bff/"):
            self._proxy_bff()
        else:
            self.send_error(405)

    def do_OPTIONS(self) -> None:  # noqa: N802
        if self.path == "/bff" or self.path.startswith("/bff/"):
            self._proxy_bff()
        else:
            self.send_error(405)

    def _proxy_bff(self) -> None:
        if not self.server.proxy_target:
            self.send_error(503, "identity BFF evidence is not running")
            return
        target = urllib.parse.urlsplit(self.server.proxy_target)
        path = self.path.removeprefix("/bff") or "/"
        body_length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(body_length) if body_length else None
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in _HOP_HEADERS
            and name.lower() not in {"host", "accept-encoding", "content-length"}
        }
        if body is not None:
            headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection(target.hostname, target.port, timeout=30)
        try:
            connection.request(self.command, path, body=body, headers=headers)
            upstream = connection.getresponse()
            payload = upstream.read()
            self.send_response(upstream.status, upstream.reason)
            for name, value in upstream.getheaders():
                if name.lower() not in _HOP_HEADERS and name.lower() != "content-length":
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        finally:
            connection.close()


class HostStaticServer(http.server.ThreadingHTTPServer):
    def __init__(
        self,
        port: int,
        *,
        directory: Path,
        proxy_target: str,
    ) -> None:
        handler = partial(HostStaticHandler, directory=str(directory))
        super().__init__(("127.0.0.1", port), handler)
        self.proxy_target = proxy_target


_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class AgentEdgeHandler(http.server.BaseHTTPRequestHandler):
    """Tiny production-shaped edge for the fixed UI mount and canonical API path."""

    server: AgentEdgeServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        self._proxy()

    def do_POST(self) -> None:  # noqa: N802
        self._proxy()

    def do_DELETE(self) -> None:  # noqa: N802
        self._proxy()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._proxy()

    def do_HEAD(self) -> None:  # noqa: N802
        self._proxy()

    def _proxy(self) -> None:
        is_api = self.path == "/agent/api" or self.path.startswith("/agent/api/")
        if is_api:
            installation_id = self.headers.get("X-CDD-Installation-ID", "")
            target = self.server.api_targets.get(installation_id, "")
            if not target and len(set(self.server.api_targets.values())) == 1:
                target = next(iter(self.server.api_targets.values()))
            if not target:
                self.send_error(503, "identity evidence backend is not running")
                return
            path = self.path.removeprefix("/agent/api") or "/"
        else:
            target = self.server.ui_origin
            path = self.path
        parsed = urllib.parse.urlsplit(target)
        body_length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(body_length) if body_length else None
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in _HOP_HEADERS
            and name.lower() not in {"host", "accept-encoding", "content-length"}
        }
        if body is not None:
            headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=30)
        try:
            connection.request(self.command, path, body=body, headers=headers)
            upstream = connection.getresponse()
            payload = upstream.read()
            self.send_response(upstream.status, upstream.reason)
            for name, value in upstream.getheaders():
                lowered = name.lower()
                if lowered in _HOP_HEADERS or lowered == "content-length":
                    continue
                if lowered == "location":
                    value = value.replace(self.server.ui_origin, self.server.public_origin)
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        finally:
            connection.close()


class AgentEdgeServer(http.server.ThreadingHTTPServer):
    def __init__(
        self,
        api_targets: dict[str, str],
        *,
        public_origin: str,
        ui_origin: str,
    ) -> None:
        port = urllib.parse.urlsplit(public_origin).port
        assert port is not None
        super().__init__(("127.0.0.1", port), AgentEdgeHandler)
        self.api_targets = api_targets
        self.public_origin = public_origin
        self.ui_origin = ui_origin


@dataclass(slots=True)
class AgentEdgeService:
    api_targets: dict[str, str]
    public_origin: str = AGENT_ORIGIN
    ui_origin: str = UI_ORIGIN
    server: AgentEdgeServer | None = None
    thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            self.server = AgentEdgeServer(
                self.api_targets,
                public_origin=self.public_origin,
                ui_origin=self.ui_origin,
            )
        except OSError as error:
            raise RuntimeError(
                f"{self.public_origin} is occupied by an unexpected service"
            ) from error
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _wait_for(f"{self.public_origin}{LOADER_PATH}", "cdd-sow-research embed loader v1")

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


@dataclass(slots=True)
class StaticService:
    origin: str
    directory: Path
    marker: str
    proxy_target: str = ""
    server: socketserver.TCPServer | None = None
    thread: threading.Thread | None = None

    def start_or_reuse(self) -> None:
        if _url_contains(self.origin, self.marker):
            return
        port = urllib.parse.urlsplit(self.origin).port
        assert port is not None
        try:
            if self.proxy_target:
                self.server = HostStaticServer(
                    port,
                    directory=self.directory,
                    proxy_target=self.proxy_target,
                )
            else:
                handler = partial(QuietHandler, directory=str(self.directory))
                self.server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError as error:
            raise RuntimeError(f"{self.origin} is occupied by an unexpected service") from error
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _wait_for(self.origin, self.marker)

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


class DemoServices:
    def __init__(self, api_targets: dict[str, str] | None = None) -> None:
        targets = api_targets or {}
        self.ui_process: subprocess.Popen[str] | None = None
        self.mode5_ui_process: subprocess.Popen[str] | None = None
        self.edge = AgentEdgeService(targets)
        self.mode5_edge = (
            AgentEdgeService(
                {"inst_mode5": targets["inst_mode5"]},
                public_origin=MODE5_AGENT_ORIGIN,
                ui_origin=MODE5_UI_ORIGIN,
            )
            if "inst_mode5" in targets
            else None
        )
        self.static = (
            StaticService(
                HOST_A,
                HOSTS / "host-a",
                "Synthetic bank host A",
                proxy_target=targets.get("inst_mode5", ""),
            ),
            StaticService(HOST_B, HOSTS / "host-b", "Synthetic bank host B"),
            StaticService(
                UNREGISTERED_HOST,
                BROWSER_FIXTURES / "unregistered-host",
                "Unregistered portal",
            ),
            StaticService(
                STANDALONE,
                BROWSER_FIXTURES / "standalone",
                "Standalone fixture service",
            ),
        )

    def __enter__(self) -> DemoServices:
        try:
            self._start_ui()
            self.edge.start()
            if self.mode5_edge is not None:
                self._start_mode5_ui()
                self.mode5_edge.start()
            for service in self.static:
                service.start_or_reuse()
            expected_csp = "frame-ancestors http://127.0.0.1:4101"
            if not _header_contains(
                f"{AGENT_ORIGIN}/agent/embed/inst_host_a",
                "content-security-policy",
                expected_csp,
            ):
                raise RuntimeError(
                    "cdd-sow-research UI did not load the synthetic multi-host installation "
                    "manifest"
                )
            if self.mode5_edge is not None and not _header_contains(
                f"{MODE5_AGENT_ORIGIN}/agent/embed/inst_mode5",
                "content-security-policy",
                "frame-ancestors http://127.0.0.1:4101",
            ):
                raise RuntimeError(
                    "cdd-sow-research Mode 5 UI did not load its reviewed installation"
                )
            return self
        except Exception:
            self.__exit__(*sys.exc_info())
            raise

    def _start_ui(self) -> None:
        if not (UI / "node_modules").is_dir():
            raise RuntimeError("ui/node_modules is missing; run 'cd ui && npm ci'")
        subprocess.run(["npm", "run", "build"], cwd=UI, check=True)
        environment = {
            **os.environ,
            "CDD_INSTALLATION_MANIFEST": str(HOSTS / "installations.json"),
            "NEXT_TELEMETRY_DISABLED": "1",
        }
        self.ui_process = subprocess.Popen(
            ["npm", "start", "--", "--hostname", "127.0.0.1", "--port", "3201"],
            cwd=UI,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            text=True,
        )
        _wait_for(f"{UI_ORIGIN}{LOADER_PATH}", "cdd-sow-research embed loader v1")

    def _start_mode5_ui(self) -> None:
        environment = {
            **os.environ,
            "CDD_INSTALLATION_MANIFEST": str(BROWSER_FIXTURES / "installations-mode5.json"),
            "NEXT_TELEMETRY_DISABLED": "1",
        }
        self.mode5_ui_process = subprocess.Popen(
            ["npm", "start", "--", "--hostname", "127.0.0.1", "--port", "3211"],
            cwd=UI,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            text=True,
        )
        _wait_for(f"{MODE5_UI_ORIGIN}{LOADER_PATH}", "cdd-sow-research embed loader v1")

    def __exit__(self, *_error: object) -> None:
        for service in reversed(self.static):
            service.close()
        if self.mode5_edge is not None:
            self.mode5_edge.close()
        self.edge.close()
        for process in (self.mode5_ui_process, self.ui_process):
            if process is None:
                continue
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _read_url(url: str, timeout: float = 1.0) -> tuple[bytes, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "doc1-portability-evidence/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read(), response.headers


def _url_contains(url: str, marker: str) -> bool:
    try:
        body, _headers = _read_url(url)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return False
    return marker.encode() in body


def _header_contains(url: str, header: str, marker: str) -> bool:
    try:
        _body, headers = _read_url(url)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return False
    return marker in (headers.get(header) or "")


def _wait_for(url: str, marker: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _url_contains(url, marker):
            return
        time.sleep(0.1)
    raise RuntimeError(f"service readiness failed: {url} did not contain {marker!r}")


def loader_evidence() -> tuple[str, str]:
    loader, headers = _read_url(f"{AGENT_ORIGIN}{LOADER_PATH}", timeout=5)
    cache = headers.get("cache-control") or ""
    if headers.get("access-control-allow-origin") != "*" or "immutable" not in cache:
        raise AssertionError("loader response is not cross-origin and immutable")
    sri = "sha384-" + base64.b64encode(hashlib.sha384(loader).digest()).decode()
    fixture_sri = (UI / "public" / "embed" / "v1" / "cdd-agent.js.sri").read_text().strip()
    if sri != fixture_sri:
        raise AssertionError("served loader does not match its generated SRI")
    return sri, hashlib.sha256(loader).hexdigest()


_HOST_BOUNDARY_PROBE = r"""
(() => {
  if (window.__cddHostBoundaryProbe) return;
  const encoder = new TextEncoder();
  const events = [];
  const counters = Object.create(null);
  const pending = new Set();

  async function sha256(value) {
    const bytes = await crypto.subtle.digest("SHA-256", encoder.encode(value));
    return Array.from(new Uint8Array(bytes), (byte) =>
      byte.toString(16).padStart(2, "0")
    ).join("");
  }

  async function collectDigests(value, output, seen = new WeakSet()) {
    if (typeof value === "string") {
      output.add(await sha256(value));
      const bearer = value.match(/^Bearer\s+(.+)$/i);
      if (bearer) output.add(await sha256(bearer[1]));
      if (/^\s*[\[{]/.test(value)) {
        try {
          await collectDigests(JSON.parse(value), output, seen);
        } catch {}
      }
      try {
        const url = new URL(value, location.href);
        if (/^https?:$/.test(url.protocol)) {
          for (const parameter of url.searchParams.values()) {
            await collectDigests(parameter, output, seen);
          }
        }
      } catch {}
      return;
    }
    if (
      value === null ||
      value === undefined ||
      typeof value === "function" ||
      typeof value === "symbol"
    ) {
      return;
    }
    if (typeof value !== "object") {
      output.add(await sha256(String(value)));
      return;
    }
    if (seen.has(value)) return;
    seen.add(value);
    if (Array.isArray(value)) {
      for (const item of value) await collectDigests(item, output, seen);
      return;
    }
    for (const [key, item] of Object.entries(value)) {
      output.add(await sha256(key));
      await collectDigests(item, output, seen);
    }
  }

  function observe(surface, direction, value) {
    const counter = `${surface}:${direction}`;
    counters[counter] = (counters[counter] || 0) + 1;
    let task;
    task = (async () => {
      const digests = new Set();
      await collectDigests(value, digests);
      events.push({surface, direction, digests: Array.from(digests).sort()});
    })()
      .catch(() => {
        events.push({surface, direction, digests: [], observation_failed: true});
      })
      .finally(() => pending.delete(task));
    pending.add(task);
  }

  async function bodyView(body) {
    if (body === null || body === undefined) return null;
    if (typeof body === "string") return body;
    if (body instanceof URLSearchParams) return body.toString();
    if (body instanceof FormData) {
      const entries = [];
      for (const [key, value] of body.entries()) {
        entries.push([
          key,
          typeof value === "string"
            ? value
            : {name: value.name, type: value.type, size: value.size},
        ]);
      }
      return entries;
    }
    if (body instanceof Blob) return body.text();
    if (body instanceof ArrayBuffer) {
      return new TextDecoder().decode(new Uint8Array(body));
    }
    if (ArrayBuffer.isView(body)) {
      return new TextDecoder().decode(
        new Uint8Array(body.buffer, body.byteOffset, body.byteLength)
      );
    }
    return String(body);
  }

  const nativeFetch = window.fetch.bind(window);
  window.fetch = async function boundaryFetch(input, init) {
    let requestBody = init?.body;
    let url = String(input);
    let method = init?.method || "GET";
    let headers = Array.from(new Headers(init?.headers).entries());
    if (input instanceof Request) {
      url = input.url;
      method = init?.method || input.method;
      headers = Array.from(
        new Headers(init?.headers === undefined ? input.headers : init.headers).entries()
      );
      if (requestBody === undefined && input.method !== "GET" && input.method !== "HEAD") {
        try {
          requestBody = await input.clone().text();
        } catch {}
      }
    }
    observe("network", "host-to-network", {
      url,
      method,
      headers,
      body: await bodyView(requestBody),
    });
    const response = await nativeFetch(input, init);
    let responseBody = null;
    try {
      responseBody = await response.clone().text();
    } catch {}
    observe("network", "network-to-host", {
      url: response.url,
      status: response.status,
      headers: Array.from(response.headers.entries()),
      body: responseBody,
    });
    return response;
  };

  const nativePortPost = MessagePort.prototype.postMessage;
  function boundaryPortPost(message, transfer) {
    observe("message-port", "host-to-agent", message);
    return arguments.length > 1
      ? nativePortPost.call(this, message, transfer)
      : nativePortPost.call(this, message);
  }
  Object.defineProperty(MessagePort.prototype, "postMessage", {
    configurable: false,
    enumerable: false,
    writable: false,
    value: boundaryPortPost,
  });

  const NativeMessageChannel = window.MessageChannel;
  class BoundaryMessageChannel extends NativeMessageChannel {
    constructor() {
      super();
      for (const port of [this.port1, this.port2]) {
        port.addEventListener("message", (event) => {
          observe("message-port", "agent-to-host", event.data);
        });
      }
    }
  }
  Object.defineProperty(window, "MessageChannel", {
    configurable: false,
    enumerable: false,
    writable: false,
    value: BoundaryMessageChannel,
  });

  const nativeWindowPost = Window.prototype.postMessage;
  Window.prototype.postMessage = function boundaryWindowPost(message, targetOrigin, transfer) {
    observe("window-message", "host-to-agent", message);
    return arguments.length > 2
      ? nativeWindowPost.call(this, message, targetOrigin, transfer)
      : nativeWindowPost.call(this, message, targetOrigin);
  };
  window.addEventListener(
    "message",
    (event) => observe("window-message", "agent-to-host", event.data),
    {capture: true}
  );

  Object.defineProperty(window, "__cddHostBoundaryProbe", {
    configurable: false,
    enumerable: false,
    writable: false,
    value: Object.freeze({
      observe(surface, direction, value) {
        observe(surface, direction, value);
      },
      async snapshot() {
        while (pending.size > 0) await Promise.allSettled(Array.from(pending));
        return {
          counters: {...counters},
          events: events.map((event) => ({
            surface: event.surface,
            direction: event.direction,
            digests: [...event.digests],
            observation_failed: event.observation_failed === true,
          })),
        };
      },
    }),
  });
})();
"""


def _install_host_boundary_probe(page: Any) -> None:
    page.add_init_script(_HOST_BOUNDARY_PROBE)


def _host_boundary_snapshot(page: Any) -> dict[str, Any]:
    snapshot = page.evaluate("window.__cddHostBoundaryProbe?.snapshot()")
    if not isinstance(snapshot, dict):
        raise AssertionError("host boundary probe was not installed")
    counters = snapshot.get("counters")
    events = snapshot.get("events")
    if not isinstance(counters, dict) or not isinstance(events, list):
        raise AssertionError("host boundary probe returned an invalid snapshot")
    if any(
        not isinstance(event, dict)
        or event.get("observation_failed") is True
        or not isinstance(event.get("digests"), list)
        for event in events
    ):
        raise AssertionError("host boundary probe failed to digest an observed payload")
    return snapshot


def _merge_boundary_snapshots(*snapshots: dict[str, Any]) -> dict[str, Any]:
    counters: dict[str, int] = {}
    events: list[dict[str, Any]] = []
    for snapshot in snapshots:
        for key, value in snapshot["counters"].items():
            if not isinstance(key, str) or not isinstance(value, int):
                raise AssertionError("host boundary counter is invalid")
            counters[key] = counters.get(key, 0) + value
        events.extend(snapshot["events"])
    return {"counters": counters, "events": events}


def _digest_locations(snapshot: dict[str, Any], digest: str) -> set[tuple[str, str]]:
    return {
        (str(event["surface"]), str(event["direction"]))
        for event in snapshot["events"]
        if digest in event["digests"]
    }


def _assert_required_boundary_traffic(
    snapshot: dict[str, Any],
    required: Sequence[tuple[str, str, int]],
    *,
    dimension: str,
) -> None:
    counters = snapshot["counters"]
    missing = [
        f"{surface}:{direction}"
        for surface, direction, minimum in required
        if counters.get(f"{surface}:{direction}", 0) < minimum
    ]
    if missing:
        raise DimensionFailure(
            dimension,
            f"host boundary probe observed no required traffic on {', '.join(missing)}",
        )


def _assert_forbidden_digests_absent(
    snapshot: dict[str, Any],
    forbidden: dict[str, Sequence[str]],
    *,
    dimension: str,
) -> None:
    leaked_categories = sorted(
        category
        for category, digests in forbidden.items()
        if any(_digest_locations(snapshot, digest) for digest in digests)
    )
    if leaked_categories:
        raise DimensionFailure(
            dimension,
            "forbidden credential crossed the host boundary: " + ", ".join(leaked_categories),
        )


def _mode4_boundary_evidence(
    snapshot: dict[str, Any],
    *,
    expected_credentials: Sequence[str],
    secret_digests: dict[str, Sequence[str]],
) -> dict[str, Any]:
    dimension = "identity.mode4"
    _assert_required_boundary_traffic(
        snapshot,
        (
            ("window-message", "host-to-agent", 1),
            ("message-port", "agent-to-host", 1),
            ("message-port", "host-to-agent", len(expected_credentials)),
        ),
        dimension=dimension,
    )
    allowed_location = {("message-port", "host-to-agent")}
    expected_digests = tuple(
        hashlib.sha256(value.encode()).hexdigest() for value in expected_credentials
    )
    missing = [
        index
        for index, digest in enumerate(expected_digests, start=1)
        if allowed_location - _digest_locations(snapshot, digest)
    ]
    unexpected = [
        index
        for index, digest in enumerate(secret_digests.get("mode4_credential", ()), start=1)
        if _digest_locations(snapshot, digest) - allowed_location
    ]
    if missing or unexpected:
        raise DimensionFailure(
            dimension,
            "Mode 4 credential boundary mismatch: "
            f"missing_expected={missing}, unexpected_paths={unexpected}",
        )
    _assert_forbidden_digests_absent(
        snapshot,
        {
            category: digests
            for category, digests in secret_digests.items()
            if category != "mode4_credential"
        },
        dimension=dimension,
    )
    return {
        "status": "PASS",
        "traffic": dict(sorted(snapshot["counters"].items())),
        "mode4_credentials_checked": len(secret_digests.get("mode4_credential", ())),
        "expected_credentials_observed_on_port": len(expected_digests),
        "raw_values_recorded": 0,
    }


def _mode5_boundary_evidence(
    snapshot: dict[str, Any],
    *,
    instance_id: str,
    launch_code: str,
    secret_digests: dict[str, Sequence[str]],
) -> dict[str, Any]:
    dimension = "identity.mode5"
    _assert_required_boundary_traffic(
        snapshot,
        (
            ("window-message", "host-to-agent", 1),
            ("message-port", "agent-to-host", 2),
            ("message-port", "host-to-agent", 1),
            ("network", "host-to-network", 1),
            ("network", "network-to-host", 1),
        ),
        dimension=dimension,
    )
    instance_digest = hashlib.sha256(instance_id.encode()).hexdigest()
    launch_digest = hashlib.sha256(launch_code.encode()).hexdigest()
    required_allowed = {
        "instance_id": (
            instance_digest,
            {
                ("message-port", "agent-to-host"),
                ("network", "host-to-network"),
                ("message-port", "host-to-agent"),
            },
        ),
        "launch_code": (
            launch_digest,
            {
                ("network", "network-to-host"),
                ("message-port", "host-to-agent"),
            },
        ),
    }
    missing = {
        name: sorted(required - _digest_locations(snapshot, digest))
        for name, (digest, required) in required_allowed.items()
        if required - _digest_locations(snapshot, digest)
    }
    if missing:
        raise DimensionFailure(
            dimension,
            f"allowed host binding was not observed on every required path: {missing}",
        )
    forbidden = {
        category: digests
        for category, digests in secret_digests.items()
        if category
        in {
            "mode4_credential",
            "mode5_bff_assertion",
            "mode5_doc1_token",
            "mode5_pkce_verifier",
            "mode5_subject_credential",
        }
    }
    required_forbidden_categories = {
        "mode5_bff_assertion",
        "mode5_doc1_token",
        "mode5_pkce_verifier",
        "mode5_subject_credential",
    }
    missing_categories = sorted(required_forbidden_categories - forbidden.keys())
    if missing_categories:
        raise DimensionFailure(
            dimension,
            "identity harness supplied no digest for " + ", ".join(missing_categories),
        )
    _assert_forbidden_digests_absent(snapshot, forbidden, dimension=dimension)
    return {
        "status": "PASS",
        "traffic": dict(sorted(snapshot["counters"].items())),
        "forbidden_credentials_checked": {
            category: len(digests) for category, digests in sorted(forbidden.items())
        },
        "allowed_bindings_observed": {
            "instance_id": sorted(
                f"{surface}:{direction}"
                for surface, direction in _digest_locations(snapshot, instance_digest)
            ),
            "launch_code": sorted(
                f"{surface}:{direction}"
                for surface, direction in _digest_locations(snapshot, launch_digest)
            ),
        },
        "raw_values_recorded": 0,
    }


def _prove_boundary_mutation_gate(page: Any) -> str:
    sentinel = "FORBIDDEN-BOUNDARY-MUTATION-SENTINEL"
    page.evaluate(
        """(value) => {
          const channel = new MessageChannel();
          channel.port2.start();
          channel.port1.postMessage({completely_renamed_payload: value});
        }""",
        sentinel,
    )
    snapshot = _host_boundary_snapshot(page)
    try:
        _assert_forbidden_digests_absent(
            snapshot,
            {"mutation_sentinel": (hashlib.sha256(sentinel.encode()).hexdigest(),)},
            dimension="identity.mode5",
        )
    except DimensionFailure as error:
        if "mutation_sentinel" in str(error):
            return "PASS"
        raise
    raise DimensionFailure(
        "identity.mode5",
        "mutation sentinel crossed an alternate MessagePort path without failing the gate",
    )


def run_browser(
    playwright: Any,
    browser_name: str,
    steps: Sequence[Step],
    *,
    headless: bool,
    slow_mo: int,
    pause: bool,
    screenshots: Path | None,
) -> RunEvidence:
    sri, sha256 = loader_evidence()
    evidence = RunEvidence(browser=browser_name, loader_integrity=sri, loader_sha256=sha256)
    browser_type = getattr(playwright, browser_name)
    browser = browser_type.launch(headless=headless, slow_mo=slow_mo)
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    try:
        for number, step in enumerate(steps, start=1):
            page = context.new_page()
            page.set_default_timeout(20_000)
            print(
                f"\n{'=' * 72}\n{browser_name.upper()} STEP {number:02d}: {step.title}\nID: {step.id}\n"
            )
            print(f"PRESENTER NOTES: {step.presenter_notes}", flush=True)
            try:
                step.action(page, evidence)
                evidence.completed_steps.append(step.id)
                if screenshots is not None:
                    output = screenshots / browser_name
                    output.mkdir(parents=True, exist_ok=True)
                    page.screenshot(
                        path=str(output / f"{number:02d}-{step.id}.png"),
                        full_page=True,
                    )
                if pause:
                    input("Enter for next step...")
            finally:
                page.close()
    finally:
        context.close()
        browser.close()
    return evidence


def _protected_request(
    execution_context: Any,
    *,
    path: str,
    token: str,
    installation_id: str,
    transport: str,
    api_origin: str = AGENT_ORIGIN,
) -> dict[str, Any]:
    return execution_context.evaluate(
        """async ({apiOrigin, path, token, installationId, transport}) => {
          const headers = new Headers({
            Authorization: `Bearer ${token}`,
            "X-CDD-Installation-ID": installationId,
          });
          let body;
          if (transport === "json") {
            headers.set("Content-Type", "application/json");
            body = JSON.stringify({type: "browser-json-proof"});
          } else if (transport === "form-data") {
            body = new FormData();
            body.append("note", "browser-form-proof");
          } else {
            headers.set("Content-Type", "application/octet-stream");
            body = new Uint8Array([68, 111, 99, 49]);
          }
          try {
            const response = await fetch(`${apiOrigin}/agent/api${path}`, {
              method: "POST",
              headers,
              body,
              credentials: "same-origin",
              cache: "no-store",
            });
            const contentType = response.headers.get("content-type") || "";
            const bytes = new Uint8Array(await response.arrayBuffer());
            return {
              status: response.status,
              contentType,
              prefix: Array.from(bytes.slice(0, 5)),
            };
          } catch (error) {
            return {status: 0, error: String(error)};
          }
        }""",
        {
            "path": path,
            "apiOrigin": api_origin,
            "token": token,
            "installationId": installation_id,
            "transport": transport,
        },
    )


def _credential_absent(page: Any, frame: Any, credentials: Sequence[str]) -> bool:
    script = """() => JSON.stringify({
      url: location.href,
      html: document.documentElement.outerHTML,
      local: Object.entries(localStorage),
      session: Object.entries(sessionStorage),
      resources: performance.getEntriesByType("resource").map((entry) => entry.name),
    })"""
    surfaces = (page.evaluate(script), frame.evaluate(script))
    return all(secret not in surface for secret in credentials for surface in surfaces)


def _show_proof(page: Any, title: str, facts: Sequence[str]) -> None:
    page.evaluate(
        """({title, facts}) => {
          document.querySelector("#identity-proof")?.remove();
          const section = document.createElement("section");
          section.id = "identity-proof";
          section.style.cssText =
            "margin-top:24px;padding:20px;border:2px solid #19724a;border-radius:10px;background:#effbf5";
          const heading = document.createElement("h2");
          heading.textContent = title;
          section.append(heading);
          const list = document.createElement("ul");
          for (const fact of facts) {
            const item = document.createElement("li");
            item.textContent = fact;
            list.append(item);
          }
          section.append(list);
          document.querySelector("main")?.append(section);
        }""",
        {"title": title, "facts": list(facts)},
    )


def prove_mode4(page: Any, evidence: RunEvidence, harness: Any, browser_name: str) -> None:
    console_messages: list[str] = []
    page.on("console", lambda message: console_messages.append(message.text))
    _install_host_boundary_probe(page)
    page.goto(HOST_A, wait_until="domcontentloaded")
    page.get_by_role("status").filter(has_text="cdd-sow-research ready").wait_for(state="visible")
    frame = _agent_frame(page, "inst_host_a")
    frame.get_by_text("Assess a subject", exact=True).wait_for(state="visible")

    original = harness.mint_mode4_token("valid")
    page.evaluate(
        "(token) => document.querySelector('cdd-agent').setAccessToken(token)",
        original.access_token,
    )
    json_result = _protected_request(
        frame,
        path="/v1/harness/mode4/protected/json",
        token=original.access_token,
        installation_id="inst_host_a",
        transport="json",
    )
    form_result = _protected_request(
        frame,
        path="/v1/harness/mode4/protected/form",
        token=original.access_token,
        installation_id="inst_host_a",
        transport="form-data",
    )
    blob_result = _protected_request(
        frame,
        path="/v1/harness/mode4/protected/blob",
        token=original.access_token,
        installation_id="inst_host_a",
        transport="blob",
    )
    if (
        json_result["status"] != 200
        or form_result["status"] != 200
        or blob_result["status"] != 200
        or blob_result["contentType"] != "application/pdf"
        or blob_result["prefix"] != [37, 80, 68, 70, 45]
    ):
        raise DimensionFailure("identity.mode4", "authenticated transport proof failed")

    harness.rotate_mode4_issuer()
    refreshed = harness.mint_mode4_token("refresh")
    page.evaluate(
        "(token) => document.querySelector('cdd-agent').setAccessToken(token)",
        refreshed.access_token,
    )
    refresh_result = _protected_request(
        frame,
        path="/v1/harness/mode4/protected/json",
        token=refreshed.access_token,
        installation_id="inst_host_a",
        transport="json",
    )
    if refresh_result["status"] != 200:
        raise DimensionFailure("identity.mode4", "rotated issuer refresh was rejected")

    other = page.context.new_page()
    other_console: list[str] = []
    other.on("console", lambda message: other_console.append(message.text))
    _install_host_boundary_probe(other)
    ec_token = harness.mint_mode4_token("ec")
    other_boundary: dict[str, Any]
    try:
        other.goto(HOST_B, wait_until="domcontentloaded")
        other.get_by_role("status").filter(has_text="cdd-sow-research ready").wait_for(
            state="visible"
        )
        other_frame = _agent_frame(other, "inst_host_b")
        other.evaluate(
            "(token) => document.querySelector('cdd-agent').setAccessToken(token)",
            ec_token.access_token,
        )
        ec_result = _protected_request(
            other_frame,
            path="/v1/harness/mode4/protected/json",
            token=ec_token.access_token,
            installation_id="inst_host_b",
            transport="json",
        )
        if ec_result["status"] != 200:
            raise DimensionFailure("identity.mode4", "EC issuer token was rejected")
        ec_absent = _credential_absent(other, other_frame, (ec_token.access_token,))
        other_boundary = _host_boundary_snapshot(other)
    finally:
        other.close()

    rejected: dict[str, bool] = {}
    negative_tokens: list[str] = []
    for variant, label in (
        ("cross-tenant", "cross_tenant"),
        ("cross-installation", "cross_installation"),
        ("wrong-type", "wrong_type"),
    ):
        candidate = harness.mint_mode4_token(variant)
        negative_tokens.append(candidate.access_token)
        result = _protected_request(
            frame,
            path="/v1/harness/mode4/protected/json",
            token=candidate.access_token,
            installation_id="inst_host_a",
            transport="json",
        )
        rejected[label] = result["status"] == 401

    credential_absent = ec_absent and _credential_absent(
        page,
        frame,
        (
            original.access_token,
            refreshed.access_token,
            *negative_tokens,
        ),
    )
    main_boundary = _host_boundary_snapshot(page)
    boundary_evidence = _mode4_boundary_evidence(
        _merge_boundary_snapshots(main_boundary, other_boundary),
        expected_credentials=(
            original.access_token,
            refreshed.access_token,
            ec_token.access_token,
        ),
        secret_digests=harness.boundary_secret_digests(),
    )
    page.goto(f"{HOST_A}/mode4-origin.html", wait_until="domcontentloaded")
    wrong_origin = _protected_request(
        page,
        path="/v1/harness/mode4/protected/json",
        token=refreshed.access_token,
        installation_id="inst_host_a",
        transport="json",
    )
    wrong_origin_rejected = wrong_origin["status"] == 401
    console_safe = not any(
        token in message
        for token in (
            original.access_token,
            refreshed.access_token,
            ec_token.access_token,
            *negative_tokens,
        )
        for message in (*console_messages, *other_console)
    )
    if (
        not all(rejected.values())
        or not wrong_origin_rejected
        or not credential_absent
        or not console_safe
    ):
        raise DimensionFailure(
            "identity.mode4",
            "identity boundary or leak assertion failed: "
            f"negative_status={rejected}, origin={wrong_origin.get('status')}, "
            f"credential_absent={credential_absent}, console_safe={console_safe}",
        )

    harness.record_mode4_browser_evidence(
        json_call=True,
        form_data_call=True,
        blob_call=True,
        rsa_issuer=True,
        ec_issuer=True,
        rotation_refresh=True,
        cross_tenant_rejected=rejected["cross_tenant"],
        cross_installation_rejected=rejected["cross_installation"],
        wrong_origin_rejected=wrong_origin_rejected,
        wrong_type_rejected=rejected["wrong_type"],
        credential_absent_from_dom=credential_absent,
    )
    mode4 = harness.mode4_evidence()
    if mode4.get("status") != "ready":
        raise DimensionFailure("identity.mode4", "Mode 4 evidence contract is not ready")
    _show_proof(
        page,
        "PASS: direct institutional identity",
        (
            "Short-lived RSA and EC issuer tokens were verified.",
            "JSON, FormData, and blob transport retained the installation binding.",
            "Rotation refresh passed; tenant, installation, origin, and type confusion failed.",
            "Credentials were absent from URLs, DOM, storage, console, and evidence.",
        ),
    )
    evidence.observations["identity.mode4"] = {
        "status": "PASS",
        "browser": browser_name,
        "server": mode4["server"],
        "host_boundary": boundary_evidence,
    }


def _bff_request(
    page: Any,
    path: str,
    *,
    body: dict[str, object] | None = None,
    csrf: str = "",
    origin: str = HOST_A,
) -> dict[str, Any]:
    return page.evaluate(
        """async ({origin, path, body, csrf}) => {
          const headers = new Headers();
          if (body !== null) headers.set("Content-Type", "application/json");
          if (csrf) headers.set("X-CSRF-Token", csrf);
          try {
            const response = await fetch(`${origin}/bff${path}`, {
              method: "POST",
              credentials: "include",
              cache: "no-store",
              headers,
              body: body === null ? undefined : JSON.stringify(body),
            });
            let payload = {};
            try { payload = await response.json(); } catch {}
            return {status: response.status, body: payload};
          } catch (error) {
            return {status: 0, error: String(error)};
          }
        }""",
        {"origin": origin, "path": path, "body": body, "csrf": csrf},
    )


def _mode5_redeem_probe(
    frame: Any,
    *,
    instance_id: str,
    launch_code: str,
) -> int:
    result = frame.evaluate(
        """async ({instanceId, launchCode}) => {
          const response = await fetch("/agent/api/v1/embed/token", {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: {
              "Content-Type": "application/json",
              "X-CDD-Installation-ID": "inst_mode5",
            },
            body: JSON.stringify({
              installation_id: "inst_mode5",
              instance_id: instanceId,
              launch_code: launchCode,
              pkce_verifier: "A".repeat(43),
            }),
          });
          return response.status;
        }""",
        {"instanceId": instance_id, "launchCode": launch_code},
    )
    return int(result)


def prove_mode5(page: Any, evidence: RunEvidence, harness: Any, browser_name: str) -> None:
    console_messages: list[str] = []
    api_responses: list[tuple[str, int, str]] = []
    page.on("console", lambda message: console_messages.append(message.text))
    page.on(
        "response",
        lambda response: (
            api_responses.append((response.request.method, response.status, response.url))
            if "/agent/api/" in response.url
            else None
        ),
    )
    _install_host_boundary_probe(page)
    page.goto(f"{HOST_A}/mode5.html", wait_until="domcontentloaded")
    page.get_by_role("status").filter(
        has_text="Iframe grant registered; waiting for BFF authorization"
    ).wait_for(state="visible")
    registration = page.evaluate("window.__doc1GrantRegistration")
    if (
        not isinstance(registration, dict)
        or registration.get("installationId") != "inst_mode5"
        or not isinstance(registration.get("instanceId"), str)
    ):
        raise DimensionFailure("identity.mode5", "iframe-first registration was not bounded")
    instance_id = str(registration["instanceId"])
    frame = _agent_frame(page, "inst_mode5")

    sibling = page.context.new_page()
    try:
        sibling.goto(f"{HOST_B}/mode5-sibling.html", wait_until="domcontentloaded")
        sibling_result = _bff_request(
            sibling,
            "/v1/harness/bff/session",
            origin=HOST_A,
        )
    finally:
        sibling.close()
    sibling_rejected = sibling_result["status"] in {0, 403}

    session = _bff_request(page, "/v1/harness/bff/session")
    if session["status"] != 200:
        raise DimensionFailure("identity.mode5", "authenticated BFF session was rejected")
    csrf = session["body"].get("csrf_token")
    if not isinstance(csrf, str) or len(csrf) < 22:
        raise DimensionFailure("identity.mode5", "BFF anti-forgery binding was not returned")
    intent_body: dict[str, object] = {
        "installation_id": "inst_mode5",
        "instance_id": instance_id,
        "action": "authorize-embed",
    }
    missing_csrf = _bff_request(
        page,
        "/v1/harness/bff/intents",
        body=intent_body,
    )
    wrong_csrf = _bff_request(
        page,
        "/v1/harness/bff/intents",
        body=intent_body,
        csrf="wrong-csrf-binding",
    )
    intent = _bff_request(
        page,
        "/v1/harness/bff/intents",
        body=intent_body,
        csrf=csrf,
    )
    user_intent_id = intent["body"].get("user_intent_id")
    if intent["status"] != 200 or not isinstance(user_intent_id, str):
        raise DimensionFailure("identity.mode5", "BFF user intent was not recorded")

    mismatched_instance = _bff_request(
        page,
        "/v1/harness/bff/authorize",
        body={
            "instance_id": "wrong-instance-binding-012345",
            "user_intent_id": user_intent_id,
        },
        csrf=csrf,
    )
    authorized = _bff_request(
        page,
        "/v1/harness/bff/authorize",
        body={"instance_id": instance_id, "user_intent_id": user_intent_id},
        csrf=csrf,
    )
    launch_code = authorized["body"].get("launch_code")
    if authorized["status"] != 200 or not isinstance(launch_code, str):
        raise DimensionFailure("identity.mode5", "BFF did not issue a launch code")
    duplicate = _bff_request(
        page,
        "/v1/harness/bff/authorize",
        body={"instance_id": instance_id, "user_intent_id": user_intent_id},
        csrf=csrf,
    )
    auditor = _bff_request(page, "/v1/harness/bff/session?persona=auditor")
    auditor_csrf = auditor["body"].get("csrf_token")
    if auditor["status"] != 200 or not isinstance(auditor_csrf, str):
        raise DimensionFailure("identity.mode5", "mismatched BFF session setup failed")
    subject_mismatch = _bff_request(
        page,
        "/v1/harness/bff/authorize",
        body={"instance_id": instance_id, "user_intent_id": user_intent_id},
        csrf=auditor_csrf,
    )

    wrong_verifier_status = _mode5_redeem_probe(
        frame,
        instance_id=instance_id,
        launch_code=launch_code,
    )
    page.evaluate(
        """({instanceId, launchCode}) => {
          document.querySelector("cdd-agent").setLaunchCode(instanceId, launchCode);
        }""",
        {"instanceId": instance_id, "launchCode": launch_code},
    )
    page.get_by_role("status").filter(
        has_text="PASS: embedded identity ready without host credential custody"
    ).wait_for(state="visible")
    frame.get_by_text("Assess a subject", exact=True).wait_for(state="visible")

    case_name = f"Mode Five {browser_name}"
    case_id = f"mode-five-{browser_name}"
    try:
        with page.expect_response(
            lambda response: (
                f"/agent/api/v1/cases/{case_id}/documents" in response.url
                and response.request.method == "GET"
            ),
            timeout=20_000,
        ) as protected_response:
            subject = frame.get_by_placeholder("Legal name of the company or person")
            subject.click()
            subject.press_sequentially(case_name, delay=20)
    except Exception as error:
        subject_value = frame.get_by_placeholder(
            "Legal name of the company or person"
        ).input_value()
        assess_enabled = frame.get_by_role("button", name="Build CDD dossier").is_enabled()
        raise DimensionFailure(
            "identity.mode5",
            "protected UI call was not observed; "
            f"subject={subject_value!r}; assess_enabled={assess_enabled}; "
            f"API responses={api_responses!r}; console={console_messages!r}",
        ) from error
    protected_status = protected_response.value.status
    replay_status = _mode5_redeem_probe(
        frame,
        instance_id=instance_id,
        launch_code=launch_code,
    )
    boundary_evidence = _mode5_boundary_evidence(
        _host_boundary_snapshot(page),
        instance_id=instance_id,
        launch_code=launch_code,
        secret_digests=harness.boundary_secret_digests(),
    )
    mutation_gate = _prove_boundary_mutation_gate(page)
    surface_script = """() => JSON.stringify({
      url: location.href,
      html: document.documentElement.outerHTML,
      local: Object.entries(localStorage),
      session: Object.entries(sessionStorage),
      resources: performance.getEntriesByType("resource").map((entry) => entry.name),
    })"""
    browser_surfaces = page.evaluate(surface_script) + frame.evaluate(surface_script)
    credential_absent = (
        "eyj" not in browser_surfaces.lower()
        and "pkce_verifier" not in browser_surfaces.lower()
        and "subject_token" not in browser_surfaces.lower()
        and "client_assertion" not in browser_surfaces.lower()
        and not any("eyj" in message.lower() for message in console_messages)
    )

    checks = {
        "sibling_origin_rejected": sibling_rejected,
        "missing_csrf_rejected": missing_csrf["status"] == 403,
        "wrong_csrf_rejected": wrong_csrf["status"] == 403,
        "subject_session_mismatch_rejected": subject_mismatch["status"] == 403,
        "instance_mismatch_rejected": mismatched_instance["status"] == 403,
        "duplicate_authorization_rejected": duplicate["status"] == 409,
    }
    if (
        not all(checks.values())
        or wrong_verifier_status != 401
        or replay_status != 409
        or protected_status != 200
        or not credential_absent
    ):
        raise DimensionFailure(
            "identity.mode5",
            "embedded grant boundary failed: "
            f"bff={checks}, wrong_verifier={wrong_verifier_status}, "
            f"replay={replay_status}, protected={protected_status}, "
            f"credential_absent={credential_absent}",
        )

    harness.record_mode5_browser_evidence(
        iframe_registered_first=True,
        protected_call=protected_status == 200,
        wrong_verifier_rejected=wrong_verifier_status == 401,
        launch_code_replay_rejected=replay_status == 409,
        **checks,
        host_never_received_subject_token=boundary_evidence["status"] == "PASS",
        host_never_received_pkce_verifier=boundary_evidence["status"] == "PASS",
        host_never_received_doc1_token=boundary_evidence["status"] == "PASS",
        credential_absent_from_dom=credential_absent,
    )
    mode5 = harness.mode5_evidence()
    if mode5.get("status") != "ready":
        raise DimensionFailure("identity.mode5", "Mode 5 evidence contract is not ready")
    _show_proof(
        page,
        "PASS: BFF embedded grant identity",
        (
            "The iframe registered PKCE before the host authorized access.",
            "The BFF verified session, CSRF, intent, origin, fetch metadata, and subject binding.",
            "Wrong verifier, replay, sibling origin, mismatch, and duplicate paths failed closed.",
            "Subject credential, PKCE verifier, and cdd-sow-research token never entered host "
            "custody.",
        ),
    )
    evidence.observations["identity.mode5"] = {
        "status": "PASS",
        "browser": browser_name,
        "server": mode5["server"],
        "host_boundary": boundary_evidence,
        "mutation_gate": mutation_gate,
    }


def channel_dependency_status() -> dict[str, dict[str, str]]:
    return {
        dimension: {
            "status": "NOT_RUN",
            "reason": "run --scope full for production-backed browser identity evidence",
        }
        for dimension in ("identity.mode4", "identity.mode5")
    }


def identity_dependency_status(harness: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for dimension, report in (
        ("identity.mode4", harness.mode4_evidence()),
        ("identity.mode5", harness.mode5_evidence()),
    ):
        ready = report.get("status") == "ready"
        result[dimension] = {
            "status": "PASS" if ready else "FAIL",
            "reason": (
                "production-backed browser evidence ready"
                if ready
                else "production-backed browser evidence incomplete"
            ),
            "evidence": report,
        }
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--list", action="store_true")
    result.add_argument("--from", dest="from_step", metavar="STEP-ID")
    result.add_argument("--no-pause", action="store_true")
    result.add_argument("--screenshots", type=Path, metavar="DIR")
    result.add_argument("--slow-mo", type=int, default=0, metavar="MS")
    result.add_argument(
        "--browser",
        choices=("chromium", "firefox", "webkit", "all"),
        default="chromium",
    )
    result.add_argument("--scope", choices=("channel", "full"), default="channel")
    result.add_argument("--evidence", type=Path, metavar="FILE")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        steps = selected_steps(args.from_step, args.scope)
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
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        print("NOT_READY channel.browser: install playwright==1.61.0", file=sys.stderr)
        return 3

    browsers = ("chromium", "firefox", "webkit") if args.browser == "all" else (args.browser,)
    results: list[RunEvidence] = []
    dependencies: dict[str, dict[str, Any]] = channel_dependency_status()
    try:
        if args.scope == "full":
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            from tests.browser.identity_harness import IdentityHarness

            with IdentityHarness(
                agent_origin=AGENT_ORIGIN,
                mode5_agent_origin=MODE5_AGENT_ORIGIN,
                host_origin=HOST_A,
            ) as harness:
                with DemoServices(harness.api_targets), sync_playwright() as playwright:
                    for browser_name in browsers:
                        results.append(
                            run_browser(
                                playwright,
                                browser_name,
                                bind_identity_steps(steps, harness, browser_name),
                                headless=os.environ.get("HEADLESS") == "1",
                                slow_mo=args.slow_mo,
                                pause=not args.no_pause,
                                screenshots=args.screenshots,
                            )
                        )
                dependencies = identity_dependency_status(harness)
        else:
            with DemoServices(), sync_playwright() as playwright:
                for browser_name in browsers:
                    results.append(
                        run_browser(
                            playwright,
                            browser_name,
                            steps,
                            headless=os.environ.get("HEADLESS") == "1",
                            slow_mo=args.slow_mo,
                            pause=not args.no_pause,
                            screenshots=args.screenshots,
                        )
                    )
    except DimensionFailure as error:
        print(f"FAIL {error.dimension}: {error}", file=sys.stderr)
        return 1
    except (AssertionError, RuntimeError, PlaywrightError, KeyboardInterrupt, OSError) as error:
        print(f"FAIL channel.browser: {error}", file=sys.stderr)
        return 1

    evidence_document = {
        "schema_version": 1,
        "claim_boundary": (
            "channel and identity portability browser evidence"
            if args.scope == "full"
            else "channel portability and browser-boundary evidence only"
        ),
        "channel": {
            "status": "PASS",
            "browsers": [
                {
                    "name": result.browser,
                    "loader_sri": result.loader_integrity,
                    "loader_sha256": result.loader_sha256,
                    "completed_steps": result.completed_steps,
                    "observations": result.observations,
                }
                for result in results
            ],
        },
        "dependencies": dependencies,
    }
    if args.evidence is not None:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence_document, indent=2) + "\n")
    print("PASS channel: two registered origins used one immutable artifact and strict fallback")
    if args.scope == "full":
        failed = {name: item for name, item in dependencies.items() if item["status"] != "PASS"}
        if failed:
            for dimension, item in failed.items():
                print(f"FAIL {dimension}: {item['reason']}", file=sys.stderr)
            return 1
        print("PASS identity.mode4: direct institutional token evidence is ready")
        print("PASS identity.mode5: BFF embedded-grant evidence is ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
