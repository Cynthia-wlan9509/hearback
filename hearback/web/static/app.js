"use strict";
/* SWLink console: vanilla JS, no build step. All server and user text goes through textContent (see h()).
   The texts live in i18n.js (loaded first). */

const NAV_ICON = { conversations: "chat", approvals: "check", supporters: "heart", llm: "model", agents: "persona", settings: "settings" };
const UNDER_SETTINGS = ["conversations", "approvals", "botmsgs", "supporters", "rules", "logs"];
const NO_HOWTO = ["inbox", "llm", "agents", "settings"];
const MODES = ["off", "draft", "auto"];
const PORTAL = "https://discord.com/developers/applications";

let LANG = "en";
let KEY = "";
let THEME = "light";
let PAGE = "inbox";
let FORUM = "";                  // the forum the inbox is narrowed to, if any
let STATUS = null;
let SIDE_SHOWN = "";             // what the sidebar last drew
let RENDER_SEQ = 0;
let POLL = null;                 // timer of the running AI review, if any
const HOWTO_HIDDEN = {};         // pages whose strip was opened or closed (kept even when storage is blocked)
let SPACES = null;               // /api/workspaces when SWLink has more than one server folder
const $ = (id) => document.getElementById(id);

function store(op, k, v) {
  try {
    if (op === "get") return sessionStorage.getItem(k) || localStorage.getItem(k);
    if (op === "setS") sessionStorage.setItem(k, v);
    if (op === "setL") localStorage.setItem(k, v);
  } catch (e) { /* storage may be blocked */ }
  return null;
}

function t(key, vars) {
  let s = (I18N[LANG] && I18N[LANG][key]) || I18N.en[key] || key;
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.split("{" + k + "}").join(String(v));
  return s;
}
function hasKey(key) { return !!(I18N[LANG] && I18N[LANG][key]); }

