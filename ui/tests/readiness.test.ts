import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import { requireReviewedDigest } from "../lib/server/reviewed-digest.ts";
import { manifestReadinessResponse } from "../lib/server/readiness.ts";

test("readiness returns reviewed identifiers after manifest validation", async () => {
  const response = await manifestReadinessResponse(async () => ({
    digest: "a".repeat(64),
    value: {
      deployment_manifest_id: "deployment-v1",
      build_id: "build-v1",
    },
  }));

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    status: "ready",
    manifest_sha256: "a".repeat(64),
    deployment_manifest_id: "deployment-v1",
    build_id: "build-v1",
  });
});

test("readiness fails closed when the mounted manifest is missing or malformed", async () => {
  const response = await manifestReadinessResponse(async () => {
    throw new Error("invalid manifest");
  });

  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { status: "not_ready" });
});

test("readiness route delegates to the mounted-manifest reader", async () => {
  const route = await readFile(path.resolve("app/ready/route.ts"), "utf8");

  assert.match(route, /manifestReadinessResponse\(readInstallationManifest\)/);
});

test("mounted manifest bytes are bound to the reviewed digest", () => {
  requireReviewedDigest("a".repeat(64), "a".repeat(64), true, "b".repeat(64));
  assert.throws(
    () => requireReviewedDigest("a".repeat(64), undefined, true, "b".repeat(64)),
    /production requires expected manifest and settings digests/,
  );
  assert.throws(
    () => requireReviewedDigest("a".repeat(64), "0".repeat(64), true, "b".repeat(64)),
    /manifest digest does not match reviewed deployment/,
  );
});
