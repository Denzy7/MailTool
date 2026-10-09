/* MailTool web front end. Plain JavaScript, no build step.
   Talks to the JSON API in mailtool/web/api.py and listens to /events (SSE). */
"use strict";

// ============================================================================ tiny DOM helpers
const $ = (sel, el = document) => el.querySelector(sel);

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "style") Object.assign(el.style, v);
      else if (k === "dataset") Object.assign(el.dataset, v);
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "for") el.htmlFor = v;
      else if (k in el && typeof v !== "string") el[k] = v;
      else if (k === "value" || k === "checked" || k === "disabled" || k === "selected") el[k] = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
  }
  add(el, kids);
  return el;
}

function add(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}

function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
const txt = (s) => document.createTextNode(s == null ? "" : String(s));

function debounce(fn, ms) {
  let t = null;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function humanSize(n) {
  if (n === null || n === undefined) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return Math.round(n / 1024) + " kB";
  return (n / 1048576).toFixed(1) + " MB";
}

function duration(secs) {
  secs = Math.max(0, Math.round(secs));
  return secs < 60 ? secs + "s" : Math.floor(secs / 60) + "m " + String(secs % 60).padStart(2, "0") + "s";
}

function check(label, opts = {}) {
  const input = h("input", { type: "checkbox", checked: !!opts.checked, disabled: !!opts.disabled,
    onchange: opts.onchange });
  const lab = h("label", { class: "check" + (opts.indent ? " indent" : "") + (opts.disabled ? " disabled" : "") },
    input, label);
  lab.input = input;
  return lab;
}

function radio(name, value, label, current, onchange) {
  const input = h("input", { type: "radio", name, value, checked: current === value, onchange });
  const lab = h("label", { class: "check" }, input, label);
  lab.input = input;
  return lab;
}

function field(type, value, props = {}) {
  return h("input", Object.assign({ type, value: value == null ? "" : String(value) }, props));
}

function select(options, value, props = {}) {
  const s = h("select", props);
  for (const [v, label] of options) s.append(h("option", { value: v, selected: v === value }, label));
  s.value = value;
  return s;
}

function form(rows) {
  const f = h("div", { class: "form" });
  for (const r of rows) {
    if (!r) continue;
    if (r.full) { f.append(h("div", { class: "full" }, r.full)); continue; }
    const id = "f" + Math.random().toString(36).slice(2, 9);
    if (r.input && r.input.tagName && !r.input.id) r.input.id = id;
    f.append(h("label", { for: r.input && r.input.id }, r.label), r.input);
    if (r.hint) f.append(h("div", { class: "hint" }, r.hint));
  }
  return f;
}

function card(title, desc, ...body) {
  return h("div", { class: "card" }, title ? h("h2", null, title) : null, desc ? h("p", { class: "desc" }, desc) : null,
    ...body);
}

function pill(status) {
  return h("span", { class: "status-pill st-" + status }, status);
}

// ============================================================================ toasts & dialogs
function toast(text, level = "info", ms = 5000) {
  const t = h("div", { class: "toast " + level, role: level === "error" ? "alert" : "status" }, text);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), level === "error" ? Math.max(ms, 8000) : ms);
}

function modal(title, body, buttons, opts = {}) {
  return new Promise((resolve) => {
    const back = h("div", { class: "modal-back" });
    const done = (v) => { back.remove(); document.removeEventListener("keydown", key); resolve(v); };
    const foot = h("footer");
    for (const b of buttons) {
      foot.append(h("button", { class: "btn" + (b.primary ? " primary" : "") + (b.danger ? " danger" : ""),
        type: b.submit ? "submit" : "button",
        onclick: async (e) => {
          e.preventDefault();
          if (b.action) {
            const r = await b.action();
            if (r === false) return;
            done(r === undefined ? b.value : r);
          } else done(b.value);
        } }, b.label));
    }
    const box = h("form", { class: "modal" + (opts.wide ? " wide" : ""), onsubmit: (e) => {
      e.preventDefault();
      const sub = foot.querySelector('button[type="submit"]');
      if (sub) sub.click();
    } },
      h("header", null, title), h("div", { class: "body" }, body), foot);
    back.append(box);
    back.close = () => done(null);
    const key = (e) => { if (e.key === "Escape") done(null); };
    document.addEventListener("keydown", key);
    back.addEventListener("mousedown", (e) => { if (e.target === back && !opts.sticky) done(null); });
    document.body.append(back);
    const first = box.querySelector("input, textarea, select") || box.querySelector("footer .primary");
    if (first) setTimeout(() => first.focus(), 30);
  });
}

function confirmBox(title, text, okLabel = "OK", danger = false) {
  return modal(title, h("p", null, text), [
    { label: "Cancel", value: false },
    { label: okLabel, value: true, primary: !danger, danger, submit: true },
  ]);
}

// ============================================================================ API
class ApiError extends Error {
  constructor(message, status, data) { super(message); this.status = status; this.data = data || {}; }
}

let loginPromise = null;
let passwordPromise = null;

async function api(method, url, body, opts = {}) {
  const init = { method, headers: { "X-MailTool": "1" }, credentials: "same-origin" };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(url, init);
  } catch (e) {
    throw new ApiError("Can't reach the MailTool server.", 0);
  }
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (r.status === 401) {
    await showLogin();
    return api(method, url, body, opts);
  }
  if (r.status === 409 && data && data.need === "password" && !opts.noRetry) {
    if (await askPassword()) return api(method, url, body, Object.assign({}, opts, { noRetry: true }));
    throw new ApiError("The mail password is needed for that.", 409, data);
  }
  if (r.status === 409 && data && data.need === "account") {
    go("settings", "account");
  }
  if (!r.ok) throw new ApiError((data && data.error) || r.statusText || "Request failed", r.status, data);
  return data;
}

async function act(fn) {
  try { return await fn(); } catch (e) {
    if (e && e.message) toast(e.message, "error");
    else console.error(e);
    return undefined;
  }
}

function uploadFile(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("PUT", url);
    x.setRequestHeader("X-MailTool", "1");
    x.setRequestHeader("Content-Type", "application/octet-stream");
    x.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
    x.onload = () => {
      let data = null;
      try { data = JSON.parse(x.responseText); } catch (e) { data = null; }
      if (x.status === 401) { showLogin().then(() => uploadFile(url, file, onProgress).then(resolve, reject)); return; }
      if (x.status >= 200 && x.status < 300) resolve(data);
      else reject(new ApiError((data && data.error) || "Upload failed (" + x.status + ")", x.status, data));
    };
    x.onerror = () => reject(new ApiError("Upload failed - connection lost.", 0));
    x.send(file);
  });
}

