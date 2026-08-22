"use client";

import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { health } from "../lib/api";

export function AppChrome({ children }: { children: ReactNode }) {
  const [region, setRegion] = useState("runtime");
  const pathname = usePathname();
  const embedded =
    pathname === "/embed" ||
    pathname.startsWith("/embed/") ||
    pathname === "/agent/embed" ||
    pathname.startsWith("/agent/embed/");
  useEffect(() => {
    if (embedded) return;
    let cancelled = false;
    health()
      .then((status) => {
        if (!cancelled) setRegion(status.region);
      })
      .catch(() => {
        // Startup/auth failures are rendered by AgentConsole. Keep this chrome neutral
        // instead of presenting a build-time region as deployment truth.
      });
    return () => {
      cancelled = true;
    };
  }, [embedded]);
  if (embedded) return <main className="p-4">{children}</main>;
  return (
    <>
      <header className="border-b border-ink-200 bg-white">
        <div className="mx-auto max-w-4xl px-6 py-4">
          <h1 className="text-lg font-semibold text-ink-900">
            B1 · CDD + Source-of-Wealth Agent
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
