"""SWLink web console: a local-only HTTP server (stdlib) serving a static page and a small JSON API.

Safety: binds 127.0.0.1 only; every /api request needs the per-run secret in `X-SWLink-Key`; the Host header
must be this server's own address (DNS rebinding); writes need a JSON content type; static files come only from
the package `static/` directory. The bot token and API keys are write-only: no response, log or file written by
this module contains them. Writes made from the page use the actor `human:web`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hmac
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Sequence

from hearback import __version__, actions, editor, review
from hearback.agents import AgentStore
from hearback.chatbot import (BotRuntime, Chatbot, Inbox, Message, RuleStore, avatar_url, build_messages,
                              build_polish, clean_polish, display_name, message_from_rest)
from hearback.config import Config, load_config, save_discord_ids
from hearback.discord import DiscordClient, DiscordError
from hearback.ledger import STATUSES, Ledger
from hearback.llm import (EMPTY_TEXT_ERROR, PRESETS, LLMError, LLMStore, complete_any, register_secret, scrub,
                          scrub_obj)
from hearback.queue import Queue
from hearback.supporters import (Person, SupporterBot, SupporterError, Supporters, SupporterStore, check_id,
                                 check_order_no, clean_settings, mask, match_remarks)
from hearback.sync import format_delta, sync as run_sync
from hearback.util import HearbackError, atomic_write_text, ensure_data_dir, setup_stdio

STATIC_DIR = Path(__file__).resolve().parent / "static"
ACTOR = "human:web"
KEY_HEADER = "X-SWLink-Key"
DEFAULT_PORT = 8765
MAX_BODY = 1024 * 1024
CSP = ("default-src 'self'; img-src 'self' data: https://cdn.discordapp.com https://media.discordapp.net; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
        ".ico": "image/x-icon", ".json": "application/json; charset=utf-8"}
# View Channels, Send Messages, Send Messages in Threads, Read Message History, Attach Files
# view, send, send in threads, read history, attach files; manage threads (forum tags on players' posts);
# manage roles (Supporters page). The owner can untick any of them on Discord's invite screen.
INVITE_PERMISSIONS = (1 << 10) | (1 << 11) | (1 << 38) | (1 << 16) | (1 << 15) | (1 << 34) | (1 << 28)
PORTAL_URL = "https://discord.com/developers/applications"
TEXT_CHANNEL_TYPES = (0, 5)
SEND_LIMIT = 8000
REVIEW_LIMIT = 15                 # threads per AI review run unless the page asks for another number
ADOPT_FIELDS = ("status", "cluster", "root_cause", "fix")
PROPOSE_WHY = "AI review draft, edited by a human"
DETAIL_REPLIES = 30
AFDIAN_DEV_URL = "https://afdian.com/dashboard/dev"
MEMBER_LIST_LIMIT = 1000          # the most members one REST call returns; used for remark suggestions
MEMBER_CACHE_S = 60


def _names(v: Any) -> list[str]:
    """A list of non-empty strings from a JSON value (a comma-separated string is accepted too)."""
    if isinstance(v, str):
        v = v.split(",")
    return list(dict.fromkeys(str(x).strip() for x in v if str(x).strip())) if isinstance(v, list) else []


def _member_view(m: dict) -> dict:
    u = m.get("user") or {}
    return {"id": str(u.get("id", "")), "username": str(u.get("username") or ""),
            "display": str(m.get("nick") or u.get("global_name") or u.get("username") or "")}


def _order_time(o: dict) -> str:
    """An order's creation time as ISO text when Afdian sent a Unix timestamp, else empty."""
    try:
        return dt.datetime.fromtimestamp(float(o.get("create_time")), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


class ApiError(Exception):
    """An error answer with a status code."""

    def __init__(self, status: int, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra or {}


class Request:
    def __init__(self, method: str, path: str, query: dict[str, str], body: dict, groups: tuple[str, ...]):
        self.method, self.path, self.query, self.body, self.groups = method, path, query, body, groups


class App:
    """All state and logic behind the JSON API; the HTTP layer only routes to it."""

    def __init__(self, cfg: Config, *, client_factory: Callable[[], DiscordClient] | None = None,
                 llm_store: LLMStore | None = None, runtime: Any = None, secret: str | None = None,
                 clock: Callable[[], float] = time.time, supporters_store: SupporterStore | None = None,
                 afdian_opener: Callable[..., Any] | None = None, demo: bool = False):
        self.cfg = cfg
        self.demo = demo                 # fake data: tokens and keys are never saved
        self.secret = secret or secrets.token_urlsafe(24)
        self.port = 0
        self.clock = clock
        self._client_factory = client_factory or (lambda: DiscordClient(cfg.token()))
        self.ledger = Ledger(cfg.ledger_path)
        self.queue = Queue(cfg.queue_dir, self.ledger, protected=cfg.protected_paths())
        self.llm = llm_store or LLMStore(cfg.data_dir)
        self.agents = AgentStore(cfg.data_dir)
        self.rules = RuleStore(cfg.data_dir)
        self.inbox = Inbox(cfg.data_dir)
        self.review = review.ReviewRunner(self.ledger, self.agents, self._review_llm, self._model_of)
        self.bot = Chatbot(self.rules, self.agents, self.inbox, self.ledger, self.queue, self._llm_call,
                           self._send_auto, hourly_cap=cfg.swlink_hourly_cap, clock=clock)
        self.runtime = runtime or BotRuntime(self._token, self.on_message, self._bot_state)
        self.supporters_store = supporters_store or SupporterStore(cfg.data_dir)
        self.supporters = Supporters(self.supporters_store, ledger=self.ledger, clock=clock, lang=cfg.language,
                                     templates_dir=cfg.templates_dir, opener=afdian_opener)
        self.supporter_bot = SupporterBot(self.supporters, self._client_factory, self._supporter_guild, self._plain_bot_id)
        self._members_cache: tuple[float, list[dict]] | None = None
        self._dir_cache: tuple[float, list[dict], str] | None = None
        self._bot_id = self._bot_name = ""
        self.routes = self._build_routes()

    # ---- plumbing
    def _token(self) -> str:
        t = self.cfg.token()
        register_secret(t)
        return t

    def _no_secrets(self) -> None:
        """Demo mode saves no token or key (not even a fake one): a real one pasted while recording must not stay."""
        if self.demo:
            raise ApiError(400, "demo_locked")

    def client(self) -> DiscordClient:
        try:
            return self._client_factory()
        except HearbackError:
            raise ApiError(400, "no_token") from None

    def token_state(self) -> dict:
        try:
            self._token()
        except HearbackError:
            return {"set": False, "source": "none"}
        env = (os.environ.get(self.cfg.token_env) or "").strip()
        return {"set": True, "source": "env" if env else "file"}

    def _bot_me(self) -> tuple[str, str]:
        """(id, name) of the bot: from the live gateway, else one cached REST call."""
        st = self.runtime.status()
        if st.get("bot_id"):
            return st["bot_id"], str(st.get("bot_name") or "")
        if not self._bot_id:
            me = self.client().me()
            self._bot_id = str(me.get("id", ""))
            self._bot_name = str(me.get("username") or me.get("global_name") or "")
        return self._bot_id, self._bot_name

    def bot_id(self) -> str:
        return self._bot_me()[0]

    def _plain_bot_id(self) -> str:
        """bot_id() for code outside the HTTP layer: a missing token is a HearbackError there, not an ApiError."""
        try:
            return self.bot_id()
        except ApiError as e:
            raise HearbackError(e.message) from None

    def _llm_call(self, agent: dict, system: str, messages: list[dict]) -> str:
        return complete_any(self.llm, system, messages, prefer=agent.get("profile_id") or None)[0]

    def _review_llm(self, system: str, messages: list, max_tokens: int, prefer: str | None) -> tuple[str, str]:
        return complete_any(self.llm, system, messages, max_tokens, prefer=prefer)

    def _model_of(self, profile_id: str) -> str:
        try:
            return str(self.llm.get(profile_id).get("model") or profile_id)
        except LLMError:
            return profile_id

    def _send_auto(self, channel: str, text: str) -> list[str]:
        return actions.post(self.client(), self.ledger, channel, text)["message_ids"]

    def _bot_state(self, state: str, reason: str) -> None:
        """Gateway state changes go to the event log, so 'was the bot online?' has an answer later."""
        self.ledger.event("bot:swlink", "bot_state", None, {"state": state, "reason": reason[:200]} if reason else {"state": state})

    def on_message(self, msg: Message, bot_id: str) -> None:
        """Gateway callback (runs in a worker thread). A DM with an Afdian order number goes to the supporter flow
        (fixed template replies, no model) when that is switched on; everything else follows the channel rules."""
        if (msg.kind == "dm" and not msg.is_bot and msg.author_id != bot_id
                and self.supporter_bot.handle_dm(msg.author_id, msg.channel_id, msg.content)):
            return
        res = self.bot.handle(msg, bot_id)
        if res.get("action") == "error":
            self.ledger.event("bot:swlink", "error", msg.channel_id, {"error": scrub(res.get("error", ""))[:300]})

    def directory(self, force: bool = False) -> tuple[list[dict], str]:
        """Servers and their text channels: from the live gateway when online, else REST (cached 60 s)."""
        if self.runtime.online and self.runtime.guilds():
            return self.runtime.guilds(), ""
        now = self.clock()
        if not force and self._dir_cache and now - self._dir_cache[0] < 60:
            return self._dir_cache[1], self._dir_cache[2]
        try:
            c = self.client()
            out = []
            for g in c.request("GET", "/users/@me/guilds"):
                chans = c.request("GET", "/guilds/%s/channels" % g["id"])
                cats = {str(ch["id"]): ch.get("name", "") for ch in chans if ch.get("type") == 4}
                chans = sorted(chans, key=lambda ch: ch.get("position", 0))
                out.append({"id": str(g["id"]), "name": g.get("name", ""),
                            "channels": [{"id": str(ch["id"]), "name": ch.get("name", ""),
                                          "category": cats.get(str(ch.get("parent_id")), "")}
                                         for ch in chans if ch.get("type") in TEXT_CHANNEL_TYPES]})
            res: tuple[float, list[dict], str] = (now, out, "")
        except ApiError as e:
            res = (now, [], e.message)
        except HearbackError as e:
            res = (now, [], scrub(str(e))[:200])
        self._dir_cache = res
        return res[1], res[2]

    def _channel_label(self, channel_id: str) -> str:
        rec = self.ledger.load()["threads"].get(channel_id)
        if rec and rec.get("title"):
            return "Bug forum / %s" % rec["title"]
        if self._dir_cache:
            for g in self._dir_cache[1]:
                for c in g["channels"]:
                    if c["id"] == channel_id:
                        return "%s / #%s" % (g["name"], c["name"])
        for c in self.inbox.conversations():
            if c["id"] == channel_id:
                return (c["guild_name"] + " / " if c["guild_name"] else "DM / ") + c["channel_name"]
        return channel_id

    # ---- routes
    def _build_routes(self) -> list[tuple[str, re.Pattern[str], Callable[[Request], tuple[int, Any]]]]:
        r = []

        def add(method: str, pattern: str, fn: Callable[[Request], tuple[int, Any]]) -> None:
            r.append((method, re.compile("^" + pattern + "$"), fn))

        add("GET", "/api/status", self.api_status)
        add("POST", "/api/bot/start", self.api_bot_start)
        add("POST", "/api/bot/stop", self.api_bot_stop)
        add("GET", "/api/conversations", self.api_conversations)
        add("GET", "/api/conversations/(\\d+)/messages", self.api_conv_messages)
        add("POST", "/api/conversations/(\\d+)/draft", self.api_conv_draft)
        add("POST", "/api/polish", self.api_polish)
        add("POST", "/api/conversations/(\\d+)/send", self.api_conv_send)
        add("GET", "/api/rules", self.api_rules_get)
        add("PUT", "/api/rules", self.api_rules_put)
        add("GET", "/api/guilds", self.api_guilds)
        add("GET", "/api/queue", self.api_queue)
        add("POST", "/api/queue/send", self.api_queue_send)
        add("POST", "/api/queue/([\\w-]+)/approve", self.api_queue_approve)
        add("POST", "/api/queue/([\\w-]+)/reject", self.api_queue_reject)
        add("PUT", "/api/queue/([\\w-]+)/text", self.api_queue_text)
        add("POST", "/api/queue/([\\w-]+)/retry", self.api_queue_retry)
        add("GET", "/api/llm", self.api_llm_get)
        add("POST", "/api/llm", self.api_llm_create)
        add("GET", "/api/llm/presets", lambda q: (200, {"items": PRESETS}))
        add("PUT", "/api/llm/settings", self.api_llm_settings)
        add("PUT", "/api/llm/([\\w-]+)", self.api_llm_update)
        add("DELETE", "/api/llm/([\\w-]+)", self.api_llm_delete)
        add("POST", "/api/llm/([\\w-]+)/key", self.api_llm_key)
        add("POST", "/api/llm/([\\w-]+)/activate", self.api_llm_activate)
        add("POST", "/api/llm/([\\w-]+)/test", self.api_llm_test)
        add("GET", "/api/agents", lambda q: (200, {"items": self.agents.list()}))
        add("POST", "/api/agents", lambda q: (201, self.agents.create(q.body)))
        add("PUT", "/api/agents/([\\w-]+)", lambda q: (200, self.agents.update(q.groups[0], q.body)))
        add("DELETE", "/api/agents/([\\w-]+)", self.api_agent_delete)
        add("GET", "/api/forum", self.api_forum)
        add("POST", "/api/forum/sync", self.api_forum_sync)
        add("GET", "/api/forum/review", self.api_review_status)
        add("POST", "/api/forum/review", self.api_review_start)
        add("POST", "/api/forum/review/stop", self.api_review_stop)
        add("POST", "/api/forum/export", self.api_forum_export)
        add("GET", "/api/forum/(\\d+)", self.api_forum_thread)
        add("POST", "/api/forum/(\\d+)/adopt", self.api_forum_adopt)
        add("POST", "/api/forum/(\\d+)/propose", self.api_forum_propose)
        add("GET", "/api/places", self.api_places)
        add("GET", "/api/botmsgs", self.api_botmsgs)
        add("PUT", "/api/botmsgs/(\\d+)/(\\d+)", self.api_botmsg_edit)
        add("DELETE", "/api/botmsgs/(\\d+)/(\\d+)", self.api_botmsg_delete)
        add("GET", "/api/supporters/settings", self.api_sup_settings)
        add("PUT", "/api/supporters/settings", self.api_sup_settings_put)
        add("POST", "/api/supporters/token", self.api_sup_token)
        add("POST", "/api/supporters/test", self.api_sup_test)
        add("GET", "/api/supporters/orders", self.api_sup_orders)
        add("GET", "/api/supporters/claims", self.api_sup_claims)
        add("POST", "/api/supporters/link", self.api_sup_link)
        add("POST", "/api/supporters/unlink", self.api_sup_unlink)
        add("POST", "/api/supporters/simulate", self.api_sup_simulate)
        add("GET", "/api/guilds/(\\d+)/roles", self.api_guild_roles)
        add("GET", "/api/guilds/(\\d+)/members", self.api_guild_members)
        add("GET", "/api/logs", self.api_logs)
        add("GET", "/api/settings", self.api_settings)
        add("PUT", "/api/settings/token", self.api_token)
        add("PUT", "/api/settings/forum", self.api_settings_forum)
        return r

    def dispatch(self, req_method: str, path: str, query: dict[str, str], body: dict) -> tuple[int, Any]:
        known_path = False
        for method, rx, fn in self.routes:
            m = rx.match(path)
            if not m:
                continue
            known_path = True
            if method == req_method:
                return fn(Request(req_method, path, query, body, m.groups()))
        raise ApiError(405 if known_path else 404, "method not allowed" if known_path else "not found")

    # ---- status / bot
    def api_status(self, q: Request) -> tuple[int, Any]:
        st = self.runtime.status()
        guilds = self.runtime.guilds() if self.runtime.online else (self._dir_cache[1] if self._dir_cache else [])
        rules = self.rules.load()
        rule_on = any(r["mode"] != "off" for r in [rules["dm_default"], rules["guild_default"],
                                                    *rules["channels"].values()])
        tok = self.token_state()
        active = self.llm.active_public()
        llm_ready = any(p["key_set"] for p in self.llm.list_public())
        try:
            sup = self.supporters.summary()
            supporters = {"claims": sup["claims"], "retries": sup["retries"]} if sup["enabled"] else None
        except SupporterError:
            supporters = {"claims": 0, "retries": 0, "damaged": True}
        return 200, {
            "version": __version__, "demo": self.demo, "bot": st, "guilds": len(guilds),
            "pending": len(self.queue.list("pending")), "auto_today": self.bot.auto_today(),
            "forum_threads": len(self.ledger.load()["threads"]), "forum_configured": bool(self.cfg.forum_id),
            "forum_to_review": len(review.pending_threads(self.ledger.load())) if self.cfg.forum_id else 0,
            "llm": {"id": active["id"], "name": active["name"], "model": active["model"]} if active else None,
            "token_set": tok["set"], "supporters": supporters,
            "checklist": {"token": tok["set"], "llm": llm_ready, "rule": rule_on}}

    def api_bot_start(self, q: Request) -> tuple[int, Any]:
        return 200, self.runtime.start()

    def api_bot_stop(self, q: Request) -> tuple[int, Any]:
        return 200, self.runtime.stop()

    # ---- conversations
    def api_conversations(self, q: Request) -> tuple[int, Any]:
        guilds, reason = self.directory()
        live = self.runtime.online
        items: dict[str, dict] = {}
        for g in guilds:
            for c in g["channels"]:
                items[c["id"]] = {"id": c["id"], "kind": "guild", "guild_id": g["id"], "guild_name": g["name"],
                                  "channel_name": c["name"], "preview": "", "last_ts": ""}
                if live:       # what the bot member may do there; only the gateway knows (None = unknown = fine)
                    items[c["id"]].update(readable=c.get("readable") is not False,
                                          can_send=c.get("can_send") is not False)
        for c in self.inbox.conversations():
            items.setdefault(c["id"], c).update({"preview": c["preview"], "last_ts": c["last_ts"]})
        for c in items.values():
            rule = self.rules.rule_for(c["id"], c["kind"] == "dm")
            c["mode"], c["agent"] = rule["mode"], rule["agent"]
        out = sorted(items.values(), key=lambda c: (c["guild_name"], c["channel_name"]))
        out.sort(key=lambda c: c["last_ts"], reverse=True)
        out.sort(key=lambda c: c.get("readable") is False)         # channels the bot cannot read go last
        return 200, {"items": out, "reason": reason}

    def _no_access(self, channel: str) -> ApiError:
        try:
            name = self._bot_me()[1]
        except (ApiError, HearbackError):
            name = ""
        return ApiError(403, "no_access", {"channel_id": channel, "bot_name": scrub(name)})

    def _fetch(self, channel: str, limit: int) -> tuple[dict, list[dict]]:
        c = self.client()
        try:
            info = c.request("GET", "/channels/%s" % channel)
            raw = c.request("GET", "/channels/%s/messages" % channel, {"limit": max(1, min(limit, 100))})
        except DiscordError as e:
            if e.status == 403:           # Missing Access / Missing Permissions: the bot may not read this channel
                raise self._no_access(channel) from None
            raise
        return info, list(reversed(raw))

    def api_conv_messages(self, q: Request) -> tuple[int, Any]:
        try:
            limit = int(q.query.get("limit", "50"))
        except ValueError:
            raise ApiError(400, "limit must be a number") from None
        bot_id = self.bot_id()
        _info, raw = self._fetch(q.groups[0], limit)
        items = []
        for m in raw:
            a = m.get("author") or {}
            items.append({"id": str(m.get("id")), "author_id": str(a.get("id", "")), "author_name": display_name(a),
                          "avatar": avatar_url(a), "content": m.get("content") or "", "time": m.get("timestamp") or "",
                          "is_bot": bool(a.get("bot")), "is_self": str(a.get("id", "")) == bot_id,
                          "attachments": len(m.get("attachments") or [])})
        return 200, {"items": items}

    def api_conv_draft(self, q: Request) -> tuple[int, Any]:
        channel = q.groups[0]
        bot_id = self.bot_id()
        info, raw = self._fetch(channel, 21)
        guild_id = str(info["guild_id"]) if info.get("guild_id") else None
        msgs = [message_from_rest(m, bot_id, guild_id=guild_id, channel_name=info.get("name", "")) for m in raw]
        target = next((m for m in reversed(msgs) if not m.is_bot and m.author_id != bot_id and m.content.strip()), None)
        if target is None:
            raise ApiError(400, "nothing to answer yet: no message from another person")
        history = msgs[:msgs.index(target)]
        rule = self.rules.rule_for(channel, guild_id is None)
        agent = self.agents.get_or_auto(q.body.get("agent") or rule.get("agent"))
        system, messages = build_messages(agent, history, target)
        text, pid = complete_any(self.llm, system, messages, prefer=agent.get("profile_id") or None)
        self.ledger.event(ACTOR, "ai_draft", channel, {"agent": agent["id"], "profile": pid})
        return 200, {"text": text, "agent": agent["id"], "profile": pid}

    def api_polish(self, q: Request) -> tuple[int, Any]:
        """Polish text the moderator typed. Nothing is sent; the page puts the result back in the box."""
        text = str(q.body.get("text") or "")
        if not text.strip():
            raise ApiError(400, "type something first")
        if len(text) > SEND_LIMIT:
            raise ApiError(400, "the message is over %d characters" % SEND_LIMIT)
        prefer = None
        if q.body.get("agent"):
            prefer = self.agents.get_or_auto(str(q.body["agent"])).get("profile_id") or None
        system, messages = build_polish(text)
        out, pid = complete_any(self.llm, system, messages, prefer=prefer)
        out = clean_polish(out)
        if not out:
            raise ApiError(502, EMPTY_TEXT_ERROR)
        self.ledger.event(ACTOR, "ai_polish", q.body.get("channel") or None, {"profile": pid, "chars": len(text)})
        return 200, {"text": out, "profile": pid}

    def api_conv_send(self, q: Request) -> tuple[int, Any]:
        text = str(q.body.get("text") or "")
        if not text.strip():
            raise ApiError(400, "the message is empty")
        if len(text) > SEND_LIMIT:
            raise ApiError(400, "the message is over %d characters" % SEND_LIMIT)
        channel = q.groups[0]
        res = actions.post(self.client(), self.ledger, channel, text)
        self.ledger.event(ACTOR, "send", channel, {"messages": res["message_ids"]})
        return 200, {"message_ids": res["message_ids"]}

    # ---- rules / guilds
    def api_rules_get(self, q: Request) -> tuple[int, Any]:
        return 200, {"rules": self.rules.load(), "agents": [{"id": a["id"], "name": a["name"]}
                                                           for a in self.agents.list()]}

    def api_rules_put(self, q: Request) -> tuple[int, Any]:
        clean = self.rules.save(q.body)
        self.ledger.event(ACTOR, "rules_update", None, {"channels": len(clean["channels"])})
        return 200, {"rules": clean}

    def api_guilds(self, q: Request) -> tuple[int, Any]:
        guilds, reason = self.directory(force=q.query.get("refresh") == "1")
        return 200, {"items": guilds, "reason": reason}

    # ---- queue
    def _proposal(self, p: Any) -> dict:
        return {"id": p.id, "kind": p.kind, "state": p.state, "actor": p.actor, "created": p.created,
                "thread": p.thread, "target": self._channel_label(p.thread) if p.thread else "", "text": p.text,
                "why": p.why, "title": p.title, "add": p.add, "remove": p.remove, "error": p.error}

    def api_queue(self, q: Request) -> tuple[int, Any]:
        state = q.query.get("state") or "pending"
        return 200, {"items": [self._proposal(p) for p in self.queue.list(state)]}

    def api_queue_approve(self, q: Request) -> tuple[int, Any]:
        return 200, self._proposal(self.queue.approve([q.groups[0]], ACTOR)[0])

    def api_queue_reject(self, q: Request) -> tuple[int, Any]:
        return 200, self._proposal(self.queue.reject([q.groups[0]], ACTOR, str(q.body.get("reason") or ""))[0])

    def api_queue_retry(self, q: Request) -> tuple[int, Any]:
        """Put a failed proposal back to approved. One that may be half posted is refused (check Discord, use the CLI)."""
        return 200, self._proposal(self.queue.retry(q.groups[0], ACTOR))

    def api_queue_text(self, q: Request) -> tuple[int, Any]:
        """Edit the wording of a pending proposal (the queue refuses any other state)."""
        text = q.body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ApiError(400, "the message is empty")
        if len(text) > SEND_LIMIT:
            raise ApiError(400, "the message is over %d characters" % SEND_LIMIT)
        return 200, self._proposal(self.queue.set_text(q.groups[0], text, ACTOR))

    def api_queue_send(self, q: Request) -> tuple[int, Any]:
        ids = [str(i) for i in q.body.get("ids") or []] or None
        rep = self.queue.send(self.client(), self.ledger, forum_id=self.cfg.forum_id, delay=self.cfg.send_delay,
                              actor=ACTOR, ids=ids)
        return 200, {"sent": [p.id for p in rep.sent], "failed": [{"id": p.id, "error": scrub(p.error)}
                                                                    for p in rep.failed],
                     "mismatches": rep.mismatches, "lines": rep.lines()}

    # ---- llm
    def api_llm_get(self, q: Request) -> tuple[int, Any]:
        return 200, {"settings": self.llm.settings(), "profiles": self.llm.list_public(),
                     "keyring": self.llm.keyring_available()}

    def api_llm_create(self, q: Request) -> tuple[int, Any]:
        b = dict(q.body)
        key = str(b.pop("key", "") or "")
        if key:
            self._no_secrets()
        prof = self.llm.add_profile(b, preset=str(b.get("preset") or ""))
        if key:
            try:
                prof = self.llm.set_key(prof["id"], key)
            except LLMError as e:
                return 201, {**prof, "key_error": str(e)}
        return 201, prof

    def api_llm_update(self, q: Request) -> tuple[int, Any]:
        return 200, self.llm.update_profile(q.groups[0], {k: v for k, v in q.body.items() if k != "key"})

    def api_llm_delete(self, q: Request) -> tuple[int, Any]:
        self.llm.delete_profile(q.groups[0])
        return 200, {"deleted": q.groups[0]}

    def api_llm_key(self, q: Request) -> tuple[int, Any]:
        self._no_secrets()
        return 200, self.llm.set_key(q.groups[0], str(q.body.get("key") or ""))

    def api_llm_activate(self, q: Request) -> tuple[int, Any]:
        self.llm.activate(q.groups[0])
        return 200, {"settings": self.llm.settings()}

    def api_llm_test(self, q: Request) -> tuple[int, Any]:
        res = self.llm.test_profile(q.groups[0])
        if res.get("error") == EMPTY_TEXT_ERROR:
            res["code"] = "empty_text"         # lets the page show its own translation
        return 200, {**res, "profile": self.llm.public(self.llm.get(q.groups[0]))}

    def api_llm_settings(self, q: Request) -> tuple[int, Any]:
        return 200, self.llm.set_settings(q.body)

    def api_agent_delete(self, q: Request) -> tuple[int, Any]:
        self.agents.delete(q.groups[0])
        return 200, {"deleted": q.groups[0]}

    # ---- forum / AI review
    def _forum_ready(self) -> None:
        if not self.cfg.forum_id:
            raise ApiError(400, "No bug forum is set. Add its channel id in Settings first.")

    def api_forum(self, q: Request) -> tuple[int, Any]:
        if not self.cfg.forum_id:
            return 200, {"items": [], "configured": False,
                         "reason": "No bug forum is set. Add the forum channel id in Settings "
                                   "(or forum_id under [discord] in hearback.toml)."}
        data = self.ledger.load()
        rows = []
        for tid, rec in data["threads"].items():
            tri = rec.get("triage") or {}
            replies = rec.get("replies") or []
            rows.append({"id": tid, "title": rec.get("title", ""), "author": rec.get("author", ""),
                         "status": tri.get("status", "new"), "cluster": tri.get("cluster", ""),
                         "tags": rec.get("tags", []), "reviewed": tri.get("reviewed", ""),
                         "needs_review": review.needs_review(rec), "ai": review.ai_summary(rec),
                         "last_activity": (replies[-1].get("time") if replies else rec.get("created", ""))})
        rows.sort(key=lambda r: r["last_activity"] or "", reverse=True)
        active = self.llm.active_public()
        return 200, {"items": rows, "configured": True, "reason": "", "synced_at": data.get("synced_at", ""),
                     "to_review": len(review.pending_threads(data)), "review_limit": REVIEW_LIMIT,
                     "model": active["model"] if active else "", "statuses": [s for s in STATUSES]}

    def api_forum_sync(self, q: Request) -> tuple[int, Any]:
        self.cfg.require_forum()
        delta = run_sync(self.client(), self.ledger, self.cfg, download=True, actor=ACTOR)
        return 200, {"summary": scrub(format_delta(delta))}

    def _thread_id(self, ident: str) -> str:
        self._forum_ready()
        return self.ledger.resolve(ident)

    def api_forum_thread(self, q: Request) -> tuple[int, Any]:
        tid = self._thread_id(q.groups[0])
        data = self.ledger.load()
        rec = data["threads"][tid]
        tri = rec.get("triage") or {}
        posted = {str(p.get("message_id")) for p in tri.get("replies_posted") or []}
        replies = rec.get("replies") or []
        guild = str(data.get("guild_id") or self.cfg.guild_id or "")
        return 200, {
            "id": tid, "title": rec.get("title", ""), "author": rec.get("author", ""), "created": rec.get("created", ""),
            "tags": rec.get("tags", []), "body": str(rec.get("body") or "")[:8000],
            "attachments": [os.path.basename(str(a)) for a in rec.get("attachments") or []],
            "reply_count": len(replies),
            "replies": [{"id": r.get("id"), "author": r.get("author", ""), "time": r.get("time", ""),
                         "content": str(r.get("content") or "")[:4000], "bot": bool(r.get("bot")) or str(r.get("id")) in posted,
                         "attachments": [os.path.basename(str(a)) for a in r.get("attachments") or []]}
                        for r in replies[-DETAIL_REPLIES:]],
            "triage": {k: tri.get(k, "") for k in ("status", "cluster", "root_cause", "fix", "reviewed", "reviewed_by")},
            "ai": tri.get("ai") if isinstance(tri.get("ai"), dict) else None,
            "needs_review": review.needs_review(rec), "forum_tags": list((data.get("tags") or {}).values()),
            "link": "https://discord.com/channels/%s/%s" % (guild, tid) if guild else ""}

    def api_review_status(self, q: Request) -> tuple[int, Any]:
        st = self.review.status()
        st["to_review"] = len(review.pending_threads(self.ledger.load()))
        return 200, st

    def api_review_start(self, q: Request) -> tuple[int, Any]:
        self._forum_ready()
        try:
            limit = max(1, min(int(q.body.get("limit") or REVIEW_LIMIT), 100))
        except (TypeError, ValueError):
            raise ApiError(400, "limit must be a number") from None
        agent_id = str(q.body.get("agent") or "") or None
        agent = self.agents.get_or_auto(agent_id or review.DEFAULT_AGENT)
        if not self.llm.candidates(agent.get("profile_id") or None):
            raise ApiError(400, "No model is set up yet. Add one on the Models page first.")
        ids = review.pending_threads(self.ledger.load(), limit)
        try:
            job = self.review.start(ids, agent["id"])
        except review.ReviewBusy as e:
            raise ApiError(409, str(e)) from None
        self.ledger.event(ACTOR, "ai_review_start", None, {"threads": len(ids), "agent": agent["id"]})
        return 200, {"job": job, "total": len(ids)}

    def api_review_stop(self, q: Request) -> tuple[int, Any]:
        self.ledger.event(ACTOR, "ai_review_stop", None, None)
        return 200, self.review.stop()

    def api_forum_adopt(self, q: Request) -> tuple[int, Any]:
        tid = self._thread_id(q.groups[0])
        fields = q.body.get("fields")
        if not isinstance(fields, dict):
            raise ApiError(400, "fields must be an object, e.g. {\"status\": \"investigating\"}")
        patch: dict[str, str] = {}
        for k, v in fields.items():
            if k not in ADOPT_FIELDS:
                raise ApiError(400, "cannot adopt %r; choose from: %s" % (k, ", ".join(ADOPT_FIELDS)))
            if str(v or "").strip():
                patch[k] = str(v).strip()[:2000]
        if not patch:
            raise ApiError(400, "choose at least one field with a value")
        tri = self.ledger.update_triage(tid, patch, actor=ACTOR)
        return 200, {"triage": {k: tri.get(k, "") for k in ("status", "cluster", "root_cause", "fix", "reviewed")},
                     "needs_review": review.needs_review(self.ledger.load()["threads"][tid])}

    def api_forum_propose(self, q: Request) -> tuple[int, Any]:
        tid = self._thread_id(q.groups[0])
        text = str(q.body.get("text") or "").strip()
        add, remove = _names(q.body.get("add")), _names(q.body.get("remove"))
        if not text and not add and not remove:
            raise ApiError(400, "write a reply or pick a tag first")
        known = {t.lower(): t for t in (self.ledger.load().get("tags") or {}).values()}
        if known:
            bad = [n for n in add + remove if n.lower() not in known]
            if bad:
                raise ApiError(400, "no forum tag named %s. Available: %s" % (", ".join(map(repr, bad)),
                                                                              ", ".join(known.values())))
            add, remove = [known[n.lower()] for n in add], [known[n.lower()] for n in remove]
        made: list[str] = []
        error = ""
        if text:
            made.append(self.queue.propose("reply", actor=ACTOR, thread=tid, text=text, why=PROPOSE_WHY).id)
        if add or remove:
            try:
                made.append(self.queue.propose("tags", actor=ACTOR, thread=tid, add=add, remove=remove,
                                               why=PROPOSE_WHY).id)
            except HearbackError as e:
                if not made:
                    raise
                error = scrub(str(e))[:300]
        return 200, {"proposals": made, "error": error}

    def api_forum_export(self, q: Request) -> tuple[int, Any]:
        ids = [str(i) for i in q.body["ids"]] if isinstance(q.body.get("ids"), list) else None
        path, md, n = review.export_fix_list(self.ledger, ids)
        self.ledger.event(ACTOR, "export_fix_list", None, {"threads": n, "file": path.name})
        return 200, {"path": str(path), "filename": path.name, "markdown": md, "count": n}

    # ---- bot messages (edit or delete what the bot itself posted)
    def _place_label(self, channel: str) -> str:
        rec = self.ledger.load()["threads"].get(channel)
        if rec:
            return str(rec.get("title") or channel)
        label = self._channel_label(channel)
        if label != channel:
            return label
        try:
            return "#" + str(self.client().request("GET", "/channels/%s" % channel).get("name") or channel)
        except (ApiError, HearbackError):
            return channel

    def api_places(self, q: Request) -> tuple[int, Any]:
        """Places besides plain channels: open threads outside the bug forum (grouped by their parent) and DMs."""
        guilds, reason = self.directory()
        known = set(self.ledger.load()["threads"])
        threads = []
        try:
            c = self.client()
            for g in guilds:
                names = {str(ch["id"]): ch.get("name", "") for ch in c.request("GET", "/guilds/%s/channels" % g["id"])}
                for th in c.request("GET", "/guilds/%s/threads/active" % g["id"]).get("threads", []):
                    tid, parent = str(th.get("id")), str(th.get("parent_id") or "")
                    if tid in known or parent == self.cfg.forum_id:
                        continue
                    threads.append({"id": tid, "name": th.get("name", ""), "guild": g["name"],
                                    "parent": names.get(parent, parent)})
        except ApiError as e:
            reason = reason or e.message
        except HearbackError as e:
            reason = reason or scrub(str(e))[:200]
        dms = [{"id": c["id"], "name": c["channel_name"], "last": c["last_ts"]}
               for c in self.inbox.conversations() if not c["guild_id"]]
        return 200, {"threads": threads, "dms": dms, "reason": reason}

    def api_botmsgs(self, q: Request) -> tuple[int, Any]:
        channel, focus = editor.parse_target(q.query.get("channel"))
        if not channel:
            raise ApiError(400, "pick a place, or paste a Discord channel or message link")
        msgs = actions.list_bot_messages(self.client(), channel, self.bot_id(), 100)
        items = [{"id": str(m["id"]), "content": m.get("content") or "", "time": m.get("timestamp") or "",
                  "edited": bool(m.get("edited_timestamp")),
                  "attachments": [str(a.get("filename") or "") for a in m.get("attachments") or []]} for m in msgs]
        return 200, {"channel": channel, "label": self._place_label(channel), "focus": focus or "", "items": items}

    def _own_message(self, channel: str, message: str) -> dict:
        """The message, only if the bot wrote it (the delete button must never reach other people's messages)."""
        msg = self.client().request("GET", "/channels/%s/messages/%s" % (channel, message))
        if str((msg.get("author") or {}).get("id", "")) != self.bot_id():
            raise ApiError(400, "that message was not written by the bot")
        return msg

    def api_botmsg_edit(self, q: Request) -> tuple[int, Any]:
        channel, message = q.groups
        msg = self._own_message(channel, message)
        new = editor.save_message(self.client(), self.ledger, channel, msg, str(q.body.get("text") or ""), ACTOR)
        self.inbox.revise(channel, message, new.get("content", ""))     # later replies see the corrected text
        return 200, {"id": message, "content": new.get("content", "")}

    def api_botmsg_delete(self, q: Request) -> tuple[int, Any]:
        channel, message = q.groups
        msg = self._own_message(channel, message)
        editor.remove_message(self.client(), self.ledger, channel, msg, ACTOR)
        self.inbox.revise(channel, message, None)                        # and no longer build on a deleted reply
        return 200, {"deleted": message}

    # ---- supporters (Afdian orders -> a Discord role)
    def _supporter_guild(self) -> str:
        """The server the supporter role lives in: the configured one, else the only one the bot is in."""
        if self.cfg.guild_id:
            return self.cfg.guild_id
        guilds, _reason = self.directory()
        return str(guilds[0]["id"]) if len(guilds) == 1 else ""

    def _sup_overview(self) -> dict:
        store = self.supporters_store
        cfg = store.load()
        guild = self._supporter_guild()
        known = self._dir_cache[1] if self._dir_cache else self.runtime.guilds()
        return {"settings": store.public(cfg), "token_set": store.has_token(), "keyring": store.keyring_available(),
                "guild": {"id": guild, "name": next((g["name"] for g in known if str(g["id"]) == guild), "")},
                "issues": self.supporter_bot.issues_view(),
                "counts": {"claims": len(cfg["claims"]), "retries": len(cfg["retry"])}, "dev_url": AFDIAN_DEV_URL}

    def api_sup_settings(self, q: Request) -> tuple[int, Any]:
        return 200, self._sup_overview()

    def api_sup_settings_put(self, q: Request) -> tuple[int, Any]:
        store = self.supporters_store
        with store.lock:
            cfg = store.load()
            new = clean_settings(q.body, cfg)
            if new["enabled"] and new["dm_handling"] and not (new["role_id"] and new["afdian_user_id"] and store.has_token()):
                raise ApiError(400, "dm_needs_setup")
            cfg.update(new)
            store.save(cfg)
        self.ledger.event(ACTOR, "supporters_settings", None, {"fields": sorted(q.body)})
        return 200, self._sup_overview()

    def api_sup_token(self, q: Request) -> tuple[int, Any]:
        self._no_secrets()
        self.supporters_store.set_token(str(q.body.get("token") or ""))
        self.ledger.event(ACTOR, "supporters_token_update", None, None)
        return 200, {"token_set": True}

    def _afdian(self) -> Any:
        client = self.supporters.make_client()
        if client is None:
            raise ApiError(400, "afdian_not_set")
        return client

    def api_sup_test(self, q: Request) -> tuple[int, Any]:
        client = self._afdian()
        try:
            client.ping()
        except SupporterError as e:
            return 200, {"ok": False, "error": scrub(str(e))}
        return 200, {"ok": True}

    def _guild_members(self) -> tuple[list[dict], bool]:
        """Humans in the server as {id, username, display}, cached briefly. ([], False) when Discord will not say."""
        now = self.clock()
        if self._members_cache and now - self._members_cache[0] < MEMBER_CACHE_S:
            return self._members_cache[1], True
        guild = self._supporter_guild()
        if not guild:
            return [], False
        try:
            raw = self.client().request("GET", "/guilds/%s/members" % guild, {"limit": MEMBER_LIST_LIMIT})
        except (ApiError, HearbackError):
            return [], False
        members = [_member_view(m) for m in raw if not (m.get("user") or {}).get("bot")]
        self._members_cache = (now, members)
        return members, True

    def api_sup_orders(self, q: Request) -> tuple[int, Any]:
        amounts = q.query.get("amounts") == "1"
        orders = self._afdian().recent_orders(pages=1)
        claims = self.supporters_store.load()["claims"]
        members, members_ok = self._guild_members()
        suggest: dict[str, list[dict]] = {}
        for s in match_remarks(orders, members, claimed=claims):
            suggest.setdefault(s["order"], []).append({k: s[k] for k in ("user_id", "name", "match", "remark", "buyer")})
        items = []
        for o in orders:
            no = str(o.get("out_trade_no") or "")
            c = claims.get(no)
            item = {"id": no, "masked": mask(no), "time": _order_time(o), "plan_id": str(o.get("plan_id") or ""),
                    "title": str(o.get("plan_title") or o.get("title") or "")[:80],
                    "buyer": str(o.get("user_name") or "")[:80], "paid": str(o.get("status")) == "2",
                    "claim": {"user_id": str(c.get("user_id")), "user_name": str(c.get("user_name") or ""),
                              "at": str(c.get("at") or "")} if c else None,
                    "suggestions": suggest.get(no, [])}
            if amounts:
                item["amount"] = str(o.get("total_amount") or "")
            items.append(item)
        return 200, {"items": items, "members_available": members_ok}

    def api_sup_claims(self, q: Request) -> tuple[int, Any]:
        return 200, {"items": self.supporters.claims_view(q.query.get("amounts") == "1")}

    def api_sup_link(self, q: Request) -> tuple[int, Any]:
        no, uid = check_order_no(q.body.get("order")), check_id(q.body.get("user_id"), "user_id")
        guild = self._supporter_guild()
        if not guild:
            raise ApiError(400, "no_guild")
        client = self.client()
        person = self.supporter_bot.member(client, guild, uid)
        if person is None:
            raise ApiError(400, "not_member")
        order, lookup = None, "skipped"
        afd = self.supporters.make_client()
        if afd is not None:
            try:
                order, lookup = afd.find_order(no), "ok"
            except SupporterError:
                lookup = "failed"
            if lookup == "ok" and (order is None or str(order.get("status")) != "2"):
                raise ApiError(400, "order_not_found")
        claim = self.supporters.link_order(no, uid, ACTOR, user_name=person.name, order=order)
        roles = self.supporters.roles_for(self.supporters_store.load(), claim.get("plan_id"))
        done, failed = self.supporter_bot.grant(client, guild, uid, [r for r in roles if r not in person.role_ids],
                                                actor=ACTOR)
        shown = {k: v for k, v in claim.items() if k != "amount"}          # amounts stay out unless asked for
        return 200, {"claim": {**shown, "order": no, "masked": mask(no)}, "granted": done, "lookup": lookup,
                     "failed": [{"kind": f["kind"], "role_name": f["role_name"], "bot_role": f.get("bot_role", "")}
                                for f in failed]}

    def api_sup_unlink(self, q: Request) -> tuple[int, Any]:
        claim = self.supporters.unlink(q.body.get("order"), ACTOR)
        removed: list[str] = []
        failed: list[str] = []
        if q.body.get("remove_role") is True:
            roles = self.supporters.roles_to_remove(claim)
            guild = self._supporter_guild()
            if roles and guild:
                removed, failed = self.supporter_bot.remove(self.client(), guild, str(claim["user_id"]), roles, actor=ACTOR)
        return 200, {"unlinked": mask(claim["order"]), "removed_roles": removed, "failed_roles": failed}

    def api_sup_simulate(self, q: Request) -> tuple[int, Any]:
        """Dry run of a supporter DM. It asks Afdian and Discord (reads only) and writes nothing anywhere."""
        cfg = self.supporters_store.load()
        uid = str(q.body.get("user_id") or "").strip()
        person = Person(id="0", name="simulated", display="模拟成员" if self.cfg.language == "zh" else "Simulated member",
                        role_ids=set(cfg["require_role_ids"]))
        assumed = not uid
        client = None
        guild = self._supporter_guild()
        if uid:
            uid = check_id(uid, "user_id")
            if not guild:
                raise ApiError(400, "no_guild")
            client = self.client()
            member = self.supporter_bot.member(client, guild, uid)
            if member is None:
                raise ApiError(400, "not_member")
            person = member
        res = self.supporters.simulate(str(q.body.get("text") or ""), person)
        if res is None:
            return 200, {"no_order": True, "order": "", "reply_key": "", "reply": "", "grant": [], "notify": [],
                         "assumed": assumed}
        names: dict[str, str] = {}
        if res["grant_role_ids"] and guild:
            try:
                info = self.supporter_bot.roles_info(client or self.client(), guild)
                names = {r["id"]: r["name"] for r in info["items"]}
            except (ApiError, HearbackError):
                names = {}
        return 200, {"no_order": False, "order": res["order"], "reply_key": res["reply_key"], "reply": res["reply"],
                     "grant": [{"id": r, "name": names.get(r, r)} for r in res["grant_role_ids"]],
                     "notify": res["notify_texts"], "assumed": assumed}

    def api_guild_roles(self, q: Request) -> tuple[int, Any]:
        try:
            return 200, self.supporter_bot.roles_info(self.client(), q.groups[0])
        except DiscordError:
            raise ApiError(400, "roles_unavailable") from None

    def api_guild_members(self, q: Request) -> tuple[int, Any]:
        guild, text = q.groups[0], q.query.get("q", "").strip()[:50]
        try:
            if text:
                raw = self.client().request("GET", "/guilds/%s/members/search" % guild, {"query": text, "limit": 20})
            else:
                raw = self.client().request("GET", "/guilds/%s/members" % guild, {"limit": 20})
        except DiscordError:
            raise ApiError(400, "members_unavailable") from None
        return 200, {"items": [_member_view(m) for m in raw if not (m.get("user") or {}).get("bot")]}

    # ---- logs
    def api_logs(self, q: Request) -> tuple[int, Any]:
        try:
            limit = max(1, min(int(q.query.get("limit", "200")), 1000))
        except ValueError:
            raise ApiError(400, "limit must be a number") from None
        events = self.ledger.read_events(limit)
        return 200, {"items": scrub_obj(list(reversed(events)))}

    # ---- settings
    def api_settings(self, q: Request) -> tuple[int, Any]:
        bot_id = self.runtime.status().get("bot_id") or ""
        if not bot_id and self.token_state()["set"]:
            try:
                bot_id = self.bot_id()
            except (ApiError, HearbackError):
                bot_id = ""
        invite = ""
        if bot_id:
            invite = ("https://discord.com/oauth2/authorize?client_id=%s&scope=bot&permissions=%d"
                      % (urllib.parse.quote(bot_id), INVITE_PERMISSIONS))
        return 200, {"token": self.token_state(), "port": self.port, "bot_id": bot_id, "invite_url": invite,
                     "portal_url": PORTAL_URL, "hourly_cap": self.cfg.swlink_hourly_cap, "version": __version__,
                     "guild_id": self.cfg.guild_id, "forum_id": self.cfg.forum_id}

    def api_settings_forum(self, q: Request) -> tuple[int, Any]:
        ids = {k: str(q.body.get(k) or "").strip() for k in ("guild_id", "forum_id") if k in q.body}
        if not ids:
            raise ApiError(400, "send guild_id and/or forum_id")
        path = save_discord_ids(self.cfg, **ids)
        if path not in self.queue.protected:
            self.queue.protected.append(path)        # a config file created just now is protected like the others
        self.ledger.event(ACTOR, "forum_update", None, None)
        return 200, {"guild_id": self.cfg.guild_id, "forum_id": self.cfg.forum_id}

    def api_token(self, q: Request) -> tuple[int, Any]:
        self._no_secrets()
        tok = str(q.body.get("token") or "").strip()
        if tok.lower().startswith("bot "):
            tok = tok[4:].strip()
        if not tok or len(tok) > 300 or any(c.isspace() for c in tok):
            raise ApiError(400, "the token is empty or contains spaces")
        path = self.cfg.token_path
        ensure_data_dir(path.parent)
        atomic_write_text(path, tok + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        register_secret(tok)
        self._bot_id = self._bot_name = ""
        self._dir_cache = None
        self.ledger.event(ACTOR, "token_update", None, None)
        return 200, {"set": True, "source": self.token_state()["source"]}


# ---------------------------------------------------------------- HTTP layer

class _Server(ThreadingHTTPServer):
    allow_reuse_address = False      # a busy port must stay busy (SO_REUSEADDR would share it on Windows)
    daemon_threads = True
    app: App


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "SWLink"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:   # silent: nothing about requests is logged
        pass

    # ---- responses
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str, extra: dict | None = None) -> None:
        self._json(status, {"error": message, **(extra or {})})

    # ---- request handling
    def _host_ok(self) -> bool:
        port = self.server.app.port
        return (self.headers.get("Host") or "") in ("127.0.0.1:%d" % port, "localhost:%d" % port)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        port = self.server.app.port
        return origin is None or origin in ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port)

    def _handle(self, method: str) -> None:
        if not self._host_ok() or not self._origin_ok():
            return self._error(403, "forbidden host")
        parts = urllib.parse.urlsplit(self.path)
        if parts.path.startswith("/api/"):
            return self._api(method, parts)
        if method not in ("GET", "HEAD"):
            return self._error(405, "method not allowed")
        self._static(parts.path)

    def _api(self, method: str, parts: urllib.parse.SplitResult) -> None:
        app = self.server.app
        sent = (self.headers.get(KEY_HEADER) or "").encode("utf-8")
        if not hmac.compare_digest(sent, app.secret.encode("utf-8")):
            return self._error(403, "forbidden")
        body: dict = {}
        if method in ("POST", "PUT", "DELETE"):
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._error(415, "Content-Type must be application/json")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._error(400, "bad Content-Length")
            if n < 0:
                return self._error(400, "bad Content-Length")
            if n > MAX_BODY:
                return self._error(413, "body too large")
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except ValueError:
                return self._error(400, "body is not valid JSON")
            if not isinstance(body, dict):
                return self._error(400, "body must be a JSON object")
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        try:
            status, obj = app.dispatch(method, parts.path, query, body)
        except ApiError as e:
            return self._error(e.status, e.message, e.extra)
        except HearbackError as e:
            return self._error(400, scrub(str(e))[:600])
        except Exception as e:   # never leak internals (or secrets inside them)
            if os.environ.get("HEARBACK_DEBUG"):
                import traceback
                traceback.print_exc()
            return self._error(500, "internal error: %s" % type(e).__name__)
        self._json(status, scrub_obj(obj))

    def _static(self, path: str) -> None:
        rel = urllib.parse.unquote(path)
        if "\x00" in rel or "\\" in rel or ".." in rel.split("/"):
            return self._error(404, "not found")
        rel = rel.lstrip("/") or "index.html"
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file() or target.suffix not in MIME:
            return self._error(404, "not found")
        self._send(200, target.read_bytes(), MIME[target.suffix])

    def do_GET(self) -> None: self._handle("GET")
    def do_HEAD(self) -> None: self._handle("HEAD")
    def do_POST(self) -> None: self._handle("POST")
    def do_PUT(self) -> None: self._handle("PUT")
    def do_DELETE(self) -> None: self._handle("DELETE")


def create_server(cfg: Config, port: int = DEFAULT_PORT, **app_kw: Any) -> _Server:
    """Bind 127.0.0.1:<port> (any free port if it is busy or 0) and attach a fresh App. Not serving yet."""
    srv: _Server | None = None
    for p in ([port, 0] if port else [0]):
        try:
            srv = _Server(("127.0.0.1", p), _Handler)
            break
        except OSError:
            continue
    if srv is None:
        raise HearbackError("could not open a local port")
    app = App(cfg, **app_kw)
    app.port = srv.server_address[1]
    srv.app = app
    return srv


def serve(srv: _Server, cfg: Config, open_browser: bool = True, url_extra: str = "") -> int:
    """Serve a bound server until Ctrl+C; prints the link that carries this run's key."""
    app = srv.app
    url = "http://127.0.0.1:%d/#k=%s%s" % (app.port, app.secret, url_extra)
    print("SWLink is running. Open this link (it carries this run's key):\n  %s\nPress Ctrl+C to stop." % url,
          flush=True)
    app.supporter_bot.start()
    if cfg.swlink_autostart:
        st = app.runtime.start()
        print("Bot: %s%s" % (st.get("state", "?"), (" (%s)" % st["reason"]) if st.get("reason") else ""), flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.supporter_bot.stop()
        app.runtime.stop()
        srv.server_close()
    return 0


def run(cfg: Config, port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    return serve(create_server(cfg, port), cfg, open_browser)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="swlink", description="SWLink: web console and chat bot for Hearback.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="local port (0 or busy: pick a free one)")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    ap.add_argument("--config", help="path to hearback.toml (default: search upwards)")
    ap.add_argument("--demo", action="store_true", help="try the console on fake data: no Discord, no network")
    ap.add_argument("--lang", choices=["en", "zh"], default="en", help="language of the demo data (with --demo)")
    args = ap.parse_args(list(argv) if argv is not None else None)
    setup_stdio()
    try:
        if args.demo:
            from hearback.webdemo import run_demo
            return run_demo(args.lang, args.port, not args.no_browser)
        return run(load_config(args.config), args.port, not args.no_browser)
    except HearbackError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
