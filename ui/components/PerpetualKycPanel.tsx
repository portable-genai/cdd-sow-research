"use client";

// Perpetual KYC: run a cycle for the open subject, and read the tenant's review queue.
//
// Everything numeric shown here was computed server-side by deterministic code. The
// panel deliberately renders the score arithmetic line by line, because the point of the
// module is that a reviewer can see WHY the relationship moved rather than being handed
// a number. The narrative is model prose about a decision the model did not make, and is
// labelled as such.

import { useCallback, useState } from "react";
import { CitationList } from "./CitationCard";
import { Empty, Panel, ReviewBanner } from "./ui";
import { perpetualKycQueue, runPerpetualKyc } from "../lib/api";
import type { PerpetualKycAssessment, QueuePriority } from "../lib/types";

const PRIORITY_STYLE: Record<QueuePriority, string> = {
  urgent: "bg-red-100 text-red-800",
  high: "bg-amber-100 text-amber-800",
  standard: "bg-ink-100 text-ink-700",
  low: "bg-emerald-100 text-emerald-700",
};

function PriorityBadge({ priority }: { priority: QueuePriority }) {
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-xs font-semibold uppercase ${PRIORITY_STYLE[priority]}`}
    >
      {priority}
    </span>
  );
}

function signed(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(4)}`;
}

export function PerpetualKycPanel({
  subject,
}: {
  subject: { id: string; name: string; type: "individual" | "entity"; jurisdiction: string } | null;
}) {
  const [assessment, setAssessment] = useState<PerpetualKycAssessment | null>(null);
  const [queue, setQueue] = useState<PerpetualKycAssessment[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onRun = useCallback(async () => {
    if (!subject) return;
    setBusy(true);
    setError(null);
    try {
      setAssessment(await runPerpetualKyc({ subject }));
      setQueue(await perpetualKycQueue());
    } catch (exc) {
      setError(`Perpetual-KYC cycle failed: ${exc instanceof Error ? exc.message : String(exc)}`);
    } finally {
      setBusy(false);
    }
  }, [subject]);

  const onQueue = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      setQueue(await perpetualKycQueue());
    } catch (exc) {
      setError(`Review queue unavailable: ${exc instanceof Error ? exc.message : String(exc)}`);
    } finally {
      setBusy(false);
    }
  }, []);

  return (
    <div className="space-y-4">
      <Panel title="Perpetual KYC">
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <button
            className="rounded border border-regblue-600 px-3 py-1.5 text-sm font-medium text-regblue-700 disabled:opacity-50"
            onClick={() => void onRun()}
            disabled={busy || !subject}
          >
            Run monitoring cycle
          </button>
          <button
            className="rounded border border-ink-300 px-3 py-1.5 text-sm font-medium text-ink-700 disabled:opacity-50"
            onClick={() => void onQueue()}
            disabled={busy}
          >
            Refresh review queue
          </button>
          <span className="text-xs text-ink-500">
            Deterministic re-score over bank policy weights · always routed for human review
          </span>
        </div>
        {error ? (
          <div className="rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
            {error}
          </div>
        ) : null}
        {!assessment && !error ? (
          <Empty>
            No cycle run yet. A cycle compares the current sanctions, adverse-media and
            registry picture against the stored baseline.
          </Empty>
        ) : null}
      </Panel>

      {assessment ? (
        <Panel title={`Re-score — ${assessment.subject_name || assessment.subject_id}`}>
          <ReviewBanner requiresReview={assessment.requires_human_review} />
          <dl className="mb-3 grid min-w-0 grid-cols-1 gap-2 text-sm text-ink-700 sm:grid-cols-3">
            <div>
              <dt className="text-ink-400">Score</dt>
              <dd>
                {assessment.baseline_score.toFixed(4)} → {assessment.score.toFixed(4)} (
                {signed(assessment.score_delta)})
              </dd>
            </div>
            <div>
              <dt className="text-ink-400">Band</dt>
              <dd>
                {assessment.baseline_band} → {assessment.band} · tier {assessment.tier}
              </dd>
            </div>
            <div>
              <dt className="text-ink-400">Queue</dt>
              <dd>
                {assessment.queue_item ? (
                  <>
                    <PriorityBadge priority={assessment.queue_item.priority} /> due{" "}
                    {assessment.queue_item.sla_due}
                  </>
                ) : (
                  "not queued"
                )}
              </dd>
            </div>
          </dl>

          {assessment.queue_item ? (
            <>
              <h3 className="mb-1 text-xs font-semibold uppercase text-ink-500">
                Why this surfaced
              </h3>
              <ul className="mb-3 min-w-0 space-y-1 break-words text-sm text-ink-700">
                {assessment.queue_item.reasons.map((reason, i) => (
                  <li key={i}>· {reason}</li>
                ))}
              </ul>
              <p className="mb-3 text-xs text-ink-500">
                Routed to the human-review console:{" "}
                {assessment.queue_item.routed_to_hrz7 ? "yes" : "no (retained locally for retry)"}
              </p>
            </>
          ) : null}

          <h3 className="mb-1 text-xs font-semibold uppercase text-ink-500">Signals</h3>
          {assessment.signals.length === 0 ? (
            <Empty>No signals observed on the monitored edges.</Empty>
          ) : (
            <ul className="mb-3 min-w-0 space-y-1 break-words text-sm text-ink-700">
              {assessment.signals.map((signal) => (
                <li key={signal.key}>
                  <span className="font-mono text-xs text-regblue-700">
                    [{signal.change}/{signal.severity}]
                  </span>{" "}
                  <span className="text-ink-400">{signal.source}</span> {signal.summary}
                </li>
              ))}
            </ul>
          )}

          <h3 className="mb-1 text-xs font-semibold uppercase text-ink-500">Score arithmetic</h3>
          <ul className="mb-3 min-w-0 space-y-1 break-words font-mono text-xs text-ink-600">
            {assessment.uplifts.map((uplift) => (
              <li key={uplift.key}>
                {signed(uplift.uplift)} · {uplift.reason}
              </li>
            ))}
          </ul>

          {assessment.narrative ? (
            <>
              <h3 className="mb-1 text-xs font-semibold uppercase text-ink-500">
                Narrative (model prose over the computed figures)
              </h3>
              <p className="mb-3 min-w-0 break-words text-sm text-ink-800">
                {assessment.narrative}
              </p>
            </>
          ) : null}

          <CitationList citations={assessment.queue_item?.citations ?? []} />
        </Panel>
      ) : null}

      {queue ? (
        <Panel title={`Review queue (${queue.length})`}>
          {queue.length === 0 ? (
            <Empty>Nothing queued for your tenant.</Empty>
          ) : (
            <ul className="min-w-0 space-y-2 break-words text-sm text-ink-700">
              {queue.map((item) => (
                <li key={item.subject_id} className="flex flex-wrap items-center gap-2">
                  {item.queue_item ? <PriorityBadge priority={item.queue_item.priority} /> : null}
                  <span className="font-medium">{item.subject_name || item.subject_id}</span>
                  <span className="text-ink-500">
                    {item.baseline_score.toFixed(4)} → {item.score.toFixed(4)} ({item.band})
                  </span>
                  <span className="text-ink-400">
                    due {item.queue_item?.sla_due ?? "unset"}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      ) : null}
    </div>
  );
}
