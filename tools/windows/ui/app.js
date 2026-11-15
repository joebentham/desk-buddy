// Lump Control's page. Everything it knows or changes goes through window.pywebview.api (class Api in
// control.py); it polls the bridge's state once a second and mirrors Lump: his mood, painted in his colour
// from the same baked frames the Mac menu-bar Lump uses (glow ink in dark mode, paper ink in light).
"use strict";

const SWATCHES = [["Tangerine", "FF7A45"], ["Lemon", "FFC93C"], ["Mint", "3DD68C"], ["Sky", "4FA8FF"],
                  ["Grape", "9B6BFF"], ["Bubblegum", "FF6FB5"], ["Cherry", "E8334A"]];
const MOODS = [["idle", "Idle"], ["awake", "Awake"], ["working", "Working"], ["alert", "Needs you"], ["waiting", "Waiting"],
               ["done", "Done"], ["longhaul", "Long haul"], ["night", "Night"], ["letter", "VIP letter"], ["meeting", "Meeting"]];
const MOOD_LABEL = Object.fromEntries(MOODS);
const STATE_LABEL = { working: "Working", alert: "Needs you", done: "Finished", idle: "Idle", snoozed: "Snoozed" };
const LINK_LABEL = { usb: "Plugged in by USB", wifi: "On Wi-Fi", relay: "On the remote link" };

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;
let latest = null, formFilled = false, themeSet = false, animationsShown = false;

// ---------- small helpers ----------
let toastTimer = null;
function toast(res) {
  if (!res || !res.message) return;
  const t = $("toast");
  t.textContent = res.message;
  t.classList.toggle("error", res.ok === false);
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 3500);
}
async function act(promise) { try { toast(await promise); } catch (e) { toast({ ok: false, message: String(e) }); } }
function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids) n.append(k);
  return n;
}
function rgb(hex) { const h = (hex || "FF7A45").replace("#", ""); return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) || 0); }
function setAccent(hex) {
  const [r, g, b] = rgb(hex), s = document.documentElement.style;
  s.setProperty("--accent", "#" + hex.replace("#", ""));
  s.setProperty("--accent-soft", `rgba(${r},${g},${b},0.22)`);
  s.setProperty("--accent-glow", `rgba(${r},${g},${b},0.32)`);
}
function age(s) { return s >= 3600 ? Math.floor(s / 3600) + "h" : s >= 60 ? Math.floor(s / 60) + "m" : s + "s"; }

