const agent = document.querySelector("cdd-agent");
const status = document.querySelector("#status");

window.__doc1SafeTrace = [];

agent.addEventListener("cdd:ready", () => {
  window.__doc1SafeTrace.push({ event: "channel-ready" });
  status.textContent = "Registered channel ready; iframe is creating PKCE";
});
agent.addEventListener("cdd:grant-registration", (event) => {
  const registration = {
    installationId: event.detail.installationId,
    instanceId: event.detail.instanceId,
    expiresAt: event.detail.expiresAt,
  };
  window.__doc1GrantRegistration = registration;
  window.__doc1SafeTrace.push({ event: "grant-registered", ...registration });
  status.textContent = "Iframe grant registered; waiting for BFF authorization";
});
agent.addEventListener("cdd:identity-ready", (event) => {
  window.__doc1SafeTrace.push({
    event: "identity-ready",
    installationId: event.detail.installationId,
    assurance: event.detail.assurance,
  });
  status.textContent = "PASS: embedded identity ready without host credential custody";
});
agent.addEventListener("cdd:fallback", (event) => {
  window.__doc1SafeTrace.push({ event: "fallback", reason: event.detail.reason });
  status.textContent = `Embedded grant fallback required: ${event.detail.reason}`;
});
