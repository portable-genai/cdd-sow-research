import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { test } from "node:test";

// Everything here reads only files inside ui/, so it also runs during the ui/Dockerfile
// image build, whose context is ui/. The companion checks that the checked-in embed hosts
// carry this same SRI live in tests/repo/embed-host-sri.test.ts, because those fixtures sit
// outside that context.
const uiDir = resolve(import.meta.dirname, "..");

test("generated loader is immutable-version ready and its published SRI matches", async () => {
  const loader = await readFile(resolve(uiDir, "public/embed/v1/cdd-agent.js"), "utf8");
  const published = (
    await readFile(resolve(uiDir, "public/embed/v1/cdd-agent.js.sri"), "utf8")
  ).trim();
  const actual = `sha384-${createHash("sha384").update(loader).digest("base64")}`;
  assert.equal(published, actual);
  assert.match(loader, /allow-scripts allow-same-origin/);
  assert.doesNotMatch(loader, /allow-popups|allow-downloads|allow-top-navigation/);
  assert.match(loader, /new MessageChannel/);
  assert.match(loader, /setInterval/);
  assert.match(loader, /agent:authentication/);
  assert.match(loader, /__cddHostBoundaryProbe/);
  assert.equal(loader.includes("const idPattern = /^[A-Za-z0-9_-]{1,128}$/;"), true);
  assert.match(loader, /observeHostBoundary\("window-message", "host-to-agent", init\)/);
});
