import { readFileSync } from "node:fs";

import { assertHydratableCsp, frameAncestors } from "./lib/csp.mjs";
import { readEnvSetting } from "./lib/env-setting.mjs";

// Evaluated by both `next build` and `next start`, so a policy nobody chose, or a nonce the
// rendered HTML could never carry, refuses here rather than surprising a browser later. The CSP
// itself is NOT emitted from this file: `proxy.ts` is the single enforcement point, because a
// static header table cannot express a per-request nonce and a second policy would be intersected
// against the first.
frameAncestors(process.env);
assertHydratableCsp(readFileSync(new URL("./app/layout.tsx", import.meta.url), "utf8"));

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
        source: "/assets/pdfjs/4.10.38/pdf.worker.min.mjs",
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
