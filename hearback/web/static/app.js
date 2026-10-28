"use strict";
/* SWLink console: vanilla JS, no build step. All server and user text goes through textContent (see h()). */

const I18N = {
  zh: {
    "nav.features": "功能", "nav.system": "系统",
    "nav.dashboard": "仪表盘", "nav.conversations": "会话", "nav.approvals": "待审批", "nav.forum": "Bug 论坛",
    "nav.llm": "模型", "nav.agents": "智能体", "nav.rules": "频道规则", "nav.logs": "日志", "nav.settings": "设置",
    "title.dashboard": "仪表盘", "sub.dashboard": "机器人状态与上手清单",
    "title.conversations": "会话", "sub.conversations": "查看频道与私信，起草或发送回复",
    "title.approvals": "待审批", "sub.approvals": "机器人起草的回复，经你批准后才会发出",
    "title.forum": "Bug 论坛", "sub.forum": "来自 Hearback 台账的帖子",
    "title.llm": "模型", "sub.llm": "你自己的 API 密钥，官方或中转站均可",
    "title.agents": "智能体", "sub.agents": "不同场景用不同的系统提示词",
    "title.rules": "频道规则", "sub.rules": "决定机器人在哪里回答、怎么回答",
    "title.logs": "日志", "sub.logs": "最近的操作记录",
    "title.settings": "设置", "sub.settings": "Discord 令牌与邀请机器人",
    "bot.online": "在线", "bot.offline": "离线", "bot.connecting": "连接中", "bot.error": "出错",
    "bot.start": "启动", "bot.stop": "停止",
    "common.save": "保存", "common.cancel": "取消", "common.delete": "删除", "common.confirm": "确认", "common.edit": "编辑",
    "common.refresh": "刷新", "common.loading": "加载中…", "common.none": "无", "common.new": "新建", "common.saved": "已保存",
    "common.name": "名称", "common.close": "关闭", "common.error": "出错了", "common.yes": "是", "common.no": "否",
    "mode.off": "关闭", "mode.draft": "草稿待审", "mode.auto": "自动回复",
    "trigger.mention": "被 @ 时", "trigger.all": "所有消息",
    "dash.status": "机器人状态", "dash.servers": "服务器", "dash.pending": "待审批", "dash.auto": "今日自动回复",
    "dash.start": "上手清单", "dash.token": "添加 Discord 机器人令牌", "dash.llm": "添加一个模型并填入密钥",
    "dash.rule": "为某个频道开启草稿或自动回复", "dash.go": "前往",
    "dash.model": "当前模型", "dash.nomodel": "还没有模型",
    "conv.filter.all": "全部", "conv.total": "共 {n} 个", "conv.dms": "私信", "conv.pick": "选择左侧的一个会话",
    "conv.empty.title": "还没有会话", "conv.empty.text": "先添加令牌并启动机器人，频道会出现在这里。",
    "conv.placeholder": "输入消息，Enter 发送，Shift+Enter 换行", "conv.draft": "AI 起草", "conv.send": "发送",
    "conv.model": "模型：{m}", "conv.change": "更改", "conv.agent": "智能体", "conv.mode": "此会话模式",
    "conv.drafting": "起草中…", "conv.sent": "已发送", "conv.draftdone": "草稿已放进输入框，发送前请检查",
    "conv.autoTitle": "切换到自动回复？", "conv.autoText": "自动模式下，机器人会在没有人审核的情况下直接发出回复。可以随时改回。",
    "conv.attach": "[{n} 个附件]", "conv.nomsg": "这里还没有消息",
    "appr.empty.title": "没有待审批的内容", "appr.empty.text": "机器人起草的回复会出现在这里。",
    "appr.approve": "批准", "appr.reject": "拒绝", "appr.send": "发送已批准的", "appr.pending": "待审批", "appr.approved": "已批准，等待发送",
    "appr.why": "原因", "appr.by": "来自", "appr.to": "发到", "appr.sendTitle": "发送这些消息？", "appr.sendText": "以下已批准的内容会发到 Discord：",
    "appr.nothing": "没有已批准的内容可发", "appr.done": "已发送 {n} 条", "appr.failed": "{n} 条失败",
    "forum.sync": "同步", "forum.title": "标题", "forum.status": "状态", "forum.cluster": "归类", "forum.tags": "标签",
    "forum.last": "最近活动", "forum.empty.title": "没有配置 Bug 论坛", "forum.empty.text": "在 hearback.toml 的 [discord] 里填写 forum_id 后，这里会显示台账中的帖子。", "forum.synced": "同步完成",
    "forum.none": "台账里还没有帖子，点“同步”。",
    "llm.profiles": "模型配置", "llm.retry": "重试与回退", "llm.new": "+ 新建", "llm.fallback": "失败时回退",
    "llm.active": "使用中", "llm.setActive": "设为当前", "llm.test": "测试", "llm.ok": "正常", "llm.err": "出错", "llm.untested": "未测试",
    "llm.empty.title": "还没有模型", "llm.empty.text": "添加官方或中转站的密钥即可开始。",
    "llm.preset": "类型", "llm.baseUrl": "接口地址", "llm.model": "模型", "llm.key": "API 密钥", "llm.maxTokens": "最大输出 token",
    "llm.keyNote": "密钥只保存在系统钥匙串里，不会写进文件，也不会再显示。",
    "llm.nokeyring": "没有可用的系统钥匙串。请改用环境变量 {v}（需重启）。",
    "llm.retries": "重试次数", "llm.timeout": "超时（秒）", "llm.fallbackNote": "开启后，当前模型失败时按顺序尝试其他已启用的模型。",
    "llm.delTitle": "删除这个模型？", "llm.delText": "密钥也会一并从钥匙串删除。", "llm.testing": "测试中…",
    "llm.testOk": "连通：{t}", "llm.keyHint": "密钥", "llm.nokey": "未设置密钥", "llm.keyKeep": "已设置，留空则不改",
    "agent.new": "+ 新建智能体", "agent.desc": "说明", "agent.prompt": "系统提示词", "agent.llm": "使用的模型", "agent.default": "当前模型（默认）",
    "agent.delTitle": "删除这个智能体？", "agent.locked": "默认智能体不能删除",
    "rules.dm": "私信默认", "rules.guild": "服务器默认", "rules.agent": "智能体", "rules.trigger": "触发", "rules.cooldown": "冷却（秒）",
    "rules.empty.title": "没有可用的服务器", "rules.empty.text": "令牌有效且机器人已被邀请后，服务器会出现在这里。",
    "rules.saved": "规则已保存", "rules.mode": "模式", "rules.dirty": "有未保存的改动", "rules.channel": "频道",
    "logs.time": "时间", "logs.actor": "操作者", "logs.action": "动作", "logs.detail": "详情", "logs.empty": "还没有记录",
    "set.token": "Discord 令牌", "set.tokenSet": "已设置", "set.tokenNot": "未设置", "set.tokenReplace": "粘贴新的令牌",
    "set.tokenSave": "保存令牌", "set.tokenSaved": "令牌已保存（不会再显示）", "set.tokenEnv": "当前来自环境变量，环境变量优先于文件。",
    "set.dm": "私信默认模式", "set.port": "本地端口", "set.portal": "开发者门户步骤",
    "set.step1": "在开发者门户新建应用，进入 Bot 页面，重置并复制令牌。",
    "set.step2": "在同一页打开 Message Content Intent（消息内容权限），否则机器人读不到消息。",
    "set.step3": "用下面的邀请链接把机器人加进你的服务器。机器人只会在被邀请的服务器里工作。",
    "set.invite": "邀请链接", "set.inviteHint": "令牌有效后这里会生成邀请链接。", "set.openPortal": "打开开发者门户",
    "set.cap": "每小时自动回复上限：{n}（超过后改为草稿）",
    "locked.title": "请从启动器重新打开 SWLink", "locked.text": "这个页面的密钥已失效。关掉它，再运行 hearback web（或 swlink）打开新的链接。",
    "err.no_token": "还没有设置 Discord 令牌，请先到“设置”添加。",
  },
  en: {
    "nav.features": "Features", "nav.system": "System",
    "nav.dashboard": "Dashboard", "nav.conversations": "Conversations", "nav.approvals": "Approvals", "nav.forum": "Bug forum",
    "nav.llm": "LLM", "nav.agents": "Agents", "nav.rules": "Channel rules", "nav.logs": "Logs", "nav.settings": "Settings",
    "title.dashboard": "Dashboard", "sub.dashboard": "Bot status and a getting-started list",
    "title.conversations": "Conversations", "sub.conversations": "Read channels and DMs, draft or send replies",
    "title.approvals": "Approvals", "sub.approvals": "Replies the bot drafted; nothing goes out until you approve",
    "title.forum": "Bug forum", "sub.forum": "Threads from the Hearback ledger",
    "title.llm": "LLM", "sub.llm": "Your own API keys: official or any compatible relay",
    "title.agents": "Agents", "sub.agents": "A system prompt for each kind of conversation",
    "title.rules": "Channel rules", "sub.rules": "Where the bot answers, and how",
    "title.logs": "Logs", "sub.logs": "Recent activity",
    "title.settings": "Settings", "sub.settings": "Discord token and inviting the bot",
    "bot.online": "Online", "bot.offline": "Offline", "bot.connecting": "Connecting", "bot.error": "Error",
    "bot.start": "Start", "bot.stop": "Stop",
    "common.save": "Save", "common.cancel": "Cancel", "common.delete": "Delete", "common.confirm": "Confirm", "common.edit": "Edit",
    "common.refresh": "Refresh", "common.loading": "Loading…", "common.none": "None", "common.new": "New", "common.saved": "Saved",
    "common.name": "Name", "common.close": "Close", "common.error": "Something went wrong", "common.yes": "Yes", "common.no": "No",
    "mode.off": "Off", "mode.draft": "Draft for approval", "mode.auto": "Auto reply",
    "trigger.mention": "When mentioned", "trigger.all": "Every message",
    "dash.status": "Bot status", "dash.servers": "Servers", "dash.pending": "Pending approvals", "dash.auto": "Auto replies today",
    "dash.start": "Getting started", "dash.token": "Add the Discord bot token", "dash.llm": "Add a model and its API key",
    "dash.rule": "Turn on draft or auto reply for a channel", "dash.go": "Go",
    "dash.model": "Active model", "dash.nomodel": "No model yet",
    "conv.filter.all": "All", "conv.total": "{n} total", "conv.dms": "Direct messages", "conv.pick": "Pick a conversation on the left",
    "conv.empty.title": "No conversations yet", "conv.empty.text": "Add the token and start the bot; channels show up here.",
    "conv.placeholder": "Type a message, Enter to send, Shift+Enter newline", "conv.draft": "AI draft", "conv.send": "Send",
    "conv.model": "Model: {m}", "conv.change": "change", "conv.agent": "Agent", "conv.mode": "This conversation",
    "conv.drafting": "Drafting…", "conv.sent": "Sent", "conv.draftdone": "Draft placed in the box; read it before sending",
    "conv.autoTitle": "Switch to auto reply?", "conv.autoText": "In auto mode the bot posts replies without anyone reviewing them first. You can switch back any time.",
    "conv.attach": "[{n} attachment(s)]", "conv.nomsg": "No messages here yet",
    "appr.empty.title": "Nothing to approve", "appr.empty.text": "Replies the bot drafts will appear here.",
    "appr.approve": "Approve", "appr.reject": "Reject", "appr.send": "Send approved", "appr.pending": "Pending", "appr.approved": "Approved, waiting to send",
    "appr.why": "Why", "appr.by": "From", "appr.to": "To", "appr.sendTitle": "Send these messages?", "appr.sendText": "These approved messages will be posted to Discord:",
    "appr.nothing": "Nothing approved to send", "appr.done": "{n} sent", "appr.failed": "{n} failed",
    "forum.sync": "Sync", "forum.title": "Title", "forum.status": "Status", "forum.cluster": "Cluster", "forum.tags": "Tags",
    "forum.last": "Last activity", "forum.empty.title": "No bug forum configured", "forum.empty.text": "Add forum_id under [discord] in hearback.toml and the ledger threads show up here.", "forum.synced": "Synced",
    "forum.none": "The ledger has no threads yet. Press Sync.",
    "llm.profiles": "Model profiles", "llm.retry": "Retry and fallback", "llm.new": "+ New", "llm.fallback": "Fallback",
    "llm.active": "Active", "llm.setActive": "Set active", "llm.test": "Test", "llm.ok": "OK", "llm.err": "Error", "llm.untested": "Untested",
    "llm.empty.title": "No models yet", "llm.empty.text": "Add an official or relay key to begin.",
    "llm.preset": "Type", "llm.baseUrl": "Base URL", "llm.model": "Model", "llm.key": "API key", "llm.maxTokens": "Max output tokens",
    "llm.keyNote": "The key is stored in your OS keyring only. It is never written to a file or shown again.",
    "llm.nokeyring": "No OS keyring is available. Use the environment variable {v} instead (restart needed).",
    "llm.retries": "Retries", "llm.timeout": "Timeout (seconds)", "llm.fallbackNote": "When on, a failing model hands over to the other enabled models in order.",
    "llm.delTitle": "Delete this model?", "llm.delText": "Its key is removed from the keyring too.", "llm.testing": "Testing…",
    "llm.testOk": "Reachable: {t}", "llm.keyHint": "key", "llm.nokey": "no key set", "llm.keyKeep": "Set. Leave empty to keep it",
    "agent.new": "+ New agent", "agent.desc": "Description", "agent.prompt": "System prompt", "agent.llm": "Model", "agent.default": "Active model (default)",
    "agent.delTitle": "Delete this agent?", "agent.locked": "The default agent cannot be deleted",
    "rules.dm": "Direct messages default", "rules.guild": "Server default", "rules.agent": "Agent", "rules.trigger": "Trigger", "rules.cooldown": "Cooldown (s)",
    "rules.empty.title": "No servers available", "rules.empty.text": "Servers appear once the token works and the bot is invited.",
    "rules.saved": "Rules saved", "rules.mode": "Mode", "rules.dirty": "Unsaved changes", "rules.channel": "Channel",
    "logs.time": "Time", "logs.actor": "Actor", "logs.action": "Action", "logs.detail": "Detail", "logs.empty": "Nothing logged yet",
    "set.token": "Discord token", "set.tokenSet": "set", "set.tokenNot": "not set", "set.tokenReplace": "Paste a new token",
    "set.tokenSave": "Save token", "set.tokenSaved": "Token saved (it will not be shown again)", "set.tokenEnv": "Currently read from an environment variable, which wins over the file.",
    "set.dm": "Direct message default mode", "set.port": "Local port", "set.portal": "Developer Portal steps",
    "set.step1": "In the Developer Portal create an application, open the Bot page, reset and copy the token.",
    "set.step2": "On the same page turn on Message Content Intent, or the bot cannot read messages.",
    "set.step3": "Add the bot to your server with the invite link below. The bot only works in servers it was invited to.",
    "set.invite": "Invite link", "set.inviteHint": "The invite link appears once the token works.", "set.openPortal": "Open the Developer Portal",
    "set.cap": "Hourly cap on auto replies: {n} (drafts after that)",
    "locked.title": "Reopen SWLink from the launcher", "locked.text": "This page's key is no longer valid. Close it and run hearback web (or swlink) to get a fresh link.",
    "err.no_token": "No Discord token yet. Add it under Settings first.",
  },
};

