"""SWLink web console: a local-only HTTP server (stdlib) serving a static page and a small JSON API.

Safety: binds 127.0.0.1 only; every /api request needs the per-run secret in `X-SWLink-Key`; the Host header
must be this server's own address (DNS rebinding); writes need a JSON content type; static files come only from
the package `static/` directory. The bot token and API keys are write-only: no response, log or file written by
this module contains them. Writes made from the page use the actor `human:web`.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Sequence

from hearback import __version__, actions, editor, feedback, review
from hearback.agents import AgentStore
from hearback.chatbot import (BotRuntime, Chatbot, Inbox, Message, RuleStore, avatar_url, build_messages,
                              build_polish, clean_polish, display_name, message_from_rest)
from hearback.codes import Codes
from hearback.config import CONFIG_NAME, DATA_DIR, Config, load_config, save_discord_ids
from hearback.discord import DiscordClient, DiscordError
from hearback.ledger import STATUSES, Ledger
from hearback.llm import (EMPTY_TEXT_ERROR, PRESETS, LLMError, LLMStore, complete_any, register_secret, scrub,
                          scrub_obj, take_usage)
from hearback.queue import Queue
from hearback.supporters import (Person, SupporterBot, SupporterError, Supporters, SupporterStore, check_id,
                                 check_order_no, clean_settings, extract_order_numbers, mask, match_remarks,
                                 parse_amount)
from hearback.sync import format_delta, sync as run_sync, sync_all
from hearback.util import HearbackError, atomic_write_text, ensure_data_dir, is_image, setup_stdio
from hearback.welcome import Welcome

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
FILE_MAX = 8 * 1024 * 1024        # the biggest picture the page asks to see
BULK_MAX = 100                    # posts in one "let the AI go through them" run
FEEDBACK_WHY = "Written in the feedback inbox and sent by a person"


def _names(v: Any) -> list[str]:
    """A list of non-empty strings from a JSON value (a comma-separated string is accepted too)."""
    if isinstance(v, str):
        v = v.split(",")
    return list(dict.fromkeys(str(x).strip() for x in v if str(x).strip())) if isinstance(v, list) else []


def _member_view(m: dict) -> dict:
    u = m.get("user") or {}
    return {"id": str(u.get("id", "")), "username": str(u.get("username") or ""),
            "display": str(m.get("nick") or u.get("global_name") or u.get("username") or "")}


def _masked(text: Any) -> bool:
    """The page would see this text with a secret (an issued code, say) blanked out, so it must not edit it."""
    return scrub(text) != str(text)


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


class Workspace:
    """One SWLink folder (its hearback.toml and .hearback/) as the console sees it: the bug forum ledger, the
    approval queue, AI review and supporters of that server. The bot connection, chat rules, inbox, models and
    agents are the bot's, so they belong to the App and stay the same whichever workspace is open."""

    def __init__(self, app: "App", cfg: Config, *, supporters_store: SupporterStore | None = None,
                 afdian_opener: Callable[..., Any] | None = None):
        self.cfg = cfg
        self.ledger = Ledger(cfg.ledger_path)
        self.queue = Queue(cfg.queue_dir, self.ledger, protected=cfg.protected_paths())
        self.review = review.ReviewRunner(self.ledger, app.agents, app._review_llm, app._model_of)
        self.supporters_store = supporters_store or SupporterStore(cfg.data_dir)
        self.supporters = Supporters(self.supporters_store, ledger=self.ledger, clock=app.clock, lang=cfg.language,
                                     templates_dir=cfg.templates_dir, opener=afdian_opener)
        self.supporter_bot = SupporterBot(self.supporters, app._client_factory, lambda: app._supporter_guild(self),
                                          app._plain_bot_id)
        self.codes = Codes(cfg.codes or {}, cfg.data_dir, lang=cfg.language, templates_dir=cfg.templates_dir,
                           ledger=self.ledger, clock=app.clock, runner=app._code_runner)
        self.welcome = Welcome(cfg.welcome or {}, cfg.root, cfg.data_dir, self.ledger)
        self.forums = feedback.ForumSettings(cfg.data_dir)
        self.sync_lock = threading.Lock()    # one pull from Discord at a time per server


