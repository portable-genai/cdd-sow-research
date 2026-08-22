import type { AgentPortMessage, EmbedProtocolVersion } from "./protocol";

type NavigationPost = (message: AgentPortMessage) => void;

let navigationPost: NavigationPost | null = null;
let negotiatedProtocol: EmbedProtocolVersion | null = null;

export function configureAgentNavigation(
  post: NavigationPost | null,
  protocol: EmbedProtocolVersion | null,
): void {
  navigationPost = post;
  negotiatedProtocol = protocol;
}

export function hasAgentNavigationChannel(): boolean {
  return navigationPost !== null && negotiatedProtocol !== null;
}

function isLoopback(hostname: string): boolean {
  const host = hostname.replace(/^\[|\]$/g, "").toLowerCase();
  return host === "localhost" || host === "::1" || /^127(?:\.\d{1,3}){3}$/.test(host);
}

export function emitCitationNavigation(href: string): void {
  if (!navigationPost || !negotiatedProtocol) {
    throw new Error("embedded navigation channel is not negotiated");
  }
  let parsed: URL;
  try {
    parsed = new URL(href);
  } catch {
    throw new Error("citation continuation URL is invalid");
  }
  const secureOrigin =
    parsed.protocol === "https:" ||
    (parsed.protocol === "http:" && isLoopback(parsed.hostname));
  if (!secureOrigin || parsed.username || parsed.password || href.length > 2_048) {
    throw new Error("citation continuation URL violates the secure-origin policy");
  }
  navigationPost({
    type: "agent:navigation",
    protocol_version: negotiatedProtocol,
    href,
    reason: "citation-continuation",
  });
}
