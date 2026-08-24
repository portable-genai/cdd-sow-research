#!/usr/bin/env python3
"""Load and validate Doc1 named-production environment files.

Non-secret deployment decisions belong in ``.env``. Secret values belong in
``.env.secrets``. This loader rejects cross-file leakage, duplicate keys, and production
runs that still contain placeholders. Values remain literal and shell syntax is never
evaluated. The loader does not contact GCP or apply Terraform.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = ROOT / ".env"
DEFAULT_SECRETS = ROOT / ".env.secrets"
REVIEWED_TERRAFORM_DIR = (ROOT / "infra" / "terraform").resolve()

SECRET_KEYS = frozenset(
    {
        "CDD_OIDC_CLIENT_SECRET",
        "CDD_SESSION_SIGNING_KEY",
        "CDD_S2S_TOKEN",
        "CDD_S2S_SIGNING_KEY",
        "DOC1_INSTALLATION_MANIFEST_B64",
        "DOC1_RUNTIME_SETTINGS_B64",
    }
)
PRODUCTION_SECRET_KEYS = frozenset(
    {
        "CDD_OIDC_CLIENT_SECRET",
        "CDD_SESSION_SIGNING_KEY",
        "DOC1_INSTALLATION_MANIFEST_B64",
        "DOC1_RUNTIME_SETTINGS_B64",
    }
)

BASE_REQUIRED = (
    "GOOGLE_CLOUD_PROJECT",
    "GCP_REGION",
    "DOC1_ALLOWED_REGIONS",
    "DOC1_INSTITUTION",
    "DOC1_INSTALLATION_NAME",
    "DOC1_NAME_PREFIX",
    "DOC1_STACK_LIFECYCLE",
    "DOC1_DEPLOYMENT_STAGE",
    "DOC1_DEPLOYMENT_POSTURE",
    "DOC1_DEPLOYMENT_OWNER",
    "DOC1_SECURITY_OWNER",
    "DOC1_OPERATIONS_OWNER",
    "DOC1_EVIDENCE_APPROVER",
    "DOC1_INCIDENT_CHANNEL",
    "DOC1_EVIDENCE_LOCATION",
    "DOC1_ACCESS_POLICY_ID",
    # The one deployment tenant, independent of identity mode. Every mode's manifest and
    # subject policy binds to THIS value; DOC1_MODE4_TENANT is a Mode 4 issuer input that
    # must agree with it, not the place other modes read the tenant from.
    "DOC1_DEPLOYMENT_TENANT",
    "DOC1_AGENT_DOMAIN",
    "DOC1_STANDALONE_DOMAIN",
    "DOC1_DNS_MANAGED_ZONE",
    "DOC1_DNS_OWNER",
    "DOC1_CERTIFICATE_OWNER",
    "DOC1_TERRAFORM_STATE_BUCKET",
    "DOC1_TERRAFORM_STATE_PREFIX",
    "DOC1_APPROVED_PARENT_ORIGINS",
    "DOC1_INSTALLATION_IDS",
    "DOC1_API_IMAGE",
    "DOC1_UI_IMAGE",
    "DOC1_INSTALLATION_MANIFEST_SECRET_ID",
    "DOC1_INSTALLATION_MANIFEST_SECRET_VERSION",
    "DOC1_RUNTIME_SETTINGS_SECRET_ID",
    "DOC1_RUNTIME_SETTINGS_SECRET_VERSION",
    "DOC1_PRODUCTION_MANIFEST_VERSION",
    "DOC1_INSTALLATION_MANIFEST_SHA256",
    "DOC1_RUNTIME_SETTINGS_SHA256",
    "DOC1_AUDIT_RETENTION_DAYS",
    "DOC1_EXISTING_LOCKED_RETENTION_DAYS",
    "DOC1_WORM_LOCK_APPROVED",
    "DOC1_EDGE_MIN_INSTANCES",
    "DOC1_ALERT_NOTIFICATION_CHANNELS",
    "DOC1_EMBED_SIGNING_PROTECTION_LEVEL",
    "DOC1_DEPLOYMENT_PHASE",
    "DOC1_VPC_SC_ENFORCE",
)
EDGE_ONLY_REQUIRED = frozenset(
    {
        "DOC1_AGENT_DOMAIN",
        "DOC1_STANDALONE_DOMAIN",
        "DOC1_DNS_MANAGED_ZONE",
        "DOC1_DNS_OWNER",
        "DOC1_CERTIFICATE_OWNER",
        "DOC1_APPROVED_PARENT_ORIGINS",
        "DOC1_INSTALLATION_IDS",
        "DOC1_API_IMAGE",
        "DOC1_UI_IMAGE",
        "DOC1_INSTALLATION_MANIFEST_SECRET_ID",
        "DOC1_INSTALLATION_MANIFEST_SECRET_VERSION",
        "DOC1_RUNTIME_SETTINGS_SECRET_ID",
        "DOC1_RUNTIME_SETTINGS_SECRET_VERSION",
        "DOC1_PRODUCTION_MANIFEST_VERSION",
        "DOC1_INSTALLATION_MANIFEST_SHA256",
        "DOC1_RUNTIME_SETTINGS_SHA256",
        "DOC1_EDGE_MIN_INSTANCES",
        "DOC1_EMBED_SIGNING_KEY_VERSION",
        "DOC1_DEPLOYMENT_TENANT",
    }
)

MODE4_REQUIRED = (
    "DOC1_MODE4_ISSUER",
    "DOC1_MODE4_JWKS_URI",
    "DOC1_MODE4_RESOURCE_AUDIENCE",
    "DOC1_MODE4_TENANT",
    "DOC1_MODE4_ALLOWED_CLIENTS",
    "DOC1_MODE4_REQUIRED_SCOPES",
    "DOC1_MODE4_CLAIM_MAPPING_OWNER",
    "DOC1_MODE4_NEGATIVE_TEST_OWNER",
)

#: Which RFC 8693 subject-token type the Mode 5 installations accept. The access-token
#: profile is the default; the ID-token profile's subject audience is an OAuth client id
#: rather than a broker URL, so several checks below key off this value.
ACCESS_TOKEN_SUBJECT_TYPE = "urn:ietf:params:oauth:token-type:access_token"
ID_TOKEN_SUBJECT_TYPE = "urn:ietf:params:oauth:token-type:id_token"

MODE5_REQUIRED = (
    "DOC1_MODE5_SUBJECT_TOKEN_TYPE",
    "DOC1_MODE5_SUBJECT_ISSUER",
    "DOC1_MODE5_SUBJECT_JWKS_URI",
    "DOC1_MODE5_SUBJECT_AUDIENCE",
    "DOC1_MODE5_SUBJECT_CLIENT",
    "DOC1_MODE5_GRANT_SCOPE",
    "DOC1_MODE5_RESOURCE_AUDIENCE",
    "DOC1_MODE5_RESOURCE_SCOPES",
    "DOC1_MODE5_BFF_CLIENT_ID",
    "DOC1_MODE5_BFF_AUTH_METHOD",
    "DOC1_MODE5_BFF_JWKS_URI",
    "DOC1_MODE5_REVOCATION_OWNER",
)

MODE6_REQUIRED = (
    "DOC1_MODE6_ISSUER",
    "DOC1_MODE6_CLIENT_ID",
    "DOC1_MODE6_CALLBACK_URL",
    "DOC1_MODE6_SUBJECT_LINKS_OWNER",
)

PLACEHOLDER_MARKERS = (
    "placeholder",
    "pending",
    "replace",
    "tbd",
    "todo",
    "changeme",
    "your-",
    "your_",
    "example.com",
    "example.org",
    "example.net",
    ".test",
    "<",
    ">",
)
SECRET_PLACEHOLDER_SENTINELS = frozenset(
    {
        "",
        "CHANGEME",
        "PENDING",
        "PLACEHOLDER",
        "REPLACE_WITH_BASE64_SECRET",
        "REPLACE_WITH_SECRET",
        "TBD",
        "TODO",
        "changeme",
        "pending",
        "placeholder",
        "replace_with_base64_secret",
        "replace_with_secret",
        "tbd",
        "todo",
    }
)
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])$")
DIGEST_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
INSTALLATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GCS_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
STATE_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
SECRET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
ALERT_CHANNEL_RE = re.compile(
    r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/notificationChannels/[0-9]+$"
)
MAX_SECRET_PAYLOAD_BYTES = 1024 * 1024
MAX_OPAQUE_SECRET_BYTES = 4096
RUNTIME_ROOT_KEYS = frozenset(
    {
        "adapters",
        "agent_engine",
        "browser_flow_store",
        "case_store",
        "channel",
        "deployment",
        "dlp",
        "document_ai",
        "document_store",
        "grounding_enabled",
        "identity",
        "kms_key",
        "knowledge_base",
        "live",
        "local",
        "logging",
        "model_armor",
        "models",
        "pii",
        "policy",
        "profile",
        "project_id",
        "region",
        "sanctions",
        "web",
    }
)
RUNTIME_NESTED_KEYS = {
    "deployment": frozenset({"production", "replica_count", "standalone"}),
    "channel": frozenset(
        {
            "installation_manifest",
            "manifest_version",
            "mode",
            "public_mount_path",
            "public_origin",
            "verifier_policies",
        }
    ),
    "identity": frozenset(
        {
            "access_token_issuers",
            "allowed_return_to_hosts",
            "bindings",
            "citation_subject_links",
            "embedded_grant",
            "mode",
            "session_accepted_key_envs",
            "session_signing_key_env",
            "session_ttl_seconds",
            "trusted_issuers",
        }
    ),
    "embedded_grant": frozenset(
        {
            "id_token_subject_issuers",
            "installations",
            "subject_token_issuers",
            "token",
        }
    ),
}
SAFE_AMBIENT_ENV = frozenset(
    {
        "CLOUDSDK_CONFIG",
        "GCLOUD_KEYFILE_JSON",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_BACKEND_CREDENTIALS",
        "GOOGLE_CLOUD_KEYFILE_JSON",
        "GOOGLE_CREDENTIALS",
        "GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES",
        "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
        "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT_DELEGATES",
        "GOOGLE_OAUTH_ACCESS_TOKEN",
        "GOOGLE_USE_DEFAULT_CREDENTIALS",
        "HOME",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "NO_PROXY",
        "PATH",
        "SSL_CERT_FILE",
        "TMPDIR",
    }
)
TERRAFORM_ALLOWED_OPTIONS = {
    "init": frozenset({"-input=false", "-no-color"}),
    "validate": frozenset({"-json", "-no-color"}),
    "plan": frozenset(
        {
            "-compact-warnings",
            "-detailed-exitcode",
            "-input=false",
            "-lock=true",
            "-no-color",
            "-refresh=true",
        }
    ),
    "apply": frozenset(
        {
            "-compact-warnings",
            "-input=true",
            "-lock=true",
            "-no-color",
        }
    ),
}


class DeploymentEnvError(ValueError):
    """One or more deployment environment requirements were not met."""


def _parse_value(raw: str, *, path: Path, line_number: int) -> str:
    if not raw:
        return ""
    if raw != raw.strip():
        raise DeploymentEnvError(
            f"{path}:{line_number}: leading or trailing value whitespace must be quoted"
        )
    if raw[0] in {"'", '"'}:
        delimiter = raw[0]
        if len(raw) < 2 or raw[-1] != delimiter:
            raise DeploymentEnvError(
                f"{path}:{line_number}: quoted value must end with its opening delimiter"
            )
        value = raw[1:-1]
        if delimiter in value:
            raise DeploymentEnvError(
                f"{path}:{line_number}: embedded {delimiter} is ambiguous; "
                "use the other quote delimiter"
            )
    else:
        if "#" in raw:
            raise DeploymentEnvError(
                f"{path}:{line_number}: unquoted # is forbidden; use a preceding comment line"
            )
        if any(character.isspace() for character in raw):
            raise DeploymentEnvError(
                f"{path}:{line_number}: values containing whitespace must be quoted"
            )
        value = raw
    return value


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a strict dotenv subset without evaluating shell code."""
    if not path.is_file():
        raise DeploymentEnvError(f"required environment file does not exist: {path}")

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        line = raw_line
        if line != line.lstrip():
            raise DeploymentEnvError(f"{path}:{line_number}: keys must not be indented")
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            raise DeploymentEnvError(f"{path}:{line_number}: expected KEY=VALUE")
        key, raw_value = line.split("=", 1)
        if not KEY_RE.fullmatch(key):
            raise DeploymentEnvError(f"{path}:{line_number}: invalid key {key!r}")
        if key in values:
            raise DeploymentEnvError(f"{path}:{line_number}: duplicate key {key}")
        values[key] = _parse_value(raw_value, path=path, line_number=line_number)
    return values


