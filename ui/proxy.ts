import { type NextRequest, NextResponse } from "next/server";

import { contentSecurityPolicy, frameAncestors, frameOptions, generateNonce } from "./lib/csp.mjs";
import { resolveInstallation } from "./lib/server/installations";

/**
 * The single enforcement point for the console's Content-Security-Policy.
 *
 * Every response gets the policy, and it is minted PER REQUEST because it carries a script nonce.
 * `next.config.mjs` deliberately emits no CSP: two layers both setting the header give the browser
 * two independent policies to satisfy, the stricter wins per directive, and the static table's
 * nonce-free `script-src` would quietly intersect away the nonce this function just generated.
 *
 * Both header sets below are required and they do different jobs. The REQUEST header is where Next
 * reads the nonce it stamps onto every script tag it renders; the RESPONSE header is what the
 * browser actually enforces. Setting only the request header proves nothing, and setting only the
 * response header blocks the very scripts the nonce exists to allow.
 */
export async function proxy(request: NextRequest) {
  // Next may expose the path to Proxy with basePath stripped, depending on the
  // runtime adapter. Accept only these two equivalent canonical representations.
  const match = /^(?:\/agent)?\/embed\/([A-Za-z0-9_-]{1,128})\/?$/.exec(request.nextUrl.pathname);
  const nonce = generateNonce();

  if (!match) {
    // The standalone console and everything served beside it. `frame-ancestors` comes from the
    // deployment's own variable, resolved the same three ways the FastAPI service resolves it.
    const csp = contentSecurityPolicy(process.env, nonce);
    const requestHeaders = new Headers(request.headers);
    requestHeaders.set("Content-Security-Policy", csp);
    requestHeaders.set("x-nonce", nonce);
    const response = NextResponse.next({ request: { headers: requestHeaders } });
    response.headers.set("Content-Security-Policy", csp);
    response.headers.set("Referrer-Policy", "no-referrer");
    response.headers.set("X-Content-Type-Options", "nosniff");
    const legacy = frameOptions(frameAncestors(process.env));
    if (legacy) response.headers.set("X-Frame-Options", legacy);
    return response;
  }

  const runtime = await resolveInstallation(match[1]);
  if (!runtime) {
    return new NextResponse("Not found", {
      status: 404,
      headers: {
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
      },
    });
  }

  // An embed document is framed by exactly the parent origins the reviewed manifest registered
  // for that installation, so its `frame-ancestors` overrides the deployment-wide value. No
  // `X-Frame-Options` accompanies a named allowlist: the legacy header cannot express one, and
  // sending SAMEORIGIN beside it would contradict the CSP in an older agent.
  const csp = contentSecurityPolicy(process.env, nonce, runtime.parentOrigins.join(" "));
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("Content-Security-Policy", csp);
  requestHeaders.set("x-nonce", nonce);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  response.headers.set("Cache-Control", "private, no-store");
  response.headers.set("Referrer-Policy", "no-referrer");
  response.headers.set("X-Content-Type-Options", "nosniff");
  return response;
}

export const config = {
  // Match the complete fixed-basePath surface, then fail closed only for the
  // exact dynamic embed document selected above.
  matcher: ["/:path*"],
};
