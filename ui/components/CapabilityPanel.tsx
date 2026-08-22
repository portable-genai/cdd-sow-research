"use client";

import type { CapabilityManifest } from "../lib/api";
import { Panel } from "./ui";

function label(name: string): string {
  return name
    .split("-")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function CapabilityPanel({
  manifest,
}: {
  manifest: CapabilityManifest;
}) {
  return (
    <Panel title="Deployment capabilities">
      <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
        <span
          className={`rounded-full px-2.5 py-1 font-medium ${
            manifest.demo_only
              ? "bg-amber-100 text-amber-800"
              : "bg-emerald-100 text-emerald-800"
          }`}
        >
          {manifest.demo_only ? "Functional laptop demo" : "Managed deployment"}
        </span>
        <span className="text-ink-500">
          {manifest.profile} · {manifest.region} ·{" "}
          {manifest.production_ready ? "production ready" : "not production attested"}
        </span>
      </div>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {manifest.capabilities.map((capability) => (
          <div
            key={capability.name}
            className="rounded border border-ink-200 bg-white px-3 py-2"
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-sm font-medium text-ink-900">
                {label(capability.name)}
              </span>
              <span
                className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                  capability.available
                    ? "bg-emerald-100 text-emerald-800"
                    : "bg-ink-100 text-ink-600"
                }`}
              >
                {capability.available
                  ? capability.assurance === "demo-only"
                    ? "Works locally"
                    : capability.assurance
                  : "Unavailable here"}
              </span>
            </div>
            <p className="mt-1 text-xs text-ink-500">
              {capability.provider}
              {capability.reason ? ` · ${capability.reason}` : ""}
            </p>
          </div>
        ))}
      </div>
    </Panel>
  );
}
