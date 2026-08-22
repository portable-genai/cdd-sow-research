import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  configureAgentNavigation,
  emitCitationNavigation,
  hasAgentNavigationChannel,
} from "../lib/embed/navigation.ts";

afterEach(() => configureAgentNavigation(null, null));

test("citation continuation emits only the typed server-returned URL", () => {
  const messages: unknown[] = [];
  const continuation =
    "https://standalone.example/agent/auth/citation#opaque-fragment-ticket";

  configureAgentNavigation((message) => messages.push(message), "1");
  assert.equal(hasAgentNavigationChannel(), true);
  emitCitationNavigation(continuation);

  assert.deepEqual(messages, [
    {
      type: "agent:navigation",
      protocol_version: "1",
      href: continuation,
      reason: "citation-continuation",
    },
  ]);
  const serialized = JSON.stringify(messages);
  assert.equal(serialized.includes("/api/v1/cases/case-secret/documents/source-secret"), false);
  assert.equal(serialized.includes("bearer-secret"), false);
});

test("citation continuation navigation allows only HTTPS or loopback development URLs", () => {
  assert.equal(hasAgentNavigationChannel(), false);
  assert.throws(
    () => emitCitationNavigation("https://standalone.example/agent/"),
    /not negotiated/,
  );

  const messages: unknown[] = [];
  configureAgentNavigation((message) => messages.push(message), "1");
  emitCitationNavigation("http://127.0.0.1:3300/agent/auth/citation#ticket");
  assert.equal(messages.length, 1);
  assert.throws(
    () => emitCitationNavigation("http://standalone.example/agent/"),
    /secure-origin policy/,
  );
  assert.throws(
    () => emitCitationNavigation("https://user:password@standalone.example/agent/"),
    /secure-origin policy/,
  );
});