/* DOM builder: text always goes in as text nodes, never as HTML strings */
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  let pending;
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "value") pending = v;
    else if (k === "checked" || k === "disabled" || k === "selected") el[k] = !!v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  const add = (c) => {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) c.forEach(add);
    else el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  };
  kids.forEach(add);
  if (pending !== undefined) el.value = pending;
  return el;
}
/* line icons: fixed markup written here, never text from a server or a user */
const ICONS = {
  inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1Z"/>',
  bug: '<path d="m8 2 1.9 1.9"/><path d="M14.1 3.9 16 2"/><path d="M9 7.1V6a3 3 0 1 1 6 0v1.1"/><path d="M12 20c-3.3 0-6-2.7-6-6v-3a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v3c0 3.3-2.7 6-6 6Z"/><path d="M12 20v-9"/><path d="M6.5 9C4.6 8.8 3 7.1 3 5"/><path d="M6 13H2"/><path d="M3 21c0-2.1 1.7-3.9 3.8-4"/><path d="M20.97 5c0 2.1-1.6 3.8-3.5 4"/><path d="M22 13h-4"/><path d="M17.2 17c2.1.1 3.8 1.9 3.8 4"/>',
  idea: '<path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5"/><path d="M9 18h6"/><path d="M10 22h4"/>',
  image: '<rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
  chat: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2Z"/>',
  heart: '<path d="M19 14c1.5-1.5 3-3.2 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.8 0-3 .5-4.5 2-1.5-1.5-2.7-2-4.5-2A5.5 5.5 0 0 0 2 8.500c0 2.3 1.5 4 3 5.500l7 7Z"/>',
  model: '<rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/>',
  persona: '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
  settings: '<path d="M21 4h-7"/><path d="M10 4H3"/><path d="M21 12h-9"/><path d="M8 12H3"/><path d="M21 20h-5"/><path d="M12 20H3"/><path d="M14 2v4"/><path d="M8 10v4"/><path d="M16 18v4"/>',
  side: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.9 4.9 1.4 1.4"/><path d="m17.7 17.7 1.4 1.4"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.3 17.7-1.4 1.4"/><path d="m19.1 4.9-1.4 1.4"/>',
  refresh: '<path d="M3 12a9 9 0 0 1 15-6.700L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.700L3 16"/><path d="M8 16H3v5"/>',
  spark: '<path d="M9.9 15.500A2 2 0 0 0 8.5 14.100L2.4 12.500a.5.5 0 0 1 0-1l6.1-1.600a2 2 0 0 0 1.4-1.400l1.6-6.100a.5.5 0 0 1 1 0l1.6 6.100a2 2 0 0 0 1.4 1.400l6.1 1.600a.5.5 0 0 1 0 1l-6.1 1.600a2 2 0 0 0-1.4 1.400l-1.6 6.100a.5.5 0 0 1-1 0Z"/>',
  out: '<path d="M7 7h10v10"/><path d="M7 17 17 7"/>',
  back: '<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>',
  star: '<path d="M12 2l3.1 6.3 6.9 1-5 4.9 1.2 6.8-6.2-3.200L5.8 21 7 14.2 2 9.300l6.9-1z"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
};
function icon(name, cls) {
  const doc = new DOMParser().parseFromString('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" aria-hidden="true">' + ICONS[name] + "</svg>", "image/svg+xml");
  const svg = document.importNode(doc.documentElement, true);
  svg.setAttribute("class", "i" + (cls ? " " + cls : ""));
  return svg;
}
function cat() { return h("img", { class: "cat", src: "/swlink.svg", alt: "" }); }
function trashIcon() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  for (const [k, v] of Object.entries({ viewBox: "0 0 24 24", width: "16", height: "16", fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round" })) svg.setAttribute(k, v);
  const p = document.createElementNS(ns, "path");
  p.setAttribute("d", "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3");
  svg.appendChild(p);
  return svg;
}
function lockIcon() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  for (const [k, v] of Object.entries({ viewBox: "0 0 24 24", width: "14", height: "14", fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round", class: "lock" })) svg.setAttribute(k, v);
  const body = document.createElementNS(ns, "rect");
  for (const [k, v] of Object.entries({ x: "5", y: "11", width: "14", height: "10", rx: "2" })) body.setAttribute(k, v);
  const arc = document.createElementNS(ns, "path");
  arc.setAttribute("d", "M8 11V7a4 4 0 0 1 8 0v4");
  svg.appendChild(body);
  svg.appendChild(arc);
  return svg;
}
function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

function toast(msg, bad, action) {
  const el = h("div", { class: "toast" + (bad ? " err" : "") }, msg,
    action ? h("button", { onclick: () => { el.remove(); action.run(); } }, action.label) : null);
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), bad ? 6000 : action ? 6000 : 3000);
}
function fail(e) {
  if (e && e.message === "locked") return;
  const m = (e && e.message) || "";
  toast(hasKey("err." + m) ? t("err." + m) : (m || t("common.error")), true);
}

function modal(title, body, actions, wide) {
  const root = $("modal-root");
  const close = () => clear(root);
  const bar = h("div", { class: "modal-actions" }, (actions || []).map((a) =>
    h("button", { class: "btn " + (a.cls || ""), onclick: async () => { if (a.run) { if ((await a.run()) === false) return; } close(); } }, a.label)));
  const back = h("div", { class: "modal-back", onmousedown: (ev) => { if (ev.target === back) close(); } },
    h("div", { class: "modal" + (wide ? " wide" : ""), role: "dialog" }, h("h2", null, title), body, bar));
  clear(root).appendChild(back);
  const first = back.querySelector("input,textarea,select");
  if (first) first.focus();
  return close;
}
function confirmBox(title, text, okLabel, danger) {
  return new Promise((resolve) => {
    modal(title, h("div", { class: "sub" }, text), [
      { label: t("common.cancel"), run: () => { resolve(false); } },
      { label: okLabel || t("common.confirm"), cls: danger ? "danger solid" : "primary", run: () => { resolve(true); } },
    ]);
  });
}

async function api(method, path, body) {
  const opt = { method, headers: { "X-SWLink-Key": KEY } };
  if (method !== "GET") { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body || {}); }
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch (e) { /* not JSON */ }
  if (r.status === 403 && !(data && data.error === "no_access")) { showLocked(); throw new Error("locked"); }
  if (!r.ok) { const err = new Error((data && data.error) || "HTTP " + r.status); err.data = data || {}; throw err; }
  return data;
}

function showLocked() {
  $("app").classList.add("hidden");
  $("locked").classList.remove("hidden");
  $("locked-title").textContent = t("locked.title");
  $("locked-text").textContent = t("locked.text");
}

function fmtTime(s) {
  if (!s) return "";
  const d = new Date(s);
  return isNaN(d) ? s : d.toLocaleString(LANG === "zh" ? "zh-CN" : "en-GB", { hour12: false });
}
function safeImg(url) { return typeof url === "string" && url.startsWith("https://cdn.discordapp.com/") ? url : ""; }
function modeLabel(m) { return t("mode." + m); }
function modeSelect(value, onchange) {
  return h("select", { value, onchange: (e) => onchange(e.target.value) }, MODES.map((m) => h("option", { value: m }, modeLabel(m))));
}
function empty(title, text, btn) { return h("div", { class: "card empty" }, cat(), h("h3", null, title), h("div", null, text), btn); }
function go(page) { location.hash = "#/" + page; }
function goBtn(page, label, primary) { return h("button", { class: "btn" + (primary === false ? "" : " primary"), onclick: () => go(page) }, label); }
// 「润色」: the model rewrites what you typed in the box; 「撤回」 puts your words back until you type again
function polishButtons(ta, extra, small) {
  let before = null;
  const cls = "btn" + (small ? " small" : "");
  const undo = h("button", { class: cls, hidden: true, onclick: () => { if (before === null) return; ta.value = before; reset(); ta.dispatchEvent(new Event("input")); } }, t("polish.undo"));
  const reset = () => { before = null; undo.hidden = true; };
  const btn = h("button", { class: cls, title: t("polish.hint"), onclick: async () => {
    if (!ta.value.trim()) { toast(t("polish.empty"), true); ta.focus(); return; }
    btn.disabled = true; const label = btn.textContent; btn.textContent = t("polish.working");
    try {
      const r = await api("POST", "/api/polish", Object.assign({ text: ta.value }, extra ? extra() : {}));
      const old = ta.value; ta.value = r.text; ta.dispatchEvent(new Event("input")); before = old; undo.hidden = false;
    } catch (e) { fail(e); } finally { btn.disabled = false; btn.textContent = label; }
  } }, t("polish.btn"));
  ta.addEventListener("input", (ev) => { if (ev.isTrusted) reset(); });
  return [btn, undo];
}
function field(label, input) { return h("label", { class: "field" }, h("span", null, label), input); }
function fieldHint(label, input, hint) { return h("label", { class: "field" }, h("span", null, label), input, h("div", { class: "sub small-text hint" }, hint)); }
function stopPolling() { if (POLL) { clearInterval(POLL); POLL = null; } }

/* ---------- shell ---------- */
function renderShell() {
  $("page").classList.toggle("fill", PAGE === "conversations");      // the chat page uses the whole window height
  $("page").classList.remove("inbox");
  renderSide();
  renderDemo();
  renderHowto();
}
function navItem(id, ic, label, active, onclick, count) {
  return h("button", { class: "nav-item" + (active ? " active" : ""), onclick },
    icon(ic), h("span", { class: "grow" }, label), count === undefined ? null : h("em", { class: count ? "" : "zero" }, String(count)));
}
function renderSide() {
  const side = clear($("side"));
  const inbox = (STATUS && STATUS.inbox) || { todo: 0, forums: [] };
  const page = (p) => navItem(p, NAV_ICON[p], t("nav." + p), p === PAGE || (p === "settings" && UNDER_SETTINGS.includes(PAGE) && !optional().includes(PAGE)), () => go(p),
    p === "approvals" ? (STATUS && STATUS.waiting) || 0 : undefined);
  side.appendChild(h("div", { class: "brand" }, cat(), h("b", null, "SWLink")));
  side.appendChild(h("div", { class: "nav-group" }, t("nav.feedback")));
  side.appendChild(navItem("all", "inbox", t("nav.inbox"), PAGE === "inbox" && !FORUM, () => go("inbox"), inbox.todo));
  inbox.forums.forEach((f) => side.appendChild(navItem(f.id, KIND_ICON[forumKind(f.name)], forumName(f), PAGE === "inbox" && FORUM === f.id, () => go("inbox/" + f.id), f.todo)));
  const extra = optional();
  if (extra.length) {
    side.appendChild(h("div", { class: "nav-group" }, t("nav.bot")));
    extra.forEach((p) => side.appendChild(page(p)));
  }
  side.appendChild(h("div", { class: "nav-group" }, t("nav.system")));
  ["llm", "agents", "settings"].forEach((p) => side.appendChild(page(p)));
  side.appendChild(serverFoot());
}
/* chat, supporters and waiting replies only show where this server uses them; Settings always leads to all of them */
function optional() {
  const s = STATUS || {};
  return [s.chat ? "conversations" : null, s.supporters ? "supporters" : null, s.waiting ? "approvals" : null].filter(Boolean);
}
/* the bottom row: this server and its bot. Server folders of this bot ([swlink] workspaces) re-render every page;
   SWLinks of other bots ([swlink] others) open in this tab (started first when they are not running) */
function serverFoot() {
  const b = (STATUS && STATUS.bot) || { state: "offline", reason: "" };
  const name = (STATUS && STATUS.server) || "SWLink";
  const who = h("span", { class: "foot-who" }, h("b", null, name),
    h("small", null, h("span", { class: "dot " + b.state }), (b.bot_name ? b.bot_name + " " : "") + t("bot." + b.state)));
  const avatar = h("span", { class: "foot-avatar" }, cat());
  if (!SPACES || (SPACES.items.length < 2 && !SPACES.broken.length)) {
    return h("button", { class: "foot", title: botHint(b), onclick: () => go("settings") }, avatar, who);
  }
  const sel = h("select", { "aria-label": t("ws.label"), onchange: (ev) => openSpace(ev.target.value) },
    SPACES.items.map((w) => h("option", { value: w.id, selected: w.current },
      w.name + (w.home ? t("ws.home") : "") + (w.other && !w.running ? t("ws.off") : ""))));
  const box = h("label", { class: "foot", title: t("ws.label") + " · " + botHint(b) }, avatar, who, sel);
  const wrap = h("div", { class: "foot-wrap" }, box);
  SPACES.broken.forEach((x) => wrap.prepend(h("div", { class: "small-text space-broken", title: x.error }, t("ws.broken", { f: x.folder }))));
  return wrap;
}
async function loadSpaces() {
  try { SPACES = await api("GET", "/api/workspaces"); } catch (e) { SPACES = null; }
}
async function openSpace(id) {
  if (String(id).startsWith("o")) return openOther(String(id).slice(1));
  try {
    SPACES = await api("POST", "/api/workspaces/" + encodeURIComponent(id) + "/open", {});
    const w = SPACES.items.find((x) => x.current);
    if (w) toast(t("ws.opened", { n: w.name }));
    FB.sel = null;
    await refreshStatus();
  } catch (e) { fail(e); }
  renderPage();
}
async function openOther(i) {
  const w = SPACES.items.find((x) => x.id === "o" + i);
  const n = w ? w.name : "";
  toast(t("ws.switching", { n }));
  for (let k = 0; k < 30; k++) {
    try {
      const r = await api("POST", "/api/others/" + encodeURIComponent(i) + "/open", {});
      if (r.url) { location.href = r.url; return; }
    } catch (e) { fail(e); break; }
    await new Promise((res) => setTimeout(res, 1000));
  }
  toast(t("ws.other_failed", { n }));
  renderShell();
}
/* demo mode (fake data): a banner above every page */
function renderDemo() {
  const on = !!(STATUS && STATUS.demo);
  $("demo").classList.toggle("hidden", !on);
  $("demo").textContent = on ? t("demo.banner") : "";
}
function botHint(b) {
  const r = b.reason || "";
  if (b.state === "error") {
    if (/Message Content Intent/i.test(r)) return t("bot.hint.intent");
    if (/Token rejected/i.test(r)) return t("bot.hint.token");
    if (/No bot token/i.test(r)) return t("bot.hint.notoken");
    if (/bot extra|pip install/i.test(r)) return t("bot.hint.install");
    return t("bot.hint.error", { r });
  }
  return t("bot.hint." + b.state);
}
function renderTop() {
  const top = clear($("top"));
  const search = h("input", { id: "search", placeholder: t("fb.search"), "aria-label": t("fb.search"), value: FB.q, oninput: (e) => {
    FB.q = e.target.value.trim();
    if (PAGE !== "inbox") { go("inbox"); return; }
    if (FB.doms) { drawInbox(); drawRight(); }
  } });
  top.append(
    h("button", { class: "ghost", "aria-label": t("top.side"), title: t("top.side"), onclick: () => {
      $("app").classList.toggle(innerWidth <= 860 ? "menu" : "collapsed");
    } }, icon("side")),
    h("span", { class: "topcat" }, cat()),
    h("span", { class: "sep" }),
    h("label", { class: "search" }, icon("search"), search),
    h("button", { class: "ghost lang", title: t("top.lang"), onclick: toggleLang }, LANG === "zh" ? "EN" : "中"),
    h("button", { class: "square", "aria-label": t("top.theme"), title: t("top.theme"), onclick: toggleTheme }, icon(THEME === "dark" ? "sun" : "moon")));
}
function setHead(title, sub, right) {
  const head = clear($("head"));
  const parent = UNDER_SETTINGS.includes(PAGE) && !optional().includes(PAGE)
    ? h("button", { class: "up", onclick: () => go("settings") }, icon("back"), t("nav.settings")) : null;
  head.append(h("div", null, parent, h("h1", null, title), typeof sub === "string" ? h("p", null, sub) : sub), h("div", { class: "head-right" }, right));
}
function toggleTheme() {
  THEME = THEME === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = THEME;
  store("setL", "swlink.theme", THEME);
  renderTop();
}

/* "How to use" strip: 2 to 3 numbered steps per page, shown from the ? button. The choice stays in localStorage (inside try/catch). */
function howtoKey(page) { return "swlink.howto." + page; }
function howtoHidden(page) {
  if (HOWTO_HIDDEN[page] !== undefined) return HOWTO_HIDDEN[page];
  try { return localStorage.getItem(howtoKey(page)) !== "0"; } catch (e) { return true; }
}
function setHowtoHidden(page, hidden) {
  HOWTO_HIDDEN[page] = hidden;
  try { if (!hidden) localStorage.setItem(howtoKey(page), "0"); else localStorage.removeItem(howtoKey(page)); } catch (e) { /* blocked */ }
}
function howtoSteps(page) {
  const steps = [];
  for (let i = 1; hasKey("howto." + page + "." + i); i++) steps.push(t("howto." + page + "." + i));
  return steps;
}
function renderHowto() {
  const box = clear($("howto"));
  if (howtoHidden(PAGE)) return;
  const steps = howtoSteps(PAGE);
  if (!steps.length) return;
  box.appendChild(h("div", { class: "howto" },
    h("div", { class: "howto-head" }, h("strong", null, t("howto.title")),
      h("button", { class: "textbtn", onclick: () => { setHowtoHidden(PAGE, true); renderHowto(); } }, t("howto.hide"))),
    h("ol", { class: "howto-steps" }, steps.map((s) => h("li", null, s)))));
}
function toggleHowto() { setHowtoHidden(PAGE, !howtoHidden(PAGE)); renderHowto(); }

async function toggleBot() {
  const running = STATUS && STATUS.bot && (STATUS.bot.state === "online" || STATUS.bot.state === "connecting");
  try { await api("POST", "/api/bot/" + (running ? "stop" : "start")); await refreshStatus(); } catch (e) { fail(e); }
  setTimeout(() => { refreshStatus().then(() => { if (PAGE === "settings") renderPage(); }).catch(() => {}); }, 1500);
}
function toggleLang() {
  LANG = LANG === "zh" ? "en" : "zh";
  store("setL", "swlink.lang", LANG);
  document.documentElement.lang = LANG === "zh" ? "zh-CN" : "en";
  renderTop();
  renderPage();
}
async function refreshStatus() {
  STATUS = await api("GET", "/api/status");
  if (STATUS.workspaces > 1) await loadSpaces();
  const shown = JSON.stringify([STATUS.inbox, STATUS.chat, STATUS.waiting, STATUS.supporters && 1, STATUS.server, STATUS.bot.state, STATUS.bot.bot_name, SPACES, PAGE, FORUM, LANG]);
  if (shown !== SIDE_SHOWN) { SIDE_SHOWN = shown; renderSide(); }   // only on a real change: a rebuilt <select> closes itself
  renderDemo();
  return STATUS;
}

function pageFromHash() {
  const m = /^#\/([a-z]+)(?:\/(\d+))?/.exec(location.hash);
  FORUM = m && m[1] === "inbox" && m[2] ? m[2] : "";
  return m && m[1] in PAGES ? m[1] : "inbox";
}
async function renderPage() {
  const seq = ++RENDER_SEQ;
  stopPolling();
  keepDraft();
  FB.doms = null;
  PAGE = pageFromHash();
  $("app").classList.remove("menu", "reading");
  renderShell();
  const helpBtn = howtoSteps(PAGE).length && !NO_HOWTO.includes(PAGE)
    ? h("button", { class: "ghost", title: t("howto.show"), "aria-label": t("howto.show"), onclick: toggleHowto }, "?") : null;
  setHead(t("nav." + PAGE), t("sub." + PAGE), helpBtn);
  const view = clear($("view"));
  view.appendChild(h("div", { class: "muted" }, t("common.loading")));
  try {
    const node = await PAGES[PAGE]();
    if (seq !== RENDER_SEQ) return;
    clear(view).appendChild(node);
  } catch (e) { if (seq === RENDER_SEQ) { clear(view); fail(e); } }
}

/* ---------- the feedback inbox: every forum in one list; you read, reply and move on ---------- */
const FB = { data: null, view: "todo", sel: null, q: "", drafts: {}, chosen: {}, hideAi: {}, busy: null, pulled: {}, seq: 0, job: "", doms: null, undoable: null };

function forumKind(name) {
  if (/bug|错误|报错|故障|问题/i.test(name)) return "bug";
  if (/想法|建议|意见|idea|suggest|feature|request/i.test(name)) return "idea";
  return "other";
}
const KIND_ICON = { bug: "bug", idea: "idea", other: "image" };
function forumOf(id) { return (FB.data && FB.data.forums.find((f) => f.id === id)) || { id, name: "", tags: [], agent: "auto" }; }
function forumName(f) { return f.name || t("fb.forum"); }
function relTime(s) {
  if (!s) return "";
  const d = new Date(s);
  if (isNaN(d)) return s;
  const min = Math.floor((Date.now() - d.getTime()) / 60000);
  if (min < 1) return t("time.now");
  if (min < 60) return t("time.min", { n: min });
  if (min < 1440) return t("time.hour", { n: Math.floor(min / 60) });
  if (min < 2880) return t("time.yesterday");
  return t("time.day", { n: Math.floor(min / 1440) });
}
function spendText(sp) { return sp.complete ? "$" + sp.cost.toFixed(sp.cost < 1 ? 3 : 2) : t("fb.calls", { n: sp.calls }); }
function fbItems() {
  const q = FB.q.toLowerCase();
  return FB.data.items.filter((p) => (!FORUM || p.forum === FORUM)
    && (!q || (p.title + " " + p.preview + " " + p.author).toLowerCase().includes(q)));
}
function fbShown() { return fbItems().filter((p) => p.state === FB.view); }

async function pageInbox() {
  $("page").classList.add("fill", "inbox");
  FB.data = await api("GET", "/api/feedback");
  const refresh = h("button", { class: "btn primary", onclick: () => pull(true) }, icon("refresh"), h("span", null, t("fb.refresh")));
  const f = FORUM ? forumOf(FORUM) : null;
  const sub = h("p");
  setHead(f ? forumName(f) : t("nav.inbox"), sub, refresh);
  const D = FB.doms = { sub, stats: h("div", { class: "stats" }), seg: h("div", { class: "seg" }), bulk: h("div", { class: "bulk hidden" }),
    list: h("div", { class: "list" }), right: h("div", { class: "fdetail" }), refresh };
  const bulkBtn = h("button", { class: "textbtn", onclick: showBulk }, icon("spark"), t("fb.bulk"));
  if (FB.sel === null || !fbShown().some((p) => p.id === FB.sel)) FB.sel = (fbShown()[0] || { id: null }).id;
  drawInbox();
  drawRight();
  if (!FB.pulled[STATUS && STATUS.server || ""]) pull(false);
  return h("div", { class: "inbox-wrap col grow" }, D.stats,
    h("div", { class: "work" },
      h("div", { class: "col listcol" }, h("div", { class: "bar" }, D.seg, bulkBtn), D.bulk, D.list),
      D.right));
}

function drawInbox() {
  const D = FB.doms;
  if (!D) return;
  const scope = fbItems(), left = scope.filter((p) => p.state === "todo").length;
  D.sub.textContent = (left ? t("fb.left", { n: left }) : t("fb.allDone")) + (FB.data.synced_at ? " · " + t("fb.pulledAt", { t: relTime(FB.data.synced_at) }) : "");
  const stat = (ic, label, value, cls) => h("div", { class: "stat" }, h("span", null, icon(ic), label), h("b", { class: cls || "" }, String(value)));
  clear(D.stats).append(stat("inbox", t("fb.stat.todo"), left), stat("spark", t("fb.stat.drafts"), scope.filter((p) => p.state === "todo" && p.has_draft).length),
    stat("check", t("fb.stat.today"), FB.data.handled_today, "g"), stat("model", t("fb.stat.spend"), spendText(FB.data.spend)));
  clear(D.seg);
  [["todo", t("fb.todo") + " " + left], ["done", t("fb.done") + " " + (scope.length - left)]].forEach(([v, label]) =>
    D.seg.appendChild(h("button", { class: FB.view === v ? "on" : "", onclick: () => setView(v) }, label)));
  drawList();
}
function setView(v) {
  keepDraft();
  FB.view = v;
  FB.sel = (fbShown()[0] || { id: null }).id;
  drawInbox();
  drawRight();
}
function catEmpty(text) { return h("div", { class: "empty grow" }, cat(), h("span", null, text)); }
function drawList() {
  const D = FB.doms, items = fbShown();
  clear(D.list);
  if (!items.length) {
    D.list.appendChild(catEmpty(FB.q ? t("fb.noMatch") : FB.view === "todo" ? (FB.data.items.length ? t("fb.allRead") : emptyHint()) : t("fb.noneDone")));
    return;
  }
  items.forEach((p) => {
    const f = forumOf(p.forum), kind = forumKind(f.name);
    D.list.appendChild(h("button", { class: "post" + (p.id === FB.sel ? " on" : "") + (p.state === "done" ? " done" : ""), onclick: () => selectPost(p.id) },
      h("span", { class: "tile " + kind }, icon(KIND_ICON[kind])),
      h("span", { class: "post-body" },
        h("span", { class: "post-t" }, h("b", null, p.title),
          p.fresh ? h("span", { class: "chip new" }, t("fb.fresh." + p.fresh)) : null,
          p.has_draft && p.state === "todo" ? h("span", { class: "chip draft" }, t("fb.hasDraft")) : null),
        h("span", { class: "post-s" }, p.preview),
        h("span", { class: "post-m" }, [forumName(f), p.author, relTime(p.time)].filter(Boolean).join(" · ")))));
  });
}
function emptyHint() {
  if (!FB.data.can_pull) return t("fb.noServer");
  return FB.data.forums.length ? t("fb.nothingYet") : t("fb.noForums");
}
function keepDraft() { const box = $("reply"); if (box && FB.sel) FB.drafts[FB.sel] = box.value; }
function selectPost(id, stay) {
  keepDraft();
  FB.sel = id;
  if (!stay) $("app").classList.add("reading");
  drawList();
  drawRight();
  const row = FB.doms.list.querySelector(".post.on");
  if (row && row.scrollIntoViewIfNeeded) row.scrollIntoViewIfNeeded(false);
}
function nextAfter(id) {
  const items = fbShown(), i = items.findIndex((p) => p.id === id);
  const rest = items.filter((p) => p.id !== id);
  return rest[i] || rest[i - 1] || null;
}

async function drawRight() {
  const D = FB.doms, seq = ++FB.seq;
  if (!D) return;
  const id = FB.sel;
  if (!id || !fbShown().some((p) => p.id === id)) {
    clear(D.right).appendChild(catEmpty(FB.data.items.some((p) => p.state === "todo") ? t("fb.pick") : t("fb.allRead")));
    $("app").classList.remove("reading");
    return;
  }
  let d;
  try { d = await api("GET", "/api/feedback/" + id); } catch (e) { fail(e); return; }
  if (seq !== FB.seq) return;
  const f = forumOf(d.forum);
  const botName = (STATUS && STATUS.bot && STATUS.bot.bot_name) || t("fb.bot");
  const pictures = (files) => {
    const pics = files.filter((x) => x.image), rest = files.filter((x) => !x.image);
    if (!files.length) return null;
    return h("div", { class: "shots" }, pics.map((x) => picture(id, x)), rest.map((x) => h("span", { class: "file" }, x.name)));
  };
  const say = (who, time, text, me, extra) => h("div", { class: "fmsg" },
    h("span", { class: "face" + (me ? " bot" : "") }, me ? cat() : (who || "?").slice(0, 1).toUpperCase()),
    h("div", { class: "fmsg-box" }, h("div", { class: "fmsg-who" }, h("b", null, who), relTime(time)), text ? h("p", null, text) : null, extra));
  const log = d.log ? h("details", { class: "log", open: d.log_errors > 0 }, h("summary", null, t("fb.log", { n: d.log_errors })), h("pre", null, d.log)) : null;
  const read = h("div", { class: "read" },
    h("button", { class: "textbtn back", onclick: () => $("app").classList.remove("reading") }, icon("back"), t("fb.back")),
    h("div", { class: "dh" }, h("div", null, h("h2", null, d.title), h("div", { class: "dm" }, h("span", null, forumName(f)), d.state === "done" ? h("span", { class: "chip" }, t("fb.done")) : null,
        d.tags.map((x) => h("span", { class: "chip" }, x)))),
      d.link ? h("a", { class: "open", href: d.link, target: "_blank", rel: "noopener noreferrer" }, t("fb.discord"), icon("out")) : null),
    say(d.author, d.created, d.body, false, [pictures(d.files), log]),
    d.more ? h("div", { class: "muted small-text gap-top" }, t("fb.more", { n: d.more })) : null,
    d.messages.map((m) => {
      const edit = m.bot && m.content ? h("button", { class: "link-btn", onclick: () => editBotMessage(id, m, drawRight) }, t("bm.edit")) : null;
      const node = say(m.bot ? botName : m.author, m.time, m.content, m.bot, pictures(m.files));
      if (edit) node.querySelector(".fmsg-who").appendChild(edit);
      return node;
    }));
  clear(D.right).append(read, compose(d, f));
  updateSend(d);
  if (D.right.dataset.at !== id) { read.scrollTop = read.scrollHeight; D.right.dataset.at = id; }
}

function picture(tid, x) {
  const btn = h("button", { class: "shot", title: x.name }, x.name);
  let src = "";
  api("GET", "/api/feedback/" + tid + "/file/" + x.i).then((r) => {
    src = r.data || safeImg(r.url);
    if (src) clear(btn).appendChild(h("img", { src, alt: x.name }));
  }).catch(() => { btn.textContent = x.name; });
  btn.addEventListener("click", () => {
    if (!src) return;
    const big = h("button", { class: "bigshot", onclick: () => big.remove() }, h("img", { src, alt: x.name }));
    document.body.appendChild(big);
  });
  return btn;
}

function chosenTags(d) { return FB.chosen[d.id] || (FB.chosen[d.id] = d.tags.slice()); }
function tagDiff(d) {
  const sel = chosenTags(d);
  return { add: sel.filter((x) => !d.tags.includes(x)), remove: d.tags.filter((x) => !sel.includes(x)) };
}
function updateSend(d) {
  const box = $("reply"), b = $("send");
  if (!box || !b) return;
  const diff = tagDiff(d), text = box.value.trim().length > 0;
  b.disabled = !text && !diff.add.length && !diff.remove.length;
  b.textContent = !text && (diff.add.length || diff.remove.length) ? t("fb.tagsOnly") : t("fb.send");
}

function compose(d, f) {
  const noAi = d.agent === "none";
  const ai = d.ai && !d.ai_stale && !FB.hideAi[d.id] ? d.ai : null;
  if (FB.drafts[d.id] === undefined) FB.drafts[d.id] = d.ai && !d.ai_stale && d.state === "todo" ? (d.ai.reply_draft || "") : "";
  const sel = chosenTags(d);
  const hints = ai ? (ai.tags_add || []).filter((x) => !sel.includes(x)) : [];
  const ta = h("textarea", { id: "reply", placeholder: t("fb.placeholder"), "aria-label": t("fb.placeholder"), value: FB.drafts[d.id] });
  ta.addEventListener("input", () => { FB.drafts[d.id] = ta.value; updateSend(d); });
  ta.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && ev.ctrlKey) { ev.preventDefault(); sendPost(d); } });
  const tagRow = d.forum_tags.length ? h("div", { class: "tags" }, d.forum_tags.map((x) => h("button", {
    class: "tag" + (sel.includes(x) ? " on" : "") + (hints.includes(x) ? " hint" : ""), title: hints.includes(x) ? t("fb.tagHint") : null,
    onclick: () => { keepDraft(); FB.chosen[d.id] = sel.includes(x) ? sel.filter((y) => y !== x) : sel.concat(x); drawRight(); } }, x))) : null;
  const aiBox = ai ? h("div", { class: "ai" }, cat(), h("div", { class: "ai-txt" },
    ai.summary ? h("div", null, h("b", null, t("fb.ai.judge")), ai.summary) : null,
    ai.likely_cause ? h("div", null, h("b", null, t("fb.ai.cause")), ai.likely_cause) : null,
    ai.need_info ? h("div", null, h("b", null, t("fb.ai.need")), ai.need_info) : null,
    h("div", { class: "ai-foot" }, h("span", null, [ai.model, hints.length ? t("fb.ai.hints") : ""].filter(Boolean).join(" · ")),
      h("button", { class: "textbtn", onclick: () => { keepDraft(); FB.hideAi[d.id] = true; drawRight(); } }, t("fb.ai.hide"))))) : null;
  const busy = FB.busy === d.id;
  const [polish, undoPolish] = polishButtons(ta, () => ({ channel: d.id, agent: noAi ? "" : d.agent }));
  polish.hidden = !ta.value.trim();
  ta.addEventListener("input", () => { polish.hidden = !ta.value.trim(); });
  return h("div", { class: "compose" }, aiBox, tagRow, ta, h("div", { class: "acts" },
    h("button", { class: "btn primary", id: "send", onclick: () => sendPost(d) }, t("fb.send")),
    d.state === "done" ? h("button", { class: "btn", onclick: () => reopenPost(d) }, t("fb.reopen")) : h("button", { class: "btn", onclick: () => skipPost(d) }, t("fb.skip")),
    noAi ? null : h("button", { class: "btn", disabled: busy, onclick: () => askAi(d) }, icon("spark"), busy ? t("fb.ai.busy") : (ai ? t("fb.ai.again") : t("fb.ai.go"))),
    polish, undoPolish, h("span", { class: "grow" }), h("span", { class: "keys" }, t("fb.keys"))));
}

