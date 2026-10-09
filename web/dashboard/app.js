"use strict";
// Crag conditions dashboard. Three hash routes: #/ board, #/crag/<name>, #/status.
// Data comes from the Pi's read-only JSON API (rainarchive/dashboard.py).

const $ = (s, el = document) => el.querySelector(s);
const app = $("#app");
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, nd = 0) => (v == null || Number.isNaN(v)) ? "–" : Number(v).toFixed(nd);
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const angDiff = (a, b) => Math.abs(((a - b) % 360 + 540) % 360 - 180);
const FACE_HALF_ANGLE = 60;   // same as features.py
const MPH = 2.23694;          // m/s -> mph (the API is SI; conversion is display-only)
const mph = ms => Math.round(ms * MPH);

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-cache" });
  let j;
  try { j = await r.json(); } catch { throw new Error(`${r.status} ${r.statusText}`); }
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

function ago(ts, now = Date.now() / 1000) {
  if (ts == null) return "never";
  const s = now - ts;
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 2 * 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
const utc = ts => ts == null ? "–" : new Date(ts * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC";
const localDate = (ts, tz) => new Date(ts * 1000).toLocaleDateString("en-CA", { timeZone: tz });
const noon = s => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d, 12).getTime() / 1000; };
const midnight = s => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d).getTime() / 1000; };

// --- routing ----------------------------------------------------------------------------------
let charts = [];
let cleanups = [];            // per-view teardown (maps, timers)
function destroyCharts() {
  charts.forEach(c => { c.ro.disconnect(); c.u.destroy(); }); charts = [];
  cleanups.forEach(f => f()); cleanups = [];
}