// ============================================================================ login & password
function showLogin(message) {
  if (loginPromise) return loginPromise;
  loginPromise = new Promise((resolve) => {
    const input = h("input", { type: "text", autocomplete: "one-time-code", autocapitalize: "characters",
      spellcheck: false, placeholder: "XXXX-XXXX", "aria-label": "Access code" });
    const err = h("div", { class: "err-text small" }, message || "");
    const btn = h("button", { class: "btn primary", type: "submit" }, "Open MailTool");
    const fileIn = h("input", { type: "file", accept: ".json,application/json", hidden: true });
    const fileBtn = h("button", { class: "btn", type: "button", onclick: () => fileIn.click() }, "Use a login file…");
    const f = h("form", { dataset: { ownDrop: "1" } },
      h("div", { class: "brand" }, h("img", { src: "/assets/icon_64.png", alt: "", width: 34, height: 34 }),
        h("span", null, "MailTool")),
      h("div", null, "Enter the access code shown where the MailTool server was started."),
      input, err, btn,
      h("div", { class: "login-or" }, h("span", null, "or")),
      fileBtn, fileIn,
      h("div", { class: "muted small login-hint" }, "Choose or drop a MailTool login file saved earlier."));
    const wrap = h("div", { class: "login" }, f);

    const signIn = async (body) => {
      btn.disabled = fileBtn.disabled = true;
      err.textContent = "";
      try {
        const r = await fetch("/login", { method: "POST", credentials: "same-origin",
          headers: { "X-MailTool": "1", "Content-Type": "application/json" }, body: JSON.stringify(body) });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || "Login failed");
        wrap.remove();
        loginPromise = null;
        resolve(true);
        const after = () => { if (data.via === "code") offerLoginFile(); };
        if (!booted) boot().then(after);
        else { connectEvents(true); reloadState().then(after); }
      } catch (ex) {
        err.textContent = ex.message;
        if (body.code !== undefined) input.select();
      } finally {
        btn.disabled = fileBtn.disabled = false;
      }
    };

    const useFile = (file) => {
      if (!file) return;
      if (file.size > 64 * 1024) { err.textContent = "That is not a MailTool login file."; return; }
      const reader = new FileReader();
      reader.onload = () => {
        let d = null;
        try { d = JSON.parse(reader.result); } catch (e) { d = null; }
        if (!d || !d.mailtool_login || typeof d.key !== "string") { err.textContent = "That is not a MailTool login file."; return; }
        if (d.server && d.server !== location.origin) {
          err.textContent = "Note: this file was made for " + d.server + " - trying it here anyway…";
        }
        signIn({ key: d.key });
      };
      reader.readAsText(file);
    };
    fileIn.addEventListener("change", () => { useFile(fileIn.files[0]); fileIn.value = ""; });
    f.addEventListener("dragover", (e) => { if (hasFiles(e)) { e.preventDefault(); f.classList.add("hot"); } });
    f.addEventListener("dragleave", () => f.classList.remove("hot"));
    f.addEventListener("drop", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      f.classList.remove("hot");
      useFile(e.dataTransfer.files[0]);
    });
    f.addEventListener("submit", (e) => { e.preventDefault(); signIn({ code: input.value }); });
    document.body.append(wrap);
    const m = location.hash.match(/code=([\w-]+)/);
    if (m) {
      input.value = m[1];
      history.replaceState(null, "", location.pathname + location.hash.replace(/[#&]?code=[\w-]+/, ""));
      setTimeout(() => f.requestSubmit(), 50);
    } else setTimeout(() => input.focus(), 30);
  });
  return loginPromise;
}

/** A name for this browser, e.g. "Chrome on Android" - only a default label for its login file. */
function deviceName() {
  const ua = navigator.userAgent;
  const browser = /Edg\//.test(ua) ? "Edge" : /OPR\//.test(ua) ? "Opera" : /Firefox\//.test(ua) ? "Firefox" :
    /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "Browser";
  const os = /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iOS" : /Windows/.test(ua) ? "Windows" :
    /Mac OS X/.test(ua) ? "macOS" : /Linux/.test(ua) ? "Linux" : "";
  return browser + (os ? " on " + os : "");
}

/** Make a login key on the server and hand it to the browser as a file to keep. */
async function downloadLoginFile(label) {
  const r = await api("POST", "/api/login-keys", { label });
  const data = { mailtool_login: 1, app: "MailTool", server: location.origin, label: r.info.label,
    created: r.info.created, key: r.key,
    note: "Anyone with this file can sign in to this MailTool server. Keep it private; revoke it in Settings › General." };
  const blob = new Blob([JSON.stringify(data, null, 2) + "\n"], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const safe = r.info.label.replace(/[^\w.-]+/g, "_").replace(/^_+|_+$/g, "") || "browser";
  const a = h("a", { href: url, download: "MailTool-login-" + safe + ".json" });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  return r.info;
}

async function offerLoginFile() {
  const label = field("text", deviceName(), { class: "wide", maxlength: 80 });
  const body = h("div", null,
    h("p", null, "Save a login file so you can sign in later without the access code - also after the server restarts with a new one."),
    h("label", { for: "lf-label" }, "Name for this login file"), Object.assign(label, { id: "lf-label" }),
    h("p", { class: "muted small" }, "Anyone who has the file can open MailTool, so keep it private. You can revoke it any time in Settings › General."));
  await modal("Save a login file?", body, [
    { label: "Not now", value: false },
    { label: "Download login file", primary: true, submit: true, action: async () => {
      try {
        const info = await downloadLoginFile(label.value);
        toast("Login file for " + info.label + " downloaded.", "ok");
        return true;
      } catch (e) { toast(e.message, "error"); return false; }
    } },
  ]);
}

function askPassword() {
  if (passwordPromise) return passwordPromise;
  const a = S.account || {};
  const pw = h("input", { type: "password", autocomplete: "current-password", class: "wide" });
  const remember = check("Remember it in this server's keyring", { checked: !!(S.password && S.password.remembered),
    disabled: !(S.password && S.password.keyring) });
  const err = h("div", { class: "err-text small" });
  const body = h("div", null,
    h("p", null, "Password for ", h("strong", null, a.username || "your account"), " on ", a.server || "the server", "."),
    pw, remember,
    S.password && !S.password.keyring ? h("p", { class: "muted small" },
      "No keyring on the server: the password is kept in the server's memory until it stops.") : null,
    h("p", { class: "muted small" }, "It is sent to the MailTool server only, and never written to its settings file."),
    err);
  passwordPromise = modal("Mail password", body, [
    { label: "Cancel", value: false },
    { label: "Use password", primary: true, submit: true, action: async () => {
      if (!pw.value) { err.textContent = "Enter the password."; return false; }
      try {
        const r = await api("POST", "/api/password", { password: pw.value, remember: remember.input.checked });
        S.password = r.password;
        return true;
      } catch (e) { err.textContent = e.message; return false; }
    } },
  ]).then((v) => { passwordPromise = null; return !!v; });
  return passwordPromise;
}

// ============================================================================ state, events, jobs
const S = {
  view: null, account: null, password: null, queue: null, jobs: new Map(), watchers: new Map(),
  logOpen: false, lastFetch: null, groups: 0, version: "",
};
let booted = false;
let es = null;

function connectEvents(force) {
  if (es && !force) return;
  if (es) es.close();
  es = new EventSource("/events");
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch (e) { return; }
    handleEvent(ev);
  };
  es.onerror = async () => {
    if (es.readyState === EventSource.CLOSED) {
      const r = await fetch("/api/ping", { credentials: "same-origin" }).then((x) => x.json()).catch(() => null);
      if (r && !r.authed) { es.close(); es = null; showLogin("Your session ended - enter the code again."); }
      else setTimeout(() => connectEvents(true), 3000);
    }
  };
}

function handleEvent(ev) {
  switch (ev.type) {
    case "log": addLog(ev); break;
    case "job": onJob(ev.job); break;
    case "queue":
      S.queue = ev.queue;
      updateBadges();
      if (S.view === "print") Views.print.queueChanged();
      break;
    case "library":
      Views.library.dirty = true;
      if (S.view === "library") Views.library.reload();
      break;
    case "settings":
      if (ev.account) S.account = ev.account;
      if (ev.theme) applyTheme(ev.theme);
      renderAccount();
      break;
    case "sort":
      if (S.view === "sort" && Views.sort.external) Views.sort.external();
      break;
    case "update": renderUpdate(ev.update); break;
    case "resync": reloadState(); break;
  }
}

function onJob(job) {
  const prev = S.jobs.get(job.id);
  if (prev && prev.state !== "running" && job.state === "running") return;   // a stale snapshot
  S.jobs.set(job.id, job);
  updateBar();
  if (job.state !== "running") {
    const label = { done: "finished", failed: "failed", cancelled: "stopped" }[job.state] || job.state;
    const lvl = { done: "ok", failed: "error", cancelled: "warn" }[job.state] || "info";
    setStatus(job.title + " " + label + (job.finished ? " in " + duration(job.finished - job.started) : ""), lvl);
    const cbs = S.watchers.get(job.id);
    if (cbs) {
      S.watchers.delete(job.id);
      for (const cb of cbs) { try { cb(job); } catch (e) { console.error(e); } }
    }
  }
  const v = Views[S.view];
  if (v && v.onJob) v.onJob(job);
}

/** Call cb(job) once the job finishes (also if it already has). */
function watchJob(job, cb) {
  if (!job) return;
  const prev = S.jobs.get(job.id);
  if (!prev || prev.state === "running") S.jobs.set(job.id, job);   // never replace a finished snapshot
  const known = S.jobs.get(job.id);
  if (known.state && known.state !== "running") { cb(known); return; }
  if (!S.watchers.has(job.id)) S.watchers.set(job.id, []);
  S.watchers.get(job.id).push(cb);
  // the job may have finished between the request and this call: ask once
  setTimeout(async () => {
    if (!S.watchers.has(job.id)) return;
    const j = await api("GET", "/api/jobs/" + job.id).catch(() => null);
    if (j && j.state !== "running") onJob(j);
  }, 1500);
  updateBar();
}

function runningJobs() {
  return [...S.jobs.values()].filter((j) => j.state === "running");
}

function updateBar() {
  const running = runningJobs();
  const wrap = $("#prog-wrap");
  if (!running.length) { wrap.hidden = true; return; }
  const j = running[running.length - 1];
  wrap.hidden = false;
  const prog = wrap.querySelector(".prog");
  const fill = $("#prog-fill");
  if (j.total) {
    prog.classList.remove("indet");
    fill.style.width = Math.min(100, (100 * j.current) / Math.max(1, j.total)) + "%";
    $("#prog-lbl").textContent = (j.note || "") + j.current + " / " + j.total;
  } else {
    prog.classList.add("indet");
    fill.style.width = "";
    $("#prog-lbl").textContent = j.note || "";
  }
  setStatus(j.title + "…" + (running.length > 1 ? " (+" + (running.length - 1) + " more)" : ""), "busy");
}

function setStatus(text, level = "info") {
  $("#status").textContent = text;
  $("#dot").className = "dot " + (["ok", "error", "warn", "busy"].includes(level) ? level : "");
}

function addLog(ev, replay) {
  const box = $("#log");
  const t = new Date((ev.ts || Date.now() / 1000) * 1000);
  const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
  box.append(h("div", { class: ev.level || "info" },
    h("span", { class: "t" }, t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })),
    ev.prefix ? h("span", { class: "p" }, "[" + ev.prefix + "]") : null, ev.text));
  while (box.childElementCount > 600) box.firstChild.remove();
  if (stick) box.scrollTop = box.scrollHeight;
  if (ev.level === "error" && !S.logOpen && !replay) toast(ev.text, "error");
}

function toggleLog(open) {
  S.logOpen = open === undefined ? !S.logOpen : open;
  $("#log").hidden = !S.logOpen;
  $("#log-toggle").textContent = S.logOpen ? "Hide log" : "Show log";
  try { localStorage.setItem("mt.log", S.logOpen ? "1" : "0"); } catch (e) { /* private mode */ }
  if (S.logOpen) $("#log").scrollTop = $("#log").scrollHeight;
}

// ============================================================================ shell
const NAV = [["fetch", "Fetch"], ["library", "Library"], ["sort", "Sort"], ["print", "Print"], ["settings", "Settings"]];

function buildNav() {
  const nav = $("#nav");
  for (const [key, label] of NAV) {
    nav.append(h("a", { href: "#/" + key, title: label, dataset: { key } },
      h("img", { src: "/assets/nav_" + key + "_off.png", alt: "" }),
      h("span", { class: "label" }, label), h("span", { class: "badge" })));
  }
}

function paintNav() {
  for (const a of $("#nav").children) {
    const on = a.dataset.key === S.view;
    a.classList.toggle("active", on);
    a.querySelector("img").src = "/assets/nav_" + a.dataset.key + "_" + (on ? "on" : "off") + ".png";
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
}

function setBadge(key, text) {
  const a = $("#nav").querySelector('[data-key="' + key + '"]');
  if (!a) return;
  const label = NAV.find((n) => n[0] === key)[1];
  a.querySelector(".badge").textContent = text || "";
  a.classList.toggle("has-badge", !!text);
  a.title = text ? label + " (" + text + ")" : label;
}

function toggleSidebar(open) {
  const collapsed = open === undefined ? !$("#app").classList.contains("side-collapsed") : !open;
  $("#app").classList.toggle("side-collapsed", collapsed);
  const b = $("#burger"), label = collapsed ? "Expand menu" : "Collapse menu";
  b.setAttribute("aria-expanded", String(!collapsed));
  b.setAttribute("aria-label", label);
  b.title = label;
  try { localStorage.setItem("mt.sidebar", collapsed ? "0" : "1"); } catch (e) { /* private mode */ }
}

function updateBadges() {
  const items = (S.queue && S.queue.items) || [];
  const n = items.filter((i) => ["queued", "staging", "converting", "ready"].includes(i.status)).length;
  setBadge("print", n ? String(n) : "");
  setBadge("sort", S.groups ? String(S.groups) : "");
}

function renderAccount() {
  const a = S.account || {};
  $("#acct").textContent = a.configured ? a.username + "\n" + a.server + " · " + (a.mailbox || "INBOX")
    : "No mail account yet\nSet one up in Settings";
}

function renderUpdate(u) {
  // quiet note beside the version: up to date, or a link to the newer release (nothing if the check failed)
  const el = $("#update");
  if (u && u.status === "available") {
    el.replaceChildren(" · ", h("a", { href: u.url, target: "_blank", rel: "noopener",
      title: "Open the v" + u.latest + " release page" }, "v" + u.latest + " available"));
  } else {
    el.textContent = u && u.status === "current" ? " · up to date" : "";
  }
}

function applyTheme(theme) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}

function go(view, sub) {
  const hash = "#/" + view + (sub ? "/" + sub : "");
  if (location.hash !== hash) location.hash = hash;
  else route();
}

function route() {
  const m = location.hash.match(/^#\/(\w+)(?:\/(\w+))?/);
  let key = m ? m[1] : null;
  if (!Views[key]) key = localStorage.getItem("mt.view") || "fetch";
  if (!Views[key]) key = "fetch";
  const sub = m ? m[2] : null;
  const v = Views[key];
  for (const b of document.querySelectorAll(".modal-back")) b.close();   // dialogs belong to the screen they came from
  if (S.view && Views[S.view].hide) Views[S.view].hide();
  S.view = key;
  try { localStorage.setItem("mt.view", key); } catch (e) { /* private mode */ }
  $("#title").textContent = v.title;
  $("#subtitle").textContent = v.subtitle;
  document.title = v.title + " - MailTool";
  paintNav();
  // a fresh container per visit: a slower, older render can only fill a detached element
  const root = h("div");
  $("#view").replaceChildren(root);
  v.render(root, sub);
}

async function reloadState() {
  const st = await api("GET", "/api/state");
  S.account = st.account;
  S.password = st.password;
  S.queue = st.queue;
  S.groups = st.groups;
  S.version = st.version;
  S.maxUpload = st.max_upload;
  for (const j of st.jobs) S.jobs.set(j.id, j);
  applyTheme(st.theme);
  renderAccount();
  $("#ver").textContent = "v" + st.version;
  renderUpdate(st.update);
  clear($("#log"));
  for (const ev of st.log) addLog(ev, true);
  updateBadges();
  updateBar();
  return st;
}

async function boot() {
  booted = true;
  try {
    await reloadState();
  } catch (e) {
    if (e.status !== 401) toast(e.message, "error");
    return;
  }
  let sidePref = null;
  try { sidePref = localStorage.getItem("mt.sidebar"); } catch (e) { /* private mode */ }
  toggleSidebar(sidePref !== "0");      // while still hidden, so a saved collapsed menu doesn't animate in
  $("#app").hidden = false;
  if (!$("#nav").childElementCount) {
    buildNav();
    $("#log-toggle").addEventListener("click", () => toggleLog());
    $("#burger").addEventListener("click", () => toggleSidebar());
    $("#stop").addEventListener("click", () => act(() => api("POST", "/api/jobs/cancel")));
    $("#logout").addEventListener("click", async () => {
      await fetch("/logout", { method: "POST", headers: { "X-MailTool": "1" }, credentials: "same-origin" });
      location.reload();
    });
    window.addEventListener("hashchange", route);
    setupGlobalDrop();
  }
  let logPref = null;
  try { logPref = localStorage.getItem("mt.log"); } catch (e) { /* private mode */ }
  toggleLog(logPref === "1");
  connectEvents();
  route();
}

// ============================================================================ drag & drop of files and folders
/** Files of a dropped folder in print order: like library/folders.py - a MailTool message folder
 * (has .message-uid) gives EMAILINFO first then the rest; MERGED_ copies and hidden files are skipped. */
async function readEntries(dir) {
  const reader = dir.createReader();
  const out = [];
  for (;;) {
    const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
    if (!batch.length) break;
    out.push(...batch);
  }
  return out;
}

const entryFile = (e) => new Promise((res, rej) => e.file(res, rej));

async function walkEntry(entry, depth) {
  if (entry.isFile) return [await entryFile(entry)];
  if (!entry.isDirectory) return [];
  const kids = (await readEntries(entry)).sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
  const isMessage = kids.some((k) => k.isFile && k.name === ".message-uid");
  const files = [];
  if (isMessage) {
    const info = [], rest = [];
    for (const k of kids) {
      if (!k.isFile || k.name.startsWith(".") || k.name.toUpperCase().startsWith("MERGED_")) continue;
      (k.name.toUpperCase().startsWith("EMAILINFO") ? info : rest).push(k);
    }
    for (const k of info.concat(rest)) files.push(await entryFile(k));
    return files;
  }
  for (const k of kids) {
    if (k.name.startsWith(".")) continue;
    if (k.isDirectory) { if (depth > 0) files.push(...await walkEntry(k, depth - 1)); }
    else if (!k.name.toUpperCase().startsWith("MERGED_")) files.push(await entryFile(k));
  }
  return files;
}

async function filesFromDataTransfer(dt) {
  // entries must be taken synchronously, before the first await
  const entries = [];
  if (dt.items) {
    for (const it of dt.items) {
      if (it.kind !== "file") continue;
      const e = it.webkitGetAsEntry ? it.webkitGetAsEntry() : null;
      entries.push(e);
    }
  }
  const plain = [...(dt.files || [])];
  if (!entries.length || !entries.some((e) => e && e.isDirectory)) return plain;
  const out = [];
  for (const e of entries) if (e) out.push(...await walkEntry(e, 2));
  return out;
}

/** Files from <input webkitdirectory>: grouped by folder, message folders EMAILINFO-first. */
function filesFromFolderInput(list) {
  const byDir = new Map();
  for (const f of list) {
    const rel = f.webkitRelativePath || f.name;
    const dir = rel.includes("/") ? rel.slice(0, rel.lastIndexOf("/")) : "";
    if (!byDir.has(dir)) byDir.set(dir, []);
    byDir.get(dir).push(f);
  }
  const out = [];
  for (const dir of [...byDir.keys()].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()))) {
    const files = byDir.get(dir).sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
    const isMessage = files.some((f) => f.name === ".message-uid");
    const keep = files.filter((f) => !f.name.startsWith(".") && !f.name.toUpperCase().startsWith("MERGED_"));
    if (isMessage) {
      const info = keep.filter((f) => f.name.toUpperCase().startsWith("EMAILINFO"));
      out.push(...info, ...keep.filter((f) => !info.includes(f)));
    } else out.push(...keep);
  }
  return out;
}