async function reloadInbox() {
  FB.data = await api("GET", "/api/feedback");
  drawInbox();
  refreshStatus().catch(() => {});
}
function finishPost(d, note, undo) {
  const next = d.state === "todo" ? nextAfter(d.id) : null;
  delete FB.drafts[d.id]; delete FB.chosen[d.id]; delete FB.hideAi[d.id];
  if (d.state === "todo") {
    FB.sel = next ? next.id : null;
    if (!next) $("app").classList.remove("reading");
  }
  toast(note, false, undo);
  reloadInbox().then(drawRight).catch(fail);
}
async function sendPost(d) {
  const box = $("reply"), b = $("send");
  if (!box || !b || b.disabled) return;
  const diff = tagDiff(d);
  b.disabled = true;
  try {
    const r = await api("POST", "/api/feedback/" + d.id + "/send", { text: box.value, add: diff.add, remove: diff.remove });
    if (r.mismatches && r.mismatches.length) toast(t("fb.mismatch"), true);
    finishPost(d, t("fb.sent", { f: forumName(forumOf(d.forum)) }));
  } catch (e) { fail(e); b.disabled = false; }
}
async function skipPost(d) {
  keepDraft();
  try {
    await api("POST", "/api/feedback/" + d.id + "/mark", { state: "done" });
    const kept = { text: FB.drafts[d.id], id: d.id };
    finishPost(d, t("fb.skipped"), { label: t("fb.undo"), run: async () => {
      try {
        await api("POST", "/api/feedback/" + d.id + "/mark", { state: "auto" });
        FB.drafts[kept.id] = kept.text;
        FB.sel = kept.id; FB.view = "todo";
        await reloadInbox(); drawRight();
      } catch (e) { fail(e); }
    } });
  } catch (e) { fail(e); }
}
async function reopenPost(d) {
  try {
    await api("POST", "/api/feedback/" + d.id + "/mark", { state: "open" });
    FB.view = "todo"; FB.sel = d.id;
    await reloadInbox(); drawRight();
    toast(t("fb.reopened"));
  } catch (e) { fail(e); }
}
async function askAi(d) {
  if (FB.busy) return;
  keepDraft();
  FB.busy = d.id;
  drawRight();
  try {
    const r = await api("POST", "/api/feedback/" + d.id + "/ai", {});
    FB.drafts[d.id] = r.ai.reply_draft || FB.drafts[d.id] || "";
    delete FB.hideAi[d.id];
    FB.data.spend = r.spend;
    await reloadInbox();
  } catch (e) { fail(e); }
  FB.busy = null;
  if (FB.sel === d.id) drawRight();
}

async function pull(loud) {
  const D = FB.doms, key = (STATUS && STATUS.server) || "";
  FB.pulled[key] = true;
  if (D) D.refresh.disabled = true;
  try {
    const r = await api("POST", "/api/feedback/sync", {});
    const before = FB.sel;
    keepDraft();
    await reloadInbox();
    if (FB.doms && before && before === FB.sel) drawRight();
    else if (FB.doms && !FB.sel) { FB.sel = (fbShown()[0] || { id: null }).id; drawInbox(); drawRight(); }
    if (r.warnings.length) toast(r.warnings.join("；"), true);
    else if (r.new || r.replies) toast(t("fb.pulled", { n: r.new, m: r.replies }));
    else if (loud) toast(t("fb.noNews"));
  } catch (e) {
    if (e.message !== "pull_busy" && (loud || e.message !== "no_token")) fail(e);
  } finally { if (D) D.refresh.disabled = false; }
}

