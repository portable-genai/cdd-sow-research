# Doc1 named production runbook

This runbook operates the reusable production foundation. It does not authorize an apply.
Complete and approve [`named-production-deployment-dossier.md`](named-production-deployment-dossier.md)
for one institution first.

## Preflight

1. Copy `.env.example` to `.env` and `.env.secrets.example` to `.env.secrets`. Put only
   non-secret decisions in `.env` and only secret payloads in `.env.secrets`. Optional settings
   are commented because unset inherits a reviewed default while configured-empty is refused;
   uncomment only the settings you deliberately fill. Keep
   `DOC1_DEPLOYMENT_ENABLED=false` while drafting.
2. Run `make deploy-env-check`, replace every placeholder, record the required approvals, set
   `DOC1_DEPLOYMENT_ENABLED=true`, then run `make deploy-preflight`. The latter is offline and
   fails closed on missing Modes 4, 5 or 6 registrations, DNS, owners, images, secret versions,
   an unapproved WORM lock, fewer than two replicas, missing alert channels, local Terraform
   state, or VPC-SC enforcement before clean dry-run evidence.
3. Create the reviewed manifest and runtime-settings Secret Manager versions, then run
   `make deploy-verify-secrets`. This reads each exact numeric version into memory, compares its
   SHA-256 with the reviewed local payload, and does not write or print the payload.
4. Confirm API and UI images contain `@sha256:` and have valid Cosign verification.
5. Confirm the loader SHA-256 and SRI came from the same UI build.
6. Confirm the runtime settings and installation manifest are separate reviewed Secret Manager
   versions. Mount the same manifest version into UI and API.
7. Confirm the Mode 5 active key uses the Terraform `embed_signing_key_version` output. Production
   startup rejects an exported private-key environment variable.
8. Confirm the selected region is approved, VPC-SC is in dry-run, alert channels are attached,
   Firestore point-in-time recovery is enabled, and WORM retention was explicitly approved.
9. Run `terraform fmt -check -recursive`, `terraform validate`, and `terraform plan` through
   `scripts/deployment_env.py run -- <command>` so the reviewed environment contract is loaded.
   The runner accepts only `init`, `validate`, `plan`, and interactive `apply`, each with a
   closed safe-option list. It rejects destroy, state, import, taint, workspace, targeting,
   replacement, refresh-only, auto-approval, saved plans, and every unknown command or option.
   Every plan and apply re-verifies the exact remote secret versions. Saved-plan apply is
   forbidden because an arbitrary plan file is not cryptographically bound to those inputs;
   approved applies run without a plan-file argument and the generated plan is reviewed
   interactively. The runner also queries the WORM bucket: `new` lifecycle requires a verified
   not-found response, while `existing` requires a locked bucket whose live retention exactly
   matches the reviewed existing value.
10. Review every create, replace and irreversible operation. Never approve an unexpected service,
   region, public ingress path, key replacement or retention lock.

## Rollout

1. For Mode 5, set `DOC1_DEPLOYMENT_STAGE=mode5-key-bootstrap` and run the reviewed plan and
   interactive apply. The runner maps this stage to `enable_embed_signing_key = true` and
   `production_edge_enabled = false`, defers edge-only inputs, and skips remote verification of
   edge secrets that no resource consumes yet. Record `embed_signing_key_version`, place that
   exact output in `DOC1_EMBED_SIGNING_KEY_VERSION` and the reviewed runtime settings, create a
   new immutable settings secret version, then set
   `DOC1_DEPLOYMENT_STAGE=production-edge`. The runner checks the runtime value against the
   reviewed project, region, key ring, key and version, while Terraform checks it against the
   actual bootstrap resource. Review the second plan and run the interactive apply without a
   saved plan-file argument. Never guess a future key version or export private material. Mode 4
   starts directly with `production-edge` and does not create this key.
2. Apply the complete edge to controlled pre-production with `vpc_sc_enforce = false`.
3. Verify direct `run.app` requests fail while the load balancer path is reachable.
4. Verify `/agent/ready` returns the reviewed manifest and build identifiers. Then verify the
   loader and iframe are reachable without an IAP redirect and prove unauthenticated application
   API denial and authorized Mode 4/5 `/agent/api` routing. Exercise IAP only on the separately
   deployed standalone Mode 6 edge.
5. Compare the UI health manifest digest with the API health manifest digest.
6. Run two or more API replicas and exercise one issuance and one consume under concurrency.
7. Exercise the Cloud Armor per-source throttle from one source and prove a different source can
   still register while shared Firestore capacity remains available. Attach alerting to both the
   HTTP 429 rate and the shared broker-limit rejection rate.
8. Run the complete target-host browser matrix and no-secret scans.
9. Promote the same immutable digests. Do not rebuild between pre-production and production.

## Rollback

1. Keep the previous Cloud Run revisions and image digests until the observation window closes.
2. Route traffic back to the previous UI and API revisions together. Do not mix manifests.
3. Re-run health, manifest digest, application-auth and credential-type separation checks,
   including IAP on the separate Mode 6 edge when it is part of the installation.
4. Do not roll back Firestore state or a signing key automatically. Follow the recovery and key
   procedures below.
5. Record the reason, revisions, timestamps, operator and verification result in the evidence
   pack.

## Firestore recovery

1. Stop issuance while allowing read-only diagnosis.
2. Record the database recovery point and affected flow time range.
3. Restore through the approved point-in-time recovery procedure into a reviewed target.
4. Verify one-issuance and one-consume semantics, JTI replay rejection and pending outbox state.
5. Resume only after the security and operations owners approve the recovered state.

TTL cleanup is asynchronous. Authorization always checks expiry in the transaction and must not
depend on TTL deletion.

## Signing-key rotation

1. Create a new asymmetric key version or approved replacement key and publish its public key
   under a new `kid`.
2. Add the new public verification key while retaining the previous public key.
3. Set the new `kid` and KMS version active, deploy, and verify newly minted tokens.
4. Wait for the maximum token lifetime, clock skew and accepted overlap window.
5. Remove the previous public key and disable its KMS version.
6. Record both verification windows and the disable operation.

For emergency revocation, stop issuance, disable the affected KMS version, remove its accepted
public key, revoke relevant BFF registrations, invalidate active sessions, verify rejection, and
open an incident. Never export or copy private signing material.

## Monitoring and incidents

Alert on authentication failures, grant replay, tenant or installation mismatch, CSP/frame
denials, outbox backlog, Firestore errors, KMS signing failures, key changes, VPC-SC denials and
Cloud Run error rates. Every alert must have a named channel and owner in the dossier.

The incident record must include safe correlation IDs, image and manifest digests, service
revisions and timestamps. Do not include tokens, assertions, private keys, customer documents or
PII.