// ---------- Lump, live ----------
// The board's own frames (lump.bin, 240 x 240): run-length data, [count][RGB565 byte-swapped], recoloured the way
// the firmware does it (firmware/src/main.cpp, recolor): his body is drawn in magenta, mixed with the cream line.
const lump = { key: "", frames: [], ticks: [0], fps: 24, start: performance.now(), loading: "" };
function recolour(stored, tint) {
  const v = ((stored >> 8) | (stored << 8)) & 0xffff;
  const r = (v >> 11) << 3, g = ((v >> 5) & 63) << 2, b = (v & 31) << 3;
  if (r < 8 && g < 8 && b < 8) return [r, g, b];                          // black stays black
  const beta = g / 241, alpha = r / 255 - beta;
  if (alpha <= 0.02) return [r, g, b];                                    // pure line colour
  if (b < alpha * 255 + beta * 220 - 48) return [r, g, b];                // a real colour (the banana): kept
  return [Math.min(255, alpha * tint[0] + beta * 255), Math.min(255, alpha * tint[1] + beta * 241), Math.min(255, alpha * tint[2] + beta * 220)];
}
function decodeFrame(b64, tint, cache) {
  const bin = atob(b64), img = new ImageData(240, 240), px = img.data;
  let i = 0;
  for (let p = 0; p + 2 < bin.length && i < 57600; p += 3) {
    const n = bin.charCodeAt(p), stored = bin.charCodeAt(p + 1) | (bin.charCodeAt(p + 2) << 8);
    let c = cache.get(stored);
    if (!c) { c = recolour(stored, tint); cache.set(stored, c); }
    for (let k = 0; k < n && i < 57600; k++, i++) { const o = i * 4; px[o] = c[0]; px[o + 1] = c[1]; px[o + 2] = c[2]; px[o + 3] = 255; }
  }
  const fc = document.createElement("canvas"); fc.width = 240; fc.height = 240;
  fc.getContext("2d").putImageData(img, 0, 0);
  return fc;
}
const screens = new Map();                                               // mood -> the board's data for its loop
async function showLump(mood, colour) {
  const key = `${mood}|${colour}`;
  if (key === lump.key || key === lump.loading) return;
  lump.loading = key;
  if (!screens.has(mood)) screens.set(mood, await api().screen(mood));
  const s = screens.get(mood);
  if (!s || lump.loading !== key) { if (lump.loading === key) lump.loading = ""; return; }
  const tint = rgb(colour), cache = new Map();
  const frames = s.frames.map((f) => decodeFrame(f, tint, cache));
  if (lump.loading !== key) return;
  Object.assign(lump, { key, frames, ticks: s.ticks, fps: s.fps, loading: "" });
}
// one-off animations (gags, taps, fidgets, oops, hello): the board names each as it starts ("playing" in the
// status); the window plays the same sequence once, from where the board is in it, then goes back to the loop
const anim = { key: "", frames: [], ticks: [], fps: 24, start: 0, loading: "" };
const events = new Map();                                                // name -> the board's data for it
async function playEvent(name, age, colour) {
  const key = `${name}|${Math.round(Date.now() / 1000 - age)}|${colour}`;
  if (key === anim.key || key === anim.loading) return;
  anim.loading = key;
  if (!events.has(name)) events.set(name, await api().screen_event(name));
  const e = events.get(name);
  if (!e || anim.loading !== key) { if (anim.loading === key) anim.loading = ""; return; }
  if (age > e.ticks.length / e.fps) { anim.loading = ""; return; }      // already over on the board
  const tint = rgb(colour), cache = new Map();
  Object.assign(anim, { key, frames: e.frames.map((f) => decodeFrame(f, tint, cache)), ticks: e.ticks, fps: e.fps,
                        start: performance.now() - age * 1000, loading: "" });
}
function animLabel(name) {
  return name.replace(/^tap_/, "").replace(/_(idle|awake|working|alert|waiting|done|longhaul|night|letter|meeting)$/, "").replace(/_/g, " ");
}
function drawLump(now) {
  const cv = $("lump"), x = cv.getContext("2d");
  if (anim.frames.length) {
    const tick = Math.floor((now - anim.start) / 1000 * anim.fps);
    if (tick < anim.ticks.length) {
      x.drawImage(anim.frames[anim.ticks[Math.max(0, tick)]] || anim.frames[0], 0, 0);
      requestAnimationFrame(drawLump);
      return;
    }
    anim.frames = [];                                                    // finished: back to his mood
  }
  if (lump.frames.length) {
    const tick = Math.floor((now - lump.start) / 1000 * lump.fps) % lump.ticks.length;
    x.drawImage(lump.frames[lump.ticks[tick]] || lump.frames[0], 0, 0);
  }
  requestAnimationFrame(drawLump);
}

// ---------- rendering the state ----------
function theme() { return document.documentElement.dataset.theme || "dark"; }
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  const next = t === "dark" ? "light" : "dark";
  $("theme").setAttribute("aria-label", `Switch to ${next} mode`);
  $("theme").title = `Switch to ${next} mode`;
}

