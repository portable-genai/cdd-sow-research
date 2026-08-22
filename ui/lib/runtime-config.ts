export interface EmbedRuntimeConfig {
  installationId: string;
  parentOrigins: string[];
  identityMode: string;
  protocolVersions: string[];
  publicOrigin: string;
  publicMountPath: "/agent";
  fallbackUrl: string;
  manifestDigest: string;
  buildId: string;
}
