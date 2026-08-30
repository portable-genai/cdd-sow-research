"use client";

import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { health } from "../lib/api";

// Provenance the banner states on every page (org decision, 2026-08-30): where the
// runtime sits (GCP or this machine) and which model answers (Gemini, or the
// deterministic offline stub). Values come from /v1/healthz; nothing here guesses.
function provenance(runtime: string, model: string): string {
  const where = runtime === "gcp" ? "running on GCP" : "running locally";
  return `${where} · model ${model}`;
}

export function AppChrome({ children }: { children: ReactNode }) {
  const [region, setRegion] = useState("runtime");
  const [origin, setOrigin] = useState<string | null>(null);
  const pathname = usePathname();
  const embedded =
    pathname === "/embed" ||
    pathname.startsWith("/embed/") ||
    pathname === "/agent/embed" ||
    pathname.startsWith("/agent/embed/");
  useEffect(() => {
    let cancelled = false;
    health()
      .then((status) => {
        if (cancelled) return;
        setRegion(status.region);
        setOrigin(provenance(status.runtime, status.generator_model));
      })
      .catch(() => {
        // Startup/auth failures are rendered by AgentConsole. Keep this chrome neutral
        // instead of presenting a build-time region as deployment truth.
      });
    return () => {
      cancelled = true;
    };
  }, []);
  const banner = origin ? (
    <p className="border-b border-ink-200 bg-ink-50 px-4 py-1 text-xs text-ink-600">
      {origin}
    </p>
  ) : null;
  if (embedded)
    return (
      <>
        {banner}
        <main className="p-4">{children}</main>
      </>
    );
  return (
    <>
      {banner}
      <header className="border-b border-ink-200 bg-white">
        <div className="mx-auto max-w-4xl px-6 py-4">
          <h1 className="text-lg font-semibold text-ink-900">
            CDD + Source-of-Wealth Agent
          </h1>
          <p className="text-sm text-ink-500">
            Cited CDD dossiers · region {region} · synthetic data is fictional
          </p>
        </div>
      </header>
      <main className="mx-auto max-w-4xl px-6 py-6">{children}</main>
    </>
  );
}
