const $ = id => document.getElementById(id);

chrome.storage.local.get(["token", "port"]).then(s => {
  $("token").value = s.token || "";
  $("port").value = s.port || 8321;
  if (s.token) test();
});

function show(cls, msg) { $("status").className = cls; $("status").textContent = msg; }

/* An empty batch is a valid request, so it doubles as the health check:
   200 = reachable and the token matches, 401 = wrong token. */
async function test() {
  const token = $("token").value.trim(), port = $("port").value || 8321;
  try {
    const r = await fetch("http://127.0.0.1:" + port + "/api/events/browser", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token, events: [] })
    });
    if (r.ok) show("ok", "Connected — PIOS is recording browser activity.");
    else if (r.status === 401) show("bad", "Reachable, but the token is wrong.");
    else show("bad", "PIOS answered with HTTP " + r.status + ".");
  } catch (e) {
    show("bad", "Can't reach PIOS on port " + port + " — is it running?");
  }
}

$("save").onclick = async () => {
  await chrome.storage.local.set({
    token: $("token").value.trim(), port: Number($("port").value) || 8321
  });
  show("dim", "Saved. Testing…");
  test();
};
