"use client";

// The evidence panel: put real KYC documents into a case's custody before assessing it.
// A dossier is only as good as what it was given, so this surface is deliberately blunt
// about what is actually held: every file, its kind, its size, and (after an assessment)
// how many pages were read. Documents open only through the authenticated in-frame viewer.

import { useCallback, useRef, useState } from "react";
import { deleteDocument, uploadDocument } from "../lib/api";
import type { DocType, StoredDocument } from "../lib/types";
import { useDocumentViewer } from "./DocumentViewerModal";
import { Panel } from "./ui";

const DOC_TYPES: { value: DocType; label: string }[] = [
  { value: "bank_statement", label: "Bank statement" },
  { value: "fin_statement", label: "Financial statement" },
  { value: "registry_extract", label: "Registry extract" },
  { value: "passport", label: "Passport / ID" },
  { value: "other", label: "Other" },
];

const ACCEPT = ".pdf,.png,.jpg,.jpeg,.webp,.txt,.csv,.md";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function DocumentPanel({
  caseId,
  documents,
  onChange,
  disabled,
  seededDemoEvidence,
}: {
  caseId: string;
  documents: StoredDocument[];
  onChange: (documents: StoredDocument[]) => void;
  disabled: boolean;
  seededDemoEvidence: boolean;
}) {
  const [docType, setDocType] = useState<DocType>("bank_statement");
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const viewer = useDocumentViewer();

  const upload = useCallback(
    async (files: FileList | File[]) => {
      if (!caseId) {
        setError("Enter the subject name first: documents are filed against a case.");
        return;
      }
      setBusy(true);
      setError(null);
      const added: StoredDocument[] = [];
      const failures: string[] = [];
      for (const file of Array.from(files)) {
        try {
          added.push(await uploadDocument(caseId, file, docType));
        } catch (e) {
          failures.push(`${file.name}: ${e instanceof Error ? e.message : String(e)}`);
        }
      }
      // Keep whatever did upload: one rejected file must not discard the rest.
      if (added.length) onChange([...documents, ...added]);
      if (failures.length) setError(failures.join(" | "));
      setBusy(false);
    },
    [caseId, docType, documents, onChange],
  );

  async function remove(document_: StoredDocument) {
    setError(null);
    try {
      await deleteDocument(caseId, document_.id);
      onChange(documents.filter((d) => d.id !== document_.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <Panel title="Case documents">
      <p className="text-sm text-ink-500">
        Upload the KYC pack for this subject. The dossier is grounded in these documents
        and cites them by page. PDF, image, and text files are read; scanned pages are
        transcribed. The provenance banner above names where that runs.
      </p>

      <div className="mt-3 flex flex-wrap items-end gap-3">
        <label className="min-w-0 text-sm">
          <span className="text-ink-500">Document type</span>
          <select
            className="mt-1 w-full rounded border border-ink-200 px-2 py-1.5 text-sm sm:w-56"
            value={docType}
            onChange={(e) => setDocType(e.target.value as DocType)}
            disabled={disabled || busy}
          >
            {DOC_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div
        className={`mt-3 rounded border-2 border-dashed px-4 py-6 text-center text-sm transition-colors ${
          dragging ? "border-regblue-500 bg-regblue-50" : "border-ink-200 bg-ink-50"
        }`}
        onDragOver={(e) => {
          e.preventDefault();
          if (!disabled && !busy) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          if (!disabled && !busy && e.dataTransfer.files.length) void upload(e.dataTransfer.files);
        }}
      >
        <p className="text-ink-600">
          {busy ? "Uploading…" : "Drop files here, or"}{" "}
          {!busy ? (
            <button
              type="button"
              className="font-medium text-regblue-600 underline disabled:opacity-50"
              onClick={() => inputRef.current?.click()}
              disabled={disabled || busy}
            >
              choose files
            </button>
          ) : null}
        </p>
        <p className="mt-1 text-xs text-ink-400">PDF, PNG, JPEG, WebP, TXT, CSV, Markdown</p>
        <input
          ref={inputRef}
          data-demo="document-upload"
          type="file"
          className="hidden"
          multiple
          accept={ACCEPT}
          onChange={(e) => {
            if (e.target.files?.length) void upload(e.target.files);
            e.target.value = ""; // allow re-selecting the same file after a removal
          }}
        />
      </div>

      {error ? (
        <div className="mt-3 rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </div>
      ) : null}

      {documents.length ? (
        <ul
          data-demo="document-list"
          className="mt-3 divide-y divide-ink-100 rounded border border-ink-200"
        >
          {documents.map((d) => (
            <li key={d.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-sm">
              <button
                type="button"
                className="min-w-0 flex-1 truncate font-medium text-regblue-700 underline"
                onClick={() =>
                  viewer.openDocument({
                    title: d.filename,
                    path: d.uri,
                    declaredMimeType: d.mime_type,
                  })
                }
              >
                {d.filename}
              </button>
              <span className="text-xs text-ink-500">
                {DOC_TYPES.find((t) => t.value === d.doc_type)?.label ?? d.doc_type}
              </span>
              <span className="font-mono text-xs text-ink-400">{formatSize(d.size_bytes)}</span>
              {d.pages > 0 ? (
                <span className="font-mono text-xs text-ink-400">
                  {d.pages} {d.pages === 1 ? "page" : "pages"}
                </span>
              ) : null}
              <button
                type="button"
                className="text-xs text-ink-500 underline hover:text-red-600 disabled:opacity-50"
                onClick={() => void remove(d)}
                disabled={disabled || busy}
              >
                remove
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-3 text-sm text-ink-400" data-demo="document-list-empty">
          {seededDemoEvidence
            ? "No uploaded documents. This laptop demo uses a bundled fictional evidence corpus; uploads are optional."
            : "No documents yet. An assessment with nothing to read is refused rather than answered from guesswork."}
        </p>
      )}
    </Panel>
  );
}
