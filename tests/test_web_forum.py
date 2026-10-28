"""SWLink round 2 over HTTP: bug forum settings and AI review, adopt, propose, export, and bot messages."""
from __future__ import annotations

import json
import re
import threading
import tomllib
from pathlib import Path

from test_web import KEY, SECRET, web_env  # noqa: F401  (the fixture)
from hearback import llm
from hearback.llm import set_provider_factory
from hearback.web import server as web

REVIEW_JSON = {"summary": "Oven timer freezes.", "cluster": "oven-freeze", "severity": "high",
               "suggest_status": "investigating", "confidence": 0.7, "duplicate_of": "", "likely_cause": "timer bug",
               "where_to_look": ["timer.py"], "need_info": "version?", "reply_draft": "Thanks, we look into it.",
               "tags_add": ["Need info"], "tags_remove": []}


def add_model(w, provider):
    """A profile with a key, answered by `provider` (a callable(system, messages) -> text)."""
    w.ok("POST", "/api/llm", {"name": "Main", "protocol": "openai", "model": "big-model", "key": KEY})

    class P(llm.Provider):
        def complete(self, system, messages, max_tokens):
            return provider(system, messages)
    set_provider_factory("openai", lambda profile, key, timeout: P())


def two_threads(w):
    e = w.env
    a = e.fake.add_thread(e.forum, "Oven freezes", "Mira", "Stops at 0:00", ["Crash"])
    b = e.fake.add_thread(e.forum, "Customers flicker", "Lina", "On tablet", ["UI"])
    w.ok("POST", "/api/forum/sync")
    return a, b


def finish(w):
    w.srv.app.review.wait()
    return w.ok("GET", "/api/forum/review")


# ---------------------------------------------------------------- settings: where the bug forum is

def test_forum_ids_are_saved_into_a_new_hearback_toml(web_env):
    w, e = web_env, web_env.env
    e.cfg.forum_id = e.cfg.guild_id = ""
    e.cfg.path = None
    r = w.ok("GET", "/api/forum")
    assert r["configured"] is False and "Settings" in r["reason"]
    assert w.call("POST", "/api/forum/review", {}).status == 400 and w.call("POST", "/api/forum/sync", {}).status == 400
    guild, forum = e.fake.guild_id, e.forum
    assert w.ok("PUT", "/api/settings/forum", {"guild_id": guild, "forum_id": forum}) == {"guild_id": guild, "forum_id": forum}
    toml = e.cfg.root / "hearback.toml"
    cfg = tomllib.loads(toml.read_text(encoding="utf-8"))
    assert cfg["discord"]["guild_id"] == guild and cfg["discord"]["forum_id"] == forum
    assert "[variants.free]" in toml.read_text(encoding="utf-8")                      # created from the template
    s = w.ok("GET", "/api/settings")
    assert s["guild_id"] == guild and s["forum_id"] == forum
    assert w.ok("GET", "/api/forum")["configured"] is True
    assert toml in w.srv.app.queue.protected
    assert ("human:web", "forum_update") in {(x["actor"], x["action"]) for x in e.ledger.read_events()}


def test_saving_the_forum_keeps_other_keys_and_comments_and_validates(web_env):
    w, e = web_env, web_env.env
    toml = e.cfg.root / "hearback.toml"
    toml.write_text('# my notes\n[discord]\nguild_id = ""   # the server\nstaff_channel_id = "keep"\n'
                    '\n[swlink]\nhourly_cap = 5   # careful\n', encoding="utf-8")
    e.cfg.path = toml
    w.ok("PUT", "/api/settings/forum", {"forum_id": e.forum})
    text = toml.read_text(encoding="utf-8")
    assert text.startswith("# my notes\n") and 'guild_id = ""   # the server' in text
    assert 'staff_channel_id = "keep"' in text and "hourly_cap = 5   # careful" in text
    assert tomllib.loads(text)["discord"]["forum_id"] == e.forum
    w.ok("PUT", "/api/settings/forum", {"forum_id": e.forum[:-1] + "0", "guild_id": ""})
    again = tomllib.loads(toml.read_text(encoding="utf-8"))
    assert again["discord"]["forum_id"].endswith("0") and again["discord"]["guild_id"] == ""
    for bad in ({"forum_id": "123"}, {"forum_id": "abc" * 6}, {"guild_id": "12 34"}, {}):
        assert w.call("PUT", "/api/settings/forum", bad).status == 400
    assert toml.read_text(encoding="utf-8").count("forum_id") == 1                   # a bad request changed nothing


