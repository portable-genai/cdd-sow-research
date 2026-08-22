export interface ReadinessManifest {
  digest: string;
  value: {
    deployment_manifest_id: string;
    build_id: string;
  };
}

export async function manifestReadinessResponse(
  readManifest: () => Promise<ReadinessManifest>,
): Promise<Response> {
  try {
    const manifest = await readManifest();
    return Response.json(
      {
        status: "ready",
        manifest_sha256: manifest.digest,
        deployment_manifest_id: manifest.value.deployment_manifest_id,
        build_id: manifest.value.build_id,
      },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch {
    return Response.json(
      { status: "not_ready" },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
}
