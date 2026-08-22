// Coloured risk-band badge and severity pill for the dossier.

import type { RiskBand, Severity } from "../lib/types";

const BAND_STYLE: Record<RiskBand, string> = {
  low: "bg-emerald-50 text-emerald-700 border-emerald-300",
  medium: "bg-amber-50 text-amber-700 border-amber-300",
  high: "bg-orange-50 text-orange-700 border-orange-300",
  prohibited: "bg-red-50 text-red-700 border-red-300",
};

export function RiskBadge({ band }: { band: RiskBand }) {
  return (
    <span
      className={`inline-block rounded border px-2 py-0.5 text-xs font-semibold uppercase ${BAND_STYLE[band]}`}
    >
      {band}
    </span>
  );
}

const SEVERITY_STYLE: Record<Severity, string> = {
  low: "bg-ink-100 text-ink-600",
  medium: "bg-amber-100 text-amber-700",
  high: "bg-orange-100 text-orange-700",
  critical: "bg-red-100 text-red-700",
};

export function SeverityPill({ severity }: { severity: Severity }) {
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${SEVERITY_STYLE[severity]}`}>
      {severity}
    </span>
  );
}
