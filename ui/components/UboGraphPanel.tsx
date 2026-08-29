"use client";

// UBO graph: the walked cross-jurisdiction ownership structure behind the open subject.
//
// The panel renders the chain LAYER BY LAYER and, for every effective percentage, the
// multiplication that produced it. That is deliberate and is this repo's convention: a
// beneficial-ownership percentage a reviewer cannot check by hand is a number they have to
// take on faith, and taking a UBO number on faith is the failure this module exists to
// prevent. Everything numeric here was computed server-side by deterministic code.
//
// The flags are INDICATORS, never conclusions. Nominee holdings, holding companies,
// cross-holdings and offshore jurisdictions are all lawful and ordinary; each flag carries
// the reason it was raised so a reviewer can dismiss it as easily as act on it.

import { useCallback, useState } from "react";
import { CitationList } from "./CitationCard";
import { Empty, Panel, ReviewBanner } from "./ui";
import { resolveUboGraph } from "../lib/api";
import type {
  ControlBasis,
  OwnershipFlag,
  OwnershipGraphNode,
  Severity,
  UboFinding,
  UboResolution,
} from "../lib/types";

const CONTROL_LABEL: Record<ControlBasis, string> = {
  effective_ownership: "Effective ownership majority",
  voting_majority: "Voting majority",
  board_majority: "Board majority",
  contractual: "Contractual control",
  senior_managing_official: "Senior managing official (fallback)",
  none: "Not established",
};

const SEVERITY_STYLE: Record<Severity, string> = {
  critical: "bg-red-100 text-red-800",
  high: "bg-amber-100 text-amber-800",
  medium: "bg-ink-100 text-ink-700",
  low: "bg-emerald-100 text-emerald-700",
};

