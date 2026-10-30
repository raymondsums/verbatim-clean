// All server calls happen here: MV3 content scripts are subject to the
// page's CORS and mixed-content rules, but the service worker is not, and
// host_permissions covers the server.

// Where the Verbatim server is. Set in the popup's Settings and kept in
// chrome.storage.local as "serverUrl"; the default is the local server.
// A service worker can be stopped at any time, so the address is read
// again for every message rather than trusted from memory.
const DEFAULT_SERVER = "http://localhost:5053";
let BASE = DEFAULT_SERVER;

async function loadBase() {
  try {
    const o = await chrome.storage.local.get("serverUrl");
    BASE = (o.serverUrl || DEFAULT_SERVER).replace(/\/+$/, "");
  } catch (e) {
    BASE = DEFAULT_SERVER;
  }
  return BASE;
}

async function post(path, payload) {
  const res = await fetch(BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `server error ${res.status}`);
  return data;
}

// ---- updating itself ----
// While Verbatim is being worked on, the extension reloads itself when its
// files on disk carry a new version, so it never needs a trip to the
// extensions page. Pages already open keep the old copy until reloaded.
// It waits while a fill is running, and stops checking after a week with
// no new version (a manual refresh in the extensions page turns it back on).
const UPDATE_MINUTES = 5;
const QUIET_DAYS = 7;
const busyTabs = new Set();

function armUpdates() {
  chrome.alarms.create("self-update", { periodInMinutes: UPDATE_MINUTES });
}

chrome.runtime.onInstalled.addListener(async () => {
  await chrome.storage.local.set({ selfUpdate: true, lastUpdate: Date.now() });
  armUpdates();
});

chrome.runtime.onStartup.addListener(async () => {
  const { selfUpdate = true } = await chrome.storage.local.get("selfUpdate");
  if (selfUpdate) armUpdates();
});

chrome.alarms.onAlarm.addListener(a => {
  if (a.name === "self-update") checkForUpdate();
});

async function checkForUpdate() {
  const { selfUpdate = true, lastUpdate = Date.now() } =
    await chrome.storage.local.get(["selfUpdate", "lastUpdate"]);
  if (!selfUpdate || Date.now() - lastUpdate > QUIET_DAYS * 86400000) {
    await chrome.storage.local.set({ selfUpdate: false });
    chrome.alarms.clear("self-update");
    return;
  }
  let disk;
  try {
    disk = await (await fetch(chrome.runtime.getURL("manifest.json"),
                              { cache: "no-store" })).json();
  } catch (e) {
    return;
  }
  if (!disk || !disk.version || disk.version === chrome.runtime.getManifest().version) return;
  if (busyTabs.size) return; // try again on the next check
  chrome.runtime.reload();
}

// ---- keyboard shortcut ----
// Option+Shift+F fills the form in the current tab, the same as the panel's
// Autofill button. Change the keys at arc://extensions/shortcuts. Pressing
// it grants activeTab, so it also works on careers sites the content script
// is not listed for. Picks the frame holding the form, like popup.js does.
chrome.commands.onCommand.addListener((command, tab) => {
  if (command === "fill-form" && tab && tab.id) fillTab(tab);
});

