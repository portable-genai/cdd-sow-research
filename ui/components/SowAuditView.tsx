// Renders the audit-first Source-of-Wealth view for a long-running SoW case:
// reconciliation header, source groups (at a glance, with proofs + drill-down),
// computed gaps, and the client information requests (RFIs).
//
// Mirrors the domain SowAuditView (src/cdd_sow_research/domain/models.py) and the design
// in docs/sow-longitudinal-audit-design.md. Types are declared locally so the component
// is self-contained; field names match the JSON emitted by `to_jsonable`.

import { CitationList } from "./CitationCard";
import { SeverityPill } from "./RiskBadge";
import { Empty, Panel, ReviewBanner } from "./ui";

type Severity = "low" | "medium" | "high" | "critical";

interface Citation {
  source_id: string;
  source_type: any;
  title: string;
  url?: string;
  page?: number | null;
  snippet?: string;
}
interface ReconciliationLine {
  kind: string;
  declared_band: string;
  evidenced_band: string;
  coverage_pct: number;
  corroborated: boolean;
}
interface WealthReconciliation {
  lines: ReconciliationLine[];
  declared_total_band: string;
  evidenced_total_band: string;
  coverage_pct: number;
  consistency_notes: string[];
}
interface SourceGroup {
  kind: string;
  declared_band: string;
  evidenced_band: string;
  corroborated: boolean;
  evidence_ids: string[];
  citations: Citation[];
}
interface Gap {
  id: string;
  kind: string;
  severity: Severity;
  summary: string;
  detail: string;
}
interface InformationRequest {
  id: string;
  gap_id: string;
  ask: string;
  suggested_doc_types: string[];
  priority: Severity;
  status: string;
}
export interface SowAuditView {
  subject_id: string;
  narrative: { narrative: string; confidence: number; citations: Citation[] };
  groups: SourceGroup[];
  reconciliation: WealthReconciliation;
  gaps: Gap[];
  rfis: InformationRequest[];
}

const KIND_LABEL: Record<string, string> = {
  business_ownership: "Business ownership",
  employment: "Employment",
  investments: "Investments",
  inheritance: "Inheritance",
  asset_sale: "Asset sale",
  other: "Other",
};

function pct(x: number): string {
  return `${Math.round(x * 100)}%`;
}

function CoverageBar({ value }: { value: number }) {
  return (
    <div className="h-3 overflow-hidden rounded-full border border-ink-200 bg-ink-100">
      <span
        className="block h-full bg-regblue-600"
        style={{ width: `${Math.round(value * 100)}%` }}
      />
    </div>
  );
}

export function SowAuditViewPanel({
  view,
  status,
}: {
  view: SowAuditView;
  status?: string;
}) {
  const r = view.reconciliation;
  const lineByKind = new Map(r.lines.map((l) => [l.kind, l]));
  const reviewing = status !== "approved";

  return (
    <div className="space-y-4">
      <ReviewBanner requiresReview={reviewing} />

      {/* Reconciliation: declared vs evidenced */}
      <Panel title="Reconciliation: declared vs evidenced">
        <div className="flex flex-wrap items-center gap-6">
          <div className="text-sm text-ink-600">
            Declared net worth
            <b className="block text-lg text-ink-800">{r.declared_total_band || "N/A"}</b>
          </div>
          <div className="text-sm text-ink-600">
            Evidenced
            <b className="block text-lg text-ink-800">{r.evidenced_total_band || "N/A"}</b>
          </div>
          <div className="min-w-[220px] flex-1">
            <div className="mb-1 flex justify-between text-xs text-ink-500">
              <span>Coverage</span>
              <span>{pct(r.coverage_pct)}</span>
            </div>
            <CoverageBar value={r.coverage_pct} />
          </div>
        </div>
        {r.consistency_notes.length > 0 ? (
          <ul className="mt-3 space-y-0.5 text-xs text-ink-500">
            {r.consistency_notes.map((n, i) => (
              <li key={i}>• {n}</li>
            ))}
          </ul>
        ) : null}
      </Panel>

      {/* Source groups: at a glance, with proofs */}
      <Panel title="Source groups: at a glance, with proofs">
        <div className="space-y-2">
          {view.groups.map((g) => {
            const line = lineByKind.get(g.kind);
            return (
              <details
                key={g.kind}
                open
                className="overflow-hidden rounded-lg border border-ink-200"
              >
                <summary className="flex cursor-pointer items-center gap-2 bg-ink-50 px-3 py-2">
                  <span
                    className={`h-2.5 w-2.5 rounded-full ${
                      g.corroborated ? "bg-emerald-600" : "bg-amber-500"
                    }`}
                  />
                  <span className="text-sm font-semibold">
                    {KIND_LABEL[g.kind] ?? g.kind}
                  </span>
                  <span className="ml-auto text-xs text-ink-500">
                    declared <b className="text-ink-800">{g.declared_band || "N/A"}</b> · evidenced{" "}
                    <b className="text-ink-800">{g.evidenced_band || "N/A"}</b>
                    {line ? <> · {pct(line.coverage_pct)}</> : null}
                  </span>
                </summary>
                <div className="px-3 py-2">
                  <div className="mb-1 text-xs text-ink-400">
                    {g.evidence_ids.length} supporting item(s):{" "}
                    <span className="font-mono">{g.evidence_ids.join(", ") || "none yet"}</span>
                  </div>
                  <CitationList citations={g.citations} />
                </div>
              </details>
            );
          })}
        </div>
      </Panel>

      {/* Gaps the system found */}
      <Panel title={`Gaps the system found (${view.gaps.length})`}>
        {view.gaps.length === 0 ? (
          <Empty>
            ✓ No outstanding gaps — declared net worth reconciles within tolerance and every
            declared source is corroborated. Ready for maker-checker review.
          </Empty>
        ) : (
          <ul className="space-y-2">
            {view.gaps.map((g) => (
              <li key={g.id} className="flex items-start gap-2">
                <SeverityPill severity={g.severity} />
                <div className="text-sm text-ink-800">
                  {g.summary}{" "}
                  <span className="font-mono text-xs text-ink-400">[{g.kind}]</span>
                  <div className="text-xs text-ink-500">{g.detail}</div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      {/* Information to request from the client */}
      {view.rfis.length > 0 ? (
        <Panel title={`Information to request from the client (${view.rfis.length})`}>
          <ul className="space-y-2">
            {view.rfis.map((rfi) => (
              <li key={rfi.id} className="flex items-start gap-2">
                <SeverityPill severity={rfi.priority} />
                <div className="text-sm text-ink-800">
                  {rfi.ask}
                  <div className="text-xs text-ink-500">
                    closes <span className="font-mono">{rfi.gap_id}</span>
                  </div>
                  {rfi.suggested_doc_types.length > 0 ? (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {rfi.suggested_doc_types.map((d) => (
                        <span
                          key={d}
                          className="rounded bg-ink-100 px-1.5 py-0.5 text-xs text-ink-600"
                        >
                          {d.replace(/_/g, " ")}
                        </span>
                      ))}
                    </div>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        </Panel>
      ) : null}
    </div>
  );
}
