const agent = document.querySelector("cdd-agent");
const status = document.querySelector("#status");
agent.addEventListener("cdd:ready", () => {
  status.textContent = "Doc1 ready";
});
agent.addEventListener("cdd:error", (event) => {
  status.textContent = `Doc1 error: ${event.detail.code}`;
});
