// Small shared presentational primitives for the B1 console.

import type { ReactNode } from "react";

/** A stable, framework-free hook for the browser demo scripts.
 *
 * Derived from the panel title rather than hand-written per call site, so a panel cannot ship
 * without one and a renamed panel renames its hook in step. Titles carry live values (a subject
 * name, a gap count), so everything from the first em-dash or bracket is dropped: the hook has to
 * stay the same string across runs or the demo script asserts on a selector that only existed for
 * one dossier.
 */
export function demoSlug(title: string): string {
  return title
    .split(/[—(]/)[0]
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

export function ReviewBanner({ requiresReview }: { requiresReview: boolean }) {
  if (!requiresReview) return null;
  return (
    <div className="mb-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs font-medium text-amber-800">
      HUMAN REVIEW REQUIRED: maker-checker gate (P-06). Do not act on this dossier until a
      qualified reviewer signs off.
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="text-sm text-ink-400">{children}</p>;
}
