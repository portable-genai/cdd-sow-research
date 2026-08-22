import { notFound } from "next/navigation";

import { EmbedShell } from "../../../components/EmbedShell";
import { resolveInstallation } from "../../../lib/server/installations";

export const dynamic = "force-dynamic";

export default async function EmbedPage({
  params,
}: {
  params: Promise<{ installationId: string }>;
}) {
  const { installationId } = await params;
  const runtime = await resolveInstallation(installationId);
  if (!runtime) notFound();
  return <EmbedShell runtime={runtime} />;
}
