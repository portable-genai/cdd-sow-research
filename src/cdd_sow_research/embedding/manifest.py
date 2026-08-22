"""Strict loader for the shared UI installation manifest.

The Next.js server and FastAPI process consume the same bytes.  This module therefore
retains the raw bytes, hashes those exact bytes, rejects ambiguous JSON such as duplicate
keys, and resolves every installation against reviewed verifier policy supplied by the
deployment configuration.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import SplitResult, urlsplit

_IDENTIFIER: Final = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")
_INSTALLATION_ID: Final = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
_SCOPE: Final = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")
_PROTOCOL_VERSION: Final = re.compile(r"^[1-9][0-9]{0,5}$")
_LOADER_VERSION: Final = re.compile(r"^v[1-9][0-9]{0,5}$")
_SANDBOXED_IDENTITY_MODES: Final = frozenset({"oauth-access-token", "embedded-grant"})
_CREDENTIAL_TYPE_BY_MODE: Final = {
    "oauth-access-token": "access-token",
    "embedded-grant": "subject-access-token",
}


class ManifestValidationError(ValueError):
    """The manifest or its deployment-policy binding is invalid."""


class _DuplicateKeyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PresentationDefaults:
    """Bounded, non-security presentation hints."""

    theme: str = "system"
    density: str = "comfortable"


@dataclass(frozen=True, slots=True)
class Installation:
    """One reviewed host installation on a dedicated agent origin."""

    installation_id: str
    tenant: str
    parent_origins: tuple[str, ...]
    resource_audience: str
    scopes: tuple[str, ...]
    identity_mode: str
    issuer_policy_id: str
    allowed_clients: tuple[str, ...]
    protocol_versions: tuple[str, ...]
    public_origin: str
    public_mount_path: str
    loader_version: str
    fallback_url: str
    presentation_defaults: PresentationDefaults


@dataclass(frozen=True, slots=True)
class InstallationManifest:
    """Validated, one-tenant deployment manifest."""

    schema_version: int
    deployment_manifest_id: str
    build_id: str
    installations: tuple[Installation, ...]

    @property
    def tenant(self) -> str:
        return self.installations[0].tenant

    @property
    def identity_mode(self) -> str:
        return self.installations[0].identity_mode

    def resolve(self, installation_id: str) -> Installation:
        matches = tuple(
            installation
            for installation in self.installations
            if installation.installation_id == installation_id
        )
        if len(matches) != 1:
            raise ManifestValidationError(
                f"installation_id {installation_id!r} does not resolve exactly once"
            )
        return matches[0]


@dataclass(frozen=True, slots=True)
class LoadedInstallationManifest:
    """A manifest together with the exact source bytes and their digest."""

    manifest: InstallationManifest
    raw_bytes: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class VerifierPolicy:
    """Non-secret compatibility projection of a configured verifier policy."""

    policy_id: str
    enabled: bool
    identity_mode: str
    credential_type: str
    issuer: str
    resource_audience: str
    tenant: str
    allowed_clients: tuple[str, ...]
    permitted_scopes: tuple[str, ...]


def load_installation_manifest(
    path: str | Path,
    *,
    expected_tenant: str,
    expected_identity_mode: str,
    verifier_policies: Sequence[VerifierPolicy],
) -> LoadedInstallationManifest:
    """Load, hash, and bind one exact manifest to deployment verifier policy.

    All deployment inputs are required deliberately.  A caller that cannot supply the
    tenant, exact identity mode, and verifier registry cannot safely enable embedding.
    """

    manifest_path = Path(path)
    try:
        raw_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise ManifestValidationError(f"cannot read installation manifest: {exc}") from exc
    loaded = parse_installation_manifest(raw_bytes)
    _validate_deployment_binding(
        loaded.manifest,
        expected_tenant=expected_tenant,
        expected_identity_mode=expected_identity_mode,
        verifier_policies=verifier_policies,
    )
    return loaded


def parse_installation_manifest(raw_bytes: bytes) -> LoadedInstallationManifest:
    """Parse exact JSON bytes and enforce all manifest-internal invariants."""

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestValidationError("installation manifest must be UTF-8") from exc
    try:
        document = json.loads(text, object_pairs_hook=_unique_object)
    except (_DuplicateKeyError, json.JSONDecodeError) as exc:
        raise ManifestValidationError(f"invalid installation manifest JSON: {exc}") from exc
    root = _object(
        document,
        path="$",
        required={"schema_version", "deployment_manifest_id", "build_id", "installations"},
    )
    schema_version = root["schema_version"]
    if type(schema_version) is not int or schema_version != 1:
        raise ManifestValidationError("$.schema_version must be the integer 1")
    deployment_manifest_id = _identifier(root["deployment_manifest_id"], "$.deployment_manifest_id")
    build_id = _identifier(root["build_id"], "$.build_id")
    installations_object = root["installations"]
    if not isinstance(installations_object, dict) or not installations_object:
        raise ManifestValidationError("$.installations must be a non-empty object")

    installations = tuple(
        _parse_installation(installation_id, value)
        for installation_id, value in installations_object.items()
    )
    tenants = {installation.tenant for installation in installations}
    if len(tenants) != 1:
        raise ManifestValidationError("all installations must have exactly one non-empty tenant")
    identity_modes = {installation.identity_mode for installation in installations}
    if len(identity_modes) != 1:
        raise ManifestValidationError("all installations must select one exact identity mode")
    public_origins = {installation.public_origin for installation in installations}
    if len(public_origins) != 1:
        raise ManifestValidationError("all installations must use one exact public origin")

    manifest = InstallationManifest(
        schema_version=schema_version,
        deployment_manifest_id=deployment_manifest_id,
        build_id=build_id,
        installations=installations,
    )
    return LoadedInstallationManifest(
        manifest=manifest,
        raw_bytes=raw_bytes,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )


def _parse_installation(installation_id_value: str, value: Any) -> Installation:
    path = f"$.installations.{installation_id_value}"
    installation_id = _bounded_string(
        installation_id_value,
        f"{path} key",
        pattern=_INSTALLATION_ID,
    )
    data = _object(
        value,
        path=path,
        required={
            "tenant",
            "parent_origins",
            "resource_audience",
            "scopes",
            "identity_mode",
            "issuer_policy_id",
            "allowed_clients",
            "protocol_versions",
            "public_origin",
            "public_mount_path",
            "loader_version",
            "fallback_url",
        },
        optional={"presentation_defaults"},
    )
    tenant = _identifier(data["tenant"], f"{path}.tenant")
    parent_origins = _unique_nonempty_strings(
        data["parent_origins"], f"{path}.parent_origins", transform=_canonical_origin
    )
    public_origin = _canonical_origin(data["public_origin"], f"{path}.public_origin")
    if public_origin in parent_origins:
        raise ManifestValidationError(
            f"{path}.parent_origins must not contain the dedicated agent origin"
        )
    resource_audience = _bounded_string(data["resource_audience"], f"{path}.resource_audience")
    if "*" in resource_audience:
        raise ManifestValidationError(f"{path}.resource_audience must not contain a wildcard")
    scopes = _unique_nonempty_strings(data["scopes"], f"{path}.scopes", pattern=_SCOPE)
    identity_mode = _bounded_string(data["identity_mode"], f"{path}.identity_mode")
    if identity_mode not in _SANDBOXED_IDENTITY_MODES:
        raise ManifestValidationError(
            f"{path}.identity_mode must be oauth-access-token or embedded-grant"
        )
    issuer_policy_id = _identifier(data["issuer_policy_id"], f"{path}.issuer_policy_id")
    allowed_clients = _unique_nonempty_strings(
        data["allowed_clients"], f"{path}.allowed_clients", pattern=_IDENTIFIER
    )
    protocol_versions = _unique_nonempty_strings(
        data["protocol_versions"], f"{path}.protocol_versions", pattern=_PROTOCOL_VERSION
    )
    public_mount_path = data["public_mount_path"]
    if public_mount_path != "/agent":
        raise ManifestValidationError(f"{path}.public_mount_path must be exactly /agent")
    loader_version = _bounded_string(data["loader_version"], f"{path}.loader_version")
    if _LOADER_VERSION.fullmatch(loader_version) is None:
        raise ManifestValidationError(f"{path}.loader_version is invalid")
    fallback_url = _fallback_url(data["fallback_url"], f"{path}.fallback_url")
    presentation = _parse_presentation(data.get("presentation_defaults"), path)

    return Installation(
        installation_id=installation_id,
        tenant=tenant,
        parent_origins=parent_origins,
        resource_audience=resource_audience,
        scopes=scopes,
        identity_mode=identity_mode,
        issuer_policy_id=issuer_policy_id,
        allowed_clients=allowed_clients,
        protocol_versions=protocol_versions,
        public_origin=public_origin,
        public_mount_path=public_mount_path,
        loader_version=loader_version,
        fallback_url=fallback_url,
        presentation_defaults=presentation,
    )


def _validate_deployment_binding(
    manifest: InstallationManifest,
    *,
    expected_tenant: str,
    expected_identity_mode: str,
    verifier_policies: Sequence[VerifierPolicy],
) -> None:
    expected_tenant = _identifier(expected_tenant, "configured deployment tenant")
    if manifest.tenant != expected_tenant:
        raise ManifestValidationError(
            f"manifest tenant {manifest.tenant!r} does not match deployment tenant"
        )
    if expected_identity_mode not in _SANDBOXED_IDENTITY_MODES:
        raise ManifestValidationError("configured identity mode is not valid for sandboxed channel")
    if manifest.identity_mode != expected_identity_mode:
        raise ManifestValidationError(
            f"manifest identity mode {manifest.identity_mode!r} does not match deployment"
        )
    for installation in manifest.installations:
        matches = tuple(
            policy
            for policy in verifier_policies
            if policy.policy_id == installation.issuer_policy_id
        )
        if len(matches) != 1:
            raise ManifestValidationError(
                f"issuer policy {installation.issuer_policy_id!r} must resolve exactly once"
            )
        _validate_policy(installation, matches[0])


def _validate_policy(installation: Installation, policy: VerifierPolicy) -> None:
    path = f"verifier policy {policy.policy_id!r}"
    _identifier(policy.policy_id, f"{path}.policy_id")
    if not policy.enabled:
        raise ManifestValidationError(f"{path} is disabled")
    if policy.identity_mode != installation.identity_mode:
        raise ManifestValidationError(f"{path} has an incompatible identity mode")
    expected_credential = _CREDENTIAL_TYPE_BY_MODE[installation.identity_mode]
    if policy.credential_type != expected_credential:
        raise ManifestValidationError(f"{path} has an incompatible credential type")
    _canonical_https_url(policy.issuer, f"{path}.issuer", origin_only=False)
    if policy.tenant != installation.tenant:
        raise ManifestValidationError(f"{path} has an incompatible tenant")
    if policy.resource_audience != installation.resource_audience:
        raise ManifestValidationError(f"{path} has an incompatible resource audience")
    policy_clients = set(
        _unique_nonempty_strings(
            policy.allowed_clients, f"{path}.allowed_clients", pattern=_IDENTIFIER
        )
    )
    if not set(installation.allowed_clients).issubset(policy_clients):
        raise ManifestValidationError(f"{path} does not allow every installation client")
    policy_scopes = set(
        _unique_nonempty_strings(
            policy.permitted_scopes, f"{path}.permitted_scopes", pattern=_SCOPE
        )
    )
    if not set(installation.scopes).issubset(policy_scopes):
        raise ManifestValidationError(f"{path} does not permit every installation scope")


def _parse_presentation(value: Any, installation_path: str) -> PresentationDefaults:
    if value is None:
        return PresentationDefaults()
    path = f"{installation_path}.presentation_defaults"
    data = _object(value, path=path, required=set(), optional={"theme", "density"})
    theme = data.get("theme", "system")
    density = data.get("density", "comfortable")
    if theme not in {"light", "dark", "system"}:
        raise ManifestValidationError(f"{path}.theme is invalid")
    if density not in {"compact", "comfortable"}:
        raise ManifestValidationError(f"{path}.density is invalid")
    return PresentationDefaults(theme=theme, density=density)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _object(
    value: Any,
    *,
    path: str,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestValidationError(f"{path} must be an object")
    optional = optional or set()
    keys = set(value)
    missing = required - keys
    extras = keys - required - optional
    if missing:
        raise ManifestValidationError(f"{path} is missing fields: {sorted(missing)}")
    if extras:
        raise ManifestValidationError(f"{path} has unknown fields: {sorted(extras)}")
    return value


def _identifier(value: Any, path: str) -> str:
    return _bounded_string(value, path, pattern=_IDENTIFIER)


def _bounded_string(value: Any, path: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ManifestValidationError(f"{path} must be a non-empty bounded string")
    if any(ord(character) < 0x20 for character in value):
        raise ManifestValidationError(f"{path} must not contain control characters")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ManifestValidationError(f"{path} has an invalid format")
    return value


def _unique_nonempty_strings(
    value: Any,
    path: str,
    *,
    pattern: re.Pattern[str] | None = None,
    transform: Any = None,
) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not value:
        raise ManifestValidationError(f"{path} must be a non-empty array")
    values: list[str] = []
    for index, item in enumerate(value):
        current_path = f"{path}[{index}]"
        parsed = (
            transform(item, current_path)
            if transform is not None
            else _bounded_string(item, current_path, pattern=pattern)
        )
        values.append(parsed)
    if len(set(values)) != len(values):
        raise ManifestValidationError(f"{path} must not contain duplicates")
    return tuple(values)


def _canonical_origin(value: Any, path: str) -> str:
    return _canonical_https_url(value, path, origin_only=True)


def _canonical_https_url(value: Any, path: str, *, origin_only: bool) -> str:
    text = _bounded_string(value, path)
    if "*" in text:
        raise ManifestValidationError(f"{path} must not contain a wildcard")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise ManifestValidationError(f"{path} is not a valid URL") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ManifestValidationError(f"{path} must not contain user information")
    if not parsed.hostname:
        raise ManifestValidationError(f"{path} must have a host")
    if parsed.query or parsed.fragment:
        raise ManifestValidationError(f"{path} must not have a query or fragment")
    if origin_only and parsed.path:
        raise ManifestValidationError(f"{path} must be an origin without a path")
    scheme = parsed.scheme.lower()
    if scheme != "https" and not (scheme == "http" and _is_loopback(parsed.hostname)):
        raise ManifestValidationError(f"{path} must use HTTPS except on loopback")
    canonical = _rebuild_origin(parsed, port)
    if origin_only and text != canonical:
        raise ManifestValidationError(f"{path} must be the canonical origin {canonical!r}")
    return canonical if origin_only else text


def _fallback_url(value: Any, path: str) -> str:
    text = _bounded_string(value, path)
    _canonical_https_url(text, path, origin_only=False)
    parsed = urlsplit(text)
    if not parsed.path.startswith("/agent/"):
        raise ManifestValidationError(f"{path} must target the canonical /agent/ surface")
    if "\\" in parsed.path:
        raise ManifestValidationError(f"{path} contains an invalid path")
    canonical_origin = _rebuild_origin(parsed, parsed.port)
    if not text.startswith(f"{canonical_origin}/"):
        raise ManifestValidationError(f"{path} must use a canonical authority")
    return text


def _rebuild_origin(parsed: SplitResult, port: int | None) -> str:
    assert parsed.hostname is not None
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = (parsed.scheme.lower() == "https" and port == 443) or (
        parsed.scheme.lower() == "http" and port == 80
    )
    port_suffix = "" if port is None or default_port else f":{port}"
    return f"{parsed.scheme.lower()}://{host}{port_suffix}"


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