function hasFiles(e) {
  return e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files");
}

function setupGlobalDrop() {
  let depth = 0;
  const overlay = $("#drop-overlay");
  window.addEventListener("dragenter", (e) => {
    if (!hasFiles(e)) return;
    depth++;
    if (S.view !== "print" || !$(".dropzone, .queue-wrap")) overlay.hidden = false;
  });
  window.addEventListener("dragleave", (e) => {
    if (!hasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) overlay.hidden = true;
  });
  window.addEventListener("dragover", (e) => { if (hasFiles(e)) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; } });
  window.addEventListener("drop", async (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    overlay.hidden = true;
    if (e.target.closest && e.target.closest("[data-own-drop]")) return;   // handled by that element
    const files = await filesFromDataTransfer(e.dataTransfer);
    if (S.view !== "print") go("print");
    Views.print.upload(files);
  });
}

// ============================================================================ views
const Views = {};

// ---------------------------------------------------------------------------- Fetch
Views.fetch = {
  title: "Fetch mail",
  subtitle: "Download emails from your mailbox into the library. Nothing is ever marked as read.",
  async render(root) {
    const d = await act(() => api("GET", "/api/fetch"));
    if (!d || S.view !== "fetch") return;
    const v = {};
    if (!d.account.configured) {
      root.append(h("div", { class: "banner" }, h("span", { class: "grow" }, "No mail account is set up yet."),
        h("button", { class: "btn primary", type: "button", onclick: () => go("settings", "account") }, "Set up account")));
    }
    v.from_date = field("date", d.from_date); v.from_time = field("time", d.from_time);
    v.to_date = field("date", d.to_date); v.to_time = field("time", d.to_time);
    v.mailbox = field("text", d.mailbox, { size: 28 });
    root.append(card("Which emails", "Chosen by arrival time (the Received: header) in your timezone.",
      form([
        { label: "From", input: h("div", { class: "row" }, v.from_date, v.from_time) },
        { label: "To", input: h("div", { class: "row" }, v.to_date, v.to_time) },
        { label: "Mailbox", input: v.mailbox },
        { full: h("div", { class: "muted small" }, "Times are in " + d.timezone +
          " - change the timezone in Settings › General.") },
      ])));

    const sync = () => {
      const on = v.save_attachments.input.checked;
      for (const k of ["merge_pdfs", "log_without_attachments"]) {
        v[k].input.disabled = !on;
        v[k].classList.toggle("disabled", !on);
      }
    };
    v.save_attachments = check("Save attachments", { checked: d.save_attachments, onchange: () => sync() });
    v.merge_pdfs = check("Also merge each email's PDF attachments into one MERGED_ file",
      { checked: d.merge_pdfs, indent: true });
    v.log_without_attachments = check("Log emails that have no attachments",
      { checked: d.log_without_attachments, indent: true });
    v.emailinfo = check("Create an Email info PDF for each email",
      { checked: d.emailinfo, disabled: !d.emailinfo_available });
    v.export_csv = check("Export a CSV list of the emails", { checked: d.export_csv });
    v.sort_after = check("Sort them into groups when done", { checked: d.sort_after });
    root.append(card("What to do with them", null, h("div", { class: "checks" },
      h("div", null, v.save_attachments, h("span", { class: "muted small hint-inline" },
        "  into Library / <date> / <sender> - <subject> /")),
      v.merge_pdfs, v.log_without_attachments,
      h("div", null, v.emailinfo, h("span", { class: "muted small hint-inline" }, "  ",
        d.emailinfo_available ? "layout: " + d.layout.toLowerCase() + " · " : "needs reportlab: " + d.emailinfo_hint),
        d.emailinfo_available ? h("a", { href: "#/settings/emailinfo", class: "small" }, "options") : null),
      h("div", null, v.export_csv, h("span", { class: "muted small hint-inline" }, "  saved in the library's exports folder; download it when the fetch is done")),
      h("div", null, v.sort_after, h("span", { class: "muted small hint-inline" }, "  uses the groups on the Sort screen")),
    )));
    sync();

    v.library_dir = field("text", d.library_dir, { class: "wide", spellcheck: false });
    root.append(card("Save to", "A folder on the MailTool server. MailTool keeps its index in a hidden .mailtool folder inside it.",
      form([{ label: "Library folder", input: v.library_dir }])));

    const go_ = h("button", { class: "btn primary", type: "button" }, "Fetch mail");
    const result = h("span", { class: "muted" });
    const after = h("span", { class: "row" });
    root.append(h("div", { class: "actions sticky-actions" }, go_, result, after));
    this.els = { go: go_, result, after };
    this.showResult(S.lastFetch);
    if (runningJobs().some((j) => j.kind === "fetch")) go_.disabled = true;

    go_.addEventListener("click", () => act(async () => {
      const body = {};
      for (const k of ["from_date", "from_time", "to_date", "to_time", "mailbox", "library_dir"]) body[k] = v[k].value;
      for (const k of ["save_attachments", "merge_pdfs", "log_without_attachments", "emailinfo", "export_csv", "sort_after"])
        body[k] = v[k].input.checked;
      if (!(body.save_attachments || body.emailinfo || body.export_csv || body.sort_after)) {
        if (!await confirmBox("Nothing to save", "Nothing is ticked to save. Just add the emails to the library index (subject, sender, body text)?", "Fetch")) return;
      }
      go_.disabled = true;
      result.textContent = "";
      clear(after);
      try {
        const r = await api("POST", "/api/fetch", body);
        watchJob(r.job, (job) => {
          S.lastFetch = job;
          Views.library.dirty = true;
          if (S.view === "fetch") this.showResult(job);
        });
      } finally {
        if (!runningJobs().some((j) => j.kind === "fetch")) go_.disabled = false;
      }
    }));
  },
  showResult(job) {
    if (!job || !this.els || !this.els.go.isConnected) return;
    const { go: go_, result, after } = this.els;
    go_.disabled = false;
    clear(after);
    const r = job.result;
    if (!r || r.messages === undefined) {
      result.textContent = "Fetch " + job.state + " - see the log below.";
      return;
    }
    const bits = [r.messages + " email(s)"];
    if (r.attachments) bits.push(r.attachments + " attachment(s)");
    if (r.pdfs) bits.push(r.pdfs + " info PDF(s)");
    result.textContent = "Fetched " + bits.join(", ") + ".";
    if (r.messages) {
      after.append(
        h("button", { class: "btn small", type: "button", onclick: () => Views.library.showIds(r.message_ids) }, "Show in Library"),
        h("button", { class: "btn small", type: "button", onclick: () => Views.library.printIds(r.message_ids) }, "Print them"));
    }
    if (r.csv) after.append(h("a", { class: "btn small", href: r.csv.url + "?download=1" }, "Download CSV"));
  },
  onJob(job) {
    if (job.kind === "fetch" && job.state !== "running" && this.els && this.els.go.isConnected) this.els.go.disabled = false;
  },
};