const NAV = [
  ["nav.features", ["dashboard", "conversations", "approvals", "forum"]],
  ["nav.system", ["llm", "agents", "rules", "logs", "settings"]],
];
const MODES = ["off", "draft", "auto"];

let LANG = "en";
let KEY = "";
let PAGE = "dashboard";
let STATUS = null;
let RENDER_SEQ = 0;
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
function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

function toast(msg, bad) {
  const el = h("div", { class: "toast" + (bad ? " err" : "") }, msg);
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), bad ? 6000 : 3000);
}
function fail(e) {
  if (e && e.message === "locked") return;
  const m = (e && e.message) || "";
  toast(I18N[LANG]["err." + m] ? t("err." + m) : (m || t("common.error")), true);
}

function modal(title, body, actions) {
  const root = $("modal-root");
  const close = () => clear(root);
  const bar = h("div", { class: "modal-actions" }, (actions || []).map((a) =>
    h("button", { class: "btn " + (a.cls || ""), onclick: async () => { if (a.run) { if ((await a.run()) === false) return; } close(); } }, a.label)));
  const back = h("div", { class: "modal-back", onmousedown: (ev) => { if (ev.target === back) close(); } },
    h("div", { class: "modal", role: "dialog" }, h("h2", null, title), body, bar));
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
  if (r.status === 403) { showLocked(); throw new Error("locked"); }
  if (!r.ok) throw new Error((data && data.error) || "HTTP " + r.status);
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
function field(label, input) { return h("label", { class: "field" }, h("span", null, label), input); }

/* ---------- shell ---------- */
function renderShell() {
  const side = clear($("side"));
  side.appendChild(h("div", { class: "brand" }, h("img", { src: "/swlink.svg", alt: "" }), h("span", null, "SWLink")));
  for (const [gk, pages] of NAV) {
    side.appendChild(h("div", { class: "nav-group" }, t(gk)));
    for (const p of pages) side.appendChild(h("button", { class: "nav-item" + (p === PAGE ? " active" : ""), onclick: () => go(p) }, t("nav." + p)));
  }
  side.appendChild(h("div", { class: "side-foot" }, "v" + ((STATUS && STATUS.version) || "")));
  renderTop();
}
function renderTop() {
  const top = clear($("top"));
  const b = (STATUS && STATUS.bot) || { state: "offline", reason: "" };
  const running = b.state === "online" || b.state === "connecting";
  top.appendChild(h("div", null, h("h1", null, t("title." + PAGE)), h("div", { class: "sub" }, t("sub." + PAGE))));
  top.appendChild(h("div", { class: "top-right" },
    h("span", { class: "pill " + b.state, title: b.reason || "" }, h("span", { class: "dot" }), t("bot." + b.state), b.state === "error" && b.reason ? " · " + b.reason : ""),
    h("button", { class: "btn small " + (running ? "" : "primary"), onclick: toggleBot }, running ? t("bot.stop") : t("bot.start")),
    h("button", { class: "lang", onclick: toggleLang }, LANG === "zh" ? "EN" : "中")));
}
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
  const item = (ok, text, page) => h("li", null, h("span", { class: "tick" + (ok ? " on" : "") }, ok ? "✓" : ""), h("span", { class: "grow" }, text),
    ok ? null : h("button", { class: "btn small", onclick: () => go(page) }, t("dash.go")));
  return h("div", null,
    h("div", { class: "metrics" },
      metric(t("dash.status"), t("bot." + s.bot.state), s.bot.reason || (s.llm ? t("dash.model") + ": " + s.llm.name + " · " + s.llm.model : t("dash.nomodel"))),
      metric(t("dash.servers"), String(s.guilds)), metric(t("dash.pending"), String(s.pending)), metric(t("dash.auto"), String(s.auto_today))),
    h("div", { class: "card" }, h("h2", null, t("dash.start")),
      h("ul", { class: "checklist" }, item(s.checklist.token, t("dash.token"), "settings"), item(s.checklist.llm, t("dash.llm"), "llm"), item(s.checklist.rule, t("dash.rule"), "rules"))));
}

