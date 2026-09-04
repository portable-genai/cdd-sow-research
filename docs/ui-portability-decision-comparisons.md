# UI portability decision comparisons

This document explains the main UI portability choices for `cdd-sow-research`, especially the implemented
Mode 4 and Mode 5 isolated embeds. It compares credible alternatives that an adopter,
architect, security reviewer, or platform team may ask about.

The selected options are the implemented reference. The code and synthetic evidence gate in
[`embedding-implementation-plan.md`](embedding-implementation-plan.md) passes; its separate
production gate remains open.

The decisions optimize four things together:

1. one capability can appear in native, sandboxed, and standalone channels;
2. `cdd-sow-research` verifies identity rather than trusting host-supplied identity fields;
3. one immutable `cdd-sow-research` artifact can move between hosts and deployments; and
4. the design states its trust boundaries instead of describing browser isolation as
   complete API isolation.

## 1. User-interface channel

| Option | Portability | Security and integration trade-off | Decision |
|---|---|---|---|
| Native same-origin integration | Strong visual integration, but the host owns the route and can inspect the UI | Lowest browser friction; host is inside the full UI and data trust boundary | Retain as Mode 1 and the trusted-BFF pattern |
| Dedicated-origin sandboxed iframe | Host-framework independent and preserves a clear origin boundary | Requires loader, framing policy, browser protocol, and an embedded identity flow | **Selected for Modes 4 and 5** |
| Standalone top-level application | Works with almost every browser and IdP | User leaves the host workflow; easiest secure fallback | Retain as Modes 2 and 6 |
| In-process framework component | Tightest host integration | Couples release, framework, dependency, and DOM trust boundaries to each host | Not the portable baseline |

Why: no single channel is best for every adopter. Portability means preserving the
capability across these channels while making each channel's trust boundary explicit.

## 2. Cross-host UI packaging

| Option | Host compatibility | Isolation | Release coupling | Decision |
|---|---|---|---|---|
| Cross-origin iframe plus small loader | Plain HTML and all major frameworks | Browser-enforced origin boundary | `cdd-sow-research` releases independently | **Selected** |
| Web Component using Shadow DOM | Broad framework support | CSS encapsulation, not a security boundary; host JavaScript can inspect it | Shared page dependency risk | Example wrapper only, not the security boundary |
| Module Federation | Good for compatible JavaScript stacks | No isolation from host JavaScript | Host and remote runtime versions must stay compatible | Rejected as the normative contract |
| Framework-native package | Familiar to one host stack | No origin isolation | Separate React, Angular, and Vue release trains | Optional convenience wrapper only |

Why: the normative contract is a plain iframe and browser protocol. Framework wrappers
may improve ergonomics but cannot become a portability or security dependency.

## 3. Mode 4, Mode 5, or Mode 6

| Option | Credential location | User experience | Trust boundary | Decision |
|---|---|---|---|---|
| Mode 4 direct access token | Host JavaScript and iframe memory | Embedded, usually no second prompt | Host can reuse the `cdd-sow-research` token and is inside its API-data boundary | Keep as the lower-friction compatibility option |
| Mode 5 brokered PKCE grant | Subject credential stays at host BFF; `cdd-sow-research` token stays in iframe | Embedded, no second prompt when the BFF has a broker-recognized user credential | Host front end does not receive the reusable `cdd-sow-research` token; registered BFF remains trusted | **Recommended isolated integration** |
| Mode 6 top-level OIDC | First-party standalone session | May require a top-level sign-in | Separate standalone deployment and ordinary OIDC boundary | **Required universal fallback** |

Why: Mode 5 gives the strongest ordinary host-front-end separation, but it cannot remove
the institution's BFF and identity service from the authorization trust boundary. Mode 4
is useful when the host already has the correct token. Mode 6 remains the compatibility
escape hatch.

## 4. Configuration model