# ---------------------------------------------------------------- the forum list

def test_forum_list_flags_needs_review_and_shows_the_ai_summary(web_env):
    w = web_env
    a, b = two_threads(w)
    rows = {r["id"]: r for r in w.ok("GET", "/api/forum")["items"]}
    assert rows[a]["needs_review"] and rows[a]["ai"] is None and rows[a]["author"] == "Mira"
    r = w.ok("GET", "/api/forum")
    assert r["to_review"] == 2 and r["review_limit"] == 15 and "investigating" in r["statuses"]
    assert w.ok("GET", "/api/status")["forum_to_review"] == 2
    d = w.ok("GET", "/api/forum/%s" % a)
    assert d["body"] == "Stops at 0:00" and d["ai"] is None and d["forum_tags"] and d["triage"]["status"] == "new"
    assert d["link"].endswith("/" + a) and d["replies"] == []
    assert w.call("GET", "/api/forum/12345").status == 400


# ---------------------------------------------------------------- the review job

def test_review_job_lifecycle_with_a_fake_model(web_env):
    w, e = web_env, web_env.env
    a, b = two_threads(w)
    seen = []
    add_model(w, lambda system, messages: (seen.append((system, messages)), json.dumps(REVIEW_JSON))[1])
    assert w.ok("GET", "/api/forum/review")["running"] is False
    writes_before = len(e.fake.writes())
    started = w.ok("POST", "/api/forum/review", {"limit": 5})
    assert started["total"] == 2 and started["job"]
    st = finish(w)
    assert st["done"] == 2 and st["reviewed"] == 2 and not st["running"] and st["errors"] == [] and st["to_review"] == 0
    assert seen[0][0].startswith("You are the bug desk helper") and "OUTPUT CONTRACT" in seen[0][0]
    tri = e.ledger.load()["threads"][a]["triage"]
    assert tri["status"] == "new" and "reviewed" not in tri                                # the human fields did not move
    assert tri["ai"]["cluster"] == "oven-freeze" and tri["ai"]["model"] == "big-model" and tri["ai"]["profile"] == "main"
    assert tri["ai"]["tags_add"] == ["Need info"]
    assert len(e.fake.writes()) == writes_before and e.queue.list() == []                  # nothing sent, nothing queued
    ev = {(x["actor"], x["action"]) for x in e.ledger.read_events()}
    assert {("agent:swlink", "ai_review"), ("human:web", "ai_review_start")} <= ev
    row = next(r for r in w.ok("GET", "/api/forum")["items"] if r["id"] == a)
    assert row["ai"]["summary"] == "Oven timer freezes." and row["ai"]["has_draft"] and row["needs_review"]
    assert w.ok("GET", "/api/forum")["to_review"] == 0                                      # fresh AI results are not redone
    assert w.call("POST", "/api/forum/review", {}).status == 400                             # nothing left to do


def test_review_needs_a_model_and_runs_one_job_at_a_time(web_env):
    w = web_env
    two_threads(w)
    r = w.call("POST", "/api/forum/review", {})
    assert r.status == 400 and "Models" in r.json["error"]
    gate = threading.Event()
    add_model(w, lambda system, messages: (gate.wait(5), json.dumps(REVIEW_JSON))[1])
    assert w.call("POST", "/api/forum/review", {"limit": "x"}).status == 400
    w.ok("POST", "/api/forum/review", {})
    assert w.call("POST", "/api/forum/review", {}).status == 409
    assert w.ok("GET", "/api/forum/review")["running"] is True
    assert w.ok("POST", "/api/forum/review/stop")["running"] in (True, False)
    gate.set()
    st = finish(w)
    assert st["stopped"] and st["reviewed"] == 1 and st["total"] == 2                         # stop lets the current thread finish


def test_review_errors_are_listed_per_thread_and_scrubbed(web_env):
    w = web_env
    two_threads(w)
    add_model(w, lambda system, messages: "I cannot help with %s" % KEY)
    w.ok("POST", "/api/forum/review", {})
    st = finish(w)
    assert st["done"] == 2 and st["reviewed"] == 0 and len(st["errors"]) == 2
    assert "valid JSON" in st["errors"][0]["error"] and st["errors"][0]["title"]
    assert KEY.encode() not in b"".join(w.bodies)


