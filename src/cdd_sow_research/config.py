"""Configuration and the adapter factory (dependency injection for the hexagon).

The factory reads ``config/settings.yaml`` (with ``${ENV_VAR}`` interpolation) and binds
each port to a concrete adapter by dotted path. Switching the whole system from the GCP
managed stack to an on-prem stack is a one-line change of ``profile`` (proof of the
ports-and-adapters / no-lock-in principle, P-02). Every adapter follows one construction
convention: ``Adapter(settings: Settings)``.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .domain.browser_flow import ACCESS_TOKEN_SUBJECT_TYPE, SUBJECT_TOKEN_TYPES
from .domain.policy import RiskPolicy
from .envread import (
    ConfiguredEmptyError,
    EnvSetting,
    optional_setting,
    read_env_setting,
    setting_or_default,
)

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-(.*?))?\}")
DEFAULT_GCP_REGION = "asia-southeast1"

#: Multi-regions Document AI may use as a STATED residency deviation from the deploy region.
#: Each names one jurisdiction and carries an ML-processing commitment for it. `global` is
#: deliberately absent: it names no jurisdiction at all.
_DOCUMENT_AI_MULTI_REGIONS = frozenset({"us", "eu"})

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
RUNTIME_PROFILES = frozenset({"local", "live", "gcp", "platform", "onprem"})
#: Profiles whose adapters bind real Google Cloud SDKs and therefore need a real project.
MANAGED_PROFILES = frozenset({"gcp", "platform"})
#: The project id `config/settings.yaml` documents as a placeholder. Correct on a laptop, and a
#: defect in any profile that calls a cloud API.
PLACEHOLDER_PROJECT_ID = "your-gcp-project"

#: The one environment variable that selects the runtime profile. Only :func:`resolve_profile`
#: may read it; ``tests/unit/test_profile_single_source.py`` fails the build if another module
#: re-derives the profile with its own permissive default.
_PROFILE_ENV = "CDD_PROFILE"

#: The identity mode handed to every RELAXATION when nobody chose a profile or an identity
#: mode. Deliberately NOT a member of :data:`IDENTITY_MODES`, and it never reaches an adapter
#: binding: it exists so that "no choice was made" is a distinct input to the security layers
#: rather than being indistinguishable from a deliberately chosen ``local-persona``.
UNCONSENTED_IDENTITY_MODE = "unconfigured"

#: ``oidc-session`` was once a runtime profile and is now an identity mode. The exact
#: migration wording lives here so the boot-time validator and the identity-mode inference
#: cannot drift apart.
_OIDC_SESSION_MIGRATION = (
    "CDD_PROFILE=oidc-session is no longer a runtime profile; set "
    "CDD_PROFILE=<local|live|gcp|platform|onprem> and "
    "CDD_IDENTITY_PROFILE=oidc-session"
)
RUNTIME_ADAPTER_PORTS = frozenset(
    {
        "adverse_media",
        "agent_registry",
        "audit",
        "browser_flow_store",
        "case_store",
        "compliance",
        "document_store",
        "evaluation",
        "extraction",
        "guardrail",
        "knowledge_base",
        "llm",
        "monitoring_store",
        "ownership_graph",
        "redaction",
        "registry",
        "review_router",
        "sanctions",
        "tool_catalog",
        "tracer",
    }
)
IDENTITY_MODES = frozenset(
    {
        "local-persona",
        "iap",
        "oidc-session",
        "oauth-access-token",
        "embedded-grant",
        "onprem",
    }
)
CHANNEL_MODES = frozenset({"native", "sandboxed", "standalone"})
CHANNEL_IDENTITY_MATRIX: dict[str, frozenset[str]] = {
    "native": frozenset({"local-persona", "iap", "oauth-access-token"}),
    "sandboxed": frozenset({"oauth-access-token", "embedded-grant"}),
    "standalone": frozenset({"local-persona", "iap", "oidc-session", "onprem"}),
}
PUBLIC_MOUNT_PATH = "/agent"


@dataclass(frozen=True, slots=True)
class ProfileChoice:
    """The ONE resolution of ``CDD_PROFILE``, distinguishing "unset" from "chose local".

    Absent this type the profile was a TWO-state read (``os.environ.get("CDD_PROFILE",
    "local")``), so a missing variable was indistinguishable from an operator who named the
    ``local`` profile. That mattered because ``local`` is the RELAXED posture: it infers the
    ``local-persona`` identity mode, whose seeded dev personas authenticate nobody, whose
    allowed request headers include ``X-Dev-Persona``, whose CORS allowlist falls back to
    localhost dev origins, and whose rate limit is disabled entirely.
    """

    #: Which adapter family to bind. Absent a choice this is still ``local``, because the
    #: alternative is importing cloud SDKs that are not installed; what an unconsented run
    #: loses is the identity relaxation, not the SDK-free data adapters.
    profile: str = "local"
    #: Was the profile named DELIBERATELY (``CDD_PROFILE`` set, or ``profile:`` written into
    #: the settings file)? Direct construction is deliberate by definition, so the default
    #: is True and only :meth:`Settings.load` can produce False.
    explicit: bool = True


def _validate_profile(profile: str) -> str:
    """Fail closed on a profile string nothing binds, INCLUDING a capitalisation typo.

    The comparison is exact and case-sensitive on purpose: every posture decision downstream
    matches the profile string exactly, so ``Local`` selects none of the relaxations but also
    none of the restrictions. Normalising the case here would turn a typo into a silent
    choice; refusing it turns the typo into a boot failure.
    """
    if profile == "oidc-session":
        raise ValueError(_OIDC_SESSION_MIGRATION)
    if profile not in RUNTIME_PROFILES:
        allowed = ", ".join(sorted(RUNTIME_PROFILES))
        raise ValueError(f"unknown {_PROFILE_ENV} {profile!r}; expected one of: {allowed}")
    return profile


def _profile_setting(environ: Mapping[str, str] | None) -> EnvSetting:
    if environ is None:
        return read_env_setting(_PROFILE_ENV)
    raw = environ.get(_PROFILE_ENV)
    return EnvSetting(name=_PROFILE_ENV, raw=raw, value="" if raw is None else raw.strip())


def resolve_profile(
    environ: Mapping[str, str] | None = None, *, file_profile: str = ""
) -> ProfileChoice:
    """Read ``CDD_PROFILE`` once: absent is NO CHOICE and configured-empty refuses.

    Resolution is three-state: unset is carried as unconsented; configured-empty refuses;
    set-and-valid is carried through; set-and-invalid raises here rather than at the first
    request, so a typo is a boot failure. ``file_profile`` is the settings file's own
    ``profile:`` key, which counts as a deliberate choice when it is written down; the
    shipped file leaves it as ``${CDD_PROFILE}`` so an unset variable stays unset instead of
    materialising as a written-down ``local``.
    """
    setting = _profile_setting(environ)
    if setting.is_configured_empty:
        raise ConfiguredEmptyError(
            f"{_PROFILE_ENV} is set to an empty value; unset it for the unconsented "
            "loopback-only posture, or name a supported profile."
        )
    if setting.has_value:
        return ProfileChoice(profile=_validate_profile(setting.value), explicit=True)
    chosen = file_profile.strip()
    if chosen:
        return ProfileChoice(profile=_validate_profile(chosen), explicit=True)
    return ProfileChoice(profile="local", explicit=False)


def _require_https_issuer(issuer: str) -> None:
    """Reject a non-https OIDC issuer at config load (loopback is allowed for dev).

    An ID token verified against an http issuer is trivially MITM-able, so a
    misconfigured trusted issuer is a fail-fast config error, not a runtime surprise.
    """
    if not issuer:
        return
    from urllib.parse import urlparse

    parsed = urlparse(issuer)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and (parsed.hostname or "") in _LOOPBACK_HOSTS:
        return
    raise ValueError(
        f"identity.trusted_issuers: refusing non-https OIDC issuer {issuer!r} "
        "(https is required; http is allowed only for localhost development)"
    )


#: Exact entries that are a wildcard by BEHAVIOUR rather than by spelling, so the asterisk test
#: below cannot see them. ``null`` is the one that matters: a sandboxed iframe presents a null
#: origin, so allowing it invites the framing this policy exists to refuse. They were already
#: rejected, but by the origin validator rather than by the wildcard rule, and incidental
#: coverage moves the day the validator does. The same set is refused in ``ui/lib/csp.mjs``.
_WILDCARD_TOKENS = frozenset({"*", "'*'", "null", "*.*"})


def _require_public_origin(origin: str, purpose: str) -> None:
    """Require one canonical HTTPS origin, with a loopback HTTP dev exception."""
    parsed = urlparse(origin)
    loopback_http = parsed.scheme == "http" and (parsed.hostname or "") in _LOOPBACK_HOSTS
    if parsed.scheme != "https" and not loopback_http:
        raise ValueError(f"{purpose} requires an HTTPS public origin (loopback HTTP is dev-only)")
    if not parsed.netloc or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError(f"{purpose} public origin must contain only scheme and authority")


def _require_https_endpoint(endpoint: str, purpose: str) -> None:
    parsed = urlparse(endpoint)
    loopback_http = parsed.scheme == "http" and (parsed.hostname or "") in _LOOPBACK_HOSTS
    if parsed.scheme != "https" and not loopback_http:
        raise ValueError(f"{purpose} must use HTTPS (loopback HTTP is dev-only)")
    if not parsed.netloc or parsed.fragment:
        raise ValueError(f"{purpose} must be an absolute URL without a fragment")


def _interpolate(value: Any) -> Any:
    """Replace ``${VAR}`` / ``${VAR:-default}`` tokens in strings recursively."""
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            return setting_or_default(m.group(1), m.group(2) or "")

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


_BOOL_STRINGS = {
    "true": True,
    "1": True,
    "yes": True,
    "on": True,
    "false": False,
    "0": False,
    "no": False,
    "off": False,
}


def _coerce_scalars(cls: type, raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce env-interpolated strings to each field's declared scalar type.

    ``${VAR:-default}`` interpolation always yields *strings*, and ``bool("false")`` is
    ``True`` — so without this, ``CDD_GROUNDING_ENABLED=false`` would leave grounding ON.
    Strings targeting a ``bool``/``int``/``float`` field are converted (an empty string is
    dropped so the dataclass default applies); anything else passes through unchanged.
    """
    fields = getattr(cls, "__dataclass_fields__", {})
    coerced: dict[str, Any] = {}
    for key, value in raw.items():
        declared = fields[key].type if key in fields else None
        # With `from __future__ import annotations` the declared type is a string.
        type_name = declared if isinstance(declared, str) else getattr(declared, "__name__", "")
        if isinstance(value, str) and type_name in ("bool", "int", "float"):
            text = value.strip()
            if not text:
                continue  # empty env var: fall back to the field default
            if type_name == "bool":
                if text.lower() not in _BOOL_STRINGS:
                    raise ValueError(f"{cls.__name__}.{key}: not a boolean: {value!r}")
                coerced[key] = _BOOL_STRINGS[text.lower()]
            else:
                coerced[key] = int(text) if type_name == "int" else float(text)
        else:
            coerced[key] = value
    return coerced


