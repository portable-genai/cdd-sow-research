#!/usr/bin/env node
// Prove, against a real production server, that every shipped page can hydrate.
//
// Everything cheaper than this has been fooled by the defect it catches. Unit tests assert the
// CSP string, and the string was right. `tsc` was clean. `next build` succeeded. The page
// rendered, the headers were correct, and a screenshot looked exactly like a working console.
// What was shipped was dead markup: a `script-src` with no usable allowance for Next's INLINE
// hydration bootstrap means `__next_f` never fills, React never attaches, and no control on the
// page does anything.
//
// So this check refuses to reason about the policy at all. It starts the BUILT server, fetches
// the documents a browser would fetch, and asserts three things about the bytes that come back:
//
//   1. The response CSP declares every directive the C6a baseline requires, and none of them is
//      empty (an empty directive is a parse error browsers discard, which silently removes the
//      restriction the directive was there to impose).
//   2. The response carries a nonce in `script-src`.
//   3. EVERY `<script>` tag in the document carries that same nonce.
//
// Rule 3 is the one that matters, and it is the one a header assertion cannot express. A
// statically prerendered page was built before the nonce existed, so it emits script tags with no
// nonce while the header advertises one, and because `'strict-dynamic'` disables the `'self'`
// fallback, that combination blocks strictly MORE than the unfixed policy did. Header and markup
// have to agree, and only the markup knows.
//
// BOTH surfaces are checked: the standalone console at `/agent/` and one per-installation embed
// document at `/agent/embed/<id>`. The embed route was already nonced correctly before the
// console was, so it is here as a regression guard, not as a fix.
//
// Usage: node scripts/assert-hydratable.mjs [port]
// Expects `next build` to have run. Exits non-zero with the reason on any failure.

import { spawn } from "node:child_process";
import { readEnvSetting } from "../lib/env-setting.mjs";

const manifestSetting = readEnvSetting(process.env, "CDD_INSTALLATION_MANIFEST");

const REQUESTED_PORT = process.argv[2] ?? "0";
if (!/^\d+$/.test(REQUESTED_PORT)) {
  throw new Error("port must be a non-negative integer");
}
const BOOT_TIMEOUT_MS = 90_000;
const POLL_MS = 250;

/** Directives the C6a baseline requires on every document response. */
const REQUIRED_DIRECTIVES = [
  "default-src",
  "script-src",
  "object-src",
  "base-uri",
  "frame-ancestors",
];

/**
 * Environment the built server needs. The embed document resolves a per-installation manifest, so
 * the fictional example manifest is pointed at explicitly rather than inherited from a shell.
 */
const SERVER_ENV = {
  ...process.env,
  NEXT_TELEMETRY_DISABLED: "1",
  CDD_INSTALLATION_MANIFEST:
    // Three states: unset takes the documented fixture, emptied refuses. A build check that
    // silently fell back to the example manifest when somebody emptied the variable would be
    // asserting hydration against a document nobody deployed.
    manifestSetting.isConfiguredEmpty
      ? (() => {
          throw new Error("CDD_INSTALLATION_MANIFEST is set to an empty value");
        })()
      : manifestSetting.hasValue
        ? manifestSetting.value
        : "config/installations.example.json",
};

let failed = false;

function fail(message) {
  console.error(`FAIL ${message}`);
  failed = true;
  process.exitCode = 1;
}

async function waitForServer(url, deadline) {
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { redirect: "manual" });
      if (response.status < 500) return response;
    } catch {
      // Not listening yet.
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
  return null;
}

/** Split a CSP header into its directives, lowercased by name. */
function parseDirectives(csp) {
  return new Map(
    csp
      .split(";")
      .map((part) => part.trim())
      .filter(Boolean)
      .map((part) => {
        const [name, ...value] = part.split(/\s+/);
        return [name.toLowerCase(), value.join(" ")];
      }),
  );
}

