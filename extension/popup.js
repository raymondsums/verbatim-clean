const $ = id => document.getElementById(id);
const status = $("status");
// The server address lives in chrome.storage.local as "serverUrl", the
// same key background.js and content.js read. Default: the local server.
const DEFAULT_SERVER = "http://localhost:5053";

// ---------- talking to the page ----------

// The form is often not in the top frame: careers pages embed Greenhouse
// and Lever in an iframe. Inject everywhere we are allowed to, ask each
// frame how much of a form it holds, and talk to the one that holds it.
async function pickFrame(tabId) {
  let injected;
  try {
    injected = await chrome.scripting.executeScript({
      target: { tabId, allFrames: true }, files: ["content.js"],
    });
  } catch (e) {
    // Cross-origin frames can be out of reach; the top frame never is.
    injected = await chrome.scripting.executeScript({
      target: { tabId }, files: ["content.js"],
    });
  }
  const ids = [...new Set(injected.map(r => r.frameId ?? 0))];
  if (ids.length < 2) return { frameId: ids[0] ?? 0, probe: null };

  const probes = await Promise.all(ids.map(async id => {
    try {
      const r = await chrome.tabs.sendMessage(
        tabId, { type: "probe-page" }, { frameId: id });
      return { id, data: r, weight: (r && r.weight) || 0 };
    } catch (e) {
      return { id, data: null, weight: 0 };
    }
  }));
  probes.sort((a, b) => b.weight - a.weight);
  return probes[0].weight > 0
    ? { frameId: probes[0].id, probe: probes[0].data }
    : { frameId: 0, probe: null };
}

async function withContentScript(action) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) throw new Error("no active tab");
  const { frameId } = await pickFrame(tab.id);
  let r;
  try {
    // frameChosen tells the frame it was picked on purpose, so it answers
    // even when it has nothing, rather than everyone staying silent.
    r = await chrome.tabs.sendMessage(
      tab.id, { type: action, frameChosen: true }, { frameId });
  } catch (e) {
    // No listener in that frame at all. Fall back to asking the whole tab,
    // where frames without a form stay quiet and let the right one answer.
    r = await chrome.tabs.sendMessage(tab.id, { type: action })
      .catch(() => null);
  }
  if (!r) {
    throw new Error("nothing on this page answered. If the form is in a "
      + "frame, reload the tab and try again.");
  }
  return r;
}

// ---------- server state ----------

function say(text, tone) {
  status.textContent = text;
  status.classList.toggle("bad", tone === "bad");
}

async function refreshStatus() {
  let r;
  try {
    r = await chrome.runtime.sendMessage({ type: "tailor-status" });
  } catch (e) {
    r = null;
  }
  if (!r || !r.ok) {
    $("lamp").dataset.state = "off";
    $("lamp").title = "Verbatim server is not answering";
    return;
  }
  $("lamp").dataset.state = r.data.status === "running" ? "working" : "on";
  $("lamp").title = r.data.status === "running"
    ? "Tailoring now" : "Verbatim server is up";
}

// ---------- reloading itself ----------

// Editing the extension's files does nothing until the extension is
// reloaded, and the reload icon lives on a page whose URL you have to type
// for. chrome.runtime.reload() restarts it from disk with no permissions
// and no page visit, so the popup offers it the moment it notices the
// files on disk are newer than what is running.
async function checkForUpdate() {
  let r;
  try {
    r = await chrome.runtime.sendMessage({ type: "extension-version" });
  } catch (e) {
    return;
  }
  // A worker too old to know this message is itself the proof: it is
  // running code from before this check existed.
  const stale = !r || !r.ok
    || (r.data.onDisk && r.data.onDisk !== r.data.running);
  if (!stale) return;
  $("update").style.display = "block";
  $("update-versions").textContent = r && r.ok && r.data.onDisk
    ? `${r.data.running} to ${r.data.onDisk}`
    : "reload to update";
}

$("do-reload").addEventListener("click", async () => {
  // Reload the tab first: it is still holding the old content script, and
  // once the extension restarts nothing can reach in to replace it.
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab && tab.id) await chrome.tabs.reload(tab.id);
  } catch (e) {
    // A tab we cannot touch is not a reason to skip the reload.
  }
  chrome.runtime.reload();  // closes this popup; reopen it after
});