function bulkTargets() {
  return fbItems().filter((p) => p.state === "todo" && !p.has_draft && forumOf(p.forum).agent !== "none");
}
function showBulk() {
  const el = FB.doms.bulk;
  if (FB.view !== "todo") setView("todo");
  const n = bulkTargets().length;
  el.classList.remove("hidden");
  clear(el);
  const close = h("button", { class: "textbtn", onclick: () => el.classList.add("hidden") }, n ? t("common.cancel") : t("common.close"));
  if (!n) { el.append(h("span", { class: "grow" }, t("fb.bulkNone")), close); return; }
  if (!FB.data.model) { el.append(h("span", { class: "grow" }, t("fb.noModel")), close); return; }
  const cost = FB.data.per_post === null ? "" : t("fb.bulkCost", { c: (n * FB.data.per_post).toFixed(3) });
  el.append(h("span", { class: "grow" }, t("fb.bulkAsk", { m: FB.data.model, n, c: cost })),
    h("button", { class: "btn primary", onclick: runBulk }, t("fb.bulkGo")), close);
}
async function runBulk() {
  const el = FB.doms.bulk;
  try {
    const r = await api("POST", "/api/feedback/review", { forum: FORUM || "" });
    FB.job = r.job;
    stopPolling();
    POLL = setInterval(async () => {
      try {
        const st = await api("GET", "/api/forum/review");
        clear(el);
        if (st.running) {
          const stop = h("button", { class: "textbtn", onclick: () => api("POST", "/api/forum/review/stop", {}).catch(fail) }, t("forum.stop"));
          el.append(h("span", { class: "grow" }, t("fb.bulkRun", { d: st.done, t: st.total, c: st.current })), stop);
          return;
        }
        stopPolling();
        const shut = h("button", { class: "textbtn", onclick: () => el.classList.add("hidden") }, t("common.close"));
        el.append(h("span", { class: "grow" }, t("fb.bulkDone", { n: st.reviewed }) + (st.errors.length ? " " + t("fb.bulkErr", { n: st.errors.length, e: st.errors[0].error }) : "")), shut);
        await reloadInbox();
        drawRight();
      } catch (e) { stopPolling(); fail(e); }
    }, 1000);
  } catch (e) { fail(e); }
}

/* ↑↓ moves between posts when you are not typing */
document.addEventListener("keydown", (e) => {
  if (PAGE !== "inbox" || !FB.doms || e.target.matches("textarea, select, input")) return;
  if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
  const items = fbShown(), i = items.findIndex((p) => p.id === FB.sel);
  const n = items[i + (e.key === "ArrowDown" ? 1 : -1)];
  if (n) { e.preventDefault(); selectPost(n.id, true); }
});

/* ---------- chat ---------- */
async function pageConversations() {
  await refreshStatus().catch(() => {});
  const [convs, rulesRes, agentsRes] = await Promise.all([api("GET", "/api/conversations"), api("GET", "/api/rules"), api("GET", "/api/agents")]);
  let items = convs.items, rules = rulesRes.rules, agents = agentsRes.items, sel = null, filter = "all";
  const ruleFor = (c) => rules.channels[c.id] || (c.kind === "dm" ? rules.dm_default : rules.guild_default);
  const listBox = h("div", { class: "conv-scroll" });
  const count = h("div", { class: "count" });
  const filterSel = h("select", { onchange: (e) => { filter = e.target.value; drawList(); } },
    h("option", { value: "all" }, t("conv.filter.all")),
    MODES.map((m) => h("option", { value: "mode:" + m }, t("mode." + m))),
    agents.map((a) => h("option", { value: "agent:" + a.id }, a.name)));
  const chatBox = h("div", { class: "conv-chat" });
  if (!items.length) return empty(t("conv.empty.title"), convs.reason === "no_token" ? t("err.no_token") : t("conv.empty.text"), goBtn("settings", t("nav.settings")));

  function visible() {
    return items.filter((c) => {
      if (filter === "all") return true;
      const [k, v] = filter.split(":");
      return k === "mode" ? ruleFor(c).mode === v : ruleFor(c).agent === v;
    });
  }
  function drawList() {
    const vis = visible();
    clear(listBox);
    const groups = new Map();
    for (const c of vis) {
      const g = c.kind === "dm" ? t("conv.dms") : (c.guild_name || c.guild_id);
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(c);
    }
    for (const [g, list] of groups) {
      listBox.appendChild(h("div", { class: "group-title" }, g));
      for (const c of list) {
        const m = ruleFor(c).mode;
        const locked = c.readable === false;
        listBox.appendChild(h("button", { class: "conv-item" + (sel && sel.id === c.id ? " active" : "") + (locked ? " unreadable" : ""), title: locked ? t("conv.locked") : null, onclick: () => open(c) },
          h("div", { class: "nm" }, h("span", null, locked ? lockIcon() : null, (c.kind === "dm" ? "" : "# ") + c.channel_name), m === "off" ? null : h("span", { class: "mode-tag " + m }, modeLabel(m))),
          c.preview ? h("div", { class: "pv" }, c.preview) : null));
      }
    }
    count.textContent = t("conv.total", { n: vis.length });
  }
  function noAccessCard(name) {
    return h("div", { class: "no-access" }, lockIcon(), h("h3", null, t("conv.noAccess.title", { name: name || t("conv.noAccess.bot") })), h("div", { class: "sub" }, t("conv.noAccess.text")));
  }
  async function setMode(c, mode, selectEl) {
    if (mode === "auto" && !(await confirmBox(t("conv.autoTitle"), t("conv.autoText"), t("mode.auto"), false))) { selectEl.value = ruleFor(c).mode; return; }
    const next = Object.assign({}, rules, { channels: Object.assign({}, rules.channels, { [c.id]: Object.assign({}, ruleFor(c), { mode }) }) });
    try { rules = (await api("PUT", "/api/rules", next)).rules; toast(t("common.saved")); drawList(); } catch (e) { fail(e); selectEl.value = ruleFor(c).mode; }
  }
  async function open(c) {
    sel = c; drawList();
    const body = h("div", { class: "chat-body" }, h("div", { class: "muted" }, t("common.loading")));
    const ta = h("textarea", { placeholder: t("conv.placeholder") });
    const agentSel = h("select", { value: ruleFor(c).agent }, agents.map((a) => h("option", { value: a.id }, a.name)));
    const modeSel = modeSelect(ruleFor(c).mode, (m) => setMode(c, m, modeSel));
    const draftBtn = h("button", { class: "btn", onclick: draft }, t("conv.draft"));
    const sendBtn = h("button", { class: "btn primary", onclick: send }, t("conv.send"));
    const model = STATUS && STATUS.llm ? STATUS.llm.name : "-";
    clear(chatBox).appendChild(h("div", { class: "chat-head" }, (c.kind === "dm" ? "" : c.guild_name + " / # ") + c.channel_name));
    chatBox.appendChild(body);
    chatBox.appendChild(h("div", { class: "composer" }, ta,
      h("div", { class: "row end" }, h("span", { class: "sub small-text" }, t("conv.agent")), agentSel, draftBtn,
        ...polishButtons(ta, () => ({ agent: agentSel.value, channel: c.id })), sendBtn),
      h("div", { class: "composer-meta" },
        h("span", null, t("conv.model", { m: model }) + " · ", h("a", { href: "#/llm" }, t("conv.change"))),
        h("span", null, t("conv.mode") + " ", modeSel))));
    async function load() {
      try {
        const res = await api("GET", "/api/conversations/" + c.id + "/messages?limit=50");
        if (sel !== c) return;
        clear(body);
        if (!res.items.length) body.appendChild(h("div", { class: "muted" }, t("conv.nomsg")));
        for (const m of res.items) {
          const img = safeImg(m.avatar);
          const text = [m.content, m.attachments ? t("conv.attach", { n: m.attachments }) : ""].filter(Boolean).join("\n");
          body.appendChild(h("div", { class: "msg" + (m.is_self ? " self" : "") },
            h("div", { class: "avatar" }, img ? h("img", { src: img, alt: "" }) : (m.author_name || "?").slice(0, 1).toUpperCase()),
            h("div", { class: "bubble" }, h("div", { class: "who" }, m.author_name + " · " + fmtTime(m.time),
              m.is_self && m.content ? h("button", { class: "link-btn", onclick: () => editBotMessage(c.id, m, load) }, t("bm.edit")) : null),
              h("div", { class: "txt" }, text))));
        }
        body.scrollTop = body.scrollHeight;          // the message box only; never the page
      } catch (e) {
        clear(body);
        if (e && e.message === "no_access") body.appendChild(noAccessCard((e.data && e.data.bot_name) || (STATUS && STATUS.bot && STATUS.bot.bot_name) || ""));
        else fail(e);
      }
    }
    async function draft() {
      draftBtn.disabled = true; toast(t("conv.drafting"));
      try { const r = await api("POST", "/api/conversations/" + c.id + "/draft", { agent: agentSel.value }); ta.value = r.text; toast(t("conv.draftdone")); }
      catch (e) { fail(e); } finally { draftBtn.disabled = false; }
    }
    async function send() {
      const text = ta.value;
      if (!text.trim()) return;
      sendBtn.disabled = true;
      try { await api("POST", "/api/conversations/" + c.id + "/send", { text }); ta.value = ""; toast(t("conv.sent")); await load(); }
      catch (e) { fail(e); } finally { sendBtn.disabled = false; }
    }
    ta.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); send(); } });
    load();
  }
  chatBox.appendChild(h("div", { class: "empty" }, t("conv.pick")));
  drawList();
  return h("div", { class: "conv" }, h("div", { class: "conv-list" }, h("div", { class: "conv-list-head" }, filterSel, count), listBox), chatBox);
}

/* ---------- approvals ---------- */
async function pageApprovals() {
  const [pend, appr, failed] = await Promise.all([api("GET", "/api/queue?state=pending"), api("GET", "/api/queue?state=approved"),
    api("GET", "/api/queue?state=failed")]);
  const wrap = h("div", null);
  const TEXT_KINDS = ["reply", "post", "newpost", "edit"];       // the proposals that carry wording a person can fix
  const card = (p, approved, isFailed) => {
    const view = h("div", null);
    const bar = approved ? null : h("div", { class: "card-actions" });
    if (isFailed) {
      bar.appendChild(h("button", { class: "btn primary small", onclick: () => decide(p, "retry") }, t("appr.retry")));
      bar.appendChild(h("button", { class: "btn danger small", onclick: () => decide(p, "reject") }, t("appr.drop")));
    }
    const showText = () => clear(view).appendChild(p.kind === "tags"
      ? h("div", { class: "preview" }, [p.add.length ? "+ " + p.add.join(", ") : "", p.remove.length ? "− " + p.remove.join(", ") : ""].filter(Boolean).join("   "))
      : h("div", { class: "preview" }, p.text));
    const showButtons = () => {
      clear(bar).appendChild(h("button", { class: "btn primary small", onclick: () => decide(p, "approve") }, t("appr.approve")));
      bar.appendChild(h("button", { class: "btn danger small", onclick: () => decide(p, "reject") }, t("appr.reject")));
      if (TEXT_KINDS.includes(p.kind)) bar.appendChild(h("button", { class: "btn small", onclick: showEditor }, t("common.edit")));
    };
    function showEditor() {
      const ta = h("textarea", null);
      ta.value = p.text;
      ta.rows = 6;
      clear(view).appendChild(ta);
      ta.focus();
      clear(bar).appendChild(h("button", { class: "btn primary small", onclick: async () => {
        try { await api("PUT", "/api/queue/" + p.id + "/text", { text: ta.value }); toast(t("common.saved")); renderPage(); } catch (e) { fail(e); }
      } }, t("common.save")));
      polishButtons(ta, () => ({ channel: p.thread || "" }), true).forEach((b) => bar.appendChild(b));
      bar.appendChild(h("button", { class: "btn small", onclick: () => { showText(); showButtons(); } }, t("common.cancel")));
    }
    showText();
    if (bar && !isFailed) showButtons();
    return h("div", { class: "card" },
      h("div", { class: "row" }, h("span", { class: "badge" }, p.kind), h("strong", { class: "grow" }, p.target || p.thread || p.title),
        h("span", { class: "muted small-text" }, fmtTime(p.created))),
      view,
      isFailed && p.error ? h("div", { class: "err-line small-text" }, t("appr.error") + ": " + p.error) : null,
      p.why ? h("div", { class: "sub small-text" }, t("appr.why") + ": " + p.why) : null,
      h("div", { class: "sub small-text" }, t("appr.by") + ": " + p.actor),
      bar);
  };
  async function decide(p, what) { try { await api("POST", "/api/queue/" + p.id + "/" + what, {}); renderPage(); } catch (e) { fail(e); } }
  async function sendAll() {
    if (!appr.items.length) { toast(t("appr.nothing")); return; }
    const list = h("div", null, h("div", { class: "sub" }, t("appr.sendText")),
      appr.items.map((p) => h("div", null, h("div", { class: "small-text muted" }, t("appr.to") + ": " + (p.target || p.thread)), h("div", { class: "preview" }, p.text || p.kind))));
    const ok = await new Promise((resolve) => modal(t("appr.sendTitle"), list, [
      { label: t("common.cancel"), run: () => { resolve(false); } }, { label: t("appr.send"), cls: "primary", run: () => { resolve(true); } }]));
    if (!ok) return;
    try {
      const r = await api("POST", "/api/queue/send", {});
      toast(t("appr.done", { n: r.sent.length }) + (r.failed.length ? " · " + t("appr.failed", { n: r.failed.length }) : ""), r.failed.length > 0);
      renderPage();
    } catch (e) { fail(e); }
  }
  async function approveAll() {
    const counts = {};
    pend.items.forEach((p) => { counts[p.kind] = (counts[p.kind] || 0) + 1; });
    const summary = Object.entries(counts).map(([k, n]) => (hasKey("appr.kind." + k) ? t("appr.kind." + k) : k) + " " + n).join(" · ");
    if (!(await confirmBox(t("appr.approveAllTitle", { n: pend.items.length }), t("appr.approveAllText", { s: summary }), t("appr.approveAll"), false))) return;
    let done = 0;
    const errors = [];
    for (const p of pend.items) {           // one by one through the same route as the single button
      try { await api("POST", "/api/queue/" + p.id + "/approve", {}); done++; } catch (e) { errors.push(p.id + ": " + (e.message || e)); }
    }
    toast(t("appr.approvedN", { n: done }) + (errors.length ? " · " + errors.join("; ") : ""), errors.length > 0);
    renderPage();
  }
  if (!pend.items.length && !appr.items.length && !failed.items.length) return empty(t("appr.empty.title"), t("appr.empty.text"), goBtn("inbox", t("appr.goInbox")));
  wrap.appendChild(h("div", { class: "row end" },
    pend.items.length > 1 ? h("button", { class: "btn", onclick: approveAll }, t("appr.approveAll") + " (" + pend.items.length + ")") : null,
    h("button", { class: "btn primary", disabled: !appr.items.length, onclick: sendAll }, t("appr.send") + " (" + appr.items.length + ")")));
  wrap.appendChild(h("h2", null, t("appr.pending") + " (" + pend.items.length + ")"));
  pend.items.forEach((p) => wrap.appendChild(card(p, false)));
  if (appr.items.length) { wrap.appendChild(h("h2", null, t("appr.approved"))); appr.items.forEach((p) => wrap.appendChild(card(p, true))); }
  if (failed.items.length) {
    wrap.appendChild(h("h2", null, t("appr.failedHead") + " (" + failed.items.length + ")"));
    failed.items.forEach((p) => wrap.appendChild(card(p, false, true)));
  }
  return wrap;
}

