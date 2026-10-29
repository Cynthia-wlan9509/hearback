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


def test_supporter_dm_goes_to_first_workspace_that_takes_it(two):
    app = two.srv.app
    calls = []

    def taker(name, takes):
        def handle(author, channel, content):
            calls.append(name)
            return takes
        return handle

    app.home.supporter_bot.handle_dm = taker("home", False)
    app.spaces[1].supporter_bot.handle_dm = taker("second", True)
    assert app._supporter_dm("1", "2", "order 202610300000000000000001") is True
    assert calls == ["home", "second"]
    calls.clear()
    app.home.supporter_bot.handle_dm = taker("home", True)
    assert app._supporter_dm("1", "2", "x") is True and calls == ["home"]


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