def load_environment(env_path: Path, secrets_path: Path) -> dict[str, str]:
    """Load both files and enforce the secret/non-secret boundary."""
    public = parse_env_file(env_path)
    secrets = parse_env_file(secrets_path)
    duplicates = sorted(public.keys() & secrets.keys())
    leaked = sorted(public.keys() & SECRET_KEYS)
    misplaced = sorted(key for key in secrets if key not in SECRET_KEYS)
    errors: list[str] = []
    if duplicates:
        errors.append(f"keys appear in both files: {', '.join(duplicates)}")
    if leaked:
        errors.append(f"secret keys must move to .env.secrets: {', '.join(leaked)}")
    if misplaced:
        errors.append(f"non-secret keys must move to .env: {', '.join(misplaced)}")
    if errors:
        raise DeploymentEnvError("; ".join(errors))
    return {**public, **secrets}


def validate_secret_file_permissions(path: Path) -> list[str]:
    """Reject group/world access to the real secret file on POSIX hosts."""
    exposed_bits = path.stat().st_mode & 0o077
    if exposed_bits:
        return [f"{path} must not be accessible by group or other users (use chmod 600)"]
    return []


def _is_true(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _has_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return not lowered or any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def _has_secret_placeholder(value: str) -> bool:
    return value in SECRET_PLACEHOLDER_SENTINELS


def _embed_signing_key_parent(values: dict[str, str]) -> str:
    prefix = values.get("DOC1_NAME_PREFIX", "")
    return (
        f"projects/{values.get('GOOGLE_CLOUD_PROJECT', '')}/"
        f"locations/{values.get('GCP_REGION') or values.get('CDD_REGION', '')}/"
        f"keyRings/{prefix}-agent-ring/"
        f"cryptoKeys/{prefix}-agent-cmek-embed-signing"
    )


def _is_embed_signing_key_version(value: str, values: dict[str, str]) -> bool:
    parent = re.escape(_embed_signing_key_parent(values))
    return re.fullmatch(rf"{parent}/cryptoKeyVersions/[1-9][0-9]*", value) is not None


def _require_https(value: str, key: str, errors: list[str]) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.fragment:
        errors.append(f"{key} must be an absolute HTTPS URL without a fragment")


def _csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _decode_payload(values: dict[str, str], key: str, errors: list[str]) -> bytes | None:
    try:
        decoded = base64.b64decode(values.get(key, ""), validate=True)
    except (binascii.Error, ValueError):
        errors.append(f"{key} must be valid canonical base64")
        return None
    if not decoded or len(decoded) > MAX_SECRET_PAYLOAD_BYTES:
        errors.append(f"{key} decoded payload must be 1 to {MAX_SECRET_PAYLOAD_BYTES} bytes")
        return None
    if base64.b64encode(decoded).decode("ascii") != values.get(key, ""):
        errors.append(f"{key} must use canonical padded base64")
        return None
    return decoded


def _validate_runtime_settings(payload: bytes, values: dict[str, str], errors: list[str]) -> None:
    try:
        import yaml
    except ImportError:
        errors.append(
            "PyYAML is required to validate DOC1_RUNTIME_SETTINGS_B64; run make install first"
        )
        return

    class UniqueKeyLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"duplicate key {key!r}",
                    key_node.start_mark,
                )
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    UniqueKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
    )
    try:
        document = yaml.load(payload.decode("utf-8"), Loader=UniqueKeyLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        errors.append(f"DOC1_RUNTIME_SETTINGS_B64 is not valid UTF-8 YAML: {exc}")
        return
    if not isinstance(document, dict):
        errors.append("DOC1_RUNTIME_SETTINGS_B64 YAML root must be a mapping")
        return

    try:
        source_root = str(ROOT / "src")
        if source_root not in sys.path:
            sys.path.insert(0, source_root)
        from cdd_sow_research.config import Settings

        override_keys = {
            "CDD_CHANNEL_PROFILE",
            "CDD_CORS_ORIGINS",
            "CDD_EXPECTED_MANIFEST_SHA256",
            "CDD_EXPECTED_SETTINGS_SHA256",
            "CDD_FRAME_ANCESTORS",
            "CDD_IDENTITY_PROFILE",
            "CDD_PROFILE",
            "CDD_SETTINGS",
        }
        # This pre-install validator is deliberately stdlib-only. Snapshot the complete
        # environment so UNSET, SET-EMPTY and SET-VALUE are all restored exactly without
        # importing application dependencies before the repository is bootstrapped.
        previous_environment = os.environ.copy()
        for key in override_keys:
            os.environ.pop(key, None)
        os.environ["CDD_EXPECTED_SETTINGS_SHA256"] = values.get("DOC1_RUNTIME_SETTINGS_SHA256", "")
        os.environ["CDD_EXPECTED_MANIFEST_SHA256"] = values.get(
            "DOC1_INSTALLATION_MANIFEST_SHA256", ""
        )
        try:
            Settings.load(exact_bytes=payload)
        finally:
            os.environ.clear()
            os.environ.update(previous_environment)
    except (AttributeError, ImportError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        errors.append(f"application Settings.load rejected runtime settings: {exc}")

    unknown_root = sorted(set(document) - RUNTIME_ROOT_KEYS)
    if unknown_root:
        errors.append(f"runtime settings contains unknown root keys: {', '.join(unknown_root)}")
    for section_name in ("deployment", "channel", "identity"):
        section = document.get(section_name)
        if isinstance(section, dict):
            unknown = sorted(set(section) - RUNTIME_NESTED_KEYS[section_name])
            if unknown:
                errors.append(
                    f"runtime settings {section_name} contains unknown keys: {', '.join(unknown)}"
                )
    identity_section = document.get("identity")
    if isinstance(identity_section, dict):
        grant_section = identity_section.get("embedded_grant")
        if isinstance(grant_section, dict):
            unknown = sorted(set(grant_section) - RUNTIME_NESTED_KEYS["embedded_grant"])
            if unknown:
                errors.append(
                    "runtime settings embedded_grant contains unknown keys: " + ", ".join(unknown)
                )

    expected_scalars = {
        "project_id": values.get("GOOGLE_CLOUD_PROJECT"),
        "region": values.get("GCP_REGION") or values.get("CDD_REGION"),
        "profile": "gcp",
    }
    for key, expected in expected_scalars.items():
        if document.get(key) != expected:
            errors.append(f"runtime settings {key} must equal the reviewed deployment value")

    deployment = document.get("deployment")
    if not isinstance(deployment, dict) or deployment.get("production") is not True:
        errors.append("runtime settings deployment.production must be true")
    else:
        try:
            replica_count = int(deployment.get("replica_count", 0))
        except (TypeError, ValueError):
            replica_count = 0
        if replica_count < int(values.get("DOC1_EDGE_MIN_INSTANCES", "0")):
            errors.append("runtime settings replica_count must cover DOC1_EDGE_MIN_INSTANCES")

    channel = document.get("channel")
    expected_origin = f"https://{values.get('DOC1_AGENT_DOMAIN', '')}"
    if not isinstance(channel, dict):
        errors.append("runtime settings channel must be a mapping")
    else:
        if channel.get("mode") != "sandboxed":
            errors.append("runtime settings channel.mode must be sandboxed")
        if channel.get("public_origin") != expected_origin:
            errors.append("runtime settings channel.public_origin must equal the agent origin")
        if channel.get("manifest_version") != values.get("DOC1_PRODUCTION_MANIFEST_VERSION"):
            errors.append("runtime settings manifest_version must equal the reviewed version")

    identity = document.get("identity")
    mode = values.get("DOC1_PRODUCTION_IDENTITY_MODE")
    if not isinstance(identity, dict) or identity.get("mode") != mode:
        errors.append("runtime settings identity.mode must equal the reviewed identity mode")
        return
    if mode == "oauth-access-token" and not identity.get("access_token_issuers"):
        errors.append("Mode 4 runtime settings require access_token_issuers")
    elif mode == "oauth-access-token":
        expected = {
            "issuer": values.get("DOC1_MODE4_ISSUER"),
            "jwks_uri": values.get("DOC1_MODE4_JWKS_URI"),
            "resource_audience": values.get("DOC1_MODE4_RESOURCE_AUDIENCE"),
            "tenant": values.get("DOC1_MODE4_TENANT"),
        }
        policies = identity["access_token_issuers"]
        if (
            not isinstance(policies, list)
            or len(policies) != 1
            or not isinstance(policies[0], dict)
        ):
            errors.append("Mode 4 runtime settings require exactly one issuer policy")
        else:
            policy = policies[0]
            if any(policy.get(key) != value for key, value in expected.items()):
                errors.append("Mode 4 runtime issuer policy does not match reviewed environment")
            if set(policy.get("allowed_clients", [])) != _csv_set(
                values.get("DOC1_MODE4_ALLOWED_CLIENTS", "")
            ):
                errors.append("Mode 4 runtime clients do not match reviewed environment")
            if set(policy.get("required_scopes", [])) != _csv_set(
                values.get("DOC1_MODE4_REQUIRED_SCOPES", "")
            ):
                errors.append("Mode 4 runtime scopes do not match reviewed environment")
    if mode == "embedded-grant":
        grant = identity.get("embedded_grant")
        id_token_profile = values.get("DOC1_MODE5_SUBJECT_TOKEN_TYPE", "") == ID_TOKEN_SUBJECT_TYPE
        subject_family = "id_token_subject_issuers" if id_token_profile else "subject_token_issuers"
        if not isinstance(grant, dict):
            errors.append("Mode 5 runtime settings require embedded_grant policy")
        elif (
            not grant.get(subject_family)
            or not grant.get("installations")
            or not grant.get("token")
        ):
            errors.append("Mode 5 runtime settings require subject, BFF, and token policy")
        else:
            subject_policies = grant[subject_family]
            installations = grant["installations"]
            if (
                not isinstance(subject_policies, list)
                or len(subject_policies) != 1
                or not isinstance(subject_policies[0], dict)
            ):
                errors.append("Mode 5 runtime settings require exactly one subject policy")
            else:
                subject = subject_policies[0]
                expected_subject = {
                    "issuer": values.get("DOC1_MODE5_SUBJECT_ISSUER"),
                    "jwks_uri": values.get("DOC1_MODE5_SUBJECT_JWKS_URI"),
                    # Mode-neutral: the subject policy binds to the deployment tenant.
                    "tenant": values.get("DOC1_DEPLOYMENT_TENANT"),
                }
                if id_token_profile:
                    # The ID-token profile names its audience and authorised party
                    # directly; it has no resource_audience, scope or client list.
                    expected_subject["audience"] = values.get("DOC1_MODE5_SUBJECT_AUDIENCE")
                    expected_subject["authorized_party"] = values.get("DOC1_MODE5_SUBJECT_CLIENT")
                else:
                    expected_subject["resource_audience"] = values.get(
                        "DOC1_MODE5_SUBJECT_AUDIENCE"
                    )
                if any(subject.get(key) != value for key, value in expected_subject.items()):
                    errors.append("Mode 5 subject policy does not match reviewed environment")
                if not id_token_profile:
                    if set(subject.get("allowed_clients", [])) != {
                        values.get("DOC1_MODE5_SUBJECT_CLIENT")
                    }:
                        errors.append("Mode 5 subject client does not match reviewed environment")
                    if set(subject.get("required_scopes", [])) != {
                        values.get("DOC1_MODE5_GRANT_SCOPE")
                    }:
                        errors.append("Mode 5 grant scope does not match reviewed environment")
            if not isinstance(installations, list) or {
                item.get("installation_id") for item in installations if isinstance(item, dict)
            } != _csv_set(values.get("DOC1_INSTALLATION_IDS", "")):
                errors.append("Mode 5 runtime installation ids do not match reviewed environment")
            else:
                for installation in installations:
                    if installation.get(
                        "subject_token_type", ACCESS_TOKEN_SUBJECT_TYPE
                    ) != values.get("DOC1_MODE5_SUBJECT_TOKEN_TYPE"):
                        errors.append(
                            "Mode 5 runtime subject token type does not match reviewed environment"
                        )
                    if installation.get("subject_token_audience") != values.get(
                        "DOC1_MODE5_SUBJECT_AUDIENCE"
                    ):
                        errors.append(
                            "Mode 5 runtime subject audience does not match reviewed environment"
                        )
                    if installation.get("subject_grant_scope") != values.get(
                        "DOC1_MODE5_GRANT_SCOPE"
                    ):
                        errors.append(
                            "Mode 5 runtime grant scope does not match reviewed environment"
                        )
                    bff_clients = installation.get("bff_clients", [])
                    if (
                        not isinstance(bff_clients, list)
                        or len(bff_clients) != 1
                        or not isinstance(bff_clients[0], dict)
                        or bff_clients[0].get("client_id") != values.get("DOC1_MODE5_BFF_CLIENT_ID")
                    ):
                        errors.append(
                            "Mode 5 runtime BFF client does not match reviewed environment"
                        )
                    else:
                        bff = bff_clients[0]
                        if set(bff.get("permitted_scopes", [])) != _csv_set(
                            values.get("DOC1_MODE5_RESOURCE_SCOPES", "")
                        ):
                            errors.append(
                                "Mode 5 runtime BFF scopes do not match reviewed environment"
                            )
                        if set(bff.get("allowed_subject_clients", [])) != {
                            values.get("DOC1_MODE5_SUBJECT_CLIENT")
                        }:
                            errors.append(
                                "Mode 5 runtime BFF subject clients do not match environment"
                            )
            token = grant["token"]
            if not isinstance(token, dict) or token.get("audience") != values.get(
                "DOC1_MODE5_RESOURCE_AUDIENCE"
            ):
                errors.append(
                    "Mode 5 runtime resource audience does not match reviewed environment"
                )
            if isinstance(token, dict):
                token_keys = token.get("keys")
                active_kid = token.get("active_kid")
                if not isinstance(token_keys, list):
                    errors.append("Mode 5 runtime token keys must be a list")
                else:
                    for token_key in token_keys:
                        if not isinstance(token_key, dict):
                            continue
                        kms_key_version = token_key.get("kms_key_version")
                        if kms_key_version and not _is_embed_signing_key_version(
                            kms_key_version, values
                        ):
                            errors.append(
                                "Mode 5 runtime KMS versions must use the reviewed Terraform "
                                "signing-key resource"
                            )
                    active_keys = [
                        item
                        for item in token_keys
                        if isinstance(item, dict) and item.get("kid") == active_kid
                    ]
                    reviewed_key_version = values.get("DOC1_EMBED_SIGNING_KEY_VERSION", "")
                    if (
                        len(active_keys) != 1
                        or active_keys[0].get("kms_key_version") != reviewed_key_version
                    ):
                        errors.append(
                            "Mode 5 active runtime KMS version must equal "
                            "DOC1_EMBED_SIGNING_KEY_VERSION"
                        )


def _validate_secret_payloads(values: dict[str, str], errors: list[str]) -> None:
    oidc_secret = values.get("CDD_OIDC_CLIENT_SECRET", "")
    encoded_oidc_secret = oidc_secret.encode("utf-8")
    if not 1 <= len(encoded_oidc_secret) <= MAX_OPAQUE_SECRET_BYTES:
        errors.append(f"CDD_OIDC_CLIENT_SECRET must be 1 to {MAX_OPAQUE_SECRET_BYTES} UTF-8 bytes")
    if any(not character.isprintable() for character in oidc_secret):
        errors.append("CDD_OIDC_CLIENT_SECRET must not contain control characters")

    material = _decode_payload(values, "CDD_SESSION_SIGNING_KEY", errors)
    if material is not None:
        if len(material) != 32:
            errors.append("CDD_SESSION_SIGNING_KEY must decode to exactly 32 bytes (256 bits)")
        if len(set(material)) < 16:
            errors.append("CDD_SESSION_SIGNING_KEY must be generated high-diversity material")

    manifest_bytes = _decode_payload(values, "DOC1_INSTALLATION_MANIFEST_B64", errors)
    settings_bytes = _decode_payload(values, "DOC1_RUNTIME_SETTINGS_B64", errors)
    if manifest_bytes is not None:
        try:
            source_root = str(ROOT / "src")
            if source_root not in sys.path:
                sys.path.insert(0, source_root)
            from cdd_sow_research.embedding.manifest import (
                ManifestValidationError,
                parse_installation_manifest,
            )

            loaded = parse_installation_manifest(manifest_bytes)
        except (ImportError, ManifestValidationError, ValueError) as exc:
            errors.append(f"DOC1_INSTALLATION_MANIFEST_B64 schema is invalid: {exc}")
        else:
            expected_ids = {
                item.strip()
                for item in values.get("DOC1_INSTALLATION_IDS", "").split(",")
                if item.strip()
            }
            actual_ids = {
                installation.installation_id for installation in loaded.manifest.installations
            }
            if actual_ids != expected_ids:
                errors.append("installation manifest ids must exactly match DOC1_INSTALLATION_IDS")
            if loaded.manifest.identity_mode != values.get("DOC1_PRODUCTION_IDENTITY_MODE"):
                errors.append("installation manifest identity mode does not match deployment")
            # Mode-neutral: the manifest binds to the deployment tenant, not to a Mode 4
            # variable an embedded-grant deployment has no reason to populate.
            if loaded.manifest.tenant != values.get("DOC1_DEPLOYMENT_TENANT"):
                errors.append("installation manifest tenant does not match deployment")
            expected_origin = f"https://{values.get('DOC1_AGENT_DOMAIN', '')}"
            if any(
                installation.public_origin != expected_origin
                for installation in loaded.manifest.installations
            ):
                errors.append("installation manifest public origin does not match deployment")
            expected_parents = {
                item.strip()
                for item in values.get("DOC1_APPROVED_PARENT_ORIGINS", "").split(",")
                if item.strip()
            }
            if any(
                set(installation.parent_origins) != expected_parents
                for installation in loaded.manifest.installations
            ):
                errors.append("installation manifest parent origins do not match deployment")
            mode = values.get("DOC1_PRODUCTION_IDENTITY_MODE")
            expected_resource_audience = values.get(
                "DOC1_MODE4_RESOURCE_AUDIENCE"
                if mode == "oauth-access-token"
                else "DOC1_MODE5_RESOURCE_AUDIENCE"
            )
            expected_clients = (
                _csv_set(values.get("DOC1_MODE4_ALLOWED_CLIENTS", ""))
                if mode == "oauth-access-token"
                else {values.get("DOC1_MODE5_BFF_CLIENT_ID", "")}
            )
            expected_scopes = _csv_set(
                values.get(
                    "DOC1_MODE4_REQUIRED_SCOPES"
                    if mode == "oauth-access-token"
                    else "DOC1_MODE5_RESOURCE_SCOPES",
                    "",
                )
            )
            if any(
                installation.resource_audience != expected_resource_audience
                for installation in loaded.manifest.installations
            ):
                errors.append("installation manifest resource audience does not match deployment")
            if any(
                set(installation.allowed_clients) != expected_clients
                for installation in loaded.manifest.installations
            ):
                errors.append("installation manifest clients do not match deployment")
            if any(
                set(installation.scopes) != expected_scopes
                for installation in loaded.manifest.installations
            ):
                errors.append("installation manifest scopes do not match deployment")
            expected_fallback = f"https://{values.get('DOC1_STANDALONE_DOMAIN', '')}"
            if any(
                not installation.fallback_url.startswith(expected_fallback + "/")
                for installation in loaded.manifest.installations
            ):
                errors.append("installation manifest fallback URL does not use standalone origin")
            expected_digest = values.get("DOC1_INSTALLATION_MANIFEST_SHA256", "")
            if loaded.sha256 != expected_digest:
                errors.append("installation manifest bytes do not match the reviewed SHA-256")

    if settings_bytes is not None:
        expected_digest = values.get("DOC1_RUNTIME_SETTINGS_SHA256", "")
        if hashlib.sha256(settings_bytes).hexdigest() != expected_digest:
            errors.append("runtime settings bytes do not match the reviewed SHA-256")
        _validate_runtime_settings(settings_bytes, values, errors)


def validate_environment(values: dict[str, str], *, require_ready: bool = False) -> list[str]:
    # One canonical selector. CDD_REGION is accepted for one release as an input alias.
    values.setdefault("GCP_REGION", values.get("CDD_REGION", ""))
    """Validate structure, residency, identity inputs, and production completeness.

    Placeholder values are permitted only while ``DOC1_DEPLOYMENT_ENABLED`` is false.
    ``require_ready`` is used by the execution path and forces the production gate.
    """
    errors: list[str] = []
    ready = require_ready or _is_true(values.get("DOC1_DEPLOYMENT_ENABLED", "false"))

    retention = values.get("DOC1_AUDIT_RETENTION_DAYS", "180")
    try:
        retention_days = int(retention)
        if retention_days < 180:
            errors.append("DOC1_AUDIT_RETENTION_DAYS must be at least 180")
    except ValueError:
        errors.append("DOC1_AUDIT_RETENTION_DAYS must be an integer")
        retention_days = 0
    try:
        existing_retention_days = int(values.get("DOC1_EXISTING_LOCKED_RETENTION_DAYS", "0"))
        if existing_retention_days != 0 and existing_retention_days < 180:
            errors.append("DOC1_EXISTING_LOCKED_RETENTION_DAYS must be 0 or at least 180")
        if existing_retention_days and retention_days < existing_retention_days:
            errors.append("audit retention cannot be lower than the existing locked value")
    except ValueError:
        errors.append("DOC1_EXISTING_LOCKED_RETENTION_DAYS must be an integer")
        existing_retention_days = -1

    region = values.get("GCP_REGION", "")
    allowed_regions = [item.strip() for item in values.get("DOC1_ALLOWED_REGIONS", "").split(",")]
    if region and region not in allowed_regions:
        errors.append("GCP_REGION must be present in DOC1_ALLOWED_REGIONS")

    if not ready:
        return errors

    mode = values.get("DOC1_PRODUCTION_IDENTITY_MODE", "")
    if mode not in {"oauth-access-token", "embedded-grant"}:
        errors.append("DOC1_PRODUCTION_IDENTITY_MODE must be oauth-access-token or embedded-grant")
    deployment_stage = values.get("DOC1_DEPLOYMENT_STAGE", "")
    if deployment_stage not in {"mode5-key-bootstrap", "production-edge"}:
        errors.append("DOC1_DEPLOYMENT_STAGE must be mode5-key-bootstrap or production-edge")
    bootstrap_stage = deployment_stage == "mode5-key-bootstrap"
    edge_stage = deployment_stage == "production-edge"
    if bootstrap_stage and mode != "embedded-grant":
        errors.append("mode5-key-bootstrap requires embedded-grant identity")
    # Posture is ORTHOGONAL to stage: stage says which part of the stack is being applied,
    # posture says what the deployment is FOR. "production" is the default and changes
    # nothing. "reference" is the maintainer-owned demonstration stack, and it relaxes
    # exactly two production rules, both named explicitly below and both reported in the
    # preflight summary so the relaxation can never be silent. Everything else — residency,
    # digest binding, exact origins, real alert channels, secret-version pinning — is
    # unchanged, because those are what the deployment exists to demonstrate.
    posture = values.get("DOC1_DEPLOYMENT_POSTURE", "")
    if posture not in {"production", "reference"}:
        errors.append("DOC1_DEPLOYMENT_POSTURE must be production or reference")
    reference_posture = posture == "reference"

    lifecycle = values.get("DOC1_STACK_LIFECYCLE", "")
    if lifecycle not in {"new", "existing"}:
        errors.append("DOC1_STACK_LIFECYCLE must be new or existing")
    if lifecycle == "new" and existing_retention_days != 0:
        errors.append("new lifecycle requires DOC1_EXISTING_LOCKED_RETENTION_DAYS=0")
    if lifecycle == "existing" and existing_retention_days < 180:
        errors.append("existing lifecycle requires the current locked retention")

    required_nonsecret = [
        key for key in BASE_REQUIRED if edge_stage or key not in EDGE_ONLY_REQUIRED
    ]
    if edge_stage:
        # Mode 6 is the separate recovery/fallback path and is always required. The Mode 4
        # registration is required only by a deployment that actually serves Mode 4:
        # forcing an embedded-grant deployment to register an issuer for a mode it does
        # not serve produced placeholder values that mean nothing. This mirrors how
        # DOC1_EMBED_SIGNING_KEY_VERSION is already conditional on embedded-grant.
        required_nonsecret.extend((*MODE5_REQUIRED, *MODE6_REQUIRED))
        if mode == "oauth-access-token":
            required_nonsecret.extend(MODE4_REQUIRED)
        if mode == "embedded-grant":
            required_nonsecret.append("DOC1_EMBED_SIGNING_KEY_VERSION")
    for key in required_nonsecret:
        if _has_placeholder(values.get(key, "")):
            errors.append(f"{key} is missing or still contains a placeholder")
    if edge_stage:
        for key in PRODUCTION_SECRET_KEYS:
            if _has_secret_placeholder(values.get(key, "")):
                errors.append(f"{key} is missing or still contains a placeholder")

    project = values.get("GOOGLE_CLOUD_PROJECT", "")
    if project and not PROJECT_RE.fullmatch(project):
        errors.append("GOOGLE_CLOUD_PROJECT is not a valid GCP project id")
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,18}", values.get("DOC1_NAME_PREFIX", "")):
        errors.append("DOC1_NAME_PREFIX must match the Terraform resource prefix contract")

    for key in ("DOC1_AGENT_DOMAIN", "DOC1_STANDALONE_DOMAIN") if edge_stage else ():
        value = values.get(key, "")
        if value and not DOMAIN_RE.fullmatch(value):
            errors.append(f"{key} must be a lowercase DNS name")

    state_bucket = values.get("DOC1_TERRAFORM_STATE_BUCKET", "")
    if state_bucket and not GCS_BUCKET_RE.fullmatch(state_bucket):
        errors.append("DOC1_TERRAFORM_STATE_BUCKET is not a valid GCS bucket name")
    state_prefix = values.get("DOC1_TERRAFORM_STATE_PREFIX", "")
    if state_prefix and (
        not STATE_PREFIX_RE.fullmatch(state_prefix)
        or state_prefix.startswith("/")
        or state_prefix.endswith("/")
        or ".." in state_prefix.split("/")
    ):
        errors.append("DOC1_TERRAFORM_STATE_PREFIX is invalid")

    subject_token_type = values.get("DOC1_MODE5_SUBJECT_TOKEN_TYPE", "")
    if edge_stage and subject_token_type not in {
        ACCESS_TOKEN_SUBJECT_TYPE,
        ID_TOKEN_SUBJECT_TYPE,
    }:
        errors.append("DOC1_MODE5_SUBJECT_TOKEN_TYPE must be a reviewed RFC 8693 token type")
    https_keys = [
        "DOC1_MODE5_SUBJECT_ISSUER",
        "DOC1_MODE5_SUBJECT_JWKS_URI",
        "DOC1_MODE5_BFF_JWKS_URI",
        "DOC1_MODE6_ISSUER",
        "DOC1_MODE6_CALLBACK_URL",
    ]
    if mode == "oauth-access-token":
        https_keys[:0] = [
            "DOC1_MODE4_ISSUER",
            "DOC1_MODE4_JWKS_URI",
            "DOC1_MODE4_RESOURCE_AUDIENCE",
        ]
    if subject_token_type != ID_TOKEN_SUBJECT_TYPE:
        # An ID-token subject audience is the dedicated OAuth client id, not a URL.
        https_keys.append("DOC1_MODE5_SUBJECT_AUDIENCE")
    for key in https_keys if edge_stage else ():
        value = values.get(key, "")
        if value:
            _require_https(value, key, errors)
    if (
        edge_stage
        and mode == "oauth-access-token"
        and values.get("DOC1_MODE4_TENANT") != values.get("DOC1_DEPLOYMENT_TENANT")
    ):
        errors.append("DOC1_MODE4_TENANT must equal the one DOC1_DEPLOYMENT_TENANT")

    for key in ("DOC1_API_IMAGE", "DOC1_UI_IMAGE") if edge_stage else ():
        value = values.get(key, "")
        if value and not DIGEST_RE.fullmatch(value):
            errors.append(f"{key} must be pinned by an @sha256 digest")

    for key in (
        (
            "DOC1_INSTALLATION_MANIFEST_SECRET_VERSION",
            "DOC1_RUNTIME_SETTINGS_SECRET_VERSION",
        )
        if edge_stage
        else ()
    ):
        if values.get(key) and not re.fullmatch(r"[1-9][0-9]*", values[key]):
            errors.append(f"{key} must be an immutable numeric version, never latest")
    for key in (
        (
            "DOC1_INSTALLATION_MANIFEST_SECRET_ID",
            "DOC1_RUNTIME_SETTINGS_SECRET_ID",
        )
        if edge_stage
        else ()
    ):
        if values.get(key) and not SECRET_ID_RE.fullmatch(values[key]):
            errors.append(f"{key} is not a valid Secret Manager secret id")
    for key in (
        ("DOC1_INSTALLATION_MANIFEST_SHA256", "DOC1_RUNTIME_SETTINGS_SHA256") if edge_stage else ()
    ):
        if values.get(key) and not SHA256_RE.fullmatch(values[key]):
            errors.append(f"{key} must be a lowercase SHA-256 digest")

    installation_ids = (
        [
            item.strip()
            for item in values.get("DOC1_INSTALLATION_IDS", "").split(",")
            if item.strip()
        ]
        if edge_stage
        else []
    )
    if any(not INSTALLATION_RE.fullmatch(item) for item in installation_ids):
        errors.append("DOC1_INSTALLATION_IDS contains an invalid installation id")

    parent_origins = (
        [
            item.strip()
            for item in values.get("DOC1_APPROVED_PARENT_ORIGINS", "").split(",")
            if item.strip()
        ]
        if edge_stage
        else []
    )
    for origin in parent_origins:
        parsed = urlparse(origin)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "*" in origin
        ):
            errors.append("DOC1_APPROVED_PARENT_ORIGINS must contain exact HTTPS origins")
            break

    if not reference_posture and values.get("DOC1_WORM_LOCK_APPROVED", "").lower() != "true":
        # A reference stack must stay destroyable, so the irreversible lock is not demanded of
        # it. The retention policy itself is still applied and still evidenced; what is not
        # exercised is immutability, and the evidence has to say so rather than claim WORM.
        errors.append("DOC1_WORM_LOCK_APPROVED must be true before production execution")
    if edge_stage:
        try:
            min_instances = int(values.get("DOC1_EDGE_MIN_INSTANCES", "0"))
            # Two replicas is a HIGH-AVAILABILITY requirement, not a correctness one: the
            # multi-replica state it protects (browser-flow outbox, replay cache) is in
            # Firestore and behaves identically at one replica or none. A reference stack
            # scales to zero so an idle demo costs nothing, and pays a cold start instead.
            # HA is therefore NOT demonstrated by a reference deployment, which is a
            # disclosure, not a defect.
            if not reference_posture and min_instances < 2:
                errors.append("DOC1_EDGE_MIN_INSTANCES must be at least 2")
            if min_instances < 0:
                errors.append("DOC1_EDGE_MIN_INSTANCES must not be negative")
        except ValueError:
            errors.append("DOC1_EDGE_MIN_INSTANCES must be an integer")
    alert_channels = [
        item.strip()
        for item in values.get("DOC1_ALERT_NOTIFICATION_CHANNELS", "").split(",")
        if item.strip()
    ]
    expected_channel_prefix = f"projects/{project}/notificationChannels/"
    if not alert_channels or any(
        not ALERT_CHANNEL_RE.fullmatch(item) or not item.startswith(expected_channel_prefix)
        for item in alert_channels
    ):
        errors.append(
            "DOC1_ALERT_NOTIFICATION_CHANNELS must contain reviewed project notification channels"
        )
    phase = values.get("DOC1_DEPLOYMENT_PHASE", "")
    enforce = values.get("DOC1_VPC_SC_ENFORCE", "").lower() == "true"
    if phase not in {"dry-run", "enforce"}:
        errors.append("DOC1_DEPLOYMENT_PHASE must be dry-run or enforce")
    if phase == "dry-run" and enforce:
        errors.append("DOC1_VPC_SC_ENFORCE must be false during the dry-run phase")
    if phase == "enforce" and not enforce:
        errors.append("DOC1_VPC_SC_ENFORCE must be true during the enforce phase")
    if phase == "enforce" and _has_placeholder(values.get("DOC1_VPC_SC_DRY_RUN_EVIDENCE", "")):
        errors.append("DOC1_VPC_SC_DRY_RUN_EVIDENCE is required before enforcement")
    if edge_stage and values.get("DOC1_MODE5_BFF_AUTH_METHOD") != "private_key_jwt":
        errors.append("DOC1_MODE5_BFF_AUTH_METHOD must be private_key_jwt")
    if (
        edge_stage
        and values.get("DOC1_AGENT_DOMAIN")
        and values.get("DOC1_AGENT_DOMAIN") == values.get("DOC1_STANDALONE_DOMAIN")
    ):
        errors.append("DOC1_STANDALONE_DOMAIN must be separate from DOC1_AGENT_DOMAIN")
    if values.get("DOC1_EMBED_SIGNING_PROTECTION_LEVEL") != "HSM":
        errors.append("named production requires HSM embed signing-key protection")
    if (
        edge_stage
        and mode == "embedded-grant"
        and not _is_embed_signing_key_version(
            values.get("DOC1_EMBED_SIGNING_KEY_VERSION", ""), values
        )
    ):
        errors.append(
            "DOC1_EMBED_SIGNING_KEY_VERSION must equal a numeric version of the reviewed "
            "Terraform signing-key resource"
        )

    if edge_stage:
        _validate_secret_payloads(values, errors)

    return errors


