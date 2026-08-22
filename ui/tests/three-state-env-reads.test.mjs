// Every security-relevant environment read in `ui/` resolves THREE states, proved by scanning
// the shipped JavaScript and TypeScript.
//
// The Python gate has `tests/unit/test_three_state_env_reads.py`, which fails the build on any
// two-state `os.environ.get(name, default)` in the shipped source. It cannot see this directory:
// it walks `src`, `scripts` and `eval` and parses with `ast`, so no `.mjs`, `.ts` or `.tsx` file
// was ever read. That is not a theoretical hole. `human-review-console` passed the Python
// sweep with three two-state reads in its `ui/` tier, one of which dropped
// Strict-Transport-Security from every response when a variable was emptied.
//
// The rule, mirroring the Python one exactly. `env.X || "default"` and `env.X ?? "default"`
// collapse three states into two:
//
//     unset          -> nobody expressed an intent, so a documented default may stand
//     set and empty  -> an intent WAS expressed and it names nothing, so fail closed
//     set with value -> use it
//
// The middle state is the dangerous one. Folded into the first, a value an operator deliberately
// emptied inherits the default, and where the default is the more permissive branch (a loopback
// API origin widening `connect-src`, a shipped `frame-ancestors`, a demo profile) emptying a
// variable OPENS the console. Worse, the emptied deployment is then byte-identical to one that
// never configured the variable, so a deliberate lockdown cannot be told from an omission.
//
// A Next.js wrinkle makes it worse here than in the service tier: `NEXT_PUBLIC_*` reads are
// INLINED AT BUILD TIME, so an emptied value is frozen into the bundle and cannot be corrected
// by fixing the environment at start-up.
//
// The scan is stricter than the two operators the defect wore: it fails on ANY direct
// `process.env.X` read in the shipped source, whatever is done with the value, because
// `const raw = process.env.X; if (!raw) return DEFAULT;` is the same collapse spelled over two
// lines. `lib/env-setting.mjs` returns a setting whose `isUnset` / `isConfiguredEmpty` /
// `hasValue` are mutually exclusive, so the middle state has somewhere to go.
//
// Two escapes, both narrow and both written down:
//
//   1. an EXACT-MATCH relaxation, the raw value compared against a literal
//      (`process.env.X === "1"`). There is no default to inherit and no truthiness to be
//      surprised by, so unset, emptied and "0" alike all mean no. Fail-closed by construction;
//   2. a variable named in TWO_STATE_READS_WITH_A_REASON below, which carries no posture at all.
//      Each entry needs a written reason, and a second test fails the build when an entry stops
//      matching anything, so the exemption list cannot quietly outlive its reads.
//
// `tests/` is not scanned: a test harness legitimately manipulates the environment, and none of
// it ships.

import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { readEnvSetting } from "../lib/env-setting.mjs";

const UI_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

/** Everything that ships or runs in the browser tier. */
const SCANNED_EXTENSIONS = [".mjs", ".js", ".jsx", ".ts", ".tsx", ".mts", ".cts"];

/** Never scanned: build output, third-party code, and the harness that fakes environments. */
const SKIPPED_DIRECTORIES = new Set(["node_modules", ".next", "tests", ".git", "out", "coverage"]);

/**
 * The ONE module allowed to touch `process.env[name]` directly, because reading it is its whole
 * job. Everything else calls `readEnvSetting`.
 */
const THREE_STATE_READER_MODULE = join("lib", "env-setting.mjs");

/**
 * variable name -> why a two-state read of it is not a posture decision. Adding an entry is a
 * reviewable claim, not a way past the test: if the variable can widen access, relax a check,
 * choose a weaker credential path, name a host, an origin, an audience or a profile, it does not
 * belong here and the read belongs in `readEnvSetting`.
 */
