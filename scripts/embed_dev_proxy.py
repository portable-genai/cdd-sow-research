#!/usr/bin/env python3
"""Minimal prefix-stripping proxy for the canonical public Mode 6 auth route.

It exposes ``/agent/auth/*`` and forwards to the API's internal ``/auth/*`` route while
preserving query strings and browser cookie headers. It is a local test fixture, not a
production ingress.
"""

from __future__ import annotations

import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import SplitResult, urlsplit, urlunsplit

PUBLIC_AUTH_PREFIX = "/agent/auth"
INTERNAL_AUTH_PREFIX = "/auth"
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


def internal_auth_target(target: str) -> str:
    """Map one canonical public auth target to its internal API target."""
    parsed = urlsplit(target)
    if parsed.path != PUBLIC_AUTH_PREFIX and not parsed.path.startswith(f"{PUBLIC_AUTH_PREFIX}/"):
        raise ValueError(f"target is outside {PUBLIC_AUTH_PREFIX}")
    internal_path = INTERNAL_AUTH_PREFIX + parsed.path.removeprefix(PUBLIC_AUTH_PREFIX)
    return urlunsplit(SplitResult("", "", internal_path, parsed.query, ""))


def _handler(upstream: str) -> type[BaseHTTPRequestHandler]:
    parsed_upstream = urlsplit(upstream)
    if parsed_upstream.scheme not in {"http", "https"} or not parsed_upstream.hostname:
        raise ValueError("upstream must be an http(s) origin")
    connection_type = (
        http.client.HTTPSConnection
        if parsed_upstream.scheme == "https"
        else http.client.HTTPConnection
    )
    port = parsed_upstream.port or (443 if parsed_upstream.scheme == "https" else 80)

    class AuthProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            self._proxy()

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            self._proxy()

        def _proxy(self) -> None:
            try:
                target = internal_auth_target(self.path)
            except ValueError:
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length else None
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in _HOP_BY_HOP and key.lower() != "host"
            }
            headers["Host"] = parsed_upstream.netloc
            connection = connection_type(parsed_upstream.hostname, port, timeout=10)
            try:
                connection.request(self.command, target, body=body, headers=headers)
                response = connection.getresponse()
                payload = response.read()
                self.send_response(response.status, response.reason)
                for key, value in response.getheaders():
                    if key.lower() not in _HOP_BY_HOP and key.lower() != "content-length":
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            finally:
                connection.close()

    return AuthProxyHandler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=8099)
    parser.add_argument("--upstream", default="http://127.0.0.1:8090")
    args = parser.parse_args()
    server = ThreadingHTTPServer(
        (args.listen_host, args.listen_port),
        _handler(args.upstream),
    )
    print(
        f"proxying http://{args.listen_host}:{args.listen_port}{PUBLIC_AUTH_PREFIX}/* "
        f"to {args.upstream}{INTERNAL_AUTH_PREFIX}/*"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