function SeverityBadge({ severity }: { severity: Severity }) {
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-xs font-semibold uppercase ${SEVERITY_STYLE[severity]}`}
    >
      {severity}
    </span>
  );
}

/** Group the parties by how many hops they sit above the subject. */
function byLayer(nodes: OwnershipGraphNode[]): Map<number, OwnershipGraphNode[]> {
  const layers = new Map<number, OwnershipGraphNode[]>();
  for (const node of nodes) {
    const bucket = layers.get(node.depth) ?? [];
    bucket.push(node);
    layers.set(node.depth, bucket);
  }
  return new Map([...layers.entries()].sort((a, b) => a[0] - b[0]));
}

function FindingRow({ finding }: { finding: UboFinding }) {
  return (
    <li className="min-w-0 break-words">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-medium text-ink-800">{finding.name}</span>
        {finding.is_pep ? (
          <span className="rounded bg-amber-100 px-1.5 py-0.5 text-xs font-semibold uppercase text-amber-800">
            PEP
          </span>
        ) : null}
        <span className="text-ink-500">
          {finding.effective_pct.toFixed(4)}% effective
          {finding.jurisdiction ? ` · ${finding.jurisdiction}` : ""}
        </span>
      </div>
      {/* The working, one path per line: product of the shareholdings along the chain. */}
      <ul className="mt-1 space-y-1 font-mono text-xs text-ink-600">
        {finding.paths.map((path, i) => (
          <li key={i}>
            <span className="text-regblue-700">{path.arithmetic}</span>
            <span className="text-ink-400">
              {" "}
              via {path.steps.map((s) => s.source_name).join(" → ")}
              {path.steps.length ? ` → ${path.steps[path.steps.length - 1].target_name}` : ""}
            </span>
          </li>
        ))}
      </ul>
      {finding.control_basis !== "none" ? (
        <p className="mt-1 text-xs text-ink-500">
          Control: {CONTROL_LABEL[finding.control_basis]} · {finding.control_reason}
        </p>
      ) : null}
    </li>
  );
}

function FlagRow({ flag }: { flag: OwnershipFlag }) {
  return (
    <li className="min-w-0 break-words">
      <div className="flex flex-wrap items-baseline gap-2">
        <SeverityBadge severity={flag.severity} />
        <span className="font-mono text-xs text-ink-400">{flag.kind}</span>
        <span className="text-ink-800">{flag.summary}</span>
      </div>
      <p className="text-xs text-ink-500">{flag.detail}</p>
    </li>
  );
}

export function UboGraphPanel({
  subject,
}: {
  subject: { id: string; name: string; type: "individual" | "entity"; jurisdiction: string } | null;
}) {
  const [resolution, setResolution] = useState<UboResolution | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onResolve = useCallback(async () => {
    if (!subject) return;
    setBusy(true);
    setError(null);
    try {
      setResolution(await resolveUboGraph({ subject }));
    } catch (exc) {
      setError(`UBO resolution failed: ${exc instanceof Error ? exc.message : String(exc)}`);
    } finally {
      setBusy(false);
    }
  }, [subject]);

  const graph = resolution?.graph ?? null;
  const layers = graph ? byLayer(graph.nodes) : new Map<number, OwnershipGraphNode[]>();

  return (
    <div className="space-y-4">
      <Panel title="Beneficial ownership (UBO graph)">
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <button
            className="rounded border border-regblue-600 px-3 py-1.5 text-sm font-medium text-regblue-700 disabled:opacity-50"
            onClick={() => void onResolve()}
            disabled={busy || !subject || subject.type !== "entity"}
          >
            Resolve ownership structure
          </button>
          <span className="text-xs text-ink-500">
            One cited registry hop at a time · every percentage is deterministic code ·
            always routed for human review
          </span>
        </div>
        {subject && subject.type !== "entity" ? (
          <Empty>
            The subject is a natural person, so there is no corporate structure to walk.
          </Empty>
        ) : null}
        {error ? (
          <div className="rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
            {error}
          </div>
        ) : null}
        {!resolution && !error ? (
          <Empty>
            No structure resolved yet. Resolving walks the corporate chain across
            jurisdictions and computes each owner&apos;s effective percentage.
          </Empty>
        ) : null}
      </Panel>

      {resolution ? (
        <Panel title={`Ownership — ${resolution.subject_name || resolution.subject_id}`}>
          <ReviewBanner requiresReview={resolution.requires_human_review} />
          <dl className="mb-3 grid min-w-0 grid-cols-1 gap-2 text-sm text-ink-700 sm:grid-cols-3">
            <div>
              <dt className="text-ink-400">Structure</dt>
              <dd>
                {graph ? `${graph.nodes.length} parties, depth ${graph.depth}` : "unresolved"}
                {graph?.truncated ? " (truncated)" : ""}
              </dd>
            </div>
            <div>
              <dt className="text-ink-400">Control basis</dt>
              <dd>{CONTROL_LABEL[resolution.control_basis]}</dd>
            </div>
            <div>
              <dt className="text-ink-400">Opacity</dt>
              <dd>
                {resolution.opacity_score.toFixed(4)} from {resolution.flags.length} indicator(s)
              </dd>
            </div>
          </dl>
          <p className="mb-3 text-xs text-ink-500">
            {resolution.control_rationale} Routed to the human-review console:{" "}
            {resolution.routed_to_hrz7 ? "yes" : "no (retained locally for retry)"}
          </p>

          <h3 className="mb-1 text-xs font-semibold uppercase text-ink-500">
            Beneficial owners at or above {resolution.ownership_threshold_pct.toFixed(2)}%
          </h3>
          {resolution.beneficial_owners.length === 0 ? (
            <Empty>
              No natural person reaches the threshold; the control ladder decided this
              structure.
            </Empty>
          ) : (
            <ul className="mb-3 space-y-2 text-sm">
              {resolution.beneficial_owners.map((finding) => (
                <FindingRow key={finding.node_id} finding={finding} />
              ))}
            </ul>
          )}

          <h3 className="mb-1 text-xs font-semibold uppercase text-ink-500">
            The chain, layer by layer
          </h3>
          {graph && graph.nodes.length > 0 ? (
            <ol className="mb-3 min-w-0 space-y-1 break-words text-sm text-ink-700">
              {[...layers.entries()].map(([depth, nodes]) => (
                <li key={depth}>
                  <span className="font-mono text-xs text-ink-400">
                    L{depth}
                    {depth === 0 ? " (subject)" : ""}
                  </span>{" "}
                  {nodes
                    .map(
                      (n) =>
                        `${n.name}${n.jurisdiction ? ` [${n.jurisdiction}]` : ""}${
                          graph.unresolved_ids.includes(n.id) ? " (registry silent)" : ""
                        }`,
                    )
                    .join(", ")}
                </li>
              ))}
            </ol>
          ) : (
            <Empty>The registry returned no layers for this subject.</Empty>
          )}

          <h3 className="mb-1 text-xs font-semibold uppercase text-ink-500">
            Every recorded party and its arithmetic
          </h3>
          <ul className="mb-3 space-y-2 text-sm">
            {resolution.findings.map((finding) => (
              <FindingRow key={finding.node_id} finding={finding} />
            ))}
          </ul>

          <h3 className="mb-1 text-xs font-semibold uppercase text-ink-500">
            Indicators (reasons to verify, never conclusions)
          </h3>
          {resolution.flags.length === 0 ? (
            <Empty>No structural indicators raised.</Empty>
          ) : (
            <ul className="mb-3 space-y-2 text-sm">
              {resolution.flags.map((flag, i) => (
                <FlagRow key={`${flag.kind}-${flag.node_id}-${i}`} flag={flag} />
              ))}
            </ul>
          )}

          {resolution.narrative ? (
            <>
              <h3 className="mb-1 text-xs font-semibold uppercase text-ink-500">
                Narrative (model prose over the computed figures)
              </h3>
              <p className="mb-3 min-w-0 break-words text-sm text-ink-800">
                {resolution.narrative}
              </p>
            </>
          ) : null}

          <CitationList citations={graph?.nodes.flatMap((n) => n.citations) ?? []} />
        </Panel>
      ) : null}
    </div>
  );
}
