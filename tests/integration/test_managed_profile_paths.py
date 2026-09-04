"""The managed paths that were proved once, by hand, and that nothing re-ran.

Track C item 4 states the gap exactly: cdd-sow-research, journey-portal and agent-observability were
exercised against the real services by running them, which found defects no offline profile can
reach. That is evidence those paths worked **at the moment they were run**, and nothing re-runs
them.

``test_gcp_smoke.py`` is not that. It does one trivial liveness call per adapter, which proves
each service is reachable and answers -- a different and much weaker claim. Every defect the
first live run actually found would have passed a liveness check:

* the managed knowledge base returned citations whose titles had decayed into their own
  document ids. The store was reachable. The search returned results. The citations resolved.
* the managed document store minted a fresh id per upload, so a case's corpus grew by one copy
  of the same statement per run. Every call succeeded.
* the deployment's watchlist snapshot was unreadable, so it screened nobody, and the dossier
  reported ``screening: null``, which reads as "this profile does not screen". No call failed.

So these tests assert **round-trip properties**, not reachability: what goes in comes back out
carrying what it went in with. That is the class of claim a liveness check cannot make.

**They must not be able to pass vacuously, and that is the whole design.** Three states, never
two:

* no deployment named -> SKIP, so an offline gate is unaffected;
* a deployment named and reachable -> RUN and assert;
* a deployment named and NOT usable -> **FAIL**, never skip.

The third state is the one that matters. A managed test that skips when its configuration is
wrong is the served-browser defect wearing different clothes: it reports the same green as a
test that ran, and the first time a project id is mistyped the suite silently stops testing
anything. `CDD_MANAGED_TEST_PROJECT` is therefore read three-state, like every other setting
in this repository.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from cdd_sow_research.domain.models import (
    DocType,
    KycDocument,
    RetrievalQuery,
    Subject,
    citation_title,
    document_id,
)

_PROJECT_VAR = "CDD_MANAGED_TEST_PROJECT"
_REGION_VAR = "CDD_MANAGED_TEST_REGION"


def _named_deployment() -> str | None:
    """The project under test: absent is "not asked for", empty is a configuration defect.

    A rendered deployment template that produces ``CDD_MANAGED_TEST_PROJECT=`` must fail
    loudly rather than silently take the skip path and report a green managed suite.
    """
    if _PROJECT_VAR not in os.environ:
        return None
    value = os.environ[_PROJECT_VAR].strip()
    if not value:
        raise AssertionError(
            f"{_PROJECT_VAR} is set to an empty value. Leave it unset to skip the managed "
            f"suite, or name the project you mean. A blank setting is how a rendered "
            f"template turns a managed test run into a skip nobody notices."
        )
    return value


_PROJECT = _named_deployment()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _PROJECT is None,
        reason=f"set {_PROJECT_VAR} to run the managed round-trip suite against a deployment",
    ),
]

#: Content unique per run, so a stale object from a previous run can never satisfy an
#: assertion. The defects these tests exist for were all defects of ACCUMULATION, and a
#: fixed fixture would have been indistinguishable from the leftovers of the last run.
_RUN = uuid.uuid4().hex[:8]
_TEXT = f"Proceeds of the 2019 asset sale. Managed round-trip probe {_RUN}."


@pytest.fixture(scope="module")
def settings():  # type: ignore[no-untyped-def]
    from cdd_sow_research.config import Settings

    base = Settings.load("config/settings.yaml")
    resolved = Settings(
        project_id=_PROJECT or "",
        region=os.environ.get(_REGION_VAR, "").strip() or "us-central1",
        profile="gcp",
        kms_key=base.kms_key,
        grounding_enabled=base.grounding_enabled,
        models=base.models,
        document_ai=base.document_ai,
        knowledge_base=base.knowledge_base,
        model_armor=base.model_armor,
        dlp=base.dlp,
        logging=base.logging,
        agent_engine=base.agent_engine,
        adapters=base.adapters,
        sanctions=base.sanctions,
        document_store=base.document_store,
    )
    # A named-but-unusable deployment FAILS here, at collection time, rather than skipping
    # every test below one at a time with a reason that reads like "not requested".
    from cdd_sow_research.config import PLACEHOLDER_PROJECT_ID

    if resolved.project_id == PLACEHOLDER_PROJECT_ID:
        raise AssertionError(
            f"{_PROJECT_VAR} names the documented placeholder project "
            f"{PLACEHOLDER_PROJECT_ID!r}. That is correct on a laptop and a defect in any "
            f"profile that calls a cloud API."
        )
    return resolved


@pytest.fixture(scope="module")
def case_tags() -> tuple[str, ...]:
    return (f"case:managed-probe-{_RUN}",)


#: How long a freshly ingested document may take to become searchable before that counts as a
#: failure rather than as latency. Discovery Engine indexes asynchronously: ``ingest`` returning
#: ``indexed`` means the document was accepted, not that a search can find it. Measured against
#: this deployment on 2026-08-26, a probe document became searchable after 34 seconds, so a test
#: that ingested and searched in the same breath asserted a consistency model the store does not
#: offer and failed every run for a reason that was never a defect.
#:
#: The bound is what keeps this honest. Polling forever would turn a store that never indexes
#: anything into a hang, and polling zero times is what was wrong before. A document that has not
#: arrived within this window is a real failure and is reported as one.
_INDEX_VISIBILITY_TIMEOUT_SECONDS = 180.0
_INDEX_POLL_INTERVAL_SECONDS = 5.0


def _search_once_indexed(kb, query: RetrievalQuery):  # type: ignore[no-untyped-def]
    """Search, retrying until the store has caught up or the bound expires.

    Returns whatever the last search returned, including nothing, so the caller's assertion is
    unchanged. This does not weaken any claim: it removes a race the assertions never meant to
    make, and a document that never becomes searchable still fails the test it belongs to.
    """

    deadline = time.monotonic() + _INDEX_VISIBILITY_TIMEOUT_SECONDS
    passages = kb.search(query)
    while not passages and time.monotonic() < deadline:
        time.sleep(_INDEX_POLL_INTERVAL_SECONDS)
        passages = kb.search(query)
    return passages


# --------------------------------------------------------------------------------------- #
# The knowledge base: what comes back carries what went in.
# --------------------------------------------------------------------------------------- #
def test_a_cited_document_comes_back_carrying_its_name(settings, case_tags) -> None:  # type: ignore[no-untyped-def]
    """The defect the paired run found: eight citations, eight titles decayed into ids.

    Every part of this succeeded before the fix -- the ingest, the search, the citation, the
    resolvable link. Only the NAME was gone, and a liveness check has no opinion about names.
    """
    from cdd_sow_research.adapters.gcp.agent_search_kb import AgentSearchKnowledgeBaseAdapter

    kb = AgentSearchKnowledgeBaseAdapter(settings)
    document = KycDocument(
        id=document_id(_TEXT.encode(), f"subj-probe-{_RUN}", DocType.BANK_STATEMENT, "probe.txt"),
        doc_type=DocType.BANK_STATEMENT,
    )
    result = kb.ingest(document, _TEXT.encode(), case_tags, page_texts=(_TEXT,))
    assert result.ok, f"managed ingest refused the probe document: {result.status}"

    passages = _search_once_indexed(
        kb, RetrievalQuery(text=f"asset sale {_RUN}", acl_principals=case_tags, top_k=5)
    )

    assert passages, (
        "the managed store returned nothing for a document it had just indexed, after waiting "
        f"{_INDEX_VISIBILITY_TIMEOUT_SECONDS:.0f}s for indexing to catch up"
    )
    citation = passages[0].citation
    assert citation.title == citation_title(DocType.BANK_STATEMENT), (
        f"the managed citation is named {citation.title!r}. A citation named after its own "
        f"document id is not a usable evidence link, and the link resolving is exactly why "
        f"nothing else notices."
    )
    assert citation.title != citation.source_id


def test_re_ingesting_the_same_document_does_not_make_a_second_one(settings, case_tags) -> None:  # type: ignore[no-untyped-def]
    """Accumulation, which is invisible in any single run and compounds across all of them.

    Eight copies of one synthetic statement had built up in the managed store, one per demo
    run, and the source-of-wealth narrative was grounded in "eight documents".
    """
    from cdd_sow_research.adapters.gcp.agent_search_kb import AgentSearchKnowledgeBaseAdapter

    kb = AgentSearchKnowledgeBaseAdapter(settings)
    document = KycDocument(
        id=document_id(_TEXT.encode(), f"subj-probe-{_RUN}", DocType.BANK_STATEMENT, "probe.txt"),
        doc_type=DocType.BANK_STATEMENT,
    )
    kb.ingest(document, _TEXT.encode(), case_tags, page_texts=(_TEXT,))
    second = kb.ingest(document, _TEXT.encode(), case_tags, page_texts=(_TEXT,))

    assert second.ok, "a repeated ingest must be success, not an error the caller swallows"
    assert second.status == "already-indexed"

    passages = _search_once_indexed(
        kb, RetrievalQuery(text=f"asset sale {_RUN}", acl_principals=case_tags, top_k=20)
    )
    assert len({p.citation.source_id for p in passages}) == 1, (
        "one document ingested twice produced more than one document in the managed store"
    )


def test_the_case_acl_is_enforced_by_the_managed_store(settings, case_tags) -> None:  # type: ignore[no-untyped-def]
    """Fail-closed, asserted against the real store rather than against the local mirror.

    The managed adapter enforces the ACL in Python over an over-fetch, which is a different
    implementation from the local adapter's, so the offline suite cannot speak for it.
    """
    from cdd_sow_research.adapters.gcp.agent_search_kb import AgentSearchKnowledgeBaseAdapter

    kb = AgentSearchKnowledgeBaseAdapter(settings)
    document = KycDocument(
        id=document_id(_TEXT.encode(), f"subj-probe-{_RUN}", DocType.BANK_STATEMENT, "probe.txt"),
        doc_type=DocType.BANK_STATEMENT,
    )
    kb.ingest(document, _TEXT.encode(), case_tags, page_texts=(_TEXT,))

    leaked = kb.search(
        RetrievalQuery(
            text=f"asset sale {_RUN}", acl_principals=("case:someone-elses-case",), top_k=5
        )
    )

    assert not [p for p in leaked if p.citation.source_id == document.id], (
        "a case-tagged document reached a query that does not hold its tag"
    )


# --------------------------------------------------------------------------------------- #
# Screening: the outage that produced no error anywhere.
# --------------------------------------------------------------------------------------- #
def test_the_watchlist_snapshot_is_readable(settings) -> None:  # type: ignore[no-untyped-def]
    """The deployment's actual failure, and the reason it went unnoticed for as long as it did.

    The snapshot was unreadable, so ``_screen`` returned None, so the dossier said
    ``screening: null`` -- which reads as "this profile does not screen" rather than
    "screening is down". Nothing failed. Nothing logged. The only reason anyone found out is
    that a paired run compared it against a laptop that had screened six lists.

    Asserting the snapshot is READABLE is deliberately stronger than asserting screening
    returns something, because screening returning None is precisely the symptom.
    """
    from cdd_sow_research.adapters.gcp.sanctions_provider import GcsSanctionsProviderAdapter

    provider = GcsSanctionsProviderAdapter(settings)

    version = provider.version()

    assert version and version != "unknown", (
        f"the watchlist snapshot at gs://{settings.sanctions.bucket}/"
        f"{settings.sanctions.object_name} reports version {version!r}. A deployment in this "
        f"state screens nobody and says so only as a null field."
    )
    assert any(True for _ in provider.iter_entries()), "the snapshot parsed but holds no entries"


def test_a_dossier_from_this_deployment_carries_a_screening_result(settings) -> None:  # type: ignore[no-untyped-def]
    """End to end, through the same service the console calls.

    ``screening is None`` is the honest answer for an outage and an unacceptable one for a
    deployment that is claimed to screen. This is the assertion the pair had to be built to
    make, expressed against one target instead of two.
    """
    from cdd_sow_research.api.deps import build_cdd_service
    from cdd_sow_research.config import Container

    service = build_cdd_service(Container(settings))
    subject = Subject(id=f"subj-probe-{_RUN}", name="Meridian Harbour Holdings Pte Ltd")

    screening = service._screen(subject)  # noqa: SLF001 - the unit under test is this path

    assert screening is not None, (
        "this deployment produced a dossier that had screened nobody. The field is null on "
        "the wire, which reads as a profile that does not screen rather than as an outage."
    )
    assert screening.lists_version, "a screening result with no list version is not reproducible"


def test_every_configured_model_actually_resolves(settings) -> None:  # type: ignore[no-untyped-def]
    """Call each configured model once and FAIL on a 404.

    Nothing in the fleet checked this, which is how `gemini-3.1-pro` sat in the hard-reasoning
    slot of twenty repositories resolving in NO location. It broke nothing visible because the
    hard-reasoning path is feature-flagged off, so the first deployment to switch it on would
    have been the one to find out.

    An offline gate cannot cover this: what a publisher serves is not a property of the source
    tree, it is a fact about a project and a location on a given day, and it changes without a
    commit. This is also why it asserts the CONFIGURED ids rather than a hardcoded list -- a
    list would go stale exactly the way the pins did.

    The residency half is asserted with it. `global` reaches every model and carries no
    residency guarantee, so a green run here against `global` would prove availability while
    silently voiding the in:us-locations claim.
    """
    from google import genai

    assert settings.models.location != "global", (
        "the model client is pointed at the global endpoint, which carries no ML-processing "
        "residency guarantee; the in:us-locations claim in the dossiers would not survive it"
    )

    client = genai.Client(
        vertexai=True, project=settings.project_id, location=settings.models.location
    )
    configured = {
        "reasoning": settings.models.reasoning,
        "triage": settings.models.triage,
        "hard_reasoning": settings.models.hard_reasoning,
    }
    unreachable: dict[str, str] = {}
    for slot, model in sorted(configured.items()):
        try:
            client.models.generate_content(model=model, contents="ping")
        except Exception as exc:  # noqa: BLE001 - any failure to reach it is the finding
            unreachable[slot] = f"{model}: {type(exc).__name__} {str(exc)[:120]}"

    assert not unreachable, (
        f"configured models that do not resolve in {settings.models.location!r}: {unreachable}"
    )


# --------------------------------------------------------------------------------------- #
# The non-model services: is each one actually served where the configuration points?
# --------------------------------------------------------------------------------------- #


def test_every_configured_non_model_service_is_served_where_configured(settings) -> None:  # type: ignore[no-untyped-def]
    """Reach each configured non-model service once at its configured location, FAIL on none.

    The model half of this check exists above and the non-model half did not, which is how two
    region defects survived it. Retrieval bound to the compute region produced
    `us-central1-discoveryengine.googleapis.com`, a hostname that does not exist, and grounded
    retrieval failed with a 501 blaming the api_endpoint. Seven sibling trees then pinned
    Agent Search to a location the service has never served, and their Terraform
    pre-rationalised the failing apply as the residency control working. Both defects were
    invisible offline for the same reason the model pins were: what a publisher serves is a
    fact about a project and a location on a given day, not a property of the source tree.
    org-metadata's docs/gcp-service-region-availability.md records the per-service facts; this
    test is the live half that keeps them honest.

    Three locations are configuration here and each is probed as CONFIGURED, never as derived
    from the region, because "the config and the serving reality disagree" is exactly the
    finding: Discovery Engine at `knowledge_base.location`, Document AI at
    `document_ai.location`, and Model Armor at its regional `host` with the template the
    guardrail actually screens through. A refused or unresolved endpoint FAILS; nothing here
    skips, because a probe that skips when a location is wrong reports the same green as one
    that ran.

    The residency posture is asserted with it, like the model check above: `global` retrieval
    would prove availability while silently voiding the jurisdiction claim, so it is refused
    here as well as at settings load.
    """
    from google.api_core.client_options import ClientOptions
    from google.cloud import discoveryengine_v1, documentai_v1, modelarmor_v1

    assert settings.knowledge_base.location != "global", (
        "retrieval is pointed at the global endpoint, which names no jurisdiction; the "
        "residency claim in the dossiers would not survive it"
    )

    unserved: dict[str, str] = {}

    kb = settings.knowledge_base
    kb_endpoint = f"{kb.location}-discoveryengine.googleapis.com"
    branch = (
        f"projects/{settings.project_id}/locations/{kb.location}"
        f"/collections/{kb.collection_id}/dataStores/{kb.data_store_id}"
        f"/branches/{kb.branch_id}"
    )
    try:
        documents = discoveryengine_v1.DocumentServiceClient(
            client_options=ClientOptions(api_endpoint=kb_endpoint),
        )
        next(
            iter(
                documents.list_documents(
                    request=discoveryengine_v1.ListDocumentsRequest(parent=branch, page_size=1)
                )
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001 - any failure to reach it is the finding
        unserved["knowledge_base"] = (
            f"{kb_endpoint} / {branch}: {type(exc).__name__} {str(exc)[:160]}"
        )

    docai_endpoint = f"{settings.document_ai.location}-documentai.googleapis.com"
    try:
        processors = documentai_v1.DocumentProcessorServiceClient(
            client_options=ClientOptions(api_endpoint=docai_endpoint),
        )
        # list_processors answers the pure "is this location served for this project"
        # question even before a processor id is configured; a configured id is then held
        # to exist by name, because the adapter will address exactly that resource.
        parent = f"projects/{settings.project_id}/locations/{settings.document_ai.location}"
        next(
            iter(
                processors.list_processors(
                    request=documentai_v1.ListProcessorsRequest(parent=parent, page_size=1)
                )
            ),
            None,
        )
        if settings.document_ai.processor_id:
            processors.get_processor(name=settings.document_ai.processor_id)
    except Exception as exc:  # noqa: BLE001 - any failure to reach it is the finding
        unserved["document_ai"] = f"{docai_endpoint}: {type(exc).__name__} {str(exc)[:160]}"

    template = (
        f"projects/{settings.project_id}/locations/{settings.region}"
        f"/templates/{settings.model_armor.template_id}"
    )
    try:
        armor = modelarmor_v1.ModelArmorClient(
            client_options=ClientOptions(api_endpoint=settings.model_armor.host),
        )
        armor.get_template(name=template)
    except Exception as exc:  # noqa: BLE001 - any failure to reach it is the finding
        unserved["model_armor"] = (
            f"{settings.model_armor.host} / {template}: {type(exc).__name__} {str(exc)[:160]}"
        )

    assert not unserved, (
        f"configured non-model services that are not served where the configuration points: "
        f"{unserved}"
    )