def terraform_environment(values: dict[str, str]) -> dict[str, str]:
    values.setdefault("GCP_REGION", values.get("CDD_REGION", ""))
    """Map the reviewed deployment contract to Terraform's environment interface."""
    deployment_stage = values["DOC1_DEPLOYMENT_STAGE"]
    edge_enabled = deployment_stage == "production-edge"
    signing_key_enabled = (
        deployment_stage == "mode5-key-bootstrap"
        or values["DOC1_PRODUCTION_IDENTITY_MODE"] == "embedded-grant"
    )
    mappings = {
        "TF_VAR_project_id": values["GOOGLE_CLOUD_PROJECT"],
        "TF_VAR_name_prefix": values["DOC1_NAME_PREFIX"],
        "TF_VAR_region": values["GCP_REGION"],
        "TF_VAR_allowed_regions": json.dumps(
            [item.strip() for item in values["DOC1_ALLOWED_REGIONS"].split(",")]
        ),
        "TF_VAR_access_policy_id": values["DOC1_ACCESS_POLICY_ID"],
        "TF_VAR_retention_days": values["DOC1_AUDIT_RETENTION_DAYS"],
        "TF_VAR_existing_locked_retention_days": values["DOC1_EXISTING_LOCKED_RETENTION_DAYS"],
        "TF_VAR_worm_locked": "true",
        "TF_VAR_vpc_sc_enforce": values["DOC1_VPC_SC_ENFORCE"].lower(),
        "TF_VAR_alert_notification_channels": json.dumps(
            [
                item.strip()
                for item in values["DOC1_ALERT_NOTIFICATION_CHANNELS"].split(",")
                if item.strip()
            ]
        ),
        "TF_VAR_deployment_stage": deployment_stage,
        "TF_VAR_production_edge_enabled": str(edge_enabled).lower(),
        "TF_VAR_production_identity_mode": values["DOC1_PRODUCTION_IDENTITY_MODE"],
        "TF_VAR_enable_embed_signing_key": str(signing_key_enabled).lower(),
        "TF_VAR_embed_signing_protection_level": values["DOC1_EMBED_SIGNING_PROTECTION_LEVEL"],
    }
    if edge_enabled:
        mappings.update(
            {
                "TF_VAR_api_image": values["DOC1_API_IMAGE"],
                "TF_VAR_ui_image": values["DOC1_UI_IMAGE"],
                "TF_VAR_agent_domain": values["DOC1_AGENT_DOMAIN"],
                "TF_VAR_dns_managed_zone": values["DOC1_DNS_MANAGED_ZONE"],
                "TF_VAR_installation_manifest_secret_id": values[
                    "DOC1_INSTALLATION_MANIFEST_SECRET_ID"
                ],
                "TF_VAR_installation_manifest_secret_version": values[
                    "DOC1_INSTALLATION_MANIFEST_SECRET_VERSION"
                ],
                "TF_VAR_runtime_settings_secret_id": values["DOC1_RUNTIME_SETTINGS_SECRET_ID"],
                "TF_VAR_runtime_settings_secret_version": values[
                    "DOC1_RUNTIME_SETTINGS_SECRET_VERSION"
                ],
                "TF_VAR_production_manifest_version": values["DOC1_PRODUCTION_MANIFEST_VERSION"],
                "TF_VAR_production_manifest_sha256": values["DOC1_INSTALLATION_MANIFEST_SHA256"],
                "TF_VAR_production_settings_sha256": values["DOC1_RUNTIME_SETTINGS_SHA256"],
                "TF_VAR_edge_min_instances": values["DOC1_EDGE_MIN_INSTANCES"],
                "TF_VAR_embed_signing_key_version": values.get(
                    "DOC1_EMBED_SIGNING_KEY_VERSION", ""
                ),
            }
        )
    return mappings