// ---------- actions ----------
//
// Since 0.5.0 the fill, the resume and cover letter, the written questions
// and the have-you-been-here check all live in the panel on the page. The
// popup is its switch: open it now, or have it open by itself on forms.

$("open-panel").addEventListener("click", async () => {
  $("open-panel").disabled = true;
  try {
    const r = await withContentScript("open-panel");
    if (r.error) throw new Error(r.error);
    window.close();
  } catch (e) {
    say(`Could not open it here: ${e.message}`, "bad");
  }
  $("open-panel").disabled = false;
});

// Any page, not only application forms: a posting on LinkedIn or a
// company's own site goes on the board too. The page's text goes along for
// sites the server cannot read on its own.
$("add-job").addEventListener("click", async () => {
  $("add-job").disabled = true;
  say("Reading the posting. This takes a few seconds.");
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    let text = "";
    try {
      const [res] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => (document.body && document.body.innerText || "").slice(0, 20000),
      });
      text = (res && res.result) || "";
    } catch (e) { /* a page extensions cannot read; the server tries the link */ }
    const r = await chrome.runtime.sendMessage({ type: "watch-add", payload: { url: tab.url, text } });
    if (!r || !r.ok) throw new Error(r ? r.error : "no answer");
    const j = r.data.job || {};
    const what = [j.title, j.company].filter(Boolean).join(" at ");
    say(r.data.existed ? `Already on the board: ${what}` : `Added to the job board: ${what}`);
  } catch (e) {
    say(`Could not add it: ${e.message}`, "bad");
  }
  $("add-job").disabled = false;
});

async function readAutoOpen() {
  const o = await chrome.storage.local.get("autoOpen");
  const on = "autoOpen" in o ? !!o.autoOpen : true;
  $("auto-open").setAttribute("aria-checked", String(on));
}

$("auto-open").addEventListener("click", async () => {
  const on = $("auto-open").getAttribute("aria-checked") !== "true";
  $("auto-open").setAttribute("aria-checked", String(on));
  await chrome.storage.local.set({ autoOpen: on });
});

// The same theme as the page and the panel: this browser's copy first,
// then the server's setting.
async function applyTheme() {
  const set = pref => {
    const dark = pref === "dark" || (pref !== "light" && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
  };
  const o = await chrome.storage.local.get("theme");
  set(o.theme || "system");
  try {
    const r = await chrome.runtime.sendMessage({ type: "ui-theme" });
    if (r && r.ok) { set(r.data.theme); chrome.storage.local.set({ theme: r.data.theme }); }
  } catch (e) { /* the copy stands */ }
}

// ---------- settings: the server address ----------

function cleanUrl(raw) {
  let v = (raw || "").trim().replace(/\/+$/, "");
  if (v && !/^https?:\/\//i.test(v)) v = "http://" + v;
  return v;
}

async function readServer() {
  const o = await chrome.storage.local.get("serverUrl");
  const url = o.serverUrl || DEFAULT_SERVER;
  $("server-url").value = url;
  $("open-app").href = url;
}

function serverSays(text, bad) {
  $("server-result").textContent = text;
  $("server-result").classList.toggle("bad", !!bad);
}

$("server-save").addEventListener("click", async () => {
  const url = cleanUrl($("server-url").value) || DEFAULT_SERVER;
  let origin;
  try {
    origin = new URL(url).origin;
  } catch (e) {
    serverSays("That is not a web address.", true);
    return;
  }
  // localhost is allowed by the manifest. Anywhere else needs the
  // browser's permission once, asked for here while the click counts.
  const local = /^https?:\/\/(localhost|127\.0\.0\.1)(:|$)/i.test(origin);
  if (!local) {
    const granted = await chrome.permissions.request({ origins: [origin + "/*"] })
      .catch(() => false);
    if (!granted) {
      serverSays("Chrome did not allow that address.", true);
      return;
    }
  }
  await chrome.storage.local.set({ serverUrl: url });
  $("server-url").value = url;
  $("open-app").href = url;
  serverSays("Saved. Testing...");
  try {
    const res = await fetch(url + "/api/health", { cache: "no-store" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(`answered ${res.status}`);
    serverSays("Saved. The server is answering.");
  } catch (e) {
    serverSays("Saved, but the server is not answering there. Is it running?", true);
  }
  refreshStatus();
});

readServer();
applyTheme();
readAutoOpen();
checkForUpdate();
refreshStatus();
setInterval(refreshStatus, 3000);
