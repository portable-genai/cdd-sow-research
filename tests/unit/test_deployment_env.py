from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path("scripts/deployment_env.py")
SPEC = importlib.util.spec_from_file_location("deployment_env", SCRIPT)
assert SPEC and SPEC.loader
deployment_env = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deployment_env)


@pytest.fixture(autouse=True)
def _bind_reviewed_terraform_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deployment_env, "REVIEWED_TERRAFORM_DIR", tmp_path.resolve())


def _write(path: Path, values: dict[str, str]) -> None:
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")


def _ready_values(
    *,
    subject_token_type: str = deployment_env.ACCESS_TOKEN_SUBJECT_TYPE,
) -> dict[str, str]:
    id_token_profile = subject_token_type == deployment_env.ID_TOKEN_SUBJECT_TYPE
    values = {
        key: f"approved-{key.lower().replace('_', '-')}"
        for key in (
            *deployment_env.BASE_REQUIRED,
            *deployment_env.MODE4_REQUIRED,
            *deployment_env.MODE5_REQUIRED,
            *deployment_env.MODE6_REQUIRED,
            *deployment_env.PRODUCTION_SECRET_KEYS,
        )
    }
    values.update(
        {
            "DOC1_DEPLOYMENT_ENABLED": "true",
            "DOC1_NAME_PREFIX": "cdd-sow",
            "DOC1_STACK_LIFECYCLE": "new",
            "DOC1_DEPLOYMENT_STAGE": "production-edge",
            "DOC1_DEPLOYMENT_POSTURE": "production",
            "GOOGLE_CLOUD_PROJECT": "approved-doc1-prod",
            "GCP_REGION": "asia-southeast1",
            "DOC1_ALLOWED_REGIONS": "asia-southeast1",
            "DOC1_AUDIT_RETENTION_DAYS": "180",
            "DOC1_EXISTING_LOCKED_RETENTION_DAYS": "0",
            "DOC1_WORM_LOCK_APPROVED": "true",
            "DOC1_EDGE_MIN_INSTANCES": "2",
            "DOC1_ALERT_NOTIFICATION_CHANNELS": (
                "projects/approved-doc1-prod/notificationChannels/123"
            ),
            "DOC1_DEPLOYMENT_PHASE": "dry-run",
            "DOC1_VPC_SC_ENFORCE": "false",
            "DOC1_PRODUCTION_IDENTITY_MODE": "embedded-grant",
            "DOC1_EMBED_SIGNING_PROTECTION_LEVEL": "HSM",
            "DOC1_EMBED_SIGNING_KEY_VERSION": (
                "projects/approved-doc1-prod/locations/asia-southeast1/"
                "keyRings/cdd-sow-agent-ring/"
                "cryptoKeys/cdd-sow-agent-cmek-embed-signing/"
                "cryptoKeyVersions/1"
            ),
            "DOC1_AGENT_DOMAIN": "doc1.fictionalbank.sg",
            "DOC1_STANDALONE_DOMAIN": "doc1-login.fictionalbank.sg",
            "DOC1_TERRAFORM_STATE_BUCKET": "approved-doc1-tfstate",
            "DOC1_TERRAFORM_STATE_PREFIX": "doc1/production",
            "DOC1_APPROVED_PARENT_ORIGINS": "https://portal.fictionalbank.sg",
            "DOC1_INSTALLATION_IDS": "bank_sg_1",
            "DOC1_API_IMAGE": f"repo/doc1-api@sha256:{'a' * 64}",
            "DOC1_UI_IMAGE": f"repo/doc1-ui@sha256:{'b' * 64}",
            "DOC1_INSTALLATION_MANIFEST_SECRET_VERSION": "7",
            "DOC1_RUNTIME_SETTINGS_SECRET_VERSION": "4",
            "DOC1_MODE4_ISSUER": "https://id.fictionalbank.sg",
            "DOC1_MODE4_JWKS_URI": "https://id.fictionalbank.sg/jwks",
            "DOC1_MODE4_RESOURCE_AUDIENCE": "https://doc1.fictionalbank.sg/agent/api",
            "DOC1_MODE5_SUBJECT_TOKEN_TYPE": subject_token_type,
            "DOC1_MODE5_SUBJECT_ISSUER": (
                "https://accounts.google.example"
                if id_token_profile
                else "https://id.fictionalbank.sg"
            ),
            "DOC1_MODE5_SUBJECT_JWKS_URI": (
                "https://www.googleapis.example/oauth2/v3/certs"
                if id_token_profile
                else "https://id.fictionalbank.sg/jwks"
            ),
            "DOC1_MODE5_SUBJECT_AUDIENCE": (
                "555000111-fictional.apps.googleusercontent.example"
                if id_token_profile
                else "https://broker.fictionalbank.sg"
            ),
            "DOC1_MODE5_SUBJECT_CLIENT": (
                "555000111-fictional.apps.googleusercontent.example"
                if id_token_profile
                else "approved-doc1-mode5-subject-client"
            ),
            "DOC1_MODE5_RESOURCE_AUDIENCE": "https://doc1.fictionalbank.sg/agent/api",
            "DOC1_MODE5_RESOURCE_SCOPES": "cdd.read",
            "DOC1_MODE5_BFF_CLIENT_ID": "approved-bff",
            "DOC1_MODE5_BFF_JWKS_URI": "https://portal.fictionalbank.sg/jwks",
            "DOC1_MODE5_BFF_AUTH_METHOD": "private_key_jwt",
            "DOC1_MODE6_ISSUER": "https://id.fictionalbank.sg",
            "DOC1_MODE6_CALLBACK_URL": ("https://doc1-login.fictionalbank.sg/auth/callback"),
            "CDD_OIDC_CLIENT_SECRET": "provider-issued:opaque/client+secret",
            "CDD_SESSION_SIGNING_KEY": base64.b64encode(bytes(range(32, 64))).decode(),
        }
    )
    manifest_bytes = json.dumps(
        {
            "schema_version": 1,
            "deployment_manifest_id": "doc1-production",
            "build_id": "build-v1",
            "installations": {
                "bank_sg_1": {
                    "tenant": values["DOC1_DEPLOYMENT_TENANT"],
                    "parent_origins": ["https://portal.fictionalbank.sg"],
                    "resource_audience": "https://doc1.fictionalbank.sg/agent/api",
                    "scopes": ["cdd.read"],
                    "identity_mode": "embedded-grant",
                    "issuer_policy_id": "institution-subject",
                    "allowed_clients": ["approved-bff"],
                    "protocol_versions": ["1"],
                    "public_origin": "https://doc1.fictionalbank.sg",
                    "public_mount_path": "/agent",
                    "loader_version": "v1",
                    "fallback_url": "https://doc1-login.fictionalbank.sg/agent/cdd",
                }
            },
        },
        separators=(",", ":"),
    ).encode()
    subject_family = (
        {
            "id_token_subject_issuers": [
                {
                    "policy_id": "institution-subject",
                    "issuer": values["DOC1_MODE5_SUBJECT_ISSUER"],
                    "jwks_uri": values["DOC1_MODE5_SUBJECT_JWKS_URI"],
                    "audience": values["DOC1_MODE5_SUBJECT_AUDIENCE"],
                    "authorized_party": values["DOC1_MODE5_SUBJECT_CLIENT"],
                    "hosted_domain": "fictionalbank.example",
                    "tenant": values["DOC1_DEPLOYMENT_TENANT"],
                }
            ]
        }
        if id_token_profile
        else {
            "subject_token_issuers": [
                {
                    "policy_id": "institution-subject",
                    "issuer": values["DOC1_MODE5_SUBJECT_ISSUER"],
                    "jwks_uri": values["DOC1_MODE5_SUBJECT_JWKS_URI"],
                    "resource_audience": values["DOC1_MODE5_SUBJECT_AUDIENCE"],
                    "tenant": values["DOC1_DEPLOYMENT_TENANT"],
                    "allowed_clients": [values["DOC1_MODE5_SUBJECT_CLIENT"]],
                    "required_scopes": [values["DOC1_MODE5_GRANT_SCOPE"]],
                }
            ]
        }
    )
    runtime_bytes = json.dumps(
        {
            "project_id": "approved-doc1-prod",
            "region": "asia-southeast1",
            "profile": "gcp",
            "deployment": {"production": True, "replica_count": 2},
            "channel": {
                "mode": "sandboxed",
                "public_origin": "https://doc1.fictionalbank.sg",
                "manifest_version": values["DOC1_PRODUCTION_MANIFEST_VERSION"],
            },
            "identity": {
                "mode": "embedded-grant",
                "embedded_grant": {
                    **subject_family,
                    "installations": [
                        {
                            "installation_id": "bank_sg_1",
                            "subject_policy_id": "institution-subject",
                            "subject_token_audience": values["DOC1_MODE5_SUBJECT_AUDIENCE"],
                            "subject_grant_scope": values["DOC1_MODE5_GRANT_SCOPE"],
                            "subject_token_type": subject_token_type,
                            "bff_clients": [
                                {
                                    "client_id": values["DOC1_MODE5_BFF_CLIENT_ID"],
                                    "grant_endpoint_audience": (
                                        "https://doc1.fictionalbank.sg/agent/api/v1/embed/grants"
                                    ),
                                    "permitted_scopes": ["cdd.read"],
                                    "allowed_subject_clients": [
                                        values["DOC1_MODE5_SUBJECT_CLIENT"]
                                    ],
                                    "keys": [
                                        {
                                            "kid": "bff-key-1",
                                            "algorithm": "ES256",
                                            "public_jwk": {
                                                "kty": "EC",
                                                "crv": "P-256",
                                                "x": "synthetic-x",
                                                "y": "synthetic-y",
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                    "token": {
                        "issuer": "https://doc1.fictionalbank.sg",
                        "audience": values["DOC1_MODE5_RESOURCE_AUDIENCE"],
                        "active_kid": "embed-key-1",
                        "keys": [
                            {
                                "kid": "embed-key-1",
                                "algorithm": "ES256",
                                "public_key_env": "CDD_EMBED_TOKEN_PUBLIC_KEY",
                                "kms_key_version": values["DOC1_EMBED_SIGNING_KEY_VERSION"],
                            }
                        ],
                    },
                },
            },
        },
        separators=(",", ":"),
    ).encode()
    values.update(
        {
            "DOC1_INSTALLATION_MANIFEST_B64": base64.b64encode(manifest_bytes).decode(),
            "DOC1_RUNTIME_SETTINGS_B64": base64.b64encode(runtime_bytes).decode(),
            "DOC1_INSTALLATION_MANIFEST_SHA256": hashlib.sha256(manifest_bytes).hexdigest(),
            "DOC1_RUNTIME_SETTINGS_SHA256": hashlib.sha256(runtime_bytes).hexdigest(),
        }
    )
    return values


def test_loader_enforces_secret_file_boundary(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    secrets = tmp_path / ".env.secrets"
    _write(env, {"CDD_SESSION_SIGNING_KEY": "not-allowed-here"})
    _write(secrets, {"GOOGLE_CLOUD_PROJECT": "not-allowed-here"})

    with pytest.raises(deployment_env.DeploymentEnvError) as exc:
        deployment_env.load_environment(env, secrets)

    assert "secret keys must move to .env.secrets" in str(exc.value)
    assert "non-secret keys must move to .env" in str(exc.value)


def test_tracked_examples_form_a_valid_draft_contract() -> None:
    values = deployment_env.load_environment(Path(".env.example"), Path(".env.secrets.example"))

    assert deployment_env.validate_environment(values) == []


def test_unquoted_hash_is_rejected_instead_of_truncating(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("VALUE=kept # silently-truncated\n")

    with pytest.raises(deployment_env.DeploymentEnvError, match="unquoted # is forbidden"):
        deployment_env.parse_env_file(env)


def test_dotenv_preserves_opaque_backslashes_and_quoted_bytes(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    secrets = tmp_path / ".env.secrets"
    env.write_text(
        "UNQUOTED=provider\\issued\\opaque\n"
        'DOUBLE=" leading # provider\\issued "\n'
        "SINGLE='literal \\\\ material # kept'\n"
    )
    secrets.write_text("CDD_OIDC_CLIENT_SECRET=provider\\issued$(literal)`tick`\\opaque\n")

    parsed = deployment_env.parse_env_file(env)
    loaded = deployment_env.load_environment(env, secrets)

    assert parsed["UNQUOTED"] == "provider\\issued\\opaque"
    assert parsed["DOUBLE"] == " leading # provider\\issued "
    assert parsed["SINGLE"] == "literal \\\\ material # kept"
    assert loaded["CDD_OIDC_CLIENT_SECRET"] == "provider\\issued$(literal)`tick`\\opaque"


@pytest.mark.parametrize(
    "line, expected",
    [
        ("VALUE= trailing\n", "leading or trailing value whitespace"),
        ("VALUE=two words\n", "values containing whitespace must be quoted"),
        ('VALUE="unterminated\n', "quoted value must end"),
        ('VALUE="ambiguous\\"quote"\n', "is ambiguous"),
    ],
)
def test_dotenv_rejects_ambiguous_value_syntax(
    tmp_path: Path,
    line: str,
    expected: str,
) -> None:
    env = tmp_path / ".env"
    env.write_text(line)

    with pytest.raises(deployment_env.DeploymentEnvError, match=expected):
        deployment_env.parse_env_file(env)


def test_secret_file_permissions_fail_closed(tmp_path: Path) -> None:
    secrets = tmp_path / ".env.secrets"
    secrets.write_text("CDD_SESSION_SIGNING_KEY=secret\n")
    secrets.chmod(0o644)

    errors = deployment_env.validate_secret_file_permissions(secrets)

    assert errors == [f"{secrets} must not be accessible by group or other users (use chmod 600)"]


def test_draft_placeholders_are_allowed_but_retention_is_validated() -> None:
    values = {
        "DOC1_DEPLOYMENT_ENABLED": "false",
        "GOOGLE_CLOUD_PROJECT": "placeholder-project",
        "GCP_REGION": "asia-southeast1",
        "DOC1_ALLOWED_REGIONS": "asia-southeast1",
        "DOC1_AUDIT_RETENTION_DAYS": "180",
    }
    assert deployment_env.validate_environment(values) == []

    values["DOC1_AUDIT_RETENTION_DAYS"] = "179"
    assert deployment_env.validate_environment(values) == [
        "DOC1_AUDIT_RETENTION_DAYS must be at least 180"
    ]


def test_production_gate_rejects_placeholders_and_unsafe_first_apply() -> None:
    values = _ready_values()
    values["GOOGLE_CLOUD_PROJECT"] = "your-gcp-project"
    values["DOC1_WORM_LOCK_APPROVED"] = "false"
    values["DOC1_DEPLOYMENT_PHASE"] = "dry-run"
    values["DOC1_VPC_SC_ENFORCE"] = "true"

    errors = deployment_env.validate_environment(values, require_ready=True)

    assert "GOOGLE_CLOUD_PROJECT is missing or still contains a placeholder" in errors
    assert "DOC1_WORM_LOCK_APPROVED must be true before production execution" in errors
    assert "DOC1_VPC_SC_ENFORCE must be false during the dry-run phase" in errors


def test_an_embedded_grant_edge_does_not_demand_the_mode_4_registration() -> None:
    values = _ready_values()
    assert values["DOC1_PRODUCTION_IDENTITY_MODE"] == "embedded-grant"
    for key in deployment_env.MODE4_REQUIRED:
        values[key] = ""

    assert deployment_env.validate_environment(values, require_ready=True) == []


def test_an_access_token_edge_with_blank_mode_4_variables_still_fails() -> None:
    values = _ready_values()
    values["DOC1_PRODUCTION_IDENTITY_MODE"] = "oauth-access-token"
    for key in deployment_env.MODE4_REQUIRED:
        values[key] = ""

    errors = deployment_env.validate_environment(values, require_ready=True)

    for key in deployment_env.MODE4_REQUIRED:
        assert f"{key} is missing or still contains a placeholder" in errors


def test_the_deployment_tenant_is_the_one_home_for_the_tenant_fact() -> None:
    values = _ready_values()
    values["DOC1_DEPLOYMENT_TENANT"] = "a-different-tenant"

    errors = deployment_env.validate_environment(values, require_ready=True)

    # Both couplings now read the mode-neutral variable, so a drifted value is caught.
    assert "installation manifest tenant does not match deployment" in errors
    assert "Mode 5 subject policy does not match reviewed environment" in errors


def test_an_access_token_edge_requires_its_mode_4_tenant_to_equal_the_deployment_tenant() -> None:
    values = _ready_values()
    values["DOC1_PRODUCTION_IDENTITY_MODE"] = "oauth-access-token"
    values["DOC1_MODE4_TENANT"] = "some-other-tenant"

    errors = deployment_env.validate_environment(values, require_ready=True)

    assert "DOC1_MODE4_TENANT must equal the one DOC1_DEPLOYMENT_TENANT" in errors


def test_a_google_id_token_subject_profile_passes_the_production_edge_gate() -> None:
    values = _ready_values(subject_token_type=deployment_env.ID_TOKEN_SUBJECT_TYPE)
    for key in deployment_env.MODE4_REQUIRED:
        values[key] = ""

    assert deployment_env.validate_environment(values, require_ready=True) == []


def test_an_id_token_runtime_profile_must_match_the_reviewed_subject_token_type() -> None:
    values = _ready_values(subject_token_type=deployment_env.ID_TOKEN_SUBJECT_TYPE)
    values["DOC1_MODE5_SUBJECT_TOKEN_TYPE"] = deployment_env.ACCESS_TOKEN_SUBJECT_TYPE

    errors = deployment_env.validate_environment(values, require_ready=True)

    assert "Mode 5 runtime settings require subject, BFF, and token policy" in errors


def test_vpc_sc_enforcement_requires_prior_dry_run_evidence() -> None:
    values = _ready_values()
    values["DOC1_DEPLOYMENT_PHASE"] = "enforce"
    values["DOC1_VPC_SC_ENFORCE"] = "true"
    values["DOC1_VPC_SC_DRY_RUN_EVIDENCE"] = "PLACEHOLDER_EVIDENCE"

    errors = deployment_env.validate_environment(values, require_ready=True)

    assert errors == ["DOC1_VPC_SC_DRY_RUN_EVIDENCE is required before enforcement"]


def test_production_capacity_alerts_and_locked_retention_fail_closed() -> None:
    values = _ready_values()
    values["DOC1_EDGE_MIN_INSTANCES"] = "1"
    values["DOC1_ALERT_NOTIFICATION_CHANNELS"] = ""
    values["DOC1_EMBED_SIGNING_PROTECTION_LEVEL"] = "SOFTWARE"
    values["DOC1_EXISTING_LOCKED_RETENTION_DAYS"] = "2557"

    errors = deployment_env.validate_environment(values, require_ready=True)

    assert "DOC1_EDGE_MIN_INSTANCES must be at least 2" in errors
    assert any("DOC1_ALERT_NOTIFICATION_CHANNELS" in error for error in errors)
    assert "named production requires HSM embed signing-key protection" in errors
    assert "audit retention cannot be lower than the existing locked value" in errors


def test_secret_payloads_require_canonical_base64_and_reviewed_digest() -> None:
    values = _ready_values()
    values["DOC1_INSTALLATION_MANIFEST_B64"] = "not-base64"
    values["DOC1_RUNTIME_SETTINGS_SHA256"] = "0" * 64

    errors = deployment_env.validate_environment(values, require_ready=True)

    assert "DOC1_INSTALLATION_MANIFEST_B64 must be valid canonical base64" in errors
    assert "runtime settings bytes do not match the reviewed SHA-256" in errors


def test_reserved_placeholders_and_low_diversity_secrets_are_rejected() -> None:
    values = _ready_values()
    values["DOC1_DEPLOYMENT_OWNER"] = "TODO"
    values["DOC1_AGENT_DOMAIN"] = "doc1.bank.test"
    values["CDD_SESSION_SIGNING_KEY"] = base64.b64encode(b"x" * 32).decode()

    errors = deployment_env.validate_environment(values, require_ready=True)

    assert "DOC1_DEPLOYMENT_OWNER is missing or still contains a placeholder" in errors
    assert "DOC1_AGENT_DOMAIN is missing or still contains a placeholder" in errors
    assert "CDD_SESSION_SIGNING_KEY must be generated high-diversity material" in errors


def test_oidc_client_secret_is_validated_as_opaque_provider_material() -> None:
    values = _ready_values()
    values["CDD_OIDC_CLIENT_SECRET"] = "provider-TODO-$(literal)-`tick`:/+opaque"

    assert deployment_env.validate_environment(values, require_ready=True) == []

    values["CDD_OIDC_CLIENT_SECRET"] = "opaque\x7fsecret"
    errors = deployment_env.validate_environment(values, require_ready=True)

    assert "CDD_OIDC_CLIENT_SECRET must not contain control characters" in errors


def test_secret_placeholder_detection_uses_exact_sentinels() -> None:
    values = _ready_values()
    values["CDD_OIDC_CLIENT_SECRET"] = "provider-placeholder-suffix"

    assert deployment_env.validate_environment(values, require_ready=True) == []

    values["CDD_OIDC_CLIENT_SECRET"] = "PLACEHOLDER"
    errors = deployment_env.validate_environment(values, require_ready=True)

    assert "CDD_OIDC_CLIENT_SECRET is missing or still contains a placeholder" in errors

    values["CDD_OIDC_CLIENT_SECRET"] = " PLACEHOLDER "

    assert deployment_env.validate_environment(values, require_ready=True) == []


def test_mode5_runtime_key_version_is_bound_to_reviewed_terraform_identity() -> None:
    values = _ready_values()
    runtime = json.loads(base64.b64decode(values["DOC1_RUNTIME_SETTINGS_B64"]))
    runtime["identity"]["embedded_grant"]["token"]["keys"][0]["kms_key_version"] = (
        "projects/approved-doc1-prod/locations/asia-southeast1/"
        "keyRings/attacker-ring/cryptoKeys/attacker-key/cryptoKeyVersions/1"
    )
    runtime_bytes = json.dumps(runtime, separators=(",", ":")).encode()
    values["DOC1_RUNTIME_SETTINGS_B64"] = base64.b64encode(runtime_bytes).decode()
    values["DOC1_RUNTIME_SETTINGS_SHA256"] = hashlib.sha256(runtime_bytes).hexdigest()

    errors = deployment_env.validate_environment(values, require_ready=True)

    assert (
        "Mode 5 runtime KMS versions must use the reviewed Terraform signing-key resource" in errors
    )
    assert "Mode 5 active runtime KMS version must equal DOC1_EMBED_SIGNING_KEY_VERSION" in errors


def test_mode5_reviewed_key_version_must_match_terraform_resource_contract() -> None:
    values = _ready_values()
    values["DOC1_EMBED_SIGNING_KEY_VERSION"] = (
        "projects/approved-doc1-prod/locations/asia-southeast1/"
        "keyRings/attacker-ring/cryptoKeys/attacker-key/cryptoKeyVersions/1"
    )

    errors = deployment_env.validate_environment(values, require_ready=True)

    assert any(error.startswith("DOC1_EMBED_SIGNING_KEY_VERSION must equal") for error in errors)


def test_runtime_yaml_rejects_duplicate_and_unknown_keys() -> None:
    values = _ready_values()
    errors: list[str] = []

    deployment_env._validate_runtime_settings(
        b"project_id: approved-doc1-prod\nproject_id: shadow\n", values, errors
    )
    assert any("duplicate key" in error for error in errors)

    runtime = base64.b64decode(values["DOC1_RUNTIME_SETTINGS_B64"])
    document = json.loads(runtime)
    document["unknown_root"] = True
    errors = []
    deployment_env._validate_runtime_settings(json.dumps(document).encode(), values, errors)
    assert "runtime settings contains unknown root keys: unknown_root" in errors


def test_runtime_settings_must_pass_application_mode5_schema() -> None:
    values = _ready_values()
    runtime = json.loads(base64.b64decode(values["DOC1_RUNTIME_SETTINGS_B64"]))
    del runtime["identity"]["embedded_grant"]["installations"][0]["bff_clients"][0]["keys"]
    runtime_bytes = json.dumps(runtime, separators=(",", ":")).encode()
    values["DOC1_RUNTIME_SETTINGS_B64"] = base64.b64encode(runtime_bytes).decode()
    values["DOC1_RUNTIME_SETTINGS_SHA256"] = hashlib.sha256(runtime_bytes).hexdigest()

    errors = deployment_env.validate_environment(values, require_ready=True)

    assert any(
        "application Settings.load rejected runtime settings" in error
        and "BFF client policy is incomplete" in error
        for error in errors
    )


def test_ready_environment_maps_only_reviewed_values_to_terraform() -> None:
    values = _ready_values()

    assert deployment_env.validate_environment(values, require_ready=True) == []
    mapped = deployment_env.terraform_environment(values)

    assert mapped["TF_VAR_project_id"] == "approved-doc1-prod"
    assert mapped["TF_VAR_retention_days"] == "180"
    assert mapped["TF_VAR_worm_locked"] == "true"
    assert mapped["TF_VAR_edge_min_instances"] == "2"
    assert json.loads(mapped["TF_VAR_alert_notification_channels"]) == [
        "projects/approved-doc1-prod/notificationChannels/123"
    ]
    assert mapped["TF_VAR_enable_embed_signing_key"] == "true"
    assert mapped["TF_VAR_deployment_stage"] == "production-edge"
    assert mapped["TF_VAR_production_edge_enabled"] == "true"
    assert json.loads(mapped["TF_VAR_allowed_regions"]) == ["asia-southeast1"]


def test_mode5_key_bootstrap_defers_edge_inputs_and_maps_exact_stage() -> None:
    values = _ready_values()
    values["DOC1_DEPLOYMENT_STAGE"] = "mode5-key-bootstrap"
    for key in deployment_env.EDGE_ONLY_REQUIRED:
        values.pop(key, None)
    for key in deployment_env.PRODUCTION_SECRET_KEYS:
        values.pop(key, None)

    assert deployment_env.validate_environment(values, require_ready=True) == []
    mapped = deployment_env.terraform_environment(values)

    assert mapped["TF_VAR_deployment_stage"] == "mode5-key-bootstrap"
    assert mapped["TF_VAR_production_edge_enabled"] == "false"
    assert mapped["TF_VAR_enable_embed_signing_key"] == "true"
    assert "TF_VAR_api_image" not in mapped
    assert "TF_VAR_runtime_settings_secret_id" not in mapped
    assert "TF_VAR_embed_signing_key_version" not in mapped


def test_mode5_key_bootstrap_rejects_non_mode5_identity() -> None:
    values = _ready_values()
    values["DOC1_DEPLOYMENT_STAGE"] = "mode5-key-bootstrap"
    values["DOC1_PRODUCTION_IDENTITY_MODE"] = "oauth-access-token"

    errors = deployment_env.validate_environment(values, require_ready=True)

    assert "mode5-key-bootstrap requires embedded-grant identity" in errors


def test_child_environment_excludes_secrets_and_inherited_terraform_values(monkeypatch) -> None:
    values = _ready_values()
    monkeypatch.setenv("TF_VAR_project_id", "attacker-project")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-cross")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/approved/adc.json")

    child = deployment_env.sanitized_child_environment(values)

    assert child["TF_VAR_project_id"] == "approved-doc1-prod"
    assert child["GOOGLE_APPLICATION_CREDENTIALS"] == "/approved/adc.json"
    assert "UNRELATED_SECRET" not in child
    assert "CDD_OIDC_CLIENT_SECRET" not in child
    assert "DOC1_INSTALLATION_MANIFEST_B64" not in child


def test_terraform_runner_injects_backend_and_rejects_competing_inputs(
    tmp_path: Path,
) -> None:
    values = _ready_values()
    command = ["terraform", f"-chdir={tmp_path}", "init", "-input=false"]

    prepared = deployment_env.prepare_terraform_command(command, values)

    assert "-backend-config=bucket=approved-doc1-tfstate" in prepared
    assert "-backend-config=prefix=doc1/production" in prepared
    assert "-reconfigure" in prepared
    assert deployment_env.requires_live_verification(prepared) is False
    assert (
        deployment_env.requires_live_verification(["terraform", f"-chdir={tmp_path}", "plan"])
        is True
    )
    assert (
        deployment_env.requires_live_verification(["terraform", f"-chdir={tmp_path}", "apply"])
        is True
    )

    (tmp_path / "terraform.tfvars").write_text('project_id = "bypass"\n')
    with pytest.raises(deployment_env.DeploymentEnvError, match="competing Terraform"):
        deployment_env.prepare_terraform_command(command, values)


def test_terraform_runner_requires_gcs_metadata_and_rejects_var_args(tmp_path: Path) -> None:
    values = _ready_values()
    metadata_dir = tmp_path / ".terraform"
    metadata_dir.mkdir()
    metadata = metadata_dir / "terraform.tfstate"
    metadata.write_text(json.dumps({"backend": {"type": "local", "config": {}}}))

    with pytest.raises(deployment_env.DeploymentEnvError, match="backend must be GCS"):
        deployment_env.prepare_terraform_command(
            ["terraform", f"-chdir={tmp_path}", "plan"], values
        )

    metadata.write_text(
        json.dumps(
            {
                "backend": {
                    "type": "gcs",
                    "config": {
                        "bucket": "approved-doc1-tfstate",
                        "prefix": "doc1/production",
                    },
                }
            }
        )
    )
    with pytest.raises(deployment_env.DeploymentEnvError, match="is forbidden"):
        deployment_env.prepare_terraform_command(
            ["terraform", f"-chdir={tmp_path}", "plan", "-var=project_id=bypass"],
            values,
        )


def test_terraform_runner_rejects_backend_drift_credentials_and_workspace(
    tmp_path: Path,
) -> None:
    values = _ready_values()
    metadata_dir = tmp_path / ".terraform"
    metadata_dir.mkdir()
    metadata = metadata_dir / "terraform.tfstate"
    base_backend = {
        "type": "gcs",
        "config": {
            "bucket": "wrong-bucket",
            "prefix": "doc1/production",
        },
    }
    metadata.write_text(json.dumps({"backend": base_backend}))
    command = ["terraform", f"-chdir={tmp_path}", "plan"]
    with pytest.raises(deployment_env.DeploymentEnvError, match="bucket does not match"):
        deployment_env.prepare_terraform_command(command, values)

    base_backend["config"]["bucket"] = "approved-doc1-tfstate"
    base_backend["config"]["credentials"] = "persisted-secret"
    metadata.write_text(json.dumps({"backend": base_backend}))
    with pytest.raises(deployment_env.DeploymentEnvError, match="credential"):
        deployment_env.prepare_terraform_command(command, values)

    base_backend["config"].pop("credentials")
    metadata.write_text(json.dumps({"backend": base_backend}))
    (metadata_dir / "environment").write_text("shadow")
    with pytest.raises(deployment_env.DeploymentEnvError, match="default Terraform workspace"):
        deployment_env.prepare_terraform_command(command, values)


def test_terraform_runner_forbids_unbound_saved_plan_apply(tmp_path: Path) -> None:
    values = _ready_values()
    metadata_dir = tmp_path / ".terraform"
    metadata_dir.mkdir()
    (metadata_dir / "terraform.tfstate").write_text(
        json.dumps(
            {
                "backend": {
                    "type": "gcs",
                    "config": {
                        "bucket": "approved-doc1-tfstate",
                        "prefix": "doc1/production",
                    },
                }
            }
        )
    )

    with pytest.raises(deployment_env.DeploymentEnvError, match="saved-plan apply is forbidden"):
        deployment_env.prepare_terraform_command(
            ["terraform", f"-chdir={tmp_path}", "apply", "unreviewed.tfplan"],
            values,
        )

    assert (
        deployment_env.prepare_terraform_command(
            ["terraform", f"-chdir={tmp_path}", "apply", "-input=true"],
            values,
        )[-1]
        == "-input=true"
    )


@pytest.mark.parametrize(
    "subcommand",
    ["destroy", "import", "state", "taint", "workspace", "output", "show"],
)
def test_terraform_runner_rejects_every_unreviewed_subcommand(
    tmp_path: Path,
    subcommand: str,
) -> None:
    values = _ready_values()

    with pytest.raises(deployment_env.DeploymentEnvError, match="subcommand .* is forbidden"):
        deployment_env.prepare_terraform_command(
            ["terraform", f"-chdir={tmp_path}", subcommand],
            values,
        )


def test_terraform_runner_rejects_alternate_executable_and_global_options(
    tmp_path: Path,
) -> None:
    values = _ready_values()

    with pytest.raises(deployment_env.DeploymentEnvError, match="exact name"):
        deployment_env.prepare_terraform_command(
            ["/tmp/terraform", f"-chdir={tmp_path}", "plan"],
            values,
        )
    with pytest.raises(deployment_env.DeploymentEnvError, match="global option"):
        deployment_env.prepare_terraform_command(
            ["terraform", "-help", f"-chdir={tmp_path}", "plan"],
            values,
        )


def test_terraform_runner_rejects_chdir_and_symlink_escape(
    tmp_path: Path,
) -> None:
    values = _ready_values()
    reviewed = tmp_path
    escaped = tmp_path / "outside"
    escaped.mkdir()
    symlink = tmp_path / "escaped-terraform"
    symlink.symlink_to(escaped, target_is_directory=True)

    with pytest.raises(deployment_env.DeploymentEnvError, match="resolve exactly"):
        deployment_env.prepare_terraform_command(
            ["terraform", f"-chdir={escaped}", "init"],
            values,
        )
    with pytest.raises(deployment_env.DeploymentEnvError, match="resolve exactly"):
        deployment_env.prepare_terraform_command(
            ["terraform", f"-chdir={symlink}", "init"],
            values,
        )

    direct_alias = tmp_path / "reviewed-alias"
    direct_alias.symlink_to(reviewed, target_is_directory=True)
    with pytest.raises(deployment_env.DeploymentEnvError, match="must not contain a symlink"):
        deployment_env.prepare_terraform_command(
            ["terraform", f"-chdir={direct_alias}", "init"],
            values,
        )

    prepared = deployment_env.prepare_terraform_command(
        ["terraform", f"-chdir={reviewed}", "init"],
        values,
    )
    assert prepared[1] == f"-chdir={reviewed.resolve()}"
    assert reviewed.resolve() == deployment_env.REVIEWED_TERRAFORM_DIR


@pytest.mark.parametrize(
    "subcommand, option",
    [
        ("plan", "-destroy"),
        ("plan", "-out=unbound.tfplan"),
        ("plan", "-refresh=false"),
        ("plan", "-replace=google_storage_bucket.audit"),
        ("plan", "-target=google_storage_bucket.audit"),
        ("apply", "-auto-approve"),
        ("apply", "-destroy"),
        ("apply", "-input=false"),
        ("apply", "-refresh-only"),
        ("apply", "-replace=google_storage_bucket.audit"),
        ("apply", "-target=google_storage_bucket.audit"),
    ],
)
def test_terraform_runner_rejects_destructive_or_bypass_options(
    tmp_path: Path,
    subcommand: str,
    option: str,
) -> None:
    values = _ready_values()
    metadata_dir = tmp_path / ".terraform"
    metadata_dir.mkdir()
    (metadata_dir / "terraform.tfstate").write_text(
        json.dumps(
            {
                "backend": {
                    "type": "gcs",
                    "config": {
                        "bucket": "approved-doc1-tfstate",
                        "prefix": "doc1/production",
                    },
                }
            }
        )
    )

    with pytest.raises(deployment_env.DeploymentEnvError, match="option or argument .* forbidden"):
        deployment_env.prepare_terraform_command(
            ["terraform", f"-chdir={tmp_path}", subcommand, option],
            values,
        )


def test_secret_version_verification_hashes_exact_remote_bytes(monkeypatch) -> None:
    values = _ready_values()
    seen_environments: list[dict[str, str]] = []

    class Result:
        stdout = b""

    def fake_run(argv, *, check, env, capture_output):
        assert check is True
        assert capture_output is True
        seen_environments.append(env)
        result = Result()
        if f"--secret={values['DOC1_INSTALLATION_MANIFEST_SECRET_ID']}" in argv:
            result.stdout = base64.b64decode(values["DOC1_INSTALLATION_MANIFEST_B64"])
        else:
            result.stdout = base64.b64decode(values["DOC1_RUNTIME_SETTINGS_B64"])
        return result

    monkeypatch.setattr(deployment_env.subprocess, "run", fake_run)

    deployment_env.verify_secret_versions(values)

    assert len(seen_environments) == 2
    assert all("DOC1_RUNTIME_SETTINGS_B64" not in env for env in seen_environments)


def test_stack_lifecycle_proves_new_absence_and_existing_retention(monkeypatch) -> None:
    values = _ready_values()

    class Result:
        def __init__(self, returncode, stdout=b"", stderr=b""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr(
        deployment_env.subprocess,
        "run",
        lambda *args, **kwargs: Result(
            1,
            stdout=b" \n\t",
            stderr=(
                b"ERROR: (gcloud.logging.buckets.describe) "
                b"NOT_FOUND: Bucket [cdd-sow-agent-worm] not found in "
                b"[projects/approved-doc1-prod/locations/asia-southeast1]."
            ),
        ),
    )
    deployment_env.verify_stack_lifecycle(values)

    values["DOC1_STACK_LIFECYCLE"] = "existing"
    values["DOC1_EXISTING_LOCKED_RETENTION_DAYS"] = "2557"
    values["DOC1_AUDIT_RETENTION_DAYS"] = "2557"
    monkeypatch.setattr(
        deployment_env.subprocess,
        "run",
        lambda *args, **kwargs: Result(
            0, stdout=json.dumps({"retentionDays": 2557, "locked": True}).encode()
        ),
    )
    deployment_env.verify_stack_lifecycle(values)


def test_stack_lifecycle_rejects_existing_bucket_for_new_stack(monkeypatch) -> None:
    values = _ready_values()

    class Result:
        returncode = 0
        stdout = json.dumps({"retentionDays": 180, "locked": True}).encode()
        stderr = b""

    monkeypatch.setattr(deployment_env.subprocess, "run", lambda *args, **kwargs: Result())

    with pytest.raises(deployment_env.DeploymentEnvError, match="already exists"):
        deployment_env.verify_stack_lifecycle(values)


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (1, b"", b"ERROR: credential file /tmp/missing.json was not found"),
        (
            2,
            b"",
            b"ERROR: (gcloud.logging.buckets.describe) "
            b"NOT_FOUND: Bucket [cdd-sow-agent-worm] not found in "
            b"[projects/approved-doc1-prod/locations/asia-southeast1].",
        ),
        (
            1,
            b"",
            b"ERROR: (gcloud.logging.buckets.describe) "
            b"NOT_FOUND: Bucket [cdd-sow-agent-worm] not found in "
            b"[projects/missing-project/locations/asia-southeast1].",
        ),
        (
            1,
            b"",
            b"ERROR: (gcloud.logging.buckets.describe) "
            b"NOT_FOUND: Bucket [unrelated-bucket] not found in "
            b"[projects/approved-doc1-prod/locations/asia-southeast1].",
        ),
        (
            1,
            b"",
            b"ERROR: (gcloud.logging.buckets.describe) "
            b"NOT_FOUND: Bucket [cdd-sow-agent-worm] not found in "
            b"[projects/approved-doc1-prod/locations/us-central1].",
        ),
        (
            1,
            b"",
            b"ERROR: (gcloud.logging.buckets.describe) "
            b"NOT_FOUND: Bucket [cdd-sow-agent-worm] not found in "
            b"[projects/approved-doc1-prod/locations/asia-southeast1].\n"
            b"ERROR: credential refresh failed",
        ),
        (
            1,
            b"credential helper warning",
            b"ERROR: (gcloud.logging.buckets.describe) "
            b"NOT_FOUND: Bucket [cdd-sow-agent-worm] not found in "
            b"[projects/approved-doc1-prod/locations/asia-southeast1].",
        ),
        (
            1,
            b'{"name":"projects/approved-doc1-prod/locations/asia-southeast1/'
            b'buckets/cdd-sow-agent-worm"}',
            b"ERROR: (gcloud.logging.buckets.describe) "
            b"NOT_FOUND: Bucket [cdd-sow-agent-worm] not found in "
            b"[projects/approved-doc1-prod/locations/asia-southeast1].",
        ),
    ],
)
def test_new_stack_rejects_non_resource_not_found_gcloud_failures(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: bytes,
    stderr: bytes,
) -> None:
    values = _ready_values()

    class Result:
        stdout = b""

        def __init__(self) -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr(deployment_env.subprocess, "run", lambda *args, **kwargs: Result())

    with pytest.raises(deployment_env.DeploymentEnvError, match="absence could not be proven"):
        deployment_env.verify_stack_lifecycle(values)


def test_reference_posture_relaxes_only_ha_and_the_worm_lock() -> None:
    """The reference posture must relax exactly two rules, and nothing else.

    Written after the reference deployment on 2026-08-24. The risk being guarded is scope
    creep in a relaxation: a posture that quietly waives one more rule each time it is
    convenient stops being a documented deviation and becomes a second, weaker product.
    """
    values = _ready_values()
    values["DOC1_DEPLOYMENT_POSTURE"] = "reference"
    values["DOC1_WORM_LOCK_APPROVED"] = "false"
    values["DOC1_EDGE_MIN_INSTANCES"] = "0"
    assert deployment_env.validate_environment(values, require_ready=True) == []

    # The SAME two values are rejected under the production posture, which is what makes this
    # a posture and not a hole. Proved here rather than assumed.
    values["DOC1_DEPLOYMENT_POSTURE"] = "production"
    errors = deployment_env.validate_environment(values, require_ready=True)
    assert any("DOC1_WORM_LOCK_APPROVED" in error for error in errors)
    assert any("DOC1_EDGE_MIN_INSTANCES" in error for error in errors)


def test_reference_posture_still_enforces_everything_else() -> None:
    """Residency, exact origins and real alert channels are what the stack demonstrates."""
    values = _ready_values()
    values["DOC1_DEPLOYMENT_POSTURE"] = "reference"
    values["DOC1_WORM_LOCK_APPROVED"] = "false"
    values["DOC1_EDGE_MIN_INSTANCES"] = "0"
    values["GCP_REGION"] = "europe-west2"  # not in DOC1_ALLOWED_REGIONS
    errors = deployment_env.validate_environment(values, require_ready=True)
    assert any("GCP_REGION" in error for error in errors)


def test_reference_posture_rejects_a_negative_replica_count() -> None:
    values = _ready_values()
    values["DOC1_DEPLOYMENT_POSTURE"] = "reference"
    values["DOC1_WORM_LOCK_APPROVED"] = "false"
    values["DOC1_EDGE_MIN_INSTANCES"] = "-1"
    errors = deployment_env.validate_environment(values, require_ready=True)
    assert any("must not be negative" in error for error in errors)


def test_an_unknown_posture_is_rejected() -> None:
    values = _ready_values()
    values["DOC1_DEPLOYMENT_POSTURE"] = "demo"
    errors = deployment_env.validate_environment(values, require_ready=True)
    assert any("DOC1_DEPLOYMENT_POSTURE" in error for error in errors)


def test_relaxations_are_disclosed_not_silent() -> None:
    """A relaxed rule that passes quietly is how an evidence pack claims an unexercised control."""
    values = _ready_values()
    values["DOC1_DEPLOYMENT_POSTURE"] = "reference"
    values["DOC1_WORM_LOCK_APPROVED"] = "false"
    values["DOC1_EDGE_MIN_INSTANCES"] = "0"
    disclosures = deployment_env.posture_disclosures(values)
    assert any("NOT locked" in line for line in disclosures)
    assert any("high availability is not demonstrated" in line.lower() for line in disclosures)

    # A production posture discloses nothing, because it waives nothing.
    values["DOC1_DEPLOYMENT_POSTURE"] = "production"
    assert deployment_env.posture_disclosures(values) == []


def test_reference_posture_relaxes_the_retention_floor_but_not_below_a_day() -> None:
    """The six-month floor pairs with the lock; an unlocked stack evidences neither.

    Added after the 2026-08-24 reference deployment, where the first version of the posture
    relaxed the WORM lock and the replica floor but NOT this rule, because the retention check
    runs before the posture was derived. The stack failed the gate on a floor it was meant to
    be exempt from, which is the ordering bug this test pins.
    """
    values = _ready_values()
    values["DOC1_DEPLOYMENT_POSTURE"] = "reference"
    values["DOC1_WORM_LOCK_APPROVED"] = "false"
    values["DOC1_AUDIT_RETENTION_DAYS"] = "3"
    assert deployment_env.validate_environment(values, require_ready=True) == []

    values["DOC1_AUDIT_RETENTION_DAYS"] = "0"
    errors = deployment_env.validate_environment(values, require_ready=True)
    assert any("at least 1" in error for error in errors)

    values["DOC1_DEPLOYMENT_POSTURE"] = "production"
    values["DOC1_AUDIT_RETENTION_DAYS"] = "3"
    errors = deployment_env.validate_environment(values, require_ready=True)
    assert any("at least 180" in error for error in errors)


def test_a_managed_zone_of_none_is_a_statement_not_a_blank() -> None:
    """Terraform always supported no zone; the preflight demanded one, and they disagreed."""
    values = _ready_values()
    values["DOC1_DNS_MANAGED_ZONE"] = "none"
    assert deployment_env.validate_environment(values, require_ready=True) == []

    # The OWNER is still required: the control is that somebody is accountable for how the
    # name resolves, not that a Cloud DNS zone exists.
    values["DOC1_DNS_OWNER"] = ""
    errors = deployment_env.validate_environment(values, require_ready=True)
    assert any("DOC1_DNS_OWNER" in error for error in errors)


def test_none_zone_reaches_terraform_as_an_empty_string() -> None:
    values = _ready_values()
    values["DOC1_DNS_MANAGED_ZONE"] = "none"
    assert deployment_env.terraform_environment(values)["TF_VAR_dns_managed_zone"] == ""


def test_an_omitted_posture_is_treated_as_production() -> None:
    """Relaxations are asked for, never inherited by omission."""
    values = _ready_values()
    del values["DOC1_DEPLOYMENT_POSTURE"]
    values["DOC1_WORM_LOCK_APPROVED"] = "false"
    errors = deployment_env.validate_environment(values, require_ready=True)
    assert any("DOC1_WORM_LOCK_APPROVED" in error for error in errors)


def test_an_unapproved_worm_lock_is_not_asked_for_in_terraform() -> None:
    """The irreversible one. An unapproved lock must not reach Terraform as `true`.

    `TF_VAR_worm_locked` was the literal string "true", so a reference stack that had
    deliberately not approved the lock still asked Terraform for it. The preflight printed
    "audit retention is applied but NOT locked" while the same run requested the lock, and the
    only thing that stopped the apply was the variable's own
    `worm_locked ? retention_days >= 180` validation failing on a NON-compliant 3-day retention.
    Raising retention to the compliant 180, which the reference-posture disclosure nudges
    toward, would have armed a 180-day irreversible lock nobody approved.
    """

    values = _ready_values()
    values["DOC1_WORM_LOCK_APPROVED"] = "false"

    assert deployment_env.terraform_environment(values)["TF_VAR_worm_locked"] == "false"


def test_an_approved_worm_lock_still_reaches_terraform() -> None:
    """The other direction, so the fix cannot become "never lock anything"."""

    values = _ready_values()
    values["DOC1_WORM_LOCK_APPROVED"] = "true"

    assert deployment_env.terraform_environment(values)["TF_VAR_worm_locked"] == "true"


def test_a_non_boolean_worm_approval_is_not_read_as_approval() -> None:
    """Anything that is not an affirmative is not an approval, for this flag especially."""

    for value in ("", "maybe", "TRUE-ish", "0", "no"):
        values = _ready_values()
        values["DOC1_WORM_LOCK_APPROVED"] = value
        mapped = deployment_env.terraform_environment(values)
        assert mapped["TF_VAR_worm_locked"] == "false", f"{value!r} was read as approval"