/* ---------- conversations ---------- */
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
  if (!items.length) return empty(t("conv.empty.title"), convs.reason === "no_token" ? t("err.no_token") : t("conv.empty.text"), h("button", { class: "btn primary", onclick: () => go("settings") }, t("nav.settings")));

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
        listBox.appendChild(h("button", { class: "conv-item" + (sel && sel.id === c.id ? " active" : ""), onclick: () => open(c) },
          h("div", { class: "nm" }, h("span", null, (c.kind === "dm" ? "" : "# ") + c.channel_name), m === "off" ? null : h("span", { class: "mode-tag " + m }, modeLabel(m))),
          c.preview ? h("div", { class: "pv" }, c.preview) : null));
      }
    }
    count.textContent = t("conv.total", { n: vis.length });
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
        body.scrollTop = body.scrollHeight;
      } catch (e) { clear(body); fail(e); }
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
    h("div", { class: "preview" }, p.text),
    p.why ? h("div", { class: "sub small-text" }, t("appr.why") + ": " + p.why) : null,
    h("div", { class: "sub small-text" }, t("appr.by") + ": " + p.actor),
    approved ? null : h("div", { class: "card-actions" },
      h("button", { class: "btn primary small", onclick: () => decide(p, "approve") }, t("appr.approve")),
      h("button", { class: "btn danger small", onclick: () => decide(p, "reject") }, t("appr.reject"))));
  async function decide(p, what) { try { await api("POST", "/api/queue/" + p.id + "/" + what, {}); renderPage(); } catch (e) { fail(e); } }
  async function sendAll() {
    if (!appr.items.length) { toast(t("appr.nothing")); return; }
    const list = h("div", null, h("div", { class: "sub" }, t("appr.sendText")),
      appr.items.map((p) => h("div", null, h("div", { class: "small-text muted" }, t("appr.to") + ": " + (p.target || p.thread)), h("div", { class: "preview" }, p.text))));
    const ok = await new Promise((resolve) => modal(t("appr.sendTitle"), list, [
      { label: t("common.cancel"), run: () => { resolve(false); } }, { label: t("appr.send"), cls: "primary", run: () => { resolve(true); } }]));
    if (!ok) return;
    try {
      const r = await api("POST", "/api/queue/send", {});
      toast(t("appr.done", { n: r.sent.length }) + (r.failed.length ? " · " + t("appr.failed", { n: r.failed.length }) : ""), r.failed.length > 0);
      renderPage();
    } catch (e) { fail(e); }
  }
  if (!pend.items.length && !appr.items.length) return empty(t("appr.empty.title"), t("appr.empty.text"));
  wrap.appendChild(h("div", { class: "row end" }, h("button", { class: "btn primary", disabled: !appr.items.length, onclick: sendAll }, t("appr.send") + " (" + appr.items.length + ")")));
  wrap.appendChild(h("h2", null, t("appr.pending") + " (" + pend.items.length + ")"));
  pend.items.forEach((p) => wrap.appendChild(card(p, false)));
  if (appr.items.length) { wrap.appendChild(h("h2", null, t("appr.approved"))); appr.items.forEach((p) => wrap.appendChild(card(p, true))); }
  return wrap;
}

