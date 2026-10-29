"use strict";
/* SWLink console: vanilla JS, no build step. All server and user text goes through textContent (see h()).
   The texts live in i18n.js (loaded first). */

const NAV = [
  ["nav.features", ["dashboard", "conversations", "approvals", "forum", "botmsgs", "supporters"]],
  ["nav.system", ["llm", "agents", "rules", "logs", "settings"]],
];
const MODES = ["off", "draft", "auto"];
const STATUSES = ["new", "investigating", "need-info", "fixed-pending-release", "released", "wontfix", "not-bug", "duplicate", "guide"];
const PORTAL = "https://discord.com/developers/applications";

let LANG = "en";
let KEY = "";
let PAGE = "dashboard";
let STATUS = null;
let RENDER_SEQ = 0;
let POLL = null;                 // timer of the running AI review, if any
let SEEN_JOB = "";               // the review job this page has watched
const HOWTO_HIDDEN = {};         // pages whose strip was dismissed (kept even when storage is blocked)
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

function toast(msg, bad) {
  const el = h("div", { class: "toast" + (bad ? " err" : "") }, msg);
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), bad ? 6000 : 3000);
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
function empty(title, text, btn) { return h("div", { class: "card empty" }, h("h3", null, title), h("div", null, text), btn); }
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
function statusLabel(s) { return hasKey("forum.st." + s) ? t("forum.st." + s) : s; }
function sevBadge(s) { return s ? h("span", { class: "badge sev-" + s }, t("sev." + s)) : null; }
function stopPolling() { if (POLL) { clearInterval(POLL); POLL = null; } }

/* ---------- shell ---------- */
function renderShell() {
  const main = document.querySelector(".main");
  if (main) main.classList.toggle("fill", PAGE === "conversations");      // the chat page uses the whole window height
  const side = clear($("side"));
  side.appendChild(h("div", { class: "brand" }, h("img", { src: "/swlink.svg", alt: "" }), h("span", null, "SWLink")));
  const sp = spacePicker();
  if (sp) side.appendChild(sp);
  for (const [gk, pages] of NAV) {
    side.appendChild(h("div", { class: "nav-group" }, t(gk)));
    for (const p of pages) side.appendChild(h("button", { class: "nav-item" + (p === PAGE ? " active" : ""), onclick: () => go(p) }, t("nav." + p)));
  }
  side.appendChild(h("div", { class: "side-foot" }, "v" + ((STATUS && STATUS.version) || "")));
  renderTop();
  renderDemo();
  renderHowto();
}
/* server picker: one bot, several workspace folders ([swlink] workspaces); switching re-renders every page */
function spacePicker() {
  if (!SPACES || (SPACES.items.length < 2 && !SPACES.broken.length)) return null;
  const sel = h("select", { class: "space-select", "aria-label": t("ws.label"), onchange: (ev) => openSpace(ev.target.value) },
    SPACES.items.map((w) => h("option", { value: w.id, selected: w.current }, w.name + (w.home ? t("ws.home") : ""))));
  const box = h("div", { class: "space" }, h("div", { class: "nav-group" }, t("ws.label")), sel);
  for (const b of SPACES.broken) box.appendChild(h("div", { class: "sub small-text space-broken", title: b.error }, t("ws.broken", { f: b.folder })));
  return box;
}
async function loadSpaces() {
  try { SPACES = await api("GET", "/api/workspaces"); } catch (e) { SPACES = null; }
}
async function openSpace(id) {
  try {
    SPACES = await api("POST", "/api/workspaces/" + encodeURIComponent(id) + "/open", {});
    const w = SPACES.items.find((x) => x.current);
    if (w) toast(t("ws.opened", { n: w.name }));
    await refreshStatus();
  } catch (e) { fail(e); }
  renderPage();
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
  const b = (STATUS && STATUS.bot) || { state: "offline", reason: "" };
  const running = b.state === "online" || b.state === "connecting";
  top.appendChild(h("div", null, h("h1", null, t("nav." + PAGE)), h("div", { class: "sub" }, t("sub." + PAGE))));
  top.appendChild(h("div", { class: "top-right" },
    h("span", { class: "pill " + b.state, title: botHint(b) }, h("span", { class: "dot" }), t("bot." + b.state)),
    h("button", { class: "btn small " + (running ? "" : "primary"), onclick: toggleBot }, running ? t("bot.stop") : t("bot.start")),
    h("button", { class: "help", title: t("howto.show"), "aria-label": t("howto.show"), onclick: toggleHowto }, "?"),
    h("button", { class: "lang", onclick: toggleLang }, LANG === "zh" ? "EN" : "中")));
}

