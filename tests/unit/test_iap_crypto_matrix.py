"""The IAP negative matrix against a locally minted key: the REAL verifier, offline, no live GCP.

This service is deployed as an embedded application behind the journey portal's IAP edge, which
makes it one of the few in the fleet that receives a forwarded IAP assertion in production.
``test_portal_assertion_header.py``, ``test_iap_claim_half.py``, ``test_iap_refusal_split.py`` and
``test_iap_two_implementations.py`` prove the adapters' own halves, but each either replaces
``_verify`` with a stub or blocks the google-auth import, and the verifier is the thing under
suspicion: a suite that never runs it cannot show that the arguments passed make ``google-auth``
REFUSE a wrong audience, an expired token or a signature from the wrong key.

**Two implementations, both run.** Under ``CDD_PROFILE=gcp`` the identity mode is ``iap`` and
``api/security.py::get_authentication_port`` answers every request with
``IapAuthenticationAdapter`` -- the REQUEST PATH, which is what actually decides who a caller is.
``adapters/gcp/iap_identity.py::IapIdentityAdapter`` is the BOUND adapter: its ``resolve`` is not
on that path, but its ``end_user_auth = VERIFIED`` declaration is what stands the exposure guard
down, so it has to be exactly as strict. Every cell runs against both, reached the way the
service reaches them, and where the two refuse for different stated reasons each cell says which.

So this mints its own P-256 key, signs its own ES256 assertions with it (ES256 is what IAP signs
with), serves the public half from an in-process transport in the ``{kid: PEM}`` shape IAP's key
endpoint publishes, and runs the REAL ``google.oauth2.id_token.verify_token``. The only thing
faked is the HTTP fetch of the key set, and the fake ASSERTS the URL requested is IAP's.

    cell                                   refused by
    -------------------------------------  -----------------------------------------------
    a correct assertion                    NOT refused: the control
    wrong or absent audience               google-auth, because audience= is passed
    expired, not yet valid, no exp/iat     google-auth
    impostor key (own kid, IAP's kid,      google-auth: unknown key id, or a signature
      no kid) and a tampered payload         that does not verify against IAP's key set
    malformed, alg none, HS256 confusion   the bound adapter's algorithm pin; google-auth on
                                             the request path, which carries no pin
    wrong or absent issuer, no sub         the shared claim policy, after a VALID signature
    no email                               the bound adapter; the request path admits it,
                                             because its actor is the (iss, sub) pair
    unmapped domain, unmapped machine      the shared claim policy: no reviewed tenant
    absent, empty or blank header          both, before anything is fetched
    unconfigured or emptied audience       both, before anything is fetched

**Both transports, identically.** ``x-goog-*`` is Google's reserved namespace and the serverless
frontend strips it from a request entering this service, so the portal's broker forwards the
same assertion as ``x-portal-iap-assertion`` too. Every refusal above runs under BOTH names: a
header that bought a weaker check would be a second trust path, which is the defect this proves
absent. Precedence is covered as well: the edge-injected name wins when both are present, and a
bad assertion under it is refused rather than rescued by the other name.

**Machine callers.** A service account has no ``hd``, so the reviewed domain map is keyed on its
email domain, and naming that domain is how this deployment admits one
(``identity_policy._domain_for``). An unmapped machine resolves no tenant and is REFUSED, like
any other caller the reviewed map does not name.

WHY THIS MODULE MAY NOT SILENTLY SKIP. google-auth is in ``requirements-gcp.lock`` and not in the
dev locks, so the SDK-free ``make check`` cannot import it and skips this module. Where the
runtime lockfile IS installed, the hosted gate's ``offline-gate`` job names this file as its
``iap_matrix_path``, sets ``CDD_REQUIRE_IAP_MATRIX=1`` (the prefix comes from the
``CDD_PROFILE=`` line in ``.env.example``) and fails if the run reports a skip. Under that flag a
missing google-auth is a hard ERROR, not a skip.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from cdd_sow_research.adapters.gcp import iap_identity
from cdd_sow_research.adapters.gcp.iap_identity import (
    IapAudienceUnconfiguredError,
    IapIdentityAdapter,
)
from cdd_sow_research.api import security
from cdd_sow_research.config import IdentitySettings, Settings
from cdd_sow_research.domain.identity import IdentityError, Principal, RequestContext
from cdd_sow_research.envread import ConfiguredEmptyError
from cdd_sow_research.identity_policy import canonical_actor, reviewed_principal_from_iap_claims

#: Set to "1" wherever google-auth IS installed, so absence becomes an error rather than a skip.
#: An exact-match read: unset, emptied and "0" all mean "not required", which fails closed for a
#: developer running the SDK-free gate and fails LOUD in the gate that installs the runtime lock.
_REQUIRE_ENV = "CDD_REQUIRE_IAP_MATRIX"
_REQUIRED = os.environ.get(_REQUIRE_ENV) == "1"

try:  # noqa: SIM105 - the else branch is a skip, not a pass
    from google.auth import crypt as ga_crypt
    from google.auth import jwt as ga_jwt
    from google.auth.transport import requests as ga_requests
except ImportError as exc:  # pragma: no cover - exercised by whichever gate lacks the extra
    if _REQUIRED:
        raise RuntimeError(
            f"{_REQUIRE_ENV}=1 but google-auth is not importable, so the IAP negative matrix "
            "would have SKIPPED in a gate that exists to run it. Install the runtime lockfile "
            "(pip install -r requirements-gcp.lock) or stop setting the flag."
        ) from exc
    pytest.skip(
        f"google-auth is not installed (the SDK-free gate). Set {_REQUIRE_ENV}=1 where the "
        "runtime lockfile is installed to make this a hard failure instead.",
        allow_module_level=True,
    )

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
except ImportError as exc:  # pragma: no cover - cryptography ships with the runtime lockfile
    if _REQUIRED:
        raise RuntimeError(
            f"{_REQUIRE_ENV}=1 but `cryptography` is not importable, so no key could be minted."
        ) from exc
    pytest.skip("cryptography is not installed", allow_module_level=True)


# The transport contract, written as LITERALS rather than imported from the adapters. The portal's
# broker and IAP itself send these exact strings; a rename on this side would be a deployment that
# silently stopped reading its identity, and importing the adapters' own constants would agree with
# the rename instead of catching it.
EDGE_HEADER = "x-goog-iap-jwt-assertion"
FORWARDED_HEADER = "x-portal-iap-assertion"
IAP_ISSUER = "https://cloud.google.com/iap"
IAP_KEYS_URL = "https://www.gstatic.com/iap/verify/public_key"
BOTH_HEADERS = pytest.mark.parametrize(
    "header", [EDGE_HEADER, FORWARDED_HEADER], ids=["edge-injected", "host-forwarded"]
)

#: The deployment's audience variable, by the name the portal's Terraform injects.
AUDIENCE_ENV = "CDD_IAP_AUDIENCE"
#: The IAP-protected resource this deployment verifies against. Obviously fictional.
AUDIENCE = "/projects/000000000000/global/backendServices/1111111111111111111"
#: A DIFFERENT protected resource: the audience an assertion minted for another service carries.
OTHER_AUDIENCE = "/projects/999999999999/global/backendServices/2222222222222222222"

SIGNING_KID = "iap-signing-key"
IMPOSTOR_KID = "impostor-key"

HUMAN = "analyst@bank.example"
HUMAN_SUB = "accounts.google.com:100000000000000000001"
MACHINE = "e2e-caller@fictional-project-000000.iam.gserviceaccount.com"
MACHINE_DOMAIN = "fictional-project-000000.iam.gserviceaccount.com"
MACHINE_SUB = "accounts.google.com:100000000000000000002"

REQUEST_PATH = "request-path"
BOUND_ADAPTER = "bound-adapter"


def test_the_literals_are_the_values_both_implementations_read() -> None:
    """The literals above are only a contract if the adapters agree with them today."""
    for module in (iap_identity, security):
        assert module._IAP_ISSUER == IAP_ISSUER
        assert module._IAP_KEYS_URL == IAP_KEYS_URL
    assert iap_identity._ASSERTION_HEADER == EDGE_HEADER
    assert security._IAP_ASSERTION_HEADER == EDGE_HEADER
    assert security._PORTAL_ASSERTION_HEADER == FORWARDED_HEADER


# --------------------------------------------------------------------------------------- #
# The harness: a minted key, a key set served in-process, and assertions signed like IAP's.
# --------------------------------------------------------------------------------------- #
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _mint_key() -> tuple[Any, str]:
    """A fresh P-256 keypair: the private key and its public half as PEM.

    Generated per module run. Nothing here is a secret and nothing is committed: a key on disk
    in a test is a key in every repository that copies the test.
    """
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, pem.decode("ascii")


def _signer(private_key: Any, kid: str | None) -> Any:
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return ga_crypt.ES256Signer.from_string(pem, kid)


@pytest.fixture(scope="module")
def keys() -> dict[str, Any]:
    """IAP's signing key, and an impostor key that IAP's key set does not contain."""
    iap_private, iap_public_pem = _mint_key()
    impostor_private, _ = _mint_key()
    return {
        "iap": _signer(iap_private, SIGNING_KID),
        "impostor": _signer(impostor_private, IMPOSTOR_KID),
        # The forgeries that matter: the impostor claiming IAP's own key id, and claiming none.
        "impostor-as-iap": _signer(impostor_private, SIGNING_KID),
        "impostor-no-kid": _signer(impostor_private, None),
        "iap_public_pem": iap_public_pem,
        # The key set the fake transport serves: IAP's key only, in IAP's {kid: PEM} shape.
        "certs": {SIGNING_KID: iap_public_pem},
    }


class _CertsResponse:
    """What ``google.auth.transport.Request.__call__`` returns: a status and a body."""

    def __init__(self, payload: dict[str, str]) -> None:
        self.status = 200
        self.data = json.dumps(payload).encode("utf-8")
        self.headers: dict[str, str] = {}


@pytest.fixture()
def fetched(monkeypatch: pytest.MonkeyPatch, keys: dict[str, Any]) -> list[str]:
    """Serve the minted key set in-process, and record every URL the verifier asked for."""
    requested: list[str] = []

    class _Request:
        def __call__(self, url: str, method: str = "GET", **kwargs: Any) -> _CertsResponse:
            requested.append(url)
            assert url == IAP_KEYS_URL, (
                f"the verifier fetched {url!r}, not IAP's key set. google-auth's default is the "
                "OAuth2 federated set, which signs tokens IAP never issued."
            )
            return _CertsResponse(keys["certs"])

    monkeypatch.setattr(ga_requests, "Request", _Request)
    return requested


@pytest.fixture(autouse=True)
def _deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployment as the portal's stack configures it, and nothing inherited from a shell."""
    monkeypatch.setenv(AUDIENCE_ENV, AUDIENCE)


