# Contributing to the Doc1 CDD + Source-of-Wealth Agent

Thanks for your interest. This is a public engineering-portfolio reference build, so the
bar is "production-grade in style, internally consistent, and green on the offline gate".

## Ground rules

- **Keep the domain pure.** Nothing under `src/cdd_sow_research/domain/` may import
  `google-cloud-*`, `google-adk`, `google-genai`, FastAPI, httpx, or pydantic. The domain
  is standard library only; everything external is a port.
- **GCP imports are lazy.** In `adapters/gcp/*`, every Google Cloud / GenAI / ADK import
  lives inside a method or `__init__` (or under `TYPE_CHECKING`), never at module top level.
  The on-prem/test profile must import every module with no Google Cloud SDK installed.
- **One adapter constructor.** Every adapter is `def __init__(self, settings: Settings)`.
- **Cite every claim.** Each generated statement in the dossier carries a `Citation`.
- **Redact before everything (R1).** Customer PII is removed at the boundary before any
  model, index, registry or audit call.
- **Maker-checker (P-06).** A CDD dossier always sets `requires_human_review=True`.

## Local setup

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # core + dev tooling, no Google Cloud SDK
export CDD_PROFILE=local          # the WORKING offline stack (what CI and `make test` use)
make lint test eval
```

(Set the profile deliberately. An unset `CDD_PROFILE` still binds the SDK-free `local`
adapters, so an offline process starts, but it is not read as choosing `local`: the seeded
no-auth dev personas are refused, the localhost CORS fallback does not apply and the
`X-Dev-Persona` header is not accepted. Use `CDD_PROFILE=onprem` only to exercise the
fail-fast sovereign-migration placeholders.)

## The gate (must be green before a PR)

```bash
ruff check src tests             # clean
ruff format --check src tests    # clean
pytest -m 'not integration' -q   # pass (unit + contract)
mypy src                         # clean (best-effort)
python eval/run_eval.py          # pass (exit 0)
```

CI runs the same on the `local` profile (the hosted Cloud Build check and `eval-gate.yaml`).

## Adding an adapter

1. Implement the port Protocol in the right `adapters/{gcp,live,platform,local,onprem}/`
   module, or explicitly reuse a reviewed binding for a hybrid profile.
2. Bind it in `config/settings.yaml` under `adapters:` (the dotted path is the contract).
3. Keep GCP imports lazy; the on-prem stub must construct with a single `Settings` arg and
   satisfy the Protocol (the contract test enforces this).

## Adding a new port or sub-service

A new *port* (a new outbound dependency) touches a fixed list of files; miss one and the
parity test fails loudly (`test_port_protocols_matches_settings_adapters`):

1. Define the `@runtime_checkable` `Protocol` under `src/cdd_sow_research/ports/<name>.py`
   and re-export it from `ports/__init__.py`.
2. Bind each profile under `adapters:`. A dedicated class per profile is not required when
   a hybrid such as `live` intentionally reuses a reviewed local or GCP adapter. At minimum,
   `local` and `onprem` bindings are required by the parity test.
3. Bind all of them in `config/settings.yaml` under `adapters:`.
4. Add the port to `PORT_PROTOCOLS` in `tests/contract/test_port_parity.py`.
5. Add a `cached_property` for it on the `Container` in `config.py`, and wire it into the
   service that needs it in `api/deps.py`.

A new *sub-service* is pure domain: add `domain/<name>_service.py` (stdlib only), re-export
it from `domain/services.py`, thread any bank-owned constants through `domain/policy.py`
(never hard-code them), construct it in `api/deps.py`, and add unit tests.

## Markdown

Minimise em-dashes in markdown; use colons, commas, or parentheses. Validate any mermaid
diagram before committing.

## Tests

Unit tests drive the domain services with the in-memory fakes in `tests/conftest.py`.
Integration tests that need live GCP are marked `@pytest.mark.integration` and deselected
by default. Use obviously-fictional names and ids in any fixture data.
