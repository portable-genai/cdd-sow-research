const button = document.querySelector("#check");
const status = document.querySelector("#boundary-status");
const frame = document.querySelector("#probe");

button.addEventListener("click", () => {
  const channel = new MessageChannel();
  let acknowledged = false;
  channel.port1.onmessage = () => {
    acknowledged = true;
    status.textContent = "FAILED: invalid global credential was acknowledged";
  };
  frame.contentWindow.postMessage(
    {
      type: "host:init",
      installation_id: "inst_host_a",
      channel_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      loader_instance_id: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      nonce: "cccccccccccccccccccccccccccccccc",
      protocol_versions: ["1"],
      access_token: "credential-must-not-cross-the-global-message",
    },
    "http://127.0.0.1:3200",
    [channel.port2],
  );
  window.setTimeout(() => {
    if (!acknowledged) status.textContent = "PASS: invalid global credential rejected";
    channel.port1.close();
  }, 500);
});