@dataclass(frozen=True)
class ModelSettings:
    #: The Vertex location the model client calls, deliberately NOT the compute region. See
    #: the reasoning beside `models.location` in config/settings.yaml. This is the DATACLASS
    #: fallback, reached only when no settings file supplies the key: `us` is the United
    #: States multi-region, which names a jurisdiction and carries an ML-processing residency
    #: guarantee, where `global` carries none. The shipped config/settings.yaml pins
    #: `asia-southeast1` instead, which since 2026-08-27 is the stronger answer -- it serves
    #: `gemini-3.5-flash` with a documented Singapore ML-processing commitment. It is
    #: `us-central1` that serves no Gemini 3, which is why the compute region is never the
    #: model location here.
    location: str = "us"
    reasoning: str = "gemini-3.5-flash"
    triage: str = "gemini-3.5-flash"
    #: No Gemini 3 pro tier serves the `us` multi-region, and the previous default here
    #: (gemini-3.1-pro) resolves in no location at all.
    hard_reasoning: str = "gemini-3.5-flash"
    use_hard_reasoning: bool = False


@dataclass(frozen=True)
class DocumentAiSettings:
    processor_id: str = ""  # projects/.../locations/.../processors/...
    location: str = DEFAULT_GCP_REGION
    processor_version: str = "rc"


@dataclass(frozen=True)
class KnowledgeBaseSettings:
    # B1's governed RAG store IS A2; default top_k for case retrieval.
    base_url_env: str = "KNOWLEDGE_BASE_URL"
    data_store_id: str = "cdd-case-kb"  # only used by the standalone GCP adapter
    location: str = DEFAULT_GCP_REGION
    top_k: int = 10
    # Discovery Engine path segments (standalone GCP adapter only). The API defaults fit
    # most projects; enterprises with named collections/branches override them here.
    collection_id: str = "default_collection"
    branch_id: str = "default_branch"
    serving_config_id: str = "default_search"
    #: The Discovery Engine ENGINE (app) to search, when the deployment has one.
    #:
    #: Empty searches the data store's own serving config, which is Standard edition. This
    #: adapter asks for extractive segments, an Enterprise-edition feature, so a data-store
    #: search is refused with a 400 that names the engine path as the remedy. Ingestion is
    #: unaffected either way: documents are written to a data store BRANCH, and an engine has
    #: none.
    engine_id: str = ""


@dataclass(frozen=True)
class ModelArmorSettings:
    template_id: str = "cdd-sow-guardrail"
    host: str = f"modelarmor.{DEFAULT_GCP_REGION}.rep.googleapis.com"


@dataclass(frozen=True)
class DlpSettings:
    inspect_template: str = ""  # projects/.../inspectTemplates/...
    deidentify_template: str = ""  # projects/.../deidentifyTemplates/...


@dataclass(frozen=True)
class LoggingSettings:
    log_name: str = "cdd-sow-research-audit"
    bucket: str = "cdd-sow-agent-worm"
    retention_days: int = 180  # Six months


@dataclass(frozen=True)
class AgentEngineSettings:
    resource_name: str = ""  # reasoningEngine resource id, set after deploy
    display_name: str = "cdd-sow-research"


@dataclass(frozen=True)
class LocalSettings:
    """Paths for the SDK-free ``local`` profile stores (SQLite FTS5 + append-only audit).

    Empty strings select the per-package default under ``~/.cdd_sow_research/``; tests pass
    ``:memory:`` for ephemeral, deterministic stores. No Google Cloud here.
    """

    db_path: str = ""  # SQLite FTS5 case-evidence index; "" => ~/.cdd_sow_research/local.db
    audit_path: str = ""  # append-only audit store;        "" => ~/.cdd_sow_research/audit.db
    sanctions_path: str = ""  # watchlist snapshot JSON;     "" => bundled fixture
    documents_path: str = ""  # uploaded-document blobs;     "" => ~/.cdd_sow_research/documents.db
    # Shared by the isolated and standalone local proof. Deliberately no implicit
    # default: the two processes must be pointed at the same reviewed durable file.
    browser_flow_path: str = ""
    client_assertion_replay_path: str = ""


@dataclass(frozen=True)
class LiveSettings:
    """The ``live`` profile's extraction knobs (real documents, Gemini transcription).

    The profile carries no model-server settings: every model call is the Gemini API,
    configured under ``models``, because a use case that needs internet research is only
    implemented for customers who permit leaving the data centre (org decision,
    2026-08-30). What remains here is the OCR budget, and it is deliberate: transcribing
    a page image costs a model call, so a document whose text layer is missing is
    transcribed only up to ``max_ocr_pages`` pages, and the rest are reported as unread
    rather than quietly dropped.
    """

    max_output_tokens: int = 2048  # per-page transcription budget
    ocr_enabled: bool = True
    max_ocr_pages: int = 10
    render_dpi: int = 150


@dataclass(frozen=True)
class DocumentStoreSettings:
    """Custody of uploaded case documents (bucket for gcp; limits for every profile).

    ``max_upload_bytes`` is the per-document ceiling enforced at the API boundary. It is
    separate from the whole-request cap (``CDD_MAX_BODY_BYTES``) because one upload is
    one document: the request cap protects the process, this protects the pipeline.
    """

    bucket: str = "cdd-sow-research-documents"  # regional CMEK bucket (gcp profile)
    prefix: str = "case-documents/"
    max_upload_bytes: int = 25 * 1024 * 1024
    # Case-bundle reload ceilings. A bundle is a whole case, so it is legitimately much
    # larger than one upload; each limit guards a different failure. ``max_bundle_bytes``
    # bounds what is read off the wire, ``max_bundle_uncompressed_bytes`` bounds what the
    # archive may expand to (a compression bomb is small on the wire and enormous after),
    # and ``max_bundle_documents`` bounds how many members must be verified.
    max_bundle_bytes: int = 512 * 1024 * 1024
    max_bundle_uncompressed_bytes: int = 512 * 1024 * 1024
    max_bundle_documents: int = 500
    allowed_mime_types: tuple[str, ...] = (
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/webp",
        "text/plain",
        "text/csv",
        "text/markdown",
    )


@dataclass(frozen=True)
class SanctionsSettings:
    """Sanctions/PEP screening snapshot location + matching threshold."""

    bucket: str = "cdd-sow-research-sanctions"  # regional CMEK bucket synced from publishers
    object_name: str = "snapshot/current.json"  # the cached, versioned snapshot object
    match_threshold: float = 0.85  # OFAC-style fuzzy threshold for raising an alert


@dataclass(frozen=True)
class IssuerSettings:
    """One trusted OIDC issuer for the implemented Mode 6 login flow.

    The Mode 6 ``/auth/*`` login flow (``api/auth.py``) drives an Authorization Code +
    PKCE redirect and verifies the returned ``id_token`` with audience = ``client_id``
    (the standard OIDC rule: an ID token's ``aud`` is the relying party's client_id, not
    a separately configured value).

    The planned Mode 4 adapter verifies an OAuth access token under a distinct contract
    (docs/embedding-and-identity.md Section 6). It may reuse reviewed issuer and JWKS
    configuration after that policy is modelled, but it must not pass an access token to
    the Mode 6 ID-token verifier. ``audience`` is retained as a reserved migration field
    for that future resource audience and remains unused by Mode 6.

    ``client_secret_env`` names an environment variable holding the secret value; the
    secret itself is never stored here or in YAML.
    """

    issuer: str = ""  # https://idp.example.com (also the OIDC Discovery base)
    audience: str = ""  # reserved for the planned Mode 4 resource audience
    tenant: str = ""  # which tenant this issuer maps to
    algorithms: tuple[str, ...] = ("RS256", "ES256")
    tenant_claim: str = "tenant"
    groups_claim: str = "groups"
    client_id: str = ""  # OIDC client_id for the /auth/login redirect flow
    client_secret_env: str = ""  # name of the env var holding the client secret
    token_endpoint_auth_method: str = "client_secret_basic"
    allowed_endpoint_hosts: tuple[str, ...] = ()
    end_session_logout: bool = False  # Mode 6: propagate /auth/logout to this issuer too


