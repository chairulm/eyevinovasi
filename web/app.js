/* CCTV Map — front-end (Leaflet + markercluster + hls.js) */
(() => {
  const SRC = {
    medan:     { label: "Medan (ATCS Dishub)",   color: "#2563eb" },
    jogja:     { label: "Yogyakarta (Pemkot)",   color: "#d97706" },
    binamarga: { label: "Bina Marga (Nasional)", color: "#7c3aed" },
  };
  const PLAY = { hls: "HLS (analytics-ready)", resolve: "Tokenised iframe", external: "External page" };
  const MAX_TILES = 4;

  const state = { detail: null, cams: [], api: false, src: new Set(), cat: new Set(), play: new Set(), q: "", markers: new Map(), tiles: [] };
  const $ = (s) => document.querySelector(s);
  // same-origin API call: CSRF header on writes, back to the login page when the session ends
  async function api(url, opts = {}) {
    const r = await fetch(url, { cache: "no-store", ...opts, headers: { "X-Eye": "1", ...(opts.headers || {}) } });
    if (r.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search); throw new Error("login required"); }
    return r;
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---------- map
  const map = L.map("map", { preferCanvas: true, zoomControl: true }).setView([-2.5, 112], 5);
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  // Keyless basemaps (CARTO now requires an API key)
  const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services";
  const base = {
    "Streets (OSM)": L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap contributors" }),
    "Dark gray": L.tileLayer(`${ESRI}/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}`, { maxZoom: 16, attribution: "Tiles © Esri — Esri, HERE, Garmin, © OpenStreetMap" }),
    "Light gray": L.tileLayer(`${ESRI}/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}`, { maxZoom: 16, attribution: "Tiles © Esri — Esri, HERE, Garmin, © OpenStreetMap" }),
    "Satellite": L.tileLayer(`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`, { maxZoom: 19, attribution: "Tiles © Esri — Esri, Maxar, Earthstar Geographics" }),
  };
  const savedBase = (() => { try { return localStorage.getItem("cctv.base"); } catch { return null; } })();
  (base[savedBase] || (dark ? base["Dark gray"] : base["Streets (OSM)"])).addTo(map);
  map.on("baselayerchange", (e) => { try { localStorage.setItem("cctv.base", e.name); } catch {} });
  L.control.layers(base, null, { position: "topright" }).addTo(map);
  const cluster = L.markerClusterGroup({ chunkedLoading: true, maxClusterRadius: 45, showCoverageOnHover: false });
  map.addLayer(cluster);

  // ---------- data
  async function load() {
    let gj;
    try {
      const r = await api("/api/v1/cameras");
      if (!r.ok) throw 0;
      gj = await r.json(); state.api = true;
    } catch {
      gj = await (await fetch("cameras.geojson")).json(); state.api = false;
    }
    state.cams = gj.features.map((f) => f.properties);
    initFilters();
    render();
    const pts = state.cams.filter((c) => c.lat != null).map((c) => [c.lat, c.lon]);
    if (pts.length) map.fitBounds(pts, { padding: [30, 30] });
    await loadMe();
    const h = location.hash.match(/^#cam=(.+)$/);           // deep link: /#cam=<id>
    const hc = h && state.cams.find((c) => c.id === decodeURIComponent(h[1]));
    if (hc) { if (hc.lat != null) map.setView([hc.lat, hc.lon], 16); openDetail(hc, "info"); }
  }

  async function loadMe() {
    $("#refresh").style.display = "none";
    if (!state.api) return;
    try {
      const me = await (await api("/api/v1/auth/me")).json();
      state.me = me;
      const admin = me.role === "admin";
      $("#refresh").style.display = admin ? "" : "none";
      if (!me.auth) return;
      const u = $("#user");
      u.innerHTML = `<span class="who" title="${esc(me.role)}">${esc(me.username)}</span>
        ${admin ? `<a class="btn mini" href="/admin">Admin</a>` : ""}
        <button class="mini" id="logout">Log out</button>`;
      u.hidden = false;
      $("#logout").onclick = async () => { await api("/api/v1/auth/logout", { method: "POST" }).catch(() => {}); location.href = "/login"; };
    } catch {}
  }

  function counts(key) {
    const m = {}; state.cams.forEach((c) => (m[c[key]] = (m[c[key]] || 0) + 1)); return m;
  }

  function chip(container, set, value, label, n, color) {
    const b = document.createElement("button");
    b.className = "chip"; b.setAttribute("aria-pressed", "true");
    b.innerHTML = `${color ? `<i style="background:${color}"></i>` : ""}${esc(label)} <small>${n}</small>`;
    set.add(value);
    b.onclick = (e) => {
      if (e.altKey || e.metaKey) { // solo
        set.clear(); set.add(value);
        container.querySelectorAll(".chip").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
      } else {
        set.has(value) ? set.delete(value) : set.add(value);
        b.setAttribute("aria-pressed", set.has(value));
      }
      render();
    };
    container.appendChild(b);
  }

  function initFilters() {
    const sc = counts("source"), pc = counts("playback");
    $("#f-source").innerHTML = ""; $("#f-cat").innerHTML = ""; $("#f-play").innerHTML = "";
    Object.keys(SRC).filter((k) => sc[k]).forEach((k) => chip($("#f-source"), state.src, k, SRC[k].label, sc[k], SRC[k].color));
    const cc = {}; state.cams.forEach((c) => { const k = c.category || "–"; cc[k] = (cc[k] || 0) + 1; });
    Object.entries(cc).sort((a, b) => b[1] - a[1]).forEach(([k, n]) => chip($("#f-cat"), state.cat, k, k, n));
    Object.keys(PLAY).filter((k) => pc[k]).forEach((k) => chip($("#f-play"), state.play, k, PLAY[k], pc[k]));
  }

  function visible() {
    const q = state.q.toLowerCase();
    return state.cams.filter((c) =>
      state.src.has(c.source) && state.cat.has(c.category || "–") && state.play.has(c.playback) &&
      (!q || `${c.name} ${c.area} ${c.city} ${c.category} ${c.id}`.toLowerCase().includes(q)));
  }

  function stats(v) {
    const on = v.filter((c) => c.status === "online").length;
    const ar = v.filter((c) => c.analytics_ready).length;
    const mapped = v.filter((c) => c.lat != null).length;
    $("#stats").innerHTML = `
      <div class="stat"><b>${v.length}</b><span>cameras</span></div>
      <div class="stat"><b>${ar}</b><span>analytics-ready</span></div>
      <div class="stat"><b>${on || "–"}</b><span>online (last check)</span></div>`;
    $("#count").textContent = `${v.length} (${mapped} mapped)`;
  }

  function render() {
    const v = visible();
    stats(v);
    cluster.clearLayers(); state.markers.clear();
    const ms = [];
    v.forEach((c) => {
      if (c.lat == null) return;
      const m = L.circleMarker([c.lat, c.lon], {
        radius: 6, weight: c.geo_source === "approx" ? 2 : 1.5, dashArray: c.geo_source === "approx" ? "2 2" : null,
        color: c.status === "offline" ? "#dc2626" : "#fff", fillColor: SRC[c.source]?.color || "#555", fillOpacity: 0.95,
      });
      m.on("click", () => openDetail(c));
      m.bindTooltip(esc(c.name), { direction: "top", offset: [0, -6] });
      state.markers.set(c.id, m); ms.push(m);
    });
    cluster.addLayers(ms);
    const ul = $("#list"); ul.innerHTML = "";
    const frag = document.createDocumentFragment();
    v.slice(0, 1500).forEach((c) => {
      const li = document.createElement("li");
      li.dataset.id = c.id;
      li.className = state.detail?.cam.id === c.id ? "sel" : "";
      li.innerHTML = `<span class="dot" style="background:${SRC[c.source]?.color}"></span>
        <div><div class="t">${esc(c.name)}</div><div class="s">${esc(c.area || c.city)} · ${esc(c.category)}${c.lat == null ? " · <em>no coords</em>" : ""}</div></div>
        <span class="st ${esc(c.status)}" title="${esc(c.status)}"></span>`;
      li.onclick = () => locate(c);
      li.ondblclick = () => addToWall(c);
      frag.appendChild(li);
    });
    ul.appendChild(frag);
  }

  function locate(c) {
    const m = state.markers.get(c.id);
    if (window.innerWidth < 820) $("#side").classList.remove("open");
    if (m) cluster.zoomToShowLayer(m, () => {});
    openDetail(c);
  }

  // ---------- player (shared by the detail panel and the video wall)
  // Order of sources for HLS cameras:
  //   1. our re-stream (/live/...): the agency is pulled once for all viewers
  //   2. our HLS proxy (/hls?u=...): per-viewer pull through the VM
  //   3. direct from the viewer's browser (when the VM can't reach the upstream)
  function hlsAttempts(c) {
    const out = [];
    if (state.api && c.live_url) out.push({ url: c.live_url, label: "re-stream" });
    if (state.api) out.push({ url: "/hls?u=" + encodeURIComponent(c.stream_url), label: "proxy" });
    out.push({ url: c.stream_url, label: "direct" });
    return out;
  }

  /** Mount a player for camera c into container el. Returns { destroy() }. */
  function mountPlayer(el, c, onVia) {
    const handle = { hls: null, destroy() {} };
    const msg = (t) => { const d = document.createElement("div"); d.className = "msg"; d.innerHTML = t; el.appendChild(d); return d; };
    const srcLink = c.page_url ? `<br><a style="color:#9cf" href="${esc(c.page_url)}" target="_blank" rel="noopener">Open source page ↗</a>` : "";

    if (c.playback === "hls" && c.stream_url) {
      const v = document.createElement("video"); v.muted = true; v.autoplay = true; v.playsInline = true; v.controls = true;
      el.insertBefore(v, el.firstChild);
      const loading = msg("Connecting…");
      v.addEventListener("playing", () => loading.remove(), { once: true });
      const attempts = hlsAttempts(c);
      if (window.Hls && Hls.isSupported()) {
        const start = (i) => {
          const h = new Hls({ lowLatencyMode: false, liveSyncDurationCount: 3,
            manifestLoadingTimeOut: 25000, manifestLoadingMaxRetry: 1, levelLoadingTimeOut: 20000, fragLoadingTimeOut: 30000 });
          handle.hls = h; h.loadSource(attempts[i].url); h.attachMedia(v);
          onVia && onVia(attempts[i].label);
          h.on(Hls.Events.ERROR, (_, d) => {
            if (!d.fatal) return;
            h.destroy();
            if (i + 1 < attempts.length) { loading.textContent = `${attempts[i].label} failed — trying ${attempts[i + 1].label}…`; start(i + 1); }
            else loading.innerHTML = `Stream unavailable (${esc(d.details)}).<br>The camera may be offline or blocking outside access.${srcLink}`;
          });
        };
        start(0);
      } else if (v.canPlayType("application/vnd.apple.mpegurl")) {
        v.src = attempts[0].url; onVia && onVia(attempts[0].label);
      } else loading.textContent = "HLS not supported in this browser";
      handle.destroy = () => { handle.hls && handle.hls.destroy(); v.pause(); v.removeAttribute("src"); v.load(); };
    } else if (c.playback === "resolve") {
      const m = msg("Resolving fresh player URL…");
      if (!state.api) { m.innerHTML = `Needs the server (token is short-lived).${srcLink}`; return handle; }
      api("/api/v1/resolve/" + encodeURIComponent(c.id))
        .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(({ url }) => {
          const f = document.createElement("iframe"); f.src = url; f.allow = "autoplay; fullscreen"; f.referrerPolicy = "no-referrer";
          el.insertBefore(f, el.firstChild); m.remove(); onVia && onVia("provider player");
        })
        .catch((e) => { m.innerHTML = `Could not resolve player (${esc(e.message)}).${srcLink}`; });
    } else {
      msg(`Third-party player — opens on the provider's site.${srcLink}`);
    }
    return handle;
  }

  // ---------- video wall
  function addToWall(c) {
    if (state.tiles.find((t) => t.cam.id === c.id)) return;
    if (state.tiles.length >= MAX_TILES) closeTile(state.tiles[0]);
    const el = document.createElement("div"); el.className = "tile";
    el.innerHTML = `<div class="cap"><span title="${esc(c.name)}">${esc(c.name)}</span>
      <button data-a="info" title="Details">ⓘ</button><button data-a="x" title="Close">✕</button></div>`;
    const tile = { cam: c, el, player: null };
    el.querySelector('[data-a="x"]').onclick = () => closeTile(tile);
    el.querySelector('[data-a="info"]').onclick = () => openDetail(c, "info");
    $("#tiles").appendChild(el); state.tiles.push(tile); layoutWall();
    tile.player = mountPlayer(el, c);
  }
  function closeTile(t) {
    t.player && t.player.destroy();
    t.el.remove(); state.tiles = state.tiles.filter((x) => x !== t); layoutWall();
  }
  function layoutWall() {
    $("#wall").classList.toggle("hidden", state.tiles.length === 0);
    $("#tiles").classList.toggle("one", state.tiles.length === 1);
    $("#wall-hint").textContent = `${state.tiles.length}/${MAX_TILES} streams`;
    document.body.classList.toggle("wall-open", state.tiles.length > 0);
  }

  // ---------- detail panel
  const fmtTime = (s) => s ? new Date(s).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "–";

  function closeDetail() {
    if (!state.detail) return;
    state.detail.player && state.detail.player.destroy();
    state.detail = null;
    $("#detail").classList.add("hidden");
    document.body.classList.remove("detail-open");
    document.querySelectorAll("#list li.sel").forEach((x) => x.classList.remove("sel"));
  }

  function openDetail(c, tab = "live") {
    if (state.detail && state.detail.cam.id === c.id) { showTab(tab); return; }
    closeDetail();
    state.detail = { cam: c, player: null, data: null, tab: null };
    const d = $("#detail");
    d.classList.remove("hidden"); document.body.classList.add("detail-open");
    document.querySelectorAll("#list li").forEach((x) => x.classList.toggle("sel", x.dataset.id === c.id));
    $("#d-title").textContent = c.name;
    $("#d-sub").innerHTML = `<i style="background:${SRC[c.source]?.color}"></i>${esc(SRC[c.source]?.label || c.source)} · ${esc(c.area || c.city || "")}`;
    $("#d-status").className = "pill " + c.status; $("#d-status").textContent = c.status;
    showTab(tab);
    if (state.api) {
      api("/api/v1/cameras/" + encodeURIComponent(c.id) + "?history=30").then((r) => r.ok ? r.json() : null).then((data) => {
        if (!state.detail || state.detail.cam.id !== c.id || !data) return;
        state.detail.data = data;
        if (state.detail.tab === "info") renderInfo();
        if (state.detail.tab === "history") renderHistory();
        renderViewers();
      }).catch(() => {});
    }
  }

  function showTab(tab) {
    const st = state.detail; if (!st) return;
    st.tab = tab;
    document.querySelectorAll("#d-tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === tab));
    if (tab !== "live" && st.player) { st.player.destroy(); st.player = null; }
    const body = $("#d-body"); body.innerHTML = "";
    if (tab === "live") renderLive();
    else if (tab === "history") renderHistory();
    else renderInfo();
  }

  function renderLive() {
    const c = state.detail.cam, body = $("#d-body");
    body.innerHTML = `<div class="tile big"></div>
      <div class="d-row"><span class="hint" id="d-via"></span><span class="hint" id="d-viewers"></span></div>
      <div class="d-actions">
        <button class="primary" id="d-wall">⊞ Add to video wall</button>
        ${c.page_url ? `<a class="btn" href="${esc(c.page_url)}" target="_blank" rel="noopener">Source ↗</a>` : ""}
      </div>`;
    state.detail.player = mountPlayer(body.querySelector(".tile"), c, (via) => { const e = $("#d-via"); if (e) e.textContent = "via " + via; });
    $("#d-wall").onclick = () => addToWall(c);
    renderViewers();
  }

  function renderViewers() {
    const r = state.detail?.data?.restream, e = $("#d-viewers");
    if (!e || !r || r.viewers == null) return;
    e.textContent = r.active ? `${r.viewers} watching via re-stream` : "re-stream idle";
  }

  function renderHistory() {
    const st = state.detail, body = $("#d-body");
    if (!state.api) { body.innerHTML = `<p class="hint">History needs the server database.</p>`; return; }
    body.innerHTML = `<div class="hint">Loading…</div>`;
    api("/api/v1/cameras/" + encodeURIComponent(st.cam.id) + "/uptime?days=7").then((r) => r.json()).then((u) => {
      if (!state.detail || state.detail.cam.id !== st.cam.id || state.detail.tab !== "history") return;
      const t0 = new Date(u.window_start).getTime(), t1 = Date.now(), span = t1 - t0;
      const bars = u.segments.map((s) => {
        const a = new Date(s.from).getTime(), b = new Date(s.to).getTime();
        return `<span class="seg ${esc(s.status)}" style="left:${(100 * (a - t0) / span).toFixed(2)}%;width:${(100 * (b - a) / span).toFixed(2)}%" title="${esc(s.status)}: ${fmtTime(s.from)} → ${fmtTime(s.to)}"></span>`;
      }).join("");
      const log = (st.data?.status_log || []).slice(0, 15).map((x) =>
        `<li><span class="st ${esc(x.status)}"></span>${esc(x.status)}<span class="hint">${fmtTime(x.at)}</span></li>`).join("");
      body.innerHTML = `
        <div class="kpi"><b>${u.online_pct == null ? "–" : u.online_pct + "%"}</b><span>online, last ${u.days} days${u.known_hours < u.days * 24 ? ` (tracked ${u.known_hours} h)` : ""}</span></div>
        <div class="timeline" aria-label="Online timeline">${bars || ""}</div>
        <div class="tl-axis"><span>${u.days} days ago</span><span>now</span></div>
        <div class="legend"><span><i class="st online"></i>online</span><span><i class="st offline"></i>offline</span><span><i class="st unknown"></i>not tracked</span></div>
        <h5>Recent changes</h5>
        <ul class="changes">${log || `<li class="hint">No changes recorded yet — checks run every 30 min.</li>`}</ul>`;
    }).catch(() => { body.innerHTML = `<p class="hint">Could not load history.</p>`; });
  }

  function renderInfo() {
    const c = { ...state.detail.cam, ...(state.detail.data || {}) }, body = $("#d-body");
    const row = (k, v) => v == null || v === "" ? "" : `<tr><th>${k}</th><td>${v}</td></tr>`;
    const link = (u) => u ? `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(new URL(u, location.href).host)} ↗</a>` : "";
    body.innerHTML = `<table class="info">
      ${row("ID", `<code>${esc(c.id)}</code> <button class="mini" id="d-copy">copy</button>`)}
      ${row("Source", esc(SRC[c.source]?.label || c.source))}
      ${row("Category", esc(c.meta?.category_label || c.category))}
      ${row("Area", esc([c.area, c.city].filter((x, i, a) => x && a.indexOf(x) === i).join(" · ")))}
      ${row("Location", c.lat != null ? `${c.lat.toFixed(5)}, ${c.lon.toFixed(5)}${c.geo_source === "approx" ? ' <span class="tag approx">approx.</span>' : c.geo_source === "manual" ? ' <span class="tag manual">corrected</span>' : ""}` : "unknown")}
      ${row("Playback", esc(c.playback) + (c.analytics_ready ? " · analytics-ready" : ""))}
      ${row("Status", `${esc(c.status)} · checked ${fmtTime(c.last_checked)}`)}
      ${row("First seen", fmtTime(c.first_seen))}
      ${row("Last seen", fmtTime(c.last_seen))}
      ${row("Source page", link(c.page_url))}
      ${c.overridden ? row("Admin note", esc(c.override_note || "") + `<span class="hint"> — ${esc(c.override_by || "")}, ${fmtTime(c.override_at)}</span>`) : ""}
    </table>
    ${c.lat != null ? `<div class="d-actions"><button id="d-zoom">⌖ Zoom to camera</button>
      <a class="btn" target="_blank" rel="noopener" href="https://www.google.com/maps?q=${c.lat},${c.lon}">Google Maps ↗</a></div>` : ""}
    ${state.me?.role === "admin" && state.me?.auth ? `<div class="d-actions"><a class="btn" href="/admin#cameras=${encodeURIComponent(c.id)}">✎ Edit camera (admin)</a></div>` : ""}`;
    const cp = $("#d-copy"); if (cp) cp.onclick = () => navigator.clipboard?.writeText(c.id).then(() => (cp.textContent = "copied"));
    const z = $("#d-zoom"); if (z) z.onclick = () => map.setView([c.lat, c.lon], 17);
  }

  // ---------- events
  let t; $("#q").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => { state.q = e.target.value.trim(); render(); }, 150); });
  $("#wall-close").onclick = () => [...state.tiles].forEach(closeTile);
  $("#toggle-side").onclick = () => $("#side").classList.toggle("open");
  $("#d-close").onclick = closeDetail;
  document.querySelectorAll("#d-tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDetail(); });
  $("#refresh").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "Refreshing…";
    try { await api("/api/v1/refresh", { method: "POST" }); setTimeout(() => location.reload(), 45000); }
    catch { e.target.disabled = false; e.target.textContent = "Refresh"; }
  };

  load();
})();
