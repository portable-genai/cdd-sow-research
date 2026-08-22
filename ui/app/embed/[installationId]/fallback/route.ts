import { NextResponse } from "next/server";

import { resolveInstallation } from "../../../../lib/server/installations";

export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ installationId: string }> },
) {
  const { installationId } = await params;
  const runtime = await resolveInstallation(installationId);
  if (!runtime) {
    return NextResponse.json(
      { detail: "unknown installation" },
      { status: 404, headers: { "Cache-Control": "no-store" } },
    );
  }
  return NextResponse.redirect(runtime.fallbackUrl, {
    status: 303,
    headers: { "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" },
  });
}