| Option | Composition | Failure behavior | Portability effect | Decision |
|---|---|---|---|---|
| Independent channel, identity, and runtime axes | Safe combinations are explicit | Unknown or invalid combinations fail startup | Identity can move without selecting unrelated compute or data adapters | **Selected** |
| One numbered mode selects everything | Simple at first | New combinations create pseudo-modes and hidden coupling | Channel changes can accidentally move compute or data | Rejected |
| Infer identity from the credential present on each request | Flexible-looking | Ambiguous downgrade and token-confusion paths | Deployment behavior is not reproducible from configuration | Rejected |
| Infer identity from runtime profile | Small configuration surface | A runtime change can silently alter auth and network posture | Prevents independent identity portability | Rejected |

Why: a channel is not an identity provider, and an identity provider is not a compute
profile. One deployment surface selects one exact identity mode.

## 5. Public mount path and artifact strategy

| Option | Artifact reuse | Host integration cost | Evidence quality | Decision |
|---|---|---|---|---|
| Fixed `/agent` artifact with host-owned redirect or proxy alias | One build across channels | Host maps its entry route to the canonical surface | Same digest is proven | **Selected and implemented** |
| Rebuild for `/apps/doc1` or another host path | One build per host path | Easy for the current host | Same source is not the same artifact | Transitional `journey-portal` build retired |
| Runtime-selected Next.js `basePath` | Would be ideal if supported | Appears simple | Next.js asset paths are build-time, so this does not produce one immutable build | Not viable with the current stack |
| Root-only `/` deployment | Simple artifact | Consumes an entire origin and complicates shared edge routing | Portable only when every adopter grants a dedicated origin root | Not the reference contract |

Why: the same-artifact claim requires identical bytes, not repeated builds from the same
source. `journey-portal` keeps `/apps/doc1` as an entry compatibility URL while its proxy serves the
canonical `/agent` artifact.

## 6. Installation and security policy source

| Option | Consistency | Parent authority | Operational trade-off | Decision |
|---|---|---|---|---|
| One required versioned manifest, with the UI digest checked by FastAPI on sandboxed requests | One exact policy for frame and API decisions | Parent supplies only opaque `installation_id` | Requires controlled shared manifest delivery | **Selected** |
| Separate front-end and API registries | Policies can drift | Parent may be accepted by one tier but not the other | Independent operations create split-brain risk | Rejected |
| Parent-supplied issuer, tenant, origin, or API URL | Easy to prototype | Parent chooses security-critical policy | Converts selectors into authority | Rejected |
| Build-time environment variables per tenant | Familiar deployment pattern | Policy is fixed into each build | Creates tenant-specific artifacts and weakens portability proof | Development defaults only |

Why: the framed document is served by Next.js while authorization happens in FastAPI.
Both use `CDD_INSTALLATION_MANIFEST`; the UI sends the digest of its exact bytes and
FastAPI rejects a missing or different digest before serving a sandboxed API request.

## 7. First-release tenancy shape

| Option | Isolation | Operational complexity | Future migration | Decision |
|---|---|---|---|---|
| One institution per deployment, multiple same-tenant installations | Strong deployment boundary | More deployments | Registry-shaped contract permits later consolidation | **Selected for v1** |
| Shared multi-institution deployment from day one | Efficient at scale | Requires tenant-aware keys, stores, rate limits, incidents, and policy administration immediately | No later consolidation needed | Deferred until evidence justifies it |
| One deployment per installation | Maximum isolation | Highest cost and operational duplication | Harder fleet management | Available for exceptional risk, not the baseline |

Why: v1 manifest validation requires one non-empty tenant across all installations and
one exact identity mode for the deployment. This removes unnecessary multi-tenant risk
without baking a single installation into the API.

## 8. Iframe and API origin topology

| Option | Browser behavior | Security and operations | Decision |
|---|---|---|---|
| Iframe UI and API share the dedicated agent origin | No iframe-to-API CORS; bearer stays in agent context | Parent-to-frame boundary is handled by CSP and messaging | **Selected** |
| Iframe UI calls a second cross-origin API | Requires credentialed CORS on every protected transport | Adds origin policy, preflight, cache, and streaming complexity | Rejected |
| Host proxies every iframe API call | Avoids browser CORS | Host enters the full request and response data boundary | Native trusted-BFF option, not isolated Mode 5 |
| API calls traverse parent `postMessage` | Avoids direct network calls from iframe | Parent becomes a data broker; binary and streaming paths become complex | Rejected |