/* ---------- bot messages ---------- */
async function pageBotMsgs() {
  const [forum, guilds, places] = await Promise.all([
    api("GET", "/api/forum").catch(() => ({ configured: false, items: [] })),
    api("GET", "/api/guilds").catch(() => ({ items: [], reason: "" })),
    api("GET", "/api/places").catch(() => ({ threads: [], dms: [], reason: "" }))]);
  if (guilds.reason === "no_token" && !forum.items.length) return empty(t("bm.empty.title"), t("bm.noToken"), goBtn("settings", t("nav.settings")));
  const out = h("div", { class: "bm-list" }, h("div", { class: "muted pad" }, t("bm.first")));
  // one picker per kind of place, so a long server does not end up as one endless dropdown
  const groups = (rows, key) => { const m = new Map(); rows.forEach((r) => { const k = key(r); if (!m.has(k)) m.set(k, []); m.get(k).push(r); }); return [...m]; };
  const chanRows = guilds.items.flatMap((g) => g.channels.map((c) => ({ ...c, group: g.name + " · " + (c.category || t("bm.noCategory")) })));
  const forumRows = forum.items.slice().sort((a, b) => String(b.last_activity).localeCompare(String(a.last_activity)));
  const kinds = [
    { id: "channels", label: t("bm.channels"), n: chanRows.length,
      opts: () => groups(chanRows, (c) => c.group).map(([g, cs]) => h("optgroup", { label: g }, cs.map((c) => h("option", { value: c.id }, "# " + c.name)))) },
    { id: "forum", label: t("bm.threads"), n: forumRows.length,
      opts: () => forumRows.map((r) => h("option", { value: r.id }, r.title)) },
    { id: "threads", label: t("bm.otherThreads"), n: places.threads.length,
      opts: () => groups(places.threads, (r) => r.guild + " · " + r.parent).map(([g, rs]) => h("optgroup", { label: g }, rs.map((r) => h("option", { value: r.id }, r.name)))) },
    { id: "dms", label: t("bm.dms"), n: places.dms.length,
      opts: () => places.dms.map((r) => h("option", { value: r.id }, r.name)) },
    { id: "link", label: t("bm.byLink"), n: null },
  ];
  const place = h("select", { onchange: () => { if (place.value) load(place.value); } });
  const link = h("input", { type: "text", placeholder: t("bm.paste") });
  link.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); if (link.value.trim()) load(link.value.trim()); } });
  const loadBtn = h("button", { class: "btn primary", onclick: () => { const v = kind === "link" ? link.value.trim() : place.value; if (v) load(v); else toast(t("bm.first"), true); } }, t("bm.load"));
  const slot = h("div", { class: "grow" });
  const note = h("div", { class: "muted small-text" });
  const tabs = h("div", { class: "tabs" });
  let kind = "";
  function pick(id) {
    kind = id;
    const k = kinds.find((x) => x.id === id);
    tabs.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.kind === id));
    clear(slot);
    note.textContent = "";
    if (id === "link") { slot.appendChild(link); link.focus(); return; }
    clear(place).appendChild(h("option", { value: "" }, k.n ? t("bm.pick") : t("bm.none")));
    k.opts().forEach((o) => place.appendChild(o));
    slot.appendChild(place);
    if (id === "threads" && places.reason) note.textContent = places.reason;
    if (id === "dms") note.textContent = t("bm.dmsNote");
  }
  kinds.forEach((k) => { const b = h("button", { class: "tab", onclick: () => pick(k.id) }, k.label, k.n === null ? null : h("span", { class: "tab-n" }, String(k.n))); b.dataset.kind = k.id; tabs.appendChild(b); });
  pick(chanRows.length ? "channels" : forumRows.length ? "forum" : "link");
  let current = "";

  async function load(target) {
    current = target;
    clear(out).appendChild(h("div", { class: "muted pad" }, t("common.loading")));
    try {
      const r = await api("GET", "/api/botmsgs?channel=" + encodeURIComponent(target));
      if (current !== target) return;
      current = r.channel;
      clear(out);
      out.appendChild(h("div", { class: "row bm-head" }, h("strong", { class: "grow" }, r.label), h("span", { class: "muted small-text" }, t("bm.newest"))));
      if (!r.items.length) { out.appendChild(empty(t("bm.empty.title"), t("bm.empty.text"))); return; }
      r.items.forEach((m) => out.appendChild(msgCard(r, m)));
    } catch (e) { clear(out); fail(e); }
  }
  function msgCard(r, m) {
    return h("div", { class: "card bm" + (r.focus === m.id ? " focus" : "") },
      h("div", { class: "row" }, h("span", { class: "muted small-text grow" }, fmtTime(m.time) + (m.edited ? " · " + t("bm.edited") : "")),
        h("button", { class: "btn small", onclick: () => editMsg(r, m) }, t("bm.edit")),
        h("button", { class: "icon-btn", title: t("common.delete"), onclick: () => delMsg(r, m) }, trashIcon())),
      h("div", { class: "txt" }, m.content || (m.attachments.length ? m.attachments.join(", ") : "")),
      m.content && m.attachments.length ? h("div", { class: "small-text muted" }, m.attachments.join(", ")) : null);
  }
  function editMsg(r, m) { editBotMessage(r.channel, m, () => load(r.channel)); }
  async function delMsg(r, m) {
    if (!(await confirmBox(t("bm.delTitle"), t("bm.delText") + "\n\n" + (m.content || "").slice(0, 200), t("common.delete"), true))) return;
    try { await api("DELETE", "/api/botmsgs/" + r.channel + "/" + m.id, {}); toast(t("bm.deleted")); load(r.channel); } catch (e) { fail(e); }
  }
  return h("div", null, h("div", { class: "card bm-pick" }, tabs, h("div", { class: "row" }, slot, loadBtn), note), out);
}

/* the editor for one of the bot's own messages (bot messages page and the chat view) */
function editBotMessage(channel, m, done) {
  const ta = h("textarea", { value: m.content });
  ta.rows = 8;
  const counter = h("div", { class: "counter small-text" });
  const upd = () => { counter.textContent = t("bm.counter", { n: ta.value.length }); counter.classList.toggle("over", ta.value.length > 2000); };
  ta.addEventListener("input", upd); upd();
  modal(t("bm.editTitle"), h("div", null, ta, counter, h("div", { class: "row end" }, ...polishButtons(ta, () => ({ channel }), true)),
    h("div", { class: "sub small-text" }, t("bm.sameSyncNote"))), [
    { label: t("common.cancel") },
    { label: t("common.save"), cls: "primary", run: async () => {
      try { await api("PUT", "/api/botmsgs/" + channel + "/" + m.id, { text: ta.value }); toast(t("common.saved")); done(); }
      catch (e) { fail(e); return false; } } }]);
}

