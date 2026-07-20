/* PIOS browser sensor — MV3 service worker.
 *
 * Mirrors WindowSensor semantics: when focus moves, we emit the span that just
 * FINISHED (with its duration), never the one just opened. The worker is
 * non-persistent, so the in-flight visit and the retry queue live in
 * chrome.storage.session rather than module scope.
 */

const PORT_DEFAULT = 8321;
const QUEUE_MAX = 200;      // hard cap; oldest dropped first
const MIN_VISIT_S = 2;      // ignore fly-past tabs
const SKIP_SCHEMES = /^(?!https?:)/i;

/* Fallback blocklist, mirroring the server defaults. Used until (or unless)
 * the live list is fetched — if PIOS is offline at browser startup the fetch
 * fails, and an empty list would mean NOTHING is blocked. Privacy filters
 * must fail closed, so we ship a baseline the extension can always apply. */
const DEFAULT_BLOCKED = [
  "accounts.google.com", "login.microsoftonline.com", "paypal.com",
  "bankofamerica.com", "chase.com", "hsbc.co.uk", "barclays.co.uk",
  "lloydsbank.com", "monzo.com", "revolut.com", "coinbase.com",
  "1password.com", "bitwarden.com", "lastpass.com"
];

let flushing = false;

async function opts() {
  const s = await chrome.storage.local.get(["port", "token"]);
  return { port: s.port || PORT_DEFAULT, token: s.token || "" };
}

async function base() {
  return "http://127.0.0.1:" + (await opts()).port;
}

function hostOf(url) {
  try { return new URL(url).hostname.toLowerCase(); } catch (e) { return ""; }
}

function blockedBy(host, list) {
  return (list || []).some(d => {
    d = String(d).toLowerCase().trim();
    return d && (host === d || host.endsWith("." + d));
  });
}

async function eligible(tab) {
  if (!tab || !tab.url || tab.incognito) return false;
  if (SKIP_SCHEMES.test(tab.url)) return false;   // chrome://, edge://, file://, about:
  const host = hostOf(tab.url);
  if (!host) return false;
  const { blocked, sensorOn } = await chrome.storage.session.get(["blocked", "sensorOn"]);
  if (sensorOn === false) return false;
  // `blocked` is undefined until the first successful fetch — fall back to the
  // bundled list rather than to "block nothing".
  return !blockedBy(host, blocked || DEFAULT_BLOCKED);
}

/* Focus moved to `tab` (or nothing, if null). Close out the previous visit. */
async function focusChanged(tab) {
  const now = Date.now();
  const ok = await eligible(tab);
  const next = ok ? { url: tab.url, title: tab.title || "", start: now } : null;
  const { cur } = await chrome.storage.session.get("cur");

  // Same page still in front (title tweak from a live page, tab re-activated):
  // let the running span continue rather than chopping it up.
  if (cur && next && cur.url === next.url && cur.title === next.title) return;

  if (cur) {
    const dur = (now - cur.start) / 1000;
    if (dur >= MIN_VISIT_S) {
      await enqueue({ url: cur.url, title: cur.title, dur_s: Math.round(dur) });
    }
  }
  await chrome.storage.session.set({ cur: next });
}

async function enqueue(ev) {
  const { queue } = await chrome.storage.session.get("queue");
  const q = (queue || []).concat([ev]).slice(-QUEUE_MAX);
  await chrome.storage.session.set({ queue: q });
  flush();
}

async function flush() {
  if (flushing) return;
  const { token, port } = await opts();
  if (!token) return;                       // not configured yet — hold the queue
  const { queue } = await chrome.storage.session.get("queue");
  if (!queue || !queue.length) return;
  flushing = true;
  const batch = queue.slice(0, 50);
  try {
    const r = await fetch("http://127.0.0.1:" + port + "/api/events/browser", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token, events: batch })
    });
    if (r.ok) {
      const { queue: cur } = await chrome.storage.session.get("queue");
      await chrome.storage.session.set({ queue: (cur || []).slice(batch.length) });
    }
    // Non-OK (401 bad token, 5xx): keep the batch, retry on the next alarm.
    // The queue cap stops that from growing without bound.
  } catch (e) {
    // PIOS not running. Silent by design: never spam, never block browsing.
  } finally {
    flushing = false;
  }
}

async function refreshBlocklist() {
  try {
    const r = await fetch((await base()) + "/api/extension/config");
    if (!r.ok) return;
    const c = await r.json();
    await chrome.storage.session.set({
      blocked: c.blocked_domains || [], sensorOn: c.browser_sensor !== false
    });
  } catch (e) { /* offline; keep whatever we had */ }
}

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  try { await focusChanged(await chrome.tabs.get(tabId)); } catch (e) {}
});

chrome.tabs.onUpdated.addListener(async (tabId, info, tab) => {
  if (!info.url && !info.title) return;
  if (!tab.active) return;
  try { await focusChanged(tab); } catch (e) {}
});

chrome.windows.onFocusChanged.addListener(async (winId) => {
  try {
    if (winId === chrome.windows.WINDOW_ID_NONE) return focusChanged(null);
    const [tab] = await chrome.tabs.query({ active: true, windowId: winId });
    await focusChanged(tab);
  } catch (e) {}
});

chrome.alarms.onAlarm.addListener(a => {
  if (a.name === "pios-blocklist") refreshBlocklist();
  else flush();
});

function boot() {
  chrome.alarms.create("pios-flush", { periodInMinutes: 1 });
  chrome.alarms.create("pios-blocklist", { periodInMinutes: 30 });
  refreshBlocklist();
}
chrome.runtime.onInstalled.addListener(boot);
chrome.runtime.onStartup.addListener(boot);