# ---------------------------------------------------------------- adopt and propose

def reviewed_thread(w, threads=None):
    a, _ = threads or two_threads(w)
    add_model(w, lambda system, messages: json.dumps(REVIEW_JSON))
    w.ok("POST", "/api/forum/review", {})
    finish(w)
    return a


def test_adopt_copies_chosen_fields_and_marks_the_thread_reviewed(web_env):
    w, e = web_env, web_env.env
    a = reviewed_thread(w)
    r = w.ok("POST", "/api/forum/%s/adopt" % a, {"fields": {"status": "investigating", "cluster": "oven-freeze-v2",
                                                             "root_cause": "timer bug", "fix": ""}})
    assert r["triage"]["status"] == "investigating" and r["triage"]["cluster"] == "oven-freeze-v2"
    assert r["triage"]["reviewed"] and r["triage"]["fix"] == ""
    tri = e.ledger.load()["threads"][a]["triage"]
    assert tri["root_cause"] == "timer bug" and tri["reviewed_by"] == "human:web" and "fix" not in tri
    assert tri["ai"]["cluster"] == "oven-freeze"                                              # the AI block is left as it was
    assert ("human:web", "triage") in {(x["actor"], x["action"]) for x in e.ledger.read_events()}
    assert w.call("POST", "/api/forum/%s/adopt" % a, {"fields": {"rev": 99}}).status == 400
    assert w.call("POST", "/api/forum/%s/adopt" % a, {"fields": {"status": "bogus"}}).status == 400
    assert w.call("POST", "/api/forum/%s/adopt" % a, {"fields": {}}).status == 400
    assert w.call("POST", "/api/forum/%s/adopt" % a, {}).status == 400
    row = next(x for x in w.ok("GET", "/api/forum")["items"] if x["id"] == a)
    assert row["status"] == "investigating" and row["cluster"] == "oven-freeze-v2" and row["needs_review"] is False


def test_propose_puts_the_edited_draft_in_the_approval_queue(web_env):
    w, e = web_env, web_env.env
    a = reviewed_thread(w)
    writes = len(e.fake.writes())
    r = w.ok("POST", "/api/forum/%s/propose" % a, {"text": "Thanks, edited by me.", "add": ["need info"], "remove": ["Crash"]})
    assert len(r["proposals"]) == 2 and r["error"] == ""
    items = {p["kind"]: p for p in w.ok("GET", "/api/queue?state=pending")["items"]}
    assert items["reply"]["text"] == "Thanks, edited by me." and items["reply"]["actor"] == "human:web"
    assert items["reply"]["why"] == "AI review draft, edited by a human" and items["reply"]["thread"] == a
    assert "Oven freezes" in items["reply"]["target"]                                              # not just a bare id
    assert items["tags"]["add"] == ["Need info"] and items["tags"]["remove"] == ["Crash"]         # forum spelling
    assert len(e.fake.writes()) == writes                                                          # proposals are not sent
    assert w.call("POST", "/api/forum/%s/propose" % a, {"text": "Thanks, edited by me."}).status == 400   # exact duplicate
    assert w.call("POST", "/api/forum/%s/propose" % a, {}).status == 400
    r = w.call("POST", "/api/forum/%s/propose" % a, {"add": ["Nope"]})
    assert r.status == 400 and "Available" in r.json["error"]
    w.ok("POST", "/api/queue/%s/approve" % items["reply"]["id"])
    sent = w.ok("POST", "/api/queue/send")
    assert sent["sent"] == [items["reply"]["id"]]
    assert any(c.body.get("content") == "Thanks, edited by me." for c in e.fake.writes())


# ---------------------------------------------------------------- export

def test_export_returns_the_markdown_and_saves_it(web_env):
    w, e = web_env, web_env.env
    threads = two_threads(w)
    assert w.call("POST", "/api/forum/export", {}).status == 400                                  # nothing reviewed yet
    a = reviewed_thread(w, threads)
    r = w.ok("POST", "/api/forum/export", {})
    assert r["count"] == 2 and r["filename"].startswith("fix-list-") and r["filename"].endswith(".md")
    assert "## 1. oven-freeze (2)" in r["markdown"] and "timer.py" in r["markdown"] and "version?" in r["markdown"]
    saved = e.cfg.data_dir / "exports" / r["filename"]
    assert saved.read_text(encoding="utf-8") == r["markdown"] and r["path"] == str(saved)
    one = w.ok("POST", "/api/forum/export", {"ids": [a]})
    assert one["count"] == 1