/* ---------- supporters (Afdian orders -> a Discord role) ---------- */
async function pageSupporters() {
  const ov = await api("GET", "/api/supporters/settings");
  const s = ov.settings, gid = ov.guild.id;
  let roles = { items: [], can_manage_roles: true, bot_top: null }, rolesOk = !!gid;
  if (gid) { try { roles = await api("GET", "/api/guilds/" + gid + "/roles"); } catch (e) { rolesOk = false; } }
  const guilds = await api("GET", "/api/guilds").catch(() => ({ items: [] }));
  const channels = (guilds.items.find((g) => g.id === gid) || guilds.items[0] || { channels: [] }).channels;
  const roleName = (id) => { const r = roles.items.find((x) => x.id === id); return r ? r.name : id; };
  const roleOpts = (selected) => {
    const opts = [h("option", { value: "" }, t("sup.roleNone"))];
    if (selected && !roles.items.some((r) => r.id === selected)) opts.push(h("option", { value: selected }, selected));
    roles.items.filter((r) => !r.managed).forEach((r) => opts.push(h("option", { value: r.id }, r.name + (r.assignable ? "" : " " + t("sup.notAssignable")))));
    return opts;
  };
  const fixText = (role, kind, bot) => t("sup.fix." + kind, { role, bot: bot || (roles.bot_top && roles.bot_top.name) || "…" });
  const warn = (text) => h("div", { class: "badge err warn-line" }, text);

  /* notices about roles the bot could not hand out (kept by the server since the last restart) */
  const issues = ov.issues.length ? h("div", { class: "card" }, h("h2", null, t("sup.issues")),
    ov.issues.map((i) => warn(fixText(i.role_name, i.kind === "no_permission" ? "perm" : i.kind, i.bot_role)))) : null;

  /* connection */
  const enabled = h("input", { type: "checkbox", checked: s.enabled });
  const uid = h("input", { type: "text", autocomplete: "off", value: s.afdian_user_id });
  const tok = h("input", { type: "password", autocomplete: "off", placeholder: ov.token_set ? t("sup.tokenKeep") : "" });
  const status = h("span", { class: "sub small-text" });
  async function saveConnFields() {
    if (tok.value.trim()) { await api("POST", "/api/supporters/token", { token: tok.value.trim() }); tok.value = ""; }
    await api("PUT", "/api/supporters/settings", { enabled: enabled.checked, afdian_user_id: uid.value.trim() });
  }
  const saveConn = h("button", { class: "btn primary", onclick: async () => {
    try { await saveConnFields(); toast(t("common.saved")); renderPage(); } catch (e) { fail(e); }
  } }, t("common.save"));
  const testBtn = h("button", { class: "btn", onclick: async () => {
    testBtn.disabled = true; status.textContent = t("sup.testing");
    try {
      await saveConnFields();
      const r = await api("POST", "/api/supporters/test", {});
      status.textContent = r.ok ? t("sup.testOk") : t("sup.testFail", { e: r.error });
      status.className = "small-text " + (r.ok ? "sub" : "err-line");
    } catch (e) { status.textContent = ""; fail(e); } finally { testBtn.disabled = false; }
  } }, t("sup.test"));
  const connCard = h("div", { class: "card" }, h("h2", null, t("sup.conn")),
    h("a",{ class: "small-text", href: ov.dev_url, target: "_blank", rel: "noopener noreferrer" }, t("sup.openDev")),
    h("label", { class: "row gap-top" }, enabled, t("sup.enable")),
    h("div", { class: "two" }, field(t("sup.userId"), uid), field(t("sup.token"), tok)),
    h("div", { class: "row" }, h("span", { class: "badge" + (ov.token_set ? "" : " err") }, ov.token_set ? t("sup.tokenSet") : t("sup.tokenNot")),
      h("span", { class: "sub small-text" }, t("sup.tokenNote"))),
    ov.keyring ? null : h("div", { class: "badge err" }, t("sup.nokeyring", { v: "HEARBACK_AFDIAN_TOKEN" })),
    h("div", { class: "row" }, saveConn, testBtn, status));

  /* rules */
  const roleSel = h("select", { value: s.role_id }, roleOpts(s.role_id));
  const roleWarn = h("div", null);
  const showRoleWarn = () => {
    clear(roleWarn);
    const r = roles.items.find((x) => x.id === roleSel.value);
    if (r && !r.assignable) roleWarn.appendChild(warn(fixText(r.name, roles.can_manage_roles ? "hierarchy" : "perm")));
    if (!rolesOk) roleWarn.appendChild(h("div", { class: "sub small-text" }, t("sup.rolesFail")));
  };
  roleSel.addEventListener("change", showRoleWarn); showRoleWarn();
  const requireSel = h("select", { multiple: true, size: String(Math.min(6, Math.max(3, roles.items.length))) }, roles.items.filter((r) => !r.managed).map((r) => h("option", { value: r.id, selected: s.require_role_ids.includes(r.id) }, r.name)));
  const minInp = h("input", { type: "text", inputmode: "decimal", value: s.min_amount === "0" ? "" : s.min_amount });
  const lookupsInp = h("input", { type: "number", min: "1", max: "200", value: s.lookups_per_day });
  const dmBox = h("input", { type: "checkbox", checked: s.dm_handling });
  const plans = Object.entries(s.plan_roles).map(([p, r]) => ({ plan: p, role: r }));
  const planBox = h("div", null);
  const drawPlans = () => {
    clear(planBox);
    plans.forEach((row, i) => planBox.appendChild(h("div", { class: "row" },
      h("input", { type: "text", value: row.plan, placeholder: t("sup.planId"), oninput: (e) => { row.plan = e.target.value.trim(); } }),
      h("select", { value: row.role, onchange: (e) => { row.role = e.target.value; } }, roleOpts(row.role)),
      h("button", { class: "icon-btn", title: t("common.delete"), onclick: () => { plans.splice(i, 1); drawPlans(); } }, trashIcon()))));
  };
  drawPlans();
  const saveRules = h("button", { class: "btn primary", onclick: async () => {
    const plan_roles = {};
    for (const row of plans) if (row.plan && row.role) plan_roles[row.plan] = row.role;
    try {
      await api("PUT", "/api/supporters/settings", { role_id: roleSel.value, require_role_ids: Array.from(requireSel.selectedOptions).map((o) => o.value),
        min_amount: minInp.value.trim(), plan_roles, lookups_per_day: +lookupsInp.value || 10, dm_handling: dmBox.checked });
      toast(t("common.saved")); renderPage();
    } catch (e) { fail(e); }
  } }, t("common.save"));
  const rulesCard = h("div", { class: "card" }, h("h2", null, t("sup.rules")),
    field(t("sup.role"), roleSel), roleWarn,
    fieldHint(t("sup.require"), requireSel, t("sup.requireHint")),
    fieldHint(t("sup.minAmount"), minInp, t("sup.minHint")),
    h("details", { class: "acc" }, h("summary", null, t("sup.plans")),
      h("div", { class: "acc-body" }, h("div", { class: "sub small-text" }, t("sup.planHint")), planBox,
        h("div", { class: "row" }, h("button", { class: "btn small", onclick: () => { plans.push({ plan: "", role: "" }); drawPlans(); } }, t("sup.planAdd"))),
        field(t("sup.lookups"), lookupsInp))),
    h("h3", { class: "gap-top" }, t("sup.dm")),
    h("label", { class: "row" }, dmBox, t("sup.dmOn")),
    h("div", { class: "badge err warn-line" }, t("sup.dmWarn")),
    h("div", { class: "row end" }, saveRules));

  /* who to tell */
  const chanSel = h("select", { value: s.notify.channel_id }, h("option", { value: "" }, t("sup.notifyNone")),
    channels.map((c) => h("option", { value: c.id }, "# " + c.name)));
  const usersInp = h("input", { type: "text", inputmode: "numeric", autocomplete: "off", value: s.notify.user_ids.join(", ") });
  const saveNotify = h("button", { class: "btn primary", onclick: async () => {
    try {
      await api("PUT", "/api/supporters/settings", { notify: { channel_id: chanSel.value, user_ids: usersInp.value.split(",").map((x) => x.trim()).filter(Boolean) } });
      toast(t("common.saved")); renderPage();
    } catch (e) { fail(e); }
  } }, t("common.save"));
  const notifyCard = h("div", { class: "card" }, h("h2", null, t("sup.notify")), h("div", { class: "sub small-text" }, t("sup.notifyHint")),
    h("div", { class: "two" }, field(t("sup.notifyChannel"), chanSel), field(t("sup.notifyUsers"), usersInp)), h("div", { class: "row end" }, saveNotify));

  /* simulate a DM: asks Afdian and Discord (reads only), changes nothing */
  const simText = h("textarea", { placeholder: t("sup.simText") });
  simText.rows = 3;
  const simUser = h("input", { type: "text", inputmode: "numeric", autocomplete: "off" });
  const simOut = h("div", null);
  const simBtn = h("button", { class: "btn primary", onclick: async () => {
    simBtn.disabled = true;
    try {
      const r = await api("POST", "/api/supporters/simulate", { text: simText.value, user_id: simUser.value.trim() });
      clear(simOut);
      if (r.no_order) { simOut.appendChild(h("div", { class: "sub" }, t("sup.simNoOrder"))); return; }
      simOut.appendChild(h("div", { class: "lbl" }, t("sup.simReply")));
      simOut.appendChild(h("div", { class: "preview" }, r.reply || "—"));
      simOut.appendChild(h("div", { class: "lbl" }, t("sup.simRoles")));
      simOut.appendChild(h("div", null, r.grant.length ? r.grant.map((g) => h("span", { class: "badge" }, g.name)) : h("span", { class: "muted" }, t("sup.simNoRoles"))));
      if (r.notify.length) { simOut.appendChild(h("div", { class: "lbl" }, t("sup.simNotify"))); r.notify.forEach((n) => simOut.appendChild(h("div", { class: "preview" }, n))); }
    } catch (e) { fail(e); } finally { simBtn.disabled = false; }
  } }, t("sup.simGo"));
  const simCard = h("div", { class: "card" }, h("h2", null, t("sup.sim")), h("div", { class: "sub small-text" }, t("sup.simHint")),
    field(t("sup.simText"), simText), field(t("sup.simUser"), simUser), h("div", { class: "row" }, simBtn), simOut);

  /* recent orders */
  let showAmounts = false;
  const ordersBox = h("div", null);
  const amountsBox = h("input", { type: "checkbox", onchange: (e) => { showAmounts = e.target.checked; loadOrders(); } });
  /* `why` (a suggestion) carries the order's remark and the buyer's Afdian name, so the owner sees why it was suggested */
  async function link(o, userId, name, ask, why) {
    if (ask) {
      const text = h("div", null, t("sup.claim.confirm", { o: o.masked, n: name || userId }),
        why && why.buyer ? h("div", { class: "small-text" }, t("sup.col.buyer") + ": " + why.buyer) : null,
        why && why.remark ? h("div", { class: "preview" }, t("sup.suggest", { n: why.remark })) : null);
      if (!(await confirmBox(t("sup.claim.title"), text, t("sup.claim.go")))) return false;
    }
    try {
      const r = await api("POST", "/api/supporters/link", { order: o.id, user_id: userId });
      if (r.failed.length) toast(t("sup.claim.partial"), true);
      if (r.lookup !== "ok") toast(t("sup.claim.unchecked"), true);          // Afdian was unreachable or not set up
      else if (!r.failed.length) toast(t("sup.claim.done"));
      renderPage();
    } catch (e) { fail(e); return false; }
  }
  function openClaim(o) {
    const q = h("input", { type: "text", placeholder: t("sup.claim.search") });
    const idInp = h("input", { type: "text", inputmode: "numeric", autocomplete: "off" });
    const list = h("div", { class: "member-list" });
    let timer = null;
    async function search() {
      clear(list);
      if (!gid || !q.value.trim()) return;
      try {
        const r = await api("GET", "/api/guilds/" + gid + "/members?q=" + encodeURIComponent(q.value.trim()));
        if (!r.items.length) list.appendChild(h("div", { class: "muted small-text" }, t("sup.claim.none")));
        r.items.forEach((m) => list.appendChild(h("button", { class: "btn small", onclick: () => { idInp.value = m.id; clear(list).appendChild(h("div", { class: "small-text" }, t("sup.claim.pick", { n: m.display || m.username }))); } }, (m.display || m.username) + " · @" + m.username)));
      } catch (e) { list.appendChild(h("div", { class: "muted small-text" }, hasKey("err." + e.message) ? t("err." + e.message) : e.message)); }
    }
    q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 300); });
    modal(t("sup.claim.title"), h("div", null, h("div", { class: "sub small-text" }, t("sup.claim.for", { o: o.masked })),
      field(t("sup.claim.search"), q), list, field(t("sup.claim.id"), idInp)), [
      { label: t("common.cancel") },
      { label: t("sup.claim.go"), cls: "primary", run: async () => {
        const v = idInp.value.trim();
        if (!/^\d{1,20}$/.test(v)) { toast(t("sup.claim.badId"), true); return false; }
        return link(o, v, "", false);
      } }]);
  }
  function drawOrders(r) {
    clear(ordersBox);
    if (!r.items.length) { ordersBox.appendChild(h("div", { class: "muted pad" }, t("sup.noOrders"))); return; }
    if (!r.members_available) ordersBox.appendChild(h("div", { class: "sub small-text" }, t("sup.noMembers")));
    const head = h("tr", null, [t("sup.col.time"), t("sup.col.order"), t("sup.col.plan"), t("sup.col.buyer"), t("sup.col.amount"), t("sup.col.state")].map((x) => h("th", null, x)));
    const rows = r.items.map((o) => h("tr", { class: o.suggestions.length && !o.claim ? "hl" : "" },
      h("td", { class: "muted small-text" }, fmtTime(o.time) || "—"), h("td", null, h("code", null, o.masked)),
      h("td", null, o.title || (o.plan_id ? o.plan_id.slice(0, 6) : t("sup.customAmount"))),
      h("td", null, o.buyer || "—"),
      h("td", null, showAmounts ? (o.amount !== undefined ? o.amount : "—") : "•••"),
      h("td", null, o.claim ? h("span", { class: "badge" }, t("sup.claimed") + " @" + (o.claim.user_name || o.claim.user_id))
        : !o.paid ? h("span", { class: "badge idle" }, t("sup.unpaid"))
        : h("div", null, h("span", { class: "badge idle" }, t("sup.unclaimed")), " ",
            h("button", { class: "btn small", onclick: () => openClaim(o) }, t("sup.claimTo")),
            o.suggestions.map((sg) => h("div", null, h("span", { class: "small-text" }, t("sup.suggest", { n: sg.name }) + " "),
              h("button", { class: "btn small primary", onclick: () => link(o, sg.user_id, sg.name, true, sg) }, t("sup.claimToName", { n: sg.name }))))))));
    ordersBox.appendChild(h("table", null, h("thead", null, head), h("tbody", null, rows)));
  }
  async function loadOrders() {
    clear(ordersBox).appendChild(h("div", { class: "muted pad" }, t("common.loading")));
    try { drawOrders(await api("GET", "/api/supporters/orders" + (showAmounts ? "?amounts=1" : ""))); }
    catch (e) { clear(ordersBox).appendChild(h("div", { class: "sub small-text pad" }, hasKey("err." + e.message) ? t("err." + e.message) : e.message)); }
  }
  const ordersCard = h("div", { class: "card" }, h("div", { class: "row" }, h("h2", { class: "grow" }, t("sup.orders")),
    h("label", { class: "row" }, amountsBox, t("sup.showAmounts")), h("button", { class: "btn small", onclick: loadOrders }, t("common.refresh"))), ordersBox);

  /* claimed orders */
  const claimsBox = h("div", null);
  async function revoke(c) {
    const rm = h("input", { type: "checkbox", checked: true });
    const ok = await new Promise((resolve) => modal(t("sup.revokeTitle"),
      h("div", null, h("div", { class: "sub" }, t("sup.revokeText", { o: c.masked, n: c.user_name || c.user_id })), h("label", { class: "row gap-top" }, rm, t("sup.revokeRole"))),
      [{ label: t("common.cancel"), run: () => { resolve(false); } }, { label: t("sup.revoke"), cls: "danger solid", run: () => { resolve(true); } }]));
    if (!ok) return;
    try { await api("POST", "/api/supporters/unlink", { order: c.order, remove_role: rm.checked }); toast(t("sup.revoked")); renderPage(); } catch (e) { fail(e); }
  }
  async function loadClaims() {
    try {
      const r = await api("GET", "/api/supporters/claims");
      clear(claimsBox);
      if (!r.items.length) { claimsBox.appendChild(h("div", { class: "muted pad" }, t("sup.claimsNone"))); return; }
      claimsBox.appendChild(h("table", null, h("tbody", null, r.items.map((c) => h("tr", null,
        h("td", null, "@" + (c.user_name || c.user_id)), h("td", null, h("code", null, c.masked)), h("td", { class: "muted small-text" }, fmtTime(c.at)),
        h("td", { class: "small-text" }, c.plan_id || ""),
        h("td", null, h("button", { class: "btn small danger", onclick: () => revoke(c) }, t("sup.revoke"))))))));
    } catch (e) { clear(claimsBox); fail(e); }
  }
  const claimsCard = h("div", { class: "card" }, h("h2", null, t("sup.claims")), claimsBox);

  loadClaims();
  if (ov.token_set && s.afdian_user_id) loadOrders(); else clear(ordersBox).appendChild(h("div", { class: "sub small-text pad" }, t("err.afdian_not_set")));
  return h("div", null, issues, connCard, rulesCard, notifyCard, simCard, ordersCard, claimsCard);
}

