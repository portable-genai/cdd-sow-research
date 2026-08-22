// The console's Content-Security-Policy, built in ONE module so it is written once and read by
// every layer that needs it.
//
// Before this module there were two policies. `next.config.mjs` emitted a static `headers()`
// entry carrying `script-src 'self' 'unsafe-inline'`, and `proxy.ts` built a second, nonce-based
// policy inline for the per-installation embed documents only. The embed half was right; the
// standalone console half was not. A static header table cannot express a per-request value, and
// a script nonce is exactly that, so the console's policy fell back to `'unsafe-inline'`: an
// allowance broad enough that any injected inline script runs, granted in order to unblock Next's
// own inline hydration bootstrap.
//
// Two layers both emitting `Content-Security-Policy` is its own defect. The browser enforces both
// policies independently and the stricter wins per directive, so the static table would silently
// intersect away the nonce the proxy had just minted. The policy therefore lives here, the nonce
// is minted per request in `proxy.ts`, and `next.config.mjs` emits no CSP at all.
//
// `frame-ancestors` is the one directive that varies by surface, and it has two sources:
//
//   * the standalone console reads `CDD_FRAME_ANCESTORS`, mirroring the FastAPI service's
//     `WebSecuritySettings.frame_ancestors` exactly, so the two halves of the embedding posture
//     cannot disagree: unset means `'self'`, a named list means those origins, and a variable
//     that is set but names nothing REFUSES rather than silently inheriting the default;
//   * an embed document overrides it with that installation's registered parent origins, which
//     the proxy has already resolved from the reviewed manifest.
//
// Nothing here does I/O or imports a framework, so it is safe to import from the proxy, from the
// config, and from `node --test`.

/** What `frame-ancestors` says when nobody named a parent origin: framed by itself and nothing else. */
export const DEFAULT_FRAME_ANCESTORS = "'self'";

/** Exact tokens that must never be accepted as a framing ancestor.
 *
 * `null` is here because it is not a wildcard by spelling and behaves as one: a sandboxed iframe
 * presents a null origin, so allowing it invites the framing this directive exists to refuse.
 */
const WILDCARD_TOKENS = new Set(["*", "'*'", "null", "*.*"]);

/**
 * True when an entry may not be a framing ancestor.
 *
 * Exact matching alone was not enough. `https://*.example` is not in the token set, so it was
 * accepted and emitted verbatim, and CSP honours a host-source wildcard: every subdomain could
 * frame the console, including one an attacker obtains by takeover or by a user-content
 * subdomain. So ANY entry containing an asterisk is refused, which turns away nothing a
 * deployment could correctly hold, since a real origin never contains the character.
 *
 * @param {string} entry
 * @returns {boolean}
 */
function isWildcard(entry) {
  return WILDCARD_TOKENS.has(entry) || entry.includes("*");
}

/** Raised when an embedding variable is set but selects no origin at all. */
export class ConfiguredEmptyError extends Error {}

/** Raised when the nonce policy and the rendering mode disagree, which serves un-hydratable HTML. */
export class UnhydratableCspError extends Error {}

/**
 * Resolve `frame-ancestors` for the standalone console, in three states.
 *
 * Unset returns the documented default. Set and naming one or more origins returns those. Set but
 * blank, or naming only separators or refused wildcards, THROWS: an operator who empties the
 * variable has expressed an intent, and answering with the shipped default would make a
 * deliberate lockdown indistinguishable from a variable that went missing. It also never emits an
 * empty directive, which browsers discard as a parse error, removing the restriction entirely.
 *
 * Mirrors `WebSecuritySettings.__post_init__` in `src/cdd_sow_research/config.py`, which refuses an
 * empty tuple and refuses a wildcard in the same two ways.
 *
 * @param {Record<string, string | undefined>} env
 * @returns {string}
 */
