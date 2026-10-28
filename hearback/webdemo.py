"""`hearback web --demo`: the SWLink console on entirely fake data, safe for screenshots and screen recordings.

Everything lives in a temporary folder that is removed on exit: a fake Discord (`FakeDiscord`), a fake Afdian, an
in-memory keyring and a fake model. While it runs, `urllib.request.urlopen` and every non-loopback socket connection
raise, and the page refuses to save tokens or keys. Nothing is read from or written to the user's real workspace.
"""
from __future__ import annotations

import contextlib
import json
import random
import re
import socket
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from hearback import llm, review
from hearback.agents import AgentStore
from hearback.chatbot import POLISH_SYSTEM, Inbox, RuleStore, message_from_rest
from hearback.config import Config, Variant
from hearback.fake import START_MS, FakeAfdian, FakeDiscord, make_png
from hearback.ledger import Ledger
from hearback.llm import LLMStore, complete_any
from hearback.queue import Queue
from hearback.supporters import SupporterStore, Supporters
from hearback.sync import sync
from hearback.util import HearbackError, atomic_write_text, ensure_data_dir
from hearback.web import server
from hearback.webdemo_seed import (AI_APPLIED, BOT_NAME, CHANNELS, CHAT, DRAFT_DEFAULT, DRAFTS, FAKE_AFDIAN_TOKEN,
                                   FAKE_AFDIAN_USER, FAKE_BOT_TOKEN, FAKE_MODEL_KEY, FORUM, GUILD, HUMAN_TRIAGE, LANGS,
                                   MEMBERS, MODEL_NAME, ORDERS, PLANS, QUEUE_APPROVED, QUEUE_FAILED,
                                   QUEUE_PENDING_REPLY, QUEUE_PENDING_TAGS, REVIEWS, ROLES, STATUS_TAGS, TAGS, THREADS,
                                   VARIANTS, pick)

LOOPBACK = ("127.0.0.1", "localhost", "::1")
BLOCKED = "demo mode: network access is blocked"
DAY = 86400
CJK = re.compile("[㐀-鿿]")


# ---------------------------------------------------------------- guards and fakes

@contextlib.contextmanager
def block_network() -> Iterator[None]:
    """Make `urlopen`, `OpenerDirector.open` and non-loopback `socket.create_connection` raise until the block ends."""
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise HearbackError(BLOCKED)

    real_connect = socket.create_connection

    def connect(address: Any, *a: Any, **k: Any) -> Any:
        if str(address[0]) not in LOOPBACK:
            raise HearbackError(BLOCKED)
        return real_connect(address, *a, **k)

    saved = (urllib.request.urlopen, urllib.request.OpenerDirector.open, socket.create_connection)
    urllib.request.urlopen, urllib.request.OpenerDirector.open, socket.create_connection = refuse, refuse, connect  # type: ignore[method-assign,assignment]
    try:
        yield
    finally:
        urllib.request.urlopen, urllib.request.OpenerDirector.open, socket.create_connection = saved  # type: ignore[method-assign,assignment]


