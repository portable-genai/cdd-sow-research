export const EMBED_PROTOCOL_VERSIONS = ["1"] as const;
export const MAX_PROTOCOL_MESSAGE_BYTES = 64 * 1024;
export const MAX_IFRAME_HEIGHT = 4_096;
export const MIN_IFRAME_HEIGHT = 320;

export type EmbedProtocolVersion = (typeof EMBED_PROTOCOL_VERSIONS)[number];

export interface HostInitMessage {
  type: "host:init";
  installation_id: string;
  channel_id: string;
  loader_instance_id: string;
  nonce: string;
  protocol_versions: string[];
}

export interface AgentReadyMessage {
  type: "agent:ready";
  installation_id: string;
  channel_id: string;
  loader_instance_id: string;
  nonce: string;
  protocol_version: EmbedProtocolVersion;
}

export interface HostCredentialMessage {
  type: "host:credential";
  protocol_version: EmbedProtocolVersion;
  access_token: string;
}

export interface HostPresentationMessage {
  type: "host:presentation";
  protocol_version: EmbedProtocolVersion;
  height: number;
}

export interface HostLaunchCodeMessage {
  type: "host:launch-code";
  protocol_version: EmbedProtocolVersion;
  instance_id: string;
  launch_code: string;
}

export type HostPortMessage =
  | HostCredentialMessage
  | HostPresentationMessage
  | HostLaunchCodeMessage;

export type AgentPortMessage =
  | AgentReadyMessage
  | {
      type: "agent:grant-registration";
      protocol_version: EmbedProtocolVersion;
      instance_id: string;
      expires_at: number;
    }
  | {
      type: "agent:identity-ready";
      protocol_version: EmbedProtocolVersion;
      assurance: "embedded-grant";
    }
  | {
      type: "agent:resize";
      protocol_version: EmbedProtocolVersion;
      height: number;
    }
  | {
      type: "agent:navigation";
      protocol_version: EmbedProtocolVersion;
      href: string;
      reason: "citation-continuation" | "authentication-fallback";
    }
  | {
      type: "agent:authentication";
      protocol_version: EmbedProtocolVersion;
      code: "authentication_required" | "credential_rejected";
      fallback_required: true;
    }
  | {
      type: "agent:error";
      protocol_version: EmbedProtocolVersion;
      code: string;
      recoverable: boolean;
    };

const ID_PATTERN = /^[A-Za-z0-9_-]{16,128}$/;
const INSTALLATION_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
const TOKEN_MAX_BYTES = 32 * 1024;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function serializedSize(value: unknown): number {
  try {
    return new TextEncoder().encode(JSON.stringify(value)).byteLength;
  } catch {
    return Number.POSITIVE_INFINITY;
  }
}

export function validateHostInit(
  value: unknown,
  expectedInstallationId: string,
): HostInitMessage | null {
  if (!isRecord(value) || serializedSize(value) > MAX_PROTOCOL_MESSAGE_BYTES) return null;
  if (
    !hasExactKeys(value, [
      "type",
      "installation_id",
      "channel_id",
      "loader_instance_id",
      "nonce",
      "protocol_versions",
    ])
  ) {
    return null;
  }
  if (
    value.type !== "host:init" ||
    value.installation_id !== expectedInstallationId ||
    typeof value.channel_id !== "string" ||
    !ID_PATTERN.test(value.channel_id) ||
    typeof value.loader_instance_id !== "string" ||
    !ID_PATTERN.test(value.loader_instance_id) ||
    typeof value.nonce !== "string" ||
    !ID_PATTERN.test(value.nonce) ||
    !Array.isArray(value.protocol_versions) ||
    value.protocol_versions.length === 0 ||
    value.protocol_versions.length > 8 ||
    !value.protocol_versions.every(
      (version) => typeof version === "string" && /^[0-9]{1,4}$/.test(version),
    )
  ) {
    return null;
  }
  return value as unknown as HostInitMessage;
}

export function negotiateProtocol(
  offered: readonly string[],
  supported: readonly EmbedProtocolVersion[] = EMBED_PROTOCOL_VERSIONS,
): EmbedProtocolVersion | null {
  return supported.find((version) => offered.includes(version)) ?? null;
}