@dataclass(frozen=True)
class AccessTokenIssuerSettings:
    """Reviewed RFC 9068 policy for one Mode 4 institutional issuer."""

    policy_id: str = ""
    issuer: str = ""
    jwks_uri: str = ""
    resource_audience: str = ""
    tenant: str = ""
    algorithms: tuple[str, ...] = ("RS256", "ES256")
    allowed_clients: tuple[str, ...] = ()
    required_scopes: tuple[str, ...] = ()
    client_claim: str = "client_id"
    tenant_claim: str = "tenant"
    scope_claim: str = "scope"
    installation_claim: str = "installation_id"
    groups_claim: str = "groups"
    client_installations: dict[str, str] = field(default_factory=dict)
    max_lifetime_seconds: int = 300
    clock_skew_seconds: int = 30
    require_nbf: bool = False

    def __post_init__(self) -> None:
        for name in (
            "policy_id",
            "issuer",
            "jwks_uri",
            "resource_audience",
            "tenant",
            "client_claim",
            "tenant_claim",
            "scope_claim",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"access-token policy {name} must be non-empty")
        _require_https_issuer(self.issuer)
        _require_https_endpoint(self.jwks_uri, "access-token JWKS URI")
        if not self.algorithms or not set(self.algorithms) <= {"RS256", "ES256"}:
            raise ValueError("access-token algorithms must be a non-empty subset of RS256/ES256")
        if not self.allowed_clients or not self.required_scopes:
            raise ValueError("access-token policy requires allowed_clients and required_scopes")
        if not 1 <= self.max_lifetime_seconds <= 900:
            raise ValueError("access-token max_lifetime_seconds must be from 1 to 900")
        if not 0 <= self.clock_skew_seconds <= 60:
            raise ValueError("access-token clock_skew_seconds must be from 0 to 60")


@dataclass(frozen=True)
class IdTokenSubjectIssuerSettings:
    """Reviewed OIDC ID-token policy for one Mode 5 broker subject.

    This is a SEPARATE shape from :class:`AccessTokenIssuerSettings`, not an extension of
    it. A Google ID token carries no ``scope``, tenant or installation claim and does
    carry an authorised party (``azp``) and a hosted domain (``hd``), so the two profiles
    have genuinely different reviewed inputs. Modelling Google's omissions as absent
    fields, rather than as required fields nobody can populate, follows
    ``adapters/oidc/discovery.py``, which already treats ``end_session_endpoint`` as
    optional-with-empty-default for the same reason.

    Every pin below is required: an empty hosted domain would accept consumer Google
    accounts, and an empty authorised party would accept a token minted for a different
    client of the same tenant, so neither may default to "unchecked". ``tenant`` is
    reviewed deployment configuration because the token cannot assert one, and the grant
    scope likewise comes from installation policy (see ``api/embed.py``), never a claim.
    """

    policy_id: str = ""
    issuer: str = ""  # exact `iss`, e.g. https://accounts.google.com
    jwks_uri: str = ""  # e.g. https://www.googleapis.com/oauth2/v3/certs
    audience: str = ""  # the dedicated OAuth client id used for nothing else
    authorized_party: str = ""  # `azp`: the same dedicated client
    hosted_domain: str = ""  # `hd`: the reviewed Workspace domain
    tenant: str = ""  # reviewed: an ID token carries no tenant claim
    algorithms: tuple[str, ...] = ("RS256", "ES256")
    authorized_party_claim: str = "azp"
    hosted_domain_claim: str = "hd"
    max_lifetime_seconds: int = 3600
    clock_skew_seconds: int = 30

    def __post_init__(self) -> None:
        for name in (
            "policy_id",
            "issuer",
            "jwks_uri",
            "audience",
            "authorized_party",
            "hosted_domain",
            "tenant",
            "authorized_party_claim",
            "hosted_domain_claim",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"id-token subject policy {name} must be non-empty")
        _require_https_issuer(self.issuer)
        _require_https_endpoint(self.jwks_uri, "id-token subject JWKS URI")
        if not self.algorithms or not set(self.algorithms) <= {"RS256", "ES256"}:
            raise ValueError("id-token subject algorithms must be a subset of RS256/ES256")
        if not 1 <= self.max_lifetime_seconds <= 3600:
            raise ValueError("id-token subject max_lifetime_seconds must be from 1 to 3600")
        if not 0 <= self.clock_skew_seconds <= 60:
            raise ValueError("id-token subject clock_skew_seconds must be from 0 to 60")


@dataclass(frozen=True)
class EmbeddedGrantBffKeySettings:
    kid: str = ""
    algorithm: str = ""
    public_jwk: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.kid or self.algorithm not in {"RS256", "ES256"} or not self.public_jwk:
            raise ValueError("embedded-grant BFF key requires kid, RS256/ES256, and public_jwk")


@dataclass(frozen=True)
class EmbeddedGrantBffClientSettings:
    client_id: str = ""
    grant_endpoint_audience: str = ""
    permitted_scopes: tuple[str, ...] = ()
    allowed_subject_clients: tuple[str, ...] = ()
    keys: tuple[EmbeddedGrantBffKeySettings, ...] = ()
    max_lifetime_seconds: int = 60
    clock_skew_seconds: int = 30

    def __post_init__(self) -> None:
        if (
            not self.client_id
            or not self.grant_endpoint_audience
            or not self.permitted_scopes
            or not self.allowed_subject_clients
            or not self.keys
        ):
            raise ValueError("embedded-grant BFF client policy is incomplete")
        _require_https_endpoint(
            self.grant_endpoint_audience,
            "embedded-grant private_key_jwt audience",
        )
        if not 1 <= self.max_lifetime_seconds <= 60:
            raise ValueError("embedded-grant BFF assertion lifetime must be from 1 to 60")
        if not 0 <= self.clock_skew_seconds <= 60:
            raise ValueError("embedded-grant BFF assertion skew must be from 0 to 60")


@dataclass(frozen=True)
class EmbeddedGrantInstallationSettings:
    installation_id: str = ""
    subject_policy_id: str = ""
    subject_token_audience: str = ""
    subject_grant_scope: str = ""
    # Which RFC 8693 subject-token type THIS installation accepts. Reviewed per
    # installation so widening the request schema never widens what a deployment takes.
    subject_token_type: str = ACCESS_TOKEN_SUBJECT_TYPE
    bff_clients: tuple[EmbeddedGrantBffClientSettings, ...] = ()
    registration_lifetime_seconds: int = 120

    def __post_init__(self) -> None:
        if not all(
            (
                self.installation_id,
                self.subject_policy_id,
                self.subject_token_audience,
                self.subject_grant_scope,
                self.bff_clients,
            )
        ):
            raise ValueError("embedded-grant installation policy is incomplete")
        if self.subject_token_type not in SUBJECT_TOKEN_TYPES:
            raise ValueError("embedded-grant subject_token_type is not a reviewed token type")
        if self.subject_token_type == ACCESS_TOKEN_SUBJECT_TYPE:
            _require_https_endpoint(
                self.subject_token_audience,
                "embedded-grant broker subject-token audience",
            )
        if not 1 <= self.registration_lifetime_seconds <= 120:
            raise ValueError("embedded-grant registration lifetime must be from 1 to 120")


@dataclass(frozen=True)
class EmbedTokenKeySettings:
    kid: str = ""
    algorithm: str = ""
    public_key_env: str = ""
    private_key_env: str = ""
    kms_key_version: str = ""

    def __post_init__(self) -> None:
        if not self.kid or self.algorithm not in {"RS256", "ES256"} or not self.public_key_env:
            raise ValueError("embed-token key requires kid, RS256/ES256, and public_key_env")
        if self.private_key_env and self.kms_key_version:
            raise ValueError("embed-token key must use either private_key_env or kms_key_version")
        if self.kms_key_version and not self.kms_key_version.startswith("projects/"):
            raise ValueError("embed-token KMS key version must be a full resource name")


@dataclass(frozen=True)
class EmbedTokenSettings:
    issuer: str = ""
    audience: str = ""
    active_kid: str = ""
    keys: tuple[EmbedTokenKeySettings, ...] = ()
    lifetime_seconds: int = 300
    clock_skew_seconds: int = 30

    @property
    def configured(self) -> bool:
        return bool(self.issuer or self.audience or self.active_kid or self.keys)

    def __post_init__(self) -> None:
        if not self.configured:
            return
        if not self.issuer or not self.audience or not self.active_kid or not self.keys:
            raise ValueError("embedded-grant token issuer policy is incomplete")
        _require_https_issuer(self.issuer)
        active = tuple(key for key in self.keys if key.kid == self.active_kid)
        if len(active) != 1 or not (active[0].private_key_env or active[0].kms_key_version):
            raise ValueError(
                "embedded-grant active token key requires private_key_env or kms_key_version"
            )
        if not 1 <= self.lifetime_seconds <= 300:
            raise ValueError("embedded-grant token lifetime must be from 1 to 300")
        if not 0 <= self.clock_skew_seconds <= 30:
            raise ValueError("embedded-grant token skew must be from 0 to 30")


@dataclass(frozen=True)
class EmbeddedGrantSettings:
    subject_token_issuers: tuple[AccessTokenIssuerSettings, ...] = ()
    # The ID-token subject profile's own reviewed policies. Kept as a separate list
    # because the two profiles have different required inputs, not different values of
    # the same inputs; an installation selects one by ``subject_token_type``.
    id_token_subject_issuers: tuple[IdTokenSubjectIssuerSettings, ...] = ()
    installations: tuple[EmbeddedGrantInstallationSettings, ...] = ()
    token: EmbedTokenSettings = field(default_factory=EmbedTokenSettings)

    def subject_policy(
        self, installation: EmbeddedGrantInstallationSettings
    ) -> AccessTokenIssuerSettings | IdTokenSubjectIssuerSettings:
        """Resolve one installation's reviewed subject policy from its configured type."""
        policies: tuple[AccessTokenIssuerSettings | IdTokenSubjectIssuerSettings, ...] = (
            self.subject_token_issuers
            if installation.subject_token_type == ACCESS_TOKEN_SUBJECT_TYPE
            else self.id_token_subject_issuers
        )
        matches = tuple(
            policy for policy in policies if policy.policy_id == installation.subject_policy_id
        )
        if len(matches) != 1:
            raise ValueError(
                "embedded-grant installation subject policy does not resolve to exactly one "
                "policy of its configured subject token type"
            )
        return matches[0]


#: The one place ``CDD_IAP_TENANT_DOMAINS_JSON`` is read.
IAP_TENANT_DOMAINS_ENV = "CDD_IAP_TENANT_DOMAINS_JSON"


