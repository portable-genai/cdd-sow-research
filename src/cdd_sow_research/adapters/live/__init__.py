"""``live`` profile adapters — real inference on the operator's own machine.

The ``live`` profile is the one an analyst actually uses: real uploaded documents, real
subject names, real generation. It differs from ``local`` (deterministic fixtures, for
tests and CI) and from ``gcp`` (fully managed) by splitting the work along a data
boundary rather than a vendor one:

* **Customer documents never leave the machine.** Text extraction, page-image
  transcription, generation of the source-of-wealth narrative and the risk rating all
  run against a local OpenAI-compatible model server (a Gemma build under MLX or
  Ollama), and the evidence index stays in local SQLite.
* **Only the subject's NAME goes to the cloud**, and only for the capabilities a laptop
  cannot provide: adverse-media and corporate-registry research, which need a live web
  index. Those bind to the existing Gemini ``google_search`` grounded adapters.

That split is the point of the profile: the sensitive artifacts stay local, the public
research is grounded in the open web, and both halves are the same ports the managed
profile uses.
"""