/* ---------- models ---------- */
let LLM_EDIT = null;          // the profile open on the right, or "new"
async function pageLLM() {
  const [data, presetsRes, spendRes] = await Promise.all([api("GET", "/api/llm"), api("GET", "/api/llm/presets"), api("GET", "/api/feedback")]);
  const presets = presetsRes.items;
  if (LLM_EDIT !== "new" && !data.profiles.some((p) => p.id === LLM_EDIT)) LLM_EDIT = data.settings.active || (data.profiles[0] || { id: "new" }).id;
  if (!data.profiles.length) LLM_EDIT = "new";
  const right = h("div", { class: "card" });
  const left = h("div", { class: "card" });
  const reload = () => renderPage();
  function statusChip(p) {
    if (p.status === "ok") return h("span", { class: "chip draft" }, t("llm.ok"));
    if (p.status === "error") return h("span", { class: "chip new", title: p.last_error }, t("llm.err"));
    return null;
  }
  function drawLeft() {
    clear(left).append(h("h2", null, t("llm.all")),
      h("div", { class: "card-hint" }, t("llm.spend", { c: spendText(spendRes.spend) })));
    data.profiles.forEach((p) => {
      const active = data.settings.active === p.id;
      const star = h("span", { class: "star-btn", role: "button", title: t("llm.setActive"), onclick: async (ev) => {
        ev.stopPropagation();
        try { await api("POST", "/api/llm/" + p.id + "/activate", {}); toast(t("llm.switched", { n: p.name })); LLM_EDIT = p.id; reload(); } catch (e) { fail(e); }
      } }, icon("star", active ? "on" : ""));
      left.appendChild(h("button", { class: "model" + (p.id === LLM_EDIT ? " on" : ""), onclick: () => { LLM_EDIT = p.id; drawLeft(); drawRight(); } },
        h("span", { class: "model-r1" }, star, h("b", null, p.name), h("span", { class: "chip" }, p.protocol), active ? h("span", { class: "chip draft" }, t("llm.active")) : null, statusChip(p)),
        h("small", null, p.model), h("small", null, p.key_set ? "… " + p.key_hint : t("llm.nokey"))));
    });
    left.appendChild(h("div", { class: "row" }, h("button", { class: "btn", onclick: () => { LLM_EDIT = "new"; drawLeft(); drawRight(); } }, t("llm.new"))));
    left.appendChild(h("details", { class: "gap-top" }, h("summary", { class: "sub small-text" }, t("llm.retry")), retryBox()));
  }
  function retryBox() {
    const fb = h("label", { class: "row gap-top" }, h("input", { type: "checkbox", checked: data.settings.fallback, onchange: async (e) => { try { data.settings = await api("PUT", "/api/llm/settings", { fallback: e.target.checked }); toast(t("common.saved")); } catch (x) { fail(x); } } }), t("llm.fallback"));
    const retries = h("input", { type: "number", min: "0", max: "5", value: data.settings.retries });
    const timeout = h("input", { type: "number", min: "5", max: "600", value: data.settings.timeout_s });
    return h("div", null, fb, h("div", { class: "gap" }), field(t("llm.retries"), retries), field(t("llm.timeout"), timeout), h("div", { class: "sub small-text" }, t("llm.fallbackNote")),
      h("div", { class: "row end gap-top" }, h("button", { class: "btn small", onclick: async () => { try { data.settings = await api("PUT", "/api/llm/settings", { retries: +retries.value, timeout_s: +timeout.value }); toast(t("common.saved")); } catch (e) { fail(e); } } }, t("common.save"))));
  }
  function form(p) {
    const presetSel = p ? null : h("select", null, presets.map((x) => h("option", { value: x.id }, x.name)));
    const name = h("input", { type: "text", value: p ? p.name : "" }), base = h("input", { type: "url", value: p ? p.base_url || "" : "", placeholder: "https://" });
    const dl = h("datalist", { id: "model-suggest" });
    const model = h("input", { type: "text", list: "model-suggest", value: p ? p.model : "" });
    const key = h("input", { type: "password", autocomplete: "off", placeholder: p && p.key_set ? t("llm.keyKeep") : "" });
    const maxTok = h("input", { type: "number", min: "1", value: String(p ? p.max_tokens || 2048 : 2048) });
    const maxConc = h("input", { type: "number", min: "1", max: "16", value: String(p ? p.max_concurrent || 2 : 2) });
    const apply = () => {
      const x = presets.find((y) => y.id === presetSel.value);
      name.value = x.name; base.value = x.base_url; model.value = x.model;
      clear(dl); x.models.forEach((m) => dl.appendChild(h("option", { value: m })));
    };
    if (presetSel) { presetSel.addEventListener("change", apply); apply(); }
    const save = async () => {
      try {
        if (p) {
          await api("PUT", "/api/llm/" + p.id, { name: name.value, base_url: base.value, model: model.value, max_tokens: +maxTok.value, max_concurrent: +maxConc.value });
          if (key.value.trim()) await api("POST", "/api/llm/" + p.id + "/key", { key: key.value.trim() });
          toast(t("common.saved"));
        } else {
          const x = presets.find((y) => y.id === presetSel.value);
          const r = await api("POST", "/api/llm", { preset: presetSel.value, name: name.value, protocol: x.protocol, base_url: base.value, model: model.value, key: key.value, max_tokens: +maxTok.value, max_concurrent: +maxConc.value });
          if (r.key_error) toast(r.key_error, true);
          LLM_EDIT = r.id;
        }
        reload();
      } catch (e) { fail(e); }
    };
    const test = p ? h("button", { class: "btn", onclick: async (ev) => {
      ev.target.disabled = true; toast(t("llm.testing"));
      try { const r = await api("POST", "/api/llm/" + p.id + "/test", {}); toast(r.ok ? t("llm.testOk", { t: r.text }) : (r.code === "empty_text" ? t("llm.testEmpty") : r.error), !r.ok); reload(); }
      catch (e) { fail(e); ev.target.disabled = false; }
    } }, t("llm.test")) : null;
    const del = p ? h("button", { class: "icon-btn", title: t("common.delete"), onclick: async () => {
      if (await confirmBox(t("llm.delTitle"), t("llm.delText"), t("common.delete"), true)) { try { await api("DELETE", "/api/llm/" + p.id, {}); LLM_EDIT = null; reload(); } catch (e) { fail(e); } }
    } }, trashIcon()) : null;
    return h("div", null, h("h2", null, p ? t("common.edit") + "：" + p.name : t("llm.new")),
      presetSel ? field(t("llm.preset"), presetSel) : null, field(t("common.name"), name), field(t("llm.baseUrl"), base), fieldHint(t("llm.model"), model, t("llm.modelHint")), dl,
      field(t("llm.key"), key), h("div", { class: "sub small-text" }, t("llm.keyNote")), h("div", { class: "gap" }),
      h("div", { class: "two" }, field(t("llm.maxTokens"), maxTok), fieldHint(t("llm.maxConcurrent"), maxConc, t("llm.maxConcurrentHint"))),
      data.keyring ? null : h("div", { class: "badge err warn-line" }, t("llm.nokeyring", { v: "HEARBACK_LLM_KEY_<ID>" })),
      h("div", { class: "acts" }, h("button", { class: "btn primary", onclick: save }, t("common.save")), test, h("span", { class: "grow" }), del));
  }
  function drawRight() {
    clear(right).appendChild(form(LLM_EDIT === "new" ? null : data.profiles.find((p) => p.id === LLM_EDIT)));
  }
  drawLeft(); drawRight();
  return h("div", { class: "mgrid" }, left, right);
}

/* ---------- agents ("人设"): which forum uses which, and their words ---------- */
/* the two built-in agents speak the page's language until someone renames them */
function agentName(a) {
  if (a.id === "auto" && a.name === "Auto") return t("agent.auto");
  if (a.id === "bug-desk" && a.name === "Bug desk") return t("agent.bug");
  return a.name;
}
async function pageAgents() {
  const [res, llm, fb] = await Promise.all([api("GET", "/api/agents"), api("GET", "/api/llm"), api("GET", "/api/feedback")]);
  function edit(a) {
    const name = h("input", { type: "text", value: a ? a.name : "" }), desc = h("input", { type: "text", value: a ? a.description : "" });
    const prompt = h("textarea", { value: a ? a.system_prompt : "" });
    prompt.rows = 12;
    const prof = h("select", { value: a && a.profile_id ? a.profile_id : "" }, h("option", { value: "" }, t("agent.default")), llm.profiles.map((p) => h("option", { value: p.id }, p.name)));
    modal(a ? t("common.edit") : t("agent.new"), h("div", null, field(t("common.name"), name), field(t("agent.desc"), desc), field(t("agent.prompt"), prompt), field(t("agent.llm"), prof)),
      [{ label: t("common.cancel") }, { label: t("common.save"), cls: "primary", run: async () => {
        const body = { name: name.value, description: desc.value, system_prompt: prompt.value, profile_id: prof.value || null };
        try { await api(a ? "PUT" : "POST", "/api/agents" + (a ? "/" + a.id : ""), body); renderPage(); } catch (e) { fail(e); return false; }
      } }], true);
  }
  const setForum = async (f, body) => {
    try { await api("PUT", "/api/feedback/forums/" + f.id, body); toast(t("common.saved")); await refreshStatus(); } catch (e) { fail(e); renderPage(); }
  };
  const forumRows = fb.forums.map((f) => {
    const kind = forumKind(f.name);
    const row = h("div", { class: "srow" + (f.hidden ? " off" : "") });
    const hide = h("input", { type: "checkbox", checked: f.hidden, onchange: async (e) => { row.classList.toggle("off", e.target.checked); await setForum(f, { hidden: e.target.checked }); } });
    row.append(h("span", { class: "tile " + kind }, icon(KIND_ICON[kind])),
      h("div", { class: "grow" }, forumName(f), h("small", null, f.tags.length ? t("agent.tags", { n: f.tags.length }) : t("agent.noTags"))),
      h("label", { class: "row small-text sub" }, hide, t("agent.hide")),
      h("select", { value: f.agent, "aria-label": forumName(f), onchange: (e) => setForum(f, { agent: e.target.value }) },
        res.items.map((a) => h("option", { value: a.id }, agentName(a))), h("option", { value: "none" }, t("agent.none"))));
    return row;
  });
  return h("div", null,
    h("div", { class: "card" }, h("h2", null, t("agent.forums")), h("div", { class: "card-hint" }, t("agent.forumsHint")),
      forumRows.length ? forumRows : h("div", { class: "muted" }, t("agent.noForums"))),
    h("div", { class: "card" }, h("div", { class: "row" }, h("h2", { class: "grow" }, t("agent.all")), h("button", { class: "btn", onclick: () => edit(null) }, t("agent.new"))),
      res.items.map((a) => h("div", { class: "srow" },
        h("div", { class: "grow" }, agentName(a), h("small", null, a.description || a.system_prompt.slice(0, 80))),
        h("button", { class: "btn small", onclick: () => edit(a) }, t("common.edit")),
        a.id === "auto" ? null : h("button", { class: "icon-btn", title: t("common.delete"), onclick: async () => { if (await confirmBox(t("agent.delTitle"), a.name, t("common.delete"), true)) { try { await api("DELETE", "/api/agents/" + a.id, {}); renderPage(); } catch (e) { fail(e); } } } }, trashIcon())))));
}

