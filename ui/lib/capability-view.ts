import type { CapabilityManifest } from "./api";

/**
 * What the readiness panel shows, decided in one place.
 *
 * The manifest has three states and they are not interchangeable:
 *
 *   undefined  the console has not asked yet
 *   null       the console asked and could not get an answer
 *   manifest   the console asked and was told
 *
 * Only the first may render nothing. Collapsing the middle state into it is
 * how a console stops reporting a control it cannot see: the page renders, it
 * looks like a deployment with nothing to declare, and the absence of a
 * readiness panel is indistinguishable from a deployment that has no reduced
 * controls to report. That is a demonstration presenting itself as production
 * by omission, and it is the exact failure the panel exists to prevent.
 *
 * This lives in a `.ts` module rather than inside the component so the
 * decision can be executed by a test. A test that only greps the component's
 * source can watch the control be switched off and stay green.
 */
export type CapabilityView =
  | { kind: "hidden" }
  | { kind: "unavailable"; title: string; detail: string }
  | { kind: "manifest"; manifest: CapabilityManifest };

export const MANIFEST_UNAVAILABLE_TITLE = "Readiness manifest unavailable";

export const MANIFEST_UNAVAILABLE_DETAIL =
  "This console could not reach the capability manifest, so it cannot state " +
  "which controls are live. Treat nothing on this page as attested until it loads.";

export function capabilityView(
  manifest: CapabilityManifest | null | undefined,
): CapabilityView {
  if (manifest === undefined) {
    return { kind: "hidden" };
  }
  if (manifest === null) {
    return {
      kind: "unavailable",
      title: MANIFEST_UNAVAILABLE_TITLE,
      detail: MANIFEST_UNAVAILABLE_DETAIL,
    };
  }
  return { kind: "manifest", manifest };
}
