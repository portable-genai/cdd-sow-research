"use client";

import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";

import { readDocument } from "../lib/api";

const MAX_DOCUMENT_BYTES = 15 * 1024 * 1024;
const MAX_PDF_PAGES = 50;
const MAX_IMAGE_PIXELS = 24_000_000;
const MAX_PDF_CANVAS_PIXELS = 16_000_000;
const ALLOWED_MEDIA = new Set([
  "application/pdf",
  "image/jpeg",
  "image/png",
  "image/webp",
  "text/plain",
  "text/csv",
  "text/markdown",
]);

export interface ViewerResource {
  title: string;
  path: string;
  declaredMimeType?: string;
  page?: number | null;
}

interface ViewerContextValue {
  openDocument: (resource: ViewerResource) => void;
}

const ViewerContext = createContext<ViewerContextValue | null>(null);

export function useDocumentViewer(): ViewerContextValue {
  const context = useContext(ViewerContext);
  if (!context) throw new Error("useDocumentViewer must be used inside DocumentViewerProvider");
  return context;
}

export function DocumentViewerProvider({ children }: { children: ReactNode }) {
  const [resource, setResource] = useState<ViewerResource | null>(null);
  return (
    <ViewerContext.Provider value={{ openDocument: setResource }}>
      {children}
      {resource ? (
        <DocumentViewerModal resource={resource} onClose={() => setResource(null)} />
      ) : null}
    </ViewerContext.Provider>
  );
}

type Rendered =
  | { kind: "text"; text: string }
  | { kind: "image"; objectUrl: string }
  | { kind: "pdf" };

export function detectMediaType(bytes: Uint8Array, declared: string): string {
  if (
    bytes.length >= 5 &&
    bytes[0] === 0x25 &&
    bytes[1] === 0x50 &&
    bytes[2] === 0x44 &&
    bytes[3] === 0x46 &&
    bytes[4] === 0x2d
  ) {
    return "application/pdf";
  }
  if (
    bytes.length >= 8 &&
    bytes[0] === 0x89 &&
    bytes[1] === 0x50 &&
    bytes[2] === 0x4e &&
    bytes[3] === 0x47 &&
    bytes[4] === 0x0d &&
    bytes[5] === 0x0a &&
    bytes[6] === 0x1a &&
    bytes[7] === 0x0a
  ) {
    return "image/png";
  }
  if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) {
    return "image/jpeg";
  }
  if (
    bytes.length >= 12 &&
    String.fromCharCode(...bytes.slice(0, 4)) === "RIFF" &&
    String.fromCharCode(...bytes.slice(8, 12)) === "WEBP"
  ) {
    return "image/webp";
  }
  if (declared.startsWith("text/") && !bytes.slice(0, 4_096).includes(0)) {
    return "text/plain";
  }
  return "application/octet-stream";
}

function mediaFamily(mediaType: string): string {
  return mediaType.startsWith("text/") ? "text" : mediaType;
}

function assertMediaPolicy(
  declared: string,
  responseType: string,
  detected: string,
  size: number,
): void {
  if (size <= 0 || size > MAX_DOCUMENT_BYTES) {
    throw new Error(`Document size must be between 1 byte and ${MAX_DOCUMENT_BYTES} bytes.`);
  }
  if (!ALLOWED_MEDIA.has(declared)) throw new Error(`Unsupported declared media type: ${declared}`);
  if (!ALLOWED_MEDIA.has(responseType)) throw new Error(`Unsupported response media type: ${responseType}`);
  if (
    mediaFamily(declared) !== mediaFamily(responseType) ||
    mediaFamily(declared) !== mediaFamily(detected)
  ) {
    throw new Error("Document media type does not match its declared and detected content.");
  }
}