export const TWO_STATE_READS_WITH_A_REASON = {
  CDD_EXPECTED_MANIFEST_SHA256:
    "a RESTRICTION, and the only place its three states are decided is requireReviewedDigest, " +
    "which is handed the raw value. Unset and emptied both mean no reviewed digest was named, " +
    "and both refuse in production; neither can widen anything, because the value is only ever " +
    "compared against a digest this process computed itself.",
  CDD_EXPECTED_SETTINGS_SHA256:
    "the same restriction for the settings digest, decided in the same function and by the same " +
    "rule: naming nothing refuses, and no value of this variable relaxes a check.",
  NODE_ENV:
    "set by the toolchain, never by an operator, and compared against the exact literal "
    + "'production'. Unset and emptied both land on the DEV branch, which is the closed "
    + "direction here: dev adds no allowance, it only declines to assert production-only "
    + "headers.",
};

/** Recursively collect the shipped sources this rule applies to. */
export function scannedSources(root = UI_ROOT) {
  const found = [];
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    if (entry.name.startsWith(".")) continue;
    const full = join(root, entry.name);
    if (entry.isDirectory()) {
      if (SKIPPED_DIRECTORIES.has(entry.name)) continue;
      found.push(...scannedSources(full));
      continue;
    }
    if (SCANNED_EXTENSIONS.some((extension) => entry.name.endsWith(extension))) found.push(full);
  }
  return found.sort();
}

/**
 * Blank out comments, preserving line numbering and every string literal.
 *
 * Comments are removed because these modules deliberately QUOTE the two-state reads they
 * replaced, to say what the defect was, and a scanner that could not tell code from prose would
 * forbid explaining the very thing it guards. String literals are KEPT so the exact-match escape
 * can still see the literal it compares against. The walk is character by character rather than
 * regex-based so that `"http://127.0.0.1:8080"` is not mistaken for a line comment.
 */
export function codeOnly(source) {
  let out = "";
  let i = 0;
  while (i < source.length) {
    const two = source.slice(i, i + 2);
    if (two === "//") {
      while (i < source.length && source[i] !== "\n") i += 1;
      continue;
    }
    if (two === "/*") {
      i += 2;
      while (i < source.length && source.slice(i, i + 2) !== "*/") {
        if (source[i] === "\n") out += "\n";
        i += 1;
      }
      i += 2;
      continue;
    }
    const quote = source[i];
    if (quote === '"' || quote === "'" || quote === "`") {
      out += quote;
      i += 1;
      while (i < source.length && source[i] !== quote) {
        if (source[i] === "\\") {
          out += source[i];
          i += 1;
        }
        if (i < source.length) {
          out += source[i];
          i += 1;
        }
      }
      out += quote;
      i += 1;
      continue;
    }
    out += source[i];
    i += 1;
  }
  return out;
}

