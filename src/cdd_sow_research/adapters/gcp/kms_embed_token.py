"""Cloud KMS asymmetric signer for dedicated Mode 5 resource tokens."""

from __future__ import annotations

import hashlib
from typing import Any

from ...config import Settings
from ...domain.identity import IdentityError
from ..oidc.embed_token import EmbedTokenKey


class KmsEmbedTokenSigner:
    """Sign compact JWT input without exporting private key material."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._api_endpoint = f"{settings.region}-cloudkms.googleapis.com"
        self._client: Any | None = None

    def _kms(self) -> Any:
        if self._client is None:
            from google.api_core.client_options import ClientOptions
            from google.cloud import kms_v1

            self._client = kms_v1.KeyManagementServiceClient(
                client_options=ClientOptions(api_endpoint=self._api_endpoint)
            )
        return self._client

    def __call__(self, key: EmbedTokenKey, signing_input: bytes) -> bytes:
        if not key.kms_key_version:
            raise IdentityError("managed signer requires a KMS key version")
        digest = hashlib.sha256(signing_input).digest()
        try:
            response = self._kms().asymmetric_sign(
                request={
                    "name": key.kms_key_version,
                    "digest": {"sha256": digest},
                }
            )
            signature = bytes(response.signature)
            if key.algorithm == "ES256":
                signature = _ecdsa_der_to_jose(signature)
            return signature
        except IdentityError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise IdentityError("Cloud KMS embedded-token signing failed") from exc


def _ecdsa_der_to_jose(signature: bytes) -> bytes:
    """Convert KMS DER output into JWT's fixed-width R||S representation."""
    try:
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        r, s = decode_dss_signature(signature)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")
    except Exception as exc:  # noqa: BLE001
        raise IdentityError("Cloud KMS returned an invalid ES256 signature") from exc