class MemoryKeyring:
    """A keyring that lives in memory only (the `keyring` module's three calls)."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, user: str) -> str | None:
        return self.store.get((service, user))

    def set_password(self, service: str, user: str, pw: str) -> None:
        self.store[(service, user)] = pw

    def delete_password(self, service: str, user: str) -> None:
        self.store.pop((service, user), None)


class DemoRuntime:
    """Stands in for the gateway: Start makes the bot "online" and shows the fake server, nothing connects."""

    def __init__(self, fake: FakeDiscord):
        self.fake = fake
        self.state = "offline"

    online = property(lambda self: self.state == "online")

    def status(self) -> dict:
        up = self.online
        return {"state": self.state, "reason": "", "bot_id": self.fake.bot["id"] if up else "",
                "bot_name": str(self.fake.bot["global_name"]) if up else ""}

    def start(self) -> dict:
        self.state = "online"
        return self.status()

    def stop(self) -> dict:
        self.state = "offline"
        return self.status()

    def guilds(self) -> list[dict]:
        chans = [{"id": c["id"], "name": c["name"], "readable": True, "can_send": True}
                 for c in self.fake.channels.values() if c["type"] == 0]
        return [{"id": self.fake.guild_id, "name": self.fake.guild_name, "channels": chans}]


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(str(b.get("text", "")) for b in content if b.get("type") != "image")


class DemoProvider(llm.Provider):
    """The fake model: a connectivity answer, a canned review per thread title, and chat drafts picked by keywords."""

    def __init__(self, lang: str, reviews: Mapping[str, dict]):
        self.lang, self.reviews = lang, reviews        # reviews: thread title -> the JSON object the model "wrote"

    def complete(self, system: str, messages: Sequence[Mapping[str, Any]], max_tokens: int) -> str:
        text = _text_of(messages[-1]["content"])
        if system.startswith("You are a connectivity test"):
            return "OK"
        if system == POLISH_SYSTEM:
            return self._polish(text)
        if "OUTPUT CONTRACT" in system:
            m = re.search(r"^Title: (.*)$", text, re.M)
            return json.dumps(self.reviews.get(m.group(1).strip() if m else "") or self._unknown(), ensure_ascii=False)
        return self._draft(text)

    def _unknown(self) -> dict:
        zh = self.lang == "zh"
        return {"summary": "报告里的信息不够，判断不了。" if zh else "The report has too little detail to judge.",
                "cluster": "unsorted", "severity": "low", "suggest_status": "need-info", "confidence": 0.2,
                "duplicate_of": "", "likely_cause": "", "where_to_look": [],
                "need_info": "设备、版本和步骤。" if zh else "Device, build and steps.",
                "reply_draft": "谢谢反馈！能补充设备、版本和步骤吗？" if zh else "Thanks! Could you add device, build and steps?",
                "tags_add": [], "tags_remove": []}

    @staticmethod
    def _polish(prompt: str) -> str:
        """Tidy the draft between the markers: spacing, a capital letter, end punctuation."""
        body = prompt.split("<<<DRAFT>>>", 1)[-1].split("<<<END DRAFT>>>", 1)[0]
        lines = [" ".join(ln.split()) for ln in body.strip().splitlines()]
        out = []
        for ln in lines:
            if ln and ln[0].isascii() and ln[0].isalpha():
                ln = ln[0].upper() + ln[1:]
            if ln and ln[-1] not in ".!?。！？：:)）":
                ln += "。" if any("一" <= ch <= "鿿" for ch in ln) else "."
            out.append(ln)
        return "\n".join(out)

    def _draft(self, prompt: str) -> str:
        block = prompt.split("NEW message to answer:")[-1]
        new = "\n".join(ln for ln in block.splitlines() if not ln.startswith("<<<"))
        low = new.lower()
        lang = "zh" if CJK.search(new) else "en"
        for words, reply in DRAFTS:
            if any(w in low for w in words):
                return pick(reply, lang)
        return pick(DRAFT_DEFAULT, lang)


# ---------------------------------------------------------------- the workspace

class Demo:
    """A fake-data workspace in a temporary folder. `with Demo("en") as d:` builds it; leaving the block removes it.

    `d.cfg` and `d.app_kw` plug into `hearback.web.server.create_server`; `d.server(port)` does that for you.
    """

    def __init__(self, lang: str = "en"):
        if lang not in LANGS:
            raise HearbackError("lang must be one of: %s" % ", ".join(LANGS))
        self.lang = lang
        self._stack = contextlib.ExitStack()
        self._servers: list[Any] = []
        self.root = Path()
        self.cfg: Config
        self.app_kw: dict[str, Any] = {}
        self.fake: FakeDiscord
        self.ids: dict[str, str] = {}

    def __enter__(self) -> "Demo":
        self._stack.enter_context(block_network())
        try:
            tmp = tempfile.TemporaryDirectory(prefix="hearback-web-demo-", ignore_cleanup_errors=True)
            self._stack.callback(tmp.cleanup)
            self.root = Path(tmp.name)
            self._build()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        for srv in self._servers:                       # a running AI review must not outlive the folder
            srv.app.review.stop()
            srv.app.review.wait(10)
        self._servers = []
        self._stack.close()

    def server(self, port: int = 0, **kw: Any) -> Any:
        """A bound (not yet serving) console server on this workspace."""
        srv = server.create_server(self.cfg, port, **{**self.app_kw, **kw})
        self._servers.append(srv)
        return srv

    # ---- building
    def _p(self, v: Any) -> Any:
        return pick(v, self.lang)

    def _build(self) -> None:
        P = self._p
        root = self.root
        fake = self.fake = FakeDiscord(page_size=20)
        fake.guild_name = P(GUILD)
        fake.bot["username"], fake.bot["global_name"] = "demobot", P(BOT_NAME)
        fake.tick(time.time() - START_MS / 1000 - 3.2 * DAY)       # the seeded history ends a day or so ago
        role = {"verified": fake.add_role(P(ROLES["verified"]), position=1),
                "supporter": fake.add_role(P(ROLES["supporter"]), position=2),
                "bot": fake.give_bot_role(position=6, name=P(ROLES["bot"])),
                "mod": fake.add_role(P(ROLES["mod"]), position=8)}
        users: dict[str, dict] = {}
        for username, display, roles in MEMBERS:
            u = fake.add_user(username)
            u["global_name"] = P(display)
            users[username] = fake.add_member(u, roles=[role[r] for r in roles])
        chan = {k: fake.add_channel(P(v)) for k, v in CHANNELS.items()}
        forum = fake.add_forum(P(FORUM), [P(t) for t in TAGS.values()])
        for t in THREADS:
            files = [("screenshot.png", make_png(24, 16, (230 - 20 * len(self.ids), 130, 90 + 15 * len(self.ids))))] if t["shot"] else []
            tid = fake.add_thread(forum, P(t["title"]), users[t["who"]], P(t["body"]), [P(TAGS[k]) for k in t["tags"]],
                                  files=files, gap=6 * 3600)
            self.ids[t["key"]] = tid
            for who, text in t["replies"]:
                fake.add_reply(tid, users[who], P(text), gap=5400)
        cfg = self.cfg = Config(
            root=root, guild_id=fake.guild_id, forum_id=forum, announce_channel_id=chan["news"], send_delay=0.0,
            language=self.lang, token_env="HEARBACK_DEMO_NO_TOKEN",       # never the real environment variable
            status_tags={k: P(TAGS[k]) for k in STATUS_TAGS},
            variants={k: Variant(k, tag=P(TAGS[k]), download=P(VARIANTS[k])) for k in VARIANTS})
        ensure_data_dir(cfg.data_dir)
        atomic_write_text(cfg.token_path, FAKE_BOT_TOKEN + "\n")
        ledger = Ledger(cfg.ledger_path)
        queue = Queue(cfg.queue_dir, ledger, protected=cfg.protected_paths())
        keyring = MemoryKeyring()
        llm_store = LLMStore(cfg.data_dir, environ={}, keyring_mod=keyring)
        sup_store = SupporterStore(cfg.data_dir, environ={}, keyring_mod=keyring)
        afdian = FakeAfdian(user_id=FAKE_AFDIAN_USER, token=FAKE_AFDIAN_TOKEN)

        self._model(llm_store)
        sync(fake.client(), ledger, cfg, actor="human:web")
        self._triage(ledger, llm_store)
        self._queue(queue, fake, ledger)
        self._chat(fake, chan, users, Inbox(cfg.data_dir))
        RuleStore(cfg.data_dir).save({"channels": {chan["general"]: {"mode": "draft"}, chan["test"]: {"mode": "auto"}}})
        self._supporters(sup_store, afdian, ledger, role, chan, users)
        self.app_kw = {"client_factory": lambda: fake.client(), "llm_store": llm_store, "runtime": DemoRuntime(fake),
                       "supporters_store": sup_store, "afdian_opener": afdian, "demo": True}

    def _model(self, store: LLMStore) -> None:
        """One active profile, "Demo model", whose provider is the fake model; the real factory returns on exit."""
        P = self._p
        reviews = {}
        for t in (t for t in THREADS if t["key"] in REVIEWS):
            r = {k: P(v) for k, v in REVIEWS[t["key"]].items()}
            dup = r["duplicate_of"]
            r["duplicate_of"] = self.ids[dup] if dup else ""
            r["tags_add"], r["tags_remove"] = [P(TAGS[k]) for k in r["tags_add"]], []
            reviews[P(t["title"])] = r
        provider = DemoProvider(self.lang, reviews)
        previous = llm._FACTORIES["openai"]
        llm.set_provider_factory("openai", lambda profile, key, timeout: provider)
        self._stack.callback(llm.set_provider_factory, "openai", previous)
        prof = store.add_profile({"name": P(MODEL_NAME), "protocol": "openai", "base_url": "", "model": "demo-model"})
        store.set_key(prof["id"], FAKE_MODEL_KEY)

    def _triage(self, ledger: Ledger, store: LLMStore) -> None:
        """What people decided, then the AI reviews four threads (written by review.py, so the shape is the real one)."""
        P = self._p
        for key, fields in HUMAN_TRIAGE.items():
            ledger.update_triage(self.ids[key], {k: P(v) for k, v in fields.items()}, actor="human:web")
        agent = AgentStore(self.cfg.data_dir).get_or_auto(review.DEFAULT_AGENT)

        def ask(system: str, messages: list, max_tokens: int, prefer: str | None) -> tuple[str, str]:
            return complete_any(store, system, messages, max_tokens, prefer=prefer)

        for key in AI_APPLIED:
            res, pid = review.review_thread(ledger, ask, self.ids[key], system=review.system_prompt(agent))
            review.save_ai_result(ledger, self.ids[key], res, model=str(store.get(pid)["model"]), profile=pid)

    def _queue(self, queue: Queue, fake: FakeDiscord, ledger: Ledger) -> None:
        """One failed (a real send that Discord refused), one approved, a reply and a tag change waiting."""
        P = self._p
        f = QUEUE_FAILED
        p = queue.propose("reply", actor="agent:swlink", thread=self.ids[f["thread"]], text=P(f["text"]), why=P(f["why"]))
        fake.fail_next(r"POST /channels/%s/messages" % self.ids[f["thread"]], 403, {"message": f["error"], "code": 50001})
        queue.approve([p.id], "human:web")
        queue.send(fake.client(), ledger, forum_id=self.cfg.forum_id, delay=0, sleep=lambda s: None, actor="human:web",
                   ids=[p.id])
        a = QUEUE_APPROVED
        queue.approve([queue.propose("reply", actor="agent:swlink", thread=self.ids[a["thread"]], text=P(a["text"]),
                                     why=P(a["why"])).id], "human:web")
        r = QUEUE_PENDING_REPLY
        queue.propose("reply", actor="human:web", thread=self.ids[r["thread"]],
                      text=P(REVIEWS[r["thread"]]["reply_draft"]), why=P(r["why"]))
        t = QUEUE_PENDING_TAGS
        queue.propose("tags", actor="human:web", thread=self.ids[t["thread"]], add=[P(TAGS[k]) for k in t["add"]],
                      why=P(t["why"]))

    def _chat(self, fake: FakeDiscord, chan: dict[str, str], users: dict[str, dict], inbox: Inbox) -> None:
        P = self._p
        peer = ""
        for where, who, text, is_bot in CHAT:
            fake.tick(1200)
            if where == "dm":
                peer = who or peer
                channel, gid, name = fake._open_dm(users[peer]["id"])["id"], None, ""
            else:
                channel, gid, name = chan[where], fake.guild_id, P(CHANNELS[where])
            msg = fake._add_message(channel, fake.bot if is_bot else users[who], P(text))
            inbox.add(message_from_rest(msg, fake.bot["id"], guild_id=gid, guild_name=fake.guild_name if gid else "",
                                        channel_name=name))

    def _supporters(self, store: SupporterStore, afdian: FakeAfdian, ledger: Ledger, role: dict[str, str],
                    chan: dict[str, str], users: dict[str, dict]) -> None:
        P = self._p
        store.set_token(FAKE_AFDIAN_TOKEN)
        store.update({"enabled": True, "afdian_user_id": FAKE_AFDIAN_USER, "role_id": role["supporter"],
                      "require_role_ids": [role["verified"]], "dm_handling": True,
                      "notify": {"channel_id": chan["test"], "user_ids": []}})
        rng, now, claims = random.Random(7), time.time(), []
        for n, (buyer, plan, days, remark, paid, claimed_by) in reversed(list(enumerate(ORDERS))):   # newest ends up first
            when = now - days * DAY
            number = time.strftime("%Y%m%d%H%M%S", time.gmtime(when)) + "".join(rng.choice("0123456789") for _ in range(12 + n % 4))
            order = afdian.add_order(out_trade_no=number, plan_id=PLANS[plan][0], plan_title=P(PLANS[plan][1]),
                                     total_amount="30.00" if plan else "5.00", status=2 if paid else 1,
                                     remark=P(remark), user_name=P(buyer), create_time=int(when))
            if claimed_by:
                claims.append((number, claimed_by, order))
        sup = Supporters(store, ledger=ledger, lang=self.lang, templates_dir=self.cfg.templates_dir, opener=afdian)
        for number, username, order in claims:
            sup.link_order(number, users[username]["id"], "human:web", user_name=str(users[username]["global_name"]),
                           order=order)


def run_demo(lang: str = "en", port: int = server.DEFAULT_PORT, open_browser: bool = True) -> int:
    """`hearback web --demo`: build the fake workspace, serve the console on it, clean up on Ctrl+C."""
    with Demo(lang) as demo:
        srv = demo.server(port)
        print("Demo mode: all data is fake; nothing connects to Discord. Files are removed when you stop.", flush=True)
        return server.serve(srv, demo.cfg, open_browser, url_extra="&lang=" + lang)