/** `process.env.NAME`, and the bracket form, wherever they appear in real code. */
const DIRECT_READ = /process\.env(?:\.([A-Za-z_$][\w$]*)|\[\s*["'`]([^"'`]+)["'`]\s*\])/g;

/** The exact-match relaxation: the raw read compared against a literal, with no default. */
const EXACT_MATCH =
  /process\.env(?:\.[A-Za-z_$][\w$]*|\[\s*["'`][^"'`]+["'`]\s*\])\s*(?:===|!==|==|!=)\s*["'`]/;

/** @returns {{file: string, line: number, variable: string, text: string}[]} */
export function findings(sources = scannedSources()) {
  const out = [];
  for (const file of sources) {
    const relativePath = relative(UI_ROOT, file);
    if (relativePath === THREE_STATE_READER_MODULE) continue;
    const lines = codeOnly(readFileSync(file, "utf8")).split("\n");
    lines.forEach((text, index) => {
      for (const match of text.matchAll(DIRECT_READ)) {
        const variable = match[1] ?? match[2];
        if (variable in TWO_STATE_READS_WITH_A_REASON) continue;
        // The exact-match escape is judged on the line, because that is the whole expression:
        // a comparison against a literal has no default to inherit.
        if (EXACT_MATCH.test(text)) continue;
        out.push({ file: relativePath, line: index + 1, variable, text: text.trim() });
      }
    });
  }
  return out;
}

test("no shipped file under ui/ reads an environment variable in two states", () => {
  const found = findings();
  const report = found
    .map((f) => `  ${f.file}:${f.line}  ${f.variable}\n      ${f.text}`)
    .join("\n");
  assert.equal(
    found.length,
    0,
    `two-state environment reads in the interface tier:\n${report}\n\n` +
      "Read it with readEnvSetting(process.env, NAME) from lib/env-setting.mjs and decide the " +
      "three states, or add the variable to TWO_STATE_READS_WITH_A_REASON with a reason that " +
      "survives review.",
  );
});

test("the scan finds the exact defect it exists for", () => {
  // A checker never observed red is indistinguishable from one that asserts nothing. The planted
  // line is the shape this repository actually shipped: the API base widening connect-src when a
  // variable is emptied.
  const planted = 'const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8080";';
  const matches = [...planted.matchAll(DIRECT_READ)].map((m) => m[1] ?? m[2]);
  assert.deepEqual(matches, ["NEXT_PUBLIC_API_BASE"]);

  const nullish = 'const b = process.env.NEXT_PUBLIC_API_BASE?.replace(/\\/$/, "") ?? "x";';
  assert.deepEqual(
    [...nullish.matchAll(DIRECT_READ)].map((m) => m[1] ?? m[2]),
    ["NEXT_PUBLIC_API_BASE"],
  );

  const bracket = 'const v = process.env["NEXT_PUBLIC_API_BASE"] || "x";';
  assert.deepEqual(
    [...bracket.matchAll(DIRECT_READ)].map((m) => m[1] ?? m[2]),
    ["NEXT_PUBLIC_API_BASE"],
  );

  // Two lines, no operator: the same collapse, and the reason the scan bans the READ rather
  // than the operator.
  const spread = "const raw = process.env.NEXT_PUBLIC_API_BASE;";
  assert.deepEqual(
    [...spread.matchAll(DIRECT_READ)].map((m) => m[1] ?? m[2]),
    ["NEXT_PUBLIC_API_BASE"],
  );
});

test("the exact-match relaxation is allowed and prose is not a read", () => {
  assert.ok(EXACT_MATCH.test('const embed = process.env.NEXT_PUBLIC_EMBED === "1";'));
  assert.equal(codeOnly('// process.env.X || "default"').trim(), "");
  assert.match(codeOnly('const a = "process.env.X";'), /process\.env\.X/);
});

test("every exemption still matches a real read, so the list cannot outlive its reasons", () => {
  // An exemption that stops matching anything is an unreviewed permission sitting in the file
  // waiting for a future variable of the same name to inherit it.
  const sources = scannedSources()
    .filter((file) => relative(UI_ROOT, file) !== THREE_STATE_READER_MODULE)
    .map((file) => codeOnly(readFileSync(file, "utf8")));
  for (const name of Object.keys(TWO_STATE_READS_WITH_A_REASON)) {
    assert.ok(
      sources.some((source) => source.includes(`process.env.${name}`)),
      `${name} is exempted in TWO_STATE_READS_WITH_A_REASON but nothing under ui/ reads it. ` +
        "Delete the entry rather than leaving an unreviewed permission behind.",
    );
  }
});

test("the three-state reader resolves all three states", () => {
  assert.equal(readEnvSetting({}, "X").isUnset, true);
  assert.equal(readEnvSetting({ X: "" }, "X").isConfiguredEmpty, true);
  assert.equal(readEnvSetting({ X: "   " }, "X").isConfiguredEmpty, true);
  assert.equal(readEnvSetting({ X: " v " }, "X").value, "v");
  assert.equal(readEnvSetting({ X: "v" }, "X").hasValue, true);
});