async function fillTab(tab) {
  let injected;
  try {
    injected = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true }, files: ["content.js"],
    });
  } catch (e) {
    try {
      injected = await chrome.scripting.executeScript({
        target: { tabId: tab.id }, files: ["content.js"],
      });
    } catch (e2) {
      return; // a page extensions cannot touch, like the extensions page
    }
  }
  const ids = [...new Set(injected.map(r => r.frameId ?? 0))];
  const probes = await Promise.all(ids.map(async id => {
    try {
      const r = await chrome.tabs.sendMessage(tab.id, { type: "probe-page" }, { frameId: id });
      return { id, weight: (r && r.weight) || 0 };
    } catch (e) {
      return { id, weight: 0 };
    }
  }));
  probes.sort((a, b) => b.weight - a.weight);
  if (!probes.length || !probes[0].weight) return; // no form here
  chrome.tabs.sendMessage(tab.id, { type: "panel-fill", frameChosen: true },
                          { frameId: probes[0].id }).catch(() => {});
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      await loadBase();
      if (msg.type === "fill-busy") {
        const tab = sender.tab ? sender.tab.id : -1;
        if (msg.on) busyTabs.add(tab); else busyTabs.delete(tab);
        sendResponse({ ok: true });
      } else if (msg.type === "tailor-start") {
        // mode "resume" or "letter": the panel's two buttons, one half each.
        sendResponse({ ok: true, data: await post("/api/tailor/start", msg.payload || {}) });
      } else if (msg.type === "tailor-status") {
        const mode = (msg.payload || {}).mode;
        const res = await fetch(BASE + "/api/tailor/status" + (mode ? "?mode=" + encodeURIComponent(mode) : ""));
        sendResponse({ ok: res.ok, data: await res.json() });
      } else if (msg.type === "ui-theme") {
        const res = await fetch(BASE + "/api/ui/theme");
        sendResponse({ ok: res.ok, data: await res.json() });
      } else if (msg.type === "ui-theme-set") {
        sendResponse({ ok: true, data: await post("/api/ui/theme", msg.payload) });
      } else if (msg.type === "watch-add") {
        // A posting the user found themselves, onto the job board.
        sendResponse({ ok: true, data: await post("/api/watch/add", msg.payload) });
      } else if (msg.type === "jobs-check") {
        // Have you applied to this already. Reports only, never writes: a row
        // appears in the tracker when you press Log it, not when you look at
        // a posting.
        sendResponse({ ok: true, data: await post("/api/jobs/check", msg.payload) });
      } else if (msg.type === "watch-jobs") {
        // The job watcher's list, for the panel's fit pill. A server
        // without the watcher answers 404, which the panel treats as none.
        const res = await fetch(BASE + "/api/watch/jobs?view=all");
        const data = await res.json().catch(() => ({}));
        sendResponse(res.ok ? { ok: true, data } : { ok: false, error: data.error || `server error ${res.status}` });
      } else if (msg.type === "latest-docs") {
        // The page's address picks a resume made for this job, when there
        // is one; the server falls back to the standard one otherwise.
        const p = msg.payload || {};
        const q = p.url ? "?" + new URLSearchParams({ url: p.url, title: p.title || "",
                                                      standard: p.standard ? "1" : "" }) : "";
        const res = await fetch(BASE + "/api/latest-docs" + q);
        sendResponse({ ok: res.ok, data: await res.json() });
      } else if (msg.type === "plan") {
        sendResponse({ ok: true, data: await post("/api/autofill/plan", msg.payload) });
      } else if (msg.type === "corrections") {
        sendResponse({ ok: true, data: await post("/api/autofill/correction", msg.payload) });
      } else if (msg.type === "answers-suggest") {
        sendResponse({ ok: true, data: await post("/api/answers/suggest", msg.payload) });
      } else if (msg.type === "extension-version") {
        // What is running, against what is on disk. A mismatch means the
        // files changed and the extension has not been reloaded.
        const res = await fetch(BASE + "/api/extension/version");
        const data = await res.json().catch(() => ({}));
        sendResponse({ ok: true, data: {
          running: chrome.runtime.getManifest().version,
          onDisk: data.version || "",
        } });
      } else if (msg.type === "applied") {
        sendResponse({ ok: true, data: await post("/api/applied", msg.payload) });
      } else if (msg.type === "save-answers") {
        sendResponse({ ok: true, data: await post("/api/answers/capture-batch", msg.payload) });
      } else if (msg.type === "answers-field") {
        sendResponse({ ok: true, data: await post("/api/answers/field", msg.payload) });
      } else if (msg.type === "answers-dictate") {
        sendResponse({ ok: true, data: await post("/api/answers/dictate", msg.payload) });
      } else if (msg.type === "answers-draft-field") {
        sendResponse({ ok: true, data: await post("/api/answers/draft-field", msg.payload) });
      } else if (msg.type === "posting-check") {
        sendResponse({ ok: true, data: await post("/api/posting/check", msg.payload) });
      } else if (msg.type === "answers-check") {
        sendResponse({ ok: true, data: await post("/api/answers/check", msg.payload) });
      } else if (msg.type === "answers-facts") {
        sendResponse({ ok: true, data: await post("/api/answers/facts", msg.payload) });
      } else if (msg.type === "answers-predraft") {
        sendResponse({ ok: true, data: await post("/api/answers/predraft", msg.payload) });
      } else if (msg.type === "answers-predraft-status") {
        sendResponse({ ok: true, data: await post("/api/answers/predraft/status", msg.payload) });
      } else if (msg.type === "answers-stories") {
        const res = await fetch(BASE + "/api/answers/stories");
        const data = await res.json().catch(() => ({}));
        sendResponse(res.ok ? { ok: true, data } : { ok: false, error: data.error || `server error ${res.status}` });
      } else if (msg.type === "answers-learn") {
        sendResponse({ ok: true, data: await post("/api/answers/learn", msg.payload) });
      } else if (msg.type === "cover-letter-doc") {
        const res = await fetch(BASE + "/api/tailor/cover-letter");
        sendResponse({ ok: res.ok, data: await res.json() });
      } else if (msg.type === "letter-get") {
        const res = await fetch(BASE + "/api/letter/" + encodeURIComponent(msg.payload.id));
        const data = await res.json().catch(() => ({}));
        sendResponse(res.ok ? { ok: true, data } : { ok: false, error: data.error });
      } else if (["letter-save", "letter-revise", "letter-approve", "letter-learn"].includes(msg.type)) {
        const path = "/api/letter/" + encodeURIComponent(msg.payload.id) + "/" + msg.type.slice(7);
        sendResponse({ ok: true, data: await post(path, msg.payload) });
      } else if (msg.type === "letter-doc") {
        const res = await fetch(BASE + "/api/letter/" + encodeURIComponent(msg.payload.id) + "/doc");
        sendResponse({ ok: res.ok, data: await res.json() });
      } else if (msg.type === "capture") {
        sendResponse({ ok: true, data: await post("/api/extension/capture", msg.payload) });
      } else {
        // A message type this worker predates: the extension was updated
        // on disk but not reloaded, so this is still the old background.
        sendResponse({ ok: false, error:
          "the extension needs a reload: open arc://extensions and hit " +
          "the refresh icon on the Verbatim card" });
      }
    } catch (e) {
      sendResponse({ ok: false, error: e.message });
    }
  })();
  return true; // keep the message channel open for the async response
});