/* "How to use" strip: 2 to 3 numbered steps per page. Dismissal is remembered in localStorage (inside try/catch). */
function howtoKey(page) { return "swlink.howto." + page; }
function howtoHidden(page) {
  if (HOWTO_HIDDEN[page] !== undefined) return HOWTO_HIDDEN[page];
  try { return localStorage.getItem(howtoKey(page)) === "1"; } catch (e) { return false; }
}
function setHowtoHidden(page, hidden) {
  HOWTO_HIDDEN[page] = hidden;
  try { if (hidden) localStorage.setItem(howtoKey(page), "1"); else localStorage.removeItem(howtoKey(page)); } catch (e) { /* blocked */ }
}
function renderHowto() {
  const box = clear($("howto"));
  if (howtoHidden(PAGE)) return;
  const steps = [];
  for (let i = 1; hasKey("howto." + PAGE + "." + i); i++) steps.push(t("howto." + PAGE + "." + i));
  if (!steps.length) return;
  box.appendChild(h("div", { class: "howto" },
    h("div", { class: "howto-head" }, h("strong", null, t("howto.title")),
      h("button", { class: "btn small", onclick: () => { setHowtoHidden(PAGE, true); renderHowto(); } }, t("howto.hide"))),
    h("ol", { class: "howto-steps" }, steps.map((s) => h("li", null, s)))));
}
function toggleHowto() { setHowtoHidden(PAGE, !howtoHidden(PAGE)); renderHowto(); }

async function toggleBot() {
  const running = STATUS && STATUS.bot && (STATUS.bot.state === "online" || STATUS.bot.state === "connecting");
  try { await api("POST", "/api/bot/" + (running ? "stop" : "start")); await refreshStatus(); } catch (e) { fail(e); }
  setTimeout(() => refreshStatus().catch(() => {}), 1500);
}
function toggleLang() {
  LANG = LANG === "zh" ? "en" : "zh";
  store("setL", "swlink.lang", LANG);
  document.documentElement.lang = LANG === "zh" ? "zh-CN" : "en";
  renderShell();
  renderPage();
}
async function refreshStatus() {
  STATUS = await api("GET", "/api/status");
  if (STATUS.workspaces > 1) {
    const before = JSON.stringify(SPACES);
    await loadSpaces();
    if (JSON.stringify(SPACES) !== before) renderShell();   // only on a real change: a rebuilt <select> closes itself
  }
  renderTop();
  renderDemo();
  const v = document.querySelector(".side-foot");
  if (v) v.textContent = "v" + STATUS.version;
  return STATUS;
}

function pageFromHash() {
  const m = /^#\/([a-z]+)/.exec(location.hash);
  return m && m[1] in PAGES ? m[1] : "dashboard";
}
async function renderPage() {
  const seq = ++RENDER_SEQ;
  stopPolling();
  PAGE = pageFromHash();
  renderShell();
  const view = clear($("view"));
  view.appendChild(h("div", { class: "muted" }, t("common.loading")));
  try {
    const node = await PAGES[PAGE]();
    if (seq !== RENDER_SEQ) return;
    clear(view).appendChild(node);
  } catch (e) { if (seq === RENDER_SEQ) { clear(view); fail(e); } }
}

/* ---------- dashboard ---------- */
async function pageDashboard() {
  const s = await refreshStatus();
  const metric = (label, value, note) => h("div", { class: "card metric" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), note ? h("div", { class: "note" }, note) : null);
  const item = (n, ok, title, why, action, optional) => h("li", { class: "todo" },
    h("span", { class: "tick" + (ok ? " on" : "") }, ok ? "✓" : String(n)),
    h("div", { class: "grow" }, h("div", null, title, optional ? h("span", { class: "badge idle opt" }, t("common.optional")) : null), h("div", { class: "sub small-text" }, why)),
    ok ? null : action);
  const online = s.bot.state === "online";
  const portal = h("a", { class: "btn small", href: PORTAL, target: "_blank", rel: "noopener noreferrer" }, t("dash.openPortal"));
  const metrics = [
    metric(t("dash.status"), t("bot." + s.bot.state), s.bot.reason ? botHint(s.bot) : (s.llm ? t("dash.model") + ": " + s.llm.name + " · " + s.llm.model : t("dash.nomodel"))),
    metric(t("dash.servers"), String(s.guilds)), metric(t("dash.pending"), String(s.pending)), metric(t("dash.auto"), String(s.auto_today))];
  if (s.forum_configured) metrics.push(metric(t("dash.toreview"), String(s.forum_to_review)));
  if (s.supporters) metrics.push(metric(t("dash.supporters"), String(s.supporters.claims), s.supporters.retries ? t("dash.supNote", { n: s.supporters.retries }) : null));
  return h("div", null,
    h("div", { class: "metrics" }, metrics),
    h("div", { class: "card" }, h("h2", null, t("dash.start")),
      h("ul", { class: "checklist" },
        item(1, s.checklist.token, t("dash.c1"), t("dash.c1w"), goBtn("settings", t("dash.go"), false)),
        item(2, s.checklist.llm, t("dash.c2"), t("dash.c2w"), goBtn("llm", t("dash.goModels"), false)),
        item(3, online, t("dash.c3"), t("dash.c3w"), portal),
        item(4, s.checklist.rule, t("dash.c4"), t("dash.c4w"), goBtn("rules", t("dash.goRules"), false)),
        item(5, s.forum_configured, t("dash.c5"), t("dash.c5w"), goBtn("settings", t("dash.go"), false), true))));
}

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
  if (!pend.items.length && !appr.items.length && !failed.items.length) return empty(t("appr.empty.title"), t("appr.empty.text"), goBtn("conversations", t("appr.goChat")));
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

