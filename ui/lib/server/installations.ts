import "server-only";

import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import type { EmbedRuntimeConfig } from "../runtime-config";
import { requireReviewedDigest } from "./reviewed-digest";
import { parseUniqueJson } from "./unique-json";
import { readEnvSetting } from "../env-setting.mjs";

interface InstallationRecord {
  tenant: string;
  parent_origins: string[];
  resource_audience: string;
  scopes: string[];
  identity_mode: string;
  issuer_policy_id: string;
  allowed_clients: string[];
  protocol_versions: string[];
  public_origin: string;
  public_mount_path: "/agent";
  loader_version: "v1";
  fallback_url: string;
  presentation_defaults?: {
    theme?: "light" | "dark" | "system";
    density?: "compact" | "comfortable";
  };
}

interface InstallationManifest {
  schema_version: 1;
  deployment_manifest_id: string;
  build_id: string;
  installations: Record<string, InstallationRecord>;
}

const ROOT_KEYS = [
  "schema_version",
  "deployment_manifest_id",
  "build_id",
  "installations",
] as const;
const INSTALLATION_KEYS = [
  "tenant",
  "parent_origins",
  "resource_audience",
  "scopes",
  "identity_mode",
  "issuer_policy_id",
  "allowed_clients",
  "protocol_versions",
  "public_origin",
  "public_mount_path",
  "loader_version",
  "fallback_url",
] as const;
const OPTIONAL_INSTALLATION_KEYS = ["presentation_defaults"] as const;
const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const INSTALLATION_ID = /^[A-Za-z0-9_-]{1,128}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExpectedKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
  optional: readonly string[] = [],
): boolean {
  const actual = Object.keys(value).sort();
  const allowed = new Set([...expected, ...optional]);
  return expected.every((key) => key in value) && actual.every((key) => allowed.has(key));
}

function isLoopback(hostname: string): boolean {
  const host = hostname.replace(/^\[|\]$/g, "").toLowerCase();
  return (
    host === "localhost" ||
    host === "::1" ||
    /^127(?:\.\d{1,3}){3}$/.test(host)
  );
}

function isCanonicalOrigin(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {
    const url = new URL(value);
    return (
      (url.protocol === "https:" || (url.protocol === "http:" && isLoopback(url.hostname))) &&
      url.origin === value &&
      url.username === "" &&
      url.password === "" &&
      url.pathname === "/"
    );
  } catch {
    return false;
  }
}

function isFallbackUrl(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {
    const url = new URL(value);
    return (
      (url.protocol === "https:" || (url.protocol === "http:" && isLoopback(url.hostname))) &&
      url.username === "" &&
      url.password === "" &&
      url.pathname.startsWith("/agent/") &&
      !url.pathname.includes("\\") &&
      url.search === "" &&
      url.hash === ""
    );
  } catch {
    return false;
  }
}

function stringList(value: unknown, pattern?: RegExp): value is string[] {
  return (
    Array.isArray(value) &&
    value.length > 0 &&
    value.length <= 64 &&
    value.every((entry) => typeof entry === "string" && (!pattern || pattern.test(entry)))
  );
}

