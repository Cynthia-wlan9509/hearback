"""SWLink with several workspace folders ([swlink] workspaces): one bot connection, the page switches which
server's forum, approvals, supporters, logs and forum ids it shows. Offline: FakeDiscord, no gateway."""
from __future__ import annotations

import threading

import pytest

from conftest import make_env
from hearback.config import load_config, parse_config
from hearback.ledger import Ledger
from hearback.llm import LLMStore
from hearback.util import HearbackError
from hearback.web import server as web
from test_web import SECRET, FakeKeyring, StubRuntime, Web

TOKEN = "bot-WS-SECRET-token-0123456789abcdef"
OTHER_GUILD, OTHER_FORUM = "9001", "9002"
NEW_FORUM = str(10 ** 17 + 9)                    # built at run time: the hygiene test bans id-like literals


def write_ws(folder, guild=OTHER_GUILD, forum=OTHER_FORUM, token=None, extra=""):
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["[discord]", 'guild_id = "%s"' % guild, 'forum_id = "%s"' % forum]
    if token is not None:
        (folder / "tok.txt").write_text(token + "\n", encoding="utf-8")
        lines.append('token_file = "tok.txt"')
    (folder / "hearback.toml").write_text("\n".join(lines) + "\n" + extra, encoding="utf-8")
    return folder


