# Portability FAQ

For architecture, cloud-governance, and exit-planning teams. This FAQ distinguishes what the
repository proves today from the broader portability target. Cross-references:
[`ARCHITECTURE.md`](../../ARCHITECTURE.md) §6
(portability principles), [`docs/onprem-migration.md`](../onprem-migration.md),
[`DEMO.md`](../../DEMO.md) §4 (the live portability tour).

### What does "portable" actually mean here?

The target separates **channel**, **identity**, and **runtime** so each can change without
rewriting the domain core. Data, model, agent-protocol, and audit portability are supporting
dimensions rather than hidden assumptions.

The executable proofs stay dimension-specific:

- `PYTHONPATH=src python scripts/portability_demo.py` verifies the runtime adapter seam,
  local and on-premises interface parity, tamper detection, open audit export/reload,
  complete case/document bundle export and reload on a fresh store (with both forgery
  cases refused), and local identity resolution.
- `HEADLESS=1 python scripts/embed_portability_demo.py --browser all --no-pause --scope full`
  verifies the implemented Modes 4/5 channel and identity layer. The full synthetic run
  uses one immutable artifact in two host origins, RSA and EC issuers with rotation, strict
  MessagePort transport, the brokered PKCE chain, Mode 6 fallback, negative paths, and leak
  scans across Chromium, Firefox, and WebKit.

Neither proof establishes infrastructure, model, complete data, audit-record, sovereign,
production, or whole-system portability. The separate completion gates live in
[`embedding-implementation-plan.md`](../embedding-implementation-plan.md).

### How does the profile switch work?

The pure-domain core speaks only to `typing.Protocol` **ports**. `config/settings.yaml`
binds one adapter per port per runtime profile. `CDD_PROFILE` (or `profile:` in settings)
selects the compute and data stack:

- `local`: a WORKING offline stack (SQLite FTS5 KB, deterministic LLM, regex DLP,
  hash-chained audit). No Google Cloud SDK. The default for dev/test/CI.
- `live`: local documents and local model inference with selected cloud web sources.
- `gcp`: real managed services (Document AI, Agent Search, Gemini, Model Armor, DLP,
  Cloud Logging WORM, Cloud Trace, Gen AI Evals).
- `platform`: sibling-service HTTP clients where contracts exist plus managed adapters
  for vertical-owned capabilities; every reuse is explicit.
- `onprem`: placeholder stubs that still satisfy every Protocol (the sovereign-exit
  target); a primary CLI command exits 2 by design.

No `domain/` code changes across any of these. The contract test
(`tests/contract/test_port_parity.py`) proves both `local` and `onprem` construct and
satisfy all 18 runtime/data ports with no cloud SDK installed. `IdentityPort` is the
separate nineteenth domain port and has its own exact binding matrix.

`CDD_IDENTITY_PROFILE` and `CDD_CHANNEL_PROFILE` are exact independent selectors. Selecting
OIDC, IAP, a Mode 4 access token, a Mode 5 embedded grant, or a local persona does not
change compute or data custody. Missing, disabled, or unsafe combinations fail startup.

### Does the kernel/vertical split affect portability?

It reinforces it. `domain/kernel.py` (citations, LLM envelope, safety, audit, eval) is
vertical-neutral and reusable across products; `domain/models.py` holds the CDD-specific
artifacts. Neither imports a cloud SDK or a framework. A fork for a different vertical can
reuse the kernel and port seams; it must still implement and evidence its own adapters.

### How do we get our data out?

The audit trail exports to JSON Lines (`cdd-sow audit export`), one
`{seq, prev_hash, entry_hash, event}` object per line, and reloads into a fresh store with
the hash chain re-verified line by line (`cdd-sow audit restore`). Records rehydrate to
first-class `AuditEvent` objects (`domain/serialization.py`). The exit story for the audit
trail is "copy the JSONL file", not "migrate a product". Case evidence and dossiers
serialize to open JSON-compatible structures via `to_jsonable`. The standalone UI and API
also export and reload a versioned `cdd-dossier/v1` envelope whose SHA-256 digest is verified
before import.

The complete case/document bundle closes the gap that envelope left. `POST
/v1/cases/{case_id}/bundle/export` (or `cdd-sow bundle export`) produces a
`cdd-case-bundle/v1` archive: a ZIP holding `manifest.json`, `dossier.json` and
`documents/<id>` with each source document's ORIGINAL bytes, no re-encoding. `cdd-sow
bundle restore` reloads it into a different deployment, re-computing every digest first
and keeping each document's original id, so the dossier's citations still resolve to the
same file on the far side. `unzip` and `python -m json.tool` are enough to read it, which
is the point: the exit story is "copy the archive", not "migrate a product".

Two things about that archive are worth stating plainly. Its internal digests are a
CORRUPTION check: a party who rewrites a document and its manifest entry together
produces a self-consistent bundle, so the export also returns the digest of the manifest
(`X-Bundle-Manifest-Sha256`) to be carried out of band, and supplying it on reload is what
makes the bundle tamper-evident. And the tags inside a bundle are provenance only: a
reload files every document under tags derived from the RESTORING side's verified
principal, so an archive edited to carry another tenant's tags gains nothing by it.
`PYTHONPATH=src python scripts/portability_demo.py` exercises all of this, including both
forgeries.

### Is on-prem / sovereign deployment real or aspirational?

The `onprem` adapters are deliberate fail-fast placeholders (they raise
`NotImplementedError`) that nonetheless satisfy every Protocol and construct with a single
`Settings` arg, so the *interface contract* for a sovereign migration is proven and
enforced by CI today. The actual on-prem implementations are the migration work, scoped in
[`docs/onprem-migration.md`](../onprem-migration.md). This repo is not the sovereign-exit
*planner* (that is the sibling **Rsk5 exit-portability planner**: APRA CPS 230, MAS/HKMA
outsourcing); this repo is one of the systems whose exit that planner reasons about.

### Does residency compromise portability?

No: residency is a deploy-time pin (the region, Org Policy resource-location allowlist,
CMEK, VPC-SC), and portability is the ability to change *where* the stack runs by
configuration. They are orthogonal. The region is validated to fail fast, and a second
enterprise or a second region is a tfvars change, not a fork (ARCHITECTURE PT-13). Residency
enforcement infra overlaps with the sibling **Rsk4 residency validator** (a CI gate for
region violations), which a fork should run rather than re-implement.

### What is NOT yet portable?

Modes 4/5 implementation and full synthetic channel/identity conformance are complete.
Production enablement still requires named external IdP and BFF registrations, dedicated
DNS/TLS/hosting, shared production BrowserFlow and JTI stores, production key custody and
rotation, approved origins and CSP, target-hosting cross-browser evidence, and a separately
deployed Mode 6 fallback.

The `onprem` adapters are fail-fast contract placeholders, not working sovereign
implementations. A production on-premises claim therefore remains client-specific migration
work even though interface drift is checked in CI. Complete case/document export plus
restore/reload also remains separate; the executable data portability proof is audit-only.
