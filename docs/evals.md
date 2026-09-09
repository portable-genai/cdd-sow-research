# How the CDD and source-of-wealth agent is evaluated

Read this page if you decide what this service is allowed to say. The metrics, the bars and the
corpora below are generated from the artifacts that actually gate the build, so they cannot drift
from what runs: `make evals-doc-check` fails the build when this page and those artifacts
disagree.

## How to run it

```sh
make eval              # offline, no credentials
make evals-doc-check   # this page is still true
```

`make check` runs both on every change.

## What is measured, and against what bar

Every bar below lives in `eval/rubrics/*.yaml` next to the argument for it, and the
runner reads it from there. There is no dict of thresholds in the runner any more: a
metric scored with no reviewed bar fails the build, and so does a bar that names no
metric, which is the direction that rots quietly because it rots toward looking well
governed.

The third column is the denominator rule, and it applies only where a score is a
FRACTION over scored positives: such a threshold `t` tolerates a single miss only over
at least `1/(1-t)` of them. `all or nothing` marks a bar that already asks for no
headroom, so a bigger corpus would not change what it means. Each rubric declares which
it is rather than the rule being guessed from the number.

| Metric | Bar | Denominator | What it measures |
|---|---|---|---|
| `adverse_media_relevance` | 1 | all or nothing | Share of labelled adverse-media hits the shipped relevance predicate classifies the way a reviewer did: kept when the article names the subject, dropped when it does not. |
| `citation_accuracy` | 0.9 | a rate; needs 10 positives | Per-case correctness of the citation set: no citation outside the retrieved or derived evidence (KYC documents, registry records, adverse media). Averaged over the dataset. |
| `pii_safety` | 0.99 | a rate; needs 100 positives | No unredacted customer PII (NRIC, email) survives into the dossier or the audit records. A single leak drops the whole metric below 0.99. |
| `pkyc_priority` | 0.9 | a rate; needs 10 positives | Per-case agreement between the engine's computed review-queue priority plus re-score direction and the golden set's declared expectation for the simulated change. Averaged over the dataset. |
| `risk_band_accuracy` | 0.8 | a rate; needs 5 positives | The assigned risk band matches the expected band for the golden case, after the deterministic hard-signal raise (sanctions/terrorism/PEP). |
| `sow_groundedness` | 0.8 | a rate; needs 5 positives | Fraction of the source-of-wealth narrative's claim-bearing sentences that are supported by a cited evidence source. A narrative with claims but no citations scores 0. |
| `ubo_accuracy` | 0.9 | a rate; needs 10 positives | Per-case agreement between the engine's computed beneficial owners (with their effective percentages), control basis and indicator kinds, and the golden case's declared expectation for the same structure. Averaged over the dataset. |

Scored over 6 golden CDD cases.

## What is exercised

- **6 golden CDD cases** in `eval/datasets/golden_cases.jsonl`, each with the
  risk band, the perpetual-KYC queue placement and the beneficial owners a reviewer
  assigned. Every expectation is the dataset's, never a re-read of what the engine
  produced: an oracle taken from the thing under test agrees with it by construction.
- **10 labelled adverse-media hits** in
  `eval/datasets/adverse_media_relevance.jsonl`, 5 of which a reviewer says name
  the subject and the rest of which share only a jurisdiction, a sector, a partial word
  or a similar name. Scored through the shipped `finding_names_subject` predicate.

## The metric this gate was missing

On 2026-08-26 a paired demonstration asked for adverse media on a fictional company. The
deployment's grounded web search returned a **real** money-laundering prosecution naming real
banks, marked it critical, and the risk policy turned that severity into a PROHIBITED band for a
company the article never mentions. Two things were wrong and only one of them is about search
quality: a returned article carried its severity straight into the most consequential field in
the dossier, with nothing deterministic in between.

`finding_names_subject` is the fix, and it was unit-tested and never scored. A unit test can be
deleted in the same commit as the thing it guards, and a promotion that regressed this should not
be certifiable. `adverse_media_relevance` puts it in the gate, over its own labelled corpus,
because the question "is this article about this subject" needs articles that are not, and a
golden case carries only articles that are.

Its bar is 1.0 and the asymmetry is the argument. One wrong KEEP is a subject given the most
severe band the system can assign, on evidence about somebody else. One wrong DROP is a finding
silently missing from a dossier a supervisor reads. The falsification runs in both directions
before anything is scored: a predicate that keeps everything is the original defect, and one that
drops everything is the over-correction, which a one-sided proof would certify.

## The fake that had drifted from its port

The gate's inlined `FakeKnowledgeBase.ingest` was missing the `page_texts` argument the port
declares, so every case-document ingestion in the gate raised. `CddService` catches an ingestion
failure and continues with "this document will not ground any citation", which is right for the
product and fatal for the gate: the run stayed green while no case document grounded anything at
all, and the grounding metrics scored the adverse-media and ownership citations alone.

`runtime_checkable` Protocols compare method NAMES, so an `isinstance` check would have passed.
`tests/unit/test_eval_fakes_match_their_ports.py` compares signatures, which is where the drift
was, and proves the comparison can fail.

## How a metric is prevented from being decoration

1. **The bars are read from the rubrics, in both directions.** There is no `THRESHOLDS` dict any
   more. What was here before was both a dict and a loader that overlaid four rubric files on top
   of it, silently falling back to the dict when PyYAML was missing: two homes for one number,
   with a silent path that used the one nobody reviews. `assert_covers` fails the build when a
   metric has no reviewed bar AND when a bar names no metric.
2. **The corpus must be able to express its own bars.** `citation_accuracy` at 0.90 is measured
   over the citations the dossiers actually carry, not over six golden cases, and the runner
   asserts that relationship against the count the run produced. Six cases would not have
   supported it.
3. **The relevance predicate's red cases run as the first statement of the scored run.** Run only
   in `tests/`, a proof says the metric could have gone red on some machine at some point.

## What is NOT measured here

Naming this is part of the page, because an unmeasured claim that goes unmentioned reads as a
measured one.

- **A real model's words.** Every metric here scores a deterministic core against a deterministic
  fake LLM adapter, so `sow_groundedness` is a measurement of the VALIDATOR rather than of a
  model's restraint: it would stay green through a model swap or a prompt regression.
- **Grounding at claim level.** `sow_groundedness` asks whether the source-of-wealth narrative
  carries a citation, not whether every figure in it appears in the cited source.
- **Retrieval quality.** The governed knowledge base's recall is not scored separately from what
  the narrative did with what it returned.
- **Production traffic.** Everything here is a golden set. Nothing samples live requests.
