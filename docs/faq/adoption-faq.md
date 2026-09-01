# Adoption FAQ

For an engineering lead forking this repo as their institution's base. The step-by-step is
[`docs/ADOPTING.md`](../ADOPTING.md); this answers the "will it hurt later?" questions.

### How do I rebrand it for my institution?

`scripts/rename_fork.py` rewrites the package name, CLI entry point, `CDD_` env prefix, and
resource ids in one pass (preview with `--dry-run`, apply with `--yes`). Then recreate the
venv, `pip install -e ".[dev]"`, and run `make lint test eval`. The script does the
mechanical rename; the human decisions (region, IdP, PII pack, risk policy, fixtures, eval
golden set) are the checklist in `ADOPTING.md`.

### If five banks fork this, how does each take upstream security fixes?

Track upstream via **git tags** (semver). The repo declares a **core-vs-adopter-owned boundary** (ADOPTING §2): upstream owns
`domain/kernel.py`, `ports/`, `tests/contract/`, the eval harness mechanics and CI; you own
`config/settings.yaml` values, fixtures, the sanctions snapshot, `adapters/onprem/*`, UI
theming, and the eval golden set. Rebase your adopter-owned changes onto each release rather
than merging `main` continuously, and merge conflicts stay in files you were told to expect.

### How do I add a new outbound dependency (a new port)?

There is a fixed touch list, and the contract test fails loudly if you miss part of it
(`test_port_protocols_matches_settings_adapters`): define the `@runtime_checkable` Protocol
under `ports/`, re-export it, implement one adapter per profile (at least `local` and
`onprem`), bind all of them in `config/settings.yaml`, add the port to `PORT_PROTOCOLS` in
the parity test, add a `cached_property` on the `Container`, and wire it in `api/deps.py`.
Full instructions in [`CONTRIBUTING.md`](../../CONTRIBUTING.md) ("Adding a new port or
sub-service").

### How do I add a new sub-service or output panel?

A sub-service is pure domain: add `domain/<name>_service.py` (stdlib only), re-export it from
`domain/services.py`, thread any bank-owned constants through `domain/policy.py` (never
hard-code them), construct it in `api/deps.py`, and unit-test it. For an output panel, the
renderer (`scripts/render_sow_ui.py`) already renders attached artifacts when present; add a
`data-panel` hook so the demo walkthrough can target it.

### How do I change the taxonomy (wealth-source kinds, doc types)?

They are `StrEnum`s and the engines are typed on `str`, so you extend the vocabulary through
the policy tables (`policy.gap.mandatory_docs`, etc.) without editing engine code. Serialized
JSON values are the enum strings. To replace the taxonomy wholesale for a different vertical,
edit the enums in `domain/models.py` and the label maps in the UI.

### How do I retune the risk policy without touching code?

Every threshold a compliance function owns lives under `policy:` in
`config/settings.yaml` (scorecard weights, gap tolerances, review cadences, FATF country
lists, escalation bands), parsed into `domain/policy.py` dataclasses whose defaults equal the
reference constants. `tests/unit/test_risk_policy.py` shows overrides changing behavior.

### Will the demo rot after I diverge?

There is a CI self-test (`make demo-selftest`, wired into the hosted Cloud Build check) that
drives the whole walkthrough headless and asserts each step's live state, plus a browserless
`tests/unit/test_demo_server.py`. A refactor that breaks the demo fails the PR instead of
surfacing the morning of a stakeholder presentation.

### Does the CI run for my fork out of the box?

Yes. CI and the eval gate run on the `local` profile with **no cloud credentials and no org
secrets**, a fork's build is green immediately. You add secrets only when you wire the
`gcp`/`platform` profiles. Note the eval gate measures the *reference* vertical until you
rebuild the golden set; that is an explicit adoption step, not a silent pass.
