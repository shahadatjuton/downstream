const $ = (id) => document.getElementById(id);

function fmtSize(n) {
  if (!n) return "";
  const units = ["B", "KB", "MB", "GB"];
  let v = n, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i >= 2 ? 1 : 0)} ${units[i]}`;
}

function send(msg) {
  return new Promise((resolve) => chrome.runtime.sendMessage(msg, resolve));
}

async function currentTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

function setMsg(text, ok) {
  const el = $("msg");
  el.textContent = text;
  el.style.color = ok ? "#2e9e5b" : "#d64545";
}

async function refreshList(tab) {
  const { items } = await send({ type: "list", tabId: tab.id });
  const list = $("list");
  list.innerHTML = "";
  $("media-title").textContent = items.length ? `Media detected on this tab (${items.length})` : "No media streams detected on this tab yet. Play the video, then reopen this popup.";
  for (const item of items) {
    const row = document.createElement("div");
    row.className = "item";
    const name = document.createElement("div");
    name.className = "name";
    name.title = item.url;
    name.textContent = item.filename;
    const meta = document.createElement("div");
    meta.className = "muted meta";
    meta.textContent = [item.playlist ? "stream" : item.type.replace(/^(video|audio)\//, "$1 "), fmtSize(item.size)].filter(Boolean).join(" · ");
    const btn = document.createElement("button");
    btn.textContent = "Download";
    btn.onclick = async () => {
      btn.disabled = true;
      const kind = item.playlist ? "media" : "file";
      const resp = await send({ type: "download", item: { ...item, kind, referrer: tab.url, pageUrl: tab.url } });
      setMsg(resp.ok ? "Sent to IDM Clone" : resp.error || "Failed", resp.ok);
      btn.disabled = false;
    };
    row.append(name, meta, btn);
    list.appendChild(row);
  }
}

(async () => {
  const tab = await currentTab();
  const settings = await chrome.storage.local.get({ intercept: true, sniff: true });
  $("intercept").checked = settings.intercept;
  $("sniff").checked = settings.sniff;
  $("intercept").onchange = (e) => chrome.storage.local.set({ intercept: e.target.checked });
  $("sniff").onchange = (e) => chrome.storage.local.set({ sniff: e.target.checked });

  const pageOk = tab && /^https?:/i.test(tab.url || "");
  $("page-video").disabled = !pageOk;
  $("page-video").onclick = async () => {
    $("page-video").disabled = true;
    setMsg("Asking IDM Clone to analyse the page…", true);
    const resp = await send({ type: "page-video", pageUrl: tab.url });
    setMsg(resp.ok ? "Sent to IDM Clone — pick the quality there." : resp.error || "Failed", resp.ok);
    $("page-video").disabled = false;
  };
  $("open").onclick = () => send({ type: "show-app" });

  if (pageOk) await refreshList(tab);

  const ping = await send({ type: "ping" });
  const st = $("status");
  if (ping && ping.ok) {
    st.textContent = ping.app_running ? "connected" : "app will start on demand";
  } else {
    st.textContent = "host not installed";
    st.className = "status off";
    st.title = (ping && ping.error) || "";
    setMsg("Native host not found. Run register-browser.ps1 (see README).", false);
  }
})();
