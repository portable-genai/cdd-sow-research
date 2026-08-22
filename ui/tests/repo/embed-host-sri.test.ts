import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { test } from "node:test";

// Repo-scope checks: the checked-in embed host fixtures must pin the published loader SRI.
// They read tests/embed_hosts/** and tests/browser/**, which live OUTSIDE the ui/ directory
// and therefore outside the ui/Dockerfile build context, so `npm run test:unit` (what the
// image build runs) does not include them and `npm test` does. Resolve every path from this
// file so the suite is independent of the working directory.
const repoRoot = resolve(import.meta.dirname, "..", "..", "..");

test("every checked-in embed host pins the published loader integrity", async () => {
  const published = (
    await readFile(resolve(repoRoot, "ui/public/embed/v1/cdd-agent.js.sri"), "utf8")
  ).trim();
  const hostPages = [
    "tests/embed_hosts/host-a/index.html",
    "tests/embed_hosts/host-b/index.html",
    "tests/embed_hosts/host-a/mode5.html",
    "tests/browser/unregistered-host/index.html",
  ];
  for (const page of hostPages) {
    const html = await readFile(resolve(repoRoot, page), "utf8");
    assert.equal(html.includes(`integrity="${published}"`), true, `${page} pins a stale SRI`);
  }
});
