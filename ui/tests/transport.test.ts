import assert from "node:assert/strict";
import { test } from "node:test";

import {
  AuthenticatedTransport,
  AuthRequiredError,
  CANONICAL_API_BASE,
  type TransportSignal,
} from "../lib/embed/transport.ts";

test("one authenticated transport covers JSON, multipart, and blob requests", async () => {
  const calls: { input: string; init: RequestInit }[] = [];
  const fetchMock = async (input: string | URL | Request, init?: RequestInit) => {
    calls.push({ input: String(input), init: init ?? {} });
    const path = String(input);
    if (path.endsWith("/document")) {
      return new Response(new Blob(["evidence"], { type: "text/plain" }), {
        headers: { "content-type": "text/plain", "content-length": "8" },
      });
    }
    return Response.json({ ok: true });
  };
  const transport = new AuthenticatedTransport(
    {
      installationId: "inst_host_a",
      manifestDigest: "a".repeat(64),
      accessToken: "access-token",
    },
    fetchMock as typeof fetch,
  );

  await transport.json("/v1/cdd", { method: "POST", body: JSON.stringify({ subject: "x" }) });
  const multipart = new FormData();
  multipart.append("file", new Blob(["file"]), "evidence.txt");
  await transport.multipart("/v1/documents", multipart, { method: "POST" });
  const document_ = await transport.blob("/v1/document");

  assert.equal(document_.contentType, "text/plain");
  assert.equal(document_.contentLength, 8);
  assert.deepEqual(
    calls.map((call) => call.input),
    [
      `${CANONICAL_API_BASE}/v1/cdd`,
      `${CANONICAL_API_BASE}/v1/documents`,
      `${CANONICAL_API_BASE}/v1/document`,
    ],
  );
  for (const call of calls) {
    const headers = new Headers(call.init.headers);
    assert.equal(headers.get("authorization"), "Bearer access-token");
    assert.equal(headers.get("x-cdd-installation-id"), "inst_host_a");
    assert.equal(headers.get("x-cdd-manifest-sha256"), "a".repeat(64));
    assert.equal(call.init.credentials, "same-origin");
    assert.equal(call.init.cache, "no-store");
  }
  assert.equal(new Headers(calls[0].init.headers).get("content-type"), "application/json");
  assert.equal(new Headers(calls[1].init.headers).has("content-type"), false);
});

test("authentication failures emit a structured fallback signal and clear the token", async () => {
  const signals: TransportSignal[] = [];
  const authorizations: (string | null)[] = [];
  const fetchMock = async (_input: string | URL | Request, init?: RequestInit) => {
    authorizations.push(new Headers(init?.headers).get("authorization"));
    return authorizations.length === 1
      ? new Response("", { status: 401 })
      : Response.json({ ok: true });
  };
  const transport = new AuthenticatedTransport(
    {
      installationId: "inst_host_a",
      accessToken: "rejected-token",
      onSignal: (signal) => signals.push(signal),
    },
    fetchMock as typeof fetch,
  );

  await assert.rejects(() => transport.json("/v1/cdd"), AuthRequiredError);
  await transport.json("/v1/version");

  assert.deepEqual(signals, [
    {
      type: "authentication",
      code: "authentication_required",
      fallbackRequired: true,
    },
  ]);
  assert.deepEqual(authorizations, ["Bearer rejected-token", null]);
});

test("transport rejects non-rooted API paths", async () => {
  const transport = new AuthenticatedTransport({}, async () => Response.json({}));
  await assert.rejects(() => transport.json("https://attacker.example"), /must start/);
});

test("transport invokes the captured browser fetch with its required global receiver", async () => {
  let receiver: unknown;
  async function receiverSensitiveFetch(
    this: unknown,
    _input: string | URL | Request,
    _init?: RequestInit,
  ): Promise<Response> {
    receiver = this;
    if (this !== globalThis) throw new TypeError("Illegal invocation");
    return Response.json({ ok: true });
  }
  const transport = new AuthenticatedTransport(
    {},
    receiverSensitiveFetch as typeof fetch,
  );

  await transport.json("/v1/version");

  assert.equal(receiver, globalThis);
});