function render(s) {
  latest = s;
  if (!themeSet) { applyTheme(s.theme); themeSet = true; }
  const st = s.status, up = !!st, board = up && st.device;
  const colour = (up && st.colour) || s.colour;
  setAccent(colour);

  const live = $("live");
  live.className = "live " + (board ? "ok" : up ? "warn" : "");
  $("live-text").textContent = board ? "Live" : up ? "Board not connected" : "Bridge not running";
  $("start-bridge").classList.toggle("hidden", up);
  $("halo").classList.toggle("offline", !board);

  // what the board is showing: a mood previewed from the Play tab until the bridge next sends his real one
  const shown = board ? (st.shown || st.mood) : "idle", previewing = board && shown !== st.mood;
  const mood = shown === "demo" ? st.mood : shown;
  $("mood").textContent = board ? (shown === "demo" ? "Showcase" : MOOD_LABEL[shown] || shown) : up ? "Offline" : "Asleep";
  $("link").textContent = !board ? (up ? "Plug him in, or check Wi-Fi" : "Lump can't hear from Claude")
    : previewing ? "Preview · Back to live returns him to " + (MOOD_LABEL[st.mood] || st.mood).toLowerCase()
    : (LINK_LABEL[st.link] || "Connected");
  showLump(mood, colour);
  if (board && st.playing) {
    playEvent(st.playing.name, st.playing.age, colour);
    if (st.playing.age < 6) $("link").textContent = "Playing " + animLabel(st.playing.name);
  }

  const c = (up && st.counts) || {};
  $("n-working").textContent = c.working || 0; $("n-alert").textContent = c.alert || 0; $("n-unseen").textContent = c.unseen || 0;
  renderUsage(up ? st : null);
  renderApps(up ? st.apps : null);

  // status page
  const sess = Object.entries((up && st.sessions) || {}).sort((a, b) => a[1].age_s - b[1].age_s);
  $("sessions").replaceChildren(...sess.map(([id, v]) => el("div", { class: "item" },
    el("span", { class: "sdot " + v.state }),
    el("span", { class: "name", title: v.cwd || id }, (v.cwd || "").split(/[\\/]/).filter(Boolean).pop() || id),
    el("span", { class: "meta" }, STATE_LABEL[v.state] || v.state),
    el("span", { class: "meta" }, age(v.age_s)))));
  $("no-sessions").classList.toggle("hidden", sess.length > 0 || !up);

  // play page
  for (const b of document.querySelectorAll(".tile")) b.classList.toggle("current", board && b.dataset.mood === shown);
  if (!animationsShown && up) loadAnimations();
  if (!document.activeElement || document.activeElement.id !== "tap") { $("tap").value = s.tap; $("tap-value").textContent = Number(s.tap).toFixed(2) + " g"; }

  // sounds page
  $("snd-on").checked = s.sounds.enabled;
  $("snd-quiet").checked = s.sounds.quiet_at_night;
  if (document.activeElement !== $("snd-vol")) { $("snd-vol").value = s.sounds.volume; $("snd-vol-value").textContent = Math.round(s.sounds.volume * 100) + "%"; }
  renderMoments(s.sounds.moments);

  // settings page
  for (const b of document.querySelectorAll(".swatch")) b.classList.toggle("current", b.dataset.hex === s.colour);
  $("colour-pick").value = "#" + s.colour.toLowerCase();
  $("pairing").textContent = s.settings.lump_paired ? "Wi-Fi pairing: set up" : "Wi-Fi pairing: not set up (run tools\\wifi_setup.py with him plugged in)";
  $("relay-text").textContent = s.relay.set_up
    ? (s.relay.text || "Set up. Waiting for the bridge to start listening.").replace(/^Remote Lump: /, "").replace(/^./, (m) => m.toUpperCase())
    : "Not set up. To drive a Lump plugged into another computer, run tools\\windows\\install.ps1 -AllowRelay on this PC.";
  $("relay-actions").classList.toggle("hidden", !s.relay.set_up);
  if (!formFilled) {
    const f = $("settings-form");
    for (const [k, v] of Object.entries(s.settings)) if (f.elements[k]) f.elements[k].value = v;
    formFilled = true;
  }
}