async function checkDocument(label, url) {
  const response = await fetch(url, { redirect: "manual" });
  if (response.status !== 200) {
    fail(`${label}: ${url} answered ${response.status}, so no document was served to check`);
    return;
  }
  const csp = response.headers.get("content-security-policy") ?? "";
  const html = await response.text();

  // Two layers both emitting a CSP is itself the defect. `Headers.get` joins repeated headers
  // with a comma, and a comma is exactly how CSP separates two INDEPENDENT policies: the browser
  // enforces both and the stricter wins per directive, which is how a nonce policy gets quietly
  // reverted by a leftover static header table.
  if (csp.includes(",")) {
    fail(
      `${label}: the response carries TWO Content-Security-Policy policies, which the browser ` +
        `intersects rather than merges. Exactly one layer may emit it. CSP: ${csp}`,
    );
  }

  const directives = parseDirectives(csp);
  for (const name of REQUIRED_DIRECTIVES) {
    if (!directives.has(name)) {
      fail(`${label}: the response CSP has no \`${name}\` directive at all. CSP: ${csp || "(none)"}`);
    }
  }
  for (const [name, value] of directives) {
    if (!value) {
      fail(
        `${label}: the CSP directive \`${name}\` is empty, which browsers discard as a parse ` +
          `error, silently removing the restriction. CSP: ${csp}`,
      );
    }
  }

  const scriptSrc = directives.get("script-src") ?? "";
  if (scriptSrc.includes("'unsafe-inline'")) {
    fail(
      `${label}: \`script-src\` carries 'unsafe-inline', which allows any injected inline ` +
        `script. The nonce path exists so it does not have to. CSP: ${csp}`,
    );
  }

  const nonceInHeader = csp.match(/'nonce-([^']+)'/)?.[1];
  if (!nonceInHeader) {
    fail(
      `${label}: no nonce in the response CSP, so Next's inline hydration bootstrap is blocked, ` +
        `\`__next_f\` never fills and React never attaches. CSP: ${csp || "(none)"}`,
    );
  }

  const scriptTags = html.match(/<script\b[^>]*>/g) ?? [];
  if (scriptTags.length === 0) {
    fail(`${label}: the document carries no script tags at all, which is not a hydrating page`);
    return;
  }

  const unnonced = scriptTags.filter((tag) => !tag.includes(`nonce="${nonceInHeader}"`));
  if (nonceInHeader && unnonced.length > 0) {
    fail(
      `${label}: ${unnonced.length} of ${scriptTags.length} script tags do not carry the CSP ` +
        "nonce, so the browser blocks them and the page never hydrates. This is what a " +
        "statically prerendered route looks like: check that the route's layout or page sets " +
        '`export const dynamic = "force-dynamic"`.\n  ' +
        unnonced.slice(0, 3).join("\n  "),
    );
    return;
  }

  if (nonceInHeader) {
    console.log(
      `OK ${label}: every one of the ${scriptTags.length} script tags carries the CSP nonce; ` +
        "the page hydrates.",
    );
  }
}

const server = spawn("npx", ["next", "start", "-p", REQUESTED_PORT], {
  cwd: new URL("..", import.meta.url),
  env: SERVER_ENV,
  stdio: ["ignore", "pipe", "pipe"],
});
let serverLog = "";
let reportedPort = null;
let exited = false;
function capture(chunk) {
  const text = chunk.toString();
  serverLog += text;
  const match = text.match(/http:\/\/localhost:(\d+)/);
  if (match) reportedPort = Number(match[1]);
}
server.stdout.on("data", capture);
server.stderr.on("data", capture);
server.on("exit", () => {
  exited = true;
});

async function waitForReportedPort(deadline) {
  while (Date.now() < deadline && reportedPort === null && !exited) {
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
  return reportedPort;
}

try {
  const port = await waitForReportedPort(Date.now() + BOOT_TIMEOUT_MS);
  if (port === null) throw new Error(`this Next child never reported a bound port\n${serverLog}`);
  if (REQUESTED_PORT !== "0" && port !== Number(REQUESTED_PORT)) {
    throw new Error(`requested ${REQUESTED_PORT}, but this child bound ${port}`);
  }
  const base = `http://127.0.0.1:${port}/agent`;
  const ready = await waitForServer(base, Date.now() + BOOT_TIMEOUT_MS);
  if (exited) {
    fail(`this Next child exited before its document was checked\n${serverLog}`);
  } else if (!ready) {
    fail(`the built server never answered on ${base} within ${BOOT_TIMEOUT_MS}ms\n${serverLog}`);
  } else {
    await checkDocument("standalone console", base);
    await checkDocument("embed document", `${base}/embed/inst_demo_bank`);
  }
} finally {
  server.kill("SIGTERM");
}

if (!failed) {
  console.log("OK both the standalone console and the embed document hydrate under the nonce CSP.");
}