test("Mode 6 bootstraps exact in-memory CSRF tokens for every unsafe transport shape", async () => {
  const calls: { receiver: unknown; input: string; init: RequestInit }[] = [];
  let tokenNumber = 0;
  async function receiverSensitiveFetch(
    this: unknown,
    input: string | URL | Request,
    init?: RequestInit,
  ): Promise<Response> {
    calls.push({ receiver: this, input: String(input), init: init ?? {} });
    if (String(input).includes("/auth/csrf?")) {
      tokenNumber += 1;
      return Response.json(
        { csrf_token: `csrf-token-${tokenNumber}-${"x".repeat(32)}` },
        { headers: { "cache-control": "private, no-store" } },
      );
    }
    return Response.json({ ok: true });
  }
  const transport = new AuthenticatedTransport(
    { identityMode: "oidc-session" },
    receiverSensitiveFetch as typeof fetch,
  );

  await transport.json("/v1/cdd", {
    method: "POST",
    body: JSON.stringify({ subject: "x" }),
  });
  const multipart = new FormData();
  multipart.append("file", new Blob(["evidence"]), "evidence.txt");
  await transport.multipart("/v1/cases/case-1/documents", multipart, {
    method: "POST",
  });
  await transport.json("/v1/cases/case-1/documents/doc-1", {
    method: "DELETE",
  });

  assert.deepEqual(
    calls.map((call) => call.input),
    [
      `${CANONICAL_API_BASE}/auth/csrf?method=POST&path=%2Fv1%2Fcdd`,
      `${CANONICAL_API_BASE}/v1/cdd`,
      `${CANONICAL_API_BASE}/auth/csrf?method=POST&path=%2Fv1%2Fcases%2Fcase-1%2Fdocuments`,
      `${CANONICAL_API_BASE}/v1/cases/case-1/documents`,
      `${CANONICAL_API_BASE}/auth/csrf?method=DELETE&path=%2Fv1%2Fcases%2Fcase-1%2Fdocuments%2Fdoc-1`,
      `${CANONICAL_API_BASE}/v1/cases/case-1/documents/doc-1`,
    ],
  );
  assert.ok(calls.every((call) => call.receiver === globalThis));
  assert.equal(
    new Headers(calls[1].init.headers).get("x-csrf-token"),
    `csrf-token-1-${"x".repeat(32)}`,
  );
  assert.equal(
    new Headers(calls[3].init.headers).get("x-csrf-token"),
    `csrf-token-2-${"x".repeat(32)}`,
  );
  assert.equal(
    new Headers(calls[5].init.headers).get("x-csrf-token"),
    `csrf-token-3-${"x".repeat(32)}`,
  );
  assert.equal(new Headers(calls[3].init.headers).has("content-type"), false);
  assert.ok(
    calls.every(
      (call) =>
        !call.input.includes("csrf-token") &&
        !String(call.init.body ?? "").includes("csrf-token"),
    ),
  );
});

test("Mode 6 discards and refreshes a rejected action token once", async () => {
  const actionTokens: (string | null)[] = [];
  let bootstrapCount = 0;
  const fetchMock = async (input: string | URL | Request, init?: RequestInit) => {
    if (String(input).includes("/auth/csrf?")) {
      bootstrapCount += 1;
      return Response.json(
        { csrf_token: `csrf-token-${bootstrapCount}-${"x".repeat(32)}` },
        { headers: { "cache-control": "private, no-store" } },
      );
    }
    actionTokens.push(new Headers(init?.headers).get("x-csrf-token"));
    return actionTokens.length === 1
      ? Response.json({ detail: "CSRF token is invalid" }, { status: 403 })
      : Response.json({ ok: true });
  };
  const transport = new AuthenticatedTransport(
    { identityMode: "oidc-session" },
    fetchMock as typeof fetch,
  );

  await transport.json("/v1/cdd", { method: "POST", body: "{}" });

  assert.equal(bootstrapCount, 2);
  assert.deepEqual(actionTokens, [
    `csrf-token-1-${"x".repeat(32)}`,
    `csrf-token-2-${"x".repeat(32)}`,
  ]);
});

test("authorization denials are not retried or converted into authentication fallback", async () => {
  for (const identityMode of ["oidc-session", "oauth-access-token"]) {
    const calls: string[] = [];
    const signals: TransportSignal[] = [];
    const fetchMock = async (input: string | URL | Request) => {
      calls.push(String(input));
      if (String(input).includes("/auth/csrf?")) {
        return Response.json(
          { csrf_token: `csrf-token-${"x".repeat(32)}` },
          { headers: { "cache-control": "private, no-store" } },
        );
      }
      return Response.json(
        { detail: "verified identity lacks required scope: cdd.write" },
        { status: 403 },
      );
    };
    const transport = new AuthenticatedTransport(
      {
        identityMode,
        accessToken: identityMode === "oauth-access-token" ? "valid-read-only-token" : "",
        onSignal: (signal) => signals.push(signal),
      },
      fetchMock as typeof fetch,
    );

    await assert.rejects(
      () => transport.json("/v1/cdd", { method: "POST", body: "{}" }),
      (error: unknown) =>
        error instanceof Error &&
        !(error instanceof AuthRequiredError) &&
        error.message.includes("lacks required scope"),
    );

    assert.equal(
      calls.filter((call) => call.endsWith("/v1/cdd")).length,
      1,
    );
    assert.deepEqual(signals, [
      {
        type: "error",
        code: "authorization_denied",
        recoverable: false,
      },
    ]);
  }
});

test("non-session identity modes do not bootstrap browser CSRF tokens", async () => {
  for (const identityMode of [
    "local-persona",
    "iap",
    "oauth-access-token",
    "embedded-grant",
  ]) {
    const calls: string[] = [];
    const transport = new AuthenticatedTransport(
      {
        identityMode,
        accessToken: identityMode.includes("token") ? "host-token" : "",
      },
      (async (input: string | URL | Request) => {
        calls.push(String(input));
        return Response.json({ ok: true });
      }) as typeof fetch,
    );

    await transport.json("/v1/cdd", { method: "POST", body: "{}" });

    assert.deepEqual(calls, [`${CANONICAL_API_BASE}/v1/cdd`]);
  }
});
