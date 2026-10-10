/* Admin page — users, collector & health, camera overrides, audit log */
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (t) => t ? new Date(t).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "–";
  const ago = (t) => {
    if (!t) return "–";
    const s = (Date.now() - new Date(t).getTime()) / 1000;
    if (s < 90) return "just now"; if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 129600) return Math.round(s / 3600) + " h ago"; return Math.round(s / 86400) + " d ago";
  };

  async function api(url, opts = {}) {
    const r = await fetch(url, { cache: "no-store", ...opts,
      headers: { "X-Eye": "1", ...(opts.body ? { "Content-Type": "application/json" } : {}), ...(opts.headers || {}) } });
    if (r.status === 401) { location.href = "/login?next=" + encodeURIComponent("/admin" + location.hash); throw new Error("login required"); }
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : (d.detail ? JSON.stringify(d.detail) : "HTTP " + r.status));
    return d;
  }
  const msg = (el, text, ok) => { el.textContent = text || ""; el.className = "msg " + (ok ? "ok" : "err"); };

  let me = {};

  // ---------- tabs
  const loaders = { users: loadUsers, collector: loadCollector, cameras: loadCameras, audit: () => loadAudit(0) };
  function showTab(tab) {
    document.querySelectorAll("nav.tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === tab));
    document.querySelectorAll("main > section").forEach((s) => (s.hidden = s.id !== "tab-" + tab));
    loaders[tab]();
  }
  document.querySelectorAll("nav.tabs button").forEach((b) => (b.onclick = () => {
    history.replaceState(null, "", "#" + b.dataset.tab); showTab(b.dataset.tab);
  }));

  // ---------- users
  async function loadUsers() {
    const users = await api("/api/v1/admin/users");
    $("#users tbody").innerHTML = users.map((u) => `<tr data-id="${u.id}">
      <td><b>${esc(u.username)}</b>${u.id === me.id ? ' <span class="pill">you</span>' : ""}<div class="muted small">since ${fmt(u.created_at)}</div></td>
      <td><select data-act="role">${["viewer", "admin"].map((r) => `<option ${r === u.role ? "selected" : ""}>${r}</option>`).join("")}</select></td>
      <td><span class="pill ${u.active ? "ok" : "bad"}">${u.active ? "active" : "disabled"}</span></td>
      <td title="${esc(fmt(u.last_login))}">${ago(u.last_login)}</td>
      <td class="num">${u.sessions}</td>
      <td style="white-space:nowrap">
        <button class="sm" data-act="toggle">${u.active ? "Disable" : "Enable"}</button>
        <button class="sm" data-act="pw">Reset password</button>
        ${u.id === me.id ? "" : `<button class="sm danger" data-act="del">Delete</button>`}
      </td></tr>`).join("");
    $("#users tbody").querySelectorAll("tr").forEach((tr) => {
      const id = +tr.dataset.id, name = tr.querySelector("b").textContent, m = $("#users-msg");
      const patch = async (body, done) => {
        try { await api(`/api/v1/admin/users/${id}`, { method: "PATCH", body: JSON.stringify(body) }); msg(m, done, true); }
        catch (e) { msg(m, e.message); }
        loadUsers();
      };
      tr.querySelector('[data-act="role"]').onchange = (e) => patch({ role: e.target.value }, `${name} is now ${e.target.value}`);
      tr.querySelector('[data-act="toggle"]').onclick = (e) =>
        patch({ active: e.target.textContent === "Enable" }, `${name} ${e.target.textContent === "Enable" ? "enabled" : "disabled"}`);
      tr.querySelector('[data-act="pw"]').onclick = () => {
        const pw = prompt(`New password for ${name} (min 10 characters):`);
        if (pw) patch({ password: pw }, `password reset for ${name}`);
      };
      const del = tr.querySelector('[data-act="del"]');
      if (del) del.onclick = async () => {
        if (!confirm(`Delete account ${name}? This can't be undone.`)) return;
        try { await api(`/api/v1/admin/users/${id}`, { method: "DELETE" }); msg(m, `${name} deleted`, true); }
        catch (e) { msg(m, e.message); }
        loadUsers();
      };
    });
  }
  $("#new-user").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target, m = $("#users-msg");
    try {
      await api("/api/v1/admin/users", { method: "POST",
        body: JSON.stringify({ username: f.username.value.trim().toLowerCase(), password: f.password.value, role: f.role.value }) });
      msg(m, `created ${f.username.value.trim().toLowerCase()} (${f.role.value})`, true); f.reset();
    } catch (err) { msg(m, err.message); }
    loadUsers();
  };

  // ---------- collector & health
  async function loadCollector() {
    const [srcs, runs] = await Promise.all([api("/api/v1/admin/sources"), api("/api/v1/admin/runs?limit=40")]);
    $("#sources tbody").innerHTML = srcs.map((s) => {
      const lr = s.last_run || {};
      const stale = !s.last_live_success || (Date.now() - new Date(s.last_live_success)) > 2 * 86400e3;
      return `<tr><td><b>${esc(s.name || s.id)}</b><div class="muted small">${esc(s.id)}</div></td>
        <td class="num">${s.active ?? 0}</td><td class="num">${s.online ?? 0}</td><td class="num">${s.offline ?? 0}</td>
        <td class="num">${s.retired ?? 0}</td><td class="num">${s.hidden ?? 0}</td>
        <td title="${esc(fmt(s.last_checked))}">${ago(s.last_checked)}</td>
        <td>${lr.mode ? `<span class="pill ${lr.error ? "bad" : lr.mode === "live" ? "ok" : "warn"}">${esc(lr.mode)}</span> ${ago(lr.started_at)}` : "–"}</td>
        <td><span class="${stale ? "pill warn" : ""}" title="${esc(fmt(s.last_live_success))}">${ago(s.last_live_success)}</span></td></tr>`;
    }).join("");
    $("#runs tbody").innerHTML = runs.map((r) => {
      const dur = r.finished_at ? Math.round((new Date(r.finished_at) - new Date(r.started_at)) / 1000) + " s" : "running";
      return `<tr><td>${fmt(r.started_at)}</td><td>${esc(r.source)}</td>
        <td><span class="pill ${r.mode === "live" ? "ok" : "warn"}">${esc(r.mode || "–")}</span></td>
        <td class="num">${r.cameras ?? "–"}</td><td class="num">+${r.added ?? 0} / −${r.removed ?? 0}</td>
        <td>${dur}</td><td class="small" style="color:var(--bad)">${esc(r.error || "")}</td></tr>`;
    }).join("") || `<tr><td colspan="7" class="muted">No runs yet.</td></tr>`;
  }
  $("#run-now").onclick = async (e) => {
    const m = $("#col-msg"); e.target.disabled = true;
    try {
      const r = await fetch("/api/v1/refresh", { method: "POST", headers: { "X-Eye": "1" } });
      msg(m, r.status === 409 ? "A run is already in progress." : r.ok ? "Started — scraping and checking takes a few minutes. This page refreshes in 60 s." : "Failed: HTTP " + r.status, r.ok || r.status === 409);
      setTimeout(() => { e.target.disabled = false; if (!$("#tab-collector").hidden) loadCollector(); }, 60000);
    } catch (err) { msg(m, err.message); e.target.disabled = false; }
  };

  // ---------- cameras & overrides
  let cams = null;
  async function reloadCamList() {
    if (!cams) {
      const gj = await api("/api/v1/cameras?include_hidden=1&include_inactive=1");
      cams = gj.features.map((f) => f.properties);
    }
    renderCamResults();
    await loadOverrides();
  }
  async function loadCameras() {
    await reloadCamList();
    const m = location.hash.match(/^#cameras=(.+)$/);
    if (m) editCamera(decodeURIComponent(m[1]));
  }
  function renderCamResults() {
    const q = $("#cam-q").value.trim().toLowerCase();
    const hits = cams.filter((c) => !q || [c.id, c.name, c.area, c.city].join(" ").toLowerCase().includes(q)).slice(0, 200);
    $("#cam-results").innerHTML = hits.map((c) => `<div data-id="${esc(c.id)}">
      <span>${esc(c.name)} <span class="muted small">· ${esc(c.source)}</span></span>
      <span>${c.hidden ? '<span class="pill warn">hidden</span>' : ""}${c.overridden && !c.hidden ? '<span class="pill ok">edited</span>' : ""}${c.active === false ? '<span class="pill">retired</span>' : ""}</span></div>`).join("")
      || `<div class="muted">No match.</div>`;
    $("#cam-results").querySelectorAll("div[data-id]").forEach((d) => (d.onclick = () => editCamera(d.dataset.id)));
  }
  let qt; $("#cam-q").oninput = () => { clearTimeout(qt); qt = setTimeout(renderCamResults, 120); };

  async function loadOverrides() {
    const ov = await api("/api/v1/admin/overrides");
    $("#overrides tbody").innerHTML = ov.map((o) => {
      const ch = [o.hidden && "hidden", o.name && `name → “${esc(o.name)}”`, o.lat != null && "location", o.note && "note"].filter(Boolean).join(", ");
      return `<tr data-id="${esc(o.camera_id)}" style="cursor:pointer"><td>${esc(o.name || o.source_name)}<div class="muted small">${esc(o.camera_id)}</div></td>
        <td class="small">${ch}</td><td class="small">${esc(o.updated_by || "")}<div class="muted">${ago(o.updated_at)}</div></td></tr>`;
    }).join("") || `<tr><td colspan="3" class="muted">None yet.</td></tr>`;
    $("#overrides tbody").querySelectorAll("tr[data-id]").forEach((tr) => (tr.onclick = () => editCamera(tr.dataset.id)));
  }

  async function editCamera(id) {
    const box = $("#cam-edit");
    document.querySelectorAll("#cam-results div").forEach((d) => d.classList.toggle("sel", d.dataset.id === id));
    box.innerHTML = `<p class="muted">Loading…</p>`;
    let c;
    try { c = await api("/api/v1/cameras/" + encodeURIComponent(id) + "?history=0"); }
    catch (e) { box.innerHTML = `<p class="msg err">${esc(e.message)}</p>`; return; }
    history.replaceState(null, "", "#cameras=" + encodeURIComponent(id));
    const srcLL = c.source_lat != null ? `${c.source_lat.toFixed(5)}, ${c.source_lon.toFixed(5)}` : "none";
    const ov = c.overridden;
    box.innerHTML = `<form class="form" id="ov-form">
      <div><b>${esc(c.name)}</b> <span class="muted small">· ${esc(c.source)} · <code>${esc(c.id)}</code></span>
        <div class="small">${c.active === false ? '<span class="pill">retired</span> ' : ""}<span class="pill">${esc(c.status)}</span>
        <a class="small" href="/#cam=${encodeURIComponent(c.id)}" target="_blank" rel="noopener">open map ↗</a></div></div>
      <label class="chk"><input type="checkbox" name="hidden" ${c.hidden ? "checked" : ""}> Hide from the map and API</label>
      <label>Display name <input name="name" value="${ov && c.name !== c.source_name ? esc(c.name) : ""}" placeholder="${esc(c.source_name)}">
        <span class="orig">source: ${esc(c.source_name)}</span></label>
      <div class="two">
        <label>Latitude <input name="lat" inputmode="decimal" value="${c.geo_source === "manual" ? c.lat : ""}" placeholder="${c.source_lat ?? ""}"></label>
        <label>Longitude <input name="lon" inputmode="decimal" value="${c.geo_source === "manual" ? c.lon : ""}" placeholder="${c.source_lon ?? ""}"></label>
      </div>
      <span class="orig">source location: ${srcLL}${c.meta?.geo === "approx" || c.geo_source === "approx" ? " (approximate)" : ""} · tip: paste “lat, lon” from Google Maps into Latitude</span>
      <label>Note <textarea name="note" rows="2" placeholder="why this was changed">${esc(c.override_note || "")}</textarea></label>
      <div class="row"><button class="primary" type="submit">Save override</button>
        ${ov ? `<button type="button" id="ov-reset" class="danger">Reset to source</button>` : ""}</div>
      ${ov ? `<span class="orig">last changed by ${esc(c.override_by || "?")}, ${fmt(c.override_at)}</span>` : ""}
      <p class="msg" id="ov-msg"></p></form>`;
    const f = $("#ov-form"), m = $("#ov-msg");
    f.lat.addEventListener("paste", (e) => {
      const t = (e.clipboardData || window.clipboardData).getData("text");
      const p = t.split(/[,\s]+/).filter(Boolean);
      if (p.length === 2 && p.every((x) => !isNaN(+x))) { e.preventDefault(); f.lat.value = p[0]; f.lon.value = p[1]; }
    });
    f.onsubmit = async (e) => {
      e.preventDefault();
      const num = (v) => v.trim() === "" ? null : Number(v.trim().replace(",", "."));
      const body = { hidden: f.hidden.checked, name: f.name.value.trim() || null, lat: num(f.lat.value), lon: num(f.lon.value), note: f.note.value.trim() || null };
      if ([body.lat, body.lon].some((x) => x !== null && isNaN(x))) { msg(m, "Latitude/longitude must be numbers"); return; }
      try {
        await api("/api/v1/admin/overrides/" + encodeURIComponent(id), { method: "PUT", body: JSON.stringify(body) });
        cams = null; await reloadCamList(); await editCamera(id); msg($("#ov-msg"), "Saved.", true);
      } catch (err) { msg(m, err.message); }
    };
    const rs = $("#ov-reset");
    if (rs) rs.onclick = async () => {
      if (!confirm("Remove all overrides for this camera?")) return;
      try {
        await api("/api/v1/admin/overrides/" + encodeURIComponent(id), { method: "DELETE" });
        cams = null; await reloadCamList(); await editCamera(id); msg($("#ov-msg"), "Reset to source data.", true);
      } catch (err) { msg(m, err.message); }
    };
  }

  // ---------- audit
  const PAGE = 50; let auditOffset = 0;
  async function loadAudit(offset) {
    auditOffset = Math.max(0, offset);
    const f = $("#audit-filter"), qs = new URLSearchParams({ limit: PAGE, offset: auditOffset });
    if (f.action.value) qs.set("action", f.action.value);
    if (f.username.value.trim()) qs.set("username", f.username.value.trim().toLowerCase());
    const d = await api("/api/v1/admin/audit?" + qs);
    const bad = new Set(["login_failed", "login_blocked", "user_delete"]);
    $("#audit tbody").innerHTML = d.items.map((a) => {
      const det = a.detail && Object.keys(a.detail).length ? `<details><summary>show</summary><pre>${esc(JSON.stringify(a.detail, null, 1))}</pre></details>` : "";
      return `<tr><td class="small" style="white-space:nowrap">${fmt(a.at)}</td><td>${esc(a.username || "")}</td>
        <td><span class="pill ${bad.has(a.action) ? "bad" : ""}">${esc(a.action)}</span></td>
        <td class="small"><code>${esc(a.target || "")}</code></td><td class="small">${esc(a.ip || "")}</td><td>${det}</td></tr>`;
    }).join("") || `<tr><td colspan="6" class="muted">Nothing logged.</td></tr>`;
    $("#audit-info").textContent = d.total ? `${auditOffset + 1}–${auditOffset + d.items.length} of ${d.total}` : "";
    $("#audit-prev").disabled = auditOffset === 0;
    $("#audit-next").disabled = auditOffset + PAGE >= d.total;
  }
  $("#audit-filter").onsubmit = (e) => { e.preventDefault(); loadAudit(0); };
  $("#audit-prev").onclick = () => loadAudit(auditOffset - PAGE);
  $("#audit-next").onclick = () => loadAudit(auditOffset + PAGE);

  // ---------- boot
  $("#logout").onclick = async () => { await api("/api/v1/auth/logout", { method: "POST" }).catch(() => {}); location.href = "/login"; };
  (async () => {
    me = await api("/api/v1/auth/me");
    $("#me").textContent = me.auth ? `${me.username} · ${me.role}` : "accounts off (no database)";
    const h = location.hash.slice(1);
    showTab(h.startsWith("cameras") ? "cameras" : loaders[h] ? h : "users");
  })().catch((e) => { document.querySelector("main").innerHTML = `<p class="msg err">${esc(e.message)}</p>`; });
})();
