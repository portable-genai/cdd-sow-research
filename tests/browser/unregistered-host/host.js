const status = document.querySelector("#status");
const fallback = document.querySelector("#fallback");
const agent = document.querySelector("cdd-agent");
agent.addEventListener("cdd:fallback", (event) => {
  status.textContent = "Embedding denied; registered standalone fallback available";
  fallback.href = event.detail.href;
  fallback.hidden = false;
});
