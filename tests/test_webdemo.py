"""`hearback web --demo`: fake data in a temporary folder, no network, nothing saved. Both seeds (en and zh)."""
from __future__ import annotations

import socket
import threading
import urllib.request
from pathlib import Path

import pytest

from hearback import cli, llm
from hearback.util import HearbackError
from hearback.web import server as web
from hearback.webdemo import BLOCKED, Demo, MemoryKeyring
from hearback.webdemo_seed import CHANNELS, GUILD, THREADS, pick
from test_web import SECRET, Web

LANGS = ("en", "zh")


@pytest.fixture(params=LANGS)
def demo_web(request, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                              # a stray write would land here, not in a real workspace
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    with Demo(request.param) as d:
        srv = d.server(0, secret=SECRET)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        w = Web(None, srv)
        w.demo, w.lang = d, request.param
        yield w
        srv.shutdown()
        srv.server_close()
    assert list(tmp_path.iterdir()) == []


def p(w, pair):
    return pick(pair, w.lang)


# ---------------------------------------------------------------- the guards

def test_status_says_demo_and_the_bot_starts_without_a_gateway(demo_web):
    w = demo_web
    s = w.ok("GET", "/api/status")
    assert s["demo"] is True and s["bot"]["state"] == "offline" and s["token_set"] and s["supporters"]["claims"] == 1
    assert s["checklist"] == {"token": True, "llm": True, "rule": True}
    assert w.ok("POST", "/api/bot/start")["state"] == "online"
    assert w.ok("GET", "/api/status")["bot"]["bot_name"] == p(w, ("Demo Bot", "演示机器人"))
    assert w.ok("POST", "/api/bot/stop")["state"] == "offline"


def test_a_normal_console_is_not_demo(tmp_path, monkeypatch):
    from conftest import make_env
    from test_web import StubRuntime
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    e = make_env(tmp_path)
    srv = web.create_server(e.cfg, 0, client_factory=lambda: e.fake.client(), runtime=StubRuntime(), secret=SECRET)
    try:
        assert srv.app.api_status(None)[1]["demo"] is False
    finally:
        srv.server_close()


def test_the_network_is_blocked_while_the_demo_runs_and_restored_after(tmp_path):
    real = (urllib.request.urlopen, socket.create_connection)
    prev_factory = llm._FACTORIES["openai"]
    with Demo("en") as d:
        assert d.root.is_dir()
        with pytest.raises(HearbackError, match=BLOCKED):
            urllib.request.urlopen("https://example.com/")
        with pytest.raises(HearbackError, match=BLOCKED):
            urllib.request.build_opener().open("https://example.com/")
        with pytest.raises(HearbackError, match=BLOCKED):
            socket.create_connection(("example.com", 443), timeout=1)
        assert llm._FACTORIES["openai"] is not prev_factory           # the fake model is plugged in
        root = d.root
    assert not root.exists()                                            # the temporary folder is gone
    assert (urllib.request.urlopen, socket.create_connection) == real
    assert llm._FACTORIES["openai"] is prev_factory


def test_the_folder_is_removed_even_after_serving_and_a_review(tmp_path):
    with Demo("en") as d:
        srv = d.server(0, secret=SECRET)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        w = Web(None, srv)
        w.ok("POST", "/api/forum/review", {})
        srv.shutdown()
        srv.server_close()
        root = d.root
        assert (root / ".hearback" / "ledger.json").is_file()
    assert not root.exists()


def test_nothing_real_is_used_for_secrets(demo_web, monkeypatch):
    w, app = demo_web, demo_web.srv.app
    monkeypatch.setenv("HEARBACK_LLM_KEY_DEMO_MODEL", "sk-real-looking-0123456789")
    assert isinstance(app.llm._keyring, MemoryKeyring) and app.supporters_store._keyring is app.llm._keyring
    assert app.cfg.token_env != "DISCORD_BOT_TOKEN"
    pid = app.llm.list_public()[0]["id"]
    for method, path, body in (("PUT", "/api/settings/token", {"token": "tok_REAL_0123456789abcdef"}),
                               ("POST", "/api/llm/%s/key" % pid, {"key": "sk-real-0123456789"}),
                               ("POST", "/api/llm", {"name": "Mine", "protocol": "openai", "model": "m", "key": "sk-real-0123456789"}),
                               ("POST", "/api/supporters/token", {"token": "real-afdian-token"})):
        r = w.call(method, path, body)
        assert r.status == 400 and r.json == {"error": "demo_locked"}, (path, r.raw)
    assert len(app.llm.list_public()) == 1                               # the refused create left no profile behind
    assert app.llm.get_key(pid) == "sk-demo-not-a-real-key-0000"
    assert not any(b"real" in body for body in w.bodies if b"demo_locked" not in body)


# ---------------------------------------------------------------- the seed

def test_the_forum_is_seeded_with_mixed_triage_and_four_ai_reviews(demo_web):
    w = demo_web
    r = w.ok("GET", "/api/forum")
    assert r["configured"] and len(r["items"]) == 8 == len(THREADS) and r["to_review"] == 2
    assert {x["title"] for x in r["items"]} == {p(w, t["title"]) for t in THREADS}
    assert {x["status"] for x in r["items"]} == {"new", "need-info", "investigating", "fixed-pending-release", "not-bug"}
    reviewed = {x["title"]: x["ai"] for x in r["items"] if x["ai"]}
    assert len(reviewed) == 4 and {a["suggest_status"] for a in reviewed.values()} == {"investigating", "duplicate", "need-info"}
    assert all(x["needs_review"] and not x["ai"] for x in r["items"] if x["status"] == "new" and not x["ai"])
    assert sum(1 for x in r["items"] if x["needs_review"] and not x["ai"]) == 2
    assert r["model"] == "demo-model"
    assert w.ok("GET", "/api/status")["forum_to_review"] == 2
    assert w.ok("GET", "/api/status")["forum_threads"] == 8


def test_threads_have_replies_tags_an_image_and_ai_shape(demo_web):
    w = demo_web
    rows = {x["title"]: x for x in w.ok("GET", "/api/forum")["items"]}
    dup = rows[p(w, THREADS[1]["title"])]
    first = w.ok("GET", "/api/forum/%s" % rows[p(w, THREADS[0]["title"])]["id"])
    assert first["reply_count"] == 2 and first["attachments"] and first["attachments"][0].endswith("screenshot.png")
    assert first["ai"]["suggest_status"] == "investigating" and first["ai"]["reply_draft"] and first["ai"]["where_to_look"]
    assert set(first["ai"]) >= {"summary", "cluster", "severity", "suggest_status", "confidence", "duplicate_of",
                                "likely_cause", "where_to_look", "need_info", "reply_draft", "tags_add", "tags_remove",
                                "at", "model", "profile"}
    d = w.ok("GET", "/api/forum/%s" % dup["id"])
    assert d["ai"]["duplicate_of"] == rows[p(w, THREADS[0]["title"])]["id"] and d["ai"]["suggest_status"] == "duplicate"
    need = next(x for x in rows.values() if x["ai"] and x["ai"]["suggest_status"] == "need-info")
    assert w.ok("GET", "/api/forum/%s" % need["id"])["ai"]["need_info"]
    tags = d["forum_tags"]
    assert len(tags) == 12 and p(w, ("Free", "体验版")) in tags and p(w, ("Full", "完整版")) in tags
    assert set(first["tags"]) <= set(tags)


def test_ai_review_works_offline_with_the_fake_model(demo_web):
    w, app = demo_web, demo_web.srv.app
    job = w.ok("POST", "/api/forum/review", {})
    assert job["total"] == 2
    app.review.wait()
    st = w.ok("GET", "/api/forum/review")
    assert st["running"] is False and st["reviewed"] == 2 and st["errors"] == [] and st["to_review"] == 0
    rows = w.ok("GET", "/api/forum")["items"]
    assert all(x["ai"] for x in rows if x["status"] == "new") and w.ok("GET", "/api/forum")["to_review"] == 0
    assert w.ok("POST", "/api/llm/%s/test" % app.llm.list_public()[0]["id"])["ok"] is True


def test_the_queue_has_pending_approved_and_a_readable_failure(demo_web):
    w = demo_web
    pending = w.ok("GET", "/api/queue?state=pending")["items"]
    assert sorted(x["kind"] for x in pending) == ["reply", "tags"] and w.ok("GET", "/api/status")["pending"] == 2
    assert all(x["target"].startswith("Bug forum / ") for x in pending)
    assert len(w.ok("GET", "/api/queue?state=approved")["items"]) == 1
    failed = w.ok("GET", "/api/queue?state=failed")["items"]
    assert len(failed) == 1 and "Missing Access" in failed[0]["error"] and failed[0]["text"]
    tags = next(x for x in pending if x["kind"] == "tags")
    assert tags["add"] == [p(w, ("Duplicate", "重复"))]
    assert w.ok("POST", "/api/queue/%s/retry" % failed[0]["id"])["state"] == "approved"


def test_conversations_rules_and_an_ai_draft(demo_web):
    w = demo_web
    items = w.ok("GET", "/api/conversations")["items"]
    guild = p(w, GUILD)
    by_name = {c["channel_name"]: c for c in items}
    assert {p(w, v) for v in CHANNELS.values()} <= set(by_name)
    assert sum(1 for c in items if c["kind"] == "dm") == 1 and all(c["guild_name"] == guild for c in items if c["kind"] == "guild")
    general, test = by_name[p(w, CHANNELS["general"])], by_name[p(w, CHANNELS["test"])]
    assert (general["mode"], test["mode"]) == ("draft", "auto")
    rules = w.ok("GET", "/api/rules")["rules"]
    assert rules["dm_default"]["mode"] == rules["guild_default"]["mode"] == "off"
    msgs = w.ok("GET", "/api/conversations/%s/messages" % general["id"])["items"]
    assert len(msgs) == 3 and not any(m["is_bot"] for m in msgs)
    dm = next(c for c in items if c["kind"] == "dm")
    assert [m["is_bot"] for m in w.ok("GET", "/api/conversations/%s/messages" % dm["id"])["items"]] == [False, True]
    draft = w.ok("POST", "/api/conversations/%s/draft" % general["id"], {})
    assert draft["text"] == p(w, ("A dark mode is on the wishlist, but there is no date yet. You can add the idea in the "
                                  "bug forum so it gets counted.",
                                  "夜间模式在愿望清单上，不过暂时没有时间表。你可以在 bug 论坛里留一条建议，这样会被统计到。"))
    assert draft["profile"]


def test_supporters_are_set_up_with_orders_suggestions_and_a_claim(demo_web):
    w = demo_web
    ov = w.ok("GET", "/api/supporters/settings")
    s = ov["settings"]
    assert s["enabled"] and s["afdian_user_id"] and s["role_id"] and len(s["require_role_ids"]) == 1 and ov["token_set"]
    assert s["notify"]["channel_id"] and ov["guild"]["name"] == p(w, GUILD) and ov["counts"]["claims"] == 1
    assert w.ok("POST", "/api/supporters/test")["ok"] is True
    roles = w.ok("GET", "/api/guilds/%s/roles" % ov["guild"]["id"])
    assert roles["can_manage_roles"] and roles["bot_top"]["name"] == p(w, ("Demo Bot", "演示机器人"))
    sup = next(r for r in roles["items"] if r["id"] == s["role_id"])
    assert sup["name"] == p(w, ("Supporter", "支持者")) and sup["assignable"]
    orders = w.ok("GET", "/api/supporters/orders")
    items = orders["items"]
    assert orders["members_available"] and len(items) == 10
    assert all(26 <= len(o["id"]) <= 29 and o["id"].isdigit() for o in items) and len({o["id"] for o in items}) == 10
    assert len([o for o in items if o["suggestions"]]) == 2 and sum(1 for o in items if o["claim"]) == 1
    assert sum(1 for o in items if not o["paid"]) == 1 and all(o["title"] and o["buyer"] for o in items)
    assert len(w.ok("GET", "/api/supporters/claims")["items"]) == 1


def test_a_simulated_dm_reply_comes_from_the_templates(demo_web):
    w = demo_web
    order = w.ok("GET", "/api/supporters/orders")["items"][0]["id"]
    r = w.ok("POST", "/api/supporters/simulate", {"text": "my order is " + order})
    assert r["reply_key"] and r["reply"]                                 # no network: Afdian is the fake


def test_both_languages_seed_their_own_text(tmp_path):
    seen = {}
    for lang in LANGS:
        with Demo(lang) as d:
            seen[lang] = (d.fake.guild_name, d.cfg.language, sorted(d.cfg.status_tags.values()))
            assert d.cfg.variants["free"].tag == pick(("Free", "体验版"), lang)
    assert seen["en"][0] == "Demo Server" and seen["zh"][0] == "演示服务器"
    assert seen["en"][1:] != seen["zh"][1:] and set(seen["en"][2]).isdisjoint(seen["zh"][2])
    assert any("一" <= ch <= "鿿" for ch in "".join(seen["zh"][2]))
    with pytest.raises(HearbackError):
        Demo("fr")


# ---------------------------------------------------------------- the page and the command

def test_the_page_shows_a_demo_banner_in_both_languages():
    static = Path(web.STATIC_DIR)
    i18n = (static / "i18n.js").read_text(encoding="utf-8")
    assert '"demo.banner": "演示模式：所有数据都是假的，不会连接 Discord"' in i18n
    assert '"demo.banner": "Demo mode: all data is fake; nothing connects to Discord"' in i18n
    assert i18n.count('"err.demo_locked"') == 2
    js, html = (static / "app.js").read_text(encoding="utf-8"), (static / "index.html").read_text(encoding="utf-8")
    assert "STATUS.demo" in js and 'id="demo"' in html


def test_the_command_line_flags(monkeypatch):
    a = cli.build_parser().parse_args(["web", "--demo", "--lang", "zh", "--no-browser", "--port", "0"])
    assert a.func is cli.cmd_web and a.demo and a.lang == "zh" and a.no_browser and a.port == 0
    b = cli.build_parser().parse_args(["web"])
    assert b.demo is False and b.lang == "en"
    calls = []
    import hearback.webdemo as wd
    monkeypatch.setattr(wd, "run_demo", lambda lang, port, open_browser: calls.append((lang, port, open_browser)) or 0)
    assert cli.main(["web", "--demo", "--lang", "zh", "--no-browser", "--port", "0"]) == 0
    assert calls == [("zh", 0, False)]
