/* CCTV Map — front-end (Leaflet + markercluster + hls.js) */
(() => {
  const SRC = {
    medan:     { label: "Medan (ATCS Dishub)",   color: "#2563eb" },
    jogja:     { label: "Yogyakarta (Pemkot)",   color: "#d97706" },
    binamarga: { label: "Bina Marga (Nasional)", color: "#7c3aed" },
  };
  const PLAY = { hls: "HLS (analytics-ready)", resolve: "Tokenised iframe", external: "External page" };
  const MAX_TILES = 4;

  const state = { cams: [], api: false, src: new Set(), cat: new Set(), play: new Set(), q: "", markers: new Map(), tiles: [] };
  const $ = (s) => document.querySelector(s);
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
      const r = await fetch("/api/cameras", { cache: "no-store" });
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
    $("#refresh").style.display = state.api ? "" : "none";
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

  function popupHtml(c) {
    const tags = [
      `<span class="tag">${esc(SRC[c.source]?.label || c.source)}</span>`,
      c.category ? `<span class="tag">${esc(c.category)}</span>` : "",
      `<span class="tag">${esc(c.playback)}</span>`,
      c.geo_source === "approx" ? `<span class="tag approx">approx. location</span>` : "",
      c.status !== "unknown" ? `<span class="tag">${esc(c.status)}</span>` : "",
    ].join("");
    const open = c.page_url ? `<a class="btn" href="${esc(c.page_url)}" target="_blank" rel="noopener">Source ↗</a>` : "";
    return `<div class="pp"><h4>${esc(c.name)}</h4>
      <div class="m">${esc([c.area, c.city].filter((x, i, a) => x && a.indexOf(x) === i).join(" · "))}<br><code>${esc(c.id)}</code></div>
      <div class="tags">${tags}</div>
      <div class="btns"><button class="primary" data-play="${esc(c.id)}">▶ Play</button>${open}</div></div>`;
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
      m.bindPopup(() => popupHtml(c));
      m.bindTooltip(esc(c.name), { direction: "top", offset: [0, -6] });
      state.markers.set(c.id, m); ms.push(m);
    });
    cluster.addLayers(ms);
    const ul = $("#list"); ul.innerHTML = "";
    const frag = document.createDocumentFragment();
    v.slice(0, 1500).forEach((c) => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="dot" style="background:${SRC[c.source]?.color}"></span>
        <div><div class="t">${esc(c.name)}</div><div class="s">${esc(c.area || c.city)} · ${esc(c.category)}${c.lat == null ? " · <em>no coords</em>" : ""}</div></div>
        <span class="st ${esc(c.status)}" title="${esc(c.status)}"></span>`;
      li.onclick = () => locate(c);
      li.ondblclick = () => play(c);
      frag.appendChild(li);
    });
    ul.appendChild(frag);
  }

  function locate(c) {
    const m = state.markers.get(c.id);
    if (window.innerWidth < 820) $("#side").classList.remove("open");
    if (!m) { play(c); return; }
    cluster.zoomToShowLayer(m, () => m.openPopup());
  }

  // ---------- playback
  function hlsUrl(u) { return state.api ? "/hls?u=" + encodeURIComponent(u) : u; }

  async function play(c) {
    if (state.tiles.find((t) => t.cam.id === c.id)) return;
    if (state.tiles.length >= MAX_TILES) closeTile(state.tiles[0]);
    const el = document.createElement("div"); el.className = "tile";
    el.innerHTML = `<div class="cap"><span title="${esc(c.name)}">${esc(c.name)}</span>
      ${c.page_url ? `<button data-a="src">↗</button>` : ""}<button data-a="x">✕</button></div>`;
    const tile = { cam: c, el, hls: null };
    el.querySelector('[data-a="x"]').onclick = () => closeTile(tile);
    const srcBtn = el.querySelector('[data-a="src"]'); if (srcBtn) srcBtn.onclick = () => window.open(c.page_url, "_blank", "noopener");
    $("#tiles").appendChild(el); state.tiles.push(tile); layoutWall();

    const msg = (t) => { const d = document.createElement("div"); d.className = "msg"; d.innerHTML = t; el.appendChild(d); return d; };

    if (c.playback === "hls" && c.stream_url) {
      const v = document.createElement("video"); v.muted = true; v.autoplay = true; v.playsInline = true; v.controls = true;
      el.insertBefore(v, el.firstChild);
      const loading = msg("Connecting…");
      v.addEventListener("playing", () => loading.remove(), { once: true });
      if (window.Hls && Hls.isSupported()) {
        // Try through our server proxy first; if that fails (e.g. upstream blocks the VM's IP
        // or port), retry once directly from the viewer's browser.
        const attempts = state.api ? [hlsUrl(c.stream_url), c.stream_url] : [c.stream_url];
        const start = (i) => {
          const h = new Hls({ lowLatencyMode: false, liveSyncDurationCount: 3,
            manifestLoadingTimeOut: 20000, manifestLoadingMaxRetry: 2, levelLoadingTimeOut: 20000, fragLoadingTimeOut: 30000 });
          tile.hls = h; h.loadSource(attempts[i]); h.attachMedia(v);
          h.on(Hls.Events.ERROR, (_, d) => {
            if (!d.fatal) return;
            h.destroy();
            if (i + 1 < attempts.length) { loading.textContent = "Proxy failed — trying direct…"; start(i + 1); }
            else loading.innerHTML = `Stream unavailable (${esc(d.details)}).<br>The camera may be offline or blocking outside access.`;
          });
        };
        start(0);
      } else if (v.canPlayType("application/vnd.apple.mpegurl")) {
        v.src = hlsUrl(c.stream_url);
      } else loading.textContent = "HLS not supported in this browser";
    } else if (c.playback === "resolve") {
      const m = msg("Resolving fresh player URL…");
      if (!state.api) { m.innerHTML = `Needs the server (token is short-lived).<br><a style="color:#9cf" href="${esc(c.page_url)}" target="_blank">Open source page ↗</a>`; return; }
      try {
        const r = await fetch("/api/resolve/" + encodeURIComponent(c.id)); if (!r.ok) throw new Error(r.status);
        const { url } = await r.json();
        const f = document.createElement("iframe"); f.src = url; f.allow = "autoplay; fullscreen"; f.referrerPolicy = "no-referrer";
        el.insertBefore(f, el.firstChild); m.remove();
      } catch (e) { m.innerHTML = `Could not resolve player (${esc(e.message)}).<br><a style="color:#9cf" href="${esc(c.page_url)}" target="_blank">Open source page ↗</a>`; }
    } else {
      msg(`Third-party player — opens on the provider's site.<br><br><a style="color:#9cf" href="${esc(c.page_url)}" target="_blank" rel="noopener">Open ${esc(new URL(c.page_url).host)} ↗</a>`);
    }
  }

  function closeTile(t) {
    if (t.hls) t.hls.destroy();
    t.el.querySelectorAll("video").forEach((v) => { v.pause(); v.removeAttribute("src"); v.load(); });
    t.el.remove(); state.tiles = state.tiles.filter((x) => x !== t); layoutWall();
  }
  function layoutWall() {
    $("#wall").classList.toggle("hidden", state.tiles.length === 0);
    $("#tiles").classList.toggle("one", state.tiles.length === 1);
    $("#wall-hint").textContent = `${state.tiles.length}/${MAX_TILES} streams`;
  }

  // ---------- events
  map.on("popupopen", (e) => {
    const b = e.popup.getElement().querySelector("[data-play]");
    if (b) b.onclick = () => { const c = state.cams.find((x) => x.id === b.dataset.play); if (c) play(c); };
  });
  let t; $("#q").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => { state.q = e.target.value.trim(); render(); }, 150); });
  $("#wall-close").onclick = () => [...state.tiles].forEach(closeTile);
  $("#toggle-side").onclick = () => $("#side").classList.toggle("open");
  $("#refresh").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "Refreshing…";
    try { await fetch("/api/refresh", { method: "POST" }); setTimeout(() => location.reload(), 45000); }
    catch { e.target.disabled = false; e.target.textContent = "Refresh"; }
  };

  load();
})();
