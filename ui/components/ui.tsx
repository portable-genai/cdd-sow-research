// Small shared presentational primitives for the B1 console.

import type { ReactNode } from "react";

export function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-ink-200 bg-white shadow-panel">
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
