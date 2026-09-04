import { createHash } from "node:crypto";
import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import ts from "typescript";

// pdfjs-dist's own version, read from ITS package.json rather than repeated as a literal: the
// runtime component (DocumentViewerModal.tsx) requests the worker at a path keyed by this same
// value read off the library at import time. A bump that moved one and not the other would
// silently serve a stale worker against a newer library at runtime.
const { version: pdfjsVersion } = JSON.parse(
  await readFile(
    path.join(process.cwd(), "node_modules", "pdfjs-dist", "package.json"),
    "utf8",
  ),
);

const uiRoot = process.cwd();
const sourcePath = path.join(uiRoot, "embed", "cdd-agent.ts");
const outputDirectory = path.join(uiRoot, "public", "embed", "v1");
const outputPath = path.join(outputDirectory, "cdd-agent.js");
const integrityPath = path.join(outputDirectory, "cdd-agent.js.sri");
const pdfWorkerSource = path.join(
  uiRoot,
  "node_modules",
  "pdfjs-dist",
  "build",
  "pdf.worker.min.mjs",
);
const pdfWorkerDirectory = path.join(uiRoot, "public", "assets", "pdfjs", pdfjsVersion);
const pdfWorkerOutput = path.join(pdfWorkerDirectory, "pdf.worker.min.mjs");

const source = await readFile(sourcePath, "utf8");
const transpiled = ts.transpileModule(source, {
  compilerOptions: {
    target: ts.ScriptTarget.ES2021,
    module: ts.ModuleKind.None,
    strict: true,
    removeComments: true,
  },
  fileName: sourcePath,
  reportDiagnostics: true,
});
const errors = (transpiled.diagnostics ?? []).filter(
  (diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error,
);
if (errors.length > 0) {
  for (const error of errors) {
    console.error(ts.flattenDiagnosticMessageText(error.messageText, "\n"));
  }
  process.exit(1);
}

const output = `/* cdd-sow-research embed loader v1. Generated from embed/cdd-agent.ts. */\n${transpiled.outputText}`;
const integrity = `sha384-${createHash("sha384").update(output).digest("base64")}`;
await mkdir(outputDirectory, { recursive: true });
await writeFile(outputPath, output, "utf8");
await writeFile(integrityPath, `${integrity}\n`, "utf8");
await mkdir(pdfWorkerDirectory, { recursive: true });
await copyFile(pdfWorkerSource, pdfWorkerOutput);
console.log(`${path.relative(uiRoot, outputPath)} ${integrity}`);
