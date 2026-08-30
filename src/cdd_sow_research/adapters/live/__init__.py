"""``live`` profile adapters — real documents, generation via the Gemini API.

The ``live`` profile is the one an analyst actually uses: real uploaded documents, real
subject names, real generation. It differs from ``local`` (deterministic fixtures, for
tests and CI) and from ``gcp`` (fully managed) by where the runtime sits, not by which
model answers:

* **Custody stays on the machine.** Uploaded documents, the evidence index and the
  audit trail live in local SQLite; the deterministic text-layer extraction runs
  locally too.
* **Every model call is the Gemini API**: source-of-wealth and risk generation, scanned
  page transcription, and the adverse-media / corporate-registry ``google_search``
  research. There is deliberately no local model: a system whose use case needs
  internet research is only implemented for customers who permit leaving the data
  centre, so a local-model profile here would demo a deployment nobody would buy
  (org decision, 2026-08-30).

The profile therefore needs Application Default Credentials and a
``GOOGLE_CLOUD_PROJECT``, and the UI provenance banner states that the runtime is local
and the model is Gemini.
"""
