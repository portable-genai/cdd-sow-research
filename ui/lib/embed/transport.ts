export const CANONICAL_API_BASE = "/agent/api";

export type TransportSignal =
  | {
      type: "authentication";
      code: "authentication_required" | "credential_rejected";
      fallbackRequired: true;
    }
  | { type: "error"; code: string; recoverable: boolean };

export interface TransportOptions {
  installationId?: string;
  manifestDigest?: string;
  accessToken?: string;
  identityMode?: string;
  onSignal?: ((signal: TransportSignal) => void) | null;
}

export interface BlobResponse {
  blob: Blob;
  contentType: string;
  contentLength: number | null;
}

export class AuthRequiredError extends Error {
  readonly returnTo: string;
  readonly code: "authentication_required" | "credential_rejected";

  constructor(
    returnTo: string,
    code: "authentication_required" | "credential_rejected" =
      "authentication_required",
  ) {
    super("authentication required");
    this.name = "AuthRequiredError";
    this.returnTo = returnTo;
    this.code = code;
  }
}

export class AuthenticatedTransport {
  private installationId = "";
  private manifestDigest = "";
  private accessToken = "";
  private identityMode = "";
  private onSignal?: (signal: TransportSignal) => void;
  private readonly fetchImpl: typeof fetch;
  private readonly csrfTokens = new Map<string, string>();

  constructor(
    options: TransportOptions = {},
    fetchImpl: typeof fetch = fetch,
  ) {
    this.fetchImpl = fetchImpl;
    this.configure(options);
  }

  configure(options: TransportOptions): void {
    if (options.installationId !== undefined) this.installationId = options.installationId;
    if (options.manifestDigest !== undefined) this.manifestDigest = options.manifestDigest;
    if (options.accessToken !== undefined && options.accessToken !== this.accessToken) {
      this.accessToken = options.accessToken;
      this.csrfTokens.clear();
    }
    if (options.identityMode !== undefined && options.identityMode !== this.identityMode) {
      this.identityMode = options.identityMode;
      this.csrfTokens.clear();
    }
    if ("onSignal" in options) this.onSignal = options.onSignal ?? undefined;
  }

  clearAccessToken(): void {
    this.accessToken = "";
    this.csrfTokens.clear();
  }

  private headers(body: BodyInit | null | undefined, supplied?: HeadersInit): Headers {
    const headers = new Headers(supplied);
    if (body != null && !(body instanceof FormData) && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    if (this.installationId) headers.set("X-CDD-Installation-ID", this.installationId);
    if (this.manifestDigest) {
      headers.set("X-CDD-Manifest-SHA256", this.manifestDigest);
    }
    if (this.accessToken) headers.set("Authorization", `Bearer ${this.accessToken}`);
    return headers;
  }

  private async csrfToken(method: string, path: string): Promise<string> {
    const key = `${method} ${path}`;
    const existing = this.csrfTokens.get(key);
    if (existing) return existing;
    const query = new URLSearchParams({ method, path });
    const response = await this.fetchImpl.call(
      globalThis,
      `${CANONICAL_API_BASE}/auth/csrf?${query.toString()}`,
      {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: this.headers(null),
      },
    );
    if (response.status === 401) {
      this.authenticationFailure(response.status);
    }
    if (!response.ok) throw new Error(await responseError(response, "/auth/csrf"));
    const cacheControl = response.headers.get("cache-control") ?? "";
    if (!cacheControl.includes("private") || !cacheControl.includes("no-store")) {
      throw new Error("CSRF bootstrap response is not private and no-store");
    }
    const body = (await response.json()) as { csrf_token?: unknown };
    if (
      typeof body.csrf_token !== "string" ||
      body.csrf_token.length < 32 ||
      body.csrf_token.length > 2_048
    ) {
      throw new Error("CSRF bootstrap response is invalid");
    }
    this.csrfTokens.set(key, body.csrf_token);
    return body.csrf_token;
  }

  private authenticationFailure(status: number): never {
    const code =
      status === 401
        ? ("authentication_required" as const)
        : ("credential_rejected" as const);
    this.clearAccessToken();
    this.onSignal?.({ type: "authentication", code, fallbackRequired: true });
    const returnTo =
      typeof window === "undefined"
        ? "/agent/"
        : `${window.location.pathname}${window.location.search}`;
    throw new AuthRequiredError(returnTo, code);
  }

  private async response(
    path: string,
    init: RequestInit = {},
    retryCsrf = true,
  ): Promise<Response> {
    if (!path.startsWith("/")) throw new Error("API path must start with '/'");
    const method = (init.method ?? "GET").toUpperCase();
    const csrfProtected =
      this.identityMode === "oidc-session" &&
      ["POST", "PUT", "PATCH", "DELETE"].includes(method);
    const headers = this.headers(init.body, init.headers);
    if (csrfProtected) {
      headers.set("X-CSRF-Token", await this.csrfToken(method, path));
    }
    const response = await this.fetchImpl.call(
      globalThis,
      `${CANONICAL_API_BASE}${path}`,
      {
        ...init,
        credentials: "same-origin",
        cache: "no-store",
        headers,
      },
    );
    if (
      response.status === 403 &&
      csrfProtected &&
      retryCsrf &&
      (await isRejectedCsrfToken(response))
    ) {
      this.csrfTokens.delete(`${method} ${path}`);
      return this.response(path, init, false);
    }
    if (response.status === 401) {
      this.authenticationFailure(response.status);
    }
    if (response.status === 403) {
      this.onSignal?.({
        type: "error",
        code: "authorization_denied",
        recoverable: false,
      });
    }
    if (!response.ok) throw new Error(await responseError(response, path));
    return response;
  }

  async json<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await this.response(path, init);
    return response.json() as Promise<T>;
  }

  async multipart<T>(path: string, body: FormData, init: RequestInit = {}): Promise<T> {
    const response = await this.response(path, { ...init, body });
    return response.json() as Promise<T>;
  }

  async blob(path: string, init: RequestInit = {}): Promise<BlobResponse> {
    const response = await this.response(path, init);
    const rawLength = response.headers.get("content-length");
    return {
      blob: await response.blob(),
      contentType: response.headers.get("content-type")?.split(";", 1)[0]?.trim() ?? "",
      contentLength: rawLength != null && /^\d+$/.test(rawLength) ? Number(rawLength) : null,
    };
  }
}

async function isRejectedCsrfToken(response: Response): Promise<boolean> {
  try {
    const body = (await response.clone().json()) as { detail?: unknown };
    return body.detail === "CSRF token is invalid" || body.detail === "CSRF token is required";
  } catch {
    return false;
  }
}

async function responseError(response: Response, path: string): Promise<string> {
  const body = await response.text();
  try {
    const parsed = JSON.parse(body) as { detail?: unknown };
    if (typeof parsed.detail === "string") return parsed.detail;
  } catch {
    // Non-JSON upstream failures fall through to the bounded status message.
  }
  return `${path} failed: ${response.status} ${body.slice(0, 1_024)}`;
}
