const agent = document.querySelector("cdd-agent");
const status = document.querySelector("#status");
agent.addEventListener("cdd:ready", () => {
  status.textContent = "cdd-sow-research ready";
});
agent.addEventListener("cdd:fallback", (event) => {
  status.textContent = ``cdd-sow-research` fallback required: ${event.detail.reason}`;
});