def resolve_iap_tenant_by_domain(configured: object) -> dict[str, str]:
    """Merge the reviewed IAP domain -> tenant map with its environment override.

    Read in three states, like every other security-relevant setting here. UNSET keeps what
    ``settings.yaml`` declares; SET AND EMPTY is an operator naming no mapping at all and must
    not silently inherit the shipped one, because for this map "empty" is the branch that turns
    the tenant back into whatever Google calls the sign-in domain.
    """

    def _clean(raw: object, source: str) -> dict[str, str]:
        if not raw:
            return {}
        if not isinstance(raw, dict):
            raise ValueError(f"{source} must be an object mapping identity domains to tenants")
        cleaned: dict[str, str] = {}
        for domain, tenant in raw.items():
            key = str(domain).strip().lower()
            value = str(tenant).strip()
            if not key or not value:
                raise ValueError(
                    f"{source} contains an empty domain or tenant; a blank domain would map the "
                    "identities that present NO domain onto a real tenant"
                )
            cleaned[key] = value
        return cleaned

    setting = read_env_setting(IAP_TENANT_DOMAINS_ENV)
    if setting.is_configured_empty:
        raise ValueError(
            f"{IAP_TENANT_DOMAINS_ENV} is set to an empty value, which names no mapping. Unset it "
            "to keep the reviewed map, or provide one."
        )
    if setting.is_unset:
        return _clean(configured, "identity.iap_tenant_by_domain")
    try:
        parsed = json.loads(setting.value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{IAP_TENANT_DOMAINS_ENV} must contain a JSON object") from exc
    return _clean(parsed, IAP_TENANT_DOMAINS_ENV)


#: The one place ``CDD_IAP_GROUPS_JSON`` is read.
IAP_GROUPS_ENV = "CDD_IAP_GROUPS_JSON"


def resolve_iap_groups_by_domain(configured: object) -> dict[str, tuple[str, ...]]:
    """Merge the reviewed IAP domain -> groups map with its environment override."""

    def _clean(raw: object, source: str) -> dict[str, tuple[str, ...]]:
        if not raw:
            return {}
        if not isinstance(raw, dict):
            raise ValueError(f"{source} must be an object mapping identity domains to groups")
        cleaned: dict[str, tuple[str, ...]] = {}
        for domain, groups in raw.items():
            key = str(domain).strip().lower()
            if not key:
                raise ValueError(f"{source} contains an empty domain")
            if isinstance(groups, str) or not isinstance(groups, (list, tuple)):
                raise ValueError(f"{source}[{domain!r}] must be a list of group principals")
            values = tuple(str(group).strip() for group in groups)
            if not values or any(not group for group in values):
                raise ValueError(
                    f"{source}[{domain!r}] must name at least one non-empty group; an empty list "
                    "grants nothing and is better written by omitting the domain"
                )
            cleaned[key] = values
        return cleaned

    setting = read_env_setting(IAP_GROUPS_ENV)
    if setting.is_configured_empty:
        raise ValueError(
            f"{IAP_GROUPS_ENV} is set to an empty value, which names no mapping. Unset it to keep "
            "the reviewed map, or provide one."
        )
    if setting.is_unset:
        return _clean(configured, "identity.iap_groups_by_domain")
    try:
        parsed = json.loads(setting.value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{IAP_GROUPS_ENV} must contain a JSON object") from exc
    return _clean(parsed, IAP_GROUPS_ENV)


@dataclass(frozen=True)
class IdentitySettings:
    """Implemented Mode 6 session-login identity configuration.

    ``trusted_issuers`` configures the Mode 6 caller described by
    :class:`IssuerSettings`. The agent signs its own session cookie with an HMAC key named by
    ``session_signing_key_env`` (read via ``os.environ.get`` at adapter-construction time,
    never stored here), and ``allowed_return_to_hosts`` is the open-redirect allowlist for
    the ``/auth/login``/``/auth/callback`` ``return_to`` parameter (empty = same-origin only).

    The corrected Modes 4/5 design adds explicit installation and access-token policy
    rather than overloading these Mode 6 fields.
    """

    mode: str = ""
    trusted_issuers: tuple[IssuerSettings, ...] = ()
    session_signing_key_env: str = "CDD_SESSION_SIGNING_KEY"
    # Env var NAMES holding retired-but-still-accepted signing keys (the rotation
    # window): tokens minted under a previous key keep verifying until they expire.
    session_accepted_key_envs: tuple[str, ...] = ()
    session_ttl_seconds: int = 3600
    allowed_return_to_hosts: tuple[str, ...] = ()
    access_token_issuers: tuple[AccessTokenIssuerSettings, ...] = ()
    # Reviewed immutable issuer-qualified subject links for the Mode 4/5 to Mode 6
    # citation continuation. Keys and values are canonical ``issub:`` actors. Email is
    # display-only and is never accepted as a linking key.
    citation_subject_links: dict[str, str] = field(default_factory=dict)
    embedded_grant: EmbeddedGrantSettings = field(default_factory=EmbeddedGrantSettings)
    #: Verified IAP identity domain -> reviewed tenant id.
    #:
    #: The IAP path used the assertion's ``hd`` claim AS the tenant, which is the one identity
    #: mode with no policy behind it while the refusal it raises says "did not resolve a
    #: policy-mapped tenant". Two things break on that. A machine identity carries no ``hd`` at
    #: all, so it resolved to nothing and was refused; and a human's Workspace domain is not the
    #: institution's tenant id, so the tenant this service stamped on evidence was whatever
    #: Google happened to call the sign-in domain.
    #:
    #: Empty keeps the old behaviour. Non-empty makes the map exhaustive: an unmapped domain
    #: resolves to NO tenant and the request is refused, rather than falling back to the domain.
    iap_tenant_by_domain: dict[str, str] = field(default_factory=dict)
    #: Verified IAP identity domain -> the groups that domain's members hold here.
    #:
    #: The IAP path built ``principals=("user:<subject>",)`` and nothing else, so an IAP identity
    #: held no case-access role and :func:`may_access_case` refused it every case. On a managed
    #: deployment that is not a restriction, it is a service nobody can use: every signed-in
    #: analyst was told they were "not entitled" to the case they had just named.
    #:
    #: The groups are the SAME strings the entitlement rules already use, so this grants nothing
    #: new; it states which verified domains hold them. Empty keeps the old behaviour, and an
    #: unmapped domain grants no group at all.
    iap_groups_by_domain: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Identity bindings are selected by ``mode``, never by the runtime profile.
    bindings: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelSettings:
    """Browser delivery channel, independent of runtime and identity."""

    mode: str = ""
    public_origin: str = ""
    public_mount_path: str = PUBLIC_MOUNT_PATH
    installation_manifest: str = ""
    manifest_version: str = ""
    # Structural verifier policies are required for sandboxed identities that do not
    # use ``access_token_issuers`` directly (notably Mode 5). They bind the exact
    # manifest to independently reviewed tenant/client/scope policy at startup.
    verifier_policies: tuple[ManifestVerifierSettings, ...] = ()


@dataclass(frozen=True)
class ManifestVerifierSettings:
    """Non-secret startup binding for one sandboxed installation issuer policy."""

    policy_id: str = ""
    enabled: bool = True
    identity_mode: str = ""
    credential_type: str = ""
    issuer: str = ""
    resource_audience: str = ""
    tenant: str = ""
    allowed_clients: tuple[str, ...] = ()
    permitted_scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "policy_id",
            "identity_mode",
            "credential_type",
            "issuer",
            "resource_audience",
            "tenant",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"manifest verifier policy {name} must be non-empty")
        if self.identity_mode not in {"oauth-access-token", "embedded-grant"}:
            raise ValueError("manifest verifier policy identity_mode is not sandboxed")
        expected_credential = {
            "oauth-access-token": "access-token",
            "embedded-grant": "subject-access-token",
        }[self.identity_mode]
        if self.credential_type != expected_credential:
            raise ValueError("manifest verifier policy credential_type is incompatible")
        _require_https_issuer(self.issuer)
        if not self.allowed_clients or not self.permitted_scopes:
            raise ValueError(
                "manifest verifier policy requires allowed_clients and permitted_scopes"
            )


@dataclass(frozen=True)
class PiiSettings:
    """Which jurisdictions' national-identifier patterns the redaction boundary applies.

    Drives the local regex redactor (and, via ``CDD_PII_JURISDICTIONS``, the eval gate) so
    a non-SG deployment detects its own identifiers. Default is the build's home region.
    """

    jurisdictions: tuple[str, ...] = ("SG",)


@dataclass(frozen=True)
class DeploymentSettings:
    """Legacy control-ownership settings, not the browser-channel profile.

    Current code uses ``standalone`` to combine auth-route mounting and ownership of
    guardrail/DLP/WORM resources. The corrected embedding design deprecates that
    conflation in favour of explicit channel, identity, adapter, and infrastructure
    settings. See ``docs/embedding-implementation-plan.md`` Phase 1.
    """

    standalone: bool = True
    production: bool = False
    replica_count: int = 1

    def __post_init__(self) -> None:
        if self.replica_count < 1:
            raise ValueError("deployment.replica_count must be at least 1")


@dataclass(frozen=True)
class WebSecuritySettings:
    """Validated application-edge posture, loaded once with the deployment settings."""

    cors_origins: tuple[str, ...] = ()
    # Was the CORS allowlist CONFIGURED, as opposed to merely left empty? The two are
    # different answers: an unconfigured allowlist lets the localhost dev fallback apply
    # under a deliberately chosen local-persona run, while a configured-but-empty one
    # REFUSES every cross-origin request. Without this flag ``CDD_CORS_ORIGINS=""`` (an
    # operator deleting the allowlist) silently reopened the dev origins.
    cors_origins_configured: bool = False
    frame_ancestors: tuple[str, ...] = ("'self'",)
    # Zero derives the request ceiling from runtime; -1 derives the rate from identity.
    max_body_bytes: int = 0
    rate_limit_per_minute: int = -1

    def __post_init__(self) -> None:
        # Any entry CONTAINING an asterisk, not only an entry that IS one. A membership test
        # catches the bare "*" and lets "https://*.example" through, because the
        # origin validator below sees a well-formed https origin with an authority and no path.
        # CSP honours that host-source form, so every subdomain could frame the console,
        # including one obtained by takeover or serving user content. A real origin never
        # contains the character, so this refuses nothing a deployment could correctly hold.
        wildcards = [
            entry
            for entry in (*self.cors_origins, *self.frame_ancestors)
            if "*" in entry or entry in _WILDCARD_TOKENS
        ]
        if wildcards:
            raise ValueError(
                f"web security origin policy must never contain a wildcard, got {wildcards}"
            )
        for origin in self.cors_origins:
            _require_public_origin(origin, "web.cors_origins")
        for ancestor in self.frame_ancestors:
            if ancestor != "'self'":
                _require_public_origin(ancestor, "web.frame_ancestors")
        if not self.frame_ancestors:
            raise ValueError("web.frame_ancestors must not be empty")
        if self.max_body_bytes < 0:
            raise ValueError("web.max_body_bytes must be zero or positive")
        if self.rate_limit_per_minute < -1:
            raise ValueError("web.rate_limit_per_minute must be -1, zero, or positive")


@dataclass(frozen=True)
class CaseStoreSettings:
    """Managed SoW case store (Firestore Native) — durable, regional, CMEK-encrypted.

    ``database`` names the Firestore database (a project holds one immutable ``(default)``
    plus optional named databases); ``collection`` is the top-level document collection
    holding one ``sow_cases/{case_id}`` document per case.
    """

    collection: str = "sow_cases"
    database: str = "(default)"


@dataclass(frozen=True)
class MonitoringStoreSettings:
    """Managed perpetual-KYC store (Firestore Native) — baselines + the review queue.

    ``database`` names the Firestore database (empty reuses the SoW case store's, which is
    the usual single-database deployment); the two collections hold one document per
    subject: the last accepted baseline and the current queued assessment.
    """

    database: str = ""
    baselines_collection: str = "pkyc_baselines"
    assessments_collection: str = "pkyc_assessments"

    def __post_init__(self) -> None:
        for value in (self.baselines_collection, self.assessments_collection):
            if not value or "/" in value:
                raise ValueError("perpetual-KYC collection names must be path segments")
        if "/" in self.database:
            raise ValueError("perpetual-KYC database must be a single path segment")


@dataclass(frozen=True)
class BrowserFlowStoreSettings:
    """Shared Firestore collections for browser flows, outbox, citations and replay."""

    database: str = "sow-cases"
    records_collection: str = "browser_flows"
    aliases_collection: str = "browser_flow_aliases"
    outbox_collection: str = "browser_flow_outbox"
    citations_collection: str = "cdd_citation_ledger"
    replay_collection: str = "client_assertion_replay"
    rate_limits_collection: str = "embed_rate_limits"
    rate_limit_max_attempts: int = 600
    rate_limit_window_seconds: int = 60

    def __post_init__(self) -> None:
        values = (
            self.database,
            self.records_collection,
            self.aliases_collection,
            self.outbox_collection,
            self.citations_collection,
            self.replay_collection,
            self.rate_limits_collection,
        )
        if any(not value or "/" in value for value in values):
            raise ValueError("browser-flow Firestore names must be non-empty path segments")
        if self.rate_limit_max_attempts < 100:
            raise ValueError("shared embed rate limit must allow at least 100 attempts")
        if not 1 <= self.rate_limit_window_seconds <= 3600:
            raise ValueError("shared embed rate-limit window must be from 1 to 3600 seconds")


@dataclass(frozen=True)
class Settings:
    project_id: str = PLACEHOLDER_PROJECT_ID
    region: str = DEFAULT_GCP_REGION
    # local (default, SDK-free) | live (real local models + cloud web grounding) | gcp |
    # platform | onprem
    profile: str = "local"
    # Was the profile CHOSEN, or merely inherited because nothing named one? ``load`` sets
    # this False when neither ``CDD_PROFILE`` nor the settings file's ``profile:`` key names
    # a profile. Direct construction is deliberate by definition (a caller named it in code),
    # so the default is True. See :attr:`exposure_identity_mode` for what it gates.
    profile_explicit: bool = True
    kms_key: str = ""  # projects/.../cryptoKeys/... (regional)
    grounding_enabled: bool = False
    models: ModelSettings = field(default_factory=ModelSettings)
    document_ai: DocumentAiSettings = field(default_factory=DocumentAiSettings)
    knowledge_base: KnowledgeBaseSettings = field(default_factory=KnowledgeBaseSettings)
    model_armor: ModelArmorSettings = field(default_factory=ModelArmorSettings)
    dlp: DlpSettings = field(default_factory=DlpSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    agent_engine: AgentEngineSettings = field(default_factory=AgentEngineSettings)
    local: LocalSettings = field(default_factory=LocalSettings)
    live: LiveSettings = field(default_factory=LiveSettings)
    document_store: DocumentStoreSettings = field(default_factory=DocumentStoreSettings)
    sanctions: SanctionsSettings = field(default_factory=SanctionsSettings)
    identity: IdentitySettings = field(default_factory=IdentitySettings)
    channel: ChannelSettings = field(default_factory=ChannelSettings)
    pii: PiiSettings = field(default_factory=PiiSettings)
    case_store: CaseStoreSettings = field(default_factory=CaseStoreSettings)
    monitoring: MonitoringStoreSettings = field(default_factory=MonitoringStoreSettings)
    browser_flow_store: BrowserFlowStoreSettings = field(default_factory=BrowserFlowStoreSettings)
    deployment: DeploymentSettings = field(default_factory=DeploymentSettings)
    web: WebSecuritySettings = field(default_factory=WebSecuritySettings)
    # Bank-owned risk policy (weights, tolerances, cadences, country lists). Domain
    # dataclasses (pure stdlib) parsed from the settings ``policy:`` section; the
    # defaults reproduce the reference build's historical constants exactly.
    policy: RiskPolicy = field(default_factory=RiskPolicy)
    # port_name -> { profile -> "module.path:ClassName" }
    adapters: dict[str, dict[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Document AI may sit in the deploy region, or in a NAMED MULTI-REGION as a stated
        # deviation, and in nothing else. Singapore is "limited support" for Document AI and
        # access is gated behind Google's Single Region Request Form, so until that is granted
        # the bytes are extracted in the `us` multi-region while the rest of the stack stays in
        # region. That is a disclosed residency deviation, not a widening: a multi-region names
        # one jurisdiction and carries an ML-processing commitment for it.
        #
        # `global` is refused by name because it names NO jurisdiction, and it is precisely what
        # someone reaches for to make an apply succeed. A different single region is refused too:
        # it is neither the deploy region nor a multi-region commitment. `infra/terraform`
        # validates `docai_location` by the same rule; this is the runtime half of it, so
        # setting CDD_DOCAI_LOCATION cannot reach a location the processor half refused. The
        # deploy region itself is a runtime input here (GCP_REGION), so the guard compares
        # against the configured region rather than pinning one.
        if self.document_ai.location not in {self.region, *_DOCUMENT_AI_MULTI_REGIONS}:
            raise ValueError(
                f"Document AI location {self.document_ai.location!r} must be the deploy region "
                f"({self.region}) or a named multi-region "
                f"({', '.join(sorted(_DOCUMENT_AI_MULTI_REGIONS))}). `global` names no "
                "jurisdiction and is never acceptable here."
            )

    @property
    def identity_mode(self) -> str:
        """Exact identity selector, with one-release compatibility inference."""
        explicit = self.identity.mode.strip()
        if explicit:
            if explicit not in IDENTITY_MODES:
                allowed = ", ".join(sorted(IDENTITY_MODES))
                raise ValueError(
                    f"unknown CDD_IDENTITY_PROFILE {explicit!r}; expected one of: {allowed}"
                )
            return explicit
        compatibility = {
            "local": "local-persona",
            "gcp": "iap",
            "platform": "iap",
            "onprem": "onprem",
        }
        inferred = compatibility.get(self.profile)
        if inferred is None:
            if self.profile == "oidc-session":
                # Reachable only through direct Settings construction; ``load`` now refuses
                # this value at resolution time with the same wording.
                raise ValueError(_OIDC_SESSION_MIGRATION)
            raise ValueError(
                f"CDD_IDENTITY_PROFILE is required for runtime profile {self.profile!r}; "
                "live never infers local-persona or a managed identity"
            )
        return inferred

    @property
    def identity_mode_explicit(self) -> bool:
        """Was the identity mode CHOSEN, or inherited from a profile nobody named?

        True when ``CDD_IDENTITY_PROFILE`` (or the settings file's ``identity.mode``) names a
        mode, and also when the runtime profile itself was named: inferring ``local-persona``
        from a deliberately chosen ``local`` profile is a choice. It is False only in the one
        case where nothing was chosen at all, which is exactly the case that must not be
        indistinguishable from choosing the no-auth dev posture.
        """
        return bool(self.identity.mode.strip()) or self.profile_explicit

    @property
    def exposure_identity_mode(self) -> str:
        """The identity mode every RELAXATION keys off, never the raw :attr:`identity_mode`.

        The relaxations grant something extra to ``local-persona`` (the ``X-Dev-Persona``
        header, the localhost CORS fallback, the disabled rate limit), so an unconsented run
        must NOT look like ``local-persona``: it gets :data:`UNCONSENTED_IDENTITY_MODE`, which
        is no mode's relaxation.

        The RESTRICTIONS keep reading :attr:`identity_mode`, and must: the bind guard confines
        ``local-persona`` to loopback, so there an unconsented run has to look like the local
        posture to stay confined. A relaxation and a restriction fail closed in OPPOSITE
        directions, which is why one derived string cannot serve both.
        """
        mode = self.identity_mode
        if mode == "local-persona" and not self.identity_mode_explicit:
            return UNCONSENTED_IDENTITY_MODE
        return mode

    @property
    def channel_mode(self) -> str:
        """Exact channel selector; only the legacy local default is inferred."""
        explicit = self.channel.mode.strip()
        if explicit:
            if explicit not in CHANNEL_MODES:
                allowed = ", ".join(sorted(CHANNEL_MODES))
                raise ValueError(
                    f"unknown CDD_CHANNEL_PROFILE {explicit!r}; expected one of: {allowed}"
                )
            return explicit
        if self.profile == "local" and self.identity_mode == "local-persona":
            return "standalone"
        raise ValueError(
            "CDD_CHANNEL_PROFILE is required; choose native, sandboxed, or standalone "
            "independently of CDD_PROFILE and CDD_IDENTITY_PROFILE"
        )

    @property
    def deprecated_control_ownership(self) -> str:
        return "application" if self.deployment.standalone else "platform"

    @property
    def public_callback_uri(self) -> str:
        origin = self.channel.public_origin.rstrip("/")
        if not origin:
            raise ValueError(
                "channel.public_origin (CDD_PUBLIC_ORIGIN) is required for oidc-session"
            )
        return f"{origin}{self.channel.public_mount_path}/auth/callback"

    def configuration_hash(self) -> str:
        """Stable, safe hash of deployment selectors and public policy, never secrets."""
        payload = {
            "runtime": self.profile,
            "identity": self.identity_mode,
            "channel": self.channel_mode,
            "region": self.region,
            "public_origin": self.channel.public_origin,
            "public_mount_path": self.channel.public_mount_path,
            "manifest_version": self.channel.manifest_version,
            "manifest_path_configured": bool(self.channel.installation_manifest),
            "production": self.deployment.production,
            "replica_count": self.deployment.replica_count,
            "control_ownership": self.deprecated_control_ownership,
            "cors_origins": self.web.cors_origins,
            "frame_ancestors": self.web.frame_ancestors,
            "max_body_bytes": self.web.max_body_bytes,
            "rate_limit_per_minute": self.web.rate_limit_per_minute,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def installation_manifest(self):
        """Load and bind the exact sandbox manifest to reviewed verifier policy."""
        from .embedding.manifest import VerifierPolicy, load_installation_manifest

        identity_mode = self.identity_mode
        configured = tuple(
            policy
            for policy in self.channel.verifier_policies
            if policy.identity_mode == identity_mode
        )
        if configured:
            policies = tuple(
                VerifierPolicy(
                    policy_id=policy.policy_id,
                    enabled=policy.enabled,
                    identity_mode=policy.identity_mode,
                    credential_type=policy.credential_type,
                    issuer=policy.issuer,
                    resource_audience=policy.resource_audience,
                    tenant=policy.tenant,
                    allowed_clients=policy.allowed_clients,
                    permitted_scopes=policy.permitted_scopes,
                )
                for policy in configured
            )
        elif identity_mode == "oauth-access-token":
            policies = tuple(
                VerifierPolicy(
                    policy_id=policy.policy_id,
                    enabled=True,
                    identity_mode="oauth-access-token",
                    credential_type="access-token",
                    issuer=policy.issuer,
                    resource_audience=policy.resource_audience,
                    tenant=policy.tenant,
                    allowed_clients=policy.allowed_clients,
                    permitted_scopes=policy.required_scopes,
                )
                for policy in self.identity.access_token_issuers
            )
        else:
            policies = ()
        if not policies:
            raise ValueError(
                f"sandboxed identity {identity_mode!r} requires reviewed manifest verifier policies"
            )
        tenants = {policy.tenant for policy in policies}
        if len(tenants) != 1:
            raise ValueError("manifest verifier policies must share one deployment tenant")
        loaded = load_installation_manifest(
            self.channel.installation_manifest,
            expected_tenant=next(iter(tenants)),
            expected_identity_mode=identity_mode,
            verifier_policies=policies,
        )
        expected_digest = optional_setting("CDD_EXPECTED_MANIFEST_SHA256") or ""
        if self.deployment.production and not expected_digest:
            raise ValueError("production requires CDD_EXPECTED_MANIFEST_SHA256")
        if expected_digest and not hmac.compare_digest(loaded.sha256, expected_digest):
            raise ValueError("installation manifest digest does not match reviewed deployment")
        if any(
            installation.public_origin != self.channel.public_origin
            for installation in loaded.manifest.installations
        ):
            raise ValueError(
                "installation manifest public origin does not match channel.public_origin"
            )
        return loaded

    def validate_deployment(self) -> None:
        """Fail closed on incomplete bindings and unreviewed selector combinations."""
        if self.profile == "oidc-session":
            # Keep the migration error exact even if identity.mode is also malformed.
            _ = self.identity_mode
        if self.profile not in RUNTIME_PROFILES:
            allowed = ", ".join(sorted(RUNTIME_PROFILES))
            raise ValueError(f"unknown CDD_PROFILE {self.profile!r}; expected one of: {allowed}")

        identity_mode = self.identity_mode
        channel_mode = self.channel_mode
        if identity_mode not in CHANNEL_IDENTITY_MATRIX[channel_mode]:
            raise ValueError(
                f"unreviewed channel/identity combination: channel={channel_mode!r}, "
                f"identity={identity_mode!r}"
            )
        if identity_mode not in self.identity.bindings:
            raise ValueError(f"identity mode {identity_mode!r} is not enabled in identity.bindings")

        if self.profile in MANAGED_PROFILES and self.project_id == PLACEHOLDER_PROJECT_ID:
            # The documented placeholder is exactly right for a laptop, where nothing calls a
            # cloud API, and is a live defect anywhere a cloud SDK is bound. Unvalidated it
            # travelled all the way into a real request: the deployed agent answered 500 with
            # "projects/your-gcp-project does not exist" on the first dossier build, which reads
            # as a broken service rather than an unset GOOGLE_CLOUD_PROJECT. Refusing at load
            # turns a runtime 500 into a boot failure that names the variable.
            raise ValueError(
                f"project_id is still the documented placeholder {PLACEHOLDER_PROJECT_ID!r} in "
                f"the {self.profile!r} profile, where every adapter calls a real Google Cloud "
                "API. Set GOOGLE_CLOUD_PROJECT to the deployment project."
            )

        actual_ports = frozenset(self.adapters)
        if actual_ports != RUNTIME_ADAPTER_PORTS:
            missing_ports = sorted(RUNTIME_ADAPTER_PORTS - actual_ports)
            unexpected_ports = sorted(actual_ports - RUNTIME_ADAPTER_PORTS)
            details = []
            if missing_ports:
                details.append(f"missing: {', '.join(missing_ports)}")
            if unexpected_ports:
                details.append(f"unexpected: {', '.join(unexpected_ports)}")
            raise ValueError(
                "runtime adapter map must contain the exact required port set; "
                + "; ".join(details)
            )

        for port_name, bindings in self.adapters.items():
            missing = sorted(RUNTIME_PROFILES - bindings.keys())
            if missing:
                raise ValueError(
                    f"runtime adapter map is incomplete for port {port_name!r}; "
                    f"missing explicit bindings: {', '.join(missing)}"
                )

        if self.channel.public_mount_path != PUBLIC_MOUNT_PATH:
            raise ValueError(
                f"channel.public_mount_path is fixed at {PUBLIC_MOUNT_PATH!r}; "
                "varying it would require a different UI artifact"
            )
        if channel_mode == "sandboxed":
            manifest = Path(self.channel.installation_manifest)
            if not self.channel.manifest_version or not manifest.is_file():
                raise ValueError(
                    "sandboxed channel requires a readable installation manifest and "
                    "non-empty manifest_version"
                )
            _require_public_origin(self.channel.public_origin, "sandboxed channel")
            self.installation_manifest()
        if identity_mode == "oidc-session":
            if channel_mode != "standalone":
                raise ValueError("oidc-session is allowed only on the standalone channel")
            _require_public_origin(self.channel.public_origin, "oidc-session")
            if not self.identity.trusted_issuers:
                raise ValueError("oidc-session requires at least one trusted OIDC issuer")
        if identity_mode == "oauth-access-token":
            if not self.identity.access_token_issuers:
                raise ValueError(
                    "oauth-access-token requires at least one reviewed access-token issuer policy"
                )
            policy_ids = [policy.policy_id for policy in self.identity.access_token_issuers]
            if len(set(policy_ids)) != len(policy_ids):
                raise ValueError("access-token issuer policy_id values must be unique")
            if not self.channel.installation_manifest or not self.channel.manifest_version:
                raise ValueError(
                    "oauth-access-token requires an installation manifest and manifest_version"
                )
            policies = self.identity.access_token_issuers
            tenants = {policy.tenant for policy in policies}
            if len(tenants) != 1:
                raise ValueError("access-token policies must share one deployment tenant")
        browser_flow_binding = self.adapters.get("browser_flow_store", {}).get(self.profile, "")
        if channel_mode == "sandboxed" and (
            not browser_flow_binding or "disabled.browser_flow_store" in browser_flow_binding
        ):
            raise ValueError(
                "sandboxed channel requires an enabled shared BrowserFlowStorePort adapter"
            )
        if "local.browser_flow_store" in browser_flow_binding:
            if self.deployment.production:
                raise ValueError("local SQLite browser-flow store is disabled in production")
            if self.deployment.replica_count != 1:
                raise ValueError("local SQLite browser-flow store requires exactly one replica")
            if channel_mode == "sandboxed" and not self.local.browser_flow_path:
                raise ValueError("sandboxed local proof requires local.browser_flow_path")
        if identity_mode == "embedded-grant":
            grant = self.identity.embedded_grant
            if (
                not (grant.subject_token_issuers or grant.id_token_subject_issuers)
                or not grant.installations
                or not grant.token.configured
            ):
                raise ValueError(
                    "embedded-grant requires subject-token, installation/BFF, and "
                    "dedicated embed-token policies"
                )
            loaded = self.installation_manifest()
            manifest_ids = {
                installation.installation_id for installation in loaded.manifest.installations
            }
            configured_ids = {installation.installation_id for installation in grant.installations}
            if len(configured_ids) != len(grant.installations) or configured_ids != manifest_ids:
                raise ValueError(
                    "embedded-grant installation policies must exactly match the manifest"
                )
            all_subject_policies: tuple[
                AccessTokenIssuerSettings | IdTokenSubjectIssuerSettings, ...
            ] = (*grant.subject_token_issuers, *grant.id_token_subject_issuers)
            subject_ids = {policy.policy_id for policy in all_subject_policies}
            if len(subject_ids) != len(all_subject_policies):
                raise ValueError("embedded-grant subject policy_id values must be unique")
            expected_grant_audience = (
                self.channel.public_origin.rstrip("/")
                + self.channel.public_mount_path
                + "/api/v1/embed/grants"
            )
            for configured_installation in grant.installations:
                installation = loaded.manifest.resolve(configured_installation.installation_id)
                subject_policy = grant.subject_policy(configured_installation)
                configured_audience = (
                    subject_policy.resource_audience
                    if isinstance(subject_policy, AccessTokenIssuerSettings)
                    else subject_policy.audience
                )
                if configured_audience != configured_installation.subject_token_audience:
                    raise ValueError(
                        "embedded-grant broker subject audience must remain distinct and exact"
                    )
                if configured_installation.subject_token_audience == installation.resource_audience:
                    raise ValueError(
                        "broker subject-token audience must be distinct from Doc1 resource audience"
                    )
                if subject_policy.tenant != installation.tenant:
                    raise ValueError(
                        "embedded-grant subject policy tenant does not match installation"
                    )
                bff_ids = {client.client_id for client in configured_installation.bff_clients}
                if len(bff_ids) != len(configured_installation.bff_clients):
                    raise ValueError("embedded-grant BFF clients must be unique")
                if not bff_ids.issubset(set(installation.allowed_clients)):
                    raise ValueError("embedded-grant BFF clients are not allowed by the manifest")
                for client in configured_installation.bff_clients:
                    if client.grant_endpoint_audience != expected_grant_audience:
                        raise ValueError(
                            "private_key_jwt audience must equal the canonical grant endpoint"
                        )
            if any(
                installation.resource_audience != grant.token.audience
                for installation in loaded.manifest.installations
            ):
                raise ValueError(
                    "embedded-grant token audience must equal every Doc1 resource audience"
                )
            for key in grant.token.keys:
                if optional_setting(key.public_key_env) is None:
                    raise ValueError("embedded-grant token public verification key is unavailable")
                if key.kid == grant.token.active_kid:
                    if key.kms_key_version:
                        if self.profile not in {"gcp", "platform"}:
                            raise ValueError(
                                "embedded-grant KMS signing is supported only by managed profiles"
                            )
                        if f"/locations/{self.region}/" not in key.kms_key_version:
                            raise ValueError(
                                "embedded-grant KMS signing key must use the deployment region"
                            )
                    elif not key.private_key_env or optional_setting(key.private_key_env) is None:
                        raise ValueError("embedded-grant active private signing key is unavailable")
                    if self.deployment.production and not key.kms_key_version:
                        raise ValueError(
                            "production embedded-grant signing requires a managed KMS key version"
                        )
            if self.profile == "local" and not self.local.client_assertion_replay_path:
                raise ValueError(
                    "embedded-grant local proof requires local.client_assertion_replay_path"
                )
        if not self.deployment.standalone and identity_mode in {
            "local-persona",
            "oidc-session",
        }:
            raise ValueError(
                "CDD_STANDALONE=false assigns identity control to the platform and conflicts "
                f"with application-owned identity mode {identity_mode!r}"
            )

    @staticmethod
    def load(
        path: str | os.PathLike[str] | None = None,
        *,
        exact_bytes: bytes | None = None,
    ) -> Settings:
        """Load settings from a path or exact in-memory bytes through one contract."""
        if path is not None and exact_bytes is not None:
            raise ValueError("provide either a settings path or exact_bytes, not both")
        if exact_bytes is None:
            resolved_path = Path(path or setting_or_default("CDD_SETTINGS", "config/settings.yaml"))
            exact_bytes = resolved_path.read_bytes() if resolved_path.exists() else None
        if exact_bytes is not None:
            expected_digest = optional_setting("CDD_EXPECTED_SETTINGS_SHA256") or ""
            actual_digest = hashlib.sha256(exact_bytes).hexdigest()
            if expected_digest and not hmac.compare_digest(actual_digest, expected_digest):
                raise ValueError("runtime settings digest does not match reviewed deployment")
            raw = _interpolate(yaml.safe_load(exact_bytes.decode("utf-8")))
        else:
            raw = {}
        raw = raw or {}
        deployment_raw = raw.get("deployment", {})
        production_value = (
            deployment_raw.get("production", False) if isinstance(deployment_raw, dict) else False
        )
        production_requested = production_value is True or (
            isinstance(production_value, str)
            and production_value.strip().lower() in {"1", "true", "yes", "on"}
        )
        if production_requested:
            if optional_setting("CDD_EXPECTED_SETTINGS_SHA256") is None:
                raise ValueError("production requires CDD_EXPECTED_SETTINGS_SHA256")
            if optional_setting("CDD_EXPECTED_MANIFEST_SHA256") is None:
                raise ValueError("production requires CDD_EXPECTED_MANIFEST_SHA256")

        def _section(cls: type, key: str) -> Any:
            return cls(**_coerce_scalars(cls, raw.pop(key, {}) or {}))

        def _issuer(raw_issuer: dict[str, Any]) -> IssuerSettings:
            raw_issuer = dict(raw_issuer)
            for tuple_field in ("algorithms", "allowed_endpoint_hosts"):
                if tuple_field in raw_issuer:
                    raw_issuer[tuple_field] = tuple(raw_issuer[tuple_field])
            issuer = IssuerSettings(**_coerce_scalars(IssuerSettings, raw_issuer))
            _require_https_issuer(issuer.issuer)
            return issuer

        def _access_token_issuer(
            raw_issuer: dict[str, Any],
        ) -> AccessTokenIssuerSettings:
            raw_issuer = dict(raw_issuer)
            for tuple_field in ("algorithms", "allowed_clients", "required_scopes"):
                if tuple_field in raw_issuer:
                    raw_issuer[tuple_field] = tuple(raw_issuer[tuple_field])
            return AccessTokenIssuerSettings(
                **_coerce_scalars(AccessTokenIssuerSettings, raw_issuer)
            )

        def _id_token_subject_issuer(
            raw_issuer: dict[str, Any],
        ) -> IdTokenSubjectIssuerSettings:
            raw_issuer = dict(raw_issuer)
            if "algorithms" in raw_issuer:
                raw_issuer["algorithms"] = tuple(raw_issuer["algorithms"])
            return IdTokenSubjectIssuerSettings(
                **_coerce_scalars(IdTokenSubjectIssuerSettings, raw_issuer)
            )

        def _embedded_grant(raw_grant: dict[str, Any]) -> EmbeddedGrantSettings:
            raw_grant = dict(raw_grant)
            subject_policies = tuple(
                _access_token_issuer(policy)
                for policy in raw_grant.pop("subject_token_issuers", []) or []
            )
            id_token_policies = tuple(
                _id_token_subject_issuer(policy)
                for policy in raw_grant.pop("id_token_subject_issuers", []) or []
            )
            installations: list[EmbeddedGrantInstallationSettings] = []
            for raw_installation in raw_grant.pop("installations", []) or []:
                raw_installation = dict(raw_installation)
                bff_clients: list[EmbeddedGrantBffClientSettings] = []
                for raw_client in raw_installation.pop("bff_clients", []) or []:
                    raw_client = dict(raw_client)
                    keys = tuple(
                        EmbeddedGrantBffKeySettings(**dict(raw_key))
                        for raw_key in raw_client.pop("keys", []) or []
                    )
                    for tuple_field in ("permitted_scopes", "allowed_subject_clients"):
                        if tuple_field in raw_client:
                            raw_client[tuple_field] = tuple(raw_client[tuple_field])
                    bff_clients.append(
                        EmbeddedGrantBffClientSettings(
                            keys=keys,
                            **_coerce_scalars(
                                EmbeddedGrantBffClientSettings,
                                raw_client,
                            ),
                        )
                    )
                installations.append(
                    EmbeddedGrantInstallationSettings(
                        bff_clients=tuple(bff_clients),
                        **_coerce_scalars(
                            EmbeddedGrantInstallationSettings,
                            raw_installation,
                        ),
                    )
                )
            raw_token = dict(raw_grant.pop("token", {}) or {})
            token_keys = tuple(
                EmbedTokenKeySettings(**dict(raw_key))
                for raw_key in raw_token.pop("keys", []) or []
            )
            token = EmbedTokenSettings(
                keys=token_keys,
                **_coerce_scalars(EmbedTokenSettings, raw_token),
            )
            if raw_grant:
                raise ValueError(f"unknown embedded_grant settings: {sorted(raw_grant)}")
            return EmbeddedGrantSettings(
                subject_token_issuers=subject_policies,
                id_token_subject_issuers=id_token_policies,
                installations=tuple(installations),
                token=token,
            )

        pii_raw = dict(raw.pop("pii", {}) or {})
        if "jurisdictions" in pii_raw:
            pii_raw["jurisdictions"] = tuple(pii_raw["jurisdictions"])

        doc_store_raw = dict(raw.pop("document_store", {}) or {})
        if "allowed_mime_types" in doc_store_raw:
            doc_store_raw["allowed_mime_types"] = tuple(doc_store_raw["allowed_mime_types"])

        web_raw = dict(raw.pop("web", {}) or {})
        cors_setting = read_env_setting("CDD_CORS_ORIGINS")
        if not cors_setting.is_unset:
            # Three-state: unset leaves the allowlist unconfigured (the localhost dev
            # fallback may still apply under a deliberately chosen local-persona run);
            # set-and-empty configures an EMPTY allowlist, which refuses every cross-origin
            # request; set-and-valid configures exactly those origins.
            web_raw["cors_origins"] = tuple(
                origin.strip() for origin in cors_setting.value.split(",") if origin.strip()
            )
            web_raw["cors_origins_configured"] = True
        elif web_raw.get("cors_origins"):
            # The settings file ALWAYS carries the key, so its emptiness carries no signal:
            # ``cors_origins: []`` is the shipped placeholder, not a decision. Only a
            # non-empty list here configures the allowlist. The environment variable is the
            # channel that can express "configured and empty", because it is absent unless
            # someone sets it.
            web_raw["cors_origins"] = tuple(web_raw["cors_origins"])
            web_raw["cors_origins_configured"] = True
        else:
            web_raw.pop("cors_origins", None)
        frame_setting = read_env_setting("CDD_FRAME_ANCESTORS")
        if not frame_setting.is_unset:
            web_raw["frame_ancestors"] = tuple(
                ancestor for ancestor in frame_setting.value.split() if ancestor
            )
        elif "frame_ancestors" in web_raw:
            web_raw["frame_ancestors"] = tuple(web_raw["frame_ancestors"])

        identity_raw = raw.pop("identity", {}) or {}
        identity_override = optional_setting("CDD_IDENTITY_PROFILE")
        if identity_override is not None:
            identity_raw["mode"] = identity_override
        embedded_grant = _embedded_grant(identity_raw.pop("embedded_grant", {}) or {})
        trusted_issuers = tuple(_issuer(i) for i in identity_raw.pop("trusted_issuers", []) or [])
        access_token_issuers = tuple(
            _access_token_issuer(i) for i in identity_raw.pop("access_token_issuers", []) or []
        )
        for _tuple_field in ("allowed_return_to_hosts", "session_accepted_key_envs"):
            if _tuple_field in identity_raw:
                identity_raw[_tuple_field] = tuple(identity_raw[_tuple_field])
        identity_raw = _coerce_scalars(IdentitySettings, identity_raw)
        channel_raw = dict(raw.pop("channel", {}) or {})
        channel_override = optional_setting("CDD_CHANNEL_PROFILE")
        if channel_override is not None:
            channel_raw["mode"] = channel_override
        verifier_policies = []
        for raw_policy in channel_raw.pop("verifier_policies", []) or []:
            raw_policy = dict(raw_policy)
            for tuple_field in ("allowed_clients", "permitted_scopes"):
                if tuple_field in raw_policy:
                    raw_policy[tuple_field] = tuple(raw_policy[tuple_field])
            verifier_policies.append(
                ManifestVerifierSettings(**_coerce_scalars(ManifestVerifierSettings, raw_policy))
            )
        nested: dict[str, Any] = {
            "models": _section(ModelSettings, "models"),
            "document_ai": _section(DocumentAiSettings, "document_ai"),
            "knowledge_base": _section(KnowledgeBaseSettings, "knowledge_base"),
            "model_armor": _section(ModelArmorSettings, "model_armor"),
            "dlp": _section(DlpSettings, "dlp"),
            "logging": _section(LoggingSettings, "logging"),
            "agent_engine": _section(AgentEngineSettings, "agent_engine"),
            "local": _section(LocalSettings, "local"),
            "live": _section(LiveSettings, "live"),
            "document_store": DocumentStoreSettings(
                **_coerce_scalars(DocumentStoreSettings, doc_store_raw)
            ),
            "sanctions": _section(SanctionsSettings, "sanctions"),
            "identity": IdentitySettings(
                trusted_issuers=trusted_issuers,
                access_token_issuers=access_token_issuers,
                embedded_grant=embedded_grant,
                iap_tenant_by_domain=resolve_iap_tenant_by_domain(
                    identity_raw.pop("iap_tenant_by_domain", None)
                ),
                iap_groups_by_domain=resolve_iap_groups_by_domain(
                    identity_raw.pop("iap_groups_by_domain", None)
                ),
                **identity_raw,
            ),
            "channel": ChannelSettings(
                verifier_policies=tuple(verifier_policies),
                **_coerce_scalars(ChannelSettings, channel_raw),
            ),
            "pii": PiiSettings(**_coerce_scalars(PiiSettings, pii_raw)),
            "case_store": _section(CaseStoreSettings, "case_store"),
            "monitoring": _section(MonitoringStoreSettings, "monitoring"),
            "browser_flow_store": _section(BrowserFlowStoreSettings, "browser_flow_store"),
            "deployment": _section(DeploymentSettings, "deployment"),
            "web": WebSecuritySettings(**_coerce_scalars(WebSecuritySettings, web_raw)),
            "policy": RiskPolicy.from_mapping(raw.pop("policy", {}) or {}),
        }
        choice = resolve_profile(file_profile=str(raw.pop("profile", "") or ""))
        known = {f for f in Settings.__dataclass_fields__ if f not in nested}
        flat = _coerce_scalars(Settings, {k: v for k, v in raw.items() if k in known})
        flat.pop("profile_explicit", None)  # never settable from the settings file
        settings = Settings(
            profile=choice.profile, profile_explicit=choice.explicit, **flat, **nested
        )
        if (
            settings.deployment.production
            and optional_setting("CDD_EXPECTED_SETTINGS_SHA256") is None
        ):
            raise ValueError("production requires CDD_EXPECTED_SETTINGS_SHA256")
        return settings


def instantiate(dotted: str, settings: Settings) -> Any:
    """Import ``module.path:ClassName`` and construct it with ``settings``."""
    module_path, _, class_name = dotted.partition(":")
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    return cls(settings)


class Container:
    """Lazily-built registry of port -> adapter instances.

    Adapters are imported only on first access so that, e.g., a unit test using the
    on-prem profile never needs the Google Cloud SDKs installed.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _bind(self, port_name: str) -> Any:
        binding = self.settings.adapters.get(port_name, {})
        profile = self.settings.profile
        dotted = binding.get(profile)
        if not dotted:
            bound = ", ".join(sorted(binding)) or "none"
            raise KeyError(
                f"No adapter configured for port '{port_name}' under profile "
                f"'{profile}' (bound profiles: {bound}). Add a binding under "
                f"adapters.{port_name} in config/settings.yaml."
            )
        return instantiate(dotted, self.settings)

    def _bind_identity(self) -> Any:
        mode = self.settings.identity_mode
        dotted = self.settings.identity.bindings.get(mode)
        if not dotted:
            bound = ", ".join(sorted(self.settings.identity.bindings)) or "none"
            raise KeyError(
                f"No identity adapter configured for mode {mode!r} (enabled modes: {bound})"
            )
        return instantiate(dotted, self.settings)

    # One cached_property per port keeps wiring declarative and type-greppable.
    @cached_property
    def extraction(self) -> Any:
        return self._bind("extraction")

    @cached_property
    def knowledge_base(self) -> Any:
        return self._bind("knowledge_base")

    @cached_property
    def document_store(self) -> Any:
        return self._bind("document_store")

    @cached_property
    def adverse_media(self) -> Any:
        return self._bind("adverse_media")

    @cached_property
    def registry(self) -> Any:
        return self._bind("registry")

    @cached_property
    def ownership_graph(self) -> Any:
        return self._bind("ownership_graph")

    @cached_property
    def compliance(self) -> Any:
        return self._bind("compliance")

    @cached_property
    def llm(self) -> Any:
        return self._bind("llm")

    @cached_property
    def guardrail(self) -> Any:
        return self._bind("guardrail")

    @cached_property
    def redaction(self) -> Any:
        return self._bind("redaction")

    @cached_property
    def audit(self) -> Any:
        return self._bind("audit")

    @cached_property
    def tracer(self) -> Any:
        return self._bind("tracer")

    @cached_property
    def evaluation(self) -> Any:
        return self._bind("evaluation")

    @cached_property
    def agent_registry(self) -> Any:
        return self._bind("agent_registry")

    @cached_property
    def tool_catalog(self) -> Any:
        return self._bind("tool_catalog")

    @cached_property
    def case_store(self) -> Any:
        return self._bind("case_store")

    @cached_property
    def sanctions(self) -> Any:
        return self._bind("sanctions")

    @cached_property
    def monitoring_store(self) -> Any:
        return self._bind("monitoring_store")

    @cached_property
    def identity(self) -> Any:
        return self._bind_identity()

    @cached_property
    def review_router(self) -> Any:
        return self._bind("review_router")

    @cached_property
    def browser_flow_store(self) -> Any:
        return self._bind("browser_flow_store")


def build_container(settings: Settings | None = None) -> Container:
    return Container(settings or Settings.load())


def identity_adapter_class(settings: Settings) -> type:
    """The identity adapter CLASS the active binding names, resolved WITHOUT constructing it.

    Reads the same ``identity.bindings`` table :meth:`Container._bind_identity` binds from,
    keyed by the same :attr:`Settings.identity_mode`, so a deployment is answered about the
    adapter it ACTUALLY runs rather than the one the mode name suggests. A deployment that
    rebound a mode in ``config/settings.yaml`` (the documented on-premises path: swap the
    placeholder for the client's own IdP adapter) is answered about that.

    Constructing is deliberately avoided: the seeded-persona adapter refuses to construct
    under an inherited mode and the sandboxed adapters refuse without their reviewed policy
    present, so a posture computed from an instance would be unobtainable in several of the
    exact cases it has to describe.
    """
    dotted = settings.identity.bindings.get(settings.identity_mode)
    if not dotted:
        raise KeyError(f"No identity adapter configured for mode {settings.identity_mode!r}")
    module_path, _, class_name = dotted.partition(":")
    resolved = getattr(importlib.import_module(module_path), class_name)
    if not isinstance(resolved, type):
        raise TypeError(f"identity binding {dotted!r} does not name a class")
    return resolved


def end_user_auth_kind(settings: Settings | None = None) -> str:
    """What the BOUND identity adapter declares it does for end-user authentication.

    This is the one question "are this service's end-user routes authenticated?" reduces to.
    See ``ports/identity.py``: neither the runtime profile, nor the identity-mode string, nor
    the presence of a service credential can answer it.

    Any failure to establish the answer resolves to ``CLIENT_ASSERTED``. That covers the
    modes whose :attr:`Settings.identity_mode` itself raises (a runtime profile that infers
    nothing without ``CDD_IDENTITY_PROFILE``), and it is the fail-closed direction: a guard
    that switches OFF because a lookup raised is a guard that fails open, and nothing is lost
    by refusing here, because the same failure surfaces loudly at the first request when the
    container resolves the identical binding for real.
    """
    from .ports.identity import CLIENT_ASSERTED, declared_end_user_auth

    try:
        return declared_end_user_auth(identity_adapter_class(settings or Settings.load()))
    except Exception:
        return CLIENT_ASSERTED
