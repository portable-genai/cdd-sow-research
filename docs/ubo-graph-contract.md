# The UBO-graph output contract (frozen)

G2 does not exist yet, so nobody has agreed this shape with us. That is exactly why it is
written down: a later wave builds a consumer contract test against it, and a shape that
lives only in the code drifts silently between now and then. This document is the frozen
side of that agreement. It also records the three judgement calls the execution plan left
open, with the reasoning, so a reviewer can disagree with a decision rather than with a
mystery.

Authority order for this module: [`SPEC.md`](../SPEC.md) section 11 defines what it does;
this document freezes what it emits.

## 1. What is frozen, and what "frozen" means

The frozen artifact is the JSON body of:

* the A2A skill `resolve_ubo_graph` (`agent/tools.py`), which is `to_jsonable` over the
  domain `UboResolution`; and
* `POST /v1/ubo-graph`, whose `UboGraphResponse` mirrors it field for field.

They are the same shape by construction, and both are versioned by the **agent card's
`version`** (`agent/agent_card.py`), which is what a consumer pins.

The rule:

* **A field may be ADDED** without a version bump. A consumer that ignores unknown fields
  keeps working, which is why every field below is documented as optional-to-read.
* **A field may NOT be renamed, retyped, removed, or have its meaning changed** without
  bumping the card version. That includes narrowing an enum: adding a member is additive,
  removing one is not.
* **A percentage is always a number in 0..100**, never a 0..1 fraction, and never a string.
  This is the single most likely silent break, so it is called out on its own.

## 2. The shape

```jsonc
{
  "subject_id": "acme",
  "subject_name": "Acme Holdings Pte Ltd (FICTIONAL)",
  "tenant": "demo-bank",              // server-derived; never echoed from the request
  "as_of": "2026-08-07",              // the run replays byte for byte from this date
  "graph": {
    "root_id": "acme-holdings-pte-ltd-fictional@sg",
    "root_name": "Acme Holdings Pte Ltd (FICTIONAL)",
    "nodes": [
      {
        "id": "palewater-midco-ltd-fictional@ky",   // stable: normalised name @ jurisdiction
        "name": "Palewater Midco Ltd (FICTIONAL)",
        "kind": "entity",             // entity|natural_person|trust|nominee|state|listed|unknown
        "jurisdiction": "KY",
        "registered_address": "",
        "incorporation_date": "",     // ISO date as filed, or ""
        "status": "active",           // registry filing status, or ""
        "is_pep": false,
        "depth": 1,                   // hops from the subject; the subject is 0
        "citations": [ /* Citation */ ]
      }
    ],
    "edges": [
      {
        "source_id": "...",           // the OWNER
        "target_id": "...",           // the party owned or controlled
        "kind": "shareholding",       // shareholding|voting|directorship|nominee_arrangement|contractual
        "pct": 60.0,                  // 0..100; 0 for directorship / contractual
        "as_of": "2026-01-31",
        "citations": [ /* Citation */ ]
      }
    ],
    "depth": 3,                       // deepest layer actually reached
    "truncated": false,               // a limit stopped the walk: the picture is incomplete
    "unresolved_ids": [],             // layers the registry could not answer for
    "jurisdictions": ["JE", "KY", "SG"],
    "as_of": "2026-08-07"
  },
  "findings": [ /* UboFinding, every candidate party */ ],
  "beneficial_owners": [ /* UboFinding, natural persons at or above the threshold */ ],
  "control_basis": "board_majority",  // effective_ownership|voting_majority|board_majority|contractual|senior_managing_official|none
  "control_rationale": "Control established on the board majority rung ...",
  "flags": [
    {
      "kind": "nominee_indicator",    // nominee_indicator|shell_indicator|circular_holding|depth_truncated|secrecy_jurisdiction|unresolved_layer|no_owner_at_threshold
      "severity": "high",             // low|medium|high|critical
      "node_id": "...",
      "node_name": "...",
      "summary": "...",
      "detail": "Indicator only, never a conclusion: ...",
      "citations": [ /* Citation */ ]
    }
  ],
  "opacity_score": 0.85,              // 0..1, summed per DISTINCT flag kind, clamped
  "ownership_threshold_pct": 25.0,    // the policy threshold THIS run applied
  "rationale": "UBO resolution for ... Requires human review.",
  "narrative": "",                    // LLM prose; carries no number the code did not compute
  "requires_human_review": true,      // ALWAYS true
  "routed_to_hrz7": true,
  "generated_at": "2026-08-07T00:00:00+00:00"
}
```

A `UboFinding`:

