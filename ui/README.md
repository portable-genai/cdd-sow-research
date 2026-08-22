# Doc1 UI: CDD + Source-of-Wealth Agent console

A small React / Next.js console that renders the Doc1 CDD dossier with inline citation chips.
CI performs a production Next.js build and builds the tenant-neutral `ui/Dockerfile`. Installation
policy is mounted at runtime, so one immutable image serves every reviewed institution.

## Run locally

```bash
npm install
CDD_API_INTERNAL_ORIGIN=http://127.0.0.1:8090 npm run dev
# open http://127.0.0.1:3000/agent/
```

Point it at a running Doc1 API (`make run-api` in the repo root). Next.js proxies the
canonical same-origin `/agent/api/*` browser path to `CDD_API_INTERNAL_ORIGIN`; the browser
never needs a second API origin. The console submits a
subject to `POST /v1/cdd` and renders the returned `CDDCase`: the source-of-wealth
narrative, risk rating, adverse-media findings and the UBO summary, each with citations.

For a one-command, production-shaped local walkthrough:

```bash
make laptop-demo
```

The presenter runner starts isolated API and UI processes when needed, shows the honest
capability manifest, builds a dossier, and proves export/reload of the open
`cdd-dossier/v1` JSON envelope. Use `make laptop-demo-selftest` for unattended validation.

## Where the security policy lives

| File | Owns |
|---|---|
| `lib/csp.mjs` | The ONE Content-Security-Policy builder, plus `frame-ancestors` resolution, nonce minting, and the build-time refusals. No I/O, no framework. |
| `proxy.ts` | The ONE enforcement point. Mints a per-request nonce, sets the policy on the request headers (where Next reads the nonce it stamps onto script tags) and on the response headers (what the browser enforces). Embed documents get their installation's registered parent origins. |
| `next.config.mjs` | Runs the refusals at build and boot. Emits `nosniff` and `Referrer-Policy` only, and deliberately no CSP: two policies get intersected, not merged. |
| `app/layout.tsx` | `export const dynamic = "force-dynamic"`, required by the nonce policy and not a performance choice. |
| `scripts/assert-hydratable.mjs` | Proves the served bytes hydrate. Starts the built server and checks every script tag against the served nonce. |
| `tests/csp.test.ts` | Covers the half a string can decide. Explicitly not sufficient on its own. |

`CDD_FRAME_ANCESTORS` selects who may frame the standalone console. Unset means `'self'`;
a named list means those origins; set but naming nothing, or naming a wildcard, is refused
at build and boot. See `docs/embedding-and-identity.md` section 10.1.

## Gate

```bash
make ui-check     # from the repo root: tsc, node --test, next build, assert-hydratable
make ui-install   # npm ci, when the lockfile or dependencies changed
```

`assert-hydratable` runs last on purpose: it tests the artefact the build just produced.
CI runs the same four steps. A green `next build` and a correct-looking header do not prove
the console hydrates; only the served markup does.

The synthetic data is fictional; do not use this against live customer data without your
own legal, security and model-risk sign-off.