@pytest.fixture
def two(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    e = make_env(tmp_path / "home")
    other = load_config(write_ws(tmp_path / "second") / "hearback.toml")
    store = LLMStore(e.cfg.data_dir, environ={}, keyring_mod=FakeKeyring())
    srv = web.create_server(e.cfg, 0, client_factory=lambda: e.fake.client(), llm_store=store,
                            runtime=StubRuntime(), secret=SECRET, workspaces=[other])
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield Web(e, srv)
    srv.shutdown()


# ---------------------------------------------------------------- config
def test_config_workspaces_list(tmp_path):
    cfg = parse_config({"swlink": {"workspaces": ["../a", " C:/b "]}}, tmp_path)
    assert cfg.swlink_workspaces == ["../a", "C:/b"]
    assert parse_config({}, tmp_path).swlink_workspaces == []
    for bad in ("../a", [1], [""], {"a": 1}):
        with pytest.raises(HearbackError):
            parse_config({"swlink": {"workspaces": bad}}, tmp_path)


# ---------------------------------------------------------------- switching
def test_list_and_switch(two):
    w = two
    r = w.ok("GET", "/api/workspaces")
    assert [(i["id"], i["home"], i["current"]) for i in r["items"]] == [("0", True, True), ("1", False, False)]
    assert r["items"][1]["folder"] == "second" and r["broken"] == []
    assert w.ok("GET", "/api/status")["workspaces"] == 2
    home_forum = w.env.cfg.forum_id
    assert w.ok("GET", "/api/settings")["forum_id"] == home_forum

    r = w.ok("POST", "/api/workspaces/1/open")
    assert [i["current"] for i in r["items"]] == [False, True]
    s = w.ok("GET", "/api/settings")
    assert (s["guild_id"], s["forum_id"]) == (OTHER_GUILD, OTHER_FORUM)
    app = w.srv.app
    assert app.cfg.root.name == "second" and app.ledger is app.spaces[1].ledger
    assert any(ev["action"] == "workspace_open" for ev in app.spaces[1].ledger.read_events(20))

    w.ok("POST", "/api/workspaces/0/open")
    assert w.ok("GET", "/api/settings")["forum_id"] == home_forum
    assert w.call("POST", "/api/workspaces/7/open", {}).status == 404


def test_forum_ids_saved_to_open_workspace(two):
    w = two
    w.ok("POST", "/api/workspaces/1/open")
    w.ok("PUT", "/api/settings/forum", {"forum_id": NEW_FORUM})
    assert load_config(w.srv.app.spaces[1].cfg.path).forum_id == NEW_FORUM
    assert w.srv.app.home.cfg.forum_id == w.env.cfg.forum_id


def test_bot_side_stays_with_home(two):
    """Gateway state, auto replies and the bot token belong to the home workspace whichever one the page shows."""
    w = two
    app = w.srv.app
    w.ok("POST", "/api/workspaces/1/open")
    app._bot_state("online", "")
    assert any(ev["action"] == "bot_state" for ev in app.home.ledger.read_events(20))
    assert not any(ev["action"] == "bot_state" for ev in app.spaces[1].ledger.read_events(20))
    w.ok("PUT", "/api/settings/token", {"token": TOKEN})
    assert app.home.cfg.token_path.read_text(encoding="utf-8").strip() == TOKEN
    assert not app.spaces[1].cfg.token_path.exists()
    assert app.bot.queue is app.home.queue
    assert all(TOKEN.encode() not in b for b in w.bodies)


def test_order_goes_to_the_server_where_the_person_still_needs_the_role(two):
    from hearback.supporters import Person
    app = two.srv.app
    home, second = app.home, app.spaces[1]
    home.supporters_store.update({"role_id": "41"})
    second.supporters_store.update({"role_id": "42"})
    calls = []
    for ws, name in ((home, "home"), (second, "second")):
        ws.supporter_bot.dm_on = lambda: True
        ws.supporter_bot.handle_dm = (lambda n: lambda a, c, t: calls.append(n) or True)(name)
    roles = {"home": {"41"}, "second": set()}
    app._member_in = lambda ws, uid: Person(id=uid, role_ids=roles["home" if ws is home else "second"])
    order = "202610300000000000000001"
    assert app._supporter_dm("7", "8", "my order " + order) and calls == ["second"]
    roles["second"] = {"42"}                       # has both roles: the first server they are in
    calls.clear()
    assert app._supporter_dm("7", "8", order) and calls == ["home"]
    assert app._supporter_dm("7", "8", "no number here") is False


def test_an_order_used_in_one_server_is_refused_in_the_other(two):
    from hearback.supporters import Person
    w, app = two, two.srv.app
    home, second = app.home, app.spaces[1]
    for ws in (home, second):
        ws.supporter_bot.dm_on = lambda: True
    order = "202610300000000000000002"
    d = home.supporters_store.load()
    d["claims"][order] = {"user_id": "5", "user_name": "x", "at": "", "plan_id": "", "amount": "1", "afdian_user": ""}
    home.supporters_store.save(d)
    second.supporter_bot.handle_dm = lambda *a: pytest.fail("must not be checked again")
    app._member_in = lambda ws, uid: Person(id=uid) if ws is second else None
    user = w.env.fake.add_user("Mira")
    ch = w.env.fake._open_dm(user["id"])["id"]
    assert app._supporter_dm(user["id"], ch, order) is True
    said = [m["content"] for m in w.env.fake.messages.get(ch, [])]
    assert len(said) == 1 and "another server" in said[0]
    assert any(ev["action"] == "supporter_elsewhere" for ev in second.ledger.read_events(20))


# ---------------------------------------------------------------- loading [swlink] workspaces
def test_load_spaces_skips_bad_folders(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    home = write_ws(tmp_path / "home", guild="1", forum="2", token=TOKEN,
                    extra='[swlink]\nworkspaces = ["../same", "../none", "../other", "../missing", "../home", "%s"]\n'
                          % (tmp_path / "same").as_posix())
    write_ws(tmp_path / "same", token=TOKEN)
    write_ws(tmp_path / "none")                                     # no token of its own: uses the home one
    write_ws(tmp_path / "other", guild="7", forum="8", token="bot-DIFFERENT-token-000000000000")
    cfg = load_config(home / "hearback.toml")
    app = web.App(cfg, client_factory=lambda: None, runtime=StubRuntime(), secret=SECRET,
                  llm_store=LLMStore(cfg.data_dir, environ={}, keyring_mod=FakeKeyring()))
    assert [ws.cfg.root.name for ws in app.spaces] == ["home", "same", "none"]
    broken = {b["folder"]: b["error"] for b in app.broken}
    assert set(broken) == {"other", "missing", "home", "same"}
    assert "different bot token" in broken["other"] and "already open" in broken["home"]
    assert "DIFFERENT" not in repr(app.broken)


def test_single_workspace_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    e = make_env(tmp_path)
    app = web.App(e.cfg, client_factory=lambda: e.fake.client(), runtime=StubRuntime(), secret=SECRET,
                  llm_store=LLMStore(e.cfg.data_dir, environ={}, keyring_mod=FakeKeyring()))
    assert app.spaces == [app.home] and app.cur is app.home and app.broken == []
    assert isinstance(app.ledger, Ledger) and app.cfg is e.cfg


# ---------------------------------------------------------------- chat, rules and bot messages split by server
class TwoGuildRuntime(StubRuntime):
    def __init__(self, home_guild):
        super().__init__()
        self.state = "online"
        self.home_guild = home_guild

    def guilds(self):
        return [{"id": self.home_guild, "name": "Home", "channels": [{"id": "11", "name": "home-chat", "category": ""}]},
                {"id": OTHER_GUILD, "name": "Second", "channels": [{"id": "21", "name": "second-chat", "category": ""}]},
                {"id": "5555", "name": "Loose", "channels": [{"id": "31", "name": "loose-chat", "category": ""}]}]


def test_chat_and_servers_follow_the_open_workspace(tmp_path, monkeypatch):
    from hearback.chatbot import Message
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    e = make_env(tmp_path / "home")
    other = load_config(write_ws(tmp_path / "second") / "hearback.toml")
    app = web.App(e.cfg, client_factory=lambda: e.fake.client(), runtime=TwoGuildRuntime(e.cfg.guild_id),
                  secret=SECRET, workspaces=[other],
                  llm_store=LLMStore(e.cfg.data_dir, environ={}, keyring_mod=FakeKeyring()))
    members = {e.cfg.guild_id: {"u-home", "u-both"}, OTHER_GUILD: {"u-second", "u-both"}}
    app._member_ids = lambda g: members.get(g)
    for uid in ("u-home", "u-second", "u-both", "u-none"):
        app.inbox.add(Message(id="m-" + uid, channel_id="dm-" + uid, author_id=uid, author_name=uid, content="hi"))

    def chat():
        return {c["id"] for c in app.api_conversations(None)[1]["items"]}

    def servers():
        return [g["name"] for g in app.api_guilds(web.Request("GET", "/api/guilds", {}, {}, ()))[1]["items"]]

    assert chat() == {"11", "31", "dm-u-home", "dm-u-both", "dm-u-none"}      # home: its server, unclaimed ones
    assert servers() == ["Home", "Loose"]
    app.cur = app.spaces[1]
    assert chat() == {"21", "dm-u-second", "dm-u-both"}
    assert servers() == ["Second"]
    members[OTHER_GUILD] = None                      # Discord will not list members: those DMs stay on home
    app.cur = app.home
    assert {"dm-u-second", "dm-u-both"} <= chat()


def test_one_workspace_shows_everything(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    e = make_env(tmp_path)
    app = web.App(e.cfg, client_factory=lambda: e.fake.client(), runtime=TwoGuildRuntime(e.cfg.guild_id),
                  secret=SECRET, llm_store=LLMStore(e.cfg.data_dir, environ={}, keyring_mod=FakeKeyring()))
    assert {c["id"] for c in app.api_conversations(None)[1]["items"]} == {"11", "21", "31"}


def test_dms_the_bot_answers_itself_still_show_on_the_persons_server(tmp_path, monkeypatch):
    """An order number or device code is answered by the bot without the chat rules; the person's DM is still kept,
    and a DM holding only the bot's words is placed by who Discord says the other end is."""
    from hearback.chatbot import Message
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    e = make_env(tmp_path / "home")
    other = load_config(write_ws(tmp_path / "second") / "hearback.toml")
    app = web.App(e.cfg, client_factory=lambda: e.fake.client(), runtime=TwoGuildRuntime(e.cfg.guild_id),
                  secret=SECRET, workspaces=[other],
                  llm_store=LLMStore(e.cfg.data_dir, environ={}, keyring_mod=FakeKeyring()))
    bot = e.fake.bot["id"]
    mira, noor = e.fake.add_user("Mira"), e.fake.add_user("Noor")
    members = {e.cfg.guild_id: set(), OTHER_GUILD: {mira["id"], noor["id"]}}
    app._member_ids = lambda g: members.get(g)
    app._codes_dm = lambda a, c, t: True                     # the bot answered it itself
    dm1, dm2 = e.fake._open_dm(mira["id"])["id"], e.fake._open_dm(noor["id"])["id"]
    app.on_message(Message(id="1", channel_id=dm1, author_id=mira["id"], author_name="Mira", content="AAAA-BBBB"), bot)
    assert app.inbox.has(dm1, "1")
    app.inbox.add(Message(id="2", channel_id=dm2, author_id=bot, author_name="Bot", content="your code", is_bot=True))

    def dms():
        return {c["id"]: c["channel_name"] for c in app.api_conversations(None)[1]["items"] if c["kind"] == "dm"}

    assert dms() == {}                                       # home: both people are in the second server only
    app.cur = app.spaces[1]
    assert dms() == {dm1: "Mira", dm2: "Noor"}
    assert {d["id"] for d in app.api_places(web.Request("GET", "/api/places", {}, {}, ()))[1]["dms"]} == {dm1, dm2}
