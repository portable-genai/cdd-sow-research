const agent = document.querySelector("cdd-agent");
const status = document.querySelector("#status");
agent.addEventListener("cdd:ready", () => {
  status.textContent = "cdd-sow-research ready";
});
agent.addEventListener("cdd:error", (event) => {
  status.textContent = ``cdd-sow-research` error: ${event.detail.code}`;
});
