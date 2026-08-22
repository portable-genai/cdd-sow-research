import { readInstallationManifest } from "../../lib/server/installations";
import { manifestReadinessResponse } from "../../lib/server/readiness";

export const dynamic = "force-dynamic";

export async function GET() {
  return manifestReadinessResponse(readInstallationManifest);
}