Why: cross-origin is needed between the host and the agent frame, not between the agent
frame and its own API.

## 9. Iframe sandbox capability

| Option | Functionality | Residual risk | Decision |
|---|---|---|---|
| `allow-scripts allow-same-origin` on a different agent origin | Supports application code, exact origins, and same-origin API calls | Safe only because parent and child origins differ | **Selected baseline** |
| `allow-scripts` without `allow-same-origin` | Stronger opaque-origin restriction | Breaks exact-origin protocol checks and normal same-origin API behavior | Rejected |
| Add popups, downloads, forms, or top navigation | Enables more browser behaviors | Expands exfiltration and navigation capability | Add only for a tested requirement |
| Unsandboxed cross-origin iframe | Origin isolation remains | Loses sandbox defense in depth | Not the baseline |
| Same-origin iframe with both flags | Full application behavior | Child may remove its sandbox because it shares the parent origin | Forbidden for the isolated profile |

Why: the selected pair is safe only with a dedicated agent origin and fail-closed
validation that the allowed parent origin is different.

## 10. Host-to-iframe protocol

| Option | Binding strength | Portability | Decision |
|---|---|---|---|
| Host-initiated exact-origin `postMessage` bootstrap, then dedicated `MessagePort` | Binds source, origin, installation, version, and channel instance | Browser-native and framework independent | **Selected** |
| Continue using global `window.postMessage` for the whole session | Origin can be checked | More listeners, routing ambiguity, and accidental cross-instance messages | Rejected |
| Query-string configuration | Easy to inspect and bookmark | Leaks into URLs and encourages parent-selected security policy | Presentation hints only, never authority or credentials |
| Shared browser storage | Avoids messaging | Cross-origin storage is unavailable or policy-sensitive and creates persistence risk | Rejected |

Why: the global message is used only to establish a private channel. Mode 4 transfers its
credential only over that negotiated channel; no credential is sent in the bootstrap.

## 11. Loader distribution

| Option | Adoption | Supply-chain control | Decision |
|---|---|---|---|
| Immutable versioned loader URL with SRI | One script tag, framework neutral | Released bytes are pinned and independently checkable | **Selected** |
| Floating `latest` loader | Simplest upgrades | Host behavior can change without review | Rejected |
| Copy loader code into every host | No cross-origin script dependency | Forks drift and security fixes fragment | Supported only as a pinned, verified vendoring process |
| Framework SDK as the normative integration | Good developer ergonomics in one stack | Creates runtime and release coupling | Examples only |
| Plain iframe without loader | Minimum dependency | Host must implement handshake, fallback, sizing, and error behavior itself | Supported low-level contract |

Why: the iframe protocol remains normative. The loader is a small, pinned convenience
layer rather than a framework or package-manager dependency.

## 12. Framing and host CSP

| Option | Enforcement point | Failure mode | Decision |
|---|---|---|---|
| Installation-specific response-header `frame-ancestors` plus host `frame-src` | Agent and host both approve the relationship | Exact origin mismatch fails closed | **Selected** |
| `frame-ancestors *` | Agent accepts every parent | Enables unauthorized framing and UI redress | Forbidden |
| `X-Frame-Options` only | Legacy same-origin or deny control | Cannot express the required cross-origin allowlist | Insufficient |
| CSP in an HTML `<meta>` element | Easy application change | `frame-ancestors` is not enforced from meta | Rejected |
| API response carries the only framing policy | Protects the wrong response | Browser frames the Next.js document, not the API JSON | Defense in depth only |

Why: the load-bearing policy must be on the actual framed document, and the client portal
must separately permit the loader and frame.

## 13. Mode 4 credential type

| Option | Semantic fit | Verification requirement | Decision |
|---|---|---|---|
| RFC 9068-style JWT OAuth access token for the `cdd-sow-research` audience | Resource authorization token | Pin type, algorithm, issuer, audience, client, subject, tenant, scope, and time claims | **Selected v1 profile** |
| OIDC ID token | Authenticates a user to an OIDC client | Audience and token purpose are wrong for a `cdd-sow-research` resource API | Reject |
| Opaque OAuth access token with introspection | Valid resource credential | Requires authoritative introspection availability and egress | Later adapter |
| Host-signed custom JWT | Can be made to work | Creates a bespoke protocol and claim contract | Reject unless it is standardized as the reviewed access-token profile |
| Unsigned host identity headers | No token integration | Host can assert any actor or tenant | Reject |