def sanitized_child_environment(values: dict[str, str]) -> dict[str, str]:
    """Build a minimal process environment without parsed secret payloads or stale TF_VARs."""
    ambient = {
        key: value
        for key, value in os.environ.items()
        if key in SAFE_AMBIENT_ENV and not key.startswith("TF_VAR_")
    }
    return {**ambient, **terraform_environment(values)}


def _terraform_command_parts(command: list[str]) -> tuple[Path, str]:
    if not command or command[0] != "terraform":
        raise DeploymentEnvError("run accepts only the terraform executable by exact name")
    terraform_dir = Path.cwd()
    raw_terraform_dir = terraform_dir
    subcommand_index = -1
    chdir_seen = False
    for index, argument in enumerate(command[1:], 1):
        if argument.startswith("-chdir="):
            if chdir_seen or argument == "-chdir=":
                raise DeploymentEnvError("terraform accepts exactly one non-empty -chdir option")
            chdir_seen = True
            raw_terraform_dir = Path(argument.split("=", 1)[1])
            terraform_dir = raw_terraform_dir
            continue
        if argument.startswith("-"):
            raise DeploymentEnvError(f"unreviewed Terraform global option {argument!r}")
        subcommand_index = index
        break
    if subcommand_index < 0:
        raise DeploymentEnvError("terraform command is missing a subcommand")
    try:
        terraform_dir = terraform_dir.resolve(strict=True)
    except OSError as exc:
        raise DeploymentEnvError("Terraform -chdir must resolve to the reviewed directory") from exc
    if terraform_dir != REVIEWED_TERRAFORM_DIR:
        raise DeploymentEnvError(
            "Terraform -chdir must resolve exactly to the repository infra/terraform directory"
        )
    lexical_dir = Path(os.path.abspath(raw_terraform_dir))
    if lexical_dir != terraform_dir:
        raise DeploymentEnvError("Terraform -chdir must not contain a symlink")
    subcommand = command[subcommand_index]
    if subcommand not in TERRAFORM_ALLOWED_OPTIONS:
        allowed = ", ".join(TERRAFORM_ALLOWED_OPTIONS)
        raise DeploymentEnvError(
            f"Terraform subcommand {subcommand!r} is forbidden; allowed: {allowed}"
        )
    return terraform_dir, subcommand