# ---------------------------------------------------------------- bot messages

def test_bot_messages_list_edit_and_delete_only_the_bots_own(web_env):
    w, e = web_env, web_env.env
    ch = e.fake.add_channel("general")
    other = e.fake.add_user("Mira")
    e.fake._add_message(ch, other, "a person talking")
    mine = [e.fake._add_message(ch, e.fake.bot, "bot message %d" % i)["id"] for i in range(3)]
    r = w.ok("GET", "/api/botmsgs?channel=" + ch)
    assert [m["id"] for m in r["items"]] == mine[::-1] and r["label"] == "#general" and r["channel"] == ch
    link = "https://discord.com/channels/%s/%s/%s" % (e.fake.guild_id, ch, mine[0])
    r = w.ok("GET", "/api/botmsgs?channel=" + link.replace("/", "%2F").replace(":", "%3A"))
    assert r["channel"] == ch and r["focus"] == mine[0]
    assert w.call("GET", "/api/botmsgs?channel=nothing").status == 400
    assert w.call("GET", "/api/botmsgs").status == 400
    w.ok("PUT", "/api/botmsgs/%s/%s" % (ch, mine[0]), {"text": "fixed typo"})
    assert e.fake.message(ch, mine[0])["content"] == "fixed typo"
    assert w.call("PUT", "/api/botmsgs/%s/%s" % (ch, mine[0]), {"text": "x" * 2001}).status == 400
    assert w.call("PUT", "/api/botmsgs/%s/%s" % (ch, mine[0]), {"text": "  "}).status == 400
    person = next(m for m in e.fake.messages[ch] if m["author"]["id"] == other["id"])["id"]
    assert w.call("PUT", "/api/botmsgs/%s/%s" % (ch, person), {"text": "hijack"}).status == 400
    assert w.call("DELETE", "/api/botmsgs/%s/%s" % (ch, person), {}).status == 400
    assert e.fake.message(ch, person)["content"] == "a person talking"                            # untouched
    assert w.ok("DELETE", "/api/botmsgs/%s/%s" % (ch, mine[1])) == {"deleted": mine[1]}
    assert e.fake.message(ch, mine[1]) is None
    ev = {(x["actor"], x["action"]) for x in e.ledger.read_events()}
    assert {("human:web", "edit_message"), ("human:web", "delete_message")} <= ev


def test_bot_messages_in_a_forum_thread_keep_the_ledger_in_step(web_env):
    w, e = web_env, web_env.env
    a, _ = two_threads(w)
    p = e.queue.propose("reply", actor="agent:test", thread=a, text="first words", why="t")
    e.approve_and_send(p)
    items = w.ok("GET", "/api/botmsgs?channel=" + a)
    assert items["label"] == "Oven freezes" and [m["content"] for m in items["items"]] == ["first words"]
    mid = items["items"][0]["id"]
    w.ok("PUT", "/api/botmsgs/%s/%s" % (a, mid), {"text": "better words"})
    assert e.ledger.load()["threads"][a]["triage"]["replies_posted"][0]["content"] == "better words"
    w.ok("DELETE", "/api/botmsgs/%s/%s" % (a, mid))
    assert e.ledger.load()["threads"][a]["triage"]["replies_posted"] == []
    assert w.ok("GET", "/api/botmsgs?channel=" + a)["items"] == []


def test_places_list_other_threads_and_dms_but_not_bug_posts(web_env):
    from hearback.chatbot import Message
    w, e = web_env, web_env.env
    a, _ = two_threads(w)
    ideas = e.fake.add_forum("ideas")
    idea = e.fake.add_thread(ideas, "Cake of the week", "Mira", "lemon?")
    w.srv.app.inbox.add(Message(id="9", channel_id="777", author_id="5", author_name="Mira", content="hi",
                                ts="2026-10-24T10:00:00Z", guild_id=None, channel_name="Mira"))
    r = w.ok("GET", "/api/places")
    assert [(t["id"], t["name"], t["parent"]) for t in r["threads"]] == [(idea, "Cake of the week", "ideas")]
    assert a not in {t["id"] for t in r["threads"]}
    assert r["dms"] == [{"id": "777", "name": "Mira", "last": "2026-10-24T10:00:00Z"}] and r["reason"] == ""
    chans = w.ok("GET", "/api/guilds")["items"][0]["channels"]
    assert all("category" in c for c in chans)


