// IDM Clone — background service worker (Manifest V3).
// 1. Intercepts browser downloads and hands them to the desktop app.
// 2. Watches network responses for video/audio streams and lists them per tab.
// 3. Adds "Download with IDM Clone" context menus.

const HOST = "com.idmclone.host";
const MEDIA_TYPES = /^(video\/|audio\/|application\/(vnd\.apple\.mpegurl|x-mpegurl|dash\+xml|octet-stream))/i;
const MEDIA_EXT = /\.(mp4|m4v|webm|mkv|mov|avi|flv|mp3|m4a|aac|ogg|wav|flac|m3u8|mpd|ts)(\?|#|$)/i;
const FILE_EXT = /\.(zip|rar|7z|tar|gz|bz2|xz|iso|exe|msi|apk|dmg|deb|rpm|pdf|epub|mp4|mkv|mp3|flac)(\?|#|$)/i;
const SKIP_HOSTS = /(^|\.)googlevideo\.com$|(^|\.)fbcdn\.net$|(^|\.)ttvnw\.net$/i;
const MIN_MEDIA_BYTES = 512 * 1024;

const mediaByTab = new Map(); // tabId -> Map(url -> {url, type, size, filename, pageUrl, ts})

// ---------------------------------------------------------------- settings
async function getSettings() {
  const d = await chrome.storage.local.get({ intercept: true, sniff: true, minSizeMB: 1 });
  return d;
}

// ---------------------------------------------------------------- native host
function sendToApp(msg) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendNativeMessage(HOST, msg, (resp) => {
        if (chrome.runtime.lastError) {
          resolve({ ok: false, error: chrome.runtime.lastError.message });
        } else {
          resolve(resp || { ok: false, error: "no reply" });
        }
      });
    } catch (e) {
      resolve({ ok: false, error: String(e) });
    }
  });
}

async function cookieHeaderFor(url) {
  try {
    const cookies = await chrome.cookies.getAll({ url });
    if (!cookies.length) return "";
    return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch (_e) {
    return "";
  }
}

async function addDownload({ url, filename, referrer, pageUrl, kind, tabId, mime, size }) {
  const headers = {};
  if (referrer) headers["Referer"] = referrer;
  const cookie = await cookieHeaderFor(url);
  if (cookie) headers["Cookie"] = cookie;
  headers["User-Agent"] = navigator.userAgent;
  const resp = await sendToApp({
    cmd: "add",
    url,
    filename: filename || "",
    referrer: referrer || pageUrl || "",
    page_url: pageUrl || "",
    kind: kind || "auto",
    mime: mime || "",
    size: size || 0,
    headers,
  });
  if (!resp.ok) {
    notify("IDM Clone", resp.error || "Could not send to IDM Clone");
    if (tabId !== undefined && kind === "download") {
      // Fall back to the browser download so the user doesn't lose the file.
      chrome.downloads.download({ url, filename: filename || undefined });
    }
  }
  return resp;
}

function notify(title, message) {
  if (!chrome.notifications) return;
  chrome.notifications.create({ type: "basic", iconUrl: "icons/128.png", title, message });
}

// ---------------------------------------------------------------- download interception
const handled = new Set();

chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  suggest();
  (async () => {
    const settings = await getSettings();
    if (!settings.intercept) return;
    if (!/^https?:/i.test(item.url) && !/^https?:/i.test(item.finalUrl || "")) return;
    if (item.byExtensionId) return; // our own fallback or another extension
    if (handled.has(item.id)) return;
    handled.add(item.id);
    const url = item.finalUrl || item.url;
    try {
      await chrome.downloads.cancel(item.id);
      await chrome.downloads.erase({ id: item.id });
    } catch (_e) {
      return;
    }
    await addDownload({
      url,
      filename: item.filename ? item.filename.split(/[\\/]/).pop() : "",
      referrer: item.referrer,
      pageUrl: item.referrer,
      kind: "download",
      mime: item.mime,
      size: item.totalBytes > 0 ? item.totalBytes : 0,
    });
  })();
});