def _validate_terraform_options(command: list[str], subcommand: str) -> None:
    subcommand_index = command.index(subcommand)
    options = command[subcommand_index + 1 :]
    duplicates = sorted({option for option in options if options.count(option) > 1})
    if duplicates:
        raise DeploymentEnvError(
            "duplicate Terraform options are forbidden: " + ", ".join(duplicates)
        )
    allowed = TERRAFORM_ALLOWED_OPTIONS[subcommand]
    for option in options:
        if option not in allowed:
            if subcommand == "apply" and not option.startswith("-"):
                raise DeploymentEnvError(
                    "saved-plan apply is forbidden because the plan is not bound to reviewed "
                    "inputs; run apply without a plan file"
                )
            raise DeploymentEnvError(
                f"Terraform {subcommand} option or argument {option!r} is forbidden"
            )


def _reject_competing_terraform_inputs(terraform_dir: Path, command: list[str]) -> None:
    forbidden_args = (
        "-var",
        "-var-file",
        "-backend-config",
        "-backend=false",
        "-state",
    )
    for argument in command:
        if any(
            argument == prefix or argument.startswith(prefix + "=") for prefix in forbidden_args
        ):
            raise DeploymentEnvError(f"reviewed Terraform runner forbids argument {argument!r}")

    forbidden_files = [
        path
        for path in terraform_dir.iterdir()
        if (
            path.name
            in {"terraform.tfvars", "terraform.tfvars.json", "override.tf", "override.tf.json"}
            or path.name.endswith(".auto.tfvars")
            or path.name.endswith(".auto.tfvars.json")
            or path.name.endswith("_override.tf")
            or path.name.endswith("_override.tf.json")
        )
    ]
    if forbidden_files:
        names = ", ".join(sorted(path.name for path in forbidden_files))
        raise DeploymentEnvError(f"remove competing Terraform input files: {names}")
    local_state = sorted(terraform_dir.glob("terraform.tfstate*"))
    if local_state:
        names = ", ".join(path.name for path in local_state)
        raise DeploymentEnvError(f"local Terraform state is forbidden: {names}")


