# Demo scripts: long-running, auditable Source-of-Wealth

All scripts are SDK-free and run against the in-process `local` stack (no Google Cloud,
no API key). Run them from the repo root with the package on the path:

```bash
export PYTHONPATH=src
```

| Script | What it does |
|--------|--------------|
| `portability_demo.py` | The bounded **portability-seam tour** (DEMO §4): profile swap (local works, onprem fails fast), complete 18-runtime/data-port interface parity with map-drift detection, tamper-evident hash-chained audit, open-format JSONL export/reload, and separately selected local identity resolution. It does not prove cross-host or cross-issuer portability. Exit 0 only if every named check passes. |
| `embed_portability_demo.py` | Presenter-paced, production-build browser evidence for the same immutable loader and fixed `/agent` UI in two registered host origins, strict MessagePort bootstrap rejection, installation-specific framing policy, and manifest-owned fallback. The evidence is bounded to channel portability. |
| `sow_demo.py` | Drives the synthetic Acme case through 3 rounds and writes the audit-view JSON (one entry per iteration + the sealed snapshot). |
| `render_sow_ui.py` | Renders that JSON into static audit-first HTML pages (one per round + a timeline) for screenshots. |
| `sow_demo_server.py` | A **live, click-through** server that advances the *real* `SowCaseService` one step per click and renders the audit-first UI. |
| `sow_demo_playwright.py` | A **presenter-controlled** Playwright walkthrough of the live server: it narrates each step and waits for you to press Enter before performing it. |
| `sync_sanctions.py` | Refresh the sanctions/PEP **watchlist snapshot** the screening providers read; fetches OFAC SLS (SDN + Consolidated) + UN/EU/UK, parses, versions, writes JSON (and optionally uploads to the GCS snapshot bucket). Network egress required for real data. |
| `related_party_demo.py` | Onboards a **company + its key individuals**: runs the company SoW case, then CDD + (for source-of-funds individuals) a full SoW sub-case per UBO/director, plus sanctions/PEP screening, a risk-based scorecard + CDD tier, Source-of-Funds reconciliation, and an ongoing-monitoring / periodic-review assessment, each rolled up into its own audit panel. Render with `render_sow_ui.py`. |

## Static screenshots

```bash
python scripts/sow_demo.py sow_demo.json
python scripts/render_sow_ui.py sow_demo.json ./out      # ./out/sow-iter-*.html, sow-timeline.html
```

## Live, presenter-controlled demo

**One command** (it starts its own server and stops it on exit):

```bash
pip install -e ".[demo]" && playwright install chromium      # one-time
make demo                                                    # or: PYTHONPATH=src python scripts/sow_demo_playwright.py
```

The walkthrough is **paced by you** and narrates entirely on the terminal (the Playwright
console) so the audience sees only the clean UI: it prints what each step does and waits.
At the prompt, **Enter** runs the step, **b** goes back, a **number** (`1`-`10`) jumps, **q**
quits. After each step it reads the server's `/state` and asserts the coverage % and gap
count, then scrolls the relevant `data-panel` into view and flashes a highlight. The ten
steps: case opened → Round 0 (gaps + RFIs) → Round 1 → Round 2 (clean) → key individuals
(CDD + SoW) → sanctions/PEP screening → risk scorecard + CDD tier → Source of Funds →
ongoing monitoring → MLRO approval (four-eyes) + sealed snapshot, finishing on the timeline
recap.

You can also run only the server (`PYTHONPATH=src python scripts/sow_demo_server.py`) and
drive it by hand in any browser at `http://localhost:8099` (**Next ▶** / **Restart**), or
set `DEMO_URL` so the walkthrough attaches to it instead of spawning its own.

Useful environment overrides for `sow_demo_playwright.py`:

| Var | Default | Purpose |
|-----|---------|---------|
| `DEMO_URL` | (spawns its own) | attach to an already-running server instead of spawning one |
| `DEMO_PORT` | `8099` | port for the auto-spawned server |
| `HEADLESS=1` | off | run without a window (self-test / recording) |
| `DEMO_AUTO=1` | off | don't wait for input, advance automatically |
| `SLOWMO_MS` | `250` headed | per-action slow motion |
| `CHROME_PATH` | (none) | explicit Chromium/Chrome binary |
| `lock.py` | Compiles **every** lockfile the repo ships (`requirements-dev.lock`, `requirements-gcp.lock`, `requirements-dev-oidc.lock`) and puts the header back, because `uv pip compile` REPLACES the output file: it writes its own two-line provenance comment and destroys the `tag = commit` map the pin tests check against. `make lock` runs this rather than uv directly. Adding a lockfile means adding it to `_TARGETS`; `tests/unit/test_lockfile_pins.py` fails the gate if one is shipped without being covered. |

`make demo-selftest` runs it headless + unattended (the CI smoke test), asserting every step.

## UI portability evidence

Install the pinned demo dependency and all three browser engines once:

```bash
pip install -r requirements-dev-oidc.lock
pip install playwright==1.61.0
python -m playwright install chromium firefox webkit
cd ui && npm ci && cd ..
```

The interactive command opens a visible Chromium browser by default. Narration stays in the
terminal, and each action reaches stable visible proof before the Enter pause.

```bash
python scripts/embed_portability_demo.py
python scripts/embed_portability_demo.py --list
python scripts/embed_portability_demo.py --from same-artifact
```

Unattended evidence for every supported engine:

```bash
HEADLESS=1 python scripts/embed_portability_demo.py \
  --browser all --no-pause --scope full \
  --screenshots artifacts/portability/screenshots \
  --evidence artifacts/portability/evidence.json
```

The runner builds and starts the production Next server when needed, starts only its synthetic
host services, and stops every service it owns in `finally`. The two host pages use fictional
installation data and loopback origins. Full scope adds production-module synthetic Mode 4 and Mode 5
identity chains: two pinned issuer policies through the OAuth verifier, plus an institution BFF
session/CSRF/user-intent exchange through the embed broker and dedicated Doc1 token verifier.
The evidence proves channel and identity portability; it does not prove infrastructure, model,
data, audit-record, or whole-system portability.

Use channel scope when rehearsing only the five host-channel steps:

```bash
HEADLESS=1 python scripts/embed_portability_demo.py \
  --browser all --no-pause --scope channel
```

Channel scope records both identity dimensions as `NOT_RUN`; full scope fails with a
dimension-specific nonzero exit if either production-module synthetic browser chain is incomplete.
The UI pins patched Sharp 0.35.3, and CI rejects every high or critical npm finding without an
advisory exception.
