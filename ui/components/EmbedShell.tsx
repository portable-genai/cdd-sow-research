"use client";

import { useEffect, useRef, useState } from "react";

import { configureApiTransport, setAccessToken } from "../lib/api";
import {
  EMBED_PROTOCOL_VERSIONS,
  MAX_IFRAME_HEIGHT,
  MIN_IFRAME_HEIGHT,
  negotiateProtocol,
  validateHostInit,
  validateHostPortMessage,
  type AgentPortMessage,
  type EmbedProtocolVersion,
} from "../lib/embed/protocol";
import type { TransportSignal } from "../lib/embed/transport";
import { CANONICAL_API_BASE } from "../lib/embed/transport";
import { configureAgentNavigation } from "../lib/embed/navigation";
import type { EmbedRuntimeConfig } from "../lib/runtime-config";
import { AgentConsole } from "./AgentConsole";

const OPAQUE_BINDING = /^[A-Za-z0-9._~:-]{22,256}$/;

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

async function newPkce(): Promise<{ verifier: string; challenge: string }> {
  const verifier = base64Url(crypto.getRandomValues(new Uint8Array(48)));
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return { verifier, challenge: base64Url(new Uint8Array(digest)) };
}

async function brokerPost(
  path: string,
  installationId: string,
  manifestDigest: string,
  body: Record<string, unknown>,
): Promise<unknown> {
  const response = await fetch(`${CANONICAL_API_BASE}${path}`, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      "X-CDD-Installation-ID": installationId,
      "X-CDD-Manifest-SHA256": manifestDigest,
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error("embedded grant request rejected");
  return response.json();
}

export function EmbedShell({ runtime }: { runtime: EmbedRuntimeConfig }) {
  const [ready, setReady] = useState(false);
  const [identityReady, setIdentityReady] = useState(runtime.identityMode !== "embedded-grant");
  const [failure, setFailure] = useState("");
  const portRef = useRef<MessagePort | null>(null);
  const protocolRef = useRef<EmbedProtocolVersion | null>(null);
  const grantRef = useRef<{ instanceId: string; verifier: string } | null>(null);
  const redeemStartedRef = useRef(false);

  useEffect(() => {
    let resizeObserver: ResizeObserver | null = null;
    let lastResizeAt = 0;
    if (window.location.origin !== runtime.publicOrigin) {
      setFailure("The embed installation is not running on its reviewed public origin.");
      return;
    }
    if (window.parent === window) {
      setFailure("This installation route must be opened by its registered host.");
      return;
    }

    const post = (message: AgentPortMessage) => portRef.current?.postMessage(message);
    const onTransportSignal = (signal: TransportSignal) => {
      const protocol = protocolRef.current;
      if (!protocol) return;
      if (signal.type === "authentication") {
        post({
          type: "agent:authentication",
          protocol_version: protocol,
          code: signal.code,
          fallback_required: true,
        });
      } else {
        post({
          type: "agent:error",
          protocol_version: protocol,
          code: signal.code,
          recoverable: signal.recoverable,
        });
      }
    };
    configureApiTransport({
      installationId: runtime.installationId,
      manifestDigest: runtime.manifestDigest,
      accessToken: "",
      identityMode: runtime.identityMode,
      onSignal: onTransportSignal,
    });

    const failGrant = () => {
      setFailure("The embedded identity grant could not be completed.");
      const protocol = protocolRef.current;
      if (protocol) {
        post({
          type: "agent:error",
          protocol_version: protocol,
          code: "embedded_grant_rejected",
          recoverable: false,
        });
      }
    };

    const registerGrant = async (port: MessagePort, protocol: EmbedProtocolVersion) => {
      try {
        const pkce = await newPkce();
        const value = (await brokerPost(
          "/v1/embed/instances",
          runtime.installationId,
          runtime.manifestDigest,
          {
            installation_id: runtime.installationId,
            protocol_version: protocol,
            pkce_challenge: pkce.challenge,
            pkce_method: "S256",
          },
        )) as Record<string, unknown>;
        if (
          value.state !== "REGISTERED" ||
          typeof value.instance_id !== "string" ||
          !OPAQUE_BINDING.test(value.instance_id) ||
          typeof value.expires_at !== "number" ||
          !Number.isInteger(value.expires_at)
        ) {
          throw new Error("invalid embedded grant registration");
        }
        grantRef.current = { instanceId: value.instance_id, verifier: pkce.verifier };
        port.postMessage({
          type: "agent:grant-registration",
          protocol_version: protocol,
          instance_id: value.instance_id,
          expires_at: value.expires_at,
        } satisfies AgentPortMessage);
      } catch {
        failGrant();
      }
    };

    const redeemGrant = async (
      instanceId: string,
      launchCode: string,
      protocol: EmbedProtocolVersion,
    ) => {
      const grant = grantRef.current;
      if (
        redeemStartedRef.current ||
        !grant ||
        grant.instanceId !== instanceId
      ) {
        failGrant();
        return;
      }
      redeemStartedRef.current = true;
      try {
        const value = (await brokerPost(
          "/v1/embed/token",
          runtime.installationId,
          runtime.manifestDigest,
          {
            installation_id: runtime.installationId,
            instance_id: instanceId,
            launch_code: launchCode,
            pkce_verifier: grant.verifier,
          },
        )) as Record<string, unknown>;
        if (
          value.token_type !== "Bearer" ||
          typeof value.access_token !== "string" ||
          value.access_token.length === 0 ||
          typeof value.expires_in !== "number" ||
          value.expires_in <= 0 ||
          typeof value.scope !== "string"
        ) {
          throw new Error("invalid embedded grant token response");
        }
        grantRef.current = null;
        setAccessToken(value.access_token);
        setIdentityReady(true);
        post({
          type: "agent:identity-ready",
          protocol_version: protocol,
          assurance: "embedded-grant",
        });
      } catch {
        grantRef.current = null;
        failGrant();
      }
    };

    const onBootstrap = (event: MessageEvent<unknown>) => {
      if (
        event.source !== window.parent ||
        !runtime.parentOrigins.includes(event.origin) ||
        event.ports.length !== 1
      ) {
        return;
      }
      const init = validateHostInit(event.data, runtime.installationId);
      if (!init || portRef.current) return;
      const enabled = EMBED_PROTOCOL_VERSIONS.filter((version) =>
        runtime.protocolVersions.includes(version),
      );
      const protocol = negotiateProtocol(init.protocol_versions, enabled);
      if (!protocol) {
        setFailure("No mutually supported embed protocol version.");
        event.ports[0].close();
        return;
      }

      const port = event.ports[0];
      portRef.current = port;
      protocolRef.current = protocol;
      configureAgentNavigation((message) => port.postMessage(message), protocol);
      const onPortMessage = (messageEvent: MessageEvent<unknown>) => {
        const message = validateHostPortMessage(messageEvent.data);
        if (!message || message.protocol_version !== protocol) {
          post({
            type: "agent:error",
            protocol_version: protocol,
            code: "invalid_protocol_message",
            recoverable: false,
          });
          return;
        }
        if (message.type === "host:credential") {
          if (runtime.identityMode !== "oauth-access-token") {
            post({
              type: "agent:error",
              protocol_version: protocol,
              code: "credential_not_allowed",
              recoverable: false,
            });
            return;
          }
          setAccessToken(message.access_token);
          setIdentityReady(true);
          return;
        }
        if (message.type === "host:launch-code") {
          if (runtime.identityMode !== "embedded-grant") {
            post({
              type: "agent:error",
              protocol_version: protocol,
              code: "launch_code_not_allowed",
              recoverable: false,
            });
            return;
          }
          void redeemGrant(message.instance_id, message.launch_code, protocol);
        }
      };
      port.addEventListener("message", onPortMessage);
      port.start();
      port.postMessage({
        type: "agent:ready",
        installation_id: runtime.installationId,
        channel_id: init.channel_id,
        loader_instance_id: init.loader_instance_id,
        nonce: init.nonce,
        protocol_version: protocol,
      } satisfies AgentPortMessage);
      window.removeEventListener("message", onBootstrap);
      setReady(true);
      if (runtime.identityMode === "embedded-grant") {
        void registerGrant(port, protocol);
      }

      resizeObserver = new ResizeObserver(() => {
        const now = performance.now();
        if (now - lastResizeAt < 100) return;
        lastResizeAt = now;
        const height = Math.min(
          MAX_IFRAME_HEIGHT,
          Math.max(MIN_IFRAME_HEIGHT, Math.ceil(document.documentElement.scrollHeight)),
        );
        post({ type: "agent:resize", protocol_version: protocol, height });
      });
      resizeObserver.observe(document.documentElement);
    };

    window.addEventListener("message", onBootstrap);
    return () => {
      window.removeEventListener("message", onBootstrap);
      resizeObserver?.disconnect();
      portRef.current?.close();
      portRef.current = null;
      protocolRef.current = null;
      configureAgentNavigation(null, null);
      grantRef.current = null;
      redeemStartedRef.current = false;
      configureApiTransport({
        installationId: "",
        manifestDigest: "",
        accessToken: "",
        identityMode: "",
        onSignal: null,
      });
    };
  }, [runtime]);

  if (failure) {
    return (
      <div className="rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
        {failure}
      </div>
    );
  }
  if (!ready) {
    return (
      <div className="rounded border border-ink-200 bg-white px-3 py-6 text-sm text-ink-500">
        Establishing the registered host channel…
      </div>
    );
  }
  if (!identityReady) {
    return (
      <div
        role="status"
        className="rounded border border-ink-200 bg-white px-3 py-6 text-sm text-ink-500"
      >
        Requesting a short-lived embedded identity grant…
      </div>
    );
  }
  return <AgentConsole embedded />;
}
