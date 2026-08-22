// Renders a source-and-page Citation chip. Every dossier claim is traceable.
//
import { useState } from "react";

import { createCitationContinuation } from "../lib/api";
import {
  emitCitationNavigation,
  hasAgentNavigationChannel,
} from "../lib/embed/navigation";
import type { Citation } from "../lib/types";
import { useDocumentViewer } from "./DocumentViewerModal";

const TYPE_LABEL: Record<string, string> = {
  document: "DOC",
  registry: "REG",
  media: "MEDIA",
  regulation: "REGUL",
};

export function CitationCard({ citation }: { citation: Citation }) {
  const viewer = useDocumentViewer();
  const [continuing, setContinuing] = useState(false);
  const [continuationError, setContinuationError] = useState("");
  const page = citation.page != null ? ` p.${citation.page}` : "";
  const isDocument =
    citation.source_type === "document" &&
    Boolean(citation.url) &&
    !/^https?:\/\//i.test(citation.url ?? "");
  const publicOrigin = publicCitationOrigin(citation.url);
  const canContinue = Boolean(citation.continuation_id) && hasAgentNavigationChannel();
  async function continueCitation() {
    if (!citation.continuation_id || continuing) return;
    setContinuing(true);
    setContinuationError("");
    try {
      const result = await createCitationContinuation(citation.continuation_id);
      emitCitationNavigation(result.continuation_url);
    } catch {
      setContinuationError("Unable to open protected source");
    } finally {
      setContinuing(false);
    }
  }
  return (
    <span
      className="inline-flex max-w-full flex-wrap items-center gap-1 rounded border border-ink-200 bg-ink-50 px-2 py-0.5 text-xs text-ink-700"
      title={citation.snippet || citation.title}
    >
      <span className="font-mono font-semibold text-regblue-700">
        {TYPE_LABEL[citation.source_type] ?? citation.source_type}
      </span>
      <span className="font-mono">
        {citation.source_id}
        {page}
      </span>
      {canContinue ? (
        <button
          type="button"
          className="text-regblue-600 underline disabled:text-ink-400"
          disabled={continuing}
          onClick={() => void continueCitation()}
        >
          {continuing ? "preparing…" : "open protected source"}
        </button>
      ) : isDocument ? (
        <button
          type="button"
          className="text-regblue-600 underline"
          onClick={() =>
            viewer.openDocument({
              title: citation.title,
              path: citation.url ?? "",
              page: citation.page,
            })
          }
        >
          view
        </button>
      ) : null}
      {!isDocument && publicOrigin ? <span className="text-ink-400">{publicOrigin}</span> : null}
      {continuationError ? (
        <span className="text-red-600" role="alert">
          {continuationError}
        </span>
      ) : null}
    </span>
  );
}

function publicCitationOrigin(url: string | undefined): string {
  if (!url || !/^https:\/\//i.test(url)) return "";
  try {
    return new URL(url).origin;
  } catch {
    return "";
  }
}

export function CitationList({ citations }: { citations: Citation[] }) {
  if (!citations.length) {
    return <span className="text-xs text-ink-400">(no citations)</span>;
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {citations.map((c, i) => (
        <CitationCard key={`${c.source_id}-${c.page ?? "x"}-${i}`} citation={c} />
      ))}
    </div>
  );
}