class App:
    """All state and logic behind the JSON API; the HTTP layer only routes to it.

    `home` is the workspace SWLink was started in: its token runs the one bot connection, and the bot's own
    events and drafts go to its ledger and queue. `[swlink] workspaces` adds folders for other servers the same
    bot is in; the page switches `cur` between them (forum, approvals, supporters, logs, forum ids)."""

    def __init__(self, cfg: Config, *, client_factory: Callable[[], DiscordClient] | None = None,
                 llm_store: LLMStore | None = None, runtime: Any = None, secret: str | None = None,
                 clock: Callable[[], float] = time.time, supporters_store: SupporterStore | None = None,
                 afdian_opener: Callable[..., Any] | None = None, demo: bool = False,
                 workspaces: Sequence[Config] | None = None, code_runner: Callable[..., Any] | None = None,
                 start_other: Callable[[Path], Any] | None = None):
        self.demo = demo                 # fake data: tokens and keys are never saved
        self.secret = secret or secrets.token_urlsafe(24)
        self.port = 0
        self.clock = clock
        self._client_factory = client_factory or (lambda: DiscordClient(cfg.token()))
        self._code_runner = code_runner or subprocess.run      # [codes] command; tests pass a fake
        self.llm = llm_store or LLMStore(cfg.data_dir)
        self.agents = AgentStore(cfg.data_dir)
        self.usage = feedback.Usage(cfg.data_dir, clock)
        self.rules = RuleStore(cfg.data_dir)
        self.inbox = Inbox(cfg.data_dir)
        self._dir_cache: tuple[float, list[dict], str] | None = None
        self.home = Workspace(self, cfg, supporters_store=supporters_store, afdian_opener=afdian_opener)
        self.cur = self.home
        self.spaces = [self.home]
        self.broken: list[dict] = []     # [swlink] workspaces entries that could not be opened: {folder, error}
        for c in (workspaces if workspaces is not None else self._load_spaces(cfg)):
            self.spaces.append(Workspace(self, c, afdian_opener=afdian_opener))
        self.bot = Chatbot(self.rules, self.agents, self.inbox, self.home.ledger, self.home.queue, self._llm_call,
                           self._send_auto, hourly_cap=cfg.swlink_hourly_cap, clock=clock)
        self.runtime = runtime or BotRuntime(self._token, self.on_message, self._bot_state,
                                                 self.on_missed, lambda: self.bot.inbox.last_dm_ids(), self.on_join,
                                                 lambda: any(ws.welcome.enabled for ws in self.spaces))
        self.others = other_folders(cfg)        # SWLink folders of other bots: shared order claims, the server picker
        self.peers = self._load_peers(cfg)      # (folder name, supporter store) of those folders
        self._start_other = start_other or start_folder
        self._members_cache: tuple[float, list[dict]] | None = None
        self._member_ids_cache: dict[str, tuple[float, set[str] | None]] = {}
        self._dm_peers: dict[str, tuple[float, dict | None]] = {}     # DM channel id -> the person at the other end
        self._bot_id = self._bot_name = ""
        self.routes = self._build_routes()

    # ---- the open workspace (what the forum, approvals, supporters, logs and forum-id pages show)
    cfg = property(lambda self: self.cur.cfg)
    ledger = property(lambda self: self.cur.ledger)
    queue = property(lambda self: self.cur.queue)
    review = property(lambda self: self.cur.review)
    supporters_store = property(lambda self: self.cur.supporters_store)
    supporters = property(lambda self: self.cur.supporters)
    supporter_bot = property(lambda self: self.cur.supporter_bot)

    def _load_spaces(self, cfg: Config) -> list[Config]:
        """The configs named in `[swlink] workspaces` (folders, relative to this config). A folder that does not
        load, repeats one, or holds a different bot token is skipped and listed in `broken`."""
        out: list[Config] = []
        seen = {cfg.root.resolve()}
        for entry in cfg.swlink_workspaces:
            d = Path(entry)
            d = (d if d.is_absolute() else cfg.root / d).resolve()
            folder = d.parent.name if d.name == CONFIG_NAME else d.name
            try:
                c = load_config(d if d.is_file() else d / CONFIG_NAME)
                if c.root.resolve() in seen:
                    raise HearbackError("this folder is already open")
                if not self._same_bot(cfg, c):
                    raise HearbackError("it holds a different bot token; one SWLink runs one bot")
            except HearbackError as e:
                self.broken.append({"folder": folder, "error": scrub(str(e))[:200]})
                continue
            seen.add(c.root.resolve())
            out.append(c)
        return out

    @staticmethod
    def _load_peers(cfg: Config) -> list[tuple[str, SupporterStore]]:
        """`[swlink] others`: folders of SWLinks that run other bots; their claimed orders are only read, so
        one Afdian order still works in one server only."""
        return [(d.name, SupporterStore(d / DATA_DIR)) for d in other_folders(cfg)]

    @staticmethod
    def _same_bot(home: Config, other: Config) -> bool:
        """True when `other` has no token of its own or the same one as `home` (compared, never shown)."""
        try:
            theirs = other.token()
        except HearbackError:
            return True
        register_secret(theirs)
        try:
            mine = home.token()
        except HearbackError:
            return False
        return hmac.compare_digest(theirs.encode(), mine.encode())

    # ---- which places the open workspace shows (chat, channel rules, bot messages); one workspace shows all
    def _here(self, guild_id: str) -> bool:
        """A server's channels show on the workspace for that server; home also shows servers no workspace claims."""
        if len(self.spaces) < 2:
            return True
        if self.cur.cfg.guild_id and str(guild_id) == self.cur.cfg.guild_id:
            return True
        return self.cur is self.home and str(guild_id) not in {ws.cfg.guild_id for ws in self.spaces}

    def _member_ids(self, guild: str) -> set[str] | None:
        """User ids in a server (REST, cached briefly); None when Discord will not list them."""
        now = self.clock()
        hit = self._member_ids_cache.get(guild)
        if hit and now - hit[0] < MEMBER_CACHE_S:
            return hit[1]
        ids: set[str] | None
        try:
            raw = self.client().request("GET", "/guilds/%s/members" % guild, {"limit": MEMBER_LIST_LIMIT})
            ids = {str((m.get("user") or {}).get("id", "")) for m in raw}
        except (ApiError, HearbackError):
            ids = None
        self._member_ids_cache[guild] = (now, ids)
        return ids

    def _dm_here(self, user_id: str) -> bool:
        """A DM shows on the workspace of each server the person is in; someone in none of them (or when Discord
        will not list members) shows on home."""
        if len(self.spaces) < 2:
            return True

        def member_of(ws: Workspace) -> bool:
            ids = self._member_ids(ws.cfg.guild_id) if ws.cfg.guild_id and user_id else None
            return bool(ids) and user_id in ids

        if member_of(self.cur):
            return True
        return self.cur is self.home and not any(member_of(ws) for ws in self.spaces if ws is not self.home)

    def _dm_peer(self, c: dict) -> dict:
        """A DM where only the bot has spoken holds no message of the person's; Discord still knows who the other
        end is (the channel's recipient). Filled into the conversation's user id and name; cached, a failure briefly."""
        if c["kind"] != "dm" or c.get("user_id"):
            return c
        now = self.clock()
        hit = self._dm_peers.get(c["id"])
        if hit is None or (hit[1] is None and now - hit[0] >= MEMBER_CACHE_S):
            try:
                ch = self.client().request("GET", "/channels/%s" % c["id"])
                u = (ch.get("recipients") or [{}])[0]
                hit = (now, {"user_id": str(u["id"]), "channel_name": u.get("global_name") or u.get("username") or ""})
            except (ApiError, HearbackError, KeyError, TypeError, AttributeError):
                hit = (now, None)
            self._dm_peers[c["id"]] = hit
        if hit[1]:
            c.update({k: v for k, v in hit[1].items() if v})
        return c

    def start_background(self) -> None:
        """Supporter retry loops of every workspace (REST only, whichever workspace the page has open)."""
        for ws in self.spaces:
            ws.supporter_bot.start()

    def stop_background(self) -> None:
        for ws in self.spaces:
            ws.supporter_bot.stop()

    # ---- plumbing
    def _token(self) -> str:
        t = self.home.cfg.token()
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
        env = (os.environ.get(self.home.cfg.token_env) or "").strip()
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

    def _ask(self, system: str, messages: list, max_tokens: int | None = None, prefer: str | None = None) -> tuple[str, str]:
        """Every model call of the console goes through here, so the page can say what they cost this month."""
        take_usage()
        text, pid = complete_any(self.llm, system, messages, max_tokens, prefer=prefer)
        self.usage.add(self._model_of(pid), take_usage())
        return text, pid

    def _llm_call(self, agent: dict, system: str, messages: list[dict]) -> str:
        return self._ask(system, messages, prefer=agent.get("profile_id") or None)[0]

    def _review_llm(self, system: str, messages: list, max_tokens: int, prefer: str | None) -> tuple[str, str]:
        return self._ask(system, messages, max_tokens, prefer)

    def _model_of(self, profile_id: str) -> str:
        try:
            return str(self.llm.get(profile_id).get("model") or profile_id)
        except LLMError:
            return profile_id

    def _send_auto(self, channel: str, text: str) -> list[str]:
        return actions.post(self.client(), self.home.ledger, channel, text)["message_ids"]

    def _bot_state(self, state: str, reason: str) -> None:
        """Gateway state changes go to the event log, so 'was the bot online?' has an answer later."""
        self.home.ledger.event("bot:swlink", "bot_state", None, {"state": state, "reason": reason[:200]} if reason else {"state": state})

    def on_message(self, msg: Message, bot_id: str) -> None:
        """Gateway callback (runs in a worker thread). A DM with an Afdian order number goes to the supporter flow
        (fixed template replies, no model) when that is switched on; everything else follows the channel rules."""
        if (msg.kind == "dm" and not msg.is_bot and msg.author_id != bot_id
                and (self._supporter_dm(msg.author_id, msg.channel_id, msg.content)
                     or self._codes_dm(msg.author_id, msg.channel_id, msg.content))):
            self.inbox.add(msg)      # kept like any DM: the chat shows it, and it names who the conversation is with
            return
        res = self.bot.handle(msg, bot_id)
        if res.get("action") == "error":
            self.home.ledger.event("bot:swlink", "error", msg.channel_id, {"error": scrub(res.get("error", ""))[:300]})

    def on_missed(self, msgs: list[Message], bot_id: str) -> None:
        """DMs that came in while SWLink was closed (read back on connect): kept so the conversations show; an
        Afdian order nobody answered still goes to the supporter flow, nothing else gets a late reply. Answered means
        a later bot message SWLink itself saw: what the bot said while SWLink was closed came from another program
        on the same token (a welcome, another server's flow) and answers nothing here."""
        orders = 0
        held = {(o.channel_id, o.id) for o in msgs if self.inbox.has(o.channel_id, o.id)}
        fresh = [m for m in msgs if (m.channel_id, m.id) not in held]
        for m in fresh:
            answered = any(o.channel_id == m.channel_id and (o.is_bot or o.author_id == bot_id)
                           and (o.channel_id, o.id) in held
                           and o.id.isdigit() and m.id.isdigit() and int(o.id) > int(m.id) for o in msgs)
            if (not answered and not m.is_bot and m.author_id != bot_id
                    and (self._supporter_dm(m.author_id, m.channel_id, m.content)
                         or self._codes_dm(m.author_id, m.channel_id, m.content))):
                orders += 1
            self.bot.inbox.add(m)
        if fresh:
            self.home.ledger.event("bot:swlink", "dm_catchup", None, {"messages": len(fresh), "orders": orders})

    def _supporter_dm(self, author_id: str, channel_id: str, content: str) -> bool:
        """An order-number DM goes to one workspace whose supporter DM handling is on: with several, the first whose
        server the person is in and whose own rules would take the order (still lacking that role first), else the
        first they are in, else the first. An order already claimed in another workspace is refused (one order, one
        server)."""
        live = [ws for ws in self.spaces if ws.supporter_bot.dm_on()]
        nums = extract_order_numbers(content)
        if not live or not nums:
            return False
        ws = live[0] if len(live) == 1 else self._order_space(live, str(author_id), nums[0])
        others = [(o.cfg.root.name, o.supporters_store) for o in self.spaces if o is not ws] + self.peers
        for name, store in others:
            try:
                used = nums[0] in store.load()["claims"]
            except SupporterError:
                used = False
            if used:
                return self._claimed_elsewhere(ws, name, str(author_id), channel_id, nums[0])
        if self.peers and self._try_other(ws, str(author_id), channel_id, nums[0]):
            return True
        return ws.supporter_bot.handle_dm(author_id, channel_id, content)

    def _try_other(self, ws: Workspace, uid: str, channel_id: str, no: str) -> bool:
        """An order below this server's minimum that another bot's server would take ([swlink] others): point the
        person there instead of turning them away. True when that was said."""
        p = self._member_in(ws, uid)
        if p is None:
            return False
        try:
            out = ws.supporters.handle_order(p, no, dry_run=True)
        except HearbackError:
            return False
        if out.reply_key != "below_min":
            return False
        for name, store in self.peers:
            try:
                cfg = store.load()
                takes = bool(cfg["enabled"]) and parse_amount(out.amount) >= parse_amount(cfg["min_amount"])
            except (SupporterError, KeyError):
                takes = False
            if not takes:
                continue
            try:
                ws.supporter_bot.say(self._client_factory(), channel_id, ws.supporters.text("try_other", server=name))
            except HearbackError as e:
                ws.supporters.event("bot:swlink", "supporter_error", {"error": scrub(e)[:300]})
            ws.supporters.event("bot:swlink", "supporter_try_other", {"order": mask(no), "user": uid, "server": name})
            return True
        return False

    def on_join(self, guild_id: str, uid: str, is_bot: bool, owner_id: str) -> None:
        """Someone joined a server (or was found on connect): the workspace for that server sends its welcome once."""
        for ws in self.spaces:
            if ws.welcome.enabled and ws.cfg.guild_id and str(guild_id) == ws.cfg.guild_id:
                def send(user: str, text: str) -> None:
                    client = self._client_factory()
                    ws.supporter_bot.say(client, ws.supporter_bot.dm_channel(client, user), text)
                ws.welcome.greet(send, uid, is_bot=is_bot, owner_id=owner_id)

    def _member_in(self, ws: Workspace, uid: str) -> Person | None:
        guild = self._supporter_guild(ws)
        if not guild:
            return None
        try:
            return ws.supporter_bot.member(self._client_factory(), guild, uid)
        except HearbackError:
            return None

    def _order_space(self, live: list[Workspace], uid: str, no: str) -> Workspace:
        """Servers the person is in, those still lacking the role first, then the ones they are not in; the first
        whose dry run takes the order (amount, required roles: one server's minimum must not turn away an order
        another would take). One they are not in answers "join first". None takes it: the first they are in."""
        need, have, away = [], [], []
        for ws in live:
            p = self._member_in(ws, uid)
            if p is None:
                away.append((ws, Person(id=uid)))
                continue
            try:
                role = str(ws.supporters_store.load()["role_id"])
            except SupporterError:
                role = ""
            (need if role and role not in {str(r) for r in p.role_ids} else have).append((ws, p))
        for ws, p in need + have + away:
            try:
                if ws.supporters.handle_order(p, no, dry_run=True).reply_key in ("ok", "ok_again"):
                    return ws
            except HearbackError:
                continue
        ranked = need + have
        return ranked[0][0] if ranked else live[0]

    def _claimed_elsewhere(self, ws: Workspace, other: str, uid: str, channel_id: str, no: str) -> bool:
        sup = ws.supporters
        p = self._member_in(ws, uid) or Person(id=uid)
        try:
            client = self._client_factory()
            ws.supporter_bot.say(client, channel_id, sup.text("claimed_elsewhere"))
            ws.supporter_bot.notify(client, [sup.text("note_elsewhere", who=sup.who(p), order=mask(no),
                                                       server=other)])
        except HearbackError as e:
            sup.event("bot:swlink", "supporter_error", {"error": scrub(e)[:300]})
        sup.event("bot:swlink", "supporter_elsewhere", {"order": mask(no), "user": uid})
        return True

    def _codes_dm(self, author_id: str, channel_id: str, content: str) -> bool:
        """A DM holding a device code, for a workspace with [codes] on: the member with its role gets the code from
        the owner's command. With several such workspaces, the first whose server the person is in. With several
        servers, codes from people without the role who belong elsewhere are left alone (False)."""
        live = [ws for ws in self.spaces if ws.codes.enabled]
        dev = next((d for d in (ws.codes.find(content) for ws in live) if d), "")
        if not dev:
            return False
        uid = str(author_id)
        pick = None
        for ws in live:
            p = self._member_in(ws, uid)
            if p is not None and (pick is None or ws.codes.role_id in {str(r) for r in p.role_ids}):
                pick = (ws, p)
                if ws.codes.role_id in {str(r) for r in p.role_ids}:
                    break
        if len(self.spaces) > 1 and not (pick and pick[0].codes.role_id in {str(r) for r in pick[1].role_ids}):
            # several servers on one bot: a device code from someone outside the codes servers, or without the role
            # there while in another server, is likely for that other server's own program; say nothing
            if pick is None or any(self._member_in(ws, uid) is not None for ws in self.spaces if not ws.codes.enabled):
                return False
        try:
            client = self._client_factory()
            if pick is None:
                live[0].supporter_bot.say(client, channel_id, live[0].codes.text("not_member"))
                return True
            ws, p = pick
            reply, code, notes = ws.codes.handle(p, dev)
            ws.supporter_bot.say(client, channel_id, reply)
            if code:      # on its own: a long-press copy on a phone takes the whole message, and apps want just the code
                ws.supporter_bot.say(client, channel_id, code)
            ws.supporter_bot.notify(client, notes)
        except HearbackError as e:
            (pick[0] if pick else live[0]).ledger.event("bot:swlink", "code_error", None, {"error": scrub(e)[:300]})
        return True

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
        add("GET", "/api/feedback", self.api_feedback)
        add("POST", "/api/feedback/sync", self.api_feedback_sync)
        add("POST", "/api/feedback/review", self.api_feedback_review)
        add("POST", "/api/feedback/handoff", self.api_feedback_handoff)
        add("PUT", "/api/feedback/forums/(\\d+)", self.api_feedback_forum)
        add("GET", "/api/feedback/(\\d+)", self.api_feedback_thread)
        add("GET", "/api/feedback/(\\d+)/file/(\\d+)", self.api_feedback_file)
        add("POST", "/api/feedback/(\\d+)/ai", self.api_feedback_ai)
        add("POST", "/api/feedback/(\\d+)/send", self.api_feedback_send)
        add("POST", "/api/feedback/(\\d+)/mark", self.api_feedback_mark)
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
        add("GET", "/api/workspaces", self.api_workspaces)
        add("POST", "/api/workspaces/(\\d+)/open", self.api_workspace_open)
        add("POST", "/api/others/(\\d+)/open", self.api_other_open)
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
            "workspaces": len(self.spaces) + len(self.broken) + len(self.others),
            "checklist": {"token": tok["set"], "llm": llm_ready, "rule": rule_on},
            "server": next((str(g.get("name") or "") for g in guilds if str(g["id"]) == self.cfg.guild_id), "") or self.cfg.root.name,
            "inbox": self._inbox_counts(), "chat": bool(self.inbox.conversations()),
            "waiting": sum(len(self.queue.list(st)) for st in ("pending", "approved", "failed"))}

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
            if not self._here(g["id"]):
                continue
            for c in g["channels"]:
                items[c["id"]] = {"id": c["id"], "kind": "guild", "guild_id": g["id"], "guild_name": g["name"],
                                  "channel_name": c["name"], "preview": "", "last_ts": ""}
                if live:       # what the bot member may do there; only the gateway knows (None = unknown = fine)
                    items[c["id"]].update(readable=c.get("readable") is not False,
                                          can_send=c.get("can_send") is not False)
        for c in map(self._dm_peer, self.inbox.conversations()):
            if not (self._dm_here(c.get("user_id", "")) if c["kind"] == "dm" else self._here(c["guild_id"])):
                continue
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
                          "masked": _masked(m.get("content") or ""),
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
        text, pid = self._ask(system, messages, prefer=agent.get("profile_id") or None)
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
        out, pid = self._ask(system, messages, prefer=prefer)
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
        return 200, {"items": [g for g in guilds if self._here(g["id"])], "reason": reason}

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
            "needs_review": review.needs_review(rec), "forum_tags": review.forum_tags(data, tid),
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
        known = {t.lower(): t for t in review.forum_tags(self.ledger.load(), tid)}
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

    # ---- the feedback inbox: every forum of the server in one list, a person answers in place
    def _forum_rows(self, data: dict) -> list[dict]:
        """The forums to show, in Discord's order. Before the first pull: the configured forum, unnamed."""
        hidden = self.cur.forums.hidden()
        known = data["forums"] or ({self.cfg.forum_id: {"name": "", "position": 0, "tags": data["tags"]}}
                                   if self.cfg.forum_id else {})
        rows = sorted(known.items(), key=lambda kv: (kv[1].get("position", 0), int(kv[0])))
        return [{"id": f, "name": v.get("name", ""), "tags": list((v.get("tags") or {}).values()),
                 "agent": self._forum_agent_id(f), "hidden": f in hidden} for f, v in rows]

    def _forum_agent_id(self, forum: str) -> str:
        """A forum's own choice, else the bug desk for the configured bug forum, else the general agent."""
        chosen = str(self.cur.forums.get(forum).get("agent") or "")
        if chosen:
            return chosen
        return review.DEFAULT_AGENT if forum == self.cfg.forum_id else "auto"

    def _home_forum(self, data: dict, rec: dict) -> str:
        return str(rec.get("forum_id") or data.get("forum_id") or self.cfg.forum_id or "")

    def _inbox_rows(self, data: dict) -> list[dict]:
        hidden = self.cur.forums.hidden()
        staff = feedback.staff_ids(data, self.cfg.approvers)
        out = []
        for tid, rec in data["threads"].items():
            forum = self._home_forum(data, rec)
            state = feedback.inbox_state(tid, rec, staff)
            if state and forum not in hidden and (not data["forums"] or forum in data["forums"]):
                out.append(feedback.row(tid, rec, state, staff, forum, self.cfg.status_tags))
        out.sort(key=lambda r: r["time"] or "", reverse=True)
        return out

    def _inbox_counts(self) -> dict:
        data = self.ledger.load()
        todo = [r for r in self._inbox_rows(data) if r["state"] == "todo"]
        return {"todo": len(todo), "forums": [{"id": f["id"], "name": f["name"], "todo": sum(r["forum"] == f["id"] for r in todo)}
                                              for f in self._forum_rows(data) if not f["hidden"]]}

    def _spend(self) -> dict:
        m = self.usage.this_month()
        return {"calls": m["calls"], "cost": m["cost"], "complete": not m["unpriced"]}

    def _active_model(self) -> tuple[str, str]:
        active = self.llm.active_public()
        return (active["name"], active["model"]) if active else ("", "")

    def api_feedback(self, q: Request) -> tuple[int, Any]:
        data = self.ledger.load()
        today = time.strftime("%Y-%m-%d", time.gmtime(self.clock()))
        handled = sum(1 for e in self.ledger.read_events(1000)
                      if e.get("action") == "feedback_done" and str(e.get("t") or "").startswith(today))
        name, model = self._active_model()
        return 200, {"forums": self._forum_rows(data), "items": self._inbox_rows(data), "synced_at": data.get("synced_at", ""),
                     "handled_today": handled, "spend": self._spend(), "model": name, "per_post": feedback.estimate(model),
                     "can_pull": bool(self.cfg.guild_id or self.cfg.forum_id or data.get("guild_id"))}

    def api_feedback_sync(self, q: Request) -> tuple[int, Any]:
        ws = self.cur
        if not ws.sync_lock.acquire(blocking=False):
            raise ApiError(409, "pull_busy")
        try:
            delta = sync_all(self.client(), ws.ledger, ws.cfg, download=True, actor=ACTOR, skip=ws.forums.hidden())
        finally:
            ws.sync_lock.release()
        self.directory()             # the server's name for the sidebar, also while the bot is offline
        return 200, {"new": len(delta.new_threads), "replies": len(delta.with_replies),
                     "warnings": [scrub(w) for w in delta.warnings]}

    def _feedback_thread(self, ident: str) -> tuple[str, dict, dict]:
        data = self.ledger.load()
        rec = data["threads"].get(ident)
        if rec is None:
            raise ApiError(404, "no such post")
        return ident, rec, data

    @staticmethod
    def _files(rec: dict) -> list[str]:
        """Attachment entries of a post, the post's own first. The page refers to them by position."""
        return [str(a) for a in rec.get("attachments") or []] + [str(a) for r in rec.get("replies") or []
                                                                 for a in r.get("attachments") or []]

    def api_feedback_thread(self, q: Request) -> tuple[int, Any]:
        tid, rec, data = self._feedback_thread(q.groups[0])
        tri = rec.get("triage") or {}
        forum = self._home_forum(data, rec)
        files = self._files(rec)
        spot = {"n": 0}

        def views(entries: Any) -> list[dict]:
            out = []
            for a in entries or []:
                out.append({"i": spot["n"], "name": os.path.basename(str(a)).split("_", 1)[-1], "image": is_image(str(a))})
                spot["n"] += 1
            return out

        post_files = views(rec.get("attachments"))
        seen, messages = set(), []
        posted = {str(p.get("message_id")) for p in tri.get("replies_posted") or []}
        staff = feedback.staff_ids(data, self.cfg.approvers)
        for r in rec.get("replies") or []:
            seen.add(str(r.get("id")))
            messages.append({"id": str(r.get("id")), "author": r.get("author", ""), "time": r.get("time", ""),
                             "content": str(r.get("content") or "")[:4000], "files": views(r.get("attachments")),
                             "bot": bool(r.get("bot")) or str(r.get("id")) in posted,
                             "staff": str(r.get("author_id") or "") in staff, "masked": _masked(r.get("content") or "")})
        for p in tri.get("replies_posted") or []:      # sent from here, not pulled back yet
            if str(p.get("message_id")) not in seen:
                messages.append({"id": str(p.get("message_id")), "author": "", "time": p.get("time", ""),
                                 "content": str(p.get("content") or "")[:4000], "files": [], "bot": True, "staff": True,
                                 "masked": _masked(p.get("content") or "")})
        messages.sort(key=lambda m: int(m["id"]) if m["id"].isdigit() else 0)
        paths = review.thread_files(self.ledger, rec)
        ai = tri.get("ai") if isinstance(tri.get("ai"), dict) else None
        guild = str(data.get("guild_id") or self.cfg.guild_id or "")
        return 200, {"id": tid, "forum": forum, "title": rec.get("title", ""), "author": rec.get("author", ""),
                     "created": rec.get("created", ""), "body": str(rec.get("body") or "")[:8000], "files": post_files,
                     "messages": messages[-DETAIL_REPLIES:], "more": max(0, len(messages) - DETAIL_REPLIES), "tags": list(rec.get("tags") or []),
                     "forum_tags": review.forum_tags(data, tid), "agent": self._forum_agent_id(forum),
                     "state": feedback.inbox_state(tid, rec, staff) or "done", "has_files": len(files),
                     "log": review.log_excerpt(paths), "log_errors": len(review.error_lines(paths)),
                     "ai": ai, "ai_stale": review.ai_is_stale(rec),
                     "link": "https://discord.com/channels/%s/%s" % (guild, tid) if guild else ""}

    def api_feedback_file(self, q: Request) -> tuple[int, Any]:
        """One attachment for the page: a picture as a data: link (the only kind of link the page may show from here)."""
        _tid, rec, _data = self._feedback_thread(q.groups[0])
        files = self._files(rec)
        i = int(q.groups[1])
        if i >= len(files):
            raise ApiError(404, "no such file")
        where = self.ledger.abs_path(files[i])
        if not isinstance(where, Path):
            return 200, {"url": where}
        path = where.resolve()
        if self.ledger.dir.resolve() not in path.parents or not path.is_file():
            raise ApiError(404, "no such file")
        if path.stat().st_size > FILE_MAX:
            raise ApiError(413, "file_too_big")
        raw = path.read_bytes()
        kind = review.sniff_image(raw[:16])
        if not kind:
            raise ApiError(415, "not a picture")
        return 200, {"data": "data:%s;base64,%s" % (kind, base64.b64encode(raw).decode("ascii"))}

    def _agent_for_post(self, tid: str) -> dict:
        data = self.ledger.load()
        aid = self._forum_agent_id(self._home_forum(data, data["threads"][tid]))
        if aid == feedback.NO_AGENT:
            raise ApiError(400, "forum_no_ai")
        return self.agents.get_or_auto(aid)

    def api_feedback_ai(self, q: Request) -> tuple[int, Any]:
        """The AI looks at one post because a person pressed the button. It only writes a draft; nothing is sent."""
        tid, _rec, _data = self._feedback_thread(q.groups[0])
        agent = self._agent_for_post(tid)
        if not self.llm.candidates(agent.get("profile_id") or None):
            raise ApiError(400, "no_model")
        res, pid = review.review_thread(self.ledger, self._review_llm, tid, system=review.system_prompt(agent),
                                        prefer=agent.get("profile_id") or None)
        ai = review.save_ai_result(self.ledger, tid, res, model=self._model_of(pid), profile=pid)
        return 200, {"ai": ai, "agent": agent["name"], "spend": self._spend()}

    def api_feedback_review(self, q: Request) -> tuple[int, Any]:
        """Drafts for every waiting post without one (in one forum, or all). Only on request, and only drafts."""
        data = self.ledger.load()
        forum = str(q.body.get("forum") or "")
        ids = [r["id"] for r in self._inbox_rows(data)
               if r["state"] == "todo" and not r["has_draft"] and (not forum or r["forum"] == forum)
               and self._forum_agent_id(r["forum"]) != feedback.NO_AGENT][:BULK_MAX]
        if not ids:
            raise ApiError(400, "nothing_to_review")
        if not self.llm.candidates(None):
            raise ApiError(400, "no_model")
        try:
            job = self.review.start(ids, agent_of=self._agent_for_post)
        except review.ReviewBusy as e:
            raise ApiError(409, str(e)) from None
        self.ledger.event(ACTOR, "ai_review_start", None, {"threads": len(ids)})
        return 200, {"job": job, "total": len(ids)}

    def api_feedback_handoff(self, q: Request) -> tuple[int, Any]:
        """The chosen posts as one text to paste into a coding assistant. Reads only; the caller copies it."""
        data = self.ledger.load()
        asked = q.body.get("ids")
        if not isinstance(asked, list) or not asked:
            raise ApiError(400, "pick at least one post")
        ids = list(dict.fromkeys(str(x) for x in asked))
        if any(x not in data["threads"] for x in ids):
            raise ApiError(404, "no such post")
        picked = ids[:feedback.HANDOFF_MAX]
        guild = str(data.get("guild_id") or self.cfg.guild_id or "")
        entry = self.cfg.swlink_project_dir
        project = str(Path(entry) if Path(entry).is_absolute() else (self.cfg.root / entry).resolve()) if entry else ""
        text = feedback.handoff_text(self.ledger, data, picked, project=project, zh=self.cfg.language == "zh",
                                     forum_names={r["id"]: r["name"] for r in self._forum_rows(data)},
                                     staff=feedback.staff_ids(data, self.cfg.approvers),
                                     link=lambda tid: "https://discord.com/channels/%s/%s" % (guild, tid) if guild else "")
        return 200, {"text": text, "count": len(picked), "left": len(ids) - len(picked)}

    def api_feedback_send(self, q: Request) -> tuple[int, Any]:
        """A person pressed Send: that is the approval, so the reply and the tags go out at once (through the queue,
        which keeps its record and checks what reached Discord)."""
        tid, rec, data = self._feedback_thread(q.groups[0])
        text = str(q.body.get("text") or "").strip()
        add, remove = _names(q.body.get("add")), _names(q.body.get("remove"))
        if not text and not add and not remove:
            raise ApiError(400, "write a reply or pick a tag first")
        if len(text) > SEND_LIMIT:
            raise ApiError(400, "the message is over %d characters" % SEND_LIMIT)
        known = {t.lower(): t for t in review.forum_tags(data, tid)}
        bad = [n for n in add + remove if n.lower() not in known]
        if bad:
            raise ApiError(400, "no forum tag named %s. Available: %s" % (", ".join(map(repr, bad)), ", ".join(known.values())))
        add, remove = [known[n.lower()] for n in add], [known[n.lower()] for n in remove]
        made = []
        if add or remove:
            made.append(self.queue.propose("tags", actor=ACTOR, thread=tid, add=add, remove=remove, why=FEEDBACK_WHY).id)
        if text:
            made.append(self.queue.propose("reply", actor=ACTOR, thread=tid, text=text, why=FEEDBACK_WHY, force=True).id)
        self.queue.approve(made, ACTOR)
        rep = self.queue.send(self.client(), self.ledger, forum_id=self._home_forum(data, rec), delay=self.cfg.send_delay,
                              actor=ACTOR, ids=made)
        if rep.failed:
            clean = [p.id for p in rep.failed if not p.result.get("message_ids")]
            if clean:                 # nothing went out and the words are still in the box: no leftovers in Waiting
                self.queue.reject(clean, ACTOR, "send failed; still in the reply box")
            raise ApiError(502, scrub(rep.failed[0].error)[:300] or "send failed")
        statuses = {v: k for k, v in self.cfg.status_tags.items()}
        patch = next(({"status": statuses[t]} for t in reversed(add) if t in statuses), None)
        if patch:                     # the release flow reads the status, so a status tag keeps it in step
            self.ledger.update_triage(tid, patch, actor=ACTOR)
        feedback.set_mark(self.ledger, tid, None)
        self.ledger.event(ACTOR, "feedback_done", tid, {"reply": bool(text), "tags": len(add) + len(remove)})
        return 200, {"sent": [p.id for p in rep.sent], "mismatches": rep.mismatches}

    def api_feedback_mark(self, q: Request) -> tuple[int, Any]:
        tid, rec, data = self._feedback_thread(q.groups[0])
        state = str(q.body.get("state") or "")
        if state not in ("done", "open", "auto"):
            raise ApiError(400, "state must be done, open or auto")
        feedback.set_mark(self.ledger, tid, None if state == "auto" else state)
        if state == "done":
            self.ledger.event(ACTOR, "feedback_done", tid, {"skipped": True})
        rec = self.ledger.load()["threads"][tid]
        return 200, {"state": feedback.inbox_state(tid, rec, feedback.staff_ids(data, self.cfg.approvers))}

    def api_feedback_forum(self, q: Request) -> tuple[int, Any]:
        fields: dict[str, Any] = {}
        if "agent" in q.body:
            agent = str(q.body["agent"] or "")
            if agent != feedback.NO_AGENT:
                self.agents.get(agent)
            fields["agent"] = agent
        if "hidden" in q.body:
            fields["hidden"] = bool(q.body["hidden"])
        if not fields:
            raise ApiError(400, "send agent and/or hidden")
        self.cur.forums.set(q.groups[0], **fields)
        return 200, {"forums": self._forum_rows(self.ledger.load())}

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
                if not self._here(g["id"]):
                    continue
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
               for c in map(self._dm_peer, self.inbox.conversations())
               if not c["guild_id"] and self._dm_here(c.get("user_id", ""))]
        return 200, {"threads": threads, "dms": dms, "reason": reason}

    def api_botmsgs(self, q: Request) -> tuple[int, Any]:
        channel, focus = editor.parse_target(q.query.get("channel"))
        if not channel:
            raise ApiError(400, "pick a place, or paste a Discord channel or message link")
        msgs = actions.list_bot_messages(self.client(), channel, self.bot_id(), 100)
        items = [{"id": str(m["id"]), "content": m.get("content") or "", "time": m.get("timestamp") or "",
                  "edited": bool(m.get("edited_timestamp")),
                  "masked": _masked(m.get("content") or ""),
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
        if _masked(msg.get("content") or ""):    # the page only has *** to offer; saving would replace the real text
            raise ApiError(400, "masked_message")
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
    def _supporter_guild(self, ws: Workspace | None = None) -> str:
        """The server the supporter role lives in: the workspace's configured one, else the only one the bot is in."""
        ws = ws or self.cur
        if ws.cfg.guild_id:
            return ws.cfg.guild_id
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

    # ---- workspaces
    def api_workspaces(self, q: Request) -> tuple[int, Any]:
        known = self.directory()[0] if self.token_state()["set"] else []     # live guilds, else REST cached 60 s
        names = {str(g["id"]): str(g.get("name") or "") for g in known}
        items = [{"id": str(i), "name": names.get(ws.cfg.guild_id) or ws.cfg.root.name, "folder": ws.cfg.root.name,
                  "guild_id": ws.cfg.guild_id, "forum_id": ws.cfg.forum_id, "home": ws is self.home,
                  "current": ws is self.cur} for i, ws in enumerate(self.spaces)]
        own = names.get(self.home.cfg.guild_id) or ""
        if own:
            self._tell_name(own)
        for i, d in enumerate(self.others):
            try:
                name = (d / DATA_DIR / "swlink.name").read_text(encoding="utf-8").strip()
            except OSError:
                name = ""
            items.append({"id": "o%d" % i, "name": name or d.name, "folder": d.name, "guild_id": "", "forum_id": "",
                          "home": False, "current": False, "other": True, "running": folder_running(d)})
        return 200, {"items": items, "broken": list(self.broken)}

    def _tell_name(self, name: str) -> None:
        """This server's name, for the picker of SWLinks that list this folder in [swlink] others."""
        f = self.home.cfg.data_dir / "swlink.name"
        try:
            if f.read_text(encoding="utf-8").strip() == name:
                return
        except OSError:
            pass
        try:
            atomic_write_text(f, name + "\n")
        except OSError:
            pass

    def api_other_open(self, q: Request) -> tuple[int, Any]:
        """Another bot's SWLink: its link when it runs (the page goes there), else start it ({starting}: ask again)."""
        i = int(q.groups[0])
        if i >= len(self.others):
            raise ApiError(404, "no such workspace")
        d = self.others[i]
        if not folder_running(d):
            try:
                self._start_other(d)
            except OSError as e:
                raise ApiError(500, "could not start SWLink in %s: %s" % (d.name, scrub(str(e))[:200])) from None
            return 200, {"starting": True}
        try:
            url = (d / DATA_DIR / "swlink.url").read_text(encoding="utf-8").strip()
        except OSError:
            url = ""
        return 200, {"url": url} if url.startswith("http://127.0.0.1:") else {"starting": True}

    def api_workspace_open(self, q: Request) -> tuple[int, Any]:
        i = int(q.groups[0])
        if i >= len(self.spaces):
            raise ApiError(404, "no such workspace")
        if self.spaces[i] is not self.cur:
            self.cur = self.spaces[i]
            self._members_cache = None       # member lists belong to one server
            self._member_ids_cache.clear()
            self.ledger.event(ACTOR, "workspace_open", None, {"folder": self.cfg.root.name})
        return self.api_workspaces(q)

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
                     "guild_id": self.cfg.guild_id, "forum_id": self.cfg.forum_id,
                     "welcome_file": str(self.cfg.root / self.cfg.welcome["file"]) if (self.cfg.welcome or {}).get("enabled") else ""}

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
        path = self.home.cfg.token_path      # the bot's token: the home workspace runs the connection
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
    app.start_background()
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
        app.stop_background()
        app.runtime.stop()
        srv.server_close()
    return 0


def hold_folder(cfg: Config) -> Any | None:
    """One SWLink per folder: an exclusive lock on `<data>/swlink.lock`, kept while serving. None when another
    SWLink holds it (a second one would bind another port, connect the same bot and answer every DM again)."""
    ensure_data_dir(cfg.data_dir)
    fh = open(cfg.data_dir / "swlink.lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def other_folders(cfg: Config) -> list[Path]:
    """`[swlink] others`: SWLink folders that run other bots (paths relative to this config)."""
    out = []
    for entry in cfg.swlink_others:
        d = Path(entry)
        d = (d if d.is_absolute() else cfg.root / d).resolve()
        out.append(d.parent if d.name == CONFIG_NAME else d)
    return out


def folder_running(folder: Path) -> bool:
    """Whether a SWLink holds that folder's lock (see hold_folder) right now."""
    path = folder / DATA_DIR / "swlink.lock"
    if not path.exists():
        return False
    try:
        fh = open(path, "a+")
    except OSError:
        return False
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        return True
    finally:
        fh.close()
    return False


def start_folder(folder: Path) -> None:
    """Start SWLink for another folder in its own minimized window (closing that window stops it), no browser."""
    kw: dict[str, Any] = {}
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7                                    # SW_SHOWMINNOACTIVE
        kw = {"creationflags": subprocess.CREATE_NEW_CONSOLE, "startupinfo": si}
    else:
        kw = {"start_new_session": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    subprocess.Popen([sys.executable, "-m", "hearback", "web", "--no-browser"], cwd=str(folder), **kw)


def run(cfg: Config, port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    lock = hold_folder(cfg)
    url_file = cfg.data_dir / "swlink.url"           # the running one's link, so a second start can open it
    if lock is None:
        try:
            url = url_file.read_text(encoding="utf-8").strip()
        except OSError:
            url = ""
        print("SWLink is already running for this folder%s" % (": " + url if url else "."), flush=True)
        if open_browser and url:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        return 0
    try:
        url_file.unlink(missing_ok=True)             # until the new link is written, the old one must not be handed out
        srv = create_server(cfg, port)
        atomic_write_text(url_file, "http://127.0.0.1:%d/#k=%s\n" % (srv.app.port, srv.app.secret))
        for d in srv.app.others:                     # the other bots' SWLinks come up too, so the picker can switch
            if (d / CONFIG_NAME).is_file() and not folder_running(d):
                try:
                    srv.app._start_other(d)
                except OSError as e:
                    print("could not start SWLink in %s: %s" % (d.name, e), flush=True)
        return serve(srv, cfg, open_browser)
    finally:
        lock.close()


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