// usage: a 5-hour and a weekly bar for each Claude app with readings (each app is its own account)
function renderUsage(st) {
  const usage = (st && st.usage) || {}, apps = (st && st.apps) || [{ account: "personal", name: "Claude" }];
  const pct = (acct, w) => { const v = usage[acct === "personal" ? w : `${acct}:${w}`]; return v && v.pct != null ? v.pct : null; };
  let shown = apps.filter((a) => pct(a.account, "five_hour") != null || pct(a.account, "seven_day") != null);
  if (!shown.length) shown = apps.slice(0, 1);
  const named = shown.length > 1;
  const bar = (label, p) => el("div", { class: "usage-cell" },
    el("div", { class: "usage-row" }, el("span", {}, label), el("span", {}, p == null ? "–" : Math.round(p) + "%")),
    el("div", { class: "bar" }, el("div", { class: "fill", style: `width:${p == null ? 0 : Math.min(100, p)}%` })));
  // one app: two full-width bars; several: a compact pair per app, side by side
  $("usage").classList.toggle("multi", named);
  $("usage").replaceChildren(...shown.flatMap((a) => [
    ...(named ? [el("div", { class: "usage-app" }, a.name)] : []),
    bar(named ? "5-hour" : "5-hour usage", pct(a.account, "five_hour")), bar(named ? "Week" : "Weekly usage", pct(a.account, "seven_day"))]));
}

// Claude apps card: names are editable; usual and running ones stay listed, added folders can be removed
let appsKey = "", appsDraft = null, appsList = [];               // appsList: the card's rows, as edited
function renderApps(apps) {
  if (appsDraft) return;                                                 // being edited: leave it alone
  const key = JSON.stringify(apps || []);
  if (key === appsKey) return;
  appsKey = key;
  drawApps((apps || []).map((a) => ({ ...a })));
}
function drawApps(list) {
  appsList = list;
  const badge = { usual: "usual folder", configured: "added", running: "found running" };
  $("apps").replaceChildren(...list.map((a, i) => el("div", { class: "app-row" },
    el("input", { type: "text", value: a.name, "aria-label": "Name for " + a.path,
      oninput: (e) => { appsDraft = appsDraft || list; list[i].name = e.target.value; } }),
    el("span", { class: "path", title: a.path }, a.path),
    el("span", { class: "badge" }, badge[a.source] || a.source),
    a.source === "configured"
      ? el("button", { class: "btn", type: "button", onclick: () => { appsDraft = list; list.splice(i, 1); drawApps(list); } }, "Remove")
      : el("span", {}))));
}

let momentsKey = "";
function renderMoments(moments) {
  const key = JSON.stringify(moments);
  if (key === momentsKey) return;
  momentsKey = key;
  $("moments").replaceChildren(...Object.entries(moments).map(([name, on]) => {
    const sw = el("input", { type: "checkbox", class: "switch", "aria-label": name.replace(/_/g, " "),
      onchange: (e) => act(api().set_moment(name, e.target.checked)) });
    sw.checked = on;
    return el("div", { class: "item moment" },
      el("span", { class: "name" }, name.replace(/_/g, " ").replace(/^./, (m) => m.toUpperCase())),
      el("button", { class: "btn", type: "button", "aria-label": "Play " + name.replace(/_/g, " "), onclick: () => act(api().play_sound(name)) }, "Play"),
      sw);
  }));
}

async function loadAnimations() {
  animationsShown = true;
  const list = await api().animations();
  if (!list.length) { animationsShown = false; return; }
  $("animations").replaceChildren(...list.map((a) => el("button", { class: "tile", type: "button", onclick: () => act(api().play(a.name)) }, a.label)));
}

async function poll() {
  try { render(await api().state()); } catch (e) { /* the window is closing */ }
  setTimeout(poll, 1000);
}