async function route() {
  destroyCharts();
  const h = location.hash.replace(/^#/, "") || "/";
  const nav = h === "/status" ? "status" : h === "/radar" ? "radar" : h.startsWith("/crag/") ? "" : "board";
  document.querySelectorAll("[data-nav]").forEach(a => a.classList.toggle("on", a.dataset.nav === nav));
  try {
    if (h.startsWith("/crag/")) await showCrag(decodeURIComponent(h.slice(6)));
    else if (h === "/status") await showStatus();
    else if (h === "/radar") await showRadar();
    else await showBoard();
  } catch (e) {
    app.innerHTML = `<p class="err">Couldn't load data: ${esc(e.message)}</p><p class="muted">Is the Pi reachable?</p>`;
  }
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", route);

// --- board ------------------------------------------------------------------------------------
function thermal(d) {
  if (!d || d.tmax == null) return "";
  if (d.tmax <= 0) return "frozen";
  if (d.tmin >= 0) return "thawed";
  return "ft";
}

function cell(d, date, crag, today) {
  const cls = ["cell", thermal(d), date === today ? "today" : "", date > today ? "fcst" : ""].join(" ");
  const href = `#/crag/${encodeURIComponent(crag)}`;
  if (!d) return `<td><a class="${cls}" href="${href}"></a></td>`;
  const snow = d.frozen_precip_mm >= 1 ? Math.round(d.frozen_precip_mm) : "";
  const tip = `${date}: ${fmt(d.tmin, 1)}…${fmt(d.tmax, 1)} °C, snow ${fmt(d.frozen_precip_mm, 1)} mm, ` +
    `rain ${fmt(d.rain_mm, 1)} mm, rime on face ${fmt(d.rime_face_hrs)} h, lee loading ${fmt(d.lee_loading_mm, 1)} mm`;
  return `<td><a class="${cls}" href="${href}" title="${esc(tip)}">` +
    (d.lee_loading_mm >= 5 ? `<i class="ld"></i>` : "") +
    (d.rime_face_hrs >= 3 ? `<i class="rm"></i>` : "") +
    (d.rain_mm >= 1 ? `<i class="rn"></i>` : "") + `${snow}</a></td>`;
}

async function showBoard() {
  const b = await getJSON("api/board");
  const areas = [];
  for (const c of b.crags) {
    let a = areas.find(x => x.name === c.area);
    if (!a) areas.push(a = { name: c.area, crags: [] });
    a.crags.push(c);
  }
  const head = b.dates.map(d => {
    const dt = new Date(midnight(d) * 1000);
    const cls = d === b.today ? "today" : d > b.today ? "fc" : "";
    return `<th class="${cls}">${dt.toLocaleDateString("en-GB", { weekday: "narrow" })}<br>${dt.getDate()}</th>`;
  }).join("");
  let rows = "";
  for (const a of areas) {
    rows += `<tr class="area"><th colspan="${b.dates.length + 1}"><span>${esc(a.name || "Other")}</span></th></tr>`;
    for (const c of a.crags) {
      const dst = c.days_since_thaw == null ? "no thaw in record" : `${fmt(c.days_since_thaw)} d since thaw`;
      rows += `<tr><th class="crag"><a href="#/crag/${encodeURIComponent(c.name)}">${esc(c.name)}</a>` +
        `<small>${dst} · ${fmt(c.refreeze_7d)} refrz/7d</small></th>` +
        c.days.map((d, i) => cell(d, b.dates[i], c.name, b.today)).join("") + `</tr>`;
    }
  }
  app.innerHTML = `
    <h1>All crags</h1>
    <p class="sub">Last 7 days and next 7 (forecast in italics). Numbers are snow (frozen precipitation, mm water). Tap a crag.</p>
    <div class="board-wrap"><table class="board"><thead><tr><th class="crag"></th>${head}</tr></thead>
    <tbody>${rows}</tbody></table></div>
    <div class="legend">
      <span><i class="sw" style="background:var(--frozen)"></i>frozen all day (max ≤ 0 °C)</span>
      <span><i class="sw" style="background:var(--ft)"></i>freeze–thaw</span>
      <span><i class="sw" style="background:var(--thawed)"></i>thawed all day (min ≥ 0 °C)</span>
      <span><i class="sw" style="background:var(--nodata)"><i class="cell-mark ld" style="position:absolute;left:3px;top:3px;border-left:4px solid transparent;border-right:4px solid transparent;border-bottom:7px solid var(--load)"></i></i>lee loading ≥ 5 mm</span>
      <span><i class="sw" style="background:var(--nodata)"><i style="position:absolute;left:4px;top:4px;width:6px;height:6px;transform:rotate(45deg);background:var(--rime)"></i></i>rime on face ≥ 3 h</span>
      <span><i class="sw" style="background:var(--nodata)"><i style="position:absolute;left:2px;right:2px;bottom:3px;height:3px;border-radius:2px;background:var(--rain)"></i></i>rain ≥ 1 mm</span>
    </div>`;
  // on a narrow screen, scroll so the last few days, today and the forecast are in view
  const wrap = $(".board-wrap"), th = $("thead th.today", wrap), name = $("thead th.crag", wrap);
  if (wrap && th && name) wrap.scrollLeft = Math.max(0, th.offsetLeft - name.offsetWidth - 3 * th.offsetWidth);
}

// --- crag view --------------------------------------------------------------------------------
// uPlot plugin: forecast shading right of "now", and a "now" line.
function nowPlugin(now) {
  return {
    hooks: {
      drawClear: [u => {
        const x = u.valToPos(now, "x", true), { left, top, width, height } = u.bbox;
        if (x < left + width) {
          u.ctx.fillStyle = css("--fc");
          u.ctx.fillRect(Math.max(x, left), top, left + width - Math.max(x, left), height);
        }
      }],
      draw: [u => {
        const x = u.valToPos(now, "x", true), { left, top, width, height } = u.bbox;
        if (x < left || x > left + width) return;
        const c = u.ctx; c.save();
        c.strokeStyle = css("--now"); c.lineWidth = devicePixelRatio; c.setLineDash([4 * devicePixelRatio, 3 * devicePixelRatio]);
        c.beginPath(); c.moveTo(x, top); c.lineTo(x, top + height); c.stroke(); c.restore();
      }],
    },
  };
}

function hLines(scaleKey, specs) {   // horizontal reference lines/bands: [{y} | {y0, y1}, colourVar]
  return {
    hooks: {
      drawClear: [u => {
        const { left, width } = u.bbox, c = u.ctx;
        for (const [s, col] of specs) {
          c.save(); c.fillStyle = c.strokeStyle = css(col);
          if (s.y0 != null) {
            const a = u.valToPos(s.y1, scaleKey, true), b = u.valToPos(s.y0, scaleKey, true);
            c.fillRect(left, a, width, b - a);
          } else {
            const y = u.valToPos(s.y, scaleKey, true);
            c.lineWidth = devicePixelRatio; c.beginPath(); c.moveTo(left, y); c.lineTo(left + width, y); c.stroke();
          }
          c.restore();
        }
      }],
    },
  };
}

function windArrows(H, aspect) {
  return {
    hooks: {
      draw: [u => {
        const c = u.ctx, dpr = devicePixelRatio, { left, top, width } = u.bbox;
        const n = H.time.length;
        const step = Math.max(1, Math.ceil(n / (width / (13 * dpr))));
        const L = 9 * dpr, y = top + 8 * dpr;
        const cols = { face: css("--rime"), lee: css("--load"), cross: css("--cross") };
        for (let i = 0; i < n; i += step) {
          const wd = H.wdir[i];
          if (wd == null) continue;
          const x = u.valToPos(H.time[i], "x", true);
          if (x < left || x > left + width) continue;
          const kind = angDiff(wd, aspect) <= FACE_HALF_ANGLE ? "face"
            : angDiff(wd, aspect + 180) <= FACE_HALF_ANGLE ? "lee" : "cross";
          c.save(); c.translate(x, y); c.rotate((wd + 180) * Math.PI / 180);   // point downwind
          c.strokeStyle = c.fillStyle = cols[kind]; c.lineWidth = 1.5 * dpr;
          c.beginPath(); c.moveTo(0, L / 2); c.lineTo(0, -L / 2 + 2 * dpr); c.stroke();
          c.beginPath(); c.moveTo(0, -L / 2); c.lineTo(-3 * dpr, -L / 2 + 4 * dpr); c.lineTo(3 * dpr, -L / 2 + 4 * dpr); c.fill();
          c.restore();
        }
      }],
    },
  };
}

function addPanel(container, title, note, key) {
  const el = document.createElement("section");
  el.className = "panel";
  el.innerHTML = `<h3>${title}</h3>${key ? `<div class="keyrow">${key}</div>` : ""}<div class="chart"></div>` +
    (note ? `<p class="note">${note}</p>` : "");
  container.append(el);
  return el.querySelector(".chart");
}

function chart(el, range, now, series, data, opts = {}) {
  const muted = css("--muted"), grid = css("--grid");
  const axis = extra => ({ stroke: muted, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid, width: 1 }, ...extra });
  const h = opts.height || 160;
  const u = new uPlot({
    width: el.clientWidth || 320, height: h,
    scales: { x: { time: true, range: () => range }, ...(opts.scales || {}) },
    axes: [axis({ space: 56, values: (u, ts) => ts.map(t => new Date(t * 1000).toLocaleDateString("en-GB", { day: "numeric", month: "short" })) }),
      ...(opts.axes || [{ size: 42 }]).map(a => axis(a.label ? { labelSize: 16, labelFont: "11px system-ui", ...a } : a))],
    series: [{}, ...series],
    cursor: { sync: { key: "crag" }, drag: { x: false, y: false } },
    legend: { live: true },
    plugins: [nowPlugin(now), ...(opts.plugins || [])],
  }, data, el);
  // follow the container's width (first layout, rotation, window resize)
  const ro = new ResizeObserver(() => {
    const w = el.clientWidth;
    if (w && Math.abs(w - u.width) > 1) u.setSize({ width: w, height: h });
  });
  ro.observe(el);
  charts.push({ u, ro });
  return u;
}

async function showCrag(name) {
  const d = await getJSON("api/crag/" + encodeURIComponent(name));
  const c = d.crag, H = d.hourly, D = d.daily, now = d.now;
  const today = localDate(now, d.tz);
  const range = [midnight(d.from), midnight(d.to) + 86400];
  const xd = D.date.map(noon);
  const ti = D.date.indexOf(today);
  const upto = ti >= 0 ? ti : D.date.filter(x => x <= today).length - 1;
  const last7 = k => D[k].slice(Math.max(0, upto - 6), upto + 1).reduce((s, v) => s + (v || 0), 0);
  const at = k => upto >= 0 ? D[k][upto] : null;
  const f = d.factors || {};
  const facTxt = ["ukv", "era5"].filter(s => f[s]).map(s => `${s} ×${fmt(f[s].factor, 2)}`).join(", ");
  const fallbackShare = H.w850.length ? H.w850.filter(v => !v).length / H.w850.length : 0;
  const nowIdx = H.time.filter(t => t <= now).length - 1;
  const rimeNow = nowIdx >= 0 ? H.rime_cum[nowIdx] : null;
  const thawRule = `> ${d.thaw_rule.t} °C for > ${d.thaw_rule.hours} h`;

  app.innerHTML = `
    <h1>${esc(c.name)}</h1>
    <p class="sub">${esc(c.area)} · ${fmt(c.elev_m)} m · face aspect ${fmt(c.aspect)}° · ${esc(d.from)} → ${esc(d.to)} (shaded = forecast)</p>
    <div class="chips">
      <div class="chip"><b>${at("days_since_thaw") == null ? "–" : fmt(at("days_since_thaw"))}</b><span>days since thaw</span></div>
      <div class="chip"><b>${fmt(at("refreeze_7d"))}</b><span>refreezes, 7 d</span></div>
      <div class="chip"><b>${fmt(last7("frozen_precip_mm"))}</b><span>snow mm, 7 d</span></div>
      <div class="chip"><b>${fmt(last7("rain_mm"))}</b><span>rain mm, 7 d</span></div>
      <div class="chip"><b>${fmt(at("lee_loading_7d"))}</b><span>lee loading mm, 7 d</span></div>
      <div class="chip"><b>${fmt(rimeNow)}</b><span>rime-on-face h since thaw</span></div>
      <div class="chip"><b>${fmt(at("swe_model_mm"))}</b><span>modelled SWE mm</span></div>
    </div>
    <div id="panels"></div>`;
  const P = $("#panels");

  // 1. temperature
  chart(addPanel(P, "Temperature at crag height (°C)",
    "Open-Meteo 2 m temperature, lapse-rate adjusted to the crag's height. Band = ±0.5 °C freeze/thaw hysteresis used for refreeze counting."),
    range, now, [{ label: "T", stroke: css("--temp"), width: 1.5, value: (u, v) => v == null ? "–" : v.toFixed(1) + " °C" }],
    [H.time, H.t], { plugins: [hLines("y", [[{ y0: -0.5, y1: 0.5 }, "--band"], [{ y: 0 }, "--muted"]])] });

  // 2. ridge wind
  const wSolid = H.wind.map((v, i) => H.w850[i] && v != null ? v * MPH : null);
  const wFall = H.wind.map((v, i) => H.w850[i] || v == null ? null : v * MPH);
  chart(addPanel(P, "Ridge wind (mph) and direction",
    `Solid = 850 hPa model wind; dashed = 10 m wind × calibrated factor (${esc(facTxt || "no factors yet")})` +
    (fallbackShare > 0 ? `, used for ${Math.round(fallbackShare * 100)}% of hours shown.` : "."),
    `<span><i style="background:var(--rime)"></i>onto the face (rime)</span><span><i style="background:var(--load)"></i>over the top (loading)</span><span><i style="background:var(--cross)"></i>cross</span>`),
    range, now, [
      { label: "850 hPa", stroke: css("--wind"), width: 1.5, value: (u, v) => v == null ? "–" : v.toFixed(0) + " mph" },
      { label: "10 m × factor", stroke: css("--wind"), width: 1.5, dash: [5, 4], value: (u, v) => v == null ? "–" : v.toFixed(0) + " mph" },
    ], [H.time, wSolid, wFall], {
      scales: { y: { range: (u, mn, mx) => [0, Math.max(20, (mx || 0) * 1.3)] } },
      plugins: [windArrows(H, c.aspect)], height: 170,
    });

  // 3. precipitation (daily)
  const bars = uPlot.paths.bars({ size: [0.75, 40] });
  const total = D.frozen_precip_mm.map((v, i) => (v == null && D.rain_mm[i] == null) ? null : (v || 0) + (D.rain_mm[i] || 0));
  chart(addPanel(P, "Precipitation per day (mm water)",
    "Frozen = falls with wet-bulb < 1 °C. Dots = rain radar at the crag (archive since 2026-09-25) for comparison with the model."),
    range, now, [
      { label: "Rain (top)", stroke: css("--rain"), fill: css("--rain"), paths: bars, points: { show: false }, value: (u, v, s, i) => fmt(D.rain_mm[i], 1) },
      { label: "Frozen", stroke: css("--snow"), fill: css("--snow"), paths: bars, points: { show: false }, value: (u, v) => fmt(v, 1) },
      { label: "Radar", stroke: css("--radar"), paths: () => null, points: { show: true, size: 6, fill: css("--radar") }, value: (u, v) => fmt(v, 1) },
    ], [xd, total, D.frozen_precip_mm, D.radar_mm], { scales: { y: { range: (u, mn, mx) => [0, Math.max(5, (mx || 0) * 1.1)] } } });

  // 4. snowpack and loading
  chart(addPanel(P, "Snowpack and wind loading",
    `Modelled SWE is a simple degree-day snowpack at the crag point, not an observation. Lee loading = frozen precipitation while ridge wind ≥ ${mph(7)} mph (7 m/s) blows over the top of the face.`),
    range, now, [
      { label: "Lee loading mm", stroke: css("--load"), fill: css("--load"), paths: bars, points: { show: false }, scale: "mm", value: (u, v) => fmt(v, 1) },
      { label: "Modelled SWE mm", stroke: css("--swe"), width: 2, scale: "swe", value: (u, v) => fmt(v) },
    ], [xd, D.lee_loading_mm, D.swe_model_mm], {
      scales: { mm: { range: (u, mn, mx) => [0, Math.max(5, (mx || 0) * 1.1)] }, swe: { range: (u, mn, mx) => [0, Math.max(20, (mx || 0) * 1.1)] } },
      axes: [{ scale: "mm", size: 42, label: "loading mm/day" }, { scale: "swe", side: 1, size: 46, grid: { show: false }, label: "SWE mm" }],
    });

  // 5. rime: hourly, counter reset by sustained thaws
  const rimeStrip = H.rime_rime.map(v => v ? 1 : null);
  chart(addPanel(P, "Rime on the face (hours since last thaw)",
    `Rime hour: in cloud (RH ≥ 95 %), −10 … −0.5 °C, ridge wind ≥ ${mph(5)} mph (5 m/s) from within ±60° of the face aspect. ` +
    `The line counts rime hours since the last sustained thaw (${thawRule}), which resets it to zero.`,
    `<span><i style="background:var(--rime)"></i>rime hour</span><span><i style="background:var(--thawband);height:10px"></i>sustained thaw (${thawRule})</span>`),
    range, now, [
      { label: "Rime h since thaw", stroke: css("--fg"), width: 1.5, scale: "h", value: (u, v) => fmt(v) },
      { label: "Rime hour", stroke: css("--rime"), fill: css("--rime"), width: 0, scale: "flag",
        paths: uPlot.paths.stepped({ align: 1 }), points: { show: false }, value: (u, v) => v ? "yes" : "–" },
    ], [H.time, H.rime_cum, rimeStrip], {
      scales: { h: { range: (u, mn, mx) => [0, Math.max(6, (mx || 0) * 1.15)] }, flag: { range: [0, 9] } },
      axes: [{ scale: "h", size: 42, label: "hours" }],
      plugins: [thawBands(H)],
    });
}

// shade runs of hours flagged as sustained thaw
function thawBands(H) {
  return {
    hooks: {
      drawClear: [u => {
        const c = u.ctx, { left, top, width, height } = u.bbox;
        c.save(); c.fillStyle = css("--thawband");
        for (let i = 0; i < H.time.length; i++) {
          if (!H.rime_thaw[i] || H.rime_thaw[i - 1]) continue;
          let j = i;
          while (j + 1 < H.time.length && H.rime_thaw[j + 1]) j++;
          const x0 = Math.max(left, u.valToPos(H.time[i], "x", true));
          const x1 = Math.min(left + width, u.valToPos(H.time[j] + 3600, "x", true));
          if (x1 > x0) c.fillRect(x0, top, x1 - x0, height);
        }
        c.restore();
      }],
    },
  };
}

// --- radar ------------------------------------------------------------------------------------
const ACC_STEPS = [1, 2, 3, 6, 9, 12, 18, 24, 36, 48, 72, 96, 120, 144, 168];   // hours
const hhmm = ts => new Date(ts * 1000).toLocaleString("en-GB", { weekday: "short", hour: "2-digit", minute: "2-digit" });
const accLabel = h => h < 48 ? `${h} h` : `${h / 24} days`;

function swatches(stops, cols, unit) {
  return `<div class="scale">${stops.map((s, i) =>
    `<span><i style="background:${cols[i]}"></i>${s}</span>`).join("")}<span class="muted">${unit}</span></div>`;
}

async function showRadar() {
  const info = await getJSON("api/radar");
  const b = info.bounds;
  let mode = "now", frameIdx = info.frames.length - 1, accIdx = ACC_STEPS.indexOf(24), playing = null;
  let frames = info.frames, latest = info.latest;

  app.innerHTML = `
    <h1>Rain radar</h1>
    <p class="sub">RainViewer composite, archived on the Pi every 10 minutes. Pinch to zoom; tap a crag for its value.</p>
    <div id="map"></div>
    <div class="seg" role="tablist">
      <button data-mode="now" class="on">Now</button><button data-mode="acc">Accumulated</button>
    </div>
    <div id="ctl"></div>
    <div id="legend"></div>
    <div id="cragvals"></div>`;

  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const base = dark ? "Canvas/World_Dark_Gray" : "Canvas/World_Light_Gray";
  const map = L.map("map", { zoomSnap: 0.5, attributionControl: true });
  cleanups.push(() => map.remove());
  L.tileLayer(`https://server.arcgisonline.com/ArcGIS/rest/services/${base}_Base/MapServer/tile/{z}/{y}/{x}`,
    { maxZoom: 13, attribution: "Tiles © Esri · Radar © RainViewer" }).addTo(map);
  map.createPane("labels");
  map.getPane("labels").style.zIndex = 650;
  map.getPane("labels").style.pointerEvents = "none";
  L.tileLayer(`https://server.arcgisonline.com/ArcGIS/rest/services/${base}_Reference/MapServer/tile/{z}/{y}/{x}`,
    { maxZoom: 13, pane: "labels" }).addTo(map);
  map.fitBounds([[55.3, -7.6], [58.7, -1.8]]);
  const overlay = L.imageOverlay("", [[b.south, b.west], [b.north, b.east]],
    { opacity: 0.8, className: "radar-img" }).addTo(map);
  const markers = {};
  for (const c of info.crags) {
    markers[c.name] = L.circleMarker([c.lat, c.lon], { radius: 5, weight: 2, color: css("--fg"), fillColor: css("--card"), fillOpacity: 1 })
      .bindTooltip(esc(c.name), { direction: "top", offset: [0, -4] }).addTo(map);
  }

  const ctl = $("#ctl"), legend = $("#legend"), vals = $("#cragvals");

  function stopPlay() { if (playing) { clearInterval(playing); playing = null; } const p = $("#play"); if (p) p.textContent = "▶ Play"; }
  cleanups.push(stopPlay);

  function showFrame() {
    if (!frames.length) { ctl.innerHTML = `<p class="muted">No radar frames yet.</p>`; return; }
    const t = frames[frameIdx];
    overlay.setUrl(`api/radar/frame/${t}.png`);
    $("#flabel").textContent = `${hhmm(t)} · ${ago(t + 600)}`;
    $("#fslider").value = frameIdx;
  }

  function renderNow() {
    stopPlay();
    ctl.innerHTML = frames.length ? `
      <div class="ctlrow"><button id="play" class="btn">▶ Play</button><b id="flabel"></b></div>
      <input id="fslider" type="range" min="0" max="${frames.length - 1}" step="1" value="${frameIdx}">
      <p class="note">Last ${frames.length} frames (${Math.round(frames.length / 6)} h). Drag to step through, or play the loop.</p>` : "";
    legend.innerHTML = swatches(info.rate_stops, info.rate_cols, "mm/h");
    vals.innerHTML = "";
    for (const c of info.crags) markers[c.name].setTooltipContent(esc(c.name));
    if (!frames.length) return showFrame();
    $("#fslider").addEventListener("input", e => { stopPlay(); frameIdx = +e.target.value; showFrame(); });
    $("#play").addEventListener("click", () => {
      if (playing) return stopPlay();
      frames.forEach(t => { new Image().src = `api/radar/frame/${t}.png`; });   // warm the browser cache
      $("#play").textContent = "❚❚ Pause";
      playing = setInterval(() => { frameIdx = (frameIdx + 1) % frames.length; showFrame(); }, 600);
    });
    showFrame();
  }

  let accTimer = null, accSeq = 0;
  cleanups.push(() => clearTimeout(accTimer));
  async function loadAcc() {
    const h = ACC_STEPS[accIdx], seq = ++accSeq;
    let a;
    try {
      a = await getJSON(`api/radar/accum?hours=${h}`);
    } catch (e) {
      if (seq !== accSeq || mode !== "acc") return;
      $("#alabel2").textContent = e.message;
      if (/warming/.test(e.message)) accTimer = setTimeout(loadAcc, 3000);
      return;
    }
    if (seq !== accSeq || mode !== "acc") return;
    overlay.setUrl(a.png);
    const cov = a.expected_frames ? Math.round(100 * a.frames / a.expected_frames) : 0;
    $("#alabel2").textContent = `${hhmm(a.start)} → ${hhmm(a.end)} · max ${fmt(a.max_mm, 0)} mm · ${cov}% of frames`;
    legend.innerHTML = swatches(a.stops, a.cols, "mm") + (a.adaptive
      ? `<p class="note">Colour scale fitted to this window: each colour covers about the same share of the area with rain, so compare colours within one map, not between windows.</p>`
      : `<p class="note">Fixed colour scale.</p>`);
    const rows = Object.entries(a.crags).sort((x, y) => y[1] - x[1]);
    for (const [n, v] of rows) markers[n] && markers[n].setTooltipContent(`${esc(n)}: ${fmt(v, 1)} mm`);
    vals.innerHTML = `<h2>At the crags (≈4 km box)</h2><table class="kv">${rows.map(([n, v]) =>
      `<tr><th><a href="#/crag/${encodeURIComponent(n)}">${esc(n)}</a></th><td>${fmt(v, 1)} mm</td></tr>`).join("")}</table>`;
  }

  function renderAcc() {
    stopPlay();
    ctl.innerHTML = `
      <div class="ctlrow"><b id="alabel">Last ${accLabel(ACC_STEPS[accIdx])}</b></div>
      <input id="aslider" type="range" min="0" max="${ACC_STEPS.length - 1}" step="1" value="${accIdx}">
      <div class="ticks"><span>1 h</span><span>24 h</span><span>7 days</span></div>
      <p class="note" id="alabel2">Loading…</p>`;
    legend.innerHTML = "";
    $("#aslider").addEventListener("input", e => {
      accIdx = +e.target.value;
      $("#alabel").textContent = `Last ${accLabel(ACC_STEPS[accIdx])}`;
      clearTimeout(accTimer); accTimer = setTimeout(loadAcc, 250);
    });
    loadAcc();
  }

  document.querySelectorAll(".seg button").forEach(btn => btn.addEventListener("click", () => {
    mode = btn.dataset.mode;
    document.querySelectorAll(".seg button").forEach(x => x.classList.toggle("on", x === btn));
    mode === "now" ? renderNow() : renderAcc();
  }));

  // pick up new frames every 2 minutes while the page is open
  const poll = setInterval(async () => {
    try {
      const fresh = await getJSON("api/radar");
      if (fresh.latest === latest) return;
      const atEnd = frameIdx === frames.length - 1;
      frames = fresh.frames; latest = fresh.latest;
      if (mode === "now" && !playing) { frameIdx = atEnd ? frames.length - 1 : Math.min(frameIdx, frames.length - 1); renderNow(); }
      else if (mode === "acc") loadAcc();
    } catch { /* keep showing what we have */ }
  }, 120000);
  cleanups.push(() => clearInterval(poll));
  renderNow();
}

// --- status -----------------------------------------------------------------------------------
function badge(ok, warnTxt = "check") { return ok === null ? "" : ok ? `<span class="badge ok">ok</span>` : `<span class="badge warn">${warnTxt}</span>`; }

async function showStatus() {
  const s = await getJSON("api/status");
  const now = s.now, w = s.weather_update, rc = s.radar_collector, r = s.radar;
  const wAge = w ? now - Date.parse(w.at_utc) / 1000 : null;
  const rows = [];
  rows.push(["Weather update", w ? `${ago(Date.parse(w.at_utc) / 1000, now)} · errors ${w.errors}, skipped (budget) ${w.skipped_budget}` +
    badge(wAge < 4 * 3600 && !w.errors) : "no run yet"]);
  for (const [src, v] of Object.entries(s.sources || {})) {
    rows.push([`Model data: ${src}`, v.crags ? `${v.crags} crags · ${utc(v.first).slice(0, 10)} → ${utc(v.last_min)}` +
      (v.last_min !== v.last_max ? ` (newest ${utc(v.last_max)})` : "") : "none"]);
  }
  for (const [src, v] of Object.entries(s.backfill || {})) rows.push([`Backfill ${src} (crag-years)`, `${v.done}/${v.total}` + badge(v.done >= v.total, "running")]);
  rows.push(["Open-Meteo use today", `${fmt(s.api_weight_today)} / ${fmt(s.daily_budget)} weighted calls`]);
  if (r) {
    const lastAge = now - r.last;
    rows.push(["Radar last frame", `${utc(r.last)} (${ago(r.last, now)})` + badge(lastAge < 45 * 60, "stale")]);
    rows.push(["Radar frames, last 24 h", `${r.frames_24h} / ${r.expected_24h}` + badge(r.frames_24h >= 140, "gaps")]);
    rows.push(["Radar archive", `${r.frames_total} frames since ${utc(r.first).slice(0, 10)}`]);
    rows.push(["Radar unmatched colour px (24 h mean)", `${fmt(r.unmatched_px_24h_mean)}` + badge(r.unmatched_px_24h_mean < 100, "investigate")]);
  }
  if (rc) rows.push(["Radar collector", `last poll ${ago(Date.parse(rc.last_poll_utc) / 1000, now)} · consecutive errors ${rc.consecutive_errors}` + badge(!rc.consecutive_errors)]);
  rows.push(["Conditions observations logged", fmt(s.observations)]);

  const fac = s.wind_factors || {};
  const facRows = Object.entries(fac).map(([crag, v]) =>
    `<tr><th>${esc(crag)}</th><td>${["ukv", "era5", "fc"].map(k => v[k] ? `${k} ×${fmt(v[k].factor, 2)}` : "").filter(Boolean).join(" · ")}</td></tr>`).join("");
  app.innerHTML = `
    <h1>Status</h1>
    <p class="sub">Checked ${new Date(now * 1000).toLocaleString("en-GB")}</p>
    <table class="kv">${rows.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${v}</td></tr>`).join("")}</table>
    <h2>10 m → ridge wind factors</h2>
    <p class="sub">Median 850 hPa / 10 m wind ratio, used where 850 hPa is missing (ERA5; UKV before 2024-08).</p>
    <table class="kv">${facRows || `<tr><td>none yet</td></tr>`}</table>`;
}

route();
