(() => {
  const scriptAtLoad = document.currentScript;
  if (!(scriptAtLoad instanceof HTMLScriptElement) || !scriptAtLoad.src) {
    throw new Error("Doc1 loader must be loaded from a versioned external script");
  }
  const agentOrigin = new URL(scriptAtLoad.src).origin;
  const protocolVersions = ["1"] as const;
  const idPattern = /^[A-Za-z0-9_-]{1,128}$/;
  const handshakeTimeoutMs = 8_000;
  const minHeight = 320;
  const maxHeight = 4_096;
  const opaqueBindingPattern = /^[A-Za-z0-9._~:-]{22,256}$/;

  type ReadyMessage = {
    type: "agent:ready";
    installation_id: string;
    channel_id: string;
    loader_instance_id: string;
    nonce: string;
    protocol_version: string;
  };

  function randomId(): string {
    const bytes = crypto.getRandomValues(new Uint8Array(24));
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  function exactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
    const actual = Object.keys(value).sort();
    const wanted = [...expected].sort();
    return actual.length === wanted.length && actual.every((key, index) => key === wanted[index]);
  }

  function withinMessageLimit(value: unknown): boolean {
    try {
      return new TextEncoder().encode(JSON.stringify(value)).byteLength <= 64 * 1024;
    } catch {
      return false;
    }
  }

  function observeHostBoundary(
    surface: "window-message",
    direction: "host-to-agent",
    value: unknown,
  ): void {
    const probe = (
      window as unknown as {
        __cddHostBoundaryProbe?: {
          observe?: (surface: string, direction: string, value: unknown) => void;
        };
      }
    ).__cddHostBoundaryProbe;
    if (typeof probe?.observe !== "function") return;
    try {
      probe.observe(surface, direction, value);
    } catch {
      // Evidence instrumentation must never alter the production channel decision.
    }
  }

  function isReadyMessage(value: unknown): value is ReadyMessage {
    if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
    if (!withinMessageLimit(value)) return false;
    const record = value as Record<string, unknown>;
    return (
      exactKeys(record, [
        "type",
        "installation_id",
        "channel_id",
        "loader_instance_id",
        "nonce",
        "protocol_version",
      ]) &&
      record.type === "agent:ready" &&
      typeof record.installation_id === "string" &&
      typeof record.channel_id === "string" &&
      typeof record.loader_instance_id === "string" &&
      typeof record.nonce === "string" &&
      typeof record.protocol_version === "string"
    );
  }

  class CddAgentElement extends HTMLElement {
    private iframe: HTMLIFrameElement | null = null;
    private port: MessagePort | null = null;
    private protocolVersion = "";
    private timeout: number | null = null;
    private retryTimer: number | null = null;
    private pendingChannels = new Set<MessageChannel>();
    private handshakeStopped = false;
    private lastResizeAt = 0;
    private connected = false;
    private fallbackHref = "";

    connectedCallback(): void {
      if (this.connected) return;
      this.connected = true;
      const installationId = this.getAttribute("installation-id")?.trim() ?? "";
      if (!idPattern.test(installationId)) {
        this.renderConfigurationError("A valid installation-id is required.");
        return;
      }

      const shadow = this.attachShadow({ mode: "closed" });
      const container = document.createElement("div");
      container.style.cssText = "display:block;width:100%;min-height:320px";

      const fallback = document.createElement("a");
      fallback.href = `${agentOrigin}/agent/embed/${encodeURIComponent(installationId)}/fallback`;
      this.fallbackHref = fallback.href;
      fallback.textContent = this.getAttribute("fallback-label")?.trim() || "Open Doc1 standalone";
      fallback.referrerPolicy = "no-referrer";
      fallback.style.cssText =
        "display:inline-block;margin:0 0 8px;font:14px system-ui;color:#2945d6;text-decoration:underline";
      container.append(fallback);

      const iframe = document.createElement("iframe");
      iframe.title = this.getAttribute("title")?.trim() || "Doc1 CDD and Source-of-Wealth Agent";
      iframe.src = `${agentOrigin}/agent/embed/${encodeURIComponent(installationId)}/`;
      iframe.sandbox.value = "allow-scripts allow-same-origin";
      iframe.referrerPolicy = "no-referrer";
      iframe.allow = "";
      iframe.style.cssText =
        "display:block;width:100%;border:0;min-height:320px;background:#f5f7fa";
      const requestedHeight = Number(this.getAttribute("height"));
      iframe.height = String(
        Number.isInteger(requestedHeight)
          ? Math.min(maxHeight, Math.max(minHeight, requestedHeight))
          : 720,
      );
      iframe.addEventListener(
        "load",
        () => {
          this.beginHandshake(installationId);
          this.retryTimer = window.setInterval(
            () => this.beginHandshake(installationId),
            250,
          );
        },
        { once: true },
      );
      container.append(iframe);
      shadow.append(container);
      this.iframe = iframe;

      this.timeout = window.setTimeout(() => {
        if (!this.port) {
          this.handshakeStopped = true;
          this.stopHandshakeAttempts();
          this.signalFallback("handshake_timeout", fallback.href);
        }
      }, handshakeTimeoutMs);
    }

    disconnectedCallback(): void {
      this.cleanup();
    }

    setAccessToken(accessToken: string): void {
      if (!this.port || !this.protocolVersion) {
        throw new Error("Doc1 iframe is not ready");
      }
      if (!accessToken || new TextEncoder().encode(accessToken).byteLength > 32 * 1024) {
        throw new Error("Doc1 access token is empty or too large");
      }
      this.port.postMessage({
        type: "host:credential",
        protocol_version: this.protocolVersion,
        access_token: accessToken,
      });
    }

    setLaunchCode(instanceId: string, launchCode: string): void {
      if (!this.port || !this.protocolVersion) {
        throw new Error("Doc1 iframe is not ready");
      }
      if (
        !opaqueBindingPattern.test(instanceId) ||
        !opaqueBindingPattern.test(launchCode)
      ) {
        throw new Error("Doc1 grant binding is invalid");
      }
      this.port.postMessage({
        type: "host:launch-code",
        protocol_version: this.protocolVersion,
        instance_id: instanceId,
        launch_code: launchCode,
      });
    }

    private beginHandshake(installationId: string): void {
      if (this.port || this.handshakeStopped) return;
      if (!this.iframe?.contentWindow) {
        this.handshakeStopped = true;
        this.stopHandshakeAttempts();
        this.signalFallback(
          "iframe_unavailable",
          `${agentOrigin}/agent/embed/${encodeURIComponent(installationId)}/fallback`,
        );
        return;
      }
      const channelId = randomId();
      const loaderInstanceId = randomId();
      const nonce = randomId();
      const channel = new MessageChannel();
      this.pendingChannels.add(channel);
      const onHandshake = (event: MessageEvent<unknown>) => {
        if (
          !isReadyMessage(event.data) ||
          event.data.installation_id !== installationId ||
          event.data.channel_id !== channelId ||
          event.data.loader_instance_id !== loaderInstanceId ||
          event.data.nonce !== nonce ||
          !protocolVersions.includes(event.data.protocol_version as "1")
        ) {
          this.handshakeStopped = true;
          this.stopHandshakeAttempts();
          this.signalFallback(
            "invalid_handshake",
            `${agentOrigin}/agent/embed/${encodeURIComponent(installationId)}/fallback`,
          );
          channel.port1.close();
          return;
        }
        this.handshakeStopped = true;
        this.stopHandshakeAttempts(channel.port1);
        channel.port1.removeEventListener("message", onHandshake);
        channel.port1.addEventListener("message", (message) => this.onAgentMessage(message));
        this.port = channel.port1;
        this.protocolVersion = event.data.protocol_version;
        if (this.timeout !== null) window.clearTimeout(this.timeout);
        this.timeout = null;
        this.dispatchEvent(
          new CustomEvent("cdd:ready", {
            bubbles: true,
            detail: {
              installationId,
              protocolVersion: this.protocolVersion,
              loaderInstanceId,
            },
          }),
        );
      };
      channel.port1.addEventListener("message", onHandshake);
      channel.port1.start();
      const init = {
        type: "host:init",
        installation_id: installationId,
        channel_id: channelId,
        loader_instance_id: loaderInstanceId,
        nonce,
        protocol_versions: [...protocolVersions],
      };
      observeHostBoundary("window-message", "host-to-agent", init);
      this.iframe.contentWindow.postMessage(init, agentOrigin, [channel.port2]);
    }

    private stopHandshakeAttempts(activePort: MessagePort | null = null): void {
      if (this.retryTimer !== null) window.clearInterval(this.retryTimer);
      this.retryTimer = null;
      for (const pending of this.pendingChannels) {
        if (pending.port1 !== activePort) pending.port1.close();
      }
      this.pendingChannels.clear();
    }

    private onAgentMessage(event: MessageEvent<unknown>): void {
      const value = event.data;
      if (typeof value !== "object" || value === null || Array.isArray(value)) return;
      if (!withinMessageLimit(value)) return;
      const message = value as Record<string, unknown>;
      if (message.protocol_version !== this.protocolVersion || typeof message.type !== "string") {
        return;
      }
      if (
        message.type === "agent:identity-ready" &&
        exactKeys(message, ["type", "protocol_version", "assurance"]) &&
        message.assurance === "embedded-grant"
      ) {
        this.dispatchEvent(
          new CustomEvent("cdd:identity-ready", {
            bubbles: true,
            detail: {
              installationId: this.getAttribute("installation-id") ?? "",
              assurance: message.assurance,
            },
          }),
        );
        return;
      }
      if (
        message.type === "agent:grant-registration" &&
        exactKeys(message, [
          "type",
          "protocol_version",
          "instance_id",
          "expires_at",
        ]) &&
        typeof message.instance_id === "string" &&
        opaqueBindingPattern.test(message.instance_id) &&
        typeof message.expires_at === "number" &&
        Number.isInteger(message.expires_at) &&
        message.expires_at > 0
      ) {
        this.dispatchEvent(
          new CustomEvent("cdd:grant-registration", {
            bubbles: true,
            detail: {
              installationId: this.getAttribute("installation-id") ?? "",
              instanceId: message.instance_id,
              expiresAt: message.expires_at,
            },
          }),
        );
        return;
      }
      if (
        message.type === "agent:resize" &&
        exactKeys(message, ["type", "protocol_version", "height"]) &&
        typeof message.height === "number" &&
        Number.isInteger(message.height) &&
        message.height >= minHeight &&
        message.height <= maxHeight
      ) {
        const now = performance.now();
        if (now - this.lastResizeAt >= 100 && this.iframe) {
          this.iframe.height = String(message.height);
          this.lastResizeAt = now;
        }
        return;
      }
      if (
        message.type === "agent:authentication" &&
        exactKeys(message, ["type", "protocol_version", "code", "fallback_required"]) &&
        typeof message.code === "string" &&
        ["authentication_required", "credential_rejected"].includes(message.code) &&
        message.fallback_required === true
      ) {
        this.signalFallback(
          message.code,
          this.fallbackHref,
        );
        return;
      }
      if (
        message.type === "agent:navigation" &&
        exactKeys(message, ["type", "protocol_version", "href", "reason"]) &&
        typeof message.href === "string" &&
        message.href.length <= 2_048 &&
        ["citation-continuation", "authentication-fallback"].includes(
          String(message.reason),
        )
      ) {
        this.dispatchEvent(
          new CustomEvent("cdd:navigation", {
            bubbles: true,
            detail: { href: message.href, reason: message.reason },
          }),
        );
        return;
      }
      if (
        message.type === "agent:error" &&
        exactKeys(message, ["type", "protocol_version", "code", "recoverable"]) &&
        typeof message.code === "string" &&
        /^[a-z0-9_]{1,64}$/.test(message.code) &&
        typeof message.recoverable === "boolean"
      ) {
        this.dispatchEvent(
          new CustomEvent("cdd:error", {
            bubbles: true,
            detail: { code: message.code, recoverable: message.recoverable },
          }),
        );
      }
    }

    private signalFallback(reason: string, href: string): void {
      this.dispatchEvent(
        new CustomEvent("cdd:fallback", {
          bubbles: true,
          detail: { reason, href },
        }),
      );
    }

    private renderConfigurationError(message: string): void {
      this.textContent = message;
      this.dispatchEvent(
        new CustomEvent("cdd:error", {
          bubbles: true,
          detail: { code: "invalid_configuration", recoverable: false },
        }),
      );
    }

    private cleanup(): void {
      if (this.timeout !== null) window.clearTimeout(this.timeout);
      this.timeout = null;
      this.handshakeStopped = true;
      this.stopHandshakeAttempts();
      this.port?.close();
      this.port = null;
      this.protocolVersion = "";
      this.fallbackHref = "";
    }
  }

  if (!customElements.get("cdd-agent")) {
    customElements.define("cdd-agent", CddAgentElement);
  }
})();
