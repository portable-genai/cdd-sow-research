import type {
  CddCase,
  CddRequest,
  PerpetualKycAssessment,
  PerpetualKycRequest,
  StoredDocument,
  UboGraphRequest,
  UboResolution,
} from "./types";
import {
  AuthenticatedTransport,
  AuthRequiredError,
  CANONICAL_API_BASE,
  type BlobResponse,
  type TransportSignal,
} from "./embed/transport";

// Dev-only identity selection. In LOCAL mode the backend resolves identity from
// the X-Dev-Persona header; in secure profiles this is ignored (identity comes
// from an IAP assertion injected by the platform, or a Mode 6 session cookie the
// browser attaches automatically).
let devPersona = "";
const transport = new AuthenticatedTransport();

export function configureApiTransport(options: {
  installationId?: string;
  manifestDigest?: string;
  accessToken?: string;
  identityMode?: string;
  onSignal?: ((signal: TransportSignal) => void) | null;
}): void {
  transport.configure(options);
}

export function setAccessToken(accessToken: string): void {
  transport.configure({ accessToken });
}

export function setDevPersona(id: string): void {
  devPersona = id;
}

export function getDevPersona(): string {
  return devPersona;
}

function requestHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  if (devPersona) headers["X-Dev-Persona"] = devPersona;
  return headers;
}

export interface BlockedResponse {
  blocked: true;
  requires_human_review: true;
  detail: string;
  reason: string;
}

function isBlocked(body: unknown): body is BlockedResponse {
  return typeof body === "object" && body !== null && (body as { blocked?: boolean }).blocked === true;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  return transport.json<T>(path, {
    ...init,
    headers: { ...requestHeaders(), ...(init?.headers as Record<string, string> | undefined) },
  });
}

export async function assessCdd(request_: CddRequest): Promise<CddCase | BlockedResponse> {
  return request<CddCase | BlockedResponse>("/v1/cdd", {
    method: "POST",
    body: JSON.stringify(request_),
  });
}

export async function runPerpetualKyc(
  request_: PerpetualKycRequest,
): Promise<PerpetualKycAssessment> {
  return request<PerpetualKycAssessment>("/v1/perpetual-kyc", {
    method: "POST",
    body: JSON.stringify(request_),
  });
}

export async function perpetualKycQueue(): Promise<PerpetualKycAssessment[]> {
  const body = await request<{ items: PerpetualKycAssessment[] }>("/v1/perpetual-kyc/queue");
  return body.items ?? [];
}

// Resolving is the consequential verb: it always requires human review and is routed to
// Hrz7 server-side. There is a companion GET /v1/ubo-graph/{id} that returns the walked
// structure alone (no verdict), which the console does not need because the resolution
// already carries the graph.
export async function resolveUboGraph(request_: UboGraphRequest): Promise<UboResolution> {
  return request<UboResolution>("/v1/ubo-graph", {
    method: "POST",
    body: JSON.stringify(request_),
  });
}

export async function health(): Promise<{
  status: string;
  profile: string;
  runtime: string;
  generator_model: string;
  region: string;
  identity_mode: string;
  channel_mode: string;
}> {
  // `/v1/healthz`, not `/healthz`: the platform reserves the latter, so a request to it through
  // the embedding host's reverse proxy is answered by the frontend and never reaches the API.
  // The console then waits forever on "Connecting to Doc1..." against a healthy service.
  return request("/v1/healthz");
}

export interface CapabilityManifest {
  service: string;
  profile: string;
  region: string;
  schema_version: string;
  portable_core: boolean;
  demo_only: boolean;
  production_ready: boolean;
  capabilities: {
    name: string;
    available: boolean;
    mode: string;
    assurance: string;
    provider: string;
    reason: string;
    required_for_production: boolean;
  }[];
}

export async function capabilities(): Promise<CapabilityManifest> {
  return request("/v1/capabilities");
}

export interface PortableDossierArtifact {
  schema_version: "cdd-dossier/v1";
  sha256: string;
  exported_at: string;
  dossier: CddCase;
}

export async function exportPortableDossier(
  dossier: CddCase,
): Promise<PortableDossierArtifact> {
  return request("/v1/portable/dossiers/export", {
    method: "POST",
    body: JSON.stringify(dossier),
  });
}

export async function importPortableDossier(
  artifact: PortableDossierArtifact,
): Promise<CddCase> {
  return request("/v1/portable/dossiers/import", {
    method: "POST",
    body: JSON.stringify(artifact),
  });
}

export async function createCitationContinuation(
  continuationId: string,
): Promise<{ continuation_url: string }> {
  if (!continuationId || continuationId.length > 1_024) {
    throw new Error("citation continuation identifier is invalid");
  }
  const value = await request<{ continuation_url?: unknown }>(
    `/v1/embed/citations/${encodeURIComponent(continuationId)}/continuations`,
    { method: "POST" },
  );
  if (typeof value.continuation_url !== "string") {
    throw new Error("citation continuation response is invalid");
  }
  return { continuation_url: value.continuation_url };
}

export async function listPersonas(): Promise<
  { id: string; subject: string; tenant: string; principals: string }[]
> {
  return request("/v1/personas");
}

// --------------------------------------------------------------------------- //
// Case documents
// --------------------------------------------------------------------------- //

/** Upload one document into a case's custody. Multipart, so no Content-Type header:
 *  the browser must set it itself to include the multipart boundary. */
export async function uploadDocument(
  caseId: string,
  file: File,
  docType: string,
): Promise<StoredDocument> {
  const body = new FormData();
  body.append("file", file);
  body.append("doc_type", docType);
  return transport.multipart<StoredDocument>(
    `/v1/cases/${encodeURIComponent(caseId)}/documents`,
    body,
    {
      method: "POST",
      headers: requestHeaders(),
    },
  );
}

export async function listDocuments(caseId: string): Promise<StoredDocument[]> {
  const body = await request<{ documents: StoredDocument[] }>(
    `/v1/cases/${encodeURIComponent(caseId)}/documents`,
  );
  return body.documents;
}

export async function deleteDocument(caseId: string, documentId: string): Promise<void> {
  await request(
    `/v1/cases/${encodeURIComponent(caseId)}/documents/${encodeURIComponent(documentId)}`,
    { method: "DELETE" },
  );
}

/** Accept only an API-relative document path for the authenticated in-frame viewer. */
export function documentPath(url: string): string {
  if (!url || /^https?:\/\//i.test(url)) return "";
  return url.startsWith("/") ? url : `/${url}`;
}

export async function readDocument(path: string): Promise<BlobResponse> {
  return transport.blob(documentPath(path), { headers: requestHeaders() });
}

export { AuthRequiredError, CANONICAL_API_BASE as API_BASE, isBlocked };