function parseManifest(value: unknown): InstallationManifest {
  if (!isRecord(value) || !hasExpectedKeys(value, ROOT_KEYS)) {
    throw new Error("installation manifest has unknown or missing root fields");
  }
  if (
    value.schema_version !== 1 ||
    typeof value.deployment_manifest_id !== "string" ||
    !ID.test(value.deployment_manifest_id) ||
    typeof value.build_id !== "string" ||
    !ID.test(value.build_id) ||
    !isRecord(value.installations) ||
    Object.keys(value.installations).length === 0
  ) {
    throw new Error("installation manifest root is invalid");
  }
  const tenants = new Set<string>();
  const identityModes = new Set<string>();
  const publicOrigins = new Set<string>();
  for (const [installationId, raw] of Object.entries(value.installations)) {
    if (
      !INSTALLATION_ID.test(installationId) ||
      !isRecord(raw) ||
      !hasExpectedKeys(raw, INSTALLATION_KEYS, OPTIONAL_INSTALLATION_KEYS)
    ) {
      throw new Error(`installation ${installationId} has an invalid closed schema`);
    }
    if (
      typeof raw.tenant !== "string" ||
      !ID.test(raw.tenant) ||
      !stringList(raw.parent_origins) ||
      !raw.parent_origins.every(isCanonicalOrigin) ||
      new Set(raw.parent_origins).size !== raw.parent_origins.length ||
      typeof raw.resource_audience !== "string" ||
      raw.resource_audience.length === 0 ||
      raw.resource_audience.includes("*") ||
      !stringList(raw.scopes, /^[A-Za-z0-9:._-]{1,128}$/) ||
      new Set(raw.scopes).size !== raw.scopes.length ||
      typeof raw.identity_mode !== "string" ||
      !["oauth-access-token", "embedded-grant"].includes(raw.identity_mode) ||
      typeof raw.issuer_policy_id !== "string" ||
      !ID.test(raw.issuer_policy_id) ||
      !stringList(raw.allowed_clients, ID) ||
      new Set(raw.allowed_clients).size !== raw.allowed_clients.length ||
      !stringList(raw.protocol_versions, /^[1-9][0-9]{0,5}$/) ||
      new Set(raw.protocol_versions).size !== raw.protocol_versions.length ||
      typeof raw.public_origin !== "string" ||
      !isCanonicalOrigin(raw.public_origin) ||
      raw.parent_origins.includes(raw.public_origin) ||
      raw.public_mount_path !== "/agent" ||
      raw.loader_version !== "v1" ||
      !isFallbackUrl(raw.fallback_url) ||
      !validPresentation(raw.presentation_defaults)
    ) {
      throw new Error(`installation ${installationId} is invalid`);
    }
    tenants.add(raw.tenant);
    identityModes.add(raw.identity_mode);
    publicOrigins.add(raw.public_origin);
  }
  if (tenants.size !== 1 || identityModes.size !== 1 || publicOrigins.size !== 1) {
    throw new Error("installation manifest must contain one tenant, identity mode, and public origin");
  }
  return value as unknown as InstallationManifest;
}

function validPresentation(value: unknown): boolean {
  if (value === undefined) return true;
  if (!isRecord(value) || !hasExpectedKeys(value, [], ["theme", "density"])) return false;
  return (
    (value.theme === undefined || ["light", "dark", "system"].includes(String(value.theme))) &&
    (value.density === undefined ||
      ["compact", "comfortable"].includes(String(value.density)))
  );
}

export interface LoadedInstallationManifest {
  exactBytes: Buffer;
  digest: string;
  value: InstallationManifest;
}

export async function readInstallationManifest(): Promise<LoadedInstallationManifest> {
  // Three states, stated rather than implied. `!configured` already refused both the unset and
  // the emptied case, which is correct, but it said so by truthiness: a reader could not tell
  // whether the refusal was deliberate or a default nobody had thought about, and the next
  // variable added beside it would have inherited the ambiguity rather than the rule.
  const configuredSetting = readEnvSetting(process.env, "CDD_INSTALLATION_MANIFEST");
  if (!configuredSetting.hasValue) {
    throw new Error(
      configuredSetting.isConfiguredEmpty
        ? "CDD_INSTALLATION_MANIFEST is set to an empty value, which names no manifest"
        : "CDD_INSTALLATION_MANIFEST is required",
    );
  }
  const configured = configuredSetting.value;
  const repositoryRoot = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    "..",
    "..",
    "..",
  );
  const manifestPath = path.isAbsolute(configured)
    ? configured
    : path.resolve(repositoryRoot, configured);
  const exactBytes = await readFile(manifestPath);
  if (exactBytes.byteLength === 0 || exactBytes.byteLength > 1024 * 1024) {
    throw new Error("installation manifest size is invalid");
  }
  let parsed: unknown;
  try {
    parsed = parseUniqueJson(exactBytes.toString("utf8"));
  } catch {
    throw new Error("installation manifest is not valid JSON");
  }
  const digest = createHash("sha256").update(exactBytes).digest("hex");
  requireReviewedDigest(
    digest,
    process.env.CDD_EXPECTED_MANIFEST_SHA256,
    process.env.CDD_PRODUCTION === "true",
    process.env.CDD_EXPECTED_SETTINGS_SHA256,
  );
  return {
    exactBytes,
    digest,
    value: parseManifest(parsed),
  };
}

export async function resolveInstallation(
  installationId: string,
): Promise<EmbedRuntimeConfig | null> {
  if (!INSTALLATION_ID.test(installationId)) return null;
  const loaded = await readInstallationManifest();
  const installation = loaded.value.installations[installationId];
  if (!installation) return null;
  return {
    installationId,
    parentOrigins: [...installation.parent_origins],
    identityMode: installation.identity_mode,
    protocolVersions: [...installation.protocol_versions],
    publicOrigin: installation.public_origin,
    publicMountPath: "/agent",
    fallbackUrl: installation.fallback_url,
    manifestDigest: loaded.digest,
    buildId: loaded.value.build_id,
  };
}
