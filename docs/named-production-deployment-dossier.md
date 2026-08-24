---
type: Deployment Input Dossier
title: Doc1 named production deployment
description: Non-secret decision and evidence template for one institution enabling UI portability Modes 4 and 5.
status: draft
---

# Doc1 named production deployment dossier

This dossier is the entry gate for one real production installation. Complete every field with
the named institution and obtain the listed approvals before applying
[`infra/terraform/`](../infra/terraform/). Do not put credentials, private keys, subject tokens,
session values, or customer data in this file. Record only approved resource names and evidence
locations. Secret values belong in the institution's Secret Manager.

The executable input contract is [`.env.example`](../.env.example) plus
[`.env.secrets.example`](../.env.secrets.example). Copy them to the gitignored `.env` and
`.env.secrets` files. Keep non-secret decisions in `.env` and secret payloads in
`.env.secrets`. Run `make deploy-env-check` while drafting, then `make deploy-preflight`.
The production preflight rejects every `PENDING`, `PLACEHOLDER`, `REPLACE`, example domain,
`TBD`, `TODO`, `CHANGEME`, reserved `.test` domain, floating secret version and mutable image
tag before any command can run. It also decodes and
schema-validates both secret payloads, binds their exact bytes to reviewed SHA-256 digests, and
requires at least two replicas plus a real alert notification channel.

Repository code and reusable infrastructure are ready for a named deployment.

**Assumption changed 2026-08-24: a fictional institution IS acceptable evidence here, with one
carve-out.** This dossier previously refused fictional inputs outright, which made every field
below unfillable without a real counterparty and left the whole Track C deployment blocked on
one. The maintainer's decision reverses that default for the reference deployment: a fictional
institution may supply the identity, origin, installation and browser-policy inputs, because
what those inputs exercise is the *mechanism* — preflight rejection, digest binding, perimeter
enforcement, CMEK, WORM routing — and a mechanism does not know whether the name bound to it is
real.

**The carve-out is data, not names.** Where a control's correctness depends on real-world data
that fictional data cannot stand in for, fictional input is NOT acceptable and the evidence must
say so. The named case is **adverse-news and open-source search in the CDD flow**: a search over
an invented entity returns nothing, and "no adverse news found" against a name that cannot
generate adverse news is a vacuous pass, not a control that was exercised. The same test applies
to any future check whose signal comes from outside this deployment. For those, either use a
real public entity with published, verifiable coverage, or record the check as UNEXERCISED
rather than PASS.

**What this evidence is, and is not.** It demonstrates that the deployment mechanism works
end to end under real GCP enforcement. It is not an institutional production evidence pack: the
institution is fictional and, per the recorded deviation below, the evidence approver is not
independent. Any pitch or brief citing it states both.

## 1. Deployment identity and owners

| Input | Required value | Approval or evidence |
|---|---|---|
| Institution | `PLACEHOLDER` | Executive sponsor |
| Installation name | `doc1-<institution>-production` | Deployment owner |
| Deployment owner | Ashish Awasthi | Named person or team |
| Security owner | Ashish Awasthi | Named person or team |
| Operations owner | Ashish Awasthi | Named person or team |
| Evidence approver | Ashish Awasthi | Independent reviewer — **NOT independent; see the recorded deviation below** |
| Incident channel | `PENDING` | Tested escalation route |
| Evidence-retention location | `PENDING` | Access-controlled record location |

**Recorded deviation: single-person ownership. DECIDED 2026-08-24.** Every owner role above
is Ashish Awasthi, the sole maintainer, by explicit decision rather than by default: the
question was asked directly and answered "all four are me for now." The two consequences below
are therefore accepted, not merely disclosed, and stay accepted until a second person joins one
of these roles:

1. **The evidence approver is not independent.** The review rule for that row asks for an
   independent reviewer, and self-approval does not satisfy it. This is the same
   maker-checker property (P-06) the agent itself enforces by always setting
   `requires_human_review=True`, so a self-approved evidence pack is weaker evidence than the
   product's own control model assumes. Acceptable for a controlled pre-production apply by a
   single maintainer; not acceptable as institutional production evidence.
2. **There is no escalation path or second custodian.** The incident channel, the emergency
   key-revocation procedure and the backup/restore path all terminate at the same person, so
   there is no fallback if that person is unavailable during an incident.