/* ---------- forum ---------- */
async function pageForum() {
  const f = await api("GET", "/api/forum");
  if (!f.configured) return empty(t("forum.empty.title"), t("forum.empty.text"));
  const sync = h("button", { class: "btn primary", onclick: async () => { sync.disabled = true; try { const r = await api("POST", "/api/forum/sync", {}); toast(t("forum.synced") + ": " + r.summary.split("\n")[0]); renderPage(); } catch (e) { fail(e); sync.disabled = false; } } }, t("forum.sync"));
  const head = h("div", { class: "row end" }, sync);
  if (!f.items.length) return h("div", null, head, empty(t("nav.forum"), t("forum.none")));
  return h("div", null, head, h("div", { class: "card" }, h("table", null,
    h("thead", null, h("tr", null, [t("forum.title"), t("forum.status"), t("forum.cluster"), t("forum.tags"), t("forum.last")].map((x) => h("th", null, x)))),
    h("tbody", null, f.items.map((r) => h("tr", null, h("td", null, r.title), h("td", null, h("span", { class: "badge" }, r.status)), h("td", null, r.cluster),
      h("td", null, (r.tags || []).map((x) => h("span", { class: "badge proto" }, x))), h("td", { class: "muted" }, fmtTime(r.last_activity))))))));
}

