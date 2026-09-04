# Security FAQ

For an application-security team reviewing this repo before adopting it as a base. Answers
reflect the current code. Cross-references: [`ARCHITECTURE.md`](../../ARCHITECTURE.md)
§7 (security principles), [`COMPLIANCE.md`](../../COMPLIANCE.md),
[`docs/embedding-and-identity.md`](../embedding-and-identity.md) §13 (threat model).

### How is a request authenticated? Can a client spoof its identity?

No. Identity is resolved **server-side** from the transport context by an `IdentityPort`
adapter (`api/security.py` → `domain/identity.py`), never from the request body. The
request schemas carry no `actor` field (`api/schemas.py`), and any client-asserted actor or
ACL is discarded. The audit actor and the entitlement principals both come from the
verified `Principal`. Per identity mode: `local-persona` = seeded dev personas (no IdP, offline only),
IAP = the edge-injected signed assertion, `oidc-session` = the agent's own session cookie
minted after an OIDC Authorization Code + PKCE login, `oauth-access-token` = the verified
Mode 4 `cdd-sow-research`-audience token, and `embedded-grant` = the dedicated Mode 5 token minted only
after the brokered PKCE exchange. Identity and channel are exact selectors independent of
the runtime profile.

### How is object-level authorization (multi-tenant isolation) enforced?

The case retrieval ACL is derived server-side in `domain/entitlements.py`: a caller gets
the `case:<id>` retrieval principal only after an entitlement check against the verified
principal, and evidence is tagged with **both** `case:<id>` and `tenant:<tenant>` at
ingest. The knowledge-base ACL match is **subset-based and fail-closed** (a reader must
hold every tag on a passage), so an authenticated user in another tenant gets zero passages
for a case id they merely guessed. Proven in `tests/unit/test_entitlements.py`. An
unentitled request returns 403, not a data leak.

### What about the service-to-service calls in the `platform` profile?

The platform adapters (`adapters/platform/_s2s.py`) require `https://` base URLs outside
loopback (rejected at construction), attach a bearer credential from `S2S_TOKEN` (a
Cloud Run ID token / OIDC service-account JWT / gateway key), and propagate the verified
end-user actor as an HMAC-signed header rather than a trust-me JSON field. The receiving
platform services own verification.

### Is the demo/dev server safe? Does anything bind 0.0.0.0 by default?

No. There are two bounds, and the load-bearing one rides the **app object** rather than an
entry point.

`main()` binds **loopback (127.0.0.1)** via `hex_service_kit.resolve_bind_host`, and
`make run-api` does the same. On its own that is a property of one entry point, not of the
application: the Dockerfile `CMD` is
`uvicorn cdd_sow_research.api.app:app --host 0.0.0.0 --port ${PORT}`, and a
`uvicorn ... --host 0.0.0.0` typed by hand behaves the same way, so neither ever reaches that
call. The real bound is `add_loopback_exposure_guard`, registered inside `create_app` as the
outermost middleware, so it holds however the service is started: a non-loopback peer is
refused with a 503 before the deployment-security middleware (CORS, CSRF, limits, headers)
and before any route or dependency runs.

**What switches it off is the identity BINDING, and nothing else.** The guard asks the
adapter bound to the identity port whether it verifies the end user (see
`src/cdd_sow_research/ports/identity.py`). The seeded persona adapter reads `X-Dev-Persona`, a
header the caller writes, so it declares `client-asserted` and the guard stays on; the
on-premises placeholder resolves nobody, so it declares `unimplemented` and the guard stays
on; the IAP, OIDC-session, access-token and embedded-grant adapters each verify a signed
assertion server-side, so they declare `verified` and stand the guard down. Neither
`CDD_PROFILE` nor `CDD_IDENTITY_PROFILE` decides this on its own: they move independently and
a deployment may rebind any mode, so only the bound adapter can answer. A run that named
nothing at all is bounded too, and additionally refuses the seeded personas outright, so a
lost environment variable cannot publish an unauthenticated API.

`CDD_S2S_TOKEN` is deliberately **not** part of that decision. It authenticates a calling
service and no end user, so setting one closes the service-to-service dependency and changes
nothing about the end-user routes. A guard derived from it would switch off for exactly the
routes it was protecting.

