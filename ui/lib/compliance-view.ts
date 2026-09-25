import type { CddCase, ComplianceAnswer } from "./types";

/**
 * What the dossier's regulatory-check panel says, decided in one place.
 *
 * Three states, and a reviewer must be able to tell them apart:
 *
 *   answered         compliance-advisory answered; show the answer and its sources
 *   unavailable      the leg did not answer, and the dossier says why
 *   absent           neither field is present: a dossier exported before either existed
 *
 * The unavailable state is the laptop run's normal case when compliance-advisory
 * was never started or is down. It must read as "not checked", in plain words,
 * never as an answer and never as a blank panel. Kept out of the component so a
 * test can execute the decision rather than grep for it.
 */
export type ComplianceView =
  | { kind: "answered"; answer: ComplianceAnswer }
  | { kind: "unavailable"; message: string }
  | { kind: "absent"; message: string };

export const NOT_CONFIGURED_MESSAGE =
  "Not checked: this run was started without the compliance-advisory service, " +
  "so no regulatory check was made. Start compliance-advisory and re-run the dossier to add one.";

export const NO_ANSWER_MESSAGE =
  "Not checked: compliance-advisory was asked but did not answer (it may be down), " +
  "so this dossier carries no regulatory check. The rating above does not depend on it.";

export const ABSENT_MESSAGE =
  "Not checked: this dossier carries no regulatory compliance answer.";

export function complianceView(
  caseData: Pick<CddCase, "compliance" | "compliance_unavailable">,
): ComplianceView {
  if (caseData.compliance) {
    return { kind: "answered", answer: caseData.compliance };
  }
  const unavailable = caseData.compliance_unavailable;
  if (unavailable) {
    if (unavailable.reason === "not_configured") {
      return { kind: "unavailable", message: NOT_CONFIGURED_MESSAGE };
    }
    if (unavailable.reason === "no_answer") {
      return { kind: "unavailable", message: NO_ANSWER_MESSAGE };
    }
    // A reason this console does not know yet still says what the server said.
    return { kind: "unavailable", message: unavailable.detail || ABSENT_MESSAGE };
  }
  return { kind: "absent", message: ABSENT_MESSAGE };
}
