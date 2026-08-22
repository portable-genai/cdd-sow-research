from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from cdd_sow_research.embedding.manifest import (
    ManifestValidationError,
    VerifierPolicy,
    load_installation_manifest,
)


def _document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "deployment_manifest_id": "doc1-demo-bank-embed",
        "build_id": "build-123",
        "installations": {
            "inst_demo_bank": {
                "tenant": "demo-bank",
                "parent_origins": ["https://portal.demo-bank.example"],
                "resource_audience": "https://doc1.example/api",
                "scopes": ["cdd.read", "documents.read"],
                "identity_mode": "embedded-grant",
                "issuer_policy_id": "demo-bank-launch",
                "allowed_clients": ["demo-bank-portal-bff"],
                "protocol_versions": ["1"],
                "public_origin": "https://doc1.bank-agent.example",
                "public_mount_path": "/agent",
                "loader_version": "v1",
                "fallback_url": "https://doc1-standalone.example/agent/",
                "presentation_defaults": {
                    "theme": "system",
                    "density": "comfortable",
                },
            }
        },
    }


def _policy(**changes: object) -> VerifierPolicy:
    values: dict[str, object] = {
        "policy_id": "demo-bank-launch",
        "enabled": True,
        "identity_mode": "embedded-grant",
        "credential_type": "subject-access-token",
        "issuer": "https://idp.demo-bank.example",
        "resource_audience": "https://doc1.example/api",
        "tenant": "demo-bank",
        "allowed_clients": ("demo-bank-portal-bff",),
        "permitted_scopes": ("cdd.read", "documents.read"),
    }
    values.update(changes)
    return VerifierPolicy(**values)  # type: ignore[arg-type]


def _write(tmp_path: Path, document: dict[str, object], *, indent: int = 2) -> tuple[Path, bytes]:
    raw = (json.dumps(document, indent=indent, sort_keys=False) + "\n").encode()
    path = tmp_path / "installations.json"
    path.write_bytes(raw)
    return path, raw


def _load(
    path: Path,
    *,
    policies: tuple[VerifierPolicy, ...] | None = None,
    tenant: str = "demo-bank",
    identity_mode: str = "embedded-grant",
):
    return load_installation_manifest(
        path,
        expected_tenant=tenant,
        expected_identity_mode=identity_mode,
        verifier_policies=policies if policies is not None else (_policy(),),
    )


def test_load_preserves_and_hashes_exact_manifest_bytes(tmp_path: Path) -> None:
    path, raw = _write(tmp_path, _document(), indent=4)

    loaded = _load(path)

    assert loaded.raw_bytes == raw
    assert loaded.sha256 == hashlib.sha256(raw).hexdigest()
    assert loaded.manifest.tenant == "demo-bank"
    assert loaded.manifest.identity_mode == "embedded-grant"
    installation = loaded.manifest.resolve("inst_demo_bank")
    assert installation.public_mount_path == "/agent"
    assert installation.parent_origins == ("https://portal.demo-bank.example",)


def test_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "installations.json"
    path.write_text(
        '{"schema_version":1,"schema_version":1,'
        '"deployment_manifest_id":"m","build_id":"b","installations":{}}'
    )

    with pytest.raises(ManifestValidationError, match="duplicate object key"):
        _load(path)


def test_loader_rejects_unknown_fields(tmp_path: Path) -> None:
    document = _document()
    document["unreviewed"] = True
    path, _ = _write(tmp_path, document)

    with pytest.raises(ManifestValidationError, match="unknown fields"):
        _load(path)


def test_loader_rejects_cross_tenant_manifest(tmp_path: Path) -> None:
    document = _document()
    installations = document["installations"]
    assert isinstance(installations, dict)
    second = copy.deepcopy(installations["inst_demo_bank"])
    assert isinstance(second, dict)
    second["tenant"] = "other-bank"
    installations["inst_other_bank"] = second
    path, _ = _write(tmp_path, document)

    with pytest.raises(ManifestValidationError, match="exactly one non-empty tenant"):
        _load(path)