function DocumentViewerModal({
  resource,
  onClose,
}: {
  resource: ViewerResource;
  onClose: () => void;
}) {
  const [rendered, setRendered] = useState<Rendered | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const pdfContainer = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    let objectUrl = "";
    let destroyPdf: (() => Promise<void>) | undefined;
    (async () => {
      try {
        const response = await readDocument(resource.path);
        if (
          response.contentLength !== null &&
          (response.contentLength <= 0 || response.contentLength > MAX_DOCUMENT_BYTES)
        ) {
          throw new Error("Document exceeds the in-frame viewer size limit.");
        }
        const bytes = new Uint8Array(await response.blob.arrayBuffer());
        const declared = (resource.declaredMimeType || response.contentType).toLowerCase();
        const detected = detectMediaType(bytes, declared);
        assertMediaPolicy(declared, response.contentType.toLowerCase(), detected, bytes.byteLength);
        if (cancelled) return;

        if (mediaFamily(detected) === "text") {
          setRendered({ kind: "text", text: new TextDecoder("utf-8", { fatal: true }).decode(bytes) });
          return;
        }
        if (detected.startsWith("image/")) {
          objectUrl = URL.createObjectURL(new Blob([bytes], { type: detected }));
          const dimensions = await inspectImage(objectUrl);
          if (dimensions.width * dimensions.height > MAX_IMAGE_PIXELS) {
            throw new Error("Image dimensions exceed the in-frame viewer limit.");
          }
          if (!cancelled) setRendered({ kind: "image", objectUrl });
          return;
        }

        const pdfjs = await import("pdfjs-dist");
        pdfjs.GlobalWorkerOptions.workerSrc =
          "/agent/assets/pdfjs/4.10.38/pdf.worker.min.mjs";
        const task = pdfjs.getDocument({ data: bytes });
        const document_ = await task.promise;
        destroyPdf = () => document_.destroy();
        if (document_.numPages > MAX_PDF_PAGES) {
          throw new Error(`PDF exceeds the ${MAX_PDF_PAGES}-page viewer limit.`);
        }
        if (cancelled) return;
        setRendered({ kind: "pdf" });
        await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
        const container = pdfContainer.current;
        if (!container) return;
        const requested = resource.page;
        const pages =
          requested != null && requested >= 1 && requested <= document_.numPages
            ? [requested]
            : Array.from({ length: document_.numPages }, (_, index) => index + 1);
        for (const pageNumber of pages) {
          if (cancelled) break;
          const page = await document_.getPage(pageNumber);
          const initial = page.getViewport({ scale: 1 });
          const scale = Math.min(1.5, 1_200 / initial.width);
          const viewport = page.getViewport({ scale });
          if (viewport.width * viewport.height > MAX_PDF_CANVAS_PIXELS) {
            page.cleanup();
            throw new Error(`PDF page ${pageNumber} exceeds the canvas rendering limit.`);
          }
          const canvas = document.createElement("canvas");
          canvas.width = Math.ceil(viewport.width);
          canvas.height = Math.ceil(viewport.height);
          canvas.setAttribute("aria-label", `PDF page ${pageNumber}`);
          canvas.style.cssText = "display:block;max-width:100%;height:auto;margin:0 auto 16px";
          container.append(canvas);
          const context = canvas.getContext("2d");
          if (!context) throw new Error("Canvas rendering is unavailable.");
          await page.render({ canvasContext: context, viewport }).promise;
          page.cleanup();
        }
      } catch (caught) {
        if (objectUrl) {
          URL.revokeObjectURL(objectUrl);
          objectUrl = "";
        }
        await destroyPdf?.();
        destroyPdf = undefined;
        pdfContainer.current?.replaceChildren();
        if (!cancelled) {
          setError(caught instanceof Error ? caught.message : String(caught));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      void destroyPdf?.();
      pdfContainer.current?.replaceChildren();
    };
  }, [resource]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink-950/70 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={`Viewing ${resource.title}`}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section className="flex max-h-[94vh] w-full max-w-5xl flex-col overflow-hidden rounded bg-white shadow-panel">
        <header className="flex items-center justify-between gap-3 border-b border-ink-200 px-4 py-3">
          <h2 className="truncate text-sm font-semibold text-ink-900">{resource.title}</h2>
          <button
            type="button"
            className="rounded border border-ink-200 px-3 py-1 text-sm"
            onClick={onClose}
          >
            Close
          </button>
        </header>
        <div className="min-h-72 overflow-auto p-4">
          {loading ? <p className="text-sm text-ink-500">Loading authenticated document…</p> : null}
          {error ? (
            <div className="rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
              {error}
            </div>
          ) : null}
          {rendered?.kind === "text" ? (
            <pre className="whitespace-pre-wrap break-words text-sm text-ink-800">{rendered.text}</pre>
          ) : null}
          {rendered?.kind === "image" ? (
            // This object URL is created from authenticated bytes and revoked during cleanup.
            // eslint-disable-next-line @next/next/no-img-element
            <img className="mx-auto max-h-[78vh] max-w-full" src={rendered.objectUrl} alt={resource.title} />
          ) : null}
          <div ref={pdfContainer} hidden={rendered?.kind !== "pdf"} />
        </div>
      </section>
    </div>
  );
}

function inspectImage(url: string): Promise<{ width: number; height: number }> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve({ width: image.naturalWidth, height: image.naturalHeight });
    image.onerror = () => reject(new Error("Image content could not be decoded."));
    image.src = url;
  });
}