// ---------------------------------------------------------------- media sniffing
function headerValue(headers, name) {
  const h = (headers || []).find((x) => x.name.toLowerCase() === name);
  return h ? h.value || "" : "";
}

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const ct = headerValue(details.responseHeaders, "content-type");
    const len = parseInt(headerValue(details.responseHeaders, "content-length") || "0", 10);
    const url = details.url;
    let host = "";
    try {
      host = new URL(url).hostname;
    } catch (_e) {
      return;
    }
    if (SKIP_HOSTS.test(host)) return; // chunked CDN streams (YouTube etc.) — use the page URL instead
    const isMedia = MEDIA_TYPES.test(ct) || MEDIA_EXT.test(url);
    if (!isMedia) return;
    const isPlaylist = /m3u8|mpd|mpegurl|dash/i.test(ct + url);
    if (!isPlaylist && len && len < MIN_MEDIA_BYTES) return;
    if (!isPlaylist && !len && !/^video\/|^audio\//i.test(ct)) return;
    getSettings().then((s) => {
      if (!s.sniff) return;
      let list = mediaByTab.get(details.tabId);
      if (!list) {
        list = new Map();
        mediaByTab.set(details.tabId, list);
      }
      const clean = url.split("#")[0];
      if (list.has(clean)) return;
      const name = clean.split("?")[0].split("/").pop() || "video";
      list.set(clean, { url: clean, type: ct.split(";")[0], size: len, filename: name, pageUrl: details.initiator || "", ts: Date.now(), playlist: isPlaylist });
      if (list.size > 30) list.delete(list.keys().next().value);
      updateBadge(details.tabId);
    });
  },
  { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other", "object"] },
  ["responseHeaders"]
);

function updateBadge(tabId) {
  const list = mediaByTab.get(tabId);
  const n = list ? list.size : 0;
  chrome.action.setBadgeText({ tabId, text: n ? String(n) : "" });
  chrome.action.setBadgeBackgroundColor({ tabId, color: "#2d7dd2" });
}

chrome.tabs.onRemoved.addListener((tabId) => mediaByTab.delete(tabId));
chrome.webNavigation?.onCommitted?.addListener((d) => {
  if (d.frameId === 0) {
    mediaByTab.delete(d.tabId);
    updateBadge(d.tabId);
  }
});
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status === "loading" && info.url) {
    mediaByTab.delete(tabId);
    updateBadge(tabId);
  }
});

// ---------------------------------------------------------------- context menus
chrome.runtime.onStartup.addListener(() => {
  sendToApp({ cmd: "hello", version: chrome.runtime.getManifest().version }).then(() => {});
});

chrome.runtime.onInstalled.addListener(() => {
  // Tell the desktop app we exist, so its setup screen can show "extension connected".
  sendToApp({ cmd: "hello", version: chrome.runtime.getManifest().version }).then(() => {});
  chrome.contextMenus.create({ id: "dl-link", title: "Download link with IDM Clone", contexts: ["link"] });
  chrome.contextMenus.create({ id: "dl-media", title: "Download with IDM Clone", contexts: ["video", "audio", "image"] });
  chrome.contextMenus.create({ id: "dl-page", title: "Download video from this page with IDM Clone", contexts: ["page", "frame"] });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const pageUrl = info.pageUrl || (tab && tab.url) || "";
  if (info.menuItemId === "dl-link" && info.linkUrl) {
    await addDownload({ url: info.linkUrl, referrer: pageUrl, pageUrl, kind: "auto" });
  } else if (info.menuItemId === "dl-media" && info.srcUrl) {
    await addDownload({ url: info.srcUrl, referrer: pageUrl, pageUrl, kind: "file" });
  } else if (info.menuItemId === "dl-page" && pageUrl) {
    await addDownload({ url: pageUrl, referrer: pageUrl, pageUrl, kind: "media" });
  }
});

// ---------------------------------------------------------------- popup API
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg.type === "list") {
      const list = mediaByTab.get(msg.tabId);
      sendResponse({ items: list ? [...list.values()].sort((a, b) => b.ts - a.ts) : [] });
    } else if (msg.type === "download") {
      sendResponse(await addDownload(msg.item));
    } else if (msg.type === "page-video") {
      sendResponse(await addDownload({ url: msg.pageUrl, referrer: msg.pageUrl, pageUrl: msg.pageUrl, kind: "media" }));
    } else if (msg.type === "video-button") {
      // From the floating button over a <video>. Direct media file -> file download; otherwise (blob:/MSE
      // players such as YouTube) hand the page to yt-dlp through the app's quality picker.
      const src = msg.src || "";
      let direct = /^https?:/i.test(src);
      try { if (direct && SKIP_HOSTS.test(new URL(src).hostname)) direct = false; } catch (_e) { direct = false; }
      if (direct) {
        sendResponse(await addDownload({ url: src, referrer: msg.pageUrl, pageUrl: msg.pageUrl, kind: "file" }));
      } else {
        sendResponse(await addDownload({ url: msg.pageUrl, referrer: msg.pageUrl, pageUrl: msg.pageUrl, kind: "media" }));
      }
    } else if (msg.type === "ping") {
      sendResponse(await sendToApp({ cmd: "host-ping" }));
    } else if (msg.type === "show-app") {
      sendResponse(await sendToApp({ cmd: "show" }));
    } else {
      sendResponse({ ok: false, error: "unknown message" });
    }
  })();
  return true;
});
