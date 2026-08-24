import assert from "node:assert/strict";
import { test } from "node:test";

import { getDocument } from "pdfjs-dist/legacy/build/pdf.mjs";

// DocumentViewerModal.tsx broke at 6.2.108 in three places the type checker alone could catch
// (a call site referencing a removed member) and one it could NOT: `page.render` accepting a
// `canvasContext`-only object is still type-correct there, since `canvasContext` stayed optional
// in `RenderParameters`, but a Node canvas is not available in this test environment, so what is
// exercised here is the document/page lifecycle only — `getDocument`, `getPage`, `getViewport`
// and `destroy` — against the REAL installed library rather than its type declarations. The
// build-time proof that the render call still compiles is `next build` in `make ui-check`;
// this is the runtime proof that the destroy() move is real and not a stale type stub.
//
// The PDF below is the minimal valid single-object-per-line document pdf.js will parse: one
// page, no content stream, no fonts. Not a fixture borrowed from anywhere; built to be the
// smallest input that exercises getPage/getViewport without needing rendered content.
const MINIMAL_PDF = new TextEncoder().encode(
  [
    "%PDF-1.4",
    "1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj",
    "2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj",
    "3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj",
    "trailer<</Size 4/Root 1 0 R>>",
    "%%EOF",
  ].join("\n"),
);

test("PDFDocumentProxy no longer carries destroy(); the loading task does", async () => {
  const task = getDocument({ data: MINIMAL_PDF });
  const document_ = await task.promise;
  try {
    assert.equal(
      typeof document_.destroy,
      "undefined",
      "PDFDocumentProxy.destroy exists again; DocumentViewerModal's destroyPdf should call it " +
        "directly instead of task.destroy()",
    );
    assert.equal(document_.numPages, 1);
    const page = await document_.getPage(1);
    const viewport = page.getViewport({ scale: 1 });
    assert.equal(viewport.width, 200);
    assert.equal(viewport.height, 200);
  } finally {
    // What DocumentViewerModal's destroyPdf actually calls. Asserting it does not throw is the
    // whole point: the pre-fix code called document_.destroy(), a function that does not exist
    // on this object, which throws at the first unmount or error path in a real session.
    await task.destroy();
  }
});