// ---------------------------------------------------------------------------- Library
Views.library = {
  title: "Library",
  subtitle: "Everything you have fetched. Select emails to print, sort or open them.",
  dirty: true,
  q: "", group: "", from: "", to: "", onlyIds: null,
  sortCol: "received", sortDesc: true,
  rows: [], sel: new Set(), anchor: null, detailId: null,

  render(root) {
    const search = h("input", { type: "search", placeholder: "Search subject, sender or group", value: this.q,
      oninput: debounce(() => { this.q = search.value; this.reload(); }, 250) });
    this.groupSel = h("select", { onchange: () => { this.group = this.groupSel.value; this.reload(); } });
    const from = field("date", this.from, { onchange: () => { this.from = from.value; this.reload(); }, title: "From" });
    const to = field("date", this.to, { onchange: () => { this.to = to.value; this.reload(); }, title: "To" });
    root.append(h("div", { class: "filters" }, search, this.groupSel, h("span", { class: "muted" }, "From"), from,
      h("span", { class: "muted" }, "to"), to,
      h("span", { class: "grow" }),
      h("button", { class: "btn small", type: "button", onclick: () => this.reload() }, "Refresh")));
    this.note = h("div", { class: "note row", hidden: true });
    root.append(this.note);
    this.tbody = h("tbody");
    this.headRow = h("tr");
    this.checkAll = h("input", { type: "checkbox", "aria-label": "Select all", onchange: () => {
      if (this.checkAll.checked) this.rows.forEach((r) => this.sel.add(r.id)); else this.sel.clear();
      this.paintSel(); this.showDetail();
    } });
    const cols = [["received", "Received"], ["sender", "From"], ["subject", "Subject"], ["files", "Files"], ["group", "Group"]];
    this.headRow.append(h("th", { class: "w-check" }, this.checkAll));
    for (const [k, label] of cols) {
      this.headRow.append(h("th", { class: "sortable col-" + k + (k === "files" ? " c" : ""), onclick: () => {
        if (this.sortCol === k) this.sortDesc = !this.sortDesc; else { this.sortCol = k; this.sortDesc = k === "received"; }
        this.paintRows();
      } }, label));
    }
    this.list = h("div", { class: "table-wrap lib-list" },
      h("table", { class: "grid fixed lib-table" }, h("thead", null, this.headRow), this.tbody));
    this.detail = h("div", { class: "detail" });
    root.append(h("div", { class: "lib-layout" }, this.list, this.detail));
    this.foot = h("div", { class: "lib-foot" });
    root.append(this.foot);
    this.reload();
  },

  async reload() {
    if (S.view !== "library" || !this.tbody) return;
    this.dirty = false;
    const p = new URLSearchParams();
    if (this.q) p.set("q", this.q);
    if (this.group) p.set("group", this.group);
    if (this.from) p.set("from", this.from);
    if (this.to) p.set("to", this.to);
    if (this.onlyIds) p.set("ids", this.onlyIds.join(","));
    const d = await act(() => api("GET", "/api/library?" + p));
    if (!d || !this.tbody.isConnected) return;
    this.data = d;
    this.rows = d.rows;
    const ids = new Set(this.rows.map((r) => r.id));
    for (const id of [...this.sel]) if (!ids.has(id)) this.sel.delete(id);
    clear(this.groupSel);
    const opts = [["", "All emails"], ["__unsorted__", "Not sorted yet"], ["__unmatched__", "Unmatched"]]
      .concat(d.groups.map((g) => [g, g]));
    for (const [v, l] of opts) this.groupSel.append(h("option", { value: v }, l));
    if (!opts.some(([v]) => v === this.group)) this.group = "";
    this.groupSel.value = this.group;
    this.note.hidden = !this.onlyIds;
    clear(this.note);
    if (this.onlyIds) {
      this.note.append(h("span", null, "Showing the " + this.rows.length + " email(s) from the last fetch."),
        h("button", { class: "btn small", type: "button", onclick: () => { this.onlyIds = null; this.reload(); } }, "Show all"));
    }
    const st = d.stats || {};
    this.foot.textContent = d.exists
      ? this.rows.length + " shown · " + (st.total || 0) + " in library · " + (st.sorted || 0) + " sorted · " + d.root +
        (d.limited ? " · only the newest " + this.rows.length + " are listed - narrow the search" : "")
      : "The library folder " + d.root + " doesn't exist yet.";
    this.paintRows();
    this.showDetail();
  },

  sorted() {
    const k = this.sortCol;
    const val = (r) => (k === "files" ? r.files : String(r[k] || "").toLowerCase());
    const rows = [...this.rows].sort((a, b) => (val(a) < val(b) ? -1 : val(a) > val(b) ? 1 : b.id - a.id));
    return this.sortDesc ? rows.reverse() : rows;
  },

  paintRows() {
    const tb = clear(this.tbody);
    const rows = this.sorted();
    this.view = rows;
    if (!rows.length) {
      const empty = this.data && this.data.stats && this.data.stats.total ? "No emails match." :
        "Nothing here yet. Fetch some mail and it will show up here.";
      tb.append(h("tr", null, h("td", { colspan: 6, class: "empty" }, empty)));
    }
    for (const r of rows) {
      const cb = h("input", { type: "checkbox", checked: this.sel.has(r.id), "aria-label": "Select",
        onclick: (e) => { e.stopPropagation(); this.toggle(r.id, cb.checked); } });
      const grp = r.group ? h("span", { class: "group-tag" }, r.group) : r.unmatched ? h("span", { class: "unmatched" }, "unmatched") : "";
      const tr = h("tr", { class: "clickable" + (this.sel.has(r.id) ? " sel" : ""), dataset: { id: r.id },
        onclick: (e) => this.click(r.id, e), ondblclick: () => this.printIds([r.id]) },
        h("td", { class: "w-check" }, cb), h("td", { class: "nowrap" }, r.received),
        h("td", { class: "ellip", title: r.sender_email }, r.sender), h("td", { class: "ellip", title: r.subject }, r.subject || "(no subject)"),
        h("td", { class: "c" }, r.files || ""), h("td", null, grp));
      tb.append(tr);
    }
    this.paintSel();
  },

  click(id, e) {
    if (e.shiftKey && this.anchor !== null) {
      const ids = this.view.map((r) => r.id);
      const a = ids.indexOf(this.anchor), b = ids.indexOf(id);
      if (!e.ctrlKey && !e.metaKey) this.sel.clear();
      for (let i = Math.min(a, b); i <= Math.max(a, b); i++) this.sel.add(ids[i]);
    } else if (e.ctrlKey || e.metaKey) {
      if (this.sel.has(id)) this.sel.delete(id); else this.sel.add(id);
      this.anchor = id;
    } else {
      this.sel = new Set([id]);
      this.anchor = id;
    }
    this.paintSel();
    this.showDetail();
  },

  toggle(id, on) {
    if (on) this.sel.add(id); else this.sel.delete(id);
    this.anchor = id;
    this.paintSel();
    this.showDetail();
  },

  paintSel() {
    for (const tr of this.tbody.children) {
      const id = Number(tr.dataset.id);
      if (!id) continue;
      const on = this.sel.has(id);
      tr.classList.toggle("sel", on);
      const cb = tr.querySelector("input");
      if (cb) cb.checked = on;
    }
    this.checkAll.checked = this.rows.length > 0 && this.sel.size === this.rows.length;
    this.checkAll.indeterminate = this.sel.size > 0 && this.sel.size < this.rows.length;
  },

  selectedIds() {
    return this.view.filter((r) => this.sel.has(r.id)).map((r) => r.id);
  },

  actionButtons(ids) {
    return h("div", { class: "row" },
      h("button", { class: "btn primary", type: "button", onclick: () => this.printIds(ids) }, "Print"),
      h("button", { class: "btn", type: "button", onclick: () => this.sortIds(ids) }, "Sort"),
      h("button", { class: "btn small", type: "button", onclick: () => this.clearGroup(ids) }, "Clear group"),
      h("button", { class: "btn small danger", type: "button", onclick: () => this.forget(ids) }, "Remove from index"));
  },

  async showDetail() {
    const d = clear(this.detail);
    const ids = this.selectedIds();
    if (ids.length !== 1) {
      this.detailId = null;
      if (!ids.length) { d.append(h("p", { class: "muted" }, "Select an email to see its details. Ctrl/Shift-click selects several.")); return; }
      d.append(h("h2", null, ids.length + " emails selected"), h("p", { class: "muted" }, "Print sends each email's info page, then its attachments, to the print queue."),
        this.actionButtons(ids));
      return;
    }
    const id = ids[0];
    this.detailId = id;
    d.append(h("p", { class: "muted" }, "Loading…"));
    const m = await act(() => api("GET", "/api/library/" + id));
    if (!m || this.detailId !== id) return;
    clear(d);
    let meta = "From: " + (m.from_name ? m.from_name + " <" + m.from_email + ">" : m.from_email);
    if (m.reply_email && m.reply_email !== m.from_email) meta += "\nReply-To: " + m.reply_email;
    meta += "\nTo: " + m.to + "\nReceived: " + (m.received || "?") + (m.time_source === "received" ? "" : "  (from Date: header)");
    meta += "\n" + m.mailbox + " · UID " + m.uid;
    let grp;
    if (m.group) grp = h("div", { class: "group-tag" }, "Group: " + m.group + "   (" + m.group_stage + ": " + m.group_term + ")");
    else if (m.sorted) grp = h("div", { class: "unmatched" }, "Unmatched: " + m.sort_reason);
    else grp = h("div", { class: "muted" }, "Not sorted yet.");
    const files = h("div", { class: "files" });
    for (const f of m.files) {
      const label = f.name + (f.label ? "  (" + f.label + ")" : "");
      files.append(f.url ? h("a", { href: f.url, target: "_blank", rel: "noopener" }, h("span", null, label), h("span", { class: "muted nowrap" }, humanSize(f.size)))
        : h("span", { class: "na" }, h("span", null, label + "  (not downloaded)"), h("span", { class: "nowrap" }, humanSize(f.size))));
    }
    if (!m.files.length) files.append(h("span", { class: "na" }, "No attachments."));
    d.append(h("h2", null, m.subject || "(no subject)"), h("div", { class: "meta" }, meta), grp,
      h("div", { style: { marginTop: "10px" } }, this.actionButtons([id])),
      h("h3", null, "FILES"), files, h("h3", null, "MESSAGE"), h("pre", null, m.body || "(no text)"));
  },

  showIds(ids) {
    this.onlyIds = ids;
    this.group = "";
    go("library");
  },

  async printIds(ids) {
    if (!ids.length) { toast("Select one or more emails first.", "warn"); return; }
    const r = await act(() => api("POST", "/api/library/print", { ids }));
    if (!r) return;
    if (r.skipped) toast(r.skipped + " email(s) had nothing saved on disk to print.", "warn");
    if (r.added) toast(r.added + " file(s) added to the print queue.", "ok");
    else if (!r.skipped) toast("Those files are already in the print queue.");
    if (r.added || !r.skipped) go("print");
  },

  async sortIds(ids) {
    const r = await act(() => api("POST", "/api/library/sort", { ids }));
    if (r) watchJob(r.job, (job) => { if (job.result && job.result.matched !== undefined) toast(job.result.matched + " matched, " + job.result.unmatched + " unmatched.", "ok"); });
  },

  async clearGroup(ids) {
    await act(() => api("POST", "/api/library/clear-group", { ids }));
  },

  async forget(ids) {
    if (!await confirmBox("Remove from index", "Remove " + ids.length + " email(s) from the index? Files on disk are kept; fetching again re-adds them.", "Remove", true)) return;
    const r = await act(() => api("POST", "/api/library/forget", { ids }));
    if (r) { this.sel.clear(); toast(r.removed + " removed from the index.", "ok"); }
  },
};

