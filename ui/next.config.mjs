import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

import { assertHydratableCsp, frameAncestors } from "./lib/csp.mjs";
import { readEnvSetting } from "./lib/env-setting.mjs";

// Evaluated by both `next build` and `next start`, so a policy nobody chose, or a nonce the
// rendered HTML could never carry, refuses here rather than surprising a browser later. The CSP
// itself is NOT emitted from this file: `proxy.ts` is the single enforcement point, because a
// static header table cannot express a per-request nonce and a second policy would be intersected
// against the first.
frameAncestors(process.env);
assertHydratableCsp(readFileSync(new URL("./app/layout.tsx", import.meta.url), "utf8"));

// pdfjs-dist's own version, not a repeated literal: DocumentViewerModal.tsx requests the worker
// keyed by this same value read off the library, and build-loader.mjs vendors it to the matching
// path. A stale literal here would leave the served worker's Cache-Control and Content-Type
// headers pointing at a path the app no longer requests.
const pdfjsVersion = createRequire(import.meta.url)("pdfjs-dist/package.json").version;

/** @type {import('next').NextConfig} */
const basePath = "/agent";
const isDev = process.env.NODE_ENV !== "production";
// Three states. This names a HOST that the rewrites proxy to, so an operator who empties it must
// not silently inherit the development loopback: on a dev build that would send proxied traffic
// to a local process nobody chose, and the emptied deployment would be byte-identical to one that
// never configured the variable.
const apiOriginSetting = readEnvSetting(process.env, "CDD_API_INTERNAL_ORIGIN");
if (apiOriginSetting.isConfiguredEmpty) {
  throw new Error(
    "CDD_API_INTERNAL_ORIGIN is set to an empty value. An emptied variable names no origin, so " +
      "it cannot inherit the unset default. Unset it to take that default deliberately, or give " +
      "it the internal API origin this deployment proxies to.",
  );
}
const apiOrigin = (
  apiOriginSetting.hasValue
    ? apiOriginSetting.value
    : isDev
      ? "http://127.0.0.1:8090"
      : ""
).replace(/\/$/, "");

const nextConfig = {
  reactStrictMode: true,
  // `next dev` writes AGENTS.md and CLAUDE.md into this directory unless this is false; the
  // writer is node_modules/next/dist/server/lib/generate-agent-files.js. This repo's working
  // agreement is the AGENTS.md at its root and there is no tool-specific alias of it, so a
  // second one here is a second agreement to keep in step and CLAUDE.md is precisely the alias
  // the convention forbids. The generated prose also carries an em-dash, which the catalog's
  // house style forbids in shipped markdown. tests/unit/test_ui_agent_documents.py fails the
  // gate if this line goes away or if either file turns up on disk anyway.
  agentRules: false,
  basePath,
  assetPrefix: basePath,
  output: "standalone",
  async rewrites() {
    if (!apiOrigin) return [];
    return [
      {
        source: "/api/:path*",
        destination: `${apiOrigin}/:path*`,
      },
    ];
  },
  async headers() {
    return [
      {
        source: "/embed/v1/cdd-agent.js",
        headers: [
          { key: "Access-Control-Allow-Origin", value: "*" },
          { key: "Cross-Origin-Resource-Policy", value: "cross-origin" },
          { key: "Cache-Control", value: "public, max-age=31536000, immutable" },
          { key: "Content-Type", value: "text/javascript; charset=utf-8" },
          { key: "X-Content-Type-Options", value: "nosniff" },
        ],
      },
      {
        source: `/assets/pdfjs/${pdfjsVersion}/pdf.worker.min.mjs`,
        headers: [
          { key: "Cache-Control", value: "public, max-age=31536000, immutable" },
          { key: "Content-Type", value: "text/javascript; charset=utf-8" },
          { key: "X-Content-Type-Options", value: "nosniff" },
        ],
      },
      {
        // Only the statically expressible half of the security baseline. The CSP is absent on
        // purpose: see the refusal at the top of this file.
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "no-referrer" },
        ],
      },
    ];
  },
};

export default nextConfig;