Why: different token types may share low-level JOSE and JWKS utilities, but they require
different policy validators and must not be interchangeable.

## 14. Mode 5 grant pattern

| Option | Token exposure | Replay and binding | Decision |
|---|---|---|---|
| Iframe-first PKCE registration plus BFF-authorized single-use code | Reusable `cdd-sow-research` token stays out of host JavaScript | Code is bound to iframe verifier, installation, subject, client, tenant, scope, and expiry | **Selected** |
| BFF returns a reusable `cdd-sow-research` token to host JavaScript | Host receives full API credential | Simple but becomes Mode 4 trust | Rejected for Mode 5 |
| Parent registers the PKCE challenge | Parent can substitute its own verifier | Weakens iframe ownership of redemption | Rejected |
| Run the institution login redirect inside the iframe | Token may stay in frame | Often blocked by IdP framing and third-party storage policy | Not the normal path |
| Server injects an auth header through a same-origin proxy | Browser sees no token | Host BFF can read all requests and responses | Classified as native trusted BFF, not cross-origin Mode 5 |

Why: PKCE protects the launch code from the parent, but it does not prove user intent or
remove the registered BFF from the authorization boundary.

## 15. Mode 5 subject credential

| Option | Portability | Type safety | Decision |
|---|---|---|---|
| Broker-audience RFC 9068 JWT access token | Standards-based and locally verifiable | Exact access-token profile and bounded lifetime | **Selected v1 profile** |
| OIDC ID token sent to the broker | Commonly available | Wrong token purpose and client audience | Reject |
| Arbitrary external token accepted directly | Broad compatibility | Token-type confusion and issuer-specific behavior | Reject |
| Explicit RFC 8693 exchange policy for a named source token type | Adds institution-specific compatibility | Exchange service and mapping remain trusted dependencies | Allowed reviewed extension |
| Unsigned BFF session assertion | Easy for host | `cdd-sow-research` cannot verify the human identity | Reject |

Why: the v1 subject credential preserves the exact upstream issuer and original subject.
The `cdd-sow-research`-issued embed token carries that provenance separately from its own token issuer
and from the BFF client identity.

## 16. Mode 5 BFF client authentication

| Option | Credential strength | Operational trade-off | Decision |
|---|---|---|---|
| mTLS | Strong client and channel binding | Certificate issuance, rotation, and network termination complexity | Supported |
| `private_key_jwt` | Strong asymmetric client authentication over ordinary TLS | Requires key registration, replay state, rotation, and strict assertion policy | **Supported reference option** |
| Shared client secret | Familiar | Wider secret distribution and weaker client separation | Not the preferred production baseline |
| Public client with no authentication | No credential operations | Any caller can act as the registered BFF | Reject |

Why: the grant endpoint authorizes a powerful delegation client. PKCE does not replace
BFF client authentication.

## 17. Grant and continuation state

| Option | Multi-replica behavior | Replay and audit behavior | Decision |
|---|---|---|---|
| Typed `BrowserFlowStorePort` with atomic state transitions and durable outbox | Shared production adapter supports failover | Exactly-once issuance/consume semantics and durable security events | **Selected** |
| Process memory | Breaks on restart and across replicas | Replay and audit races | Reject outside throwaway tests |
| Cache operations without compare-and-transition | Scales | Duplicate authorization or consume races remain | Reject |
| Fully self-contained signed grant | No store lookup | Hard to enforce one issuance, one consume, revocation, and durable audit | Rejected for v1 |
| Transactional SQLite | Good single-process and restart proof | Not safe as a multi-replica production store | Selected local proof adapter only |

Why: citation continuation and Mode 5 grants use different record variants and state
machines behind one infrastructure port. A citation ticket can never be consumed as a
grant code.

## 18. Protected document rendering