/* ---------- channel rules ---------- */
async function pageRules() {
  const [rr, gr] = await Promise.all([api("GET", "/api/rules"), api("GET", "/api/guilds")]);
  const rules = rr.rules, agents = rr.agents;
  const dirty = h("span", { class: "badge err hidden" }, t("rules.dirty"));
  const mark = () => dirty.classList.remove("hidden");
  const rowControls = (rule, apply, isAutoCheck) => {
    const mode = modeSelect(rule.mode, async (m) => {
      if (m === "auto" && isAutoCheck && !(await confirmBox(t("conv.autoTitle"), t("conv.autoText"), t("mode.auto"), false))) { mode.value = rule.mode; return; }
      apply({ mode: m }); mark();
    });
    const agent = h("select", { value: rule.agent, onchange: (e) => { apply({ agent: e.target.value }); mark(); } }, agents.map((a) => h("option", { value: a.id }, a.name)));
    const trig = h("select", { value: rule.trigger, onchange: (e) => { apply({ trigger: e.target.value }); mark(); } }, ["mention", "all"].map((k) => h("option", { value: k }, t("trigger." + k))));
    const cd = h("input", { type: "number", min: "0", max: "3600", value: rule.cooldown_s, onchange: (e) => { apply({ cooldown_s: +e.target.value }); mark(); } });
    return [mode, agent, trig, cd];
  };
  const defaultRow = (title, key) => h("div", { class: "rule-row" }, h("strong", null, title), rowControls(rules[key], (p) => Object.assign(rules[key], p), true));
  const head = h("div", { class: "rule-row sub small-text" }, h("span", null, t("rules.channel")), h("span", null, t("rules.mode")), h("span", null, t("rules.agent")), h("span", null, t("rules.trigger")), h("span", null, t("rules.cooldown")));
  const legend = h("div", { class: "card legend" }, h("h2", null, t("rules.legend")),
    h("ul", null, h("li", null, t("rules.legend.off")), h("li", null, t("rules.legend.draft")), h("li", null, t("rules.legend.auto"))),
    h("div", { class: "sub small-text" }, t("rules.legend.trigger")));
  const defaults = h("div", { class: "card" }, head, defaultRow(t("rules.dm"), "dm_default"), defaultRow(t("rules.guild"), "guild_default"));
  const accs = gr.items.map((g) => h("details", { class: "acc", open: true }, h("summary", null, g.name),
    h("div", { class: "acc-body" }, g.channels.map((c) => {
      const cur = () => rules.channels[c.id] || Object.assign({}, rules.guild_default);
      return h("div", { class: "rule-row" }, h("span", null, "# " + c.name), rowControls(cur(), (p) => { rules.channels[c.id] = Object.assign(cur(), p); }, true));
    }))));
  const save = h("button", { class: "btn primary", onclick: async () => { try { await api("PUT", "/api/rules", rules); toast(t("rules.saved")); renderPage(); } catch (e) { fail(e); } } }, t("common.save"));
  return h("div", null, legend, h("div", { class: "row end" }, dirty, save), defaults,
    accs.length ? accs : empty(t("rules.empty.title"), gr.reason === "no_token" ? t("err.no_token") : t("rules.empty.text"), goBtn("settings", t("nav.settings"))));
}

/* ---------- logs ---------- */
async function pageLogs() {
  const res = await api("GET", "/api/logs?limit=200");
  const refresh = h("button", { class: "btn", onclick: () => renderPage() }, t("common.refresh"));
  if (!res.items.length) return h("div", null, h("div", { class: "row end" }, refresh), empty(t("logs.empty"), ""));
  return h("div", null, h("div", { class: "row end" }, refresh), h("div", { class: "card" },
    h("div", { class: "log-line sub small-text" }, h("div", null, t("logs.time")), h("div", null, t("logs.actor")), h("div", null, t("logs.action")), h("div", null, t("logs.detail"))),
    res.items.map((e) => h("div", { class: "log-line" }, h("div", { class: "muted" }, fmtTime(e.t)), h("div", null, e.actor), h("div", null, e.action),
      h("div", { class: "sub" }, (e.thread ? e.thread + " " : "") + (e.detail ? JSON.stringify(e.detail) : ""))))));
}

/* ---------- settings: what is rarely needed ---------- */
async function pageSettings() {
  const [s, rr, st] = await Promise.all([api("GET", "/api/settings"), api("GET", "/api/rules"), refreshStatus()]);
  const rules = rr.rules;
  const online = st.bot.state === "online", running = online || st.bot.state === "connecting";

  /* first steps, only while something is missing */
  const item = (n, ok, title, why, action, optional) => h("li", { class: "todo" },
    h("span", { class: "tick" + (ok ? " on" : "") }, ok ? "✓" : String(n)),
    h("div", { class: "grow" }, h("div", null, title, optional ? h("span", { class: "badge idle opt" }, t("common.optional")) : null), h("div", { class: "sub small-text" }, why)),
    ok ? null : action);
  const portal = h("a", { class: "btn small", href: PORTAL, target: "_blank", rel: "noopener noreferrer" }, t("dash.openPortal"));
  const setup = !st.checklist.token || !st.checklist.llm || !online ? h("div", { class: "card" }, h("h2", null, t("dash.start")),
    h("ul", { class: "checklist" },
      item(1, st.checklist.token, t("dash.c1"), t("dash.c1w")),
      item(2, st.checklist.llm, t("dash.c2"), t("dash.c2w"), goBtn("llm", t("dash.goModels"), false)),
      item(3, online, t("dash.c3"), t("dash.c3w"), portal),
      item(4, st.checklist.rule, t("dash.c4"), t("dash.c4w"), goBtn("rules", t("dash.goRules"), false), true),
      item(5, !!s.guild_id, t("dash.c5"), t("dash.c5w"), null, true))) : null;

  const row = (title, sub, action) => h("div", { class: "srow" }, h("div", { class: "grow" }, title, sub ? h("small", null, sub) : null), action);
  const open = (p) => h("button", { class: "btn small", onclick: () => go(p) }, t("set.open"));

  const tok = h("input", { type: "password", autocomplete: "off", placeholder: t("set.tokenReplace") });
  const saveTok = h("button", { class: "btn primary", onclick: async () => {
    try { await api("PUT", "/api/settings/token", { token: tok.value }); tok.value = ""; toast(t("set.tokenSaved")); renderPage(); } catch (e) { fail(e); } } }, t("set.tokenSave"));
  const dm = modeSelect(rules.dm_default.mode, async (m) => {
    if (m === "auto" && !(await confirmBox(t("conv.autoTitle"), t("conv.autoText"), t("mode.auto"), false))) { dm.value = rules.dm_default.mode; return; }
    rules.dm_default.mode = m;
    try { await api("PUT", "/api/rules", rules); toast(t("common.saved")); } catch (e) { fail(e); }
  });
  const guildInp = h("input", { type: "text", inputmode: "numeric", autocomplete: "off", value: s.guild_id || "" });
  const forumInp = h("input", { type: "text", inputmode: "numeric", autocomplete: "off", value: s.forum_id || "" });
  const saveForum = h("button", { class: "btn primary", onclick: async () => {
    const ok = (v) => !v.trim() || /^\d{17,20}$/.test(v.trim());
    if (!ok(guildInp.value) || !ok(forumInp.value)) { toast(t("set.forumBad"), true); return; }
    try { await api("PUT", "/api/settings/forum", { guild_id: guildInp.value.trim(), forum_id: forumInp.value.trim() }); toast(t("set.forumSaved")); renderPage(); } catch (e) { fail(e); } } }, t("set.forumSave"));
  const exportBtn = h("button", { class: "btn small", onclick: async () => {
    exportBtn.disabled = true;
    try {
      const r = await api("POST", "/api/forum/export", {});
      const pre = h("pre", { class: "preview export" }, r.markdown);
      modal(t("forum.exportTitle") + " (" + r.count + ")", h("div", null, h("div", { class: "sub small-text" }, t("forum.exportSaved", { p: r.path })), pre), [
        { label: t("common.copy"), run: async () => {
          try { await navigator.clipboard.writeText(r.markdown); toast(t("common.copied")); }
          catch (e) { const sel = window.getSelection(); const range = document.createRange(); range.selectNodeContents(pre); sel.removeAllRanges(); sel.addRange(range); }
          return false; } },
        { label: t("common.download"), run: () => {
          const a = h("a", { href: URL.createObjectURL(new Blob([r.markdown], { type: "text/markdown" })), download: r.filename });
          document.body.appendChild(a); a.click(); a.remove(); return false; } },
        { label: t("common.close"), cls: "primary" }], true);
    } catch (e) { fail(e); } finally { exportBtn.disabled = false; }
  } }, t("forum.export"));

  return h("div", null, setup,
    h("div", { class: "card" }, h("h2", null, t("set.bot")),
      row((st.bot.bot_name || t("fb.bot")) + " · " + t("bot." + st.bot.state), botHint(st.bot),
        h("button", { class: "btn small" + (running ? "" : " primary"), onclick: toggleBot }, running ? t("bot.stop") : t("bot.start"))),
      s.welcome_file ? row(t("set.welcome"), t("set.welcomeHint", { p: s.welcome_file })) : null,
      row(t("set.dm"), t("set.cap", { n: s.hourly_cap }), dm),
      s.invite_url ? row(t("set.invite"), t("set.inviteWhat"), h("a", { class: "btn small", href: s.invite_url, target: "_blank", rel: "noopener noreferrer" }, t("set.open"))) : null),
    h("div", { class: "card" }, h("h2", null, t("set.rare")),
      row(t("nav.conversations"), t("sub.conversations"), open("conversations")),
      row(t("nav.rules"), t("sub.rules"), open("rules")),
      row(t("nav.botmsgs"), t("sub.botmsgs"), open("botmsgs")),
      row(t("nav.approvals"), t("sub.approvals"), open("approvals")),
      row(t("nav.supporters"), t("sub.supporters"), open("supporters")),
      row(t("forum.export"), t("set.exportWhat"), exportBtn),
      row(t("nav.logs"), t("sub.logs"), open("logs"))),
    h("details", { class: "acc gap-top" }, h("summary", null, t("set.token")),
      h("div", { class: "acc-body" }, h("div", { class: "sub small-text" }, t("set.tokenWhat")),
        h("div", { class: "row gap-top" }, h("span", { class: "badge" + (s.token.set ? " ok" : " err") }, s.token.set ? t("set.tokenSet") : t("set.tokenNot")), s.token.source === "env" ? h("span", { class: "sub small-text" }, t("set.tokenEnv")) : null),
        h("div", { class: "row gap-top" }, h("div", { class: "grow" }, tok), saveTok),
        h("h3", { class: "gap-top" }, t("set.portal")),
        h("ol", { class: "steps" }, h("li", null, t("set.step1")), h("li", null, t("set.step2")), h("li", null, t("set.step3"))),
        h("a", { href: s.portal_url, target: "_blank", rel: "noopener noreferrer" }, t("set.openPortal")))),
    h("details", { class: "acc" }, h("summary", null, t("set.forum")),
      h("div", { class: "acc-body" }, h("div", { class: "sub small-text" }, t("set.forumWhat")), h("div", { class: "gap" }),
        h("div", { class: "two" }, field(t("set.guildId"), guildInp), field(t("set.forumId"), forumInp)),
        h("div", { class: "row end" }, saveForum))),
    h("div", { class: "sub small-text gap-top" }, "SWLink v" + s.version + " · " + t("set.port") + " " + s.port));
}

const PAGES = { inbox: pageInbox, conversations: pageConversations, approvals: pageApprovals, botmsgs: pageBotMsgs, supporters: pageSupporters, llm: pageLLM, agents: pageAgents, rules: pageRules, logs: pageLogs, settings: pageSettings };

/* ---------- boot ---------- */
function boot() {
  const forced = /[#&]lang=(zh|en)/.exec(location.hash);          // the demo link carries its language
  if (forced) store("setL", "swlink.lang", forced[1]);
  const saved = forced ? forced[1] : store("get", "swlink.lang");
  LANG = saved === "zh" || saved === "en" ? saved : ((navigator.language || "en").toLowerCase().startsWith("zh") ? "zh" : "en");
  document.documentElement.lang = LANG === "zh" ? "zh-CN" : "en";
  THEME = store("get", "swlink.theme") === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = THEME;
  const m = /[#&]k=([^&]+)/.exec(location.hash);
  if (m) {
    KEY = decodeURIComponent(m[1]);
    store("setS", "swlink.key", KEY);
    history.replaceState(null, "", location.pathname + location.search);
  } else KEY = store("get", "swlink.key") || "";
  if (!KEY) { showLocked(); return; }
  $("app").classList.remove("hidden");
  window.addEventListener("hashchange", renderPage);
  renderTop();
  refreshStatus().then(() => {
    if (!STATUS.token_set && !location.hash) go("settings"); else renderPage();
  }).catch(() => renderPage());
  setInterval(() => { if (!document.hidden) refreshStatus().catch(() => {}); }, 5000);
  setInterval(() => { if (!document.hidden && PAGE === "inbox" && FB.doms) pull(false); }, 120000);
}
boot();