def test_loader_rejects_mixed_or_wrong_identity_mode(tmp_path: Path) -> None:
    path, _ = _write(tmp_path, _document())

    with pytest.raises(ManifestValidationError, match="does not match deployment"):
        _load(path, identity_mode="oauth-access-token")


def test_loader_rejects_multiple_agent_origins_in_one_deployment(tmp_path: Path) -> None:
    document = _document()
    installations = document["installations"]
    assert isinstance(installations, dict)
    second = copy.deepcopy(installations["inst_demo_bank"])
    assert isinstance(second, dict)
    second["public_origin"] = "https://other-agent.example"
    installations["inst_second_portal"] = second
    path, _ = _write(tmp_path, document)

    with pytest.raises(ManifestValidationError, match="one exact public origin"):
        _load(path)


def test_loader_rejects_parent_equal_to_agent_origin(tmp_path: Path) -> None:
    document = _document()
    installation = _installation(document)
    installation["parent_origins"] = ["https://doc1.bank-agent.example"]
    path, _ = _write(tmp_path, document)

    with pytest.raises(ManifestValidationError, match="dedicated agent origin"):
        _load(path)


@pytest.mark.parametrize("installation_id", ["inst.with.dot", "inst:with:colon"])
def test_loader_rejects_installation_ids_outside_browser_contract(
    tmp_path: Path,
    installation_id: str,
) -> None:
    document = _document()
    installations = document["installations"]
    assert isinstance(installations, dict)
    installations[installation_id] = installations.pop("inst_demo_bank")
    path, _ = _write(tmp_path, document)

    with pytest.raises(ManifestValidationError, match="invalid format"):
        _load(path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("parent_origins", ["https://*.example"], "must not contain a wildcard"),
        ("parent_origins", ["null"], "must have a host"),
        ("public_origin", "https://doc1.bank-agent.example/", "without a path"),
        ("public_mount_path", "/apps/doc1", "exactly /agent"),
        (
            "fallback_url",
            "https://doc1-standalone.example/agent/?return_to=https://evil.example",
            "query or fragment",
        ),
    ],
)
def test_loader_rejects_noncanonical_security_fields(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    document = _document()
    _installation(document)[field] = value
    path, _ = _write(tmp_path, document)

    with pytest.raises(ManifestValidationError, match=message):
        _load(path)


@pytest.mark.parametrize(
    ("policies", "message"),
    [
        ((), "must resolve exactly once"),
        ((_policy(), _policy()), "must resolve exactly once"),
        ((_policy(enabled=False),), "is disabled"),
        ((_policy(tenant="other-bank"),), "incompatible tenant"),
        ((_policy(identity_mode="oauth-access-token"),), "incompatible identity mode"),
        ((_policy(credential_type="id-token"),), "incompatible credential type"),
        ((_policy(resource_audience="wrong-audience"),), "incompatible resource audience"),
        ((_policy(allowed_clients=("other-client",)),), "does not allow every"),
        ((_policy(permitted_scopes=("cdd.read",)),), "does not permit every"),
    ],
)
def test_loader_fails_closed_on_verifier_policy_resolution(
    tmp_path: Path,
    policies: tuple[VerifierPolicy, ...],
    message: str,
) -> None:
    path, _ = _write(tmp_path, _document())

    with pytest.raises(ManifestValidationError, match=message):
        _load(path, policies=policies)


def test_example_manifest_is_valid_for_matching_reviewed_policy() -> None:
    path = Path(__file__).parents[2] / "config" / "installations.example.json"
    policy = _policy(
        permitted_scopes=(
            "cdd.read",
            "cdd.write",
            "documents.read",
            "documents.write",
        )
    )

    loaded = _load(path, policies=(policy,))

    assert loaded.manifest.resolve("inst_demo_bank").loader_version == "v1"


def _installation(document: dict[str, object]) -> dict[str, object]:
    installations = document["installations"]
    assert isinstance(installations, dict)
    installation = installations["inst_demo_bank"]
    assert isinstance(installation, dict)
    return installation
