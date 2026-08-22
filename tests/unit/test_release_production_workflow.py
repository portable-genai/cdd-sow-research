from pathlib import Path

WORKFLOW = Path(".github/workflows/release-production-images.yaml")


def test_release_tag_is_created_only_after_exact_digest_scan() -> None:
    workflow = WORKFLOW.read_text()

    scan = workflow.index("aquasecurity/trivy-action")
    sign = workflow.index("Keyless-sign exact digest")
    promote = workflow.index("Promote scanned digest to release tag")

    assert "-quarantine-${{ github.run_id }}" in workflow
    assert scan < sign < promote
    assert 'imagetools create --tag "${IMAGE}:${VERSION}" "${IMAGE}@${DIGEST}"' in workflow


def test_managed_dependency_smoke_constructs_both_clients() -> None:
    workflow = Path(".github/workflows/ci.yaml").read_text()

    assert "pip install -r requirements-gcp.lock" in workflow
    assert 'firestore.Client(project="dependency-smoke", credentials=credentials)' in workflow
    assert "kms_v1.KeyManagementServiceClient(credentials=credentials)" in workflow


def test_registry_login_uses_host_not_repository_prefix() -> None:
    workflow = WORKFLOW.read_text()

    assert 'host="${REGISTRY_PREFIX%%/*}"' in workflow
    assert "registry: ${{ steps.registry.outputs.host }}" in workflow


def test_local_promotion_output_is_not_mislabeled_as_registry_digest() -> None:
    script = Path("scripts/promote_production_images.sh").read_text()

    assert "api_image: process.env.API_DIGEST || null" in script
    assert "api_local_image_id: process.env.API_LOCAL_ID" in script
    assert "trivy image" in script
    assert script.index("trivy image") < script.index('docker push "$api_tag"')
    assert "Production push requires --sign" in script
    assert script.rindex("trivy image") < script.index("imagetools create")


def test_public_api_attaches_per_source_cloud_armor_policy() -> None:
    terraform = Path("infra/terraform/production_edge.tf").read_text()

    assert (
        "security_policy       = google_compute_security_policy.api_per_source[0].id" in terraform
    )
    assert 'enforce_on_key = "IP"' in terraform
    assert 'exceed_action  = "deny(429)"' in terraform