| Option | Authentication | Browser portability | Decision |
|---|---|---|---|
| Authenticated in-frame viewer; pinned PDF.js renders PDF to canvas | One bearer-aware transport | Consistent across supported browsers | **Selected** |
| Ordinary `_blank` link | Cannot attach bearer header reliably | Browser behavior is familiar | Reject for protected files |
| Browser PDF plug-in or nested object | Credential and CSP behavior vary | Browser and policy dependent | Reject |
| Enable direct download | Simple | Expands sandbox capability and local data persistence | Not in baseline |
| Send document bytes through parent messaging | Parent gains document access | Complex size, memory, and streaming behavior | Reject |

Why: JSON, uploads, streams, and document blobs all use one authenticated iframe-to-API
transport.

## 19. Public citation originals

| Option | Source traceability | Data leakage and navigation | Decision |
|---|---|---|---|
| In-frame evidence card plus opaque, server-resolved Mode 6 continuation | Full provenance and authenticated top-level original | Host sees no raw target; final redirect follows reauthorization and confirmation | **Selected** |
| External `_blank` link from sandbox | Direct | Requires popup capability and exposes the target to host/browser handling | Rejected |
| Put the original URL in a host message | Host can render a link | Leaks sensitive query values and makes host a navigation broker | Rejected |
| Never permit the live original | Simplest | Weakens `cdd-sow-research`'s source-traceability claim | Not acceptable for completion |
| Proxy every public website into the iframe | Keeps navigation in frame | Creates SSRF, content rewriting, legal, and active-content risk | Rejected |

Why: the server creates a short-lived opaque ticket from an authorized citation record.
The standalone deployment resolves it only for the expected verified actor and tenant.

## 20. Authentication fallback

| Option | Browser and IdP compatibility | Security effect | Decision |
|---|---|---|---|
| Separate top-level Mode 6 OIDC deployment | Broadest compatibility | First-party session and clear credential policy | **Selected** |
| OIDC login inside the embedded iframe | Seamless when allowed | IdP framing, third-party cookies, and storage policy often block it | Not the normal fallback |
| Storage Access API | Can recover third-party cookie access | Browser-specific prompts and policy variance | Rejected as a baseline |
| Downgrade to local persona | Always works in a demo | Converts an auth failure into an identity bypass | Forbidden in secure deployments |
| Accept whichever bearer or cookie is present | Fewer routes | Credential presence chooses policy and enables confusion | Rejected |

Why: the isolated and standalone deployments use the same application artifact but
different exact identity policies. They reject each other's credential types.

## 21. Mode 6 CSRF state

| Option | Horizontal scaling | Browser exposure | Decision |
|---|---|---|---|
| CSPRNG nonce in signed `HttpOnly` session, returned by authenticated same-origin no-store bootstrap and held in browser memory | Stateless across replicas and restart | JavaScript sees only the CSRF nonce, not the session | **Selected** |
| Server-memory CSRF state | Needs sticky sessions or shared memory | Browser holds a token | Reject because it weakens restart and replica portability |
| Separate JavaScript-readable CSRF cookie | Stateless double submit | Adds another cookie and persistent browser-readable state | Not selected |
| `SameSite=Strict` only | Stateless | Same-site sibling origins are not a sufficient boundary | Insufficient |
| Origin check only | Stateless | Useful but does not provide the complete unsafe-request contract | Defense in depth, not the sole control |

Why: unsafe cookie-authenticated routes require exact `Origin`, same-origin Fetch
Metadata, and a constant-time CSRF header check. Same-origin host XSS remains outside this
control and requires separate prevention or transaction confirmation.

## 22. Canonical audit actor

| Option | Stability | Collision and spoofing risk | Decision |
|---|---|---|---|
| Deterministic issuer-qualified `(source_iss, source_sub)` | Stable when the issuer preserves subject semantics | Prevents cross-issuer subject collisions | **Selected default** |
| Email address | Human-readable | Mutable, reassignable, and not globally unique | Display metadata only |
| Host-supplied actor ID | Convenient | Host can impersonate another user | Reject |
| `cdd-sow-research` embed-token issuer plus unqualified subject | Locally simple | Erases upstream identity provenance | Reject |
| Reviewed server-side subject-link policy | Supports pairwise subject identifiers | Requires governed mapping and evidence | Allowed only when exact issuer and subject cannot remain stable across clients |

