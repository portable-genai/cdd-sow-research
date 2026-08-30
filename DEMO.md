# Demo guide: Doc1 CDD + Source-of-Wealth Agent

Step-by-step scripts for demoing Doc1 five ways:

- **Demo A: Long-running, auditable Source-of-Wealth case** (the headline flow): an RM
  closes evidence gaps with a client over several rounds, the system reconciles declared
  vs evidenced wealth, raises gaps and client information requests (RFIs), and an MLRO
  approves under four-eyes. Runs **fully offline** (no cloud, no API key).
- **Demo B: One-shot CDD dossier on the managed GCP stack**: a single cited dossier
  (source-of-wealth narrative, risk rating, adverse media, UBO) produced against real
  Document AI / Gemini / Model Armor / DLP in the selected region (default
  `asia-southeast1`, configurable).
- **Demo C: Current portability-seam tour**: one offline script exercises a one-line runtime
  profile swap, interface parity across all 18 runtime/data ports, a separately selected
  local identity, a tamper-evident hash-chained audit trail, and an open-format
  export/reload round-trip. It does not
  prove cross-host or cross-issuer behavior. Runs **fully offline** (§4).
- **Demo D: A real subject and real documents** (§5): the working application. Upload an
  actual KYC pack, name an actual company or person, and get a dossier grounded in those
  files with a working link on every citation. Documents are read by a model on your own
  machine; only the subject's name is searched on the web.
- **Demo E: Portable laptop UI** (§6): the real standalone Next.js UI and FastAPI backend.
  It shows the capability manifest, runs the fictional CDD workflow, and exports then
  reloads an integrity-checked open dossier. A separate embed runner proves the same UI
  delivery artifact can be mounted in reviewed host applications.

> Demos A to C run on **fictional** synthetic KYC data. Demo D reads whatever you upload:
> it is still a reference build, so do not point it at live customer data without your own
> legal, security and model-risk sign-off.

---

## 0. Prerequisites

| Need | Demo A (local) | Demo B (GCP) | Demo C (local) | Demo D (live) | Notes |
|------|:--:|:--:|:--:|:--:|-------|
| `git` | ✅ | ✅ | ✅ | ✅ | clone the repo |
| **Python 3.12+** | ✅ | ✅ | ✅ | ✅ | the package pins `>=3.12` |
| Node.js 18+ & npm | for the UI / Playwright | for the UI | n/a | ✅ | Demo D is driven from the UI |
| **Playwright** (`pip install -e ".[demo]"` + `playwright install chromium`) | for the guided walkthrough | n/a | n/a | n/a | Demo A's presenter walkthrough (pinned in the `[demo]` extra) |
| A GCP project + `gcloud` | n/a | ✅ | n/a | ✅ | Demo D needs only ADC + a project for the grounded web searches |
| Terraform | n/a | ✅ | n/a | n/a | provisions Document AI, DLP, WORM bucket, CMEK |
| Cloud KMS key (regional) | n/a | ✅ | n/a | n/a | CMEK; set `CDD_KMS_KEY` |
| `pip install -e ".[live]"` | n/a | n/a | n/a | ✅ | pypdf, pypdfium2, pillow, google-genai |

Install/setup references (read these once):