export function validateHostPortMessage(value: unknown): HostPortMessage | null {
  if (!isRecord(value) || serializedSize(value) > MAX_PROTOCOL_MESSAGE_BYTES) return null;
  if (value.type === "host:credential") {
    if (!hasExactKeys(value, ["type", "protocol_version", "access_token"])) return null;
    if (
      !EMBED_PROTOCOL_VERSIONS.includes(value.protocol_version as EmbedProtocolVersion) ||
      typeof value.access_token !== "string" ||
      value.access_token.length === 0 ||
      new TextEncoder().encode(value.access_token).byteLength > TOKEN_MAX_BYTES
    ) {
      return null;
    }
    return value as unknown as HostCredentialMessage;
  }
  if (value.type === "host:presentation") {
    if (!hasExactKeys(value, ["type", "protocol_version", "height"])) return null;
    if (
      !EMBED_PROTOCOL_VERSIONS.includes(value.protocol_version as EmbedProtocolVersion) ||
      typeof value.height !== "number" ||
      !Number.isInteger(value.height) ||
      value.height < MIN_IFRAME_HEIGHT ||
      value.height > MAX_IFRAME_HEIGHT
    ) {
      return null;
    }
    return value as unknown as HostPresentationMessage;
  }
  if (value.type === "host:launch-code") {
    if (
      !hasExactKeys(value, [
        "type",
        "protocol_version",
        "instance_id",
        "launch_code",
      ])
    ) {
      return null;
    }
    if (
      !EMBED_PROTOCOL_VERSIONS.includes(value.protocol_version as EmbedProtocolVersion) ||
      typeof value.instance_id !== "string" ||
      !/^[A-Za-z0-9._~:-]{22,256}$/.test(value.instance_id) ||
      typeof value.launch_code !== "string" ||
      !/^[A-Za-z0-9._~:-]{22,256}$/.test(value.launch_code)
    ) {
      return null;
    }
    return value as unknown as HostLaunchCodeMessage;
  }
  return null;
}

export function validateAgentPortMessage(value: unknown): AgentPortMessage | null {
  if (!isRecord(value) || serializedSize(value) > MAX_PROTOCOL_MESSAGE_BYTES) return null;
  if (value.type === "agent:ready") {
    if (
      !hasExactKeys(value, [
        "type",
        "installation_id",
        "channel_id",
        "loader_instance_id",
        "nonce",
        "protocol_version",
      ]) ||
      typeof value.installation_id !== "string" ||
      !INSTALLATION_PATTERN.test(value.installation_id) ||
      typeof value.channel_id !== "string" ||
      !ID_PATTERN.test(value.channel_id) ||
      typeof value.loader_instance_id !== "string" ||
      !ID_PATTERN.test(value.loader_instance_id) ||
      typeof value.nonce !== "string" ||
      !ID_PATTERN.test(value.nonce) ||
      !EMBED_PROTOCOL_VERSIONS.includes(value.protocol_version as EmbedProtocolVersion)
    ) {
      return null;
    }
    return value as unknown as AgentReadyMessage;
  }
  if (value.type === "agent:resize") {
    if (!hasExactKeys(value, ["type", "protocol_version", "height"])) return null;
    if (
      !EMBED_PROTOCOL_VERSIONS.includes(value.protocol_version as EmbedProtocolVersion) ||
      typeof value.height !== "number" ||
      !Number.isInteger(value.height) ||
      value.height < MIN_IFRAME_HEIGHT ||
      value.height > MAX_IFRAME_HEIGHT
    ) {
      return null;
    }
    return value as AgentPortMessage;
  }
  if (value.type === "agent:grant-registration") {
    if (
      !hasExactKeys(value, [
        "type",
        "protocol_version",
        "instance_id",
        "expires_at",
      ])
    ) {
      return null;
    }
    if (
      !EMBED_PROTOCOL_VERSIONS.includes(value.protocol_version as EmbedProtocolVersion) ||
      typeof value.instance_id !== "string" ||
      !/^[A-Za-z0-9._~:-]{22,256}$/.test(value.instance_id) ||
      typeof value.expires_at !== "number" ||
      !Number.isInteger(value.expires_at) ||
      value.expires_at <= 0
    ) {
      return null;
    }
    return value as AgentPortMessage;
  }
  if (value.type === "agent:identity-ready") {
    if (!hasExactKeys(value, ["type", "protocol_version", "assurance"])) return null;
    if (
      !EMBED_PROTOCOL_VERSIONS.includes(value.protocol_version as EmbedProtocolVersion) ||
      value.assurance !== "embedded-grant"
    ) {
      return null;
    }
    return value as AgentPortMessage;
  }
  if (value.type === "agent:navigation") {
    if (!hasExactKeys(value, ["type", "protocol_version", "href", "reason"])) return null;
    if (
      !EMBED_PROTOCOL_VERSIONS.includes(value.protocol_version as EmbedProtocolVersion) ||
      typeof value.href !== "string" ||
      value.href.length > 2_048 ||
      !["citation-continuation", "authentication-fallback"].includes(String(value.reason))
    ) {
      return null;
    }
    return value as AgentPortMessage;
  }
  if (value.type === "agent:authentication") {
    if (
      !hasExactKeys(value, [
        "type",
        "protocol_version",
        "code",
        "fallback_required",
      ])
    ) {
      return null;
    }
    if (
      !EMBED_PROTOCOL_VERSIONS.includes(value.protocol_version as EmbedProtocolVersion) ||
      !["authentication_required", "credential_rejected"].includes(String(value.code)) ||
      value.fallback_required !== true
    ) {
      return null;
    }
    return value as AgentPortMessage;
  }
  if (value.type === "agent:error") {
    if (!hasExactKeys(value, ["type", "protocol_version", "code", "recoverable"])) return null;
    if (
      !EMBED_PROTOCOL_VERSIONS.includes(value.protocol_version as EmbedProtocolVersion) ||
      typeof value.code !== "string" ||
      !/^[a-z0-9_]{1,64}$/.test(value.code) ||
      typeof value.recoverable !== "boolean"
    ) {
      return null;
    }
    return value as AgentPortMessage;
  }
  return null;
}