// ---------------------------------------------------------------------------- Sort
Views.sort = {
  title: "Sort",
  subtitle: "Put emails into groups by keywords found in the subject, body or attached documents.",
  tab: "groups",
  editing: null,

  async render(root, sub) {
    if (sub) this.tab = sub;
    const d = await act(() => api("GET", "/api/sort"));
    if (!d || S.view !== "sort") return;
    this.d = d;
    S.groups = d.groups.length;
    updateBadges();
    this.root = root;
    const tabs = h("div", { class: "tabs", role: "tablist" });
    for (const [k, l] of [["groups", "Groups"], ["filters", "Attachment filters"], ["run", "Run"]]) {
      tabs.append(h("button", { type: "button", role: "tab", class: this.tab === k ? "active" : "",
        onclick: () => { this.tab = k; this.paint(); } }, l));
    }
    this.body = h("div");
    this.result = h("span", { class: "muted" });
    this.after = h("span", { class: "row" });
    this.goBtn = h("button", { class: "btn primary", type: "button", onclick: () => this.run() }, "Sort now");
    if (runningJobs().some((j) => j.kind === "sort")) this.goBtn.disabled = true;
    this.inputLbl = h("span", { class: "muted small" });
    root.append(tabs, this.body, h("div", { class: "actions sticky-actions" }, this.goBtn, this.inputLbl, this.result, this.after));
    this.paint();
    if (this.lastJob) this.showResult(this.lastJob);
  },

  paint() {
    const tabs = this.root.querySelector(".tabs").children;
    const keys = ["groups", "filters", "run"];
    for (let i = 0; i < tabs.length; i++) tabs[i].classList.toggle("active", keys[i] === this.tab);
    const b = clear(this.body);
    ({ groups: () => this.paintGroups(b), filters: () => this.paintFilters(b), run: () => this.paintRun(b) })[this.tab]();
    this.paintInput();
  },

  paintInput() {
    const d = this.d;
    let what;
    if (d.input === "library") what = "the library" + (d.library_from || d.library_to ? " (" + (d.library_from || "start") + " to " + (d.library_to || "now") + ")" : "");
    else what = d.csv_name || "a CSV file (choose it on the Run tab)";
    this.inputLbl.textContent = "Sorts " + what;
  },

  async save(patch) {
    const d = await act(() => api("PUT", "/api/sort", patch));
    if (d) { this.d = d; S.groups = d.groups.length; updateBadges(); this.paintInput(); }
    return d;
  },

  external() { /* settings changed in another tab: keep what's on screen, refresh the counts */ },

  // ---- groups
  paintGroups(b) {
    const d = this.d;
    const tbody = h("tbody");
    d.groups.forEach((g, i) => {
      tbody.append(h("tr", { class: "clickable" + (this.editing === i ? " sel" : ""), onclick: () => { this.editing = i; this.paint(); } },
        h("td", { class: "nowrap" }, g.group_name), h("td", null, g.desc), h("td", null, g.kws.join(", ")),
        h("td", { class: "hide-narrow" }, g.extra.join(" | "))));
    });
    if (!d.groups.length) tbody.append(h("tr", null, h("td", { colspan: 4, class: "empty" }, "No groups yet. Add one on the right.")));
    const move = (delta) => {
      const i = this.editing;
      if (i === null || i + delta < 0 || i + delta >= d.groups.length) return;
      const gs = d.groups.map((g) => ({ group_name: g.group_name, keywords: g.keywords }));
      [gs[i], gs[i + delta]] = [gs[i + delta], gs[i]];
      this.editing = i + delta;
      this.save({ groups: gs }).then(() => this.paint());
    };
    const importInput = h("input", { type: "file", accept: ".json,application/json", hidden: true, onchange: () => this.importGroups(importInput) });
    const left = h("div", null,
      h("div", { class: "row", style: { marginBottom: "8px" } }, h("span", { class: "muted small grow" }, "First matching group wins - order matters."),
        h("button", { class: "btn small icon", type: "button", title: "Move up", onclick: () => move(-1) }, "↑"),
        h("button", { class: "btn small icon", type: "button", title: "Move down", onclick: () => move(1) }, "↓"),
        h("button", { class: "btn small", type: "button", onclick: () => importInput.click() }, "Import…"), importInput,
        h("a", { class: "btn small", href: "/api/sort/groups/export" }, "Export")),
      h("div", { class: "table-wrap" }, h("table", { class: "grid" },
        h("thead", null, h("tr", null, h("th", null, "Group name"), h("th", null, "Descriptive name"),
          h("th", null, "Keywords (attachments + text)"), h("th", { class: "hide-narrow" }, "Text-only keywords"))), tbody)));

    const g = this.editing !== null ? d.groups[this.editing] : null;
    const name = field("text", g ? g.group_name : "", { class: "wide", placeholder: "e.g. G1/1/1/2026/01" });
    const kw = h("textarea", { rows: 7, spellcheck: false }, g ? g.keywords.join("\n") : "");
    const saveGroup = async () => {
      const n = name.value.trim();
      const kws = kw.value.split("\n").map((s) => s.trim()).filter(Boolean);
      if (!n) { toast("A group needs a name.", "warn"); return; }
      if (!kws.length) { toast("Enter the keywords: line 1 is the descriptive name, line 2 the comma-separated keywords.", "warn"); return; }
      const gs = d.groups.map((x) => ({ group_name: x.group_name, keywords: x.keywords }));
      let idx = this.editing !== null ? this.editing : gs.findIndex((x) => x.group_name === n);
      if (idx < 0 || idx === null) { gs.push({ group_name: n, keywords: kws }); idx = gs.length - 1; }
      else gs[idx] = { group_name: n, keywords: kws };
      if (gs.filter((x) => x.group_name === n).length > 1) { toast("Another group already has that name.", "warn"); return; }
      if (await this.save({ groups: gs })) { this.editing = idx; this.paint(); toast("Group saved.", "ok"); }
    };
    const del = async () => {
      if (this.editing === null) return;
      if (!await confirmBox("Delete group", "Delete group " + d.groups[this.editing].group_name + "?", "Delete", true)) return;
      const gs = d.groups.filter((_, i) => i !== this.editing).map((x) => ({ group_name: x.group_name, keywords: x.keywords }));
      this.editing = null;
      if (await this.save({ groups: gs })) this.paint();
    };
    const opt = (k) => check({ whole_word: "Whole-word match", case_sensitive: "Case sensitive" }[k],
      { checked: d[k], onchange: (e) => this.save({ [k]: e.target.checked }) });
    const right = card(null, null,
      h("div", { class: "stack" },
        h("label", { for: "g-name" }, "Group name"), Object.assign(name, { id: "g-name" }),
        h("label", { for: "g-kw" }, "Keywords"),
        h("div", { class: "muted small" }, "Line 1: descriptive name (goes in the CSVs).", h("br"),
          "Line 2: comma-separated keywords, e.g. jan, january.", h("br"),
          "Line 3+: one keyword per line, subject/body only.", h("br"), "Lines 1-2 are also searched in attachments."),
        Object.assign(kw, { id: "g-kw" }),
        h("div", { class: "row" }, h("button", { class: "btn primary", type: "button", onclick: saveGroup }, "Save group"),
          h("button", { class: "btn", type: "button", onclick: () => { this.editing = null; this.paint(); $("#g-name").focus(); } }, "New"),
          h("button", { class: "btn danger", type: "button", disabled: this.editing === null, onclick: del }, "Delete"))),
      h("h2", { style: { marginTop: "18px", fontSize: "14px" } }, "Matching"),
      h("div", { class: "checks" }, opt("whole_word"), opt("case_sensitive"),
        radio("prec", "keywords_first", "Keywords first, then group name", d.precedence, () => this.save({ precedence: "keywords_first" })),
        radio("prec", "groupname_first", "Group name first, then keywords", d.precedence, () => this.save({ precedence: "groupname_first" }))));
    b.append(h("div", { class: "groups-layout" }, left, right));
  },

  importGroups(input) {
    const f = input.files[0];
    input.value = "";
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => act(async () => {
      let data;
      try { data = JSON.parse(reader.result); } catch (e) { throw new Error("That file is not JSON."); }
      let replace = false;
      if (this.d.groups.length) {
        replace = await modal("Import groups", h("p", null, "Replace your " + this.d.groups.length + " group(s) with the imported ones, or add the new ones to the end?"),
          [{ label: "Cancel", value: null }, { label: "Add to the end", value: false }, { label: "Replace", value: true, primary: true }]);
        if (replace === null) return;
      }
      const d = await api("POST", "/api/sort/groups/import", { data, replace });
      this.d = d;
      S.groups = d.groups.length;
      updateBadges();
      this.paint();
      toast("Groups imported.", "ok");
    });
    reader.readAsText(f);
  },

  // ---- filters
  paintFilters(b) {
    const panel = (key, title) => {
      const list = h("ul", { class: "pat-list" });
      const items = this.d[key];
      items.forEach((p, i) => list.append(h("li", null, h("span", null, p),
        h("button", { class: "btn small", type: "button", onclick: async () => {
          const next = items.filter((_, j) => j !== i);
          if (await this.save({ [key]: next })) this.paint();
        } }, "Remove"))));
      if (!items.length) list.append(h("li", { class: "faint" }, "None"));
      const inp = field("text", "", { placeholder: "pattern, e.g. disclaimer or notes_*", class: "grow" });
      const addIt = async () => {
        const p = inp.value.trim();
        if (!p) return;
        if (await this.save({ [key]: items.concat([p]) })) this.paint();
      };
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addIt(); } });
      return card(title, null, list, h("div", { class: "row" }, inp, h("button", { class: "btn", type: "button", onclick: addIt }, "Add")));
    };
    b.append(h("p", { class: "muted" }, "Attachments matching an EXCLUDE pattern are skipped when searching for keywords. A file matching an INCLUDE pattern is always searched, even if it is excluded - e.g. exclude 'notes' but include 'notes_reports*'. Case-insensitive; plain text matches anywhere in the name, * and ? are wildcards."),
      h("div", { class: "pat-lists" }, panel("excluded", "Exclude"), panel("included", "Include (overrides exclude)")));
  },

  // ---- run
  paintRun(b) {
    const d = this.d;
    const lib = d.input === "library";
    const setInput = (v) => this.save({ input: v }).then(() => this.paint());
    const lf = field("date", d.library_from, { onchange: () => this.save({ library_from: lf.value }) });
    const lt = field("date", d.library_to, { onchange: () => this.save({ library_to: lt.value }) });
    const csvInput = h("input", { type: "file", accept: ".csv,text/csv", hidden: true, onchange: () => this.uploadCsv(csvInput.files[0]) });
    const csvDrop = h("div", { class: "dropzone", dataset: { ownDrop: "1" }, style: { padding: "18px" } },
      d.csv_exists ? h("div", null, "Current file: ", h("strong", null, d.csv_name)) : h("div", null, "No CSV chosen yet."),
      h("div", { class: "row", style: { justifyContent: "center", marginTop: "8px" } },
        h("button", { class: "btn", type: "button", onclick: () => csvInput.click() }, "Choose CSV…"), csvInput,
        h("span", { class: "muted small" }, "or drop it here")));
    csvDrop.addEventListener("dragover", (e) => { if (hasFiles(e)) { e.preventDefault(); csvDrop.classList.add("hot"); } });
    csvDrop.addEventListener("dragleave", () => csvDrop.classList.remove("hot"));
    csvDrop.addEventListener("drop", (e) => { e.preventDefault(); csvDrop.classList.remove("hot"); const f = e.dataTransfer.files[0]; if (f) this.uploadCsv(f); });
    const path = field("text", d.csv_path, { class: "wide", spellcheck: false, onchange: () => this.save({ csv_path: path.value }).then(() => this.paint()) });
    const eml = field("text", d.local_folder, { class: "wide", spellcheck: false, disabled: d.fallback_source !== "local",
      onchange: () => this.save({ local_folder: eml.value }) });
    const days = field("number", d.date_window_days, { min: 0, max: 30, class: "w-num", onchange: () => this.save({ date_window_days: days.value }) });
    const secs = field("number", d.timestamp_window_seconds, { min: 0, max: 86400, step: 30, class: "w-num", onchange: () => this.save({ timestamp_window_seconds: secs.value }) });
    const flag = (k, label) => check(label, { checked: d[k], onchange: (e) => this.save({ [k]: e.target.checked }) });
    const cacheLbl = h("span", { class: "muted small" }, d.cache_count === null ? "" : d.cache_count + " lookups cached");
    b.append(card("Emails to sort", null,
      h("div", { class: "radio-block" }, radio("input", "library", "From the library", d.input, () => setInput("library"))),
      h("div", { class: "indent-box" + (lib ? "" : " disabled") },
        h("div", { class: "row" }, h("span", null, "From"), lf, h("span", null, "to"), lt),
        h("div", { class: "muted small", style: { marginTop: "6px" } }, "Leave the dates empty to sort everything. Every email is matched by its exact UID, so nothing has to be searched for.")),
      h("div", { class: "radio-block" }, radio("input", "csv", "From a CSV file  (Sender, Email, Date Received, Subject, Body)", d.input, () => setInput("csv"))),
      h("div", { class: "indent-box" + (lib ? " disabled" : "") },
        csvDrop,
        form([
          { label: "…or a path on the server", input: path },
          { label: "Find originals in", input: h("div", { class: "row" },
            radio("src", "imap", "IMAP mailbox", d.fallback_source, () => this.save({ fallback_source: "imap" }).then(() => this.paint())),
            radio("src", "local", "Folder of .eml files (e.g. extracted Zimbra export)", d.fallback_source, () => this.save({ fallback_source: "local" }).then(() => this.paint()))),
            hint: "Rows with a UID column (MailTool's own CSV) are looked up exactly; the rest by subject + sender + time." },
          { label: ".eml folder (server)", input: eml },
          { label: "Lookup window", input: h("div", { class: "row" }, "search ±", days, "days, match time ±", secs, "seconds") },
          { label: "Cache", input: h("div", { class: "row" }, flag("use_cache", "Use the lookup cache"),
            h("button", { class: "btn small", type: "button", onclick: async () => {
              if (!await confirmBox("Clear cache", "Forget all cached CSV lookups?", "Clear")) return;
              if (await act(() => api("POST", "/api/sort/cache/clear"))) cacheLbl.textContent = "0 lookups cached";
            } }, "Clear cache"), cacheLbl) },
          { full: flag("offline", "Offline: never contact the mailbox, use cached lookups only") },
        ]))));
    const outDir = field("text", d.out_dir, { class: "wide", spellcheck: false, placeholder: "empty = a 'reports' folder in the library",
      onchange: () => this.save({ out_dir: outDir.value }) });
    b.append(card("Options", null, h("div", { class: "checks" },
      flag("search_attachments", "Search attachments (.pdf, .docx, .doc) when subject/body don't match"),
      flag("dedupe", "Also write matched_uniq.csv / unmatched_uniq.csv (one row per email address)")),
    form([{ label: "Reports folder (server)", input: outDir, hint: "The reports can be downloaded here when the sort is done." }])));
  },

  async uploadCsv(file) {
    if (!file) return;
    if (S.maxUpload && file.size > S.maxUpload) { toast(file.name + " is too large (limit " + humanSize(S.maxUpload) + ").", "error"); return; }
    await act(async () => {
      await uploadFile("/api/sort/csv?name=" + encodeURIComponent(file.name), file);
      this.d = await api("GET", "/api/sort");
      this.paint();
      toast(file.name + " uploaded.", "ok");
    });
  },

  async run() {
    const r = await act(() => api("POST", "/api/sort/run", {}));
    if (!r) return;
    this.goBtn.disabled = true;
    clear(this.after);
    this.result.textContent = "";
    watchJob(r.job, (job) => { this.lastJob = job; if (S.view === "sort") this.showResult(job); });
  },

  showResult(job) {
    if (!this.goBtn || !this.goBtn.isConnected) return;
    this.goBtn.disabled = runningJobs().some((j) => j.kind === "sort");
    clear(this.after);
    const r = job.result;
    if (!r || r.matched === undefined) { this.result.textContent = "Sort " + job.state + " - see the log."; return; }
    this.result.textContent = r.matched + " matched, " + r.unmatched + " unmatched.";
    for (const rep of r.reports || []) this.after.append(h("a", { class: "btn small", href: rep.url + "?download=1" }, rep.name));
  },

  onJob(job) {
    if (job.kind === "sort" && job.state !== "running" && this.goBtn && this.goBtn.isConnected) {
      this.goBtn.disabled = false;
      if (!this.lastJob || this.lastJob.id !== job.id) { this.lastJob = job; this.showResult(job); }
    }
  },
};

