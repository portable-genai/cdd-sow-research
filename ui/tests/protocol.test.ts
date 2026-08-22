import assert from "node:assert/strict";
import { test } from "node:test";

import {
  negotiateProtocol,
  validateAgentPortMessage,
  validateHostInit,
  validateHostPortMessage,
} from "../lib/embed/protocol.ts";

const init = {
  type: "host:init",
  installation_id: "inst_host_a",
  channel_id: "a".repeat(32),
  loader_instance_id: "b".repeat(32),
  nonce: "c".repeat(32),
  protocol_versions: ["1"],
};

test("host bootstrap accepts only the expected closed installation message", () => {
  assert.deepEqual(validateHostInit(init, "inst_host_a"), init);
  assert.equal(validateHostInit({ ...init, installation_id: "inst_host_b" }, "inst_host_a"), null);
  assert.equal(validateHostInit({ ...init, token: "must-not-cross-global-message" }, "inst_host_a"), null);
  assert.equal(validateHostInit({ ...init, nonce: "short" }, "inst_host_a"), null);
});

test("protocol negotiation is explicit and fail closed", () => {
  assert.equal(negotiateProtocol(["2", "1"]), "1");
  assert.equal(negotiateProtocol(["2"]), null);
});

test("port validators reject unknown fields, versions, and oversized credentials", () => {
  assert.deepEqual(
    validateHostPortMessage({
      type: "host:credential",
      protocol_version: "1",
      access_token: "short-lived-token",
    }),
    {
      type: "host:credential",
      protocol_version: "1",
      access_token: "short-lived-token",
    },
  );
  assert.equal(
    validateHostPortMessage({
      type: "host:credential",
      protocol_version: "1",
      access_token: "token",
      actor: "browser-asserted",
    }),
    null,
  );
  assert.equal(
    validateHostPortMessage({
      type: "host:credential",
      protocol_version: "9",
      access_token: "token",
    }),
    null,
  );
  assert.equal(
    validateHostPortMessage({
      type: "host:credential",
      protocol_version: "1",
      access_token: "x".repeat(33 * 1024),
    }),
    null,
  );
  assert.deepEqual(
    validateHostPortMessage({
      type: "host:launch-code",
      protocol_version: "1",
      instance_id: "instance_binding_0123456789",
      launch_code: "launch_binding_01234567890",
    }),
    {
      type: "host:launch-code",
      protocol_version: "1",
      instance_id: "instance_binding_0123456789",
      launch_code: "launch_binding_01234567890",
    },
  );
  assert.equal(
    validateHostPortMessage({
      type: "host:launch-code",
      protocol_version: "1",
      instance_id: "instance_binding_0123456789",
      launch_code: "short",
    }),
    null,
  );
});

test("agent events also use a closed, bounded schema", () => {
  assert.deepEqual(
    validateAgentPortMessage({
      type: "agent:authentication",
      protocol_version: "1",
      code: "authentication_required",
      fallback_required: true,
    }),
    {
      type: "agent:authentication",
      protocol_version: "1",
      code: "authentication_required",
      fallback_required: true,
    },
  );
  assert.equal(
    validateAgentPortMessage({
      type: "agent:ready",
      installation_id: "inst.with.dot",
      channel_id: "a".repeat(32),
      loader_instance_id: "b".repeat(32),
      nonce: "c".repeat(32),
      protocol_version: "1",
    }),
    null,
  );
  assert.equal(
    validateAgentPortMessage({
      type: "agent:navigation",
      protocol_version: "1",
      href: "https://standalone.example/agent/",
      reason: "host-supplied-target",
    }),
    null,
  );
  assert.deepEqual(
    validateAgentPortMessage({
      type: "agent:grant-registration",
      protocol_version: "1",
      instance_id: "instance_binding_0123456789",
      expires_at: 1_800_000_000,
    }),
    {
      type: "agent:grant-registration",
      protocol_version: "1",
      instance_id: "instance_binding_0123456789",
      expires_at: 1_800_000_000,
    },
  );
  assert.deepEqual(
    validateAgentPortMessage({
      type: "agent:identity-ready",
      protocol_version: "1",
      assurance: "embedded-grant",
    }),
    {
      type: "agent:identity-ready",
      protocol_version: "1",
      assurance: "embedded-grant",
    },
  );
});