Why: the audit can attribute an action to a verified subject and session. It does not by
itself prove the physical human's intent or cryptographic non-repudiation.

## 23. Sender-constrained tokens

| Option | Protection | Complexity | Decision |
|---|---|---|---|
| Short-lived bearer token after PKCE code redemption | PKCE protects the code; origin and CSP protect normal use | Smallest secure v1 slice | **Selected for Mode 5 v1** |
| DPoP-bound access token | Adds proof-of-possession and request replay controls | Browser key lifecycle, nonce, clock, and shared replay state | Planned hardening, not v1 blocker |
| mTLS-bound resource token | Strong sender constraint | Poor fit for browser iframe calls | BFF/server channels only |
| Long-lived bearer plus refresh token | Fewer launches | High-value browser credential and persistence risk | Reject |

Why: DPoP addresses bearer-token theft after redemption, not malicious code already
running in the iframe or misuse by an authorized BFF. It should not delay the first
honest, short-lived implementation.

## 24. `journey-portal` migration

| Option | Mode 1 continuity | Same-artifact proof | Decision |
|---|---|---|---|
| P1 keeps the `/apps/doc1` compatibility build; P2 moves `journey-portal` to canonical `/agent` and retains an entry alias | No broken journey between PRs | Native proof becomes valid after P2 cross-repo evidence | **Selected migration, complete** |
| Change `cdd-sow-research` to `/agent` immediately | Breaks current `journey-portal` assets and API routing | Eventually valid | Rejected because dependency closure is missing |
| Keep rebuilding `cdd-sow-research` at `/apps/doc1` permanently | Current journey stays green | Native channel never uses the same artifact as isolated and standalone | Rejected |
| Change `journey-portal` public journey contract with no alias | Clean target | Breaks bookmarks, tests, and integration assumptions | Rejected |

Why: `journey-portal` was an internal implementation dependency, not an external production blocker.
It now consumes the canonical artifact, and the build, proxy, asset, API, identity, and
journey tests pass.

## 25. Portability evidence

| Option | What it proves | Decision |
|---|---|---|
| Same source rebuilt for each host | Source portability, but not artifact identity | Insufficient |
| Same artifact digest in two hosts, two parent origins, and separate standalone deployment | Channel portability for the released bytes | **Selected channel proof** |
| Printed adapter names | Configuration visibility only | Insufficient runtime or identity proof |
| Synthetic RS256 and ES256 issuers with rotation and negative cases | Verifier portability and fail-closed behavior | **Selected synthetic identity proof** |
| One successful browser | A happy path in one engine | Insufficient; gate Chromium, Firefox, and WebKit |

Why: the evidence gate must name exactly what it proves. It does not convert on-premises
placeholders into a working sovereign runtime or audit-only export into complete
case/document data exit.

## Decision summary

The selected design is one canonical `/agent` artifact with three independent
configuration axes. Modes 4 and 5 run in a dedicated-origin sandbox whose UI and API are
same-origin with each other. Mode 4 accepts a tightly profiled `cdd-sow-research` OAuth access token and
explicitly trusts the host with it. Mode 5 uses iframe-first PKCE and an authenticated
host BFF to keep the reusable `cdd-sow-research` token out of ordinary host JavaScript. Mode 6 is a
separately configured top-level OIDC fallback.

One canonical manifest governs installation, framing, issuer, tenant, client, scope, and
fallback policy for both Next.js and FastAPI. Short-lived grants and citation
continuations use atomic typed browser-flow state. Protected files render in-frame;
public originals use an opaque server-resolved continuation. The same-artifact and
identity claims become real only when the cross-host, cross-issuer, cross-browser evidence
gate passes.

For the complete protocol and implementation sequence, see:

- [`embedding-and-identity.md`](embedding-and-identity.md)
- [`embedding-implementation-plan.md`](embedding-implementation-plan.md)
- [`../ARCHITECTURE.md`](../ARCHITECTURE.md)
