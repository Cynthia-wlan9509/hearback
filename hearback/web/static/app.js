"use strict";
/* SWLink console: vanilla JS, no build step. All server and user text goes through textContent (see h()).
   The texts live in i18n.js (loaded first). */

const NAV = [
  ["nav.features", ["dashboard", "conversations", "approvals", "forum", "botmsgs"]],
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
  for (const [gk, pages] of NAV) {
    side.appendChild(h("div", { class: "nav-group" }, t(gk)));
    for (const p of pages) side.appendChild(h("button", { class: "nav-item" + (p === PAGE ? " active" : ""), onclick: () => go(p) }, t("nav." + p)));
  }
  side.appendChild(h("div", { class: "side-foot" }, "v" + ((STATUS && STATUS.version) || "")));
  renderTop();
  renderHowto();
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
  renderTop();
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
      h("div", { class: "row end" }, h("span", { class: "sub small-text" }, t("conv.agent")), agentSel, draftBtn, sendBtn),
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
            h("div", { class: "bubble" }, h("div", { class: "who" }, m.author_name + " · " + fmtTime(m.time)), h("div", { class: "txt" }, text))));
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
  const [pend, appr] = await Promise.all([api("GET", "/api/queue?state=pending"), api("GET", "/api/queue?state=approved")]);
  const wrap = h("div", null);
  const card = (p, approved) => h("div", { class: "card" },
    h("div", { class: "row" }, h("span", { class: "badge" }, p.kind), h("strong", { class: "grow" }, p.target || p.thread || p.title),
      h("span", { class: "muted small-text" }, fmtTime(p.created))),
    p.kind === "tags" ? h("div", { class: "preview" }, [p.add.length ? "+ " + p.add.join(", ") : "", p.remove.length ? "− " + p.remove.join(", ") : ""].filter(Boolean).join("   ")) : h("div", { class: "preview" }, p.text),
    p.why ? h("div", { class: "sub small-text" }, t("appr.why") + ": " + p.why) : null,
    h("div", { class: "sub small-text" }, t("appr.by") + ": " + p.actor),
    approved ? null : h("div", { class: "card-actions" },
      h("button", { class: "btn primary small", onclick: () => decide(p, "approve") }, t("appr.approve")),
      h("button", { class: "btn danger small", onclick: () => decide(p, "reject") }, t("appr.reject"))));
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
  if (!pend.items.length && !appr.items.length) return empty(t("appr.empty.title"), t("appr.empty.text"), goBtn("conversations", t("appr.goChat")));
  wrap.appendChild(h("div", { class: "row end" }, h("button", { class: "btn primary", disabled: !appr.items.length, onclick: sendAll }, t("appr.send") + " (" + appr.items.length + ")")));
  wrap.appendChild(h("h2", null, t("appr.pending") + " (" + pend.items.length + ")"));
  pend.items.forEach((p) => wrap.appendChild(card(p, false)));
  if (appr.items.length) { wrap.appendChild(h("h2", null, t("appr.approved"))); appr.items.forEach((p) => wrap.appendChild(card(p, true))); }
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
      h("div", { class: "row end" }, goBtn("approvals", t("forum.goApprovals"), false), proposeBtn),
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
  const forum = await api("GET", "/api/forum").catch(() => ({ configured: false, items: [] }));
  const guilds = await api("GET", "/api/guilds").catch(() => ({ items: [], reason: "" }));
  if (guilds.reason === "no_token" && !forum.items.length) return empty(t("bm.empty.title"), t("bm.noToken"), goBtn("settings", t("nav.settings")));
  const out = h("div", { class: "bm-list" }, h("div", { class: "muted pad" }, t("bm.first")));
  const place = h("select", { onchange: () => { if (place.value) { link.value = ""; load(place.value); } } },
    h("option", { value: "" }, t("bm.pick")),
    forum.configured && forum.items.length ? h("optgroup", { label: t("bm.threads") }, forum.items.map((r) => h("option", { value: r.id }, r.title))) : null,
    guilds.items.map((g) => h("optgroup", { label: g.name + " · " + t("bm.channels") }, g.channels.map((c) => h("option", { value: c.id }, "# " + c.name)))));
  const link = h("input", { type: "text", placeholder: t("bm.paste") });
  link.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); if (link.value.trim()) { place.value = ""; load(link.value.trim()); } } });
  const loadBtn = h("button", { class: "btn primary", onclick: () => { const v = link.value.trim() || place.value; if (v) load(v); else toast(t("bm.first"), true); } }, t("bm.load"));
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
  function editMsg(r, m) {
    const ta = h("textarea", { value: m.content });
    ta.rows = 8;
    const counter = h("div", { class: "counter small-text" });
    const upd = () => { counter.textContent = t("bm.counter", { n: ta.value.length }); counter.classList.toggle("over", ta.value.length > 2000); };
    ta.addEventListener("input", upd); upd();
    modal(t("bm.editTitle"), h("div", null, ta, counter, h("div", { class: "sub small-text" }, t("bm.sameSyncNote"))), [
      { label: t("common.cancel") },
      { label: t("common.save"), cls: "primary", run: async () => {
        try { await api("PUT", "/api/botmsgs/" + r.channel + "/" + m.id, { text: ta.value }); toast(t("common.saved")); load(r.channel); }
        catch (e) { fail(e); return false; } } }]);
  }
  async function delMsg(r, m) {
    if (!(await confirmBox(t("bm.delTitle"), t("bm.delText") + "\n\n" + (m.content || "").slice(0, 200), t("common.delete"), true))) return;
    try { await api("DELETE", "/api/botmsgs/" + r.channel + "/" + m.id, {}); toast(t("bm.deleted")); load(r.channel); } catch (e) { fail(e); }
  }
  return h("div", null, h("div", { class: "card" }, h("div", { class: "row" }, h("div", { class: "grow" }, field(t("bm.place"), place)), h("div", { class: "grow" }, field(t("bm.paste"), link)), loadBtn)), out);
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

const PAGES = { dashboard: pageDashboard, conversations: pageConversations, approvals: pageApprovals, forum: pageForum, botmsgs: pageBotMsgs, llm: pageLLM, agents: pageAgents, rules: pageRules, logs: pageLogs, settings: pageSettings };

/* ---------- boot ---------- */
function boot() {
  const saved = store("get", "swlink.lang");
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
  renderPage();
  setInterval(() => { if (!document.hidden) refreshStatus().catch(() => {}); }, 5000);
}
boot();
