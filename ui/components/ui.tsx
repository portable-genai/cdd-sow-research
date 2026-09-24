// Small shared presentational primitives for the B1 console.

import type { ReactNode } from "react";

import type { ReviewRouting } from "../lib/types";

/** A stable, framework-free hook for the browser demo scripts.
 *
 * Derived from the panel title rather than hand-written per call site, so a panel cannot ship
 * without one and a renamed panel renames its hook in step. Titles carry live values (a subject
 * name, a gap count), so everything from the first colon or bracket is dropped: the hook has to
 * stay the same string across runs or the demo script asserts on a selector that only existed for
 * one dossier.
 */
export function demoSlug(title: string): string {
  return title
    .split(/[:(]/)[0]
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export function Panel({
  title,
  demo,
  children,
}: {
  title: string;
  demo?: string;
  children: ReactNode;
}) {
  return (
    <section
      data-demo={`panel-${demo ?? demoSlug(title)}`}
      className="rounded-lg border border-ink-200 bg-white shadow-panel"
    >
      <h2 className="border-b border-ink-100 px-4 py-3 text-sm font-semibold text-ink-800">
        {title}
      </h2>
      <div className="p-4">{children}</div>
    </section>
  );
}

/** What the user is told about the hand-off to the review console, in plain words. */
export const REVIEW_ROUTING_TEXT: Record<Exclude<ReviewRouting, "not_required">, string> = {
  routed: "Sent to the review console.",
  failed: "Could not reach the review console; this item is not queued for review.",
  off: "Review routing is off in this deployment; this item is not queued for review.",
};

export function ReviewBanner({
  requiresReview,
  routing,
}: {
  requiresReview: boolean;
  /** What happened to the hand-off to the review console, when the API reports it. */
  routing?: ReviewRouting;
}) {
  if (!requiresReview) return null;
  return (
    <div className="mb-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs font-medium text-amber-800">
      HUMAN REVIEW REQUIRED: maker-checker gate (P-06). Do not act on this dossier until a
      qualified reviewer signs off.
      {routing && routing !== "not_required" ? (
        <p
          data-review-routing={routing}
          className={`mt-1 ${routing === "routed" ? "text-emerald-800" : "text-rose-800"}`}
        >
          {REVIEW_ROUTING_TEXT[routing]}
        </p>
      ) : null}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="text-sm text-ink-400">{children}</p>;
}
