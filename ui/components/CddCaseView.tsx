// Renders a full CDD dossier: source of wealth, risk rating, watchlist screening,
// adverse media, ownership.

import type { CddCase } from "../lib/types";
import { CitationList } from "./CitationCard";
import { RiskBadge, SeverityPill } from "./RiskBadge";
import { Empty, Panel, ReviewBanner } from "./ui";

export function CddCaseView({ caseData }: { caseData: CddCase }) {
  const { subject, sow, rating, adverse_media, ownership, screening } = caseData;
  return (
    <div className="space-y-4">
      <ReviewBanner requiresReview={caseData.requires_human_review} />

      <Panel title={`Subject — ${subject.name}`}>
        <dl className="grid min-w-0 grid-cols-1 gap-2 text-sm text-ink-700 sm:grid-cols-2">
          <div>
            <dt className="text-ink-400">Type</dt>
            <dd>{subject.type}</dd>
          </div>
          <div>
            <dt className="text-ink-400">Jurisdiction</dt>
            <dd>{subject.jurisdiction || "n/a"}</dd>
          </div>
          <div>
            <dt className="text-ink-400">Risk band</dt>
            <dd>
              <RiskBadge band={rating.band} />
            </dd>
          </div>
          <div>
            <dt className="text-ink-400">SoW confidence</dt>
            <dd>{(sow.confidence * 100).toFixed(0)}%</dd>
          </div>
        </dl>
      </Panel>

      <Panel title="Source of wealth">
        <p className="mb-3 min-w-0 break-words text-sm text-ink-800">{sow.narrative}</p>
        <ul className="mb-3 min-w-0 space-y-1 break-words text-sm text-ink-700">
          {sow.sources.map((src, i) => (
            <li key={i}>
              <span className="font-mono text-xs text-regblue-700">[{src.kind}]</span>{" "}
              {src.description}{" "}
              <span className="text-ink-400">({src.est_value_band || "band n/a"})</span>
            </li>
          ))}
        </ul>
        <CitationList citations={sow.citations} />
      </Panel>

      <Panel title="Risk rating">
        <p className="mb-3 text-sm text-ink-800">{rating.rationale}</p>
        <ul className="mb-3 space-y-1 text-sm text-ink-700">
          {rating.factors.map((f, i) => (
            <li key={i}>
              <span className="font-mono text-xs">{f.name}</span> · weight{" "}
              {f.weight.toFixed(2)} · {f.present ? "present" : "absent"}
              {f.detail ? <span className="text-ink-500"> ({f.detail})</span> : null}
            </li>
          ))}
        </ul>
        <CitationList citations={rating.citations} />
      </Panel>

      <Panel
        title={
          screening
            ? `Watchlist screening (${screening.alerts.length} ${
                screening.alerts.length === 1 ? "alert" : "alerts"
              })`
            : "Watchlist screening"
        }
      >
        {!screening ? (
          <Empty>Not screened: no watchlist snapshot was available for this case.</Empty>
        ) : screening.alerts.length === 0 ? (
          <p className="text-sm text-ink-700">
            <span className="rounded bg-emerald-100 px-1 text-xs font-semibold text-emerald-700">
              CLEAR
            </span>{" "}
            No matches for &ldquo;{screening.query_name}&rdquo; against{" "}
            {screening.sources.join(", ") || "the synced lists"}{" "}
            <span className="text-ink-400">(snapshot {screening.lists_version})</span>
          </p>
        ) : (
          <div className="space-y-2">
            <p className="text-xs text-ink-400">
              Screened &ldquo;{screening.query_name}&rdquo; against{" "}
              {screening.sources.join(", ")} · snapshot {screening.lists_version}. Open
              alerts require analyst disposition (maker-checker); they never auto-block.
            </p>
            <ul className="space-y-2 text-sm text-ink-700">
              {screening.alerts.map((a) => (
                <li key={a.id} className="flex flex-wrap items-center gap-2">
                  <span className="rounded bg-red-100 px-1 text-xs font-semibold text-red-700">
                    {a.status.toUpperCase()}
                  </span>
                  <span className="font-mono text-xs text-regblue-700">{a.entry.source}</span>
                  <span>{a.matched_name}</span>
                  <span className="text-ink-400">
                    · score {a.score.toFixed(2)}
                    {a.entry.programs.length ? ` · ${a.entry.programs.join(", ")}` : ""}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </Panel>

      <Panel
        title={
          adverse_media
            ? `Adverse media (${adverse_media.findings.length})`
            : "Adverse media"
        }
      >
        {!adverse_media ? (
          <Empty>
            Not screened: no adverse-media backend was reachable for this case.
          </Empty>
        ) : adverse_media.findings.length === 0 ? (
          <p className="text-sm text-ink-700">
            <span className="rounded bg-emerald-100 px-1 text-xs font-semibold text-emerald-700">
              CLEAR
            </span>{" "}
            No adverse media for &ldquo;{adverse_media.subject_name}&rdquo; across{" "}
            {adverse_media.sources.join(", ") || "the configured sources"}
          </p>
        ) : (
          <ul className="space-y-2 text-sm text-ink-700">
            {adverse_media.findings.map((f, i) => (
              <li key={i} className="flex flex-wrap items-center gap-2">
                <SeverityPill severity={f.severity} />
                <span className="font-mono text-xs text-regblue-700">{f.category}</span>
                <span>{f.headline}</span>
                <span className="text-ink-400">· {f.publisher}</span>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Panel title="Beneficial ownership">
        {!ownership || ownership.owners.length === 0 ? (
          <Empty>No corporate ownership tree for this subject.</Empty>
        ) : (
          <ul className="space-y-1 text-sm text-ink-700">
            {ownership.owners.map((o, i) => (
              <li key={i}>
                {o.name} · {o.pct}%{" "}
                {o.is_pep ? (
                  <span className="rounded bg-red-100 px-1 text-xs font-semibold text-red-700">
                    PEP
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
