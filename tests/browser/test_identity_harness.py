"""Focused production-chain checks for the browser identity evidence harness.

Guarded by ``pytest.importorskip("jwt")``: ``tests.browser.identity_harness`` mints real
tokens with PyJWT, so it needs the optional ``oidc`` extra. The SDK-free ``[dev]``-only
gate skips this module while the ``oidc`` CI job runs it.
"""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("jwt")

from tests.browser.identity_harness import (  # noqa: E402
    MODE5_INSTALLATION,
    IdentityHarness,
)

from cdd_sow_research.domain.browser_flow import pkce_s256  # noqa: E402

PKCE_VERIFIER = "A" * 43


def _mode4_headers(token, *, origin: str = "http://127.0.0.1:3200") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token.access_token}",
        "X-CDD-Installation-ID": token.installation_id,
        "Origin": origin,
    }


def test_harness_exposes_canonical_edge_targets_and_secret_safe_token_repr() -> None:
    harness = IdentityHarness(mode5_agent_origin="http://127.0.0.1:3210")
    try:
        token = harness.mint_mode4_token()

        assert harness.api_targets == {
            "inst_host_a": harness.api_origin,
            "inst_host_b": harness.api_origin,
            "inst_mode5": harness.api_origin,
        }
        assert harness.mode5_agent_origin == "http://127.0.0.1:3210"
        assert token.access_token not in repr(token)
        assert harness.hook_environment == {
            "CDD_MODE4_EVIDENCE_URL": harness.mode4_evidence_url,
            "CDD_MODE5_EVIDENCE_URL": harness.mode5_evidence_url,
        }
    finally:
        harness.close()


def test_mode4_real_adapter_covers_transports_rotation_and_policy_failures() -> None:
    with IdentityHarness() as harness:
        rsa_token = harness.mint_mode4_token("valid")
        json_response = httpx.post(
            f"{harness.api_origin}/v1/harness/mode4/protected/json",
            headers=_mode4_headers(rsa_token),
            json={"type": "browser-json"},
        )
        form_response = httpx.post(
            f"{harness.api_origin}/v1/harness/mode4/protected/form",
            headers=_mode4_headers(rsa_token),
            files={"note": (None, "reviewed browser form")},
        )
        blob_response = httpx.post(
            f"{harness.api_origin}/v1/harness/mode4/protected/blob",
            headers=_mode4_headers(rsa_token),
            content=b"synthetic-browser-blob",
        )
        refreshed = harness.mint_mode4_token("refresh")
        refresh_response = httpx.post(
            f"{harness.api_origin}/v1/harness/mode4/protected/json",
            headers=_mode4_headers(refreshed),
            json={"type": "rotated-rsa"},
        )
        ec_token = harness.mint_mode4_token("ec")
        ec_response = httpx.post(
            f"{harness.api_origin}/v1/harness/mode4/protected/json",
            headers=_mode4_headers(ec_token),
            json={"type": "ec-issuer"},
        )

        failures: dict[str, int] = {}
        for variant in ("cross-tenant", "cross-installation", "wrong-type"):
            token = harness.mint_mode4_token(variant)
            response = httpx.post(
                f"{harness.api_origin}/v1/harness/mode4/protected/json",
                headers=_mode4_headers(token),
                json={"type": variant},
            )
            failures[variant] = response.status_code
            assert token.access_token not in response.text
        wrong_origin = httpx.post(
            f"{harness.api_origin}/v1/harness/mode4/protected/json",
            headers=_mode4_headers(
                harness.mint_mode4_token(),
                origin="http://127.0.0.1:4999",
            ),
            json={"type": "wrong-origin"},
        )

        assert json_response.status_code == 200
        assert json_response.json()["transport"] == "json"
        assert form_response.status_code == 200
        assert form_response.json()["transport"] == "form-data"
        assert blob_response.status_code == 200
        assert blob_response.headers["content-type"] == "application/pdf"
        assert blob_response.content.startswith(b"%PDF-1.4")
        assert refresh_response.status_code == 200
        assert ec_response.status_code == 200
        assert failures == {
            "cross-tenant": 401,
            "cross-installation": 401,
            "wrong-type": 401,
        }
        assert wrong_origin.status_code == 401

        harness.record_mode4_browser_evidence(
            json_call=True,
            form_data_call=True,
            blob_call=True,
            rsa_issuer=True,
            ec_issuer=True,
            rotation_refresh=True,
            cross_tenant_rejected=True,
            cross_installation_rejected=True,
            wrong_origin_rejected=True,
            wrong_type_rejected=True,
            credential_absent_from_dom=True,
        )
        evidence = harness.mode4_evidence()

        assert evidence["status"] == "ready"
        server = evidence["server"]
        assert server["rsa_jwks_fetches"] >= 2
        assert server["ec_jwks_fetches"] >= 1
        assert set(server["verified_kids"]) == {
            "rsa-old",
            "rsa-rotated",
            "ec-active",
        }
        assert set(server["negative_boundaries"]) == {
            "cross_installation",
            "cross_tenant",
            "wrong_origin",
            "wrong_type",
        }
        boundary_digests = harness.boundary_secret_digests()
        assert len(boundary_digests["mode4_credential"]) >= 6
        assert rsa_token.access_token not in str(boundary_digests)
        assert refreshed.access_token not in str(boundary_digests)
        assert httpx.get(harness.mode4_evidence_url).json()["status"] == "ready"


