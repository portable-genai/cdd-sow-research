import assert from "node:assert/strict";
import { test } from "node:test";

import { parseUniqueJson } from "../lib/server/unique-json.ts";

test("manifest JSON parser rejects duplicate keys at every depth", () => {
  assert.throws(() => parseUniqueJson('{"schema_version":1,"schema_version":1}'), /duplicate/);
  assert.throws(() => parseUniqueJson('{"installations":{"a":{"tenant":"x","tenant":"y"}}}'), /duplicate/);
});

test("manifest JSON parser preserves valid JSON values", () => {
  const parsed = parseUniqueJson(
    '{"schema_version":1,"enabled":true,"optional":null,"items":["one",2,-3.5e2]}',
  );
  assert.deepEqual(
    { ...(parsed as Record<string, unknown>) },
    {
      schema_version: 1,
      enabled: true,
      optional: null,
      items: ["one", 2, -350],
    },
  );
});

test("manifest JSON parser rejects trailing and malformed content", () => {
  assert.throws(() => parseUniqueJson('{"a":1}{}'), /unexpected/);
  assert.throws(() => parseUniqueJson('{"a":01}'), /expected/);
  assert.throws(() => parseUniqueJson('{"a":"unterminated}'), /unterminated/);
});