/* ---------- bug forum ---------- */
async function pageForum() {
  const f = await api("GET", "/api/forum");
  if (!f.configured) return empty(t("forum.empty.title"), t("forum.empty.text"), goBtn("settings", t("nav.settings")));
  const st0 = await api("GET", "/api/forum/review");
  const state = { filter: "all", open: new Set() };
  const progress = h("div", { class: "card review-box hidden" });
  const chips = h("div", { class: "chips" });
  const list = h("div", { class: "threads" });
  const btnSync = h("button", { class: "btn", onclick: doSync }, t("forum.sync"));
  const btnReview = h("button", { class: "btn primary", onclick: doReview }, t("forum.review"));
  const btnExport = h("button", { class: "btn", onclick: doExport }, t("forum.export"));
  const toolbar = h("div", { class: "toolbar" }, btnSync, btnReview, btnExport, h("span", { class: "grow" }),
    f.synced_at ? h("span", { class: "muted small-text" }, fmtTime(f.synced_at)) : null);

  async function doSync() {
    btnSync.disabled = true; toast(t("forum.syncing"));
    try { const r = await api("POST", "/api/forum/sync", {}); toast(t("forum.synced") + ": " + r.summary.split("\n")[0]); renderPage(); }
    catch (e) { fail(e); btnSync.disabled = false; }
  }
  async function doReview() {
    if (!f.model) { toast(t("forum.reviewNoModel"), true); return; }
    const total = f.to_review;
    if (!total) { toast(t("forum.reviewNothing")); return; }
    const n = Math.min(total, f.review_limit);
    const agents = (await api("GET", "/api/agents")).items;
    const agentSel = h("select", { value: agents.some((a) => a.id === "bug-desk") ? "bug-desk" : "auto" }, agents.map((a) => h("option", { value: a.id }, a.name)));
    const body = h("div", null, h("div", null, t("forum.reviewText", { n, m: f.model })),
      total > n ? h("div", { class: "sub small-text" }, t("forum.reviewMore", { n: f.review_limit })) : null,
      h("div", { class: "gap" }), field(t("forum.reviewAgent"), agentSel));
    const ok = await new Promise((resolve) => modal(t("forum.reviewTitle"), body, [
      { label: t("common.cancel"), run: () => { resolve(false); } }, { label: t("forum.reviewGo"), cls: "primary", run: () => { resolve(true); } }]));
    if (!ok) return;
    try {
      const r = await api("POST", "/api/forum/review", { limit: f.review_limit, agent: agentSel.value });
      SEEN_JOB = r.job;
      drawProgress(await api("GET", "/api/forum/review"));
      startPolling();
    } catch (e) { fail(e); }
  }
  async function doExport() {
    btnExport.disabled = true; toast(t("forum.exporting"));
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
    } catch (e) { fail(e); } finally { btnExport.disabled = false; }
  }

  function drawProgress(st) {
    clear(progress);
    const show = st.job && (st.running || (st.job === SEEN_JOB) || st.errors.length > 0);
    progress.classList.toggle("hidden", !show);
    btnReview.disabled = !!st.running;
    if (!show) return;
    if (st.running) {
      const stopBtn = h("button", { class: "btn small danger", onclick: async () => { stopBtn.disabled = true; try { await api("POST", "/api/forum/review/stop", {}); } catch (e) { fail(e); } } }, t("forum.stop"));
      progress.appendChild(h("div", { class: "row" }, h("strong", { class: "grow" }, t("forum.progress", { d: st.done, t: st.total })), st.stopped ? h("span", { class: "muted small-text" }, t("forum.stopping")) : stopBtn));
      progress.appendChild(h("progress", { value: st.done, max: Math.max(st.total, 1) }));
      if (st.current) progress.appendChild(h("div", { class: "sub small-text" }, t("forum.looking", { c: st.current })));
    } else {
      progress.appendChild(h("div", { class: "row" }, h("strong", { class: "grow" }, t(st.stopped ? "forum.reviewStopped" : "forum.reviewDone", { ok: st.reviewed, bad: st.errors.length }))));
    }
    if (st.errors.length) {
      progress.appendChild(h("div", { class: "sub small-text err-title" }, t("forum.errors")));
      st.errors.forEach((er) => progress.appendChild(h("div", { class: "small-text err-line" }, er.title + ": " + er.error)));
    }
  }
  function startPolling() {
    stopPolling();
    POLL = setInterval(async () => {
      try {
        const st = await api("GET", "/api/forum/review");
        drawProgress(st);
        if (!st.running) { stopPolling(); renderPage(); }
      } catch (e) { stopPolling(); }
    }, 1000);
  }

  function matches(r) {
    const fl = state.filter;
    if (fl === "all") return true;
    if (fl === "review") return r.needs_review;
    if (fl === "ai") return !!r.ai;
    return r.status === fl.slice(7);
  }
  function drawChips() {
    clear(chips);
    const counts = {};
    f.items.forEach((r) => { counts[r.status] = (counts[r.status] || 0) + 1; });
    const chip = (key, label, n) => h("button", { class: "chip" + (state.filter === key ? " active" : ""), onclick: () => { state.filter = key; drawChips(); drawList(); } }, label + " " + n);
    chips.appendChild(chip("all", t("forum.chip.all"), f.items.length));
    chips.appendChild(chip("review", t("forum.chip.review"), f.items.filter((r) => r.needs_review).length));
    chips.appendChild(chip("ai", t("forum.chip.ai"), f.items.filter((r) => r.ai).length));
    STATUSES.filter((s) => counts[s]).forEach((s) => chips.appendChild(chip("status:" + s, statusLabel(s), counts[s])));
  }
  function drawList() {
    clear(list);
    const rows = f.items.filter(matches);
    if (!rows.length) { list.appendChild(h("div", { class: "muted pad" }, f.items.length ? t("forum.nomatch") : t("forum.none"))); return; }
    rows.forEach((r) => list.appendChild(threadCard(r)));
  }
  function threadCard(r) {
    const body = h("div", { class: "thread-body" + (state.open.has(r.id) ? "" : " hidden") });
    const card = h("div", { class: "card thread" });
    let loaded = false;
    async function load() {
      if (loaded) return;
      loaded = true;
      clear(body).appendChild(h("div", { class: "muted" }, t("common.loading")));
      try { clear(body).appendChild(detailPanel(await api("GET", "/api/forum/" + r.id), r)); } catch (e) { loaded = false; clear(body); fail(e); }
    }
    const head = h("button", { class: "thread-head", onclick: () => {
      if (state.open.has(r.id)) { state.open.delete(r.id); body.classList.add("hidden"); } else { state.open.add(r.id); body.classList.remove("hidden"); load(); }
    } },
      h("span", { class: "flag" + (r.needs_review ? " on" : ""), title: r.needs_review ? t("forum.needsReview") : "" }),
      h("span", { class: "thread-title" }, r.title),
      h("span", { class: "thread-meta" },
        h("span", { class: "badge" }, statusLabel(r.status)),
        r.cluster || (r.ai && r.ai.cluster) ? h("span", { class: "badge proto" }, r.cluster || r.ai.cluster) : null,
        r.ai ? sevBadge(r.ai.severity) : null,
        r.ai ? h("span", { class: "badge idle", title: t("forum.hasAi") }, "AI") : null,
        h("span", { class: "muted small-text" }, fmtTime(r.last_activity))));
    card.appendChild(head);
    if (r.ai && r.ai.summary) card.appendChild(h("div", { class: "thread-sum sub small-text" }, r.ai.summary));
    card.appendChild(body);
    if (state.open.has(r.id)) load();
    return card;
  }

  function detailPanel(d, row) {
    const ai = d.ai || {};
    const st = (d.triage && d.triage.status) || "new";
    // reporter
    const replies = d.replies.map((x) => h("div", { class: "reply" + (x.bot ? " bot" : "") },
      h("div", { class: "who small-text" }, x.author + (x.bot ? " · " + t("forum.bot") : "") + " · " + fmtTime(x.time)),
      h("div", { class: "txt" }, x.content || (x.attachments.length ? x.attachments.join(", ") : ""))));
    const reporter = h("div", { class: "pane" }, h("h3", null, t("forum.reporter")),
      h("div", { class: "who small-text" }, t("forum.by", { a: d.author, t: fmtTime(d.created) })),
      h("div", { class: "txt" }, d.body || "—"),
      d.attachments.length ? h("div", { class: "small-text muted" }, d.attachments.join(", ")) : null,
      d.reply_count ? h("h3", null, t("forum.replies", { n: d.reply_count })) : null,
      d.reply_count > d.replies.length ? h("div", { class: "small-text muted" }, t("forum.moreReplies", { n: d.replies.length })) : null,
      replies,
      d.link ? h("a", { class: "small-text", href: d.link, target: "_blank", rel: "noopener noreferrer" }, t("forum.open")) : null);
    // AI block
    const aiPane = h("div", { class: "pane" }, h("h3", null, t("forum.ai")));
    if (!d.ai) aiPane.appendChild(h("div", { class: "muted" }, t("forum.noAi")));
    else {
      if (ai.summary) aiPane.appendChild(h("div", { class: "ai-sum" }, ai.summary));
      aiPane.appendChild(h("div", { class: "facts" },
        h("span", null, t("forum.cluster") + ": ", h("b", null, ai.cluster || "—")),
        h("span", null, t("forum.severity") + ": ", ai.severity ? sevBadge(ai.severity) : "—"),
        h("span", null, t("forum.status") + ": ", h("b", null, ai.suggest_status ? statusLabel(ai.suggest_status) : "—")),
        h("span", null, t("forum.confidence") + ": ", h("b", null, Math.round((ai.confidence || 0) * 100) + "%"))));
      if (ai.duplicate_of) aiPane.appendChild(h("div", { class: "small-text" }, t("forum.dup", { t: ai.duplicate_of.slice(-6) })));
      if (ai.likely_cause) aiPane.appendChild(h("div", null, h("div", { class: "lbl" }, t("forum.cause")), h("div", { class: "txt" }, ai.likely_cause)));
      if ((ai.where_to_look || []).length) aiPane.appendChild(h("div", null, h("div", { class: "lbl" }, t("forum.where")), h("div", { class: "codes" }, ai.where_to_look.map((w) => h("code", null, w)))));
      if (ai.need_info) aiPane.appendChild(h("div", null, h("div", { class: "lbl" }, t("forum.needInfo")), h("div", { class: "txt" }, ai.need_info)));
      if (ai.images_dropped) aiPane.appendChild(h("div", { class: "small-text muted" }, t("forum.imagesDropped")));
      aiPane.appendChild(h("div", { class: "small-text muted" }, [ai.model, fmtTime(ai.at)].filter(Boolean).join(" · ")));
    }
    // human side: reply draft, tags, triage
    const draft = h("textarea", { class: "draft", value: ai.reply_draft || "" });
    const tagsAdd = h("input", { type: "text", list: "tags-" + d.id, value: (ai.tags_add || []).join(", ") });
    const tagsRem = h("input", { type: "text", list: "tags-" + d.id, value: (ai.tags_remove || []).join(", ") });
    const dl = h("datalist", { id: "tags-" + d.id }, d.forum_tags.map((x) => h("option", { value: x })));
    const names = (el) => el.value.split(",").map((x) => x.trim()).filter(Boolean);
    const stSel = h("select", { value: ai.suggest_status || st }, STATUSES.map((s) => h("option", { value: s }, statusLabel(s))));
    const clInp = h("input", { type: "text", value: (d.triage && d.triage.cluster) || ai.cluster || "" });
    const caInp = h("input", { type: "text", value: (d.triage && d.triage.root_cause) || ai.likely_cause || "" });
    const chk = (checked) => h("input", { type: "checkbox", checked });
    const cSt = chk(!!ai.suggest_status), cCl = chk(!!ai.cluster), cCa = chk(!!ai.likely_cause);
    const adoptRow = (box, label, input) => h("label", { class: "adopt-row" }, box, h("span", { class: "adopt-label" }, label), h("span", { class: "grow" }, input));
    const adoptBtn = h("button", { class: "btn", onclick: async () => {
      const fields = {};
      if (cSt.checked) fields.status = stSel.value;
      if (cCl.checked) fields.cluster = clInp.value;
      if (cCa.checked) fields.root_cause = caInp.value;
      adoptBtn.disabled = true;
      try { await api("POST", "/api/forum/" + d.id + "/adopt", { fields }); toast(t("forum.adopted")); renderPage(); }
      catch (e) { fail(e); adoptBtn.disabled = false; }
    } }, t("forum.adopt"));
    const proposeBtn = h("button", { class: "btn primary", onclick: async () => {
      if (!draft.value.trim() && !names(tagsAdd).length && !names(tagsRem).length) { toast(t("forum.proposeNone"), true); return; }
      proposeBtn.disabled = true;
      try {
        const r = await api("POST", "/api/forum/" + d.id + "/propose", { text: draft.value, add: names(tagsAdd), remove: names(tagsRem) });
        toast(t("forum.proposed") + (r.error ? " (" + r.error + ")" : ""), !!r.error);
      } catch (e) { fail(e); } finally { proposeBtn.disabled = false; }
    } }, t("forum.propose"));
    const human = h("div", { class: "pane" },
      h("div", { class: "lbl" }, t("forum.draft")), draft,
      h("div", { class: "two" }, field(t("forum.tagsAdd"), tagsAdd), field(t("forum.tagsRemove"), tagsRem)), dl,
      h("div", { class: "sub small-text" }, t("forum.tagsHint")),
      h("div", { class: "row end" }, goBtn("approvals", t("forum.goApprovals"), false), ...polishButtons(draft, () => ({ channel: d.id })), proposeBtn),
      h("h3", null, t("forum.adoptHead")),
      h("div", { class: "sub small-text" }, t("forum.current") + ": " + statusLabel(st) + (d.triage && d.triage.cluster ? " · " + d.triage.cluster : "") + (d.triage && d.triage.reviewed ? " · " + t("forum.reviewedAt", { t: fmtTime(d.triage.reviewed) }) : "")),
      adoptRow(cSt, t("forum.status"), stSel), adoptRow(cCl, t("forum.cluster"), clInp), adoptRow(cCa, t("forum.cause"), caInp),
      h("div", { class: "row end" }, adoptBtn));
    return h("div", { class: "detail" }, reporter, h("div", { class: "detail-right" }, aiPane, human));
  }

  drawChips(); drawList();
  drawProgress(st0);
  if (st0.running) { SEEN_JOB = st0.job; startPolling(); }
  return h("div", null, toolbar, progress, chips, list);
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
      opts: () => forumRows.map((r) => h("option", { value: r.id }, r.title + " · " + statusLabel(r.status))) },
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
async function pageLLM() {
  let tab = "profiles";
  const data = await api("GET", "/api/llm");
  const presets = (await api("GET", "/api/llm/presets")).items;
  const box = h("div", null);
  const tabs = h("div", { class: "tabs" });
  function drawTabs() {
    clear(tabs);
    [["profiles", t("llm.profiles")], ["retry", t("llm.retry")]].forEach(([k, label]) => tabs.appendChild(h("button", { class: "tab" + (tab === k ? " active" : ""), onclick: () => { tab = k; draw(); } }, label)));
  }
  function draw() { drawTabs(); clear(box).appendChild(tab === "profiles" ? profilesView() : retryView()); }
  function statusBadge(p) {
    if (p.status === "ok") return h("span", { class: "badge" }, t("llm.ok"));
    if (p.status === "error") return h("span", { class: "badge err", title: p.last_error }, t("llm.err"));
    return h("span", { class: "badge idle" }, t("llm.untested"));
  }
  function profilesView() {
    const fb = h("label", { class: "row" }, h("input", { type: "checkbox", checked: data.settings.fallback, onchange: async (e) => { try { data.settings = await api("PUT", "/api/llm/settings", { fallback: e.target.checked }); toast(t("common.saved")); } catch (x) { fail(x); } } }), t("llm.fallback"));
    const top = h("div", { class: "row end" }, fb, h("button", { class: "btn primary", onclick: newProfile }, t("llm.new")));
    if (!data.profiles.length) return h("div", null, top, empty(t("llm.empty.title"), t("llm.empty.text"), h("button", { class: "btn primary", onclick: newProfile }, t("llm.new"))));
    return h("div", null, top, h("div", { class: "grid" }, data.profiles.map((p) => {
      const active = data.settings.active === p.id;
      return h("div", { class: "card" },
        h("div", { class: "card-head" }, h("span", { class: "star" + (active ? " on" : "") }, active ? "★" : "☆"), h("span", { class: "name" }, p.name), h("span", { class: "badge proto" }, p.protocol)),
        h("div", { class: "kv" }, p.model), h("div", { class: "kv" }, p.base_url || "—"),
        h("div", { class: "kv" }, p.key_set ? "… " + p.key_hint : t("llm.nokey"), " · ", p.calls_s + "/s · " + p.calls_min + "/min"),
        h("div", { class: "card-actions" }, statusBadge(p),
          h("button", { class: "btn small", disabled: active, onclick: async () => { try { await api("POST", "/api/llm/" + p.id + "/activate", {}); renderPage(); } catch (e) { fail(e); } } }, active ? t("llm.active") : t("llm.setActive")),
          h("button", { class: "btn small", onclick: () => editProfile(p) }, t("common.edit")),
          h("button", { class: "btn small", onclick: async (ev) => { ev.target.disabled = true; toast(t("llm.testing")); try { const r = await api("POST", "/api/llm/" + p.id + "/test", {}); toast(r.ok ? t("llm.testOk", { t: r.text }) : (r.code === "empty_text" ? t("llm.testEmpty") : r.error), !r.ok); renderPage(); } catch (e) { fail(e); ev.target.disabled = false; } } }, t("llm.test")),
          h("span", { class: "spacer" }),
          h("button", { class: "icon-btn", title: t("common.delete"), onclick: async () => { if (await confirmBox(t("llm.delTitle"), t("llm.delText"), t("common.delete"), true)) { try { await api("DELETE", "/api/llm/" + p.id, {}); renderPage(); } catch (e) { fail(e); } } } }, trashIcon())));
    })));
  }
  function retryView() {
    const retries = h("input", { type: "number", min: "0", max: "5", value: data.settings.retries });
    const timeout = h("input", { type: "number", min: "5", max: "600", value: data.settings.timeout_s });
    return h("div", { class: "card" }, field(t("llm.retries"), retries), field(t("llm.timeout"), timeout), h("div", { class: "sub small-text" }, t("llm.fallbackNote")),
      h("div", { class: "row end" }, h("button", { class: "btn primary", onclick: async () => { try { data.settings = await api("PUT", "/api/llm/settings", { retries: +retries.value, timeout_s: +timeout.value }); toast(t("common.saved")); } catch (e) { fail(e); } } }, t("common.save"))));
  }
  function newProfile() {
    const presetSel = h("select", null, presets.map((p) => h("option", { value: p.id }, p.name)));
    const name = h("input", { type: "text" }), base = h("input", { type: "url", placeholder: "https://" }), model = h("input", { type: "text", list: "model-suggest" });
    const dl = h("datalist", { id: "model-suggest" });
    const key = h("input", { type: "password", autocomplete: "off" }), maxTok = h("input", { type: "number", value: "2048", min: "1" });
    const maxConc = h("input", { type: "number", value: "2", min: "1", max: "16" });
    const apply = () => {
      const p = presets.find((x) => x.id === presetSel.value);
      name.value = p.name; base.value = p.base_url; model.value = p.model;
      clear(dl); p.models.forEach((m) => dl.appendChild(h("option", { value: m })));
    };
    presetSel.addEventListener("change", apply); apply();
    const body = h("div", null, field(t("llm.preset"), presetSel), field(t("common.name"), name), field(t("llm.baseUrl"), base), fieldHint(t("llm.model"), model, t("llm.modelHint")), dl,
      field(t("llm.key"), key), h("div", { class: "sub small-text" }, t("llm.keyNote")), field(t("llm.maxTokens"), maxTok),
      fieldHint(t("llm.maxConcurrent"), maxConc, t("llm.maxConcurrentHint")),
      data.keyring ? null : h("div", { class: "badge err" }, t("llm.nokeyring", { v: "HEARBACK_LLM_KEY_<ID>" })));
    modal(t("common.new"), body, [{ label: t("common.cancel") }, { label: t("common.save"), cls: "primary", run: async () => {
      const p = presets.find((x) => x.id === presetSel.value);
      try {
        const r = await api("POST", "/api/llm", { preset: presetSel.value, name: name.value, protocol: p.protocol, base_url: base.value, model: model.value, key: key.value, max_tokens: +maxTok.value, max_concurrent: +maxConc.value });
        if (r.key_error) toast(r.key_error, true);
        renderPage();
      } catch (e) { fail(e); return false; }
    } }]);
  }
  function editProfile(p) {
    const name = h("input", { type: "text", value: p.name }), base = h("input", { type: "url", value: p.base_url || "", placeholder: "https://" });
    const model = h("input", { type: "text", value: p.model }), maxTok = h("input", { type: "number", value: String(p.max_tokens || 2048), min: "1" });
    const maxConc = h("input", { type: "number", value: String(p.max_concurrent || 2), min: "1", max: "16" });
    const key = h("input", { type: "password", autocomplete: "off", placeholder: p.key_set ? t("llm.keyKeep") : "" });
    const body = h("div", null, field(t("common.name"), name), field(t("llm.baseUrl"), base), fieldHint(t("llm.model"), model, t("llm.modelHint")),
      field(t("llm.key"), key), h("div", { class: "sub small-text" }, t("llm.keyNote")), field(t("llm.maxTokens"), maxTok),
      fieldHint(t("llm.maxConcurrent"), maxConc, t("llm.maxConcurrentHint")),
      data.keyring ? null : h("div", { class: "badge err" }, t("llm.nokeyring", { v: "HEARBACK_LLM_KEY_<ID>" })));
    modal(t("common.edit") + " · " + p.name, body, [{ label: t("common.cancel") }, { label: t("common.save"), cls: "primary", run: async () => {
      try {
        await api("PUT", "/api/llm/" + p.id, { name: name.value, base_url: base.value, model: model.value, max_tokens: +maxTok.value, max_concurrent: +maxConc.value });
        if (key.value.trim()) await api("POST", "/api/llm/" + p.id + "/key", { key: key.value.trim() });
        renderPage();
      } catch (e) { fail(e); return false; }
    } }]);
  }
  draw();
  return h("div", null, tabs, box);
}