/* ---------- llm ---------- */
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
    if (!data.profiles.length) return h("div", null, top, empty(t("llm.empty.title"), t("llm.empty.text")));
    return h("div", null, top, h("div", { class: "grid" }, data.profiles.map((p) => {
      const active = data.settings.active === p.id;
      return h("div", { class: "card" },
        h("div", { class: "card-head" }, h("span", { class: "star" + (active ? " on" : "") }, active ? "★" : "☆"), h("span", { class: "name" }, p.name), h("span", { class: "badge proto" }, p.protocol)),
        h("div", { class: "kv" }, p.model), h("div", { class: "kv" }, p.base_url || "—"),
        h("div", { class: "kv" }, p.key_set ? "… " + p.key_hint : t("llm.nokey"), " · ", p.calls_s + "/s · " + p.calls_min + "/min"),
        h("div", { class: "card-actions" }, statusBadge(p),
          h("button", { class: "btn small", disabled: active, onclick: async () => { try { await api("POST", "/api/llm/" + p.id + "/activate", {}); renderPage(); } catch (e) { fail(e); } } }, active ? t("llm.active") : t("llm.setActive")),
          h("button", { class: "btn small", onclick: () => editProfile(p) }, t("common.edit")),
          h("button", { class: "btn small", onclick: async (ev) => { ev.target.disabled = true; toast(t("llm.testing")); try { const r = await api("POST", "/api/llm/" + p.id + "/test", {}); toast(r.ok ? t("llm.testOk", { t: r.text }) : r.error, !r.ok); renderPage(); } catch (e) { fail(e); ev.target.disabled = false; } } }, t("llm.test")),
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
    const key = h("input", { type: "password", autocomplete: "off" }), maxTok = h("input", { type: "number", value: "1024", min: "1" });
    const apply = () => {
      const p = presets.find((x) => x.id === presetSel.value);
      name.value = p.name; base.value = p.base_url; model.value = p.model;
      clear(dl); p.models.forEach((m) => dl.appendChild(h("option", { value: m })));
    };
    presetSel.addEventListener("change", apply); apply();
    const body = h("div", null, field(t("llm.preset"), presetSel), field(t("common.name"), name), field(t("llm.baseUrl"), base), field(t("llm.model"), model), dl,
      field(t("llm.key"), key), h("div", { class: "sub small-text" }, t("llm.keyNote")), field(t("llm.maxTokens"), maxTok),
      data.keyring ? null : h("div", { class: "badge err" }, t("llm.nokeyring", { v: "HEARBACK_LLM_KEY_<ID>" })));
    modal(t("common.new"), body, [{ label: t("common.cancel") }, { label: t("common.save"), cls: "primary", run: async () => {
      const p = presets.find((x) => x.id === presetSel.value);
      try {
        const r = await api("POST", "/api/llm", { preset: presetSel.value, name: name.value, protocol: p.protocol, base_url: base.value, model: model.value, key: key.value, max_tokens: +maxTok.value });
        if (r.key_error) toast(r.key_error, true);
        renderPage();
      } catch (e) { fail(e); return false; }
    } }]);
  }
  function editProfile(p) {
    const name = h("input", { type: "text", value: p.name }), base = h("input", { type: "url", value: p.base_url || "", placeholder: "https://" });
    const model = h("input", { type: "text", value: p.model }), maxTok = h("input", { type: "number", value: String(p.max_tokens || 1024), min: "1" });
    const key = h("input", { type: "password", autocomplete: "off", placeholder: p.key_set ? t("llm.keyKeep") : "" });
    const body = h("div", null, field(t("common.name"), name), field(t("llm.baseUrl"), base), field(t("llm.model"), model),
      field(t("llm.key"), key), h("div", { class: "sub small-text" }, t("llm.keyNote")), field(t("llm.maxTokens"), maxTok),
      data.keyring ? null : h("div", { class: "badge err" }, t("llm.nokeyring", { v: "HEARBACK_LLM_KEY_<ID>" })));
    modal(t("common.edit") + " · " + p.name, body, [{ label: t("common.cancel") }, { label: t("common.save"), cls: "primary", run: async () => {
      try {
        await api("PUT", "/api/llm/" + p.id, { name: name.value, base_url: base.value, model: model.value, max_tokens: +maxTok.value });
        if (key.value.trim()) await api("POST", "/api/llm/" + p.id + "/key", { key: key.value.trim() });
        renderPage();
      } catch (e) { fail(e); return false; }
    } }]);
  }
  draw();
  return h("div", null, tabs, box);
}

/* ---------- agents ---------- */
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

/* ---------- rules ---------- */
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
  const defaults = h("div", { class: "card" }, head, defaultRow(t("rules.dm"), "dm_default"), defaultRow(t("rules.guild"), "guild_default"));
  const accs = gr.items.map((g) => h("details", { class: "acc", open: true }, h("summary", null, g.name),
    h("div", { class: "acc-body" }, g.channels.map((c) => {
      const cur = () => rules.channels[c.id] || Object.assign({}, rules.guild_default);
      return h("div", { class: "rule-row" }, h("span", null, "# " + c.name), rowControls(cur(), (p) => { rules.channels[c.id] = Object.assign(cur(), p); }, true));
    }))));
  const save = h("button", { class: "btn primary", onclick: async () => { try { await api("PUT", "/api/rules", rules); toast(t("rules.saved")); renderPage(); } catch (e) { fail(e); } } }, t("common.save"));
  return h("div", null, h("div", { class: "row end" }, dirty, save), defaults,
    accs.length ? accs : empty(t("rules.empty.title"), gr.reason === "no_token" ? t("err.no_token") : t("rules.empty.text")));
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
  return h("div", null,
    h("div", { class: "card" }, h("h2", null, t("set.token")),
      h("div", { class: "row" }, h("span", { class: "badge" + (s.token.set ? "" : " err") }, s.token.set ? t("set.tokenSet") : t("set.tokenNot")), s.token.source === "env" ? h("span", { class: "sub small-text" }, t("set.tokenEnv")) : null),
      h("div", { class: "row" }, h("div", { class: "grow" }, tok), saveTok)),
    h("div", { class: "card" }, h("h2", null, t("set.dm")), dm, h("div", { class: "sub small-text" }, t("set.cap", { n: s.hourly_cap }))),
    h("div", { class: "card" }, h("h2", null, t("set.portal")),
      h("ol", { class: "steps" }, h("li", null, t("set.step1")), h("li", null, t("set.step2")), h("li", null, t("set.step3"))),
      h("a", { href: s.portal_url, target: "_blank", rel: "noopener noreferrer" }, t("set.openPortal")),
      h("h2", null, t("set.invite")),
      s.invite_url ? h("div", null, h("code", null, s.invite_url), h("div", null, h("a", { href: s.invite_url, target: "_blank", rel: "noopener noreferrer" }, t("set.invite")))) : h("div", { class: "muted" }, t("set.inviteHint"))),
    h("div", { class: "card" }, h("div", { class: "kv" }, t("set.port") + ": " + s.port)));
}

const PAGES = { dashboard: pageDashboard, conversations: pageConversations, approvals: pageApprovals, forum: pageForum, llm: pageLLM, agents: pageAgents, rules: pageRules, logs: pageLogs, settings: pageSettings };

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
