/**
 * No em-dash in anything the console shows a reader.
 *
 * Business-facing text carries no em-dash. Comments may, because nobody outside the team reads
 * them; a label, a placeholder, a banner reason or a sentence in a panel may not. The scan parses
 * the console's sources with the TypeScript compiler this package already uses for `lint`, and
 * reads only what can reach the page: string literals, template text and JSX text. A comment is
 * never mistaken for copy, and a lone placeholder for an empty cell is caught the same way a
 * heading is. The server-rendered demo pages and the domain's own text are held by
 * `tests/unit/test_business_facing_text_has_no_em_dash.py`.
 */

import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import ts from "typescript";

const UI = join(dirname(fileURLToPath(import.meta.url)), "..");
const SOURCE_DIRS = ["app", "components", "lib", "embed"];
const DASH = String.fromCodePoint(0x2014);
/** The character, and the HTML entities JSX text could spell it as. */
const MARKS = [DASH, "&mdash;", "&#8212;", "&#x2014;"];
const VISIBLE = new Set([
  ts.SyntaxKind.StringLiteral,
  ts.SyntaxKind.NoSubstitutionTemplateLiteral,
  ts.SyntaxKind.TemplateHead,
  ts.SyntaxKind.TemplateMiddle,
  ts.SyntaxKind.TemplateTail,
  ts.SyntaxKind.JsxText,
]);

/** The line of every visible literal in `text` that carries an em-dash. */
function emDashLiterals(fileName, text) {
  const kind = /\.[jt]sx$/.test(fileName)
    ? ts.ScriptKind.TSX
    : fileName.endsWith(".ts")
      ? ts.ScriptKind.TS
      : ts.ScriptKind.JS;
  const source = ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true, kind);
  const lines = [];
  const visit = (node) => {
    if (VISIBLE.has(node.kind) && MARKS.some((mark) => node.text.toLowerCase().includes(mark))) {
      // A literal can span lines, so name the line that carries the mark rather than the first.
      const start = node.getStart(source);
      const raw = text.slice(start, node.end).toLowerCase();
      const offsets = MARKS.map((mark) => raw.indexOf(mark)).filter((offset) => offset >= 0);
      const at = start + (offsets.length ? Math.min(...offsets) : 0);
      lines.push(source.getLineAndCharacterOfPosition(at).line + 1);
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return lines;
}

function consoleSources() {
  const files = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) walk(path);
      else if (/\.(tsx?|jsx?|mjs)$/.test(name)) files.push(path);
    }
  };
  for (const dir of SOURCE_DIRS) {
    if (existsSync(join(UI, dir))) walk(join(UI, dir));
  }
  return files.sort();
}

// One line of each kind the scan has to tell apart. Lines 3 to 6 are copy a reader would see;
// the two comments are not.
const PROBE = [
  `// A comment ${DASH} may carry one.`,
  `/* So may a block comment ${DASH} like this. */`,
  `const heading = "Subject ${DASH} a borrower";`,
  "const title = `Re-score " + DASH + " ${name} " + DASH + " today`;",
  `export const Cell = ({ v }) => <p>Measured ${DASH} {v ?? "${DASH}"}</p>;`,
  `const entity = "&mdash;";`,
].join(String.fromCharCode(10));

test("the scan reads string, template and JSX text, and leaves comments alone", () => {
  assert.deepEqual(emDashLiterals("probe.tsx", PROBE), [3, 4, 4, 5, 5, 6]);
});

test("no text the console shows carries an em-dash", () => {
  const files = consoleSources();
  assert.ok(files.length > 2, "the scan found almost no console sources, so it checked nothing");
  const found = files.flatMap((file) =>
    emDashLiterals(file, readFileSync(file, "utf8")).map((line) => `${relative(UI, file)}:${line}`),
  );
  assert.deepEqual(
    found,
    [],
    "the console shows an em-dash at these lines. Rewrite each with a colon, a comma, " +
      "parentheses or two sentences; an empty value reads n/a.",
  );
});