- Local install & profiles → [README §4.1 `local`](README.md#41-local-profile-a-working-offline-run-no-gcp)
- The live profile (real documents, Gemini API) → [README §4.2 `live`](README.md#42-live-profile-real-documents-real-subjects-on-your-own-machine)
- GCP install & deploy → [README §4.4 `gcp`](README.md#44-gcp-profile-real-managed-stack-in-asia-southeast1) and [`docs/runbook.md`](docs/runbook.md#1-deploy)
- Running the surfaces (API / CLI / UI) → [README §5](README.md#5-running-the-surfaces)
- Deployment profiles explained → [SPEC §1 “Deployment profiles”](SPEC.md#deployment-profiles)
- The demo scripts → [`scripts/README.md`](scripts/README.md)
- The UI console → [`ui/README.md`](ui/README.md)
- What the SoW-case flow is → [`docs/sow-longitudinal-audit-design.md`](docs/sow-longitudinal-audit-design.md)
- Config (`${ENV_VAR}` resolved at load) → [`config/settings.yaml`](config/settings.yaml)

---

## 1. Common setup (all demos)

```bash
git clone https://github.com/portable-genai/cdd-sow-research.git
cd cdd-sow-research

python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # core + dev tooling (NO google-cloud-* packages)

# Sanity check the offline stack before presenting:
export CDD_PROFILE=local
make lint test                   # ruff + mypy + pytest (all local, no cloud)
```

See [README §4.1](README.md#41-local-profile-a-working-offline-run-no-gcp) for details.

---

## 2. Demo A: Long-running, auditable SoW case (local, offline)

The case flow uses an in-process case store, so it needs **no Google Cloud and no API
key**, ideal for a laptop demo. Three ways to present it, in order of polish.

### 2.1 Guided, presenter-controlled walkthrough (recommended)

**One command, one terminal.** A real browser opens showing only the clean application UI;
the script narrates each step **on the terminal (the Playwright console)** and waits for
you, so you read the narration aloud while the audience sees just the product. It starts
its own demo server and stops it on exit. (One-time: `pip install -e ".[demo]" &&
playwright install chromium`.)

```bash
source .venv/bin/activate
make demo            # or: PYTHONPATH=src python scripts/sow_demo_playwright.py
```

At each prompt: **Enter** runs the step, **b** goes back, a **number** (`1`-`10`) jumps to
that step, **q** quits. The walkthrough asserts the live page state after every step (it
reads `/state` and checks the coverage % and gap counts), so the narration can never drift
from what the deterministic engine actually computed.

The ten steps:

1. **Case opened**: client declares Source of Wealth (declared net worth USD 60m–100m), no evidence yet.
2. **Round 0**: first document pack → coverage ~69%, **5 gaps**, client RFIs drafted.
3. **Round 1**: client responds (registry extract, employment letter, probate) → ~82%, 2 gaps.
4. **Round 2**: fresh brokerage statement → ~86%, **0 gaps**, ready for review.
5. **Key individuals**: UBOs (≥25%) + directors get CDD + a SoW sub-case; a PEP UBO escalates.
6. **Screening**: every in-scope party screened against the sanctions / PEP / watchlist snapshot.
7. **Risk scorecard**: deterministic score → band + CDD tier; hard signals force **PROHIBITED / EDD**.
8. **Source of Funds**: declared inflows vs evidenced advices → ~60% coverage, 2 HIGH gaps.
9. **Ongoing monitoring**: risk-based cadence + event triggers → **OVERDUE**, 4 triggers.
10. **Maker-checker**: MLRO approves under four-eyes; an immutable hash-chained snapshot is sealed.

The walkthrough finishes on the **timeline recap** page. Full options (`SLOWMO_MS`,
`HEADLESS`, `DEMO_AUTO`, `DEMO_URL`, `DEMO_PORT`, `CHROME_PATH`) are in
[`scripts/README.md`](scripts/README.md). To attach to an already-running server instead of
spawning one, set `DEMO_URL`. `make demo-selftest` runs it headless and unattended (the CI
smoke test).

### 2.2 Manual, click-through (no Playwright)

Run only the server and drive it yourself in any browser:

```bash
PYTHONPATH=src python scripts/sow_demo_server.py     # http://localhost:8099
```

Open `http://localhost:8099` and click **Next ▶** to advance the real case, **Restart** to
reset. It advances the same `STEPS` sequence in `scripts/sow_demo_server.py` that the
walkthrough above narrates, so the steps are the ones listed there rather than a second list
kept in step with them here.

### 2.3 Static artifacts (slides / screenshots)

Generate the audit-first pages and JSON without a browser:

```bash
PYTHONPATH=src python scripts/sow_demo.py sow_demo.json        # prints the round-by-round summary
PYTHONPATH=src python scripts/render_sow_ui.py sow_demo.json ./out
# -> ./out/sow-iter-0.html, sow-iter-1.html, sow-iter-2.html, sow-timeline.html
```

### 2.4 One-shot dossier via the CLI (quick variant)

If you only want to show a single cited dossier (not the multi-round case):

```bash
export CDD_PROFILE=local
cdd-sow assess "Acme Holdings Pte Ltd (FICTIONAL)" --type entity --jurisdiction SG
```

### 2.5 Company + its key individuals (one system for both)

Onboarding a **company** also runs CDD on its key individuals (UBOs ≥25% + directors) and a
full SoW sub-case on the source-of-funds individuals, rolled up into the company case:

```bash
PYTHONPATH=src python scripts/related_party_demo.py rp_demo.json
PYTHONPATH=src python scripts/render_sow_ui.py rp_demo.json ./out   # ./out/sow-iter-2.html
```

The company case shows the usual reconciliation/gaps **plus** a **"Key individuals: CDD +
SoW"** panel: each in-scope person with their screening flags (PEP / sanctions / adverse
media / ID) and their own SoW sub-case status. A PEP UBO escalates the parent to **enhanced
review** (soft, the checker still disposes). This is the same flow whether the client is a
company or an individual; see [design §15](docs/sow-longitudinal-audit-design.md).

### 2.6 Sanctions / PEP / watchlist screening

The company + key-individuals demo also screens every in-scope party against a synced watchlist snapshot (OFAC SDN + Consolidated, UN, EU, UK HMT, PEP). Hits become alerts an analyst dispositions (soft escalation). Offline it uses a bundled **fictional** snapshot; refresh real data (egress required) with:

```bash
PYTHONPATH=src python scripts/sync_sanctions.py --out src/cdd_sow_research/adapters/local/data/sanctions_snapshot.json
```

The `related_party_demo.py` output's audit view gains a **“Sanctions / PEP / watchlist screening”** panel (e.g. the PEP UBO matches OFAC SDN). See [design §16](docs/sow-longitudinal-audit-design.md).

### 2.7 Risk-based scorecard + CDD tier (SDD/CDD/EDD)

The same `related_party_demo.py` run computes a deterministic **risk scorecard** (customer type, country/FATF risk, product, channel, PEP, adverse media) and a **CDD tier**. Hard signals (open sanctions hit, PEP, FATF call-for-action country) force **EDD** and raise the band. The audit view gains a **“Risk scorecard: CDD tier”** panel (e.g. Acme → PROHIBITED / EDD). See [design §17](docs/sow-longitudinal-audit-design.md).

### 2.8 Source of Funds (distinct from Source of Wealth)

The same `related_party_demo.py` run also reconciles **Source of Funds** (the client's declared expected inflows + expected-activity profile against the evidenced credit advices) separately from the total-wealth SoW. The deterministic engine flags `unevidenced_inflow`, `unexpected_inflow`, `missing_origin_doc` and `activity_mismatch` gaps. In the synthetic Acme scenario it reports **60 % coverage** with two HIGH gaps (a declared asset-sale with no credit advice; an undeclared gift inflow), surfaced in a **“Source of Funds: declared inflows vs evidenced”** panel. Gaps escalate softly; a checker still disposes. See [design §18](docs/sow-longitudinal-audit-design.md).

### 2.9 Ongoing monitoring / periodic review

The same `related_party_demo.py` run computes the **ongoing-monitoring** outcome: a risk-based review cadence keyed off the CDD tier (EDD annual, CDD 3-yearly, SDD 5-yearly) plus event triggers derived from the case's screening, PEP and Source-of-Funds signals. In the synthetic Acme scenario the EDD relationship is **OVERDUE** with four ranked re-review triggers (overdue periodic review, open sanctions hit, SoF unusual-activity, PEP exposure), shown in an **“Ongoing monitoring: periodic review”** panel. Triggers/overdue escalate softly; a checker still disposes. See [design §19](docs/sow-longitudinal-audit-design.md).

### 2.10 Perpetual KYC: what changed, and why it is queued

Ongoing monitoring above answers *when* the relationship is next due. Perpetual KYC answers
*what moved since we last looked*. Run two cycles offline and watch the second one react:

```bash
export CDD_PROFILE=local
# 1. First cycle: establishes the baseline. Nothing has "changed" yet, by definition.
cdd-sow perpetual-kyc "Acme Holdings Pte Ltd (FICTIONAL)" --jurisdiction SG --as-of 2026-08-05

# 2. The tenant's explainable review queue, most urgent first.
cdd-sow perpetual-kyc-queue
```

What to point at on screen:

- **The score arithmetic, line by line.** Each signal contributes a signed uplift with the
  reason next to it. There is no black box: an auditor can recompute the total by hand from
  the policy weights in `config/settings.yaml`.
- **The signal ledger.** Every signal is tagged `new`, `persisting` or `cleared` against the
  stored baseline, with the citation behind it. A first run is all `persisting` on purpose:
  the run establishes the baseline rather than crying wolf about a standing picture.
- **The queue placement.** Priority and the disposition-due date are both computed from
  bank policy, not chosen by a model.
- **The human-review banner and the Hrz7 routing line.** Nothing was blocked, frozen or
  downgraded: the item is queued for a checker.
- **Replayability.** Re-run the same command with the same `--as-of` and the output is
  identical, which is what makes the control auditable.

In the browser console (`make run-api` and `make run-ui`), assess a subject and the
**Perpetual KYC** panel appears under the dossier with the same content: run a cycle, read
the arithmetic, and refresh the tenant-scoped queue.

All sanctions and adverse-media data under the `local` profile is an obviously fictional
bundled fixture. A fixture hit is never a sanctions determination about a real party.

---

## 3. Demo B: One-shot CDD dossier on the managed GCP stack

Shows the same domain producing a cited dossier against **real managed services** in
`asia-southeast1`. Follow [`docs/runbook.md`](docs/runbook.md#1-deploy) for the
authoritative deploy steps; the short version:

### 3.1 GCP setup

```bash
source .venv/bin/activate
pip install -e ".[gcp,dev]"                 # adds google-adk, google-genai, documentai, dlp, ...

export GOOGLE_CLOUD_PROJECT=your-sg-project
export CDD_PROFILE=gcp
export CDD_KMS_KEY="projects/.../locations/asia-southeast1/keyRings/.../cryptoKeys/..."
gcloud auth application-default login
```

### 3.2 Provision infra (one-time)

Copy [`infra/terraform/terraform.tfvars.example`](infra/terraform/terraform.tfvars.example)
to `terraform.tfvars` and pick a scenario: **A** quick evaluation deploy (project-scoped,
no org-level roles: `enable_vpc_sc=false`, `enable_org_policies=false`, `worm_locked=false`),
**B** full sovereign posture, or **C** a second instance / second enterprise (`name_prefix`,
another `region`). Then:

```bash
make tf-plan          # review the plan; with worm_locked=true the bucket lock is IRREVERSIBLE
cd infra/terraform && terraform apply && cd ../..
# Export the outputs the app reads (see docs/runbook.md §1):
export CDD_DOCAI_PROCESSOR="$(terraform -chdir=infra/terraform output -raw documentai_processor_id)"
export CDD_DLP_INSPECT_TEMPLATE="$(terraform -chdir=infra/terraform output -raw dlp_inspect_template)"
export CDD_DLP_DEIDENTIFY_TEMPLATE="$(terraform -chdir=infra/terraform output -raw dlp_deidentify_template)"
export CDD_MODEL_ARMOR_TEMPLATE="$(terraform -chdir=infra/terraform output -raw model_armor_template)"
export CDD_LOG_NAME="$(terraform -chdir=infra/terraform output -raw audit_log_name)"
export CDD_SANCTIONS_BUCKET="$(terraform -chdir=infra/terraform output -raw sanctions_bucket)"
```

Details and gotchas (region fail-fast, key rotation, retention): [`docs/runbook.md`](docs/runbook.md).

### 3.3 Run and show

```bash
make run-api          # FastAPI on :8090, profile=gcp
```

Then demo any surface ([README §5](README.md#5-running-the-surfaces)):

```bash
# REST: produce a dossier
curl -s localhost:8090/v1/cdd -H 'content-type: application/json' -d '{
  "subject": {"id":"acme","name":"Acme Holdings Pte Ltd (FICTIONAL)","type":"entity","jurisdiction":"SG"},
  "documents": [{"id":"acme-registry","doc_type":"registry_extract","acl_tags":["case:acme"]}],
  "actor": "analyst@bank.test"
}' | python -m json.tool

# Agent card / health
curl -s localhost:8090/.well-known/agent-card.json | python -m json.tool
curl -s localhost:8090/healthz
```

Or the browser console (talks to the API on :8090), see [`ui/README.md`](ui/README.md):

```bash
make run-ui           # http://localhost:3000
```

**What to highlight:** every claim carries a source-and-page **citation**; PII is redacted
before any model/index/audit call; the dossier is **always** marked human-review
(maker-checker); everything stays in `asia-southeast1` with CMEK + VPC-SC
([README §8](README.md#8-security-and-residency-posture)).

> **Note on the SoW *case* flow under GCP.** The managed `FirestoreCaseStoreAdapter` is
> implemented and bound for `gcp`/`platform`, with a credentialed integration test. Demo A
> intentionally uses the local store for repeatability; Demo B covers the one-shot dossier
> and is not evidence of the managed longitudinal-case path.

---

## 4. Demo C: Current portability-seam tour (local, offline, ~1 minute)

This demonstrates the portability controls that are executable today. The offline seam tour needs
only the common setup from §1. The browser proof adds cross-origin channel evidence with
Playwright, and full scope adds synthetic external-issuer identity. Neither scope claims
target production hosting or whole-system portability.

### 4.1 The one-command tour

```bash
PYTHONPATH=src python scripts/portability_demo.py
```

Five acts print PASS/FAIL checks and end with a scoreboard for the bounded seam, compute, and
audit-data claims that the script actually exercises:

1. **One-line profile swap (compute).** The same assessment runs offline under
   `CDD_PROFILE=local` (grounded, cited, maker-checker held) and **fails fast** under
   `CDD_PROFILE=onprem` (the sovereign-migration placeholder). The script prints the
   `config/settings.yaml` adapter bindings that made the difference: configuration,
   not code.
2. **Interface parity.** All 18 runtime/data ports instantiate and satisfy their Protocols under both
   SDK-free profiles; the printed table is the live port-to-adapter matrix from
   [ARCHITECTURE §2](ARCHITECTURE.md).
3. **Tamper-evident audit (data).** The local audit store is hash-chained
   (`entry_hash = SHA-256(prev_hash || record)`). The script doctors a *copy* of the
   store (silently softening an ESCALATED decision to ALLOWED) and `verify_chain` names
   the exact broken record.
4. **Open-format round-trip (data).** The trail exports to documented JSON Lines,
   reloads into a **fresh** store with the chain re-verified line by line, and records
   rehydrate to first-class domain objects.
5. **Identity seam.** Seeded dev personas resolve offline with per-user entitlements (no
   IdP/AD/LDAP), and the script prints the configured `IdentityPort` bindings for IAP, OIDC
   session, and on-premises modes. It does not execute an external issuer or a host-to-iframe
   sign-in flow.

Exit code 0 only if every named check passes. See
[`docs/embedding-implementation-plan.md`](docs/embedding-implementation-plan.md) Phase 5 for
the remaining external-issuer and production evidence.

### 4.2 Presenter-paced two-host browser evidence

Install the pinned evidence dependency and browsers once:

```bash
pip install playwright==1.61.0
python -m playwright install chromium firefox webkit
cd ui && npm ci && cd ..
```

The default command opens a visible Chromium browser, prints two to four spoken narration
sentences only in the terminal, reaches stable visible proof, and then pauses for Enter after
every step:

```bash
python scripts/embed_portability_demo.py
```

The five channel steps show the fixed `/agent` artifact in two registered parent origins,
compare one versioned loader digest, reject a credential placed in the global bootstrap message,
and deny an unregistered parent before following the manifest-owned standalone fallback. Full
scope adds direct institutional token identity and the BFF-authorized embedded grant as two
production-module synthetic browser steps.

Useful controls:

```bash
python scripts/embed_portability_demo.py --list
python scripts/embed_portability_demo.py --from handshake-boundary
HEADLESS=1 python scripts/embed_portability_demo.py \
  --browser all --no-pause --scope full \
  --screenshots artifacts/portability/screenshots \
  --evidence artifacts/portability/evidence.json
```

The fixtures are fictional and loopback-only. Full scope is executable evidence for channel
portability, browser-boundary behavior, two pinned Mode 4 issuer policies, and the Mode 5
BFF/broker/dedicated-token chain. It fails closed with a dimension-specific exit if any required
proof is incomplete. It does not prove infrastructure, model, data, audit-record, or
whole-system portability.

A complete `--browser all --scope full` run passes Chromium, Firefox, and WebKit, captures
21 screenshots, exercises RSA and EC issuers plus key rotation, and records the Mode 4,
Mode 5, negative-path, and leak-scan results in the evidence JSON. These are local
synthetic results through the production verifier and broker modules. Named registrations,
hosting, shared stores, key custody, approved origins/CSP, target-host browser evidence,
and the separately deployed Mode 6 fallback remain production work.

### 4.3 The same features, by hand

```bash
# Profile swap: identical command, two worlds. onprem exits 2 with the migration note.
CDD_PROFILE=local  cdd-sow assess "Acme Holdings Pte Ltd (FICTIONAL)" --type entity
CDD_PROFILE=onprem cdd-sow assess "Acme Holdings Pte Ltd (FICTIONAL)" --type entity; echo "exit=$?"

# Parity proof: structural (18 runtime/data ports x local/onprem) + behavioral (the same request
# through the local in-process adapter, the platform HTTP client, and the onprem stub).
pytest tests/contract -q

# Tamper-evident, exportable audit trail (throwaway store under /tmp):
export CDD_PROFILE=local CDD_LOCAL_AUDIT=/tmp/cdd-demo-audit.db
cdd-sow assess "Acme Holdings Pte Ltd (FICTIONAL)" --type entity >/dev/null
cdd-sow audit verify                        # chain INTACT: n entries (n chained)
cdd-sow audit export /tmp/cdd-audit.jsonl   # open format, hashes included
sqlite3 /tmp/cdd-demo-audit.db "DROP TRIGGER audit_log_no_update; UPDATE audit_log SET event_json = replace(event_json, 'escalated', 'allowed') WHERE seq = 1"   # WORM triggers block UPDATE/DELETE; an attacker must drop them first, and the chain still catches the edit
cdd-sow audit verify; echo "exit=$?"        # chain BROKEN at seq 1, exit 1
CDD_LOCAL_AUDIT=/tmp/cdd-restored.db cdd-sow audit restore /tmp/cdd-audit.jsonl   # reload elsewhere, verified

# Complete case exit: the dossier AND the bytes it cites, out and back on a fresh store.
export CDD_LOCAL_DOCUMENTS=/tmp/cdd-demo-documents.db
cdd-sow bundle export acme-holdings /tmp/acme-case.zip --tenant bank-test
unzip -l /tmp/acme-case.zip                 # manifest.json, dossier.json, documents/<id>
CDD_LOCAL_DOCUMENTS=/tmp/cdd-restored-documents.db cdd-sow bundle restore \
  acme-holdings /tmp/acme-case.zip --tenant bank-test \
  --manifest-sha256 "$(python -c "import json;print(json.load(open('/tmp/acme-case.zip.manifest.json'))['manifest_sha256'])")"

# Identity: seeded personas (no IdP). With `make run-api` running:
curl -s localhost:8090/v1/personas | python -m json.tool
```

**What to say:** the domain/runtime seam, the audit format and the case/document bundle are
all exercised, the local identity adapter is real, and the sovereign adapter contract fails
closed. The bundle is the answer to "how do we get the actual files out": an ordinary ZIP
with a JSON manifest, whose documents keep their ids so the dossier's citations still
resolve after the move, and whose manifest digest travels separately so a rewritten archive
is caught. Then state the boundary: the cross-host channel plus Mode 4 and Mode 5 identity
chains are browser-tested, while infrastructure and model portability remain separate
evidence dimensions.

---

## 5. Demo D: A real subject and real documents (`live` profile)

Demos A and B run on fictional fixtures, which is what makes them repeatable. This one is
the working application: upload an actual KYC pack for an actual company or person, and
get a dossier grounded in those files, cited page by page, with a link on every citation
that opens the page it came from.

What makes this demoable: **custody stays on the machine, and the model is the same
Gemini the deployment runs.** The evidence index, the uploaded files and the audit trail
live in local SQLite; extraction, drafting, risk rating and the adverse-media and
corporate-registry web searches all call the Gemini API. There is deliberately no local
model — this use case needs internet research, so it is only implemented for customers
who permit leaving the data centre (org decision, 2026-08-30) — and the UI banner states
on every page that the runtime is local and the model is Gemini.

### 5.1 Setup

```bash
pip install -e ".[live,dev]"

# Credentials: every model call in this profile is the Gemini API.
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=your-project
export CDD_PROFILE=live
export CDD_TRIAGE_MODEL=gemini-3.5-flash    # pin a model your region actually serves

# Real watchlists, so screening a real name can actually hit (one-time, ~20k entries):
PYTHONPATH=src python scripts/sync_sanctions.py --out ~/.cdd_sow_research/sanctions_snapshot.json

make run-api        # :8090
make run-ui         # :3000
```

### 5.2 The flow to show

1. **Name the subject.** Type the real legal name of a company or person. No fictional
   default: an empty field is the honest starting state.
2. **Upload the KYC pack.** Drop in PDFs (bank statements, financial statements, registry
   extracts, ID pages), images, or text files. Each is filed against the case under its
   own ACL and listed with its size and, after assessment, its page count.
3. **Build the dossier.** This takes minutes, not seconds, and the button says so with a
   running clock: several model calls plus one transcription per scanned page. That is
   the cost of a dossier grounded in the actual evidence.
4. **Follow a citation.** Click `source` on any claim. It opens the uploaded document at
   the cited page. This is the point of the whole exercise: a claim a reviewer cannot
   check is not evidence.
5. **Read what is NOT there.** A page that could not be read is marked unreadable rather
   than passed off as blank, and a case with nothing readable indexed is refused as
   ungrounded rather than answered from the model's own knowledge.
6. **The maker-checker gate still holds.** The dossier is still `requires_human_review`,
   still routed to Hrz7. Nothing about running on real data relaxes P-06.

### 5.3 What to watch out for

- **A regional model 404 looks like a clean result.** A triage model that is not served in
  your region makes adverse media and UBO come back empty, which reads as "nothing
  found". Pin the ids your region serves.
- **A fictional watchlist also looks like a clean result.** Without the sync above,
  screening a real name against the bundled fixture can only ever return no match. The
  provider logs a warning when a live run falls through to the fixture.
- The [COMPLIANCE.md](COMPLIANCE.md) position is unchanged: this is a reference build.
  Real customer data needs your own legal, security and model-risk sign-off first.

---

## 6. Demo E: Portable laptop UI

This walkthrough uses the actual standalone Next.js UI and FastAPI local profile. It
starts isolated processes when the stack is not already running, opens a visible browser,
prints narration only in the presenter terminal, and pauses after every completed step.

One-time setup:

```bash
pip install -e ".[demo]"
playwright install chromium
cd ui && npm install && cd ..
```

Present it:

```bash
make laptop-demo
```

Useful controls:

```bash
python scripts/laptop_demo_playwright.py --list
python scripts/laptop_demo_playwright.py --from dossier
python scripts/laptop_demo_playwright.py --no-pause --screenshots ./out/laptop-demo
make laptop-demo-selftest
```

The data is fictional. The script visibly proves three bounded claims: the laptop
workflow works, unavailable managed assurance is labelled honestly, and a dossier can
export and reload through the open `cdd-dossier/v1` format with an API-verified SHA-256
digest. Run `python scripts/embed_portability_demo.py` separately for the reviewed
cross-host channel and embedded identity proof. An embed alone does not prove runtime,
model, data, or audit-record portability.

The runner owns only temporary local databases and stops only the API/UI processes it
started. An attached stack is left running.

---

## 7. Talking points

- **It's a workflow, not a one-shot.** SoW clearance takes weeks of RM↔client iterations;
  the case is stateful and append-only, so you can always answer “on what evidence,
  provided when, did we clear this?”.
- **The system does the math, deterministically.** Reconciliation and gap detection are
  pure functions (replayable by an auditor); the LLM only narrates and drafts RFI wording.
- **Audit-first output.** Sources grouped at a glance, each proven by a citation, with
  computed gaps and the exact documents to request from the client.
- **Guardrails hold.** Redact-before-everything, WORM audit, case-scoped ACL, four-eyes
  maker-checker, single-region + CMEK residency.
- **Portability with an evidence boundary.** Demo C proves the adapter seam, local identity,
  tamper detection, open audit round trip, two-host browser channel claims, and
  production-module synthetic Mode 4 and Mode 5 identity chains; no one dimension proves
  whole-system portability.

---

## 8. Troubleshooting & cleanup

| Symptom | Fix |
|---------|-----|
| `python3.12: command not found` | Install Python 3.12+; the package pins `>=3.12`. |
| Playwright: “executable doesn't exist” | `playwright install chromium`, or set `CHROME_PATH=/path/to/chrome`. |
| Portability runner reports `FAIL identity.mode4` or `identity.mode5` | The named production-module synthetic browser identity chain is incomplete. Read the dimension-specific failure, rerun from `identity-mode4` or `identity-mode5`, and inspect the generated evidence. |
| No display for the headed walkthrough | Use §2.2 (manual browser) on a machine with a display, or `HEADLESS=1 DEMO_AUTO=1 python scripts/sow_demo_playwright.py` to self-run. |
| “Cannot reach the demo server” | The walkthrough starts its own server; this only appears if `DEMO_URL` is set to an unreachable server. Unset it, or point it at a running server. |
| Port 8099 / 8090 in use | `PYTHONPATH=src python scripts/sow_demo_server.py --port 9000` (then `DEMO_URL=http://127.0.0.1:9000`); API port via `make run-api PORT=...`. |
| `NotImplementedError` from a CLI command | You're on `CDD_PROFILE=onprem` (fail-fast by design; Demo C §4 shows this deliberately). Use `local` (Demos A/C) or `gcp` (Demo B). |
| GCP deploy/region/VPC-SC errors | See [`docs/runbook.md` §6 Common failures](docs/runbook.md#6-common-failures). |

**Stop / clean up:** Ctrl-C the demo server and `make run-api`. For GCP, scale the
deployment to zero or remove the app SA's `roles/aiplatform.user`; the audit trail and
case evidence remain intact ([runbook §5 Kill switch](docs/runbook.md#5-kill-switch)).
`make clean` removes local caches/artefacts.