def prepare_terraform_command(command: list[str], values: dict[str, str]) -> list[str]:
    """Enforce one reviewed Terraform input path and the mandatory GCS backend."""
    terraform_dir, subcommand = _terraform_command_parts(command)
    if not terraform_dir.is_dir():
        raise DeploymentEnvError(f"Terraform directory does not exist: {terraform_dir}")
    _validate_terraform_options(command, subcommand)
    _reject_competing_terraform_inputs(terraform_dir, command)
    prepared = list(command)
    for index, argument in enumerate(prepared):
        if argument.startswith("-chdir="):
            prepared[index] = f"-chdir={REVIEWED_TERRAFORM_DIR}"
    if subcommand == "init":
        prepared.extend(
            [
                "-reconfigure",
                f"-backend-config=bucket={values['DOC1_TERRAFORM_STATE_BUCKET']}",
                f"-backend-config=prefix={values['DOC1_TERRAFORM_STATE_PREFIX']}",
            ]
        )
        return prepared

    metadata_path = terraform_dir / ".terraform" / "terraform.tfstate"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentEnvError(
            "Terraform backend metadata is missing; run reviewed terraform init first"
        ) from exc
    backend = metadata.get("backend", {})
    if backend.get("type") != "gcs":
        raise DeploymentEnvError("Terraform backend must be GCS; local state is forbidden")
    backend_config = backend.get("config", {})
    if backend_config.get("bucket") != values["DOC1_TERRAFORM_STATE_BUCKET"]:
        raise DeploymentEnvError("cached GCS backend bucket does not match reviewed deployment")
    if backend_config.get("prefix") != values["DOC1_TERRAFORM_STATE_PREFIX"]:
        raise DeploymentEnvError("cached GCS backend prefix does not match reviewed deployment")
    for credential_key in ("access_token", "credentials"):
        if backend_config.get(credential_key):
            raise DeploymentEnvError(
                f"persisted backend credential {credential_key!r} is forbidden"
            )
    workspace_path = terraform_dir / ".terraform" / "environment"
    if workspace_path.exists() and workspace_path.read_text(encoding="utf-8").strip() != "default":
        raise DeploymentEnvError("only the default Terraform workspace is allowed")
    return prepared