```jsonc
{
  "node_id": "ines-quiller-fictional@je",
  "name": "Ines Quiller (FICTIONAL)",
  "kind": "natural_person",
  "jurisdiction": "JE",
  "is_pep": false,
  "effective_pct": 36.0,              // sum over SIMPLE paths of the product of shareholdings
  "paths": [
    {
      "steps": [ { "source_id": "...", "target_id": "...", "source_name": "...",
                   "target_name": "...", "kind": "shareholding", "pct": 75.0 } ],
      "product_pct": 36.0,
      "arithmetic": "75.00% x 80.00% x 60.00% = 36.0000%",
      "citations": [ /* Citation */ ]
    }
  ],
  "control_basis": "none",
  "control_reason": "",
  "meets_threshold": true,            // natural person at/above ownership_threshold_pct
  "citations": [ /* Citation */ ]
}
```

## 3. What a consumer must NOT infer

* **`findings` is not a list of beneficial owners.** It carries every candidate party,
  including the intermediate holding companies the graph looked through, because their
  effective percentages are useful context. Read `beneficial_owners`, or filter on
  `meets_threshold`.
* **`flags` are INDICATORS.** Nominee holdings, holding companies, cross-holdings and
  offshore jurisdictions are lawful and ordinary. A consumer must not render a flag as an
  allegation, must not aggregate flags into a customer-facing score, and must not act on
  one. Each flag carries the reason it was raised so it can be dismissed as easily as
  acted on.
* **`opacity_score` is not a risk score.** It measures how hard the structure is to see
  through, which is why it sets the review severity and nothing else.
* **`requires_human_review` is always `true`.** A consumer that branches on it has
  misunderstood the contract: there is no auto-approve path.
* **`truncated: true` or a non-empty `unresolved_ids` means the percentages are a FLOOR.**
  A consumer that presents them as complete is presenting a partial structure as a whole
  one, which is the specific failure this module exists to prevent.

## 4. The open judgement calls, decided

### 4.1 The review-router payload gets its own branch

**Decided: yes, a third verb.** `ReviewRouterPort` gains `route_ownership`, and
`adapters/_review_payload.py` gains `resolution_to_review`, alongside the dossier and
perpetual-KYC builders.

Reusing `route_monitoring` would have meant either mislabelling the action in the `human-review-console` or inventing a queue priority nothing computed. A resolution is a different
consequential claim from a re-score: it names the natural persons a bank will record as
the beneficial owners of a customer, on a stated control basis. What makes one resolution
harder to dispose of than another is the OPACITY of the structure, not a risk band and not
a queue priority, so the severity is banded on `opacity_score` and dual control is
required when the structure is opaque (at or above `policy.ubo_graph.dual_control_opacity`,
default `0.50`) or when nobody reaches the ownership threshold and the answer therefore
rests on the control ladder. The severity bands (`policy.ubo_graph.opacity_severity_bands`)
and that cut-off are bank-owned settings, not constants in the engine or the review adapter.
The idempotency key is
`doc1:<tenant>:<subject>:ubo:<as_of>`, so a retry of the same run collapses.

The owners' NAMES travel in the review summary on purpose: a checker cannot verify a
beneficial owner they cannot see. They go through the same full-jurisdiction `_redact`
pass as every other producer payload, so a national identifier a registry embedded in a
recorded name never reaches the wire.

### 4.2 The A2A output shape is proposed, and frozen anyway

**Decided: freeze it now, version it by the agent card.** Sections 1 to 3 above ARE the
freeze. G2 has not agreed it, so it is a proposal in the sense that G2 may ask for changes;
it is frozen in the sense that `cdd-sow-research` will not change it silently. When G2 arrives, its
consumer contract test pins the agent-card version, and any change that is not purely
additive bumps that version.

Two shapes rather than one, for a reason worth stating: `POST` is the consequential verb
(findings, control basis, indicators) and always routes to `human-review-console` under rule R8; `GET` returns
the walked structure ALONE. The read was drawn there rather than at "the same body without
routing" because a read that returned findings while skipping the route would make rule R8
bypassable by choosing a verb. Evidence has no verdict to escalate; a resolution does.

### 4.3 No store port

**Decided: none, and this is a design property rather than a gap.** The graph is a pure
function of the registry layers plus policy, so it is recomputable exactly: given the same
`as_of` the engine reproduces the resolution byte for byte, which is what makes an audit
recomputation possible in the first place. A store would add a second answer that can go
stale against the registry while looking authoritative, and staleness in a beneficial-
ownership record is the failure mode, not a performance concern.

This is a genuine difference from perpetual KYC, which DOES need a store: a pKYC re-score
is a DELTA against a remembered baseline, so without the baseline every unchanged fact
looks new. A UBO resolution has no baseline. If a future requirement needs point-in-time
retention (for example, evidencing what the bank believed on a given date), the durable
record already exists in two places that are designed to hold it: the `human-review-console` review item and
the WORM audit event. A store port would be added then, for that requirement, not now on
speculation.
