import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import {
  MANIFEST_UNAVAILABLE_TITLE,
  capabilityView,
} from "../lib/capability-view.ts";

// The design rule this console implements: "A console that cannot fetch it says
// so. A readiness panel that disappears on error is a demonstration presenting
// itself as production by omission."
//
// The console did exactly that. It caught the failed fetch, stored null, and
// rendered `{capabilityManifest ? <CapabilityPanel/> : null}` -- so the one
// state the panel exists to report was the one state that rendered nothing, and
// no test looked. These assertions EXECUTE the decision rather than reading the
// component for a shape, because a source-shape test watches a control be
// switched off and stays green.

test("a manifest that could not be fetched renders, and says why", () => {
  const view = capabilityView(null);

  assert.equal(view.kind, "unavailable");
  assert.equal(view.kind === "unavailable" && view.title, MANIFEST_UNAVAILABLE_TITLE);
  assert.match(
    view.kind === "unavailable" ? view.detail : "",
    /cannot state\s+which controls are live/,
  );
});

test("only the not-yet-asked state renders nothing", () => {
  assert.equal(capabilityView(undefined).kind, "hidden");
});

test("a fetched manifest is reported as itself", () => {
  const manifest = {
    service: "cdd",
    profile: "gcp",
    region: "reviewed-region",
    schema_version: "1",
    portable_core: true,
    demo_only: false,
    production_ready: true,
    capabilities: [],
  };
  const view = capabilityView(manifest as never);

  assert.equal(view.kind, "manifest");
  assert.equal(view.kind === "manifest" && view.manifest, manifest);
});

test("null and undefined never resolve to the same view", () => {
  // The defect in one line: these two were the same `null` branch, so "could
  // not answer" was served as "not asked yet" and disappeared.
  assert.notEqual(capabilityView(null).kind, capabilityView(undefined).kind);
});

test("the console delegates the three-state decision to the panel", async () => {
  const source = await readFile(
    path.resolve("components/AgentConsole.tsx"),
    "utf8",
  );

  // Belt to the executed assertions above: the console must hand the panel the
  // value it has, not decide for itself whether the panel appears.
  assert.doesNotMatch(
    source,
    /\{\s*capabilityManifest\s*\?[\s\S]{0,160}:\s*null\s*\}/,
    "capabilityManifest must not sit behind a truthiness check: null is a state that has to render",
  );
  assert.match(source, /<CapabilityPanel manifest=\{capabilityManifest\} \/>/);
});
