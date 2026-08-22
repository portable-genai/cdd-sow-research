import type { Metadata } from "next";
import type { ReactNode } from "react";
import { AppChrome } from "../components/AppChrome";
import "./globals.css";

// Required by the nonce CSP, not a performance preference. `proxy.ts` mints a per-request script
// nonce and Next can only stamp it onto the script tags of a DYNAMICALLY rendered route. A
// statically prerendered page was built before the nonce existed, so every script tag would go
// out bare while the header advertised a nonce, and `'strict-dynamic'` disables the `'self'`
// fallback that would otherwise load them. `next.config.mjs` refuses to build without this.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "B1 CDD + Source-of-Wealth Agent",
  description:
    "Demo console for the B1 CDD + Source-of-Wealth Agent: cited CDD dossiers from a KYC pack, corporate registries and adverse media.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body><AppChrome>{children}</AppChrome></body>
    </html>
  );
}