// ---------- wiring ----------
function wire() {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => {
      for (const t of document.querySelectorAll(".tab")) t.setAttribute("aria-selected", String(t === tab));
      for (const p of document.querySelectorAll(".page")) p.classList.toggle("hidden", p.id !== "page-" + tab.dataset.tab);
    });
  }
  $("theme").addEventListener("click", () => {
    const t = theme() === "dark" ? "light" : "dark";
    applyTheme(t); api().set_theme(t);
    if (latest) render(latest);
  });
  $("start-bridge").addEventListener("click", () => act(api().bridge_ctl("start")));
  for (const b of document.querySelectorAll("[data-bridge]")) b.addEventListener("click", () => act(api().bridge_ctl(b.dataset.bridge)));
  for (const b of document.querySelectorAll("[data-open]")) b.addEventListener("click", () => act(api().open(b.dataset.open)));

  $("moods").replaceChildren(...MOODS.map(([m, label]) => el("button", { class: "tile", type: "button", "data-mood": m, onclick: () => act(api().preview(m)) }, label)));
  $("showcase").addEventListener("click", () => act(api().preview("demo")));
  $("back-live").addEventListener("click", () => act(api().live()));
  $("tap").addEventListener("input", (e) => { $("tap-value").textContent = Number(e.target.value).toFixed(2) + " g"; });
  $("tap-apply").addEventListener("click", () => act(api().set_tap(Number($("tap").value))));
  $("tap-default").addEventListener("click", () => { $("tap").value = 0.35; $("tap-value").textContent = "0.35 g"; act(api().set_tap(0.35)); });

  $("snd-on").addEventListener("change", (e) => act(api().set_sound(e.target.checked)));
  $("snd-quiet").addEventListener("change", (e) => act(api().set_quiet(e.target.checked)));
  $("snd-vol").addEventListener("input", (e) => { $("snd-vol-value").textContent = Math.round(e.target.value * 100) + "%"; });
  $("snd-vol").addEventListener("change", (e) => act(api().set_volume(Number(e.target.value))));
  $("snd-test").addEventListener("click", () => act(api().play_sound("hello")));

  $("swatches").replaceChildren(...SWATCHES.map(([name, hex]) => el("button", { class: "swatch", type: "button", "data-hex": hex,
    "aria-label": name, title: name, style: `background:#${hex}`, onclick: () => act(api().set_colour(hex)) })));
  $("colour-pick").addEventListener("change", (e) => act(api().set_colour(e.target.value.replace("#", "").toUpperCase())));
  $("colour-reset").addEventListener("click", () => act(api().set_colour(null)));

  $("apps-add").addEventListener("click", async () => {
    const path = await api().pick_folder();
    if (!path) return;
    const list = appsList;
    if (!list.some((a) => a.path.toLowerCase() === path.toLowerCase())) {
      list.push({ name: path.split(/[\\/]/).filter(Boolean).pop() || "Claude", path, source: "configured", account: "" });
    }
    appsDraft = list; drawApps(list);
  });
  $("apps-save").addEventListener("click", async () => {
    const list = appsList;
    const keep = list.filter((a) => a.source !== "usual" || !/^(main|work)$/i.test(a.name)).map((a) => ({ name: a.name, path: a.path }));
    const res = await api().save_apps(keep);
    toast(res);
    if (res.ok) { appsDraft = null; appsKey = ""; }
  });
  $("relay-copy").addEventListener("click", () => act(api().copy_relay_token()));
  $("relay-new").addEventListener("click", () => {
    if (confirm("Make a new relay token? The remote Lump disconnects until its computer gets the new one (run install.ps1 -Relay there again).")) act(api().renew_relay_token());
  });
  $("settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, values = {};
    for (const k of ["lump_host", "calendar_ics", "my_email", "slack_token", "slack_vips", "slack_vip_minutes"]) values[k] = f.elements[k].value;
    await act(api().save_settings(values));
  });
}

function start() { wire(); requestAnimationFrame(drawLump); poll(); }
if (window.pywebview && window.pywebview.api) start();
else window.addEventListener("pywebviewready", start, { once: true });
