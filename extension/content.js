// IDM Clone — floating "Download" button over <video> elements (IDM-style panel).
// Runs in every frame. Finds the video under the mouse pointer (works even when a site's
// own controls cover the video) and shows a small button in its top-right corner.

(() => {
  if (window.__idmCloneOverlay) return;
  window.__idmCloneOverlay = true;

  const MIN_W = 200, MIN_H = 120;
  let enabled = true;
  let host = null, shadow = null, btn = null, label = null;
  let current = null, hideTimer = null, lastMove = 0;

  chrome.storage.local.get({ overlay: true }, (d) => { enabled = d.overlay !== false; if (!enabled) hide(); });
  chrome.storage.onChanged.addListener((changes) => {
    if (changes.overlay) { enabled = changes.overlay.newValue !== false; if (!enabled) hide(); }
  });

  function build() {
    host = document.createElement("div");
    host.setAttribute("data-idm-clone", "overlay");
    host.style.cssText = "position:fixed;z-index:2147483647;top:0;left:0;display:none;pointer-events:none;";
    shadow = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = `
      .panel { pointer-events:auto; display:flex; align-items:center; gap:8px; font:600 13px/1 system-ui,Segoe UI,sans-serif;
               background:rgba(20,24,30,.92); color:#fff; border:1px solid rgba(255,255,255,.18); border-radius:8px;
               padding:7px 10px 7px 8px; box-shadow:0 4px 16px rgba(0,0,0,.35); cursor:pointer; user-select:none;
               transition:background .12s, transform .12s; }
      .panel:hover { background:#2d7dd2; border-color:#2d7dd2; transform:translateY(-1px); }
      .panel:active { transform:translateY(0); }
      .ic { width:16px; height:16px; }
      .close { pointer-events:auto; margin-left:4px; opacity:.6; font-size:12px; padding:0 2px; }
      .close:hover { opacity:1; }
      .wrap { display:flex; align-items:center; gap:4px; }
      .sent { background:#2e9e5b !important; border-color:#2e9e5b !important; }
    `;
    shadow.appendChild(style);
    const wrap = document.createElement("div");
    wrap.className = "wrap";
    btn = document.createElement("div");
    btn.className = "panel";
    btn.title = "Download this video with IDM Clone";
    btn.innerHTML = `<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 3v12"/><path d="M6 10l6 6 6-6"/><path d="M4 21h16"/></svg><span>Download</span>`;
    label = btn.querySelector("span");
    btn.addEventListener("click", onClick);
    btn.addEventListener("mouseenter", () => clearTimeout(hideTimer));
    btn.addEventListener("mouseleave", scheduleHide);
    const close = document.createElement("div");
    close.className = "panel close";
    close.textContent = "✕";
    close.title = "Hide for this video";
    close.addEventListener("click", (e) => { e.stopPropagation(); if (current) current.__idmDismissed = true; hide(); });
    wrap.appendChild(btn);
    wrap.appendChild(close);
    shadow.appendChild(wrap);
    (document.body || document.documentElement).appendChild(host);
  }

  function videoAt(x, y) {
    const vids = document.getElementsByTagName("video");
    let best = null, bestArea = 0;
    for (const v of vids) {
      if (v.__idmDismissed) continue;
      const r = v.getBoundingClientRect();
      if (r.width < MIN_W || r.height < MIN_H) continue;
      if (x < r.left || x > r.right || y < r.top || y > r.bottom) continue;
      const area = r.width * r.height;
      if (area > bestArea) { best = v; bestArea = area; }
    }
    return best;
  }

  function place(v) {
    const r = v.getBoundingClientRect();
    host.style.left = Math.max(0, r.right - 150) + "px";
    host.style.top = Math.max(0, r.top + 12) + "px";
    host.style.display = "block";
  }

  function show(v) {
    if (!host) build();
    clearTimeout(hideTimer);
    if (current !== v) { current = v; label.textContent = "Download"; btn.classList.remove("sent"); }
    place(v);
  }

  function hide() { if (host) host.style.display = "none"; current = null; }
  function scheduleHide() { clearTimeout(hideTimer); hideTimer = setTimeout(hide, 900); }

  document.addEventListener("mousemove", (e) => {
    if (!enabled) return;
    const now = Date.now();
    if (now - lastMove < 80) return;
    lastMove = now;
    const v = videoAt(e.clientX, e.clientY);
    if (v) show(v);
    else if (current && !isOverPanel(e)) scheduleHide();
  }, { passive: true, capture: true });

  function isOverPanel(e) {
    if (!host || host.style.display === "none") return false;
    const r = host.getBoundingClientRect();
    return e.clientX >= r.left - 8 && e.clientX <= r.right + 8 && e.clientY >= r.top - 8 && e.clientY <= r.bottom + 8;
  }

  window.addEventListener("scroll", () => { if (current) place(current); }, { passive: true, capture: true });
  window.addEventListener("resize", () => { if (current) place(current); });
  document.addEventListener("fullscreenchange", hide);

  function directSource(v) {
    const candidates = [v.currentSrc, v.src];
    for (const s of v.querySelectorAll("source")) candidates.push(s.src);
    for (const c of candidates) {
      if (c && /^https?:/i.test(c)) return c;
    }
    return "";
  }

  function onClick(e) {
    e.stopPropagation();
    if (!current) return;
    const src = directSource(current);
    const pageUrl = location.href;
    label.textContent = "Sending…";
    chrome.runtime.sendMessage(
      { type: "video-button", src, pageUrl, title: document.title, frame: window !== window.top },
      (resp) => {
        if (chrome.runtime.lastError || !resp || !resp.ok) {
          label.textContent = "Failed";
          btn.title = (resp && resp.error) || (chrome.runtime.lastError && chrome.runtime.lastError.message) || "Could not reach IDM Clone";
        } else {
          label.textContent = "Sent to IDM Clone";
          btn.classList.add("sent");
        }
        setTimeout(() => { if (label) { label.textContent = "Download"; btn.classList.remove("sent"); btn.title = "Download this video with IDM Clone"; } }, 2500);
      }
    );
  }
})();