def test_new_routes_need_the_key_and_a_json_body(web_env):
    w = web_env
    ch = w.env.fake.add_channel("general")
    for method, path in (("GET", "/api/forum/review"), ("POST", "/api/forum/review"), ("POST", "/api/forum/export"),
                         ("GET", "/api/botmsgs?channel=" + ch), ("PUT", "/api/settings/forum")):
        assert w.call(method, path, {} if method != "GET" else None, key=None).status == 403
    assert w.call("POST", "/api/forum/export", b"{}", ctype="text/plain").status == 415
    assert w.call("DELETE", "/api/botmsgs/1/2", b"{}", ctype="text/plain").status == 415
    assert SECRET


# ---------------------------------------------------------------- the page explains itself

def _texts() -> dict[str, dict[str, str]]:
    src = (Path(web.STATIC_DIR) / "i18n.js").read_text(encoding="utf-8")
    zh, en = src.split("\n  en: {")
    return {lang: dict(re.findall(r'"([\w.\-]+)": "((?:[^"\\]|\\.)*)"', block)) for lang, block in (("zh", zh), ("en", en))}


PAGES = ["dashboard", "conversations", "approvals", "forum", "botmsgs", "supporters", "llm", "agents", "rules", "logs", "settings"]


def test_both_languages_have_the_same_texts():
    tx = _texts()
    assert len(tx["zh"]) > 250 and set(tx["zh"]) == set(tx["en"])
    assert all(v.strip() for lang in tx.values() for v in lang.values())


def test_every_page_has_a_title_subtitle_and_a_short_how_to():
    tx = _texts()
    for lang in ("zh", "en"):
        for p in PAGES:
            assert tx[lang]["nav." + p] and tx[lang]["sub." + p]
            steps = [k for k in tx[lang] if k.startswith("howto.%s." % p)]
            assert 1 <= len(steps) <= 3, (lang, p, steps)


def test_chinese_copy_for_the_pages_is_the_agreed_wording():
    zh = _texts()["zh"]
    assert [zh["nav." + p] for p in PAGES] == ["仪表盘", "聊天", "待审批", "Bug 论坛", "机器人消息", "支持者", "模型",
                                              "人设", "频道规则", "日志", "设置"]
    assert zh["sub.dashboard"] == "机器人状态和待办"
    assert zh["sub.approvals"].startswith("AI 写好、还没发出去的回复。批准并发送后才会发到 Discord")
    assert zh["sub.rules"] == "机器人在每个频道和私信里怎么做" and zh["sub.logs"] == "谁在什么时候做了什么"
    assert (zh["mode.off"], zh["mode.draft"], zh["mode.auto"]) == ("不理", "只起草", "自动回复")
    assert (zh["trigger.mention"], zh["trigger.all"]) == ("被 @ 时才回", "每条都看")
    assert (zh["bot.online"], zh["bot.offline"], zh["bot.error"]) == ("在线", "离线", "出错")
    assert [zh["howto.conversations.%d" % i] for i in (1, 2, 3)] == ["左边选一个频道。", "点「AI 起草」，或者直接打字。", "点「发送」。"]
    for i in range(1, 6):
        assert zh["dash.c%d" % i] and zh["dash.c%dw" % i]
    assert "Message Content Intent" in zh["dash.c3"] and "「只起草」" in zh["dash.c4"]


def test_the_page_loads_its_texts_and_remembers_dismissals_safely():
    static = Path(web.STATIC_DIR)
    html = (static / "index.html").read_text(encoding="utf-8")
    assert html.index("/i18n.js") < html.index("/app.js") and 'id="howto"' in html
    js = (static / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in (static / "i18n.js").read_text(encoding="utf-8")
    assert re.search(r"try \{[^}]*localStorage\.setItem\(howtoKey", js) and "localStorage.getItem(howtoKey" in js
    assert "toggleHowto" in js and '"?"' in js


def test_i18n_script_is_served(web_env):
    r = web_env.get("/i18n.js", key=None)
    assert r.status == 200 and r.headers["content-type"].startswith("text/javascript") and b"const I18N" in r.raw
