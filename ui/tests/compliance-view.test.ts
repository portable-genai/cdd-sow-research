import assert from "node:assert/strict";
import test from "node:test";

import {
  ABSENT_MESSAGE,
  NO_ANSWER_MESSAGE,
  NOT_CONFIGURED_MESSAGE,
  complianceView,
} from "../lib/compliance-view.ts";

// A laptop run may start without compliance-advisory, or find it down. The dossier then
// carries a typed reason instead of an answer, and the panel must say "not checked" in plain
// words: never a blank panel and never something that reads as an answer. These assertions
// execute the decision rather than reading the component for a shape.

const answer = {
  question: "What CDD expectations apply?",
  answer: "Enhanced due diligence applies.",
  citations: [],
  requires_human_review: true,
  confidence: 0.7,
};

test("an answered check shows the answer", () => {
  const view = complianceView({ compliance: answer, compliance_unavailable: null });
  assert.equal(view.kind, "answered");
});

test("a run started without compliance-advisory says so in plain words", () => {
  const view = complianceView({
    compliance: null,
    compliance_unavailable: { reason: "not_configured", detail: "server words" },
  });
  assert.deepEqual(view, { kind: "unavailable", message: NOT_CONFIGURED_MESSAGE });
  assert.match(NOT_CONFIGURED_MESSAGE, /^Not checked: .*without the compliance-advisory service/);
});

test("a compliance-advisory that did not answer is not an answer", () => {
  const view = complianceView({
    compliance: null,
    compliance_unavailable: { reason: "no_answer", detail: "server words" },
  });
  assert.deepEqual(view, { kind: "unavailable", message: NO_ANSWER_MESSAGE });
  assert.match(NO_ANSWER_MESSAGE, /^Not checked: .*did not answer/);
});

test("a dossier with neither field still renders a statement", () => {
  assert.deepEqual(complianceView({}), { kind: "absent", message: ABSENT_MESSAGE });
});