def _settings(
    tenants: dict[str, str] | None = None, groups: dict[str, tuple[str, ...]] | None = None
) -> Settings:
    """``CDD_PROFILE=gcp`` with the reviewed maps; the identity mode it implies is ``iap``."""
    return Settings(
        profile="gcp",
        identity=IdentitySettings(
            iap_tenant_by_domain=dict(tenants or {}), iap_groups_by_domain=dict(groups or {})
        ),
    )


class _Implementation:
    """One of the two verifiers, reached the way the service reaches it."""

    def __init__(self, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self.name = name
        self._monkeypatch = monkeypatch

    def build(self, settings: Settings) -> Any:
        if self.name == BOUND_ADAPTER:
            return IapIdentityAdapter(settings)
        container = SimpleNamespace(settings=settings)
        self._monkeypatch.setattr(security.deps, "get_container", lambda: container)
        port = security.get_authentication_port()
        assert isinstance(port, security.IapAuthenticationAdapter), (
            f"the iap identity mode dispatched to {type(port).__name__}, not the request-path "
            "verifier this matrix exists to exercise"
        )
        return port

    def __call__(self, headers: dict[str, str], settings: Settings | None = None) -> Principal:
        verifier = self.build(settings or _settings())
        ctx = RequestContext(headers=headers)
        if self.name == BOUND_ADAPTER:
            principal: Principal = verifier.resolve(ctx)
            return principal
        return verifier.authenticate(ctx).principal

    def reason(self, *, request_path: str, bound_adapter: str) -> str:
        return request_path if self.name == REQUEST_PATH else bound_adapter


@pytest.fixture(params=[REQUEST_PATH, BOUND_ADAPTER])
def verifier(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> _Implementation:
    return _Implementation(request.param, monkeypatch)


def _claims(
    *,
    audience: str | None = AUDIENCE,
    issuer: str | None = IAP_ISSUER,
    email: str | None = HUMAN,
    subject: str | None = HUMAN_SUB,
    hosted_domain: str | None = "bank.example",
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
    omit: tuple[str, ...] = (),
) -> dict[str, Any]:
    """One IAP-shaped claim set. ``None`` leaves a claim out; every knob is a cell."""
    now = issued_at or datetime.now(tz=UTC) - timedelta(seconds=30)
    exp = expires_at or (now + timedelta(minutes=10))
    payload: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "email": email,
        "hd": hosted_domain,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return {k: v for k, v in payload.items() if v is not None and k not in omit}


def _assertion(keys: dict[str, Any], *, signer: str = "iap", **claims: Any) -> str:
    token: bytes = ga_jwt.encode(keys[signer], _claims(**claims))
    return token.decode("ascii")


def _machine_assertion(keys: dict[str, Any], **claims: Any) -> str:
    """A service account's assertion: its address as ``email`` and NO ``hd`` claim at all."""
    return _assertion(keys, email=MACHINE, subject=MACHINE_SUB, hosted_domain=None, **claims)


# --------------------------------------------------------------------------------------- #
# The control. Without a cell that SUCCEEDS, every refusal below is satisfied by an adapter
# that refuses everything, which is not a service.
# --------------------------------------------------------------------------------------- #
def test_the_deployed_profile_authenticates_through_the_request_path_verifier() -> None:
    """What the cells call "the request path" is what ``CDD_PROFILE=gcp`` actually serves."""
    assert _settings().identity_mode == "iap"
    assert IapIdentityAdapter.end_user_auth == "verified"


@BOTH_HEADERS
def test_a_correctly_minted_assertion_verifies_and_yields_the_principal(
    keys: dict[str, Any], fetched: list[str], verifier: _Implementation, header: str
) -> None:
    principal = verifier({header: _assertion(keys)})
    actor = canonical_actor(IAP_ISSUER, HUMAN_SUB)
    assert principal.subject == actor, "the actor is the immutable (iss, sub) pair"
    assert principal.tenant == "bank.example"
    assert principal.principals == (f"user:{actor}",)
    assert principal.assurance == "iap"
    assert principal.source == "gcp-iap"
    assert fetched == [IAP_KEYS_URL], "the verifier must fetch IAP's key set, once"


@BOTH_HEADERS
def test_the_request_path_records_the_verified_provenance(
    keys: dict[str, Any], fetched: list[str], header: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The evidence the audit trail carries comes from the VERIFIED claims, never the request."""
    port = _Implementation(REQUEST_PATH, monkeypatch).build(_settings())
    evidence = port.authenticate(RequestContext(headers={header: _assertion(keys)})).evidence
    assert (evidence.issuer, evidence.source_subject) == (IAP_ISSUER, HUMAN_SUB)
    assert evidence.token_type == "iap-assertion"
    assert evidence.display_email == HUMAN


@BOTH_HEADERS
def test_the_reviewed_maps_tenant_and_entitle_a_verified_human(
    keys: dict[str, Any], fetched: list[str], verifier: _Implementation, header: str
) -> None:
    settings = _settings(
        tenants={"bank.example": "reference-bank"}, groups={"bank.example": ("group:analyst",)}
    )
    principal = verifier({header: _assertion(keys)}, settings)
    assert principal.tenant == "reference-bank"
    assert principal.principals[1:] == ("group:analyst",)


@BOTH_HEADERS
def test_a_domain_the_reviewed_map_does_not_name_is_refused(
    keys: dict[str, Any], fetched: list[str], verifier: _Implementation, header: str
) -> None:
    """With a map configured it is exhaustive: the raw ``hd`` is never a fallback partition."""
    settings = _settings(tenants={"other.example": "reference-bank"})
    with pytest.raises(IdentityError, match="did not resolve a policy-mapped tenant"):
        verifier({header: _assertion(keys)}, settings)


# --------------------------------------------------------------------------------------- #
# Refused by google-auth, because both implementations pass audience= and certs_url=.
# --------------------------------------------------------------------------------------- #
@BOTH_HEADERS
@pytest.mark.parametrize(
    ("audience", "reason"),
    [
        (OTHER_AUDIENCE, f"Token has wrong audience {OTHER_AUDIENCE}"),
        (None, "Token has wrong audience None"),
    ],
    ids=["another-service", "no-aud-claim"],
)
def test_an_assertion_for_another_audience_is_refused_by_the_verifier(
    keys: dict[str, Any],
    fetched: list[str],
    verifier: _Implementation,
    header: str,
    audience: str | None,
    reason: str,
) -> None:
    """THE defect. Signed by the right key, well formed, unexpired, and for somebody else.

    The reason is google-auth's own. The bound adapter ALSO compares the audience after
    verification, so a verifier that stopped receiving ``audience=`` would still refuse there --
    with a different reason. Matching google-auth's words is what keeps that regression red.
    """
    with pytest.raises(IdentityError, match=re.escape(f"verification failed: {reason}")):
        verifier({header: _assertion(keys, audience=audience)})
    assert fetched == [IAP_KEYS_URL]


@BOTH_HEADERS
@pytest.mark.parametrize(
    ("offset", "reason"),
    [(-timedelta(hours=2), "Token expired"), (timedelta(hours=2), "Token used too early")],
    ids=["expired", "not-yet-valid"],
)
def test_an_assertion_outside_its_lifetime_is_refused(
    keys: dict[str, Any],
    fetched: list[str],
    verifier: _Implementation,
    header: str,
    offset: timedelta,
    reason: str,
) -> None:
    issued = datetime.now(tz=UTC) + offset
    assertion = _assertion(keys, issued_at=issued, expires_at=issued + timedelta(minutes=10))
    with pytest.raises(IdentityError, match=re.escape(f"verification failed: {reason}")):
        verifier({header: assertion})


@BOTH_HEADERS
@pytest.mark.parametrize("claim", ["exp", "iat"])
def test_an_assertion_with_no_lifetime_is_refused(
    keys: dict[str, Any], fetched: list[str], verifier: _Implementation, header: str, claim: str
) -> None:
    """No ``exp`` is a credential that never stops working; google-auth refuses it outright."""
    reason = f"verification failed: Token does not contain required claim {claim}"
    with pytest.raises(IdentityError, match=re.escape(reason)):
        verifier({header: _assertion(keys, omit=(claim,))})


@BOTH_HEADERS
@pytest.mark.parametrize(
    ("signer", "reason"),
    [
        ("impostor", f"Certificate for key id {IMPOSTOR_KID} not found"),
        ("impostor-as-iap", "Could not verify token signature"),
        ("impostor-no-kid", "Could not verify token signature"),
    ],
    ids=["impostor-own-kid", "impostor-claims-iap-kid", "impostor-no-kid"],
)
def test_an_assertion_signed_by_a_key_iap_does_not_publish_is_refused(
    keys: dict[str, Any],
    fetched: list[str],
    verifier: _Implementation,
    header: str,
    signer: str,
    reason: str,
) -> None:
    """Correct claims, audience and issuer, signed by a key outside IAP's set.

    An impostor naming its OWN key id is refused at the key lookup, before any signature is
    checked. The two that reach the signature check are the forgeries that matter: one that
    claims IAP's key id, and one that names no key so every published key is tried.
    """
    with pytest.raises(IdentityError, match=re.escape(f"verification failed: {reason}")):
        verifier({header: _assertion(keys, signer=signer)})


@BOTH_HEADERS
def test_a_tampered_payload_under_a_genuine_signature_is_refused(
    keys: dict[str, Any], fetched: list[str], verifier: _Implementation, header: str
) -> None:
    """IAP's own signature, over a DIFFERENT claim set than the one presented."""
    head, _body, signature = _assertion(keys).split(".")
    forged_body = _b64(json.dumps(_claims(subject="accounts.google.com:999")).encode())
    with pytest.raises(IdentityError, match="verification failed: Could not verify token"):
        verifier({header: f"{head}.{forged_body}.{signature}"})


# --------------------------------------------------------------------------------------- #
# Malformed and unpinned: the bound adapter's pin refuses before any cryptography; the request
# path has no pin, so google-auth refuses. Each cell names BOTH real reasons.
# --------------------------------------------------------------------------------------- #
def _unsigned() -> str:
    """``alg: none`` over a claim set that would otherwise be perfect."""
    header = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    return f"{header}.{_b64(json.dumps(_claims()).encode())}."


def _hmac_keyed_with_the_public_key(keys: dict[str, Any]) -> str:
    """The HS256 confusion: an HMAC whose "secret" is IAP's PUBLIC key, which everybody has."""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": SIGNING_KID}).encode())
    signing_input = f"{header}.{_b64(json.dumps(_claims()).encode())}"
    secret = keys["iap_public_pem"].encode("ascii")
    mac = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(mac)}"


_MALFORMED: list[tuple[Callable[[dict[str, Any]], str], str, str]] = [
    (lambda keys: "not-a-jwt", "Wrong number of segments in token", "not a compact JWS or JWE"),
    (
        lambda keys: "eyJhbGciOiJFUzI1NiJ9",
        "Wrong number of segments in token",
        "not a compact JWS or JWE",
    ),
    (lambda keys: "a.b.c", "Invalid base64-encoded string", "not base64url-encoded JSON"),
    (lambda keys: "...", "Wrong number of segments in token", "not a compact JWS or JWE"),
    (
        lambda keys: _unsigned(),
        "Unsupported signature algorithm none",
        "alg 'none', which means it is UNSIGNED",
    ),
    (
        _hmac_keyed_with_the_public_key,
        "Unsupported signature algorithm HS256",
        "signed with HS256, which is not in this deployment's pinned set",
    ),
]


@BOTH_HEADERS
@pytest.mark.parametrize(
    ("build", "request_path_reason", "bound_adapter_reason"),
    _MALFORMED,
    ids=["garbage", "one-segment", "three-bad-segments", "dots", "alg-none", "hs256-confusion"],
)
def test_a_malformed_or_unpinned_assertion_is_a_refusal_and_never_an_escaping_exception(
    keys: dict[str, Any],
    fetched: list[str],
    verifier: _Implementation,
    header: str,
    build: Callable[[dict[str, Any]], str],
    request_path_reason: str,
    bound_adapter_reason: str,
) -> None:
    """Each cell asserts its OWN reason rather than a shared "verification failed".

    An unsigned token recognised as unsigned and a token rejected for being unparseable are
    different security properties. The bound adapter judges the algorithm with no cryptography
    and no network, so nothing is fetched; the request path has no such pin, so the refusal is
    google-auth's, after the key set is fetched.
    """
    reason = verifier.reason(
        request_path=f"verification failed: {request_path_reason}",
        bound_adapter=bound_adapter_reason,
    )
    with pytest.raises(IdentityError, match=re.escape(reason)):
        verifier({header: build(keys)})
    assert fetched == ([IAP_KEYS_URL] if verifier.name == REQUEST_PATH else [])


@BOTH_HEADERS
def test_a_token_that_passes_the_pin_with_an_unreadable_body_is_still_a_refusal(
    keys: dict[str, Any], fetched: list[str], verifier: _Implementation, header: str
) -> None:
    """google-auth's parse error is a ``ValueError``; unwrapped it would be a bare 500."""
    head = _b64(json.dumps({"alg": "ES256", "typ": "JWT", "kid": SIGNING_KID}).encode())
    with pytest.raises(IdentityError, match="verification failed: Can't parse segment"):
        verifier({header: f"{head}.{_b64(b'not json')}.{_b64(b'x' * 64)}"})


# --------------------------------------------------------------------------------------- #
# Refused AFTER a valid signature: verify_token checks no issuer and no identity.
# --------------------------------------------------------------------------------------- #
@BOTH_HEADERS
@pytest.mark.parametrize(
    ("issuer", "bound_adapter_reason"),
    [
        ("https://accounts.google.com", "issued by a party this deployment does not accept"),
        (
            "https://securetoken.google.com/fictional-project-000000",
            "issued by a party this deployment does not accept",
        ),
        (None, "missing required claim(s): iss"),
    ],
    ids=["oauth2-issuer", "firebase-issuer", "no-issuer"],
)
def test_a_validly_signed_assertion_from_the_wrong_issuer_is_refused(
    keys: dict[str, Any],
    fetched: list[str],
    verifier: _Implementation,
    header: str,
    issuer: str | None,
    bound_adapter_reason: str,
) -> None:
    """Right key, right audience, unexpired: ``verify_token`` returns these claims happily."""
    reason = verifier.reason(
        request_path="IAP assertion is missing the exact issuer/sub identity",
        bound_adapter=bound_adapter_reason,
    )
    with pytest.raises(IdentityError, match=re.escape(reason)):
        verifier({header: _assertion(keys, issuer=issuer)})
    assert fetched == [IAP_KEYS_URL], "refused AFTER a signature that verified"


@BOTH_HEADERS
@pytest.mark.parametrize("subject", [None, "  "], ids=["no-sub", "blank-sub"])
def test_a_validly_signed_assertion_naming_no_subject_is_refused(
    keys: dict[str, Any],
    fetched: list[str],
    verifier: _Implementation,
    header: str,
    subject: str | None,
) -> None:
    reason = verifier.reason(
        request_path="IAP assertion is missing the exact issuer/sub identity",
        bound_adapter="missing required claim(s): sub",
    )
    with pytest.raises(IdentityError, match=re.escape(reason)):
        verifier({header: _assertion(keys, subject=subject)})


@BOTH_HEADERS
def test_the_bound_adapter_refuses_an_assertion_with_no_email(
    keys: dict[str, Any], fetched: list[str], header: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _Implementation(BOUND_ADAPTER, monkeypatch)
    with pytest.raises(IdentityError, match=re.escape("missing required claim(s): email")):
        verifier({header: _assertion(keys, email=None)})


@BOTH_HEADERS
def test_the_request_path_admits_an_assertion_with_no_email_as_its_immutable_pair(
    keys: dict[str, Any], fetched: list[str], header: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one place the two implementations disagree about what is accepted, pinned as it is.

    The request path's actor is the ``(iss, sub)`` pair and its tenant comes from ``hd``, so an
    absent ``email`` removes nothing either depends on: the display address is empty and the
    same caller is named. It is not a widening -- the signature, audience, lifetime and issuer
    are all still required -- but it IS a divergence from the bound adapter, which demands the
    claim, and this cell exists so that closing it in either direction is a visible decision.
    """
    port = _Implementation(REQUEST_PATH, monkeypatch).build(_settings())
    ctx = RequestContext(headers={header: _assertion(keys, email=None)})
    authenticated = port.authenticate(ctx)
    assert authenticated.principal.subject == canonical_actor(IAP_ISSUER, HUMAN_SUB)
    assert authenticated.principal.tenant == "bank.example"
    assert authenticated.evidence.display_email == ""


# --------------------------------------------------------------------------------------- #
# The transport: which name is read, and that neither can rescue the other.
# --------------------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {EDGE_HEADER: ""},
        {EDGE_HEADER: "   "},
        {FORWARDED_HEADER: ""},
        {FORWARDED_HEADER: "\t"},
        {EDGE_HEADER: " ", FORWARDED_HEADER: "\n"},
    ],
    ids=["none", "edge-empty", "edge-blank", "forwarded-empty", "forwarded-blank", "both-blank"],
)
def test_an_absent_or_blank_assertion_is_refused_before_anything_is_fetched(
    fetched: list[str], verifier: _Implementation, headers: dict[str, str]
) -> None:
    with pytest.raises(IdentityError, match="missing IAP assertion header") as caught:
        verifier(headers)
    assert EDGE_HEADER in str(caught.value) and FORWARDED_HEADER in str(caught.value)
    assert fetched == [], "nothing should have been fetched"


def test_a_bad_edge_assertion_is_not_rescued_by_a_good_forwarded_one(
    keys: dict[str, Any], fetched: list[str], verifier: _Implementation
) -> None:
    """The fallback is for an ABSENT edge header, never for a failed one."""
    headers = {
        EDGE_HEADER: _assertion(keys, audience=OTHER_AUDIENCE),
        FORWARDED_HEADER: _assertion(keys),
    }
    with pytest.raises(IdentityError, match="Token has wrong audience"):
        verifier(headers)
    assert fetched == [IAP_KEYS_URL], "exactly one assertion was verified"


def test_the_edge_assertion_wins_and_the_forwarded_one_is_never_read(
    keys: dict[str, Any], fetched: list[str], verifier: _Implementation
) -> None:
    """With both present, the principal is the edge's caller; the other value is not a vote."""
    headers = {
        EDGE_HEADER: _assertion(keys),
        FORWARDED_HEADER: _assertion(keys, subject="accounts.google.com:2"),
    }
    assert verifier(headers).subject == canonical_actor(IAP_ISSUER, HUMAN_SUB)
    assert fetched == [IAP_KEYS_URL]


def test_header_names_are_matched_without_regard_to_case(
    keys: dict[str, Any], fetched: list[str], verifier: _Implementation
) -> None:
    principal = verifier({FORWARDED_HEADER.upper(): _assertion(keys)})
    assert principal.subject == canonical_actor(IAP_ISSUER, HUMAN_SUB)


# --------------------------------------------------------------------------------------- #
# Machine callers: admitted only by naming the account's domain in the reviewed map.
# --------------------------------------------------------------------------------------- #
@BOTH_HEADERS
def test_a_service_account_whose_domain_is_mapped_resolves_to_that_tenant(
    keys: dict[str, Any], fetched: list[str], verifier: _Implementation, header: str
) -> None:
    settings = _settings(tenants={MACHINE_DOMAIN: "reference-bank"})
    principal = verifier({header: _machine_assertion(keys)}, settings)
    assert principal.subject == canonical_actor(IAP_ISSUER, MACHINE_SUB)
    assert principal.tenant == "reference-bank"


@BOTH_HEADERS
@pytest.mark.parametrize(
    "tenants", [{}, {"bank.example": "reference-bank"}], ids=["no-map", "map-names-humans-only"]
)
def test_an_unmapped_service_account_is_refused(
    keys: dict[str, Any],
    fetched: list[str],
    verifier: _Implementation,
    header: str,
    tenants: dict[str, str],
) -> None:
    """IAP signed for it, and it is still refused: no reviewed rule gives it a tenant.

    A machine carries no ``hd``, so with no map its tenant is empty, and with a map that names
    only human domains nothing names it. Either way the claim policy refuses rather than hand a
    tenantless principal to a service whose every read is partitioned by tenant.
    """
    with pytest.raises(IdentityError, match="did not resolve a policy-mapped tenant"):
        verifier({header: _machine_assertion(keys)}, _settings(tenants=tenants))
    assert fetched == [IAP_KEYS_URL], "refused AFTER a signature that verified"


def test_a_forged_machine_assertion_is_refused_by_the_verifier(
    keys: dict[str, Any], fetched: list[str], verifier: _Implementation
) -> None:
    """A mapped domain is worth nothing without IAP's signature over the address in it."""
    settings = _settings(tenants={MACHINE_DOMAIN: "reference-bank"})
    forged = _machine_assertion(keys, signer="impostor-as-iap")
    with pytest.raises(IdentityError, match="Could not verify token signature"):
        verifier({FORWARDED_HEADER: forged}, settings)


# --------------------------------------------------------------------------------------- #
# The deployment's own failure: no audience means nobody, refused before anything is fetched.
# --------------------------------------------------------------------------------------- #
@BOTH_HEADERS
def test_an_unconfigured_audience_refuses_even_a_perfect_assertion(
    keys: dict[str, Any],
    fetched: list[str],
    verifier: _Implementation,
    header: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no verify-without-an-audience path to fall into, under either name.

    The bound adapter answers 503, the deployment's failure rather than the caller's. The
    request path refuses too, as an ordinary ``IdentityError``; both refuse before any key is
    fetched, which is the property this cell holds.
    """
    monkeypatch.delenv(AUDIENCE_ENV, raising=False)
    with pytest.raises(IdentityError, match=re.escape(f"{AUDIENCE_ENV} is not configured")) as err:
        verifier({header: _assertion(keys)})
    if verifier.name == BOUND_ADAPTER:
        assert isinstance(err.value, IapAudienceUnconfiguredError)
        assert err.value.http_status == 503
    assert fetched == []


@pytest.mark.parametrize("value", ["", "   "], ids=["empty", "blank"])
def test_an_emptied_audience_refuses_to_build_either_verifier(
    fetched: list[str], verifier: _Implementation, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three-state read, cashed: an emptied audience never becomes an expected audience."""
    monkeypatch.setenv(AUDIENCE_ENV, value)
    with pytest.raises(ConfiguredEmptyError, match=re.escape(f"{AUDIENCE_ENV} is set to an empty")):
        verifier.build(_settings())
    assert fetched == []


# --------------------------------------------------------------------------------------- #
# The mutants: prove this matrix would FAIL if the protection were removed. A negative matrix
# nobody proved can go red is a green tick over an adapter that verifies nothing.
# --------------------------------------------------------------------------------------- #
def test_the_matrix_would_go_red_without_the_audience_argument(
    keys: dict[str, Any], fetched: list[str], verifier: _Implementation
) -> None:
    """The defective call, reproduced: ``verify_token`` with no ``audience=``.

    ``certs_url`` is passed only because the fake transport is the only key server in this
    process; what is being demonstrated is the audience. The defective call accepts a token
    minted for a DIFFERENT protected resource, and each implementation refuses that same token
    under either header.
    """
    from google.oauth2 import id_token

    foreign = _assertion(keys, audience=OTHER_AUDIENCE)
    claims = id_token.verify_token(foreign, ga_requests.Request(), certs_url=IAP_KEYS_URL)
    assert claims["email"] == HUMAN, (
        "verify_token without audience= accepted an assertion minted for another service, which "
        "is the defect. If this ever stops being true, google-auth changed its default and the "
        "adapters' own checks are what still hold."
    )
    for header in (EDGE_HEADER, FORWARDED_HEADER):
        with pytest.raises(IdentityError, match="Token has wrong audience"):
            verifier({header: foreign})


def test_the_matrix_would_go_red_if_the_forwarded_header_bypassed_the_verifier(
    keys: dict[str, Any], fetched: list[str], verifier: _Implementation
) -> None:
    """Without the signature check, the shared claim policy alone would admit a forged machine.

    Decoded WITHOUT verification, an impostor's assertion for a mapped service account becomes a
    principal in the mapped tenant: nothing after the verifier can tell it apart. So the
    forwarded-header cells above are only meaningful because the forwarded name reaches the same
    verifier, and each implementation refusing the very same token is that proof.
    """
    settings = _settings(tenants={MACHINE_DOMAIN: "reference-bank"})
    forged = _machine_assertion(keys, signer="impostor-as-iap")
    unverified = ga_jwt.decode(forged, verify=False)
    principal, _, _ = reviewed_principal_from_iap_claims(
        unverified, settings, expected_issuer=IAP_ISSUER
    )
    assert principal.tenant == "reference-bank"

    with pytest.raises(IdentityError, match="Could not verify token signature"):
        verifier({FORWARDED_HEADER: forged}, settings)
