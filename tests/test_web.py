"""SWLink web console: an in-process server on port 0, fake Discord, fake model providers."""
from __future__ import annotations

import http.client
import json
import socket
import threading

import pytest

from conftest import make_env
from hearback import llm
from hearback.llm import LLMStore, set_provider_factory
from hearback.web import server as web

SECRET = "test-console-secret-key"
KEY = "sk-test-WEBSECRET-0123456789"
TOKEN = "tok_WEB_SECRET_abcdef0123456789"


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, pw):
        self.store[(service, user)] = pw

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


class StubRuntime:
    """Stands in for the gateway runtime: the tests never open a gateway connection."""

    def __init__(self):
        self.state = "offline"

    def status(self):
        return {"state": self.state, "reason": "", "bot_id": "", "bot_name": ""}

    def start(self):
        self.state = "online"
        return self.status()

    def stop(self):
        self.state = "offline"
        return self.status()

    def guilds(self):
        return []

    online = property(lambda self: self.state == "online")


class Reply:
    def __init__(self, status, raw):
        self.status, self.raw = status, raw
        try:
            self.json = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            self.json = None


class Web:
    def __init__(self, env, srv):
        self.env, self.srv, self.port = env, srv, srv.server_address[1]
        self.bodies: list[bytes] = []          # every response body, for the secrets sweep

    def call(self, method, path, body=None, *, key=SECRET, host=None, ctype="application/json", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        h["Host"] = host or "127.0.0.1:%d" % self.port
        if key is not None:
            h[web.KEY_HEADER] = key
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            if ctype:
                h["Content-Type"] = ctype
        conn.request(method, path, data, h)
        r = conn.getresponse()
        reply = Reply(r.status, r.read())
        reply.headers = {k.lower(): v for k, v in r.getheaders()}
        conn.close()
        self.bodies.append(reply.raw)
        return reply

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def ok(self, method, path, body=None):
        r = self.call(method, path, {} if body is None else body)
        assert r.status in (200, 201), (method, path, r.status, r.raw)
        return r.json


@pytest.fixture
def web_env(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    e = make_env(tmp_path)
    keyring = FakeKeyring()
    store = LLMStore(e.cfg.data_dir, environ={}, keyring_mod=keyring)
    srv = web.create_server(e.cfg, 0, client_factory=lambda: e.fake.client(), llm_store=store,
                            runtime=StubRuntime(), secret=SECRET)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    w = Web(e, srv)
    yield w
    srv.shutdown()
    srv.server_close()
    for p in llm.PROTOCOLS:
        set_provider_factory(p, None)


def say(w, channel, author, text):
    return w.env.fake._add_message(channel, w.env.fake.add_user(author), text)


# ---------------------------------------------------------------- gatekeeping

def test_binds_loopback_only(web_env):
    assert web_env.srv.server_address[0] == "127.0.0.1"


def test_api_needs_the_key(web_env):
    assert web_env.get("/api/status", key=None).status == 403
    assert web_env.get("/api/status", key="wrong").status == 403
    assert web_env.get("/api/status", key=SECRET[:-1]).status == 403
    r = web_env.get("/api/status")
    assert r.status == 200 and r.json["bot"]["state"] == "offline" and r.json["version"]


def test_wrong_host_is_rejected_for_everything(web_env):
    for path in ("/api/status", "/", "/app.js"):
        assert web_env.get(path, host="evil.example:%d" % web_env.port).status == 403
        assert web_env.get(path, host="127.0.0.1:1").status == 403
    assert web_env.get("/api/status", host="localhost:%d" % web_env.port).status == 200


def test_cross_origin_requests_are_rejected(web_env):
    assert web_env.get("/api/status", headers={"Origin": "https://evil.example"}).status == 403
    ok = web_env.get("/api/status", headers={"Origin": "http://127.0.0.1:%d" % web_env.port})
    assert ok.status == 200


def test_writes_need_a_json_content_type(web_env):
    assert web_env.call("POST", "/api/bot/start", b"{}", ctype="text/plain").status == 415
    assert web_env.call("POST", "/api/bot/start", b"{}", ctype=None).status == 415
    assert web_env.call("PUT", "/api/rules", b"[1]").status == 400
    assert web_env.call("PUT", "/api/rules", b"{oops").status == 400
    assert web_env.call("POST", "/api/bot/start", {}).status == 200


def test_unknown_routes_and_methods(web_env):
    assert web_env.get("/api/nope").status == 404
    assert web_env.call("DELETE", "/api/status", {}).status == 405


# ---------------------------------------------------------------- static files

def test_page_is_served_with_a_strict_csp(web_env):
    r = web_env.get("/", key=None)
    assert r.status == 200 and b"<title>SWLink</title>" in r.raw
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    csp = r.headers["content-security-policy"]
    assert csp.startswith("default-src 'self'; img-src 'self' data: https://cdn.discordapp.com https://media.discordapp.net")
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    for path, ctype in (("/app.css", "text/css"), ("/app.js", "text/javascript"), ("/swlink.svg", "image/svg+xml")):
        rr = web_env.get(path, key=None)
        assert rr.status == 200 and rr.headers["content-type"].startswith(ctype)


@pytest.mark.parametrize("path", ["/../server.py", "/%2e%2e/server.py", "/..%2fserver.py", "/static/../server.py",
                                  "/a/../../server.py", "/..\\server.py", "/%5c..%5cserver.py", "/%00", "/__init__.py",
                                  "/nonexistent.js"])
def test_traversal_and_non_static_files_are_refused(web_env, path):
    r = web_env.get(path, key=None)
    assert r.status == 404 and b"import" not in r.raw


def test_no_directory_listing_and_no_posting_to_static(web_env):
    assert web_env.get("//", key=None).status in (200, 404)        # never a listing
    assert web_env.call("POST", "/", {}, key=None).status == 405


def test_the_page_never_uses_innerhtml_or_inline_script():
    from pathlib import Path
    static = Path(web.STATIC_DIR)
    js = (static / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js and "outerHTML" not in js and "document.write" not in js and "eval(" not in js
    assert "alert(" not in js and "confirm(" not in js.replace("confirmBox(", "")
    html = (static / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in html and "style=" not in html and "http" not in html.replace("<!doctype html>", "")


# ---------------------------------------------------------------- port handling

def test_a_busy_port_falls_back_to_a_free_one(tmp_path):
    e = make_env(tmp_path)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        busy = blocker.getsockname()[1]
        srv = web.create_server(e.cfg, busy, runtime=StubRuntime())
        try:
            assert srv.server_address[1] != busy and srv.app.port == srv.server_address[1]
        finally:
            srv.server_close()
    finally:
        blocker.close()


def test_cli_has_a_web_command():
    from hearback import cli
    a = cli.build_parser().parse_args(["web", "--no-browser", "--port", "0"])
    assert a.func is cli.cmd_web and a.no_browser and a.port == 0


# ---------------------------------------------------------------- llm / agents / rules

def test_llm_crud_keys_are_write_only(web_env):
    w = web_env
    assert w.ok("GET", "/api/llm/presets")["items"][0]["model"] == "claude-opus-5-5"
    p = w.ok("POST", "/api/llm", {"preset": "deepseek", "name": "DeepSeek", "protocol": "openai",
                                  "base_url": "https://api.deepseek.com", "model": "deepseek-chat", "key": KEY})
    assert p["id"] == "deepseek" and p["key_hint"] == KEY[-4:] and p["key_set"] and "key" not in p
    assert KEY not in (w.env.cfg.data_dir / "llm.json").read_text(encoding="utf-8")
    got = w.ok("GET", "/api/llm")
    assert got["settings"]["active"] == "deepseek" and got["keyring"] is True and KEY.encode() not in w.bodies[-1]
    assert w.ok("PUT", "/api/llm/deepseek", {"model": "deepseek-reasoner"})["model"] == "deepseek-reasoner"
    p2 = w.ok("POST", "/api/llm", {"name": "Relay", "protocol": "anthropic", "base_url": "https://relay.example",
                                   "model": "claude-sonnet-5-5"})
    assert not p2["key_set"]
    k = w.ok("POST", "/api/llm/relay/key", {"key": KEY + "2"})
    assert k["key_hint"] == (KEY + "2")[-4:]
    w.ok("POST", "/api/llm/relay/activate")
    assert w.ok("GET", "/api/llm")["settings"]["active"] == "relay"
    s = w.ok("PUT", "/api/llm/settings", {"fallback": True, "retries": 2, "timeout_s": 30})
    assert s == {"active": "relay", "fallback": True, "retries": 2, "timeout_s": 30}
    assert w.call("PUT", "/api/llm/settings", {"retries": 50}).status == 400
    assert w.call("POST", "/api/llm", {"name": "x", "protocol": "openai", "model": ""}).status == 400
    w.ok("DELETE", "/api/llm/relay")
    assert [x["id"] for x in w.ok("GET", "/api/llm")["profiles"]] == ["deepseek"]
    assert w.call("DELETE", "/api/llm/relay", {}).status == 400
    assert w.call("POST", "/api/llm/deepseek/key", {"key": "has space"}).status == 400


def test_llm_test_endpoint_uses_the_fake_provider_and_scrubs_errors(web_env):
    w = web_env
    w.ok("POST", "/api/llm", {"name": "Main", "protocol": "openai", "model": "m", "key": KEY})

    class P(llm.Provider):
        calls = []

        def complete(self, system, messages, max_tokens):
            P.calls.append(messages)
            if len(P.calls) == 2:
                raise RuntimeError("401 Unauthorized for key " + KEY)
            return "OK"
    set_provider_factory("openai", lambda profile, key, timeout: P())
    r = w.ok("POST", "/api/llm/main/test")
    assert r["ok"] and r["text"] == "OK" and r["profile"]["status"] == "ok"
    r = w.ok("POST", "/api/llm/main/test")
    assert not r["ok"] and KEY not in json.dumps(r) and r["profile"]["status"] == "error"


def test_agents_crud(web_env):
    w = web_env
    items = w.ok("GET", "/api/agents")["items"]
    assert [a["id"] for a in items] == ["auto", "bug-desk"]
    a = w.ok("POST", "/api/agents", {"name": "Helper", "system_prompt": "Help.", "profile_id": None})
    assert a["id"] == "helper"
    assert w.ok("PUT", "/api/agents/helper", {"description": "d"})["description"] == "d"
    w.ok("DELETE", "/api/agents/helper")
    assert w.call("DELETE", "/api/agents/auto", {}).status == 400
    assert w.call("POST", "/api/agents", {"name": "", "system_prompt": "x"}).status == 400


def test_rules_roundtrip_and_validation(web_env):
    w = web_env
    assert w.ok("GET", "/api/rules")["rules"]["guild_default"]["mode"] == "off"
    r = w.ok("PUT", "/api/rules", {"channels": {"123456": {"mode": "draft", "agent": "bug-desk"}}})
    assert r["rules"]["channels"]["123456"]["mode"] == "draft"
    assert w.ok("GET", "/api/rules")["rules"]["channels"]["123456"]["agent"] == "bug-desk"
    assert w.call("PUT", "/api/rules", {"guild_default": {"mode": "yolo"}}).status == 400
    assert w.ok("GET", "/api/status")["checklist"]["rule"] is True


# ---------------------------------------------------------------- queue

def test_queue_approve_reject_send_through_the_api(web_env):
    w, e = web_env, web_env.env
    ch = e.fake.add_channel("general")
    p1 = e.queue.propose("post", actor="agent:swlink", thread=ch, text="Draft one", why="SWLink draft for Mira")
    p2 = e.queue.propose("post", actor="agent:swlink", thread=ch, text="Draft two", why="x")
    items = w.ok("GET", "/api/queue?state=pending")["items"]
    assert [i["id"] for i in items] == [p1.id, p2.id] and items[0]["actor"] == "agent:swlink"
    assert w.ok("GET", "/api/status")["pending"] == 2
    assert w.ok("POST", "/api/queue/%s/approve" % p1.id)["state"] == "approved"
    assert w.ok("POST", "/api/queue/%s/reject" % p2.id, {"reason": "no"})["state"] == "rejected"
    assert e.fake.writes() == []                                    # approving does not send
    rep = w.ok("POST", "/api/queue/send")
    assert rep["sent"] == [p1.id] and not rep["failed"] and not rep["mismatches"]
    sent = [c for c in e.fake.writes() if c.path == "/channels/%s/messages" % ch]
    assert len(sent) == 1 and sent[0].body["content"] == "Draft one" and sent[0].body["allowed_mentions"] == {"parse": []}
    events = {(x["actor"], x["action"]) for x in e.ledger.read_events()}
    assert ("human:web", "approve") in events and ("human:web", "reject") in events and ("human:web", "send") in events
    assert w.call("POST", "/api/queue/%s/approve" % p2.id, {}).status == 400      # already rejected


# ---------------------------------------------------------------- conversations

def test_conversations_list_messages_and_human_send(web_env):
    w, e = web_env, web_env.env
    ch = e.fake.add_channel("general")
    say(w, ch, "Mira", "How do I reset my save?")
    convs = w.ok("GET", "/api/conversations")["items"]
    row = next(c for c in convs if c["id"] == ch)
    assert row["kind"] == "guild" and row["channel_name"] == "general" and row["mode"] == "off"
    msgs = w.ok("GET", "/api/conversations/%s/messages?limit=10" % ch)["items"]
    assert msgs[-1]["content"] == "How do I reset my save?" and msgs[-1]["author_name"] == "Mira" and not msgs[-1]["is_bot"]
    r = w.ok("POST", "/api/conversations/%s/send" % ch, {"text": "Hello @everyone <@123>"})
    assert len(r["message_ids"]) == 1
    sent = [c for c in e.fake.writes() if c.method == "POST" and c.path == "/channels/%s/messages" % ch]
    assert sent[-1].body["allowed_mentions"] == {"parse": []} and sent[-1].body["content"] == "Hello @everyone <@123>"
    msgs = w.ok("GET", "/api/conversations/%s/messages" % ch)["items"]
    assert msgs[-1]["is_bot"] and msgs[-1]["content"].startswith("Hello")
    assert ("human:web", "send") in {(x["actor"], x["action"]) for x in e.ledger.read_events()}
    assert w.call("POST", "/api/conversations/%s/send" % ch, {"text": "   "}).status == 400
    assert w.call("POST", "/api/conversations/%s/send" % ch, {"text": "x" * 9000}).status == 400
    assert w.call("GET", "/api/conversations/abc/messages").status == 404


def test_long_human_messages_are_split(web_env):
    w, e = web_env, web_env.env
    ch = e.fake.add_channel("general")
    r = w.ok("POST", "/api/conversations/%s/send" % ch, {"text": ("word " * 700).strip()})
    assert len(r["message_ids"]) == 2
    assert all(len(c.body["content"]) <= 2000 for c in e.fake.writes())


def test_ai_draft_returns_text_and_sends_nothing(web_env):
    w, e = web_env, web_env.env
    ch = e.fake.add_channel("general")
    say(w, ch, "Mira", "Where is the download link?")
    w.ok("POST", "/api/llm", {"name": "Main", "protocol": "openai", "model": "m", "key": KEY})
    seen = []

    class P(llm.Provider):
        def complete(self, system, messages, max_tokens):
            seen.append((system, messages))
            return "Check the pinned post."
    set_provider_factory("openai", lambda profile, key, timeout: P())
    before = len(e.fake.writes())
    r = w.ok("POST", "/api/conversations/%s/draft" % ch, {})
    assert r["text"] == "Check the pinned post." and r["agent"] == "auto"
    assert len(e.fake.writes()) == before and e.queue.list() == []
    assert "Where is the download link?" in seen[0][1][0]["content"] and "UNTRUSTED DISCORD TEXT" in seen[0][1][0]["content"]
    r = w.ok("POST", "/api/conversations/%s/draft" % ch, {"agent": "bug-desk"})
    assert r["agent"] == "bug-desk"


def test_ai_draft_errors_are_plain(web_env):
    w, e = web_env, web_env.env
    ch = e.fake.add_channel("empty")
    assert w.call("POST", "/api/conversations/%s/draft" % ch, {}).status == 400          # nothing to answer
    say(w, ch, "Mira", "hi")
    r = w.call("POST", "/api/conversations/%s/draft" % ch, {})
    assert r.status == 400 and "model profile" in r.json["error"]


def test_guilds_endpoint_lists_text_channels_only(web_env):
    names = [c["name"] for c in web_env.ok("GET", "/api/guilds")["items"][0]["channels"]]
    assert "staff" in names and "announce" in names and "bugs" not in names          # the forum is not a text channel


# ---------------------------------------------------------------- forum / logs / settings

def test_forum_without_config_explains_itself(web_env):
    web_env.env.cfg.forum_id = ""
    r = web_env.ok("GET", "/api/forum")
    assert r["items"] == [] and r["configured"] is False and "forum_id" in r["reason"]
    assert web_env.call("POST", "/api/forum/sync", {}).status == 400
    ch = web_env.env.fake.add_channel("general")
    assert web_env.ok("GET", "/api/conversations")["items"]                           # chat works without forum config


def test_forum_sync_and_rows(web_env):
    w, e = web_env, web_env.env
    e.fake.add_thread(e.forum, "Oven freezes", "Mira", "Stops at 0:00", ["Crash"])
    s = w.ok("POST", "/api/forum/sync")
    assert "synced" in s["summary"]
    rows = w.ok("GET", "/api/forum")
    assert rows["configured"] and rows["items"][0]["title"] == "Oven freezes" and rows["items"][0]["status"] == "new"
    assert w.ok("GET", "/api/status")["forum_threads"] == 1


def test_logs_are_scrubbed_and_newest_first(web_env):
    w, e = web_env, web_env.env
    e.ledger.event("human:test", "one", None, {"note": "first"})
    e.ledger.event("human:test", "two", None, {"note": "token=abcdef0123456789xyz and " + KEY})
    items = w.ok("GET", "/api/logs?limit=5")["items"]
    assert [i["action"] for i in items][:2] == ["two", "one"]
    assert KEY not in json.dumps(items) and "abcdef0123456789xyz" not in json.dumps(items)
    assert w.call("GET", "/api/logs?limit=x").status == 400


def test_token_is_write_only(web_env):
    w, e = web_env, web_env.env
    assert w.ok("GET", "/api/status")["token_set"] is False
    assert w.ok("GET", "/api/settings")["token"] == {"set": False, "source": "none"}
    assert w.call("PUT", "/api/settings/token", {"token": "has space"}).status == 400
    assert w.call("PUT", "/api/settings/token", {"token": ""}).status == 400
    r = w.ok("PUT", "/api/settings/token", {"token": "Bot " + TOKEN})
    assert r == {"set": True, "source": "file"} and TOKEN.encode() not in w.bodies[-1]
    assert e.cfg.token_path.read_text(encoding="utf-8").strip() == TOKEN
    assert w.ok("GET", "/api/status")["token_set"] is True
    s = w.ok("GET", "/api/settings")
    assert s["token"]["set"] and "invite_url" in s and s["invite_url"].startswith("https://discord.com/oauth2/authorize?client_id=")
    assert "permissions=" in s["invite_url"] and TOKEN not in json.dumps(s)


def test_start_and_stop_report_status(web_env):
    assert web_env.ok("POST", "/api/bot/start")["state"] == "online"
    assert web_env.ok("GET", "/api/status")["bot"]["state"] == "online"
    assert web_env.ok("POST", "/api/bot/stop")["state"] == "offline"


def test_dashboard_checklist_follows_setup(web_env):
    w = web_env
    assert w.ok("GET", "/api/status")["checklist"] == {"token": False, "llm": False, "rule": False}
    w.ok("PUT", "/api/settings/token", {"token": TOKEN})
    w.ok("POST", "/api/llm", {"name": "Main", "protocol": "openai", "model": "m", "key": KEY})
    w.ok("PUT", "/api/rules", {"guild_default": {"mode": "draft"}})
    st = w.ok("GET", "/api/status")
    assert st["checklist"] == {"token": True, "llm": True, "rule": True}
    assert st["llm"]["name"] == "Main"


# ---------------------------------------------------------------- secrets sweep

def test_neither_key_nor_token_reaches_any_response_or_data_file(web_env):
    w, e = web_env, web_env.env
    w.ok("PUT", "/api/settings/token", {"token": TOKEN})
    w.ok("POST", "/api/llm", {"name": "Main", "protocol": "openai", "model": "m", "key": KEY})
    ch = e.fake.add_channel("general")
    say(w, ch, "Mira", "hello")

    class P(llm.Provider):
        def complete(self, system, messages, max_tokens):
            raise RuntimeError("upstream said: bad key %s and token %s" % (KEY, TOKEN))
    set_provider_factory("openai", lambda profile, key, timeout: P())
    w.call("POST", "/api/llm/main/test", {})
    w.call("POST", "/api/conversations/%s/draft" % ch, {})
    w.ok("PUT", "/api/rules", {"guild_default": {"mode": "draft"}})
    w.ok("POST", "/api/conversations/%s/send" % ch, {"text": "hi"})
    for path in ("/api/status", "/api/llm", "/api/agents", "/api/rules", "/api/logs?limit=500", "/api/settings",
                 "/api/queue?state=pending", "/api/guilds", "/api/conversations"):
        w.get(path)
    blob = b"\n".join(w.bodies)
    assert KEY.encode() not in blob and TOKEN.encode() not in blob
    for p in e.cfg.root.rglob("*"):
        if p.is_file() and p.name != "token.txt":
            data = p.read_bytes()
            assert KEY.encode() not in data and TOKEN.encode() not in data, p.name