export function frameAncestors(env) {
  const raw = env.CDD_FRAME_ANCESTORS;
  if (raw === undefined || raw === null) return DEFAULT_FRAME_ANCESTORS;
  const named = [];
  for (const piece of String(raw).split(/[\s,]+/)) {
    const entry = piece.trim();
    if (!entry) continue;
    if (isWildcard(entry)) {
      throw new ConfiguredEmptyError(
        `CDD_FRAME_ANCESTORS contains ${JSON.stringify(entry)}, and a wildcard framing policy ` +
          "permits UI redress from any site. Name the exact parent origins, or unset the " +
          `variable to keep the ${DEFAULT_FRAME_ANCESTORS} default.`,
      );
    }
    if (!named.includes(entry)) named.push(entry);
  }
  if (named.length === 0) {
    throw new ConfiguredEmptyError(
      "CDD_FRAME_ANCESTORS is set but names no origin. An empty frame-ancestors directive is a " +
        "parse error browsers discard, which would remove the framing restriction altogether, " +
        `and inheriting the ${DEFAULT_FRAME_ANCESTORS} default would hide that the variable was ` +
        "deliberately emptied. Unset it to keep the default, or name the parent origins.",
    );
  }
  if (named.includes("'none'") && named.length > 1) {
    throw new ConfiguredEmptyError(
      "CDD_FRAME_ANCESTORS mixes 'none' with named origins. CSP gives 'none' no meaning beside " +
        "an origin, so the combination is refused rather than resolved to whichever half is more " +
        "permissive. Use 'none' alone, or name only the parent origins.",
    );
  }
  return named.join(" ");
}

/**
 * The pre-CSP `X-Frame-Options` spelling of a framing policy, or "" when it has none.
 *
 * A NAMED allowlist has no `X-Frame-Options` equivalent, so none is sent rather than one that
 * contradicts the CSP in an older agent. Mirrors the service's own header logic in
 * `api/app.py::_security_headers`, which sends `SAMEORIGIN` only for the bare `'self'` case.
 *
 * @param {string} ancestors
 * @returns {string}
 */
export function frameOptions(ancestors) {
  if (ancestors === "'self'") return "SAMEORIGIN";
  if (ancestors === "'none'") return "DENY";
  return "";
}

/**
 * The full default-deny policy for one response.
 *
 * `style-src` carries `'unsafe-inline'` because the Next runtime injects critical CSS and there
 * is no nonce path for it. `script-src` does NOT: with a nonce it takes that nonce plus
 * `'strict-dynamic'`, so the nonced bootstrap may load its own chunks and nothing else may run.
 * Passing no nonce yields the strict `'self'` form, which is correct for any response that is not
 * a Next-rendered document and wrong for one that is.
 *
 * `blob:` appears in `img-src` and `worker-src` for the bundled PDF.js viewer, which renders
 * pages into blob-backed images and runs its parser in a blob worker.
 *
 * @param {Record<string, string | undefined>} env
 * @param {string} [nonce] per-request nonce from {@link generateNonce}
 * @param {string} [ancestorsOverride] an embed document's registered parent origins
 * @returns {string}
 */
export function contentSecurityPolicy(env, nonce, ancestorsOverride) {
  const isDev = env.NODE_ENV !== "production";
  const ancestors = ancestorsOverride || frameAncestors(env);
  // Dev only: Turbopack's HMR client evaluates code and opens a websocket back to the dev
  // server. Neither relaxation is ever emitted by a production build.
  const scriptSrc = [
    "script-src 'self'",
    nonce ? `'nonce-${nonce}' 'strict-dynamic'` : "",
    isDev ? "'unsafe-eval'" : "",
  ]
    .filter(Boolean)
    .join(" ");
  return [
    "default-src 'self'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
    scriptSrc,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' blob:",
    "font-src 'self'",
    `connect-src 'self'${isDev ? " ws: wss:" : ""}`,
    "worker-src 'self' blob:",
    `frame-ancestors ${ancestors}`,
  ].join("; ");
}

/** A fresh per-request nonce. Base64 of 16 random bytes from the Web Crypto global. */
export function generateNonce() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...bytes));
}

/**
 * Refuse a build whose CSP mints a nonce the rendered HTML can never carry.
 *
 * Next can only stamp a per-request nonce onto the scripts of a DYNAMICALLY rendered route. A
 * statically prerendered page was built before the nonce existed, so it emits bare script tags
 * while the header advertises a nonce, and because `'strict-dynamic'` switches off the `'self'`
 * fallback, that combination blocks strictly MORE than the unfixed `'unsafe-inline'` policy did.
 * The failure is invisible to every check that does not execute the page, so it is refused at
 * build time by `next.config.mjs`, which both `next build` and `next start` evaluate.
 *
 * @param {string} layoutSource contents of `app/layout.tsx`
 * @throws {UnhydratableCspError}
 */
export function assertHydratableCsp(layoutSource) {
  if (!/export\s+const\s+dynamic\s*=\s*["']force-dynamic["']/.test(layoutSource)) {
    throw new UnhydratableCspError(
      'app/layout.tsx must set `export const dynamic = "force-dynamic"`. The CSP mints a ' +
        "per-request nonce, and Next can only stamp it onto script tags for a dynamically " +
        "rendered route. Statically prerendered HTML was built before the nonce existed, so " +
        "every script is blocked and the page never hydrates.",
    );
  }
}