## 2. Cloud, residency and edge

`us-central1` (USA) is the repo's stated residency pin (`AGENTS.md`), matches
`infra/terraform/variables.tf`'s default `region`, and is what every regional endpoint in
`config/settings.yaml` (Document AI, Discovery Engine, Model Armor) tracks via `GCP_REGION`. Not
all GCP services this repo depends on are available in `asia-southeast1`, which is why
`us-central1` is the pin. An institution deploying under a residency obligation sets its own
in-country region explicitly rather than inheriting this default.

Values below were created and confirmed on **2026-08-24** under organization `thingz.live`
(`400961117351`). Rows still reading `PENDING` are genuinely not created.

| Input | Required value | Review rule |
|---|---|---|
| GCP organization and project | `thingz.live` / `400961117351`, project `portable-genai-prod` (number `192121533143`) | Shared deployment project, approved in [gcp-org-and-project-topology.md](https://github.com/portable-genai/org-metadata/blob/main/docs/gcp-org-and-project-topology.md) |
| Credential reachability | `ashish@whiz.coach`, holding `roles/resourcemanager.organizationAdmin`, `roles/resourcemanager.projectCreator` and `roles/accesscontextmanager.policyAdmin` | See the note below |
| Billing and quota owner | `01E200-BFA66C-B15046` (`whiz.coach`), linked and enabled; owner Ashish Awasthi | Quotas cover HSM, Firestore and Cloud Run, plus IAP for the separate Mode 6 edge |
| Approved region | `us-central1` (USA) | Must be in `allowed_regions` |
| Allowed regions | `["us-central1"]` | Reference deployment under no residency obligation; see the region record. **Satisfies no APAC residency regime** |
| Access Context Manager policy | `718324219947` (title `portable-genai`, org-scoped) | Dry-run VPC-SC before enforcement |
| Agent origin | `PENDING` | Dedicated HTTPS origin |
| Standalone fallback origin | `PENDING` | Separate Mode 6 service and cookie boundary |
| DNS managed zone and owner | `PENDING` | Change window recorded |
| Certificate authority and owner | `PENDING` | Managed certificate or approved equivalent |
| Terraform state backend | `gs://portable-genai-prod-tfstate`, prefix `doc1/`; versioned, UBLA, public access prevented | GCS bucket plus installation-specific prefix; local state is rejected |
| Alert notification channel | `projects/portable-genai-prod/notificationChannels/16092192702056537312` (email) | Real channel required by preflight |
| WORM retention decision | **3 days, UNLOCKED** (`retention_days = 3`, `worm_locked = false`). DECIDED 2026-08-24 | See the revised rule below — the six-month floor still binds a locked stack |

**Workstation credentials.** A dedicated `gcloud` configuration authenticated as the
deployment owner, holding `roles/resourcemanager.organizationAdmin` on the target
organization, is a precondition Terraform's credential path depends on. Application Default
Credentials must be re-issued for that account with the target project as quota project before
any apply.

**DNS cutover.** Moving the institution's zone under Cloud DNS requires reviewing the current
authoritative DNS console directly, not just public enumeration, since a public probe can miss
record sets (subdomain CNAMEs, verification TXT records) that must be replicated before
cutover. DNSSEC must be confirmed off before switching. After the registrar nameservers change,
propagation is confirmed complete only once the zone's own registry and multiple independent
public resolvers all return the new delegation and every record set resolves correctly.

**WORM retention lock. Revised 2026-08-24.** Locking a GCS retention policy cannot be undone:
for the locked period the retention cannot be shortened or removed and no object can be deleted,
including by a project owner. That consequence and the storage cost must still be accepted
explicitly before `DOC1_WORM_LOCK_APPROVED=true` is set.

The maintainer's decision for the reference deployment is **not to lock, and to keep retention
short and configurable: 3 days.** The reasoning is that the retention *period* is a policy
number a bank sets under its own governance, while what this deployment needs to demonstrate is
the *routing and immutability mechanism* — that audit records leave the app, land in a bucket
the app cannot delete from, and are covered by a retention policy at all. A 3-day unlocked
policy exercises every part of that except the irreversibility, and it keeps the stack
destroyable, which matters on a first apply.

**What this costs.** An unlocked policy is removable by a project owner, so this deployment does
not evidence tamper-proof retention. Any evidence pack citing it says "retention policy applied,
lock not exercised" rather than claiming WORM. The six-month floor is unchanged for a locked
stack: `retention_days >= 180` still binds whenever `worm_locked = true`, so the compliance
control (P-08) is intact for anyone deploying for real and is relaxed only where the lock is
off. Confirm the exact behaviour in `infra/terraform/variables.tf`.

## 3. Installation and browser policy

| Input | Required value | Review rule |
|---|---|---|
| Installation IDs | `PENDING` | Match `^[A-Za-z0-9_-]{1,128}$` |
| Exact parent origins | `PENDING` | No wildcard |
| Client portal CSP owner | `PENDING` | Approves loader `script-src` and agent `frame-src` |
| Installation manifest secret | `PENDING` | Same secret version mounted into UI and API |
| Manifest version | `PENDING` | Immutable reviewed version |
| Loader URL, SHA-256 and SRI | `PENDING` | From the promoted UI image build |
| Chromium accounts and host | `PENDING` | Target-host test plan |
| Firefox accounts and host | `PENDING` | Target-host test plan |
| WebKit accounts and host | `PENDING` | Target-host test plan |

## 3a. Identity provider decision: Google Cloud Identity

An installation choosing Google Cloud Identity, with no third-party authorization server, is
consistent with the rest of the catalog: most sibling repos ship a working IAP adapter or
verify Google-signed OIDC ID tokens for service-to-service calls, and every reference to Okta,
Auth0, Keycloak or Entra in the workspace sits inside an `onprem/identity.py` placeholder that
raises `NotImplementedError`. Introducing a third-party issuer would make Doc1 the only repo in
the catalog that needs one.

Two consequences follow, and both are load-bearing:

1. **Mode 4 cannot be served by Google, and no exchange changes that.** Mode 4 requires the
   host to already hold a Doc1-audience RFC 9068 access token with protected header
   `typ=at+jwt` and a narrow `cdd.*` scope. Google is not a general-purpose authorization
   server for third-party APIs: its user access tokens are opaque and its JWTs are ID tokens
   carrying an OAuth client id in `aud` and no `scope` claim. Doc1 refuses an ID token as an
   API bearer by design, enforced at
   [`access_token_identity.py`](../src/cdd_sow_research/adapters/oidc/access_token_identity.py).
   A Google-only deployment therefore serves Mode 5 and Mode 6, and section 4 below is marked
   not applicable rather than filled with values no Google tenant can produce.
2. **Mode 5 needs a per-installation subject-token policy naming the accepted type.** Section
   7.3 of `embedding-and-identity.md` sanctions a different external token type through a
   separately configured exchange policy. The code implements this in three places:
   1. the accepted `subject_token_type` is per-installation reviewed configuration in
      [`api/embed.py`](../src/cdd_sow_research/api/embed.py), not a single hardcoded literal, so
      a Google ID token can be accepted where the policy names it;
   2. the sibling verifier
      [`adapters/oidc/id_token_subject.py`](../src/cdd_sow_research/adapters/oidc/id_token_subject.py)
      pins issuer, audience, authorised party, hosted domain and the validity window for the
      Google ID-token profile, alongside the RFC 9068 access-token verifier that still hard-rejects
      anything without protected `typ=at+jwt`;
   3. under the ID-token profile the effective scope comes from reviewed installation policy
      plus the verified `(iss, aud, azp, hd)` tuple, since a Google ID token carries no
      `scope` claim to intersect against.

   What section 5 needs from the institution is not code: it is the dedicated Google OAuth
   client id.

## 4. Mode 4 issuer registration

**Not applicable when the deployment serves `embedded-grant` only.** The preflight requires
every Mode 4 field only when `DOC1_PRODUCTION_IDENTITY_MODE == oauth-access-token`, mirroring
how `DOC1_EMBED_SIGNING_KEY_VERSION` is conditional on `embedded-grant`. The Mode 5 runtime
subject policy and the installation-manifest check both compare tenants against the
mode-neutral `DOC1_DEPLOYMENT_TENANT`; `DOC1_MODE4_TENANT` remains a Mode 4 issuer input and
must equal it whenever a deployment actually serves Mode 4. Every row below stays `PENDING` for
an installation that records Mode 4 as not applicable.

| Input | Required value | Review rule |
|---|---|---|
| Issuer and discovery/JWKS URI | `PENDING` | HTTPS, restricted egress, pinned issuer |
| Algorithms | `PENDING` | RS256 or ES256 only |
| Resource audience | `PENDING` | Exact Doc1 API audience |
| Tenant | `PENDING` | One institution per deployment |
| Allowed clients | `PENDING` | Explicit client IDs |
| Required scopes | `PENDING` | Least privilege |
| Claim mapping | `PENDING` | Server-owned installation and tenant mapping |
| Maximum token lifetime and skew | `PENDING` | Reviewed bounded values |
| Negative-token matrix owner | `PENDING` | Wrong issuer, audience, type, client, scope and tenant |

## 5. Mode 5 BFF and subject policy

The RFC 8693 `id_token` profile described in section 3a is implemented, so filling in the
values below is an input-gathering task, not a code dependency. Two structural differences from
the access-token profile are reviewed explicitly, because a Google ID token cannot carry what
an RFC 9068 access token carries:

- **The subject audience is an OAuth client id, not a broker URL.** A Google ID token's `aud`
  is the registered client, so the distinct-broker-audience rule is satisfied by registering a
  client used for nothing else, not by a `https://doc1-broker...` string. The preflight
  exempts `DOC1_MODE5_SUBJECT_AUDIENCE` from its HTTPS check under this profile.
- **The grant scope cannot come from the token.** Google ID tokens have no custom `scope`
  claim, so `cdd.embed` is established by reviewed installation policy and the verified
  `(iss, aud, azp, hd)` tuple. A scope asserted by the host is never sufficient: the host's
  requested scopes are still intersected with the installation's and the BFF client's
  permitted sets.

Select the profile with `DOC1_MODE5_SUBJECT_TOKEN_TYPE=urn:ietf:params:oauth:token-type:id_token`
and configure the policy under `identity.embedded_grant.id_token_subject_issuers`.

| Input | Required value | Review rule |
|---|---|---|
| Subject-token issuer and audience | Issuer `https://accounts.google.com`; JWKS `https://www.googleapis.com/oauth2/v3/certs`; audience is the dedicated Google OAuth client id, `PENDING` until created | Distinct broker audience |
| Subject client and grant scope | Subject client is the same dedicated Google client (`azp`); grant scope `cdd.embed` established by installation policy, not by a token claim | Least privilege |
| BFF client identity | `PENDING`: the Hrz9 journey portal BFF, registered as a distinct service identity | Registered service identity |
| BFF authentication | `private_key_jwt` | mTLS or `private_key_jwt` |
| BFF public keys and accepted window | `PENDING`, tracked as the key-custody rows of `journey-portal`'s `docs/named-deployment-dossier.md` | Reviewed JWK set and rotation dates |
| Browser-session binding | Authenticated Google Cloud Identity session at the portal, bound to the instance and the verified Google subject | Authenticated user session |
| User-intent control | CSRF token, exact `Origin` matching the portal's origin, and Fetch Metadata policy on the host-facing request | CSRF, Origin and Fetch Metadata evidence |
| Revocation owner and procedure | `PENDING`, tracked in the Hrz9 dossier and rehearsed against its runbook rotation procedure | Tested emergency path |

## 6. Mode 6 fallback

Mode 6 is the only mode Google Cloud Identity satisfies with no new code. Doc1's
`oidc-session` adapter performs Authorization Code plus PKCE, and
[`adapters/oidc/discovery.py`](../src/cdd_sow_research/adapters/oidc/discovery.py) requires only
`authorization_endpoint`, `token_endpoint` and `jwks_uri`, all of which Google publishes. It
treats `end_session_endpoint` as optional, which is exactly Google's omission.

| Input | Required value | Review rule |
|---|---|---|
| Issuer, client and callback registration | `PENDING` | Separate standalone credential type |
| Cookie policy | `Secure`, `HttpOnly`, `SameSite=Lax`, exact path `/agent`, host-only on the standalone fallback origin | Secure, HttpOnly, exact path and SameSite |
| Immutable subject links | Issuer-qualified `(https://accounts.google.com, sub)`; `hd` pinned to the institution's domain; `email` never used as a link | Issuer-qualified links, never email |
| Discovery and JWKS egress | Restricted to `accounts.google.com` and `www.googleapis.com` (JWKS `https://www.googleapis.com/oauth2/v3/certs`) | Restricted to approved hosts |
| Credential separation test | Mode 5 embedded-grant token rejected at Mode 6 endpoints, and the Mode 6 session cookie rejected at Mode 5 resource endpoints | Embedded and standalone reject each other's tokens |

## 7. Shared state, keys and recovery

The selected production state backend is regional Firestore Native. It uses transactions for
state transitions and JTI consume, CMEK, point-in-time recovery and TTL cleanup. The selected
signer is a non-exportable regional Cloud KMS asymmetric key, with HSM as the default protection
level.

| Input | Required value | Review rule |
|---|---|---|
| Firestore database and data owner | `PENDING` | Regional named database |
| Backup and restore owner | `PENDING` | Recovery point and recovery time approved |
| Restore rehearsal | `PENDING` | Multi-replica state and outbox verified |
| Outbox dead-letter owner | `PENDING` | Alert and replay procedure |
| Active signing key version and `kid` | `PENDING` | Exact KMS version |
| Accepted verification keys | `PENDING` | Bounded overlap window |
| Rotation date and owner | `PENDING` | Rehearsed without token outage |
| Emergency revocation | `PENDING` | Version disable and evidence procedure |

## 8. Promotion and evidence pack

Run the gated
[`release-production-images.yaml`](../.github/workflows/release-production-images.yaml) workflow
or [`scripts/promote_production_images.sh`](../scripts/promote_production_images.sh) in the
approved build environment. Promotion requires workload identity or authenticated registry
access, Trivy and Cosign. Record the immutable API and UI image references, loader digest and SRI
emitted by the exact source commit.

The final evidence pack must contain:

- approved Terraform plan and apply record;
- immutable API and UI image digests and signatures;
- installation manifest digest and Secret Manager version;
- application-auth denial and authorized Mode 4/5 results, plus IAP results for the separate
  Mode 6 edge;
- Mode 4, Mode 5 and separate Mode 6 positive and adversarial evidence;
- multi-replica consume, expiry, failover, restore and outbox evidence;
- Chromium, Firefox and WebKit target-host results;
- no-secret log scan;
- rollout, rollback, key rotation, key revocation and incident rehearsal;
- named approvals from the deployment, security, operations and evidence owners.

## 9. Completion decision

| Gate | Status | Evidence |
|---|---|---|
| Repository and reusable infrastructure | Ready | Local gates and Terraform validation |
| Doc1 Mode 5 code for a Google subject | Ready | Section 3a's three changes are implemented: per-installation subject token type, the Google ID-token profile verifier with its negative matrix, and scope derived from reviewed installation policy |
| Named institution inputs | PENDING | No institution named; every row in sections 1 to 7 is `PENDING` or `PLACEHOLDER` |
| Identity provider capability | PARTIAL | Google Cloud Identity can serve Mode 5 and Mode 6; Mode 4 is not applicable and the preflight does not demand it |
| Hrz9 portal half | Ready (code) | `journey-portal` implements `private_key_jwt` signing behind a signing-key port with non-exportable Cloud KMS custody, the BFF JWKS route, CSRF plus exact `Origin` plus Fetch Metadata enforcement before any credential is minted, and a host authorization proof built from the verified principal. A cross-repo fixture verifies a portal-minted assertion against this repo's actual `PrivateKeyJwtVerifier`, with the replay, tamper, expiry, audience, client and foreign-key negatives refused. Its deployment inputs are tracked in its own dossier |
| Controlled pre-production apply | BLOCKED | No institution, project or credentials named yet |
| Production-complete gate | BLOCKED | All eight conditions in `embedding-implementation-plan.md` Section 15.2 must pass |

`scripts/deployment_env.py run -- <command>` is the only supported environment-file runner for
production commands. It validates the complete dossier first, loads both files without
shell evaluation, never passes parsed secrets to child commands, removes inherited `TF_VAR_*`
values, rejects competing variable files and command-line overrides, configures the GCS backend,
maps reviewed values to Terraform variables, keeps VPC-SC in dry-run for the first deployment,
and preserves `worm_locked = true`.