def requires_live_verification(command: list[str]) -> bool:
    """Return true for Terraform paths that can create or apply a reviewed plan."""
    _, subcommand = _terraform_command_parts(command)
    return subcommand in {"plan", "apply"}


def verify_secret_versions(values: dict[str, str]) -> None:
    """Hash exact Secret Manager versions in memory and bind them to reviewed digests."""
    project = values["GOOGLE_CLOUD_PROJECT"]
    child_env = sanitized_child_environment(values)
    pairs = (
        (
            values["DOC1_INSTALLATION_MANIFEST_SECRET_ID"],
            values["DOC1_INSTALLATION_MANIFEST_SECRET_VERSION"],
            values["DOC1_INSTALLATION_MANIFEST_SHA256"],
        ),
        (
            values["DOC1_RUNTIME_SETTINGS_SECRET_ID"],
            values["DOC1_RUNTIME_SETTINGS_SECRET_VERSION"],
            values["DOC1_RUNTIME_SETTINGS_SHA256"],
        ),
    )
    for secret_id, version, expected_digest in pairs:
        try:
            result = subprocess.run(
                [
                    "gcloud",
                    "secrets",
                    "versions",
                    "access",
                    version,
                    f"--secret={secret_id}",
                    f"--project={project}",
                ],
                check=True,
                env=child_env,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise DeploymentEnvError(
                f"could not verify Secret Manager version {secret_id}/{version}"
            ) from exc
        actual_digest = hashlib.sha256(result.stdout).hexdigest()
        if actual_digest != expected_digest:
            raise DeploymentEnvError(
                f"Secret Manager version {secret_id}/{version} digest does not match review"
            )


def verify_stack_lifecycle(values: dict[str, str]) -> None:
    """Prove a new audit bucket is absent or bind an existing bucket's locked retention."""
    bucket_id = f"{values['DOC1_NAME_PREFIX']}-agent-worm"
    try:
        result = subprocess.run(
            [
                "gcloud",
                "logging",
                "buckets",
                "describe",
                bucket_id,
                f"--location={values['GCP_REGION']}",
                f"--project={values['GOOGLE_CLOUD_PROJECT']}",
                "--format=json",
            ],
            check=False,
            env=sanitized_child_environment(values),
            capture_output=True,
        )
    except OSError as exc:
        raise DeploymentEnvError("could not verify audit-bucket lifecycle") from exc

    lifecycle = values["DOC1_STACK_LIFECYCLE"]
    if lifecycle == "new":
        if result.returncode == 0:
            raise DeploymentEnvError("new lifecycle rejected: audit bucket already exists")
        stderr = result.stderr.decode("utf-8", errors="replace")
        expected_not_found = (
            "ERROR: (gcloud.logging.buckets.describe) NOT_FOUND: "
            f"Bucket [{bucket_id}] not found in "
            f"[projects/{values['GOOGLE_CLOUD_PROJECT']}/locations/{values['GCP_REGION']}]."
        )
        # A real describe miss has no response payload. Tolerate only transport whitespace;
        # any stdout content may be a second error or an unexpected resource response.
        if (
            result.returncode != 1
            or result.stdout.strip()
            or stderr.rstrip("\r\n") != expected_not_found
        ):
            raise DeploymentEnvError("audit-bucket absence could not be proven")
        return

    if result.returncode != 0:
        raise DeploymentEnvError("existing lifecycle requires a readable audit bucket")
    try:
        bucket = json.loads(result.stdout)
        actual_retention = int(bucket["retentionDays"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DeploymentEnvError(
            "existing audit bucket returned invalid retention metadata"
        ) from exc
    if bucket.get("locked") is not True:
        raise DeploymentEnvError("existing production audit bucket must already be locked")
    reviewed_retention = int(values["DOC1_EXISTING_LOCKED_RETENTION_DAYS"])
    requested_retention = int(values["DOC1_AUDIT_RETENTION_DAYS"])
    if actual_retention != reviewed_retention:
        raise DeploymentEnvError(
            "live audit-bucket retention does not match DOC1_EXISTING_LOCKED_RETENTION_DAYS"
        )
    if requested_retention < actual_retention:
        raise DeploymentEnvError("requested retention cannot reduce the live locked value")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--secrets-file", type=Path, default=DEFAULT_SECRETS)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="validate files without running anything")
    validate.add_argument("--require-ready", action="store_true")
    subparsers.add_parser(
        "verify-secrets",
        help="compare exact Secret Manager versions with reviewed payload digests",
    )
    run = subparsers.add_parser("run", help="validate production readiness and run a command")
    run.add_argument("argv", nargs=argparse.REMAINDER)
    return parser


def posture_disclosures(values: dict[str, str]) -> list[str]:
    """What a reference posture is NOT evidencing.

    A relaxed rule that passes silently is indistinguishable from a rule that was met, which
    is how an evidence pack ends up claiming a control it never exercised. Every relaxation
    the reference posture grants is named here and printed by both `validate` and `run`, so
    the operator reading the preflight sees the gap at the moment it is taken rather than
    discovering it in review.
    """
    if values.get("DOC1_DEPLOYMENT_POSTURE", "") != "reference":
        return []
    disclosures = ["posture=reference: this stack is a demonstration, not institutional evidence"]
    if values.get("DOC1_WORM_LOCK_APPROVED", "").lower() != "true":
        disclosures.append(
            "  - audit retention is applied but NOT locked: routing and coverage are "
            "evidenced, immutability is not. Do not describe this stack as WORM."
        )
    try:
        if int(values.get("DOC1_EDGE_MIN_INSTANCES", "0")) < 2:
            disclosures.append(
                "  - fewer than two replicas: high availability is NOT demonstrated, and a "
                "cold start is expected on the first request after idle."
            )
    except ValueError:
        pass
    return disclosures


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        values = load_environment(args.env_file, args.secrets_file)
        require_ready = args.command != "validate" or getattr(args, "require_ready", False)
        errors = validate_environment(values, require_ready=require_ready)
        if require_ready:
            errors.extend(validate_secret_file_permissions(args.secrets_file))
        if errors:
            raise DeploymentEnvError("\n".join(f"- {error}" for error in errors))
        disclosures = posture_disclosures(values)
        if args.command == "validate":
            state = "production-ready" if args.require_ready else "draft-valid"
            print(f"Doc1 deployment environment: {state}")
            for line in disclosures:
                print(line)
            return 0
        if args.command == "verify-secrets":
            verify_secret_versions(values)
            print("Doc1 Secret Manager versions match reviewed SHA-256 digests")
            return 0
        command = args.argv
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            raise DeploymentEnvError("run requires a command after --")
        for line in disclosures:
            print(line, file=sys.stderr)
        command = prepare_terraform_command(command, values)
        if requires_live_verification(command):
            if values["DOC1_DEPLOYMENT_STAGE"] == "production-edge":
                verify_secret_versions(values)
            verify_stack_lifecycle(values)
        os.execvpe(command[0], command, sanitized_child_environment(values))
    except DeploymentEnvError as exc:
        print(f"deployment environment invalid:\n{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