def test_mode5_real_broker_is_iframe_pkce_bound_and_sqlite_secret_safe() -> None:
    with IdentityHarness() as harness:
        registered = harness.register_mode5(pkce_s256(PKCE_VERIFIER))
        assert registered["state"] == "REGISTERED"

        browser_headers = {
            "Origin": harness.host_origin,
            "Sec-Fetch-Site": "same-origin",
        }
        sibling_origin = httpx.post(
            f"{harness.api_origin}/v1/harness/bff/session",
            headers={
                "Origin": "http://127.0.0.1:4102",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        with httpx.Client(base_url=harness.api_origin) as analyst_browser:
            session_response = analyst_browser.post(
                "/v1/harness/bff/session",
                headers=browser_headers,
            )
            assert session_response.status_code == 200
            assert set(session_response.json()) == {
                "status",
                "csrf_token",
                "expires_in",
            }
            csrf_token = session_response.json()["csrf_token"]
            intent_body = {
                "installation_id": MODE5_INSTALLATION,
                "instance_id": registered["instance_id"],
                "action": "authorize-embed",
            }
            missing_csrf = analyst_browser.post(
                "/v1/harness/bff/intents",
                headers=browser_headers,
                json=intent_body,
            )
            wrong_csrf = analyst_browser.post(
                "/v1/harness/bff/intents",
                headers={**browser_headers, "X-CSRF-Token": "wrong-token"},
                json=intent_body,
            )
            intent_response = analyst_browser.post(
                "/v1/harness/bff/intents",
                headers={**browser_headers, "X-CSRF-Token": csrf_token},
                json=intent_body,
            )
            assert intent_response.status_code == 200
            intent_id = intent_response.json()["user_intent_id"]
            instance_mismatch = analyst_browser.post(
                "/v1/harness/bff/authorize",
                headers={**browser_headers, "X-CSRF-Token": csrf_token},
                json={
                    "instance_id": "wrong-instance-id-with-safe-length",
                    "user_intent_id": intent_id,
                },
            )

            with httpx.Client(base_url=harness.api_origin) as auditor_browser:
                auditor_session = auditor_browser.post(
                    "/v1/harness/bff/session?persona=auditor",
                    headers=browser_headers,
                )
                auditor_csrf = auditor_session.json()["csrf_token"]
                subject_mismatch = auditor_browser.post(
                    "/v1/harness/bff/authorize",
                    headers={**browser_headers, "X-CSRF-Token": auditor_csrf},
                    json={
                        "instance_id": registered["instance_id"],
                        "user_intent_id": intent_id,
                    },
                )

            authorize_response = analyst_browser.post(
                "/v1/harness/bff/authorize",
                headers={**browser_headers, "X-CSRF-Token": csrf_token},
                json={
                    "instance_id": registered["instance_id"],
                    "user_intent_id": intent_id,
                },
            )
            duplicate_authorization = analyst_browser.post(
                "/v1/harness/bff/authorize",
                headers={**browser_headers, "X-CSRF-Token": csrf_token},
                json={
                    "instance_id": registered["instance_id"],
                    "user_intent_id": intent_id,
                },
            )

        assert sibling_origin.status_code == 403
        assert missing_csrf.status_code == 403
        assert wrong_csrf.status_code == 403
        assert instance_mismatch.status_code == 403
        assert subject_mismatch.status_code == 403
        assert authorize_response.status_code == 200
        assert set(authorize_response.json()) == {"launch_code"}
        assert duplicate_authorization.status_code == 409
        launch_code = authorize_response.json()["launch_code"]
        wrong_verifier = harness.redeem_mode5(
            str(registered["instance_id"]),
            launch_code,
            "B" * 43,
        )
        redeemed = harness.redeem_mode5(
            str(registered["instance_id"]),
            launch_code,
            PKCE_VERIFIER,
        )
        assert redeemed.status_code == 200
        doc1_token = redeemed.json()["access_token"]
        protected = httpx.get(
            f"{harness.api_origin}/v1/cases/CASE-SYNTHETIC-001/documents",
            headers={
                "Authorization": f"Bearer {doc1_token}",
                "X-CDD-Installation-ID": MODE5_INSTALLATION,
            },
        )
        replay = harness.redeem_mode5(
            str(registered["instance_id"]),
            launch_code,
            PKCE_VERIFIER,
        )

        assert wrong_verifier.status_code == 401
        assert protected.status_code == 200
        assert protected.json() == {"documents": []}
        assert doc1_token not in protected.text
        assert replay.status_code == 409
        assert harness.sqlite_secret_scan() == {
            "safe": True,
            "checked_databases": [
                "browser-flow.sqlite3",
                "bff-replay.sqlite3",
            ],
            "leaks": [],
            "unsafe_secret_columns": [],
        }

        harness.record_mode5_browser_evidence(
            iframe_registered_first=True,
            protected_call=True,
            wrong_verifier_rejected=True,
            launch_code_replay_rejected=True,
            sibling_origin_rejected=True,
            missing_csrf_rejected=True,
            wrong_csrf_rejected=True,
            subject_session_mismatch_rejected=True,
            instance_mismatch_rejected=True,
            duplicate_authorization_rejected=True,
            host_never_received_subject_token=True,
            host_never_received_pkce_verifier=True,
            host_never_received_doc1_token=True,
            credential_absent_from_dom=True,
        )
        evidence = harness.mode5_evidence()

        assert evidence["status"] == "ready"
        assert evidence["server"]["outbox_state_counts"] == {
            "CODE_ISSUED": 1,
            "CONSUMED": 1,
            "REGISTERED": 1,
        }
        assert set(evidence["server"]["bff_negative_boundaries"]) == {
            "duplicate_authorization",
            "instance_mismatch",
            "missing_csrf",
            "private_key_jwt_replay",
            "sibling_origin",
            "subject_session_mismatch",
            "wrong_csrf",
        }
        assert evidence["server"]["sqlite_secret_scan"]["safe"] is True
        boundary_digests = harness.boundary_secret_digests()
        assert {
            "mode5_bff_assertion",
            "mode5_doc1_token",
            "mode5_pkce_verifier",
            "mode5_subject_credential",
        } <= boundary_digests.keys()
        assert all(boundary_digests[category] for category in boundary_digests)
        assert doc1_token not in str(boundary_digests)
        assert PKCE_VERIFIER not in str(boundary_digests)
        assert httpx.get(harness.mode5_evidence_url).json()["status"] == "ready"
