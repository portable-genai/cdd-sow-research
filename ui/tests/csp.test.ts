// Unit cover for the ONE policy module, `lib/csp.mjs`.
//
// These tests are NOT sufficient and must never be treated as if they were. Everything here
// decides is whether a STRING is well formed. The defect this module exists to remove was
// invisible to exactly that kind of check: the header was right, the types were right, the build
// succeeded and the page rendered, while React never attached because the markup and the header
// disagreed about the nonce. Only `scripts/assert-hydratable.mjs`, which starts the built server
// and reads the bytes it serves, can see that. This file covers the half a string can answer.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ConfiguredEmptyError,
  UnhydratableCspError,
  assertHydratableCsp,
  contentSecurityPolicy,
  frameAncestors,
  frameOptions,
  generateNonce,
} from "../lib/csp.mjs";

const PROD = { NODE_ENV: "production" } as Record<string, string | undefined>;

function directives(csp: string): Map<string, string> {
  return new Map(
    csp
      .split(";")
      .map((part) => part.trim())
      .filter(Boolean)
      .map((part) => {
        const [name, ...value] = part.split(/\s+/);
        return [name.toLowerCase(), value.join(" ")] as [string, string];
      }),
  );
}

test("the policy carries every directive the C6a baseline requires", () => {
  const parsed = directives(contentSecurityPolicy(PROD, "n0nce"));
  for (const name of [
    "default-src",
    "base-uri",
    "form-action",
    "object-src",
    "script-src",
    "style-src",
    "img-src",
    "font-src",
    "connect-src",
    "worker-src",
    "frame-ancestors",
  ]) {
    assert.ok(parsed.has(name), `missing ${name}`);
  }
  assert.equal(parsed.get("object-src"), "'none'");
  assert.equal(parsed.get("base-uri"), "'self'");
});

test("no directive is ever emitted empty, whatever the caller passes", () => {
  const policies = [
    contentSecurityPolicy(PROD, "n0nce"),
    contentSecurityPolicy(PROD),
    contentSecurityPolicy({}, "n0nce"),
    contentSecurityPolicy(PROD, "n0nce", "https://portal.demo-bank.example"),
  ];
  for (const csp of policies) {
    for (const [name, value] of directives(csp)) {
      // An empty directive is a parse error browsers discard, which silently removes the
      // restriction rather than tightening it.
      assert.notEqual(value, "", `${name} is empty in: ${csp}`);
    }
  }
});

test("script-src takes the nonce and strict-dynamic only when a nonce is supplied", () => {
  const withNonce = directives(contentSecurityPolicy(PROD, "abc123")).get("script-src");
  assert.equal(withNonce, "'self' 'nonce-abc123' 'strict-dynamic'");
  assert.equal(directives(contentSecurityPolicy(PROD)).get("script-src"), "'self'");
});

test("a production policy never allows inline or eval scripts", () => {
  const scriptSrc = directives(contentSecurityPolicy(PROD, "abc123")).get("script-src") ?? "";
  assert.ok(!scriptSrc.includes("'unsafe-inline'"));
  assert.ok(!scriptSrc.includes("'unsafe-eval'"));
});

test("frame-ancestors resolves in three states, mirroring the FastAPI service", () => {
  assert.equal(frameAncestors({}), "'self'");
  assert.equal(
    frameAncestors({ CDD_FRAME_ANCESTORS: "https://portal.demo-bank.example" }),
    "https://portal.demo-bank.example",
  );
  assert.equal(frameAncestors({ CDD_FRAME_ANCESTORS: "'none'" }), "'none'");
  assert.throws(() => frameAncestors({ CDD_FRAME_ANCESTORS: "" }), ConfiguredEmptyError);
  assert.throws(() => frameAncestors({ CDD_FRAME_ANCESTORS: "   " }), ConfiguredEmptyError);
  assert.throws(() => frameAncestors({ CDD_FRAME_ANCESTORS: " , , " }), ConfiguredEmptyError);
});

test("a wildcard framing policy is refused rather than downgraded to the default", () => {
  assert.throws(() => frameAncestors({ CDD_FRAME_ANCESTORS: "*" }), ConfiguredEmptyError);
  assert.throws(
    () => frameAncestors({ CDD_FRAME_ANCESTORS: "https://portal.demo-bank.example *" }),
    ConfiguredEmptyError,
  );
  assert.throws(
    () => frameAncestors({ CDD_FRAME_ANCESTORS: "'none' https://portal.demo-bank.example" }),
    ConfiguredEmptyError,
  );
});

test("a wildcard hiding inside an origin is refused too", () => {
  // The refusal matched a fixed set of exact tokens, so a host-source wildcard was not in it and
  // was emitted verbatim. CSP honours that form, so every subdomain could frame the console,
  // including one obtained by takeover or served as user content. These are the shapes that
  // passed.
  for (const configured of [
    "https://*.demo-bank.example",
    "https://portal.demo-bank.example https://*.evil.example",
    "*.demo-bank.example",
    "https://*",
  ]) {
    assert.throws(
      () => frameAncestors({ CDD_FRAME_ANCESTORS: configured }),
      ConfiguredEmptyError,
      `expected ${configured} to be refused`,
    );
  }
});

test("the wildcard refusal leaves a legitimate allowlist alone", () => {
  // A refusal that also turns away valid configuration is an outage, not a control.
  assert.equal(
    frameAncestors({
      CDD_FRAME_ANCESTORS: "https://portal.demo-bank.example https://ops.demo-bank.example",
    }),
    "https://portal.demo-bank.example https://ops.demo-bank.example",
  );
  assert.equal(frameAncestors({ CDD_FRAME_ANCESTORS: "'none'" }), "'none'");
  assert.equal(frameAncestors({}), "'self'");
});

test("an embed document's registered parents override the deployment-wide value", () => {
  const csp = contentSecurityPolicy(
    { ...PROD, CDD_FRAME_ANCESTORS: "https://elsewhere.example" },
    "n0nce",
    "https://portal.demo-bank.example https://ops.demo-bank.example",
  );
  assert.equal(
    directives(csp).get("frame-ancestors"),
    "https://portal.demo-bank.example https://ops.demo-bank.example",
  );
});

test("X-Frame-Options is sent only for the two policies it can express", () => {
  assert.equal(frameOptions("'self'"), "SAMEORIGIN");
  assert.equal(frameOptions("'none'"), "DENY");
  assert.equal(frameOptions("https://portal.demo-bank.example"), "");
});

test("nonces are unique and base64", () => {
  const seen = new Set<string>();
  for (let i = 0; i < 50; i += 1) {
    const nonce = generateNonce();
    assert.match(nonce, /^[A-Za-z0-9+/]+={0,2}$/);
    seen.add(nonce);
  }
  assert.equal(seen.size, 50);
});

test("the build refuses a nonce policy on a statically prerendered layout", () => {
  assert.throws(
    () => assertHydratableCsp('export const metadata = {};\nexport default function L() {}\n'),
    UnhydratableCspError,
  );
  assertHydratableCsp('export const dynamic = "force-dynamic";\n');
});