// ---------------------------------------------------------------------------- Print
Views.print = {
  title: "Print",
  subtitle: "Drop PDFs, Word documents, images or whole email folders. Download them as one PDF, or print on the server.",
  uploads: [],
  uploading: false,
  lastDownload: null,
  myJobs: new Set(),

  render(root) {
    const fileIn = h("input", { type: "file", multiple: true, hidden: true, onchange: () => { this.upload([...fileIn.files]); fileIn.value = ""; } });
    const dirIn = h("input", { type: "file", multiple: true, hidden: true, webkitdirectory: true,
      onchange: () => { this.upload(filesFromFolderInput([...dirIn.files])); dirIn.value = ""; } });
    root.append(h("div", { class: "row", style: { marginBottom: "10px" } },
      h("button", { class: "btn", type: "button", onclick: () => fileIn.click() }, "Add files…"),
      h("button", { class: "btn", type: "button", onclick: () => dirIn.click() }, "Add folder…"), fileIn, dirIn,
      h("span", { class: "grow" }),
      this.btnSort = h("button", { class: "btn small", type: "button", onclick: () => act(() => api("POST", "/api/queue/sort")) }, "Sort by name"),
      this.btnClear = h("button", { class: "btn small", type: "button", onclick: () => this.clearAll() }, "Clear")));
    this.uploadsBox = h("div", { class: "uploads" });
    this.tbody = h("tbody");
    this.wrap = h("div", { class: "table-wrap queue-wrap" }, h("table", { class: "grid fixed queue-table" },
      h("thead", null, h("tr", null, h("th", { class: "w-check" }), h("th", { class: "col-name" }, "File"),
        h("th", { class: "col-status" }, "Status"), h("th", { class: "c col-pages" }, "Pages"),
        h("th", { class: "c col-copies" }, "Copies"), h("th", { class: "r col-acts" }, ""))), this.tbody));
    this.zone = h("div", { class: "dropzone" }, h("strong", null, "Drop files or folders here"),
      "PDF, Word (.doc, .docx, .odt, .rtf) and images · or use Add files… · or Library › Print");
    for (const el of [this.wrap, this.zone]) {
      el.addEventListener("dragover", (e) => { if (hasFiles(e)) el.classList.add("hot"); });
      el.addEventListener("dragleave", () => el.classList.remove("hot"));
      el.addEventListener("drop", () => el.classList.remove("hot"));
    }
    root.append(this.uploadsBox, this.zone, this.wrap);

    this.merge = check("One print job (merge everything)", { onchange: (e) => act(() => api("PUT", "/api/print/options", { merge_batch: e.target.checked })) });
    this.clearAfter = check("Clear list after printing", { onchange: (e) => act(() => api("PUT", "/api/print/options", { clear_after: e.target.checked })) });
    this.printer = h("select", { onchange: () => act(() => api("PUT", "/api/print/options", { printer: this.printer.value })) });
    this.btnDownload = h("button", { class: "btn primary", type: "button", onclick: () => this.download() }, "Download PDF");
    this.btnPrint = h("button", { class: "btn", type: "button", onclick: () => this.printServer() }, "Print on server");
    this.btnCancel = h("button", { class: "btn", type: "button", onclick: () => act(() => api("POST", "/api/print/cancel")) }, "Cancel");
    this.dlLink = h("span", { class: "row" });
    this.serverBox = h("div", { class: "split" }, h("span", { class: "muted" }, "Printer"), this.printer, this.merge, this.clearAfter, this.btnPrint);
    this.noPrinter = h("span", { class: "muted small" });
    root.append(h("div", { class: "print-foot" },
      h("div", { class: "split" }, this.btnDownload, this.btnCancel, this.dlLink),
      h("span", { class: "grow" }), this.serverBox, this.noPrinter));
    this.loadPrinters();
    this.queueChanged();
    this.paintUploads();
    if (this.lastDownload) this.showDownload(this.lastDownload);
  },

  async loadPrinters(refresh) {
    const p = await act(() => api("GET", "/api/printers" + (refresh ? "?refresh=1" : "")));
    if (!p || !this.printer || !this.printer.isConnected) return;
    this.printers = p;
    clear(this.printer);
    this.printer.append(h("option", { value: "" }, "(system default" + (p.default ? ": " + p.default : "") + ")"));
    for (const n of p.names) this.printer.append(h("option", { value: n }, n));
    this.printer.value = p.selected && p.names.includes(p.selected) ? p.selected : "";
    this.queueChanged();
  },

  queueChanged() {
    if (!this.tbody || !this.tbody.isConnected) return;
    const q = S.queue || { items: [] };
    const busy = !!q.busy;
    const tb = clear(this.tbody);
    this.zone.hidden = q.items.length > 0;
    this.wrap.hidden = q.items.length === 0;
    q.items.forEach((it, idx) => {
      const ready = it.status === "ready" || it.status === "done";
      const copies = h("input", { type: "number", min: 1, max: 99, value: it.copies, class: "copies", disabled: busy,
        "aria-label": "Copies", onchange: () => act(() => api("PATCH", "/api/queue/" + it.id, { copies: copies.value })) });
      const tr = h("tr", { draggable: !busy, dataset: { id: it.id } },
        h("td", { class: "w-check" }, h("span", { class: "handle", title: "Drag to reorder" }, "⠿")),
        h("td", { class: "ellip", title: it.name }, it.name),
        h("td", null, pill(it.status), it.msg ? h("span", { class: "muted small" }, " " + it.msg) : null),
        h("td", { class: "c" }, it.pages_text),
        h("td", { class: "c" }, copies),
        h("td", { class: "r nowrap" },
          h("button", { class: "btn small icon", type: "button", title: "Up", disabled: busy || idx === 0, onclick: () => this.move(idx, -1) }, "↑"),
          h("button", { class: "btn small icon", type: "button", title: "Down", disabled: busy || idx === q.items.length - 1, onclick: () => this.move(idx, 1) }, "↓"),
          " ",
          h("button", { class: "btn small", type: "button", disabled: busy || !ready || !it.pages, onclick: () => this.pages(it.id) }, "Pages…"),
          ready ? h("a", { class: "btn small", href: "/api/queue/" + it.id + "/pdf", target: "_blank", rel: "noopener" }, "View") : null,
          h("button", { class: "btn small danger", type: "button", disabled: busy, title: "Remove", onclick: () => act(() => api("DELETE", "/api/queue/" + it.id)) }, "✕")));
      this.rowDrag(tr);
      tb.append(tr);
    });
    const active = q.items.filter((i) => ["queued", "staging", "converting", "ready"].includes(i.status)).length;
    this.btnDownload.disabled = busy || !active;
    this.btnPrint.disabled = busy || !active || !q.can_print;
    this.btnCancel.hidden = !busy;
    this.btnSort.disabled = this.btnClear.disabled = busy || !q.items.length;
    this.merge.input.checked = q.merge;
    this.clearAfter.input.checked = q.clear_after;
    this.serverBox.hidden = !q.can_print;
    this.noPrinter.textContent = q.can_print ? "" : "This server can't print (" + (q.print_hint || "no printing tools") + ") - use Download PDF.";
    this.btnDownload.textContent = busy && q.busy === "download" ? "Preparing PDF…" : "Download PDF";
  },

  rowDrag(tr) {
    tr.addEventListener("dragstart", (e) => {
      if (hasFiles(e)) return;
      this.dragId = tr.dataset.id;
      tr.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/x-mailtool-row", tr.dataset.id);
    });
    tr.addEventListener("dragend", () => { tr.classList.remove("dragging"); this.dragId = null; this.clearMarks(); });
    tr.addEventListener("dragover", (e) => {
      if (!this.dragId) return;
      e.preventDefault();
      this.clearMarks();
      const r = tr.getBoundingClientRect();
      tr.classList.add(e.clientY < r.top + r.height / 2 ? "drop-before" : "drop-after");
    });
    tr.addEventListener("drop", (e) => {
      if (!this.dragId) return;
      e.preventDefault();
      e.stopPropagation();
      const after = tr.classList.contains("drop-after");
      this.clearMarks();
      const ids = S.queue.items.map((i) => i.id).filter((i) => i !== this.dragId);
      let at = ids.indexOf(tr.dataset.id);
      if (after) at++;
      ids.splice(at, 0, this.dragId);
      act(() => api("POST", "/api/queue/order", { ids }));
    });
  },

  clearMarks() {
    for (const r of this.tbody.querySelectorAll(".drop-before, .drop-after")) r.classList.remove("drop-before", "drop-after");
  },

  move(idx, delta) {
    const ids = S.queue.items.map((i) => i.id);
    const j = idx + delta;
    if (j < 0 || j >= ids.length) return;
    [ids[idx], ids[j]] = [ids[j], ids[idx]];
    act(() => api("POST", "/api/queue/order", { ids }));
  },

  async clearAll() {
    if (!await confirmBox("Clear the queue", "Remove every file from the print queue?", "Clear", true)) return;
    act(() => api("DELETE", "/api/queue"));
  },

  // ---- uploads (one at a time, so they arrive in drop order)
  upload(files) {
    files = files.filter((f) => f && f.size !== undefined);
    if (!files.length) { toast("Nothing usable in that drop.", "warn"); return; }
    const big = files.filter((f) => S.maxUpload && f.size > S.maxUpload);
    for (const f of big) toast(f.name + " is too large (limit " + humanSize(S.maxUpload) + ").", "error");
    files = files.filter((f) => !big.includes(f));
    if (!files.length) return;
    for (const f of files) this.uploads.push({ file: f, name: f.name, progress: 0, state: "waiting" });
    this.paintUploads();
    this.pump();
  },

  async pump() {
    if (this.uploading) return;
    this.uploading = true;
    try {
      for (;;) {
        const u = this.uploads.find((x) => x.state === "waiting");
        if (!u) break;
        u.state = "uploading";
        this.paintUploads();
        try {
          await uploadFile("/api/upload?name=" + encodeURIComponent(u.name), u.file, (p) => { u.progress = p; this.paintUploads(); });
          u.state = "done";
        } catch (e) {
          u.state = "failed";
          toast(u.name + ": " + e.message, "error");
        }
        this.paintUploads();
      }
    } finally {
      this.uploading = false;
      this.uploads = this.uploads.filter((u) => u.state === "waiting" || u.state === "uploading");
      this.paintUploads();
    }
  },

  paintUploads() {
    if (!this.uploadsBox || !this.uploadsBox.isConnected) return;
    const box = clear(this.uploadsBox);
    const live = this.uploads.filter((u) => u.state !== "done");
    for (const u of live.slice(0, 8)) {
      const fill = h("span");
      fill.style.width = Math.round(u.progress * 100) + "%";
      box.append(h("div", { class: "upload-line" }, pill(u.state === "uploading" ? "uploading" : u.state),
        h("span", { class: "prog" }, fill), h("span", { class: "ellip grow" }, u.name),
        h("span", { class: "muted" }, humanSize(u.file.size))));
    }
    if (live.length > 8) box.append(h("div", { class: "muted small" }, "+ " + (live.length - 8) + " more waiting"));
  },

  // ---- output
  async download() {
    const r = await act(() => api("POST", "/api/print/download"));
    if (!r) return;
    this.myJobs.add(r.job.id);
    clear(this.dlLink);
    watchJob(r.job, (job) => {
      if (job.state === "done" && job.result && job.result.url) {
        this.lastDownload = job.result;
        const a = h("a", { href: job.result.url + "?download=1", download: job.result.name });
        document.body.append(a);
        a.click();
        a.remove();
        if (S.view === "print") this.showDownload(job.result);
      } else if (job.state === "failed") toast("Could not make the PDF: " + (job.error || "see the log"), "error");
    });
  },

  showDownload(r) {
    if (!this.dlLink || !this.dlLink.isConnected) return;
    clear(this.dlLink).append(h("a", { href: r.url + "?download=1", download: r.name }, "Save again"),
      h("a", { href: r.url, target: "_blank", rel: "noopener" }, "Open"),
      h("span", { class: "muted small" }, r.files + " file(s)" + (r.pages ? ", " + r.pages + " page(s)" : "") +
        (r.failed && r.failed.length ? " · skipped: " + r.failed.join(", ") : "")));
  },

  async printServer() {
    const q = S.queue;
    const n = q.items.filter((i) => ["queued", "staging", "converting", "ready"].includes(i.status)).length;
    const where = this.printer.value || "the default printer";
    if (!await confirmBox("Print on the server", "Send " + n + " file(s) to " + where + " on the MailTool server" + (q.merge ? " as one job" : ", one job per file") + "?", "Print")) return;
    const r = await act(() => api("POST", "/api/print/server", { printer: this.printer.value, merged: q.merge }));
    if (r) watchJob(r.job, () => {});
  },

  // ---- page picker
  async pages(id) {
    const it = (S.queue.items || []).find((i) => i.id === id);
    if (!it || !it.pages) return;
    const n = it.pages;
    let order = it.page_order ? [...it.page_order] : [...Array(n).keys()];
    const off = new Set(it.excluded);
    const grid = h("div", { class: "thumbs" });
    const count = h("span", { class: "muted" });
    let dragFrom = null;
    const paint = () => {
      clear(grid);
      order.forEach((p, pos) => {
        const cell = h("div", { class: "thumb" + (off.has(p) ? " off" : ""), draggable: true, title: "Click to include/exclude, drag to reorder" },
          h("img", { src: "/api/queue/" + id + "/thumb/" + p + "?w=300", alt: "Page " + (p + 1), loading: "lazy", draggable: false }),
          h("div", { class: "n" }, h("span", null, "Page " + (p + 1)), h("span", null, off.has(p) ? "excluded" : "")));
        cell.addEventListener("click", () => { if (off.has(p)) off.delete(p); else off.add(p); paint(); });
        cell.addEventListener("dragstart", (e) => { dragFrom = pos; cell.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", String(pos)); });
        cell.addEventListener("dragend", () => cell.classList.remove("dragging"));
        cell.addEventListener("dragover", (e) => { if (dragFrom !== null) { e.preventDefault(); cell.classList.add("drop-target"); } });
        cell.addEventListener("dragleave", () => cell.classList.remove("drop-target"));
        cell.addEventListener("drop", (e) => {
          e.preventDefault();
          e.stopPropagation();
          if (dragFrom === null || dragFrom === pos) return;
          const [m] = order.splice(dragFrom, 1);
          order.splice(pos, 0, m);
          dragFrom = null;
          paint();
        });
        grid.append(cell);
      });
      count.textContent = (n - off.size) + " of " + n + " page(s) will print" + (order.some((p, i) => p !== i) ? " · custom order" : "");
    };
    const rng = field("text", "", { placeholder: "e.g. 2-4, 7", size: 14 });
    const parse = (s) => {
      const out = new Set();
      for (const tok of s.split(/[,\s;]+/).filter(Boolean)) {
        const m = tok.match(/^(\d*)\s*-\s*(\d*)$/);
        let a, b;
        if (m && (m[1] || m[2])) { a = m[1] ? +m[1] : 1; b = m[2] ? +m[2] : n; }
        else if (/^\d+$/.test(tok)) a = b = +tok;
        else throw new Error("Not a page range: " + tok);
        if (a > b) [a, b] = [b, a];
        for (let i = Math.max(a, 1); i <= Math.min(b, n); i++) out.add(i - 1);
      }
      return out;
    };
    const bar = h("div", { class: "row" },
      h("button", { class: "btn small", type: "button", onclick: () => { off.clear(); paint(); } }, "Select all"),
      h("button", { class: "btn small", type: "button", onclick: () => { order.forEach((p) => off.add(p)); paint(); } }, "Select none"),
      h("span", { class: "muted" }, "Exclude pages"), rng,
      h("button", { class: "btn small", type: "button", onclick: () => { try { for (const p of parse(rng.value)) off.add(p); paint(); } catch (e) { toast(e.message, "warn"); } } }, "Exclude"),
      h("button", { class: "btn small", type: "button", onclick: () => { order.reverse(); paint(); } }, "Reverse"),
      h("button", { class: "btn small", type: "button", onclick: () => { order = [...Array(n).keys()]; paint(); } }, "Reset order"),
      h("span", { class: "grow" }), count);
    paint();
    await modal("Pages - " + it.name, h("div", null, bar, grid), [
      { label: "Cancel", value: null },
      { label: "Save", primary: true, action: async () => {
        if (off.size >= n) { toast("At least one page has to stay in.", "warn"); return false; }
        const r = await act(() => api("PATCH", "/api/queue/" + id, { excluded: [...off], page_order: order }));
        return r ? true : false;
      } },
    ], { wide: true, sticky: true });
  },

  onJob(job) {
    if (job.kind === "print" && this.tbody && this.tbody.isConnected) this.queueChanged();
  },
};

// ---------------------------------------------------------------------------- Settings
Views.settings = {
  title: "Settings",
  subtitle: "The mail account, where things are saved on the server, and how emails print.",
  tab: "account",

  async render(root, sub) {
    if (sub) this.tab = sub;
    const d = await act(() => api("GET", "/api/settings"));
    if (!d || S.view !== "settings") return;
    this.d = d;
    this.root = root;
    this.v = {};
    const tabs = h("div", { class: "tabs", role: "tablist" });
    this.panes = {};
    const defs = [["account", "Account"], ["general", "General"], ["emailinfo", "Email info PDF"], ["print", "Printing"], ["deps", "Dependencies"]];
    for (const [k, l] of defs) {
      tabs.append(h("button", { type: "button", role: "tab", dataset: { k }, onclick: () => this.show(k) }, l));
      this.panes[k] = h("div", { hidden: true });
    }
    this.saved = h("span", { class: "muted" });
    root.append(tabs, ...Object.values(this.panes),
      h("div", { class: "actions sticky-actions" },
        h("button", { class: "btn primary", type: "button", onclick: () => this.apply() }, "Save settings"),
        h("button", { class: "btn", type: "button", onclick: () => route() }, "Revert"), this.saved,
        h("span", { class: "grow" }), h("span", { class: "muted small hide-narrow" }, "Settings file: " + d.config_dir)));
    this.accountTab(this.panes.account);
    this.generalTab(this.panes.general);
    this.infoTab(this.panes.emailinfo);
    this.printTab(this.panes.print);
    this.depsTab(this.panes.deps);
    this.show(this.tab);
  },

  show(k) {
    this.tab = k;
    for (const b of this.root.querySelectorAll(".tabs button")) b.classList.toggle("active", b.dataset.k === k);
    for (const [key, p] of Object.entries(this.panes)) p.hidden = key !== k;
    if (k === "deps" && !this.depsLoaded) this.loadDeps();
  },

  accountTab(p) {
    const a = this.d.account;
    const v = this.v.account = {
      server: field("text", a.server, { size: 40, spellcheck: false, placeholder: "imap.example.com" }),
      port: field("number", a.port, { min: 1, max: 65535, class: "w-num" }),
      use_ssl: check("Use SSL/TLS (993)", { checked: a.use_ssl }),
      allow_self_signed: check("Allow a self-signed certificate (skip certificate checks - only for your own server on a network you trust)", { checked: a.allow_self_signed }),
      username: field("text", a.username, { size: 40, spellcheck: false, autocomplete: "username" }),
      mailbox: field("text", a.mailbox, { size: 30, list: "mailboxes" }),
      timeout: field("number", a.timeout, { min: 10, max: 600, step: 10, class: "w-num" }),
    };
    v.use_ssl.input.addEventListener("change", () => {
      const port = v.port.value.trim();
      if (v.use_ssl.input.checked && ["", "143"].includes(port)) v.port.value = "993";
      else if (!v.use_ssl.input.checked && ["", "993"].includes(port)) v.port.value = "143";
    });
    this.boxes = h("datalist", { id: "mailboxes" });
    p.append(card("Mail server", "IMAP access to your mailbox. MailTool only ever reads - the mailbox is opened read-only and nothing is marked as read.",
      form([
        { label: "IMAP server", input: v.server },
        { label: "Port", input: h("div", { class: "row" }, v.port, v.use_ssl) },
        { full: v.allow_self_signed },
        { label: "Email / username", input: v.username },
        { label: "Mailbox", input: h("div", { class: "row" }, v.mailbox, this.boxes, h("span", { class: "muted small" }, "Test the connection to list folders")) },
        { label: "Timeout (seconds)", input: v.timeout },
      ])));
    this.pwLbl = h("p");
    this.testLbl = h("p", { class: "muted" });
    p.append(card("Password", null, this.pwLbl,
      h("div", { class: "row" },
        h("button", { class: "btn", type: "button", onclick: () => this.enterPassword() }, "Enter password…"),
        h("button", { class: "btn", type: "button", onclick: () => this.forgetPassword() }, "Forget password"),
        h("button", { class: "btn primary", type: "button", onclick: () => this.test() }, "Test connection")),
      this.testLbl));
    this.pwStatus();
  },

  pwStatus() {
    const s = S.password = this.d.password;
    let t;
    if (!s.keyring) t = "You'll be asked for the password when MailTool needs it; the server keeps it in memory until it stops. Install 'keyring' on the server to let it remember the password securely: " + this.d.keyring_hint;
    else if (s.remembered) t = "The password is saved in the server's system keyring.";
    else if (s.have) t = "The password is kept for this server session only.";
    else t = "You'll be asked for the password when it is needed. Tick 'Remember' then to keep it in the server's keyring.";
    this.pwLbl.textContent = t + " It is never written to MailTool's settings file.";
  },

  async enterPassword() {
    if (!await this.apply(true)) return;
    if (!S.account.configured) { toast("Fill in the server and username first.", "warn"); return; }
    if (await askPassword()) { this.d.password = S.password; this.pwStatus(); }
  },

  async forgetPassword() {
    const r = await act(() => api("DELETE", "/api/password"));
    if (r) { this.d.password = r.password; this.pwStatus(); toast("Password forgotten.", "ok"); }
  },

  async test() {
    if (!await this.apply(true)) return;
    this.testLbl.className = "muted";
    this.testLbl.textContent = "Connecting…";
    const r = await act(() => api("POST", "/api/account/test"));
    if (!r) { this.testLbl.textContent = ""; return; }
    watchJob(r.job, (job) => {
      if (!this.testLbl.isConnected) return;
      api("GET", "/api/settings").then((d) => { this.d.password = d.password; this.pwStatus(); }).catch(() => {});
      if (job.state === "done" && job.result) {
        clear(this.boxes);
        for (const b of job.result.boxes) this.boxes.append(h("option", { value: b }));
        this.testLbl.className = "ok-text";
        this.testLbl.textContent = "Connected. '" + job.result.mailbox + "' has " + job.result.count + " message(s). " + job.result.boxes.length + " folder(s) available.";
      } else {
        this.testLbl.className = "err-text";
        this.testLbl.textContent = "Could not connect: " + (job.error || job.state);
      }
    });
  },

  generalTab(p) {
    const g = this.d.general;
    const v = this.v.general = {
      library_dir: field("text", this.d.library_dir, { class: "wide", spellcheck: false }),
      timezone: field("text", g.timezone, { size: 24, spellcheck: false }),
      theme: select([["system", "Follow the system"], ["light", "Light"], ["dark", "Dark"]], g.theme || "system"),
    };
    const tzOk = h("span", { class: "small" });
    const checkTz = debounce(async () => {
      const r = await api("GET", "/api/tz?name=" + encodeURIComponent(v.timezone.value)).catch(() => null);
      if (!r) return;
      tzOk.className = "small " + (r.ok ? "ok-text" : "err-text");
      tzOk.textContent = r.ok ? "✓  now " + r.now : "Not recognised";
    }, 300);
    v.timezone.addEventListener("input", checkTz);
    checkTz();
    p.append(card("Library", "Where fetched emails are saved, on the MailTool server. Moving it? Move the whole folder (including the hidden .mailtool folder) and point MailTool at the new place.",
      form([{ label: "Library folder", input: v.library_dir }])),
    card("Time", null, form([{ label: "Timezone", input: h("div", { class: "row" }, v.timezone, tzOk),
      hint: "An IANA name like Africa/Nairobi or Europe/London, or an offset like +03:00. Used for date ranges, folder dates and CSV times." }])),
    card("Appearance", null, form([{ label: "Theme", input: v.theme, hint: "Shared with the desktop app." }])));
    this.keysBox = h("div");
    p.append(card("Login files", "Files that sign a browser in without the access code. Revoke any you no longer trust - browsers signed in with it are signed out.",
      this.keysBox,
      h("div", { class: "row", style: { marginTop: "10px" } },
        h("button", { class: "btn", type: "button", onclick: async () => {
          const info = await act(() => downloadLoginFile(deviceName()));
          if (info) { toast("Login file for " + info.label + " downloaded.", "ok"); this.loadKeys(); }
        } }, "Download a login file for this browser"))));
    this.loadKeys();
  },

  async loadKeys() {
    const d = await act(() => api("GET", "/api/login-keys"));
    if (!d || !this.keysBox || !this.keysBox.isConnected) return;
    const box = clear(this.keysBox);
    if (!d.enabled) { box.append(h("p", { class: "muted" }, "Login files are switched off on this server.")); return; }
    if (!d.keys.length) { box.append(h("p", { class: "muted" }, "None yet.")); return; }
    const when = (s) => (s ? s.replace("T", " ").slice(0, 16) : "never");
    const tbody = h("tbody");
    for (const k of d.keys) {
      tbody.append(h("tr", null,
        h("td", null, k.label, k.id === d.current ? h("span", { class: "muted small" }, "  (this browser)") : null),
        h("td", { class: "nowrap hide-narrow" }, when(k.created)), h("td", { class: "nowrap" }, when(k.last_used)),
        h("td", { class: "r" }, h("button", { class: "btn small danger", type: "button", onclick: async () => {
          const self = k.id === d.current;
          if (!await confirmBox("Revoke login file", "Revoke the login file " + k.label + "? Browsers signed in with it are signed out" + (self ? " - including this one." : "."), "Revoke", true)) return;
          if (await act(() => api("DELETE", "/api/login-keys/" + k.id))) { if (self) location.reload(); else this.loadKeys(); }
        } }, "Revoke"))));
    }
    box.append(h("div", { class: "table-wrap" }, h("table", { class: "grid" },
      h("thead", null, h("tr", null, h("th", null, "Name"), h("th", { class: "hide-narrow" }, "Created"),
        h("th", null, "Last used"), h("th", { class: "r" }, ""))), tbody)));
  },

  infoTab(p) {
    const i = this.d.emailinfo;
    const pf = this.d.platform;
    const modes = Object.entries(pf.body_modes);
    const v = this.v.emailinfo = {
      header_left: field("text", i.header_left, { class: "wide" }),
      header_right: field("text", i.header_right, { class: "wide" }),
      body_mode: select(modes, pf.graphical ? (i.body_mode || "print") : "text"),
      remote_images: check("Load remote images (http/https images only). Loading them can tell the sender the email was opened.", { checked: i.remote_images }),
      quality: select(Object.entries(pf.quality), String(i.quality || 2)),
      workers: field("number", i.workers || 2, { min: 1, max: 32, class: "w-num" }),
    };
    const sync = () => {
      const m = v.body_mode.value;
      v.quality.disabled = m !== "image";
      v.remote_images.input.disabled = !(m === "print" || m === "image");
    };
    v.body_mode.addEventListener("change", sync);
    sync();
    p.append(card("Page header", "Printed at the top of every page of an EMAILINFO PDF.", form([
      { label: "Top left", input: v.header_left, hint: "Empty = your username." },
      { label: "Top right", input: v.header_right, hint: "Placeholders: " + pf.placeholders.map((x) => "<" + x + ">").join("  ") +
        ". A value starting with http(s):// becomes a clickable link, e.g. https://mail.example.com/modern/email/conversation/-<UID>/" },
    ])),
    card("Email body", null, form([
      { label: "Layout", input: v.body_mode, hint: "Printed = like your webmail's Print: selectable text, working links. Image = exact pixels. Plain text = fastest, no browser needed." +
        (pf.graphical ? "" : " Printed/Image need playwright + pillow on the server, then: playwright install chromium") },
      { full: v.remote_images },
      { label: "Image quality", input: v.quality },
      { label: "Parallel renders", input: v.workers, hint: "Each uses its own headless browser (~0.5 GB RAM); fewer are used if memory is short." },
      { full: h("div", { class: "muted small" }, "The sender's JavaScript never runs, and nothing but images is ever downloaded.") },
    ])));
  },

  printTab(p) {
    const pr = this.d.print;
    const pf = this.d.platform;
    const num = (k, lo, hi, step) => field("number", pr[k], { min: lo, max: hi, step: step || 1, class: "w-num" });
    const v = this.v.print = {
      word_engine: pr.word_engine || "libreoffice",
      max_word: num("max_word", 1, 8), max_parallel: num("max_parallel", 1, 32),
      paper: select([["A4", "A4"], ["Letter", "Letter"]], pr.paper || "A4"),
      copy_timeout: num("copy_timeout", 10, 7200, 10), convert_timeout: num("convert_timeout", 10, 7200, 10),
      print_timeout: num("print_timeout", 10, 7200, 10),
      soffice: field("text", pr.soffice || "", { class: "wide", spellcheck: false, placeholder: "found automatically" }),
      sumatra: field("text", pr.sumatra || "", { class: "wide", spellcheck: false, placeholder: "found automatically" }),
      temp_dir: field("text", pr.temp_dir || "", { class: "wide", spellcheck: false, placeholder: "system temp folder" }),
    };
    if (!pf.word && v.word_engine === "word") v.word_engine = "libreoffice";
    const lo = radio("weng", "libreoffice", "LibreOffice", v.word_engine, () => { v.word_engine = "libreoffice"; });
    const wd = radio("weng", "word", pf.word ? "Microsoft Word" : "Microsoft Word  (needs Windows, Word and pywin32 on the server)", v.word_engine, () => { v.word_engine = "word"; });
    wd.input.disabled = !pf.word;
    p.append(card("Word documents", null, h("div", { class: "checks" }, lo, wd),
      h("div", { class: "row" }, "Word instances at once", v.max_word, h("span", { class: "muted small" }, "1 = one after another (recommended)"))),
    card("Queue", null, form([
      { label: "Files prepared at once", input: v.max_parallel },
      { label: "Paper size for images", input: v.paper },
      { label: "Fetch timeout (s)", input: v.copy_timeout },
      { label: "Convert timeout (s)", input: v.convert_timeout },
      { label: "Print timeout (s)", input: v.print_timeout },
    ])),
    card("Programs on the server", "Leave empty to find them automatically.", form([
      pf.windows ? { label: "SumatraPDF.exe", input: v.sumatra } : null,
      { label: "LibreOffice (soffice)", input: v.soffice },
      { label: "Temp folder", input: v.temp_dir, hint: "Takes effect the next time MailTool starts." },
    ])));
  },

  depsTab(p) {
    this.depsList = h("ul", { class: "deps" }, h("li", { class: "muted" }, "Checking…"));
    this.depsInfo = h("span", { class: "muted small" });
    const chromium = h("button", { class: "btn", type: "button", hidden: true, onclick: async () => {
      const r = await act(() => api("POST", "/api/deps/chromium"));
      if (r) watchJob(r.job, () => this.loadDeps(true));
    } }, "Install Chromium for email bodies");
    this.chromiumBtn = chromium;
    p.append(card("What's installed on the server", "Everything optional only switches a feature on or off. Missing items show the command that installs them - run it on the MailTool server.",
      this.depsList,
      h("div", { class: "row", style: { marginTop: "10px" } },
        h("button", { class: "btn", type: "button", onclick: () => this.loadDeps(true) }, "Check again"), chromium,
        h("span", { class: "grow" }), this.depsInfo)));
  },

  async loadDeps(refresh) {
    const d = await act(() => api("GET", "/api/deps" + (refresh ? "?refresh=1" : "")));
    if (!d || !this.depsList.isConnected) return;
    this.depsLoaded = true;
    const ul = clear(this.depsList);
    let area = null;
    for (const c of d.caps) {
      if (c.area !== area) { area = c.area; ul.append(h("li", { class: "area" }, area)); }
      const mark = c.ok ? h("span", { class: "mark ok-text" }, "[ OK ]") : h("span", { class: "mark " + (c.level === "optional" ? "faint" : "err-text") }, c.level === "optional" ? "[ -- ]" : "[MISS]");
      ul.append(h("li", null, mark, h("span", null, c.label, c.ok && c.detail ? h("span", { class: "muted small" }, "  (" + c.detail + ")") : null),
        !c.ok && c.hint ? h("span", { class: "hint" }, "install: " + c.hint) : null));
    }
    this.chromiumBtn.hidden = !d.chromium_installable;
    this.depsInfo.textContent = "MailTool " + d.version + " · " + d.python;
  },

  collect() {
    const out = {};
    const a = this.v.account;
    out.account = { server: a.server.value, port: a.port.value, use_ssl: a.use_ssl.input.checked,
      allow_self_signed: a.allow_self_signed.input.checked, username: a.username.value, mailbox: a.mailbox.value,
      timeout: a.timeout.value };
    const g = this.v.general;
    out.general = { library_dir: g.library_dir.value, timezone: g.timezone.value, theme: g.theme.value };
    const i = this.v.emailinfo;
    out.emailinfo = { header_left: i.header_left.value, header_right: i.header_right.value, body_mode: i.body_mode.value,
      remote_images: i.remote_images.input.checked, quality: i.quality.value, workers: i.workers.value };
    const p = this.v.print;
    out.print = { word_engine: p.word_engine };
    for (const k of ["max_word", "max_parallel", "paper", "copy_timeout", "convert_timeout", "print_timeout", "soffice", "sumatra", "temp_dir"]) out.print[k] = p[k].value;
    return out;
  },

  async apply(quiet) {
    const r = await act(() => api("PUT", "/api/settings", this.collect()));
    if (!r) return false;
    this.d.password = r.password;
    this.pwStatus();
    S.account = { configured: !!(r.account.server && r.account.username), username: r.account.username,
      server: r.account.server, mailbox: r.account.mailbox };
    renderAccount();
    applyTheme(r.general.theme);
    for (const w of r.warnings || []) toast(w, "warn");
    if (!quiet) {
      this.saved.textContent = "Saved.";
      setStatus("Settings saved", "ok");
      setTimeout(() => { if (this.saved.isConnected) this.saved.textContent = ""; }, 4000);
      if (this.depsLoaded) this.loadDeps(true);
    }
    return true;
  },
};

// ============================================================================ start
// a file dropped outside a drop zone must never make the browser navigate away to it
window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener("drop", (e) => { if (hasFiles(e)) e.preventDefault(); });

document.addEventListener("DOMContentLoaded", async () => {
  try {
    const r = await fetch("/api/ping", { credentials: "same-origin" }).then((x) => x.json());
    if (r.authed && !/code=/.test(location.hash)) boot();
    else showLogin();
  } catch (e) {
    document.body.append(h("p", { style: { padding: "20px" } }, "Can't reach the MailTool server."));
  }
});