`CDD_ALLOW_INSECURE_DEMO=1` remains the single documented opt-out. Secure profiles keep the
container-friendly `0.0.0.0` (ingress is fronted by the platform / IAP and the identity
adapter verifies the caller). The stdlib demo server (`scripts/sow_demo_server.py`) binds
loopback and HTML-escapes its content; it is clearly dev-only. Proven in
`tests/unit/test_serving_path_exposure.py`.

### What HTTP security headers are set?

The API middleware emits CSP `frame-ancestors`, `X-Frame-Options` (same-origin case),
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and HSTS on secure
profiles. The Next.js UI (`ui/next.config.mjs`) emits a full CSP (`default-src 'self'`,
scoped `connect-src` to the API origin, `frame-ancestors`), `nosniff` and `Referrer-Policy`.
CORS never uses `*`: the localhost dev-origin fallback and the `X-Dev-Persona` header are
**local-profile-only**, so a secure deploy that forgets `CDD_CORS_ORIGINS` trusts nothing
cross-origin.

### Is there rate limiting / request-size control?

There is an in-app backstop (`api/app.py`): a per-client token-bucket rate limit (stricter
on `/auth/*`) and a request-body-size cap, both env-tunable, with the documents list bounded
in the schema. These are defense-in-depth; production is expected to enforce the primary
limit at the edge (IAP / Apigee / LB), documented as a deploy-gate item in the runbook.

### How does the OIDC login flow hold up?

It implements Authorization Code + PKCE (S256), a signed `state` compared in constant time,
an OIDC `nonce` bound to the login and verified against the id_token, ID-token verification
against the issuer JWKS with an explicit algorithm allowlist (rejects `alg=none` and
HS-with-public-key confusion), `__Host-`/`__Secure-` cookie prefixes, `HttpOnly` + `Secure`
+ `SameSite` cookies, session signing-key rotation (`kid` + accepted-key list), and an
https-issuer check at config load. Failures fail closed (401). See
`tests/unit/test_oidc_auth_flow.py`.

The local hardening gate verifies the returned issuer, HTTPS and reviewed endpoint policy,
canonical redirect/cookie behavior, subject/tenant/session binding, and CSRF. Production
still needs a named IdP registration, restricted discovery/JWKS egress, and a separately
hosted Mode 6 service and edge.

### How tamper-evident is the audit trail? What are its limits?

The `local` audit store is a hash chain (`entry_hash = SHA-256(prev_hash ‖ record)` over
canonical JSON) with SQLite `UPDATE`/`DELETE` triggers enforcing append-only, plus an
optional external head anchor (`CDD_LOCAL_AUDIT_ANCHOR`) that detects tail-truncation and
full-rewrite (which the chain alone cannot, since it carries no secret). The module
docstring states exactly which tamper classes are and are not caught. In production the
`gcp` profile uses a locked WORM bucket, which provides non-rewritability itself. This repo
does not *replace* the platform audit system (`agent-observability`), see
[features-faq.md](features-faq.md).

### Supply chain: are dependencies pinned and scanned?

Yes. Committed lockfiles (`requirements-dev.lock`, `requirements-dev-oidc.lock`,
`requirements-gcp.lock`) are installed in CI and the Docker build; the base image is pinned
by digest; GitHub Actions are SHA-pinned; `dependabot.yml` proposes bumps; and a CI job runs
`pip-audit` (on the lockfiles) + `npm audit` (on the UI). `ruff` is pinned exactly.

### Where are secrets? Are any committed?

No secret values are in the repo. `config/settings.yaml` stores only the **names** of env
vars holding secrets (e.g. `session_signing_key_env`, `client_secret_env`,
`S2S_TOKEN`); values are read at construction time and never logged. The bundled
sanctions snapshot and every fixture are obviously-fictional.

### What is explicitly out of scope / a residual risk?

- The managed Firestore `CaseStorePort` is implemented, but its live integration proof
  requires a provisioned regional database and credentials. `onprem` remains a placeholder.
- Modes 4/5 implementation and full synthetic channel/identity conformance are complete,
  but named external IdP/BFF registrations, production hosting and shared replay stores,
  key custody, approved origins/CSP, target-host browser evidence, and separately deployed
  Mode 6 remain open.
- Mode 2/6 production UI and edge hosting are not provisioned here; Mode 6 production
  registration and restricted egress remain open.
- The in-app rate limit is a backstop, not a substitute for an edge WAF/limiter.
- The hash chain needs the external anchor (or the WORM bucket) to resist truncation.
- This is a reference build: run your own pen-test, threat model, and model-risk review
  before any live-data deployment (stated throughout the docs).