/* ---------- agents ("人设") ---------- */
async function pageAgents() {
  const [res, llm] = await Promise.all([api("GET", "/api/agents"), api("GET", "/api/llm")]);
  function edit(a) {
    const name = h("input", { type: "text", value: a ? a.name : "" }), desc = h("input", { type: "text", value: a ? a.description : "" });
    const prompt = h("textarea", { value: a ? a.system_prompt : "" });
    prompt.rows = 8;
    const prof = h("select", { value: a && a.profile_id ? a.profile_id : "" }, h("option", { value: "" }, t("agent.default")), llm.profiles.map((p) => h("option", { value: p.id }, p.name)));
    modal(a ? t("common.edit") : t("agent.new"), h("div", null, field(t("common.name"), name), field(t("agent.desc"), desc), field(t("agent.prompt"), prompt), field(t("agent.llm"), prof)),
      [{ label: t("common.cancel") }, { label: t("common.save"), cls: "primary", run: async () => {
        const body = { name: name.value, description: desc.value, system_prompt: prompt.value, profile_id: prof.value || null };
        try { await api(a ? "PUT" : "POST", "/api/agents" + (a ? "/" + a.id : ""), body); renderPage(); } catch (e) { fail(e); return false; }
      } }]);
  }
  return h("div", null, h("div", { class: "row end" }, h("button", { class: "btn primary", onclick: () => edit(null) }, t("agent.new"))),
    h("div", { class: "grid" }, res.items.map((a) => h("div", { class: "card" },
      h("div", { class: "card-head" }, h("span", { class: "name" }, a.name), h("span", { class: "badge proto" }, a.profile_id || t("agent.default"))),
      h("div", { class: "kv" }, a.description), h("div", { class: "preview small-text" }, a.system_prompt.slice(0, 160) + (a.system_prompt.length > 160 ? "…" : "")),
      h("div", { class: "card-actions" }, h("button", { class: "btn small", onclick: () => edit(a) }, t("common.edit")), h("span", { class: "spacer" }),
        a.id === "auto" ? h("span", { class: "muted small-text" }, t("agent.locked")) : h("button", { class: "icon-btn", title: t("common.delete"), onclick: async () => { if (await confirmBox(t("agent.delTitle"), a.name, t("common.delete"), true)) { try { await api("DELETE", "/api/agents/" + a.id, {}); renderPage(); } catch (e) { fail(e); } } } }, trashIcon()))))));
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

/* ---------- settings ---------- */
async function pageSettings() {
  const [s, rr] = await Promise.all([api("GET", "/api/settings"), api("GET", "/api/rules")]);
  const rules = rr.rules;
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
  return h("div", null,
    h("div", { class: "card" }, h("h2", null, t("set.token")), h("div", { class: "sub small-text" }, t("set.tokenWhat")),
      h("div", { class: "row" }, h("span", { class: "badge" + (s.token.set ? "" : " err") }, s.token.set ? t("set.tokenSet") : t("set.tokenNot")), s.token.source === "env" ? h("span", { class: "sub small-text" }, t("set.tokenEnv")) : null),
      h("div", { class: "row" }, h("div", { class: "grow" }, tok), saveTok)),
    h("div", { class: "card" }, h("h2", null, t("set.forum")), h("div", { class: "sub small-text" }, t("set.forumWhat")),
      h("div", { class: "row" }, h("span", { class: "badge" + (s.forum_id ? "" : " err") }, s.forum_id ? t("set.forumSet") : t("set.forumNot"))),
      h("div", { class: "two" }, field(t("set.guildId"), guildInp), field(t("set.forumId"), forumInp)),
      h("div", { class: "row end" }, saveForum)),
    h("div", { class: "card" }, h("h2", null, t("set.dm")), dm, h("div", { class: "sub small-text" }, t("set.cap", { n: s.hourly_cap }))),
    h("div", { class: "card" }, h("h2", null, t("set.portal")),
      h("ol", { class: "steps" }, h("li", null, t("set.step1")), h("li", null, t("set.step2")), h("li", null, t("set.step3"))),
      h("a", { href: s.portal_url, target: "_blank", rel: "noopener noreferrer" }, t("set.openPortal")),
      h("h2", null, t("set.invite")),
      s.invite_url ? h("div", null, h("code", null, s.invite_url), h("div", null, h("a", { href: s.invite_url, target: "_blank", rel: "noopener noreferrer" }, t("set.invite")))) : h("div", { class: "muted" }, t("set.inviteHint"))),
    h("div", { class: "card" }, h("div", { class: "kv" }, t("set.port") + ": " + s.port)));
}

const PAGES = { dashboard: pageDashboard, conversations: pageConversations, approvals: pageApprovals, forum: pageForum, botmsgs: pageBotMsgs, supporters: pageSupporters, llm: pageLLM, agents: pageAgents, rules: pageRules, logs: pageLogs, settings: pageSettings };

/* ---------- boot ---------- */
function boot() {
  const forced = /[#&]lang=(zh|en)/.exec(location.hash);          // the demo link carries its language
  if (forced) store("setL", "swlink.lang", forced[1]);
  const saved = forced ? forced[1] : store("get", "swlink.lang");
  LANG = saved === "zh" || saved === "en" ? saved : ((navigator.language || "en").toLowerCase().startsWith("zh") ? "zh" : "en");
  document.documentElement.lang = LANG === "zh" ? "zh-CN" : "en";
  const m = /[#&]k=([^&]+)/.exec(location.hash);
  if (m) {
    KEY = decodeURIComponent(m[1]);
    store("setS", "swlink.key", KEY);
    history.replaceState(null, "", location.pathname + location.search);
  } else KEY = store("get", "swlink.key") || "";
  if (!KEY) { showLocked(); return; }
  $("app").classList.remove("hidden");
  window.addEventListener("hashchange", renderPage);
  refreshStatus().catch(() => {});
  renderPage();
  setInterval(() => { if (!document.hidden) refreshStatus().catch(() => {}); }, 5000);
}
boot();
