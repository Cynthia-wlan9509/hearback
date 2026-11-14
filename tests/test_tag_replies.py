"""[tag_replies]: a moderator-only status tag put on a forum post posts the configured reply there, once.
Offline: FakeDiscord, the gateway callback is called directly."""
from __future__ import annotations

import json

from test_web import web_env  # noqa: F401  (the fixture)
from hearback.config import parse_config

FIXED = "Fixed, awaiting release"
TEXT = "Fixed in the new build, $author. $download ($title) @everyone $nope"


def setup(w, moderated=True, text=TEXT):
    """Config with one template; the tag moderated or not; one post synced into the ledger."""
    e = w.env
    e.cfg.tag_replies = {FIXED: text}
    for t in e.fake.channels[e.forum]["available_tags"]:
        t["moderated"] = moderated
    a = e.fake.add_thread(e.forum, "Oven freezes", "Mira", "Stops at 0:00", ["Crash"])
    w.ok("POST", "/api/feedback/sync")
    return a


def forum_tags(e):
    return {t["id"]: {"name": t["name"], "moderated": t["moderated"]} for t in e.fake.channels[e.forum]["available_tags"]}


def change(w, a, before, after, with_tags=True):
    """The gateway event: the post goes from tag names `before` (None = unknown) to `after`."""
    e = w.env
    ids = lambda names: [e.fake.tag_id(e.forum, n) for n in names]
    w.srv.app.on_thread_tags(e.fake.guild_id, e.forum, a, None if before is None else ids(before), ids(after),
                             forum_tags(e) if with_tags else None)


def bot_texts(w, a):
    return [m["content"] for m in w.env.fake.messages[a][1:]]      # the first message is the post itself


def events(w, action):
    return [x for x in w.env.ledger.read_events() if x["action"] == action]


def test_config_parses_the_table():
    cfg = parse_config({"tag_replies": {" A ": " hello $title ", "B": "", "": "x"}}, __import__("pathlib").Path("."))
    assert cfg.tag_replies == {"A": "hello $title"}
    assert parse_config({}, __import__("pathlib").Path(".")).tag_replies == {}


def test_a_moderated_tag_posts_the_text_once_and_a_repeat_does_not(web_env):
    w, e = web_env, web_env.env
    a = setup(w)
    change(w, a, ["Crash"], ["Crash", FIXED])
    [text] = bot_texts(w, a)
    assert text.startswith("Fixed in the new build, Mira. the pinned link (Oven freezes)")
    assert "@everyone" not in text and "$nope" in text                  # pings defanged, unknown words left alone
    store = json.loads((e.cfg.data_dir / "tag_replies.json").read_text(encoding="utf-8"))
    assert store[a][FIXED]["by"] == "gateway" and store[a][FIXED]["message_id"] == e.fake.messages[a][-1]["id"]
    rec = e.ledger.load()["threads"][a]
    assert rec["tags"] == ["Crash", FIXED]                               # the next diff starts from here
    assert rec["triage"]["status"] == "fixed-pending-release"
    assert rec["triage"]["replies_posted"][-1]["message_id"] == e.fake.messages[a][-1]["id"]
    assert [x["detail"]["tag"] for x in events(w, "tag_reply")] == [FIXED]
    assert w.ok("GET", "/api/feedback/%s" % a)["messages"][-1]["bot"] is True
    change(w, a, ["Crash"], ["Crash", FIXED])                            # same event again
    change(w, a, ["Crash"], ["Crash", FIXED], with_tags=False)
    assert len(bot_texts(w, a)) == 1


def test_a_tag_players_can_apply_is_skipped(web_env):
    w, e = web_env, web_env.env
    a = setup(w, moderated=False)
    change(w, a, ["Crash"], ["Crash", FIXED])
    assert bot_texts(w, a) == []
    [ev] = events(w, "tag_reply_skipped")
    assert ev["detail"] == {"tag": FIXED, "reason": "tag not moderated"}
    assert not (e.cfg.data_dir / "tag_replies.json").exists()


def test_without_forum_tag_info_the_forum_is_asked_over_rest(web_env):
    w = web_env
    a = setup(w)
    change(w, a, None, ["Crash", FIXED], with_tags=False)       # before comes from the ledger, moderated from REST
    assert len(bot_texts(w, a)) == 1
    b = setup_other(w)
    w.env.fake.channels[w.env.forum]["available_tags"][0]["moderated"] = False
    for t in w.env.fake.channels[w.env.forum]["available_tags"]:
        t["moderated"] = False
    change(w, b, None, ["Crash", FIXED], with_tags=False)
    assert bot_texts(w, b) == []


def setup_other(w):
    e = w.env
    b = e.fake.add_thread(e.forum, "Second", "Lina", "Hi", ["Crash"])
    w.ok("POST", "/api/feedback/sync")
    return b


def test_unknown_before_and_a_post_the_ledger_never_saw_does_nothing(web_env):
    w, e = web_env, web_env.env
    setup(w)
    c = e.fake.add_thread(e.forum, "Unsynced", "Zoe", "Hi", ["Crash"])      # not pulled into the ledger
    change(w, c, None, ["Crash", FIXED])
    assert bot_texts(w, c) == [] and not events(w, "tag_reply_skipped")


def test_a_forum_no_workspace_owns_or_a_tag_without_text_does_nothing(web_env):
    w, e = web_env, web_env.env
    a = setup(w)
    w.srv.app.on_thread_tags(e.fake.guild_id, e.fake.add_forum("elsewhere", [FIXED]), a, None, [], None)
    change(w, a, ["Crash"], ["Crash", "UI"])                               # UI has no text
    assert bot_texts(w, a) == []


def test_send_with_empty_text_uses_the_template_and_the_gateway_does_not_repeat_it(web_env):
    w, e = web_env, web_env.env
    a = setup(w)
    w.ok("POST", "/api/feedback/%s/send" % a, {"add": [FIXED]})
    [text] = bot_texts(w, a)
    assert text.startswith("Fixed in the new build, Mira.")
    store = json.loads((e.cfg.data_dir / "tag_replies.json").read_text(encoding="utf-8"))
    assert store[a][FIXED]["by"] == "send"
    change(w, a, ["Crash"], ["Crash", FIXED])                              # the event that follows the tag change
    change(w, a, None, ["Crash", FIXED], with_tags=False)
    assert bot_texts(w, a) == [text]
    assert e.ledger.load()["threads"][a]["triage"]["status"] == "fixed-pending-release"


def test_send_with_own_words_keeps_them_and_still_blocks_the_template(web_env):
    w, e = web_env, web_env.env
    a = setup(w)
    w.ok("POST", "/api/feedback/%s/send" % a, {"text": "Sorry, try the new one.", "add": [FIXED]})
    assert bot_texts(w, a) == ["Sorry, try the new one."]
    assert json.loads((e.cfg.data_dir / "tag_replies.json").read_text(encoding="utf-8"))[a][FIXED]["by"] == "send"
    change(w, a, ["Crash"], ["Crash", FIXED])
    assert bot_texts(w, a) == ["Sorry, try the new one."]


def test_a_failed_send_takes_its_records_back(web_env):
    w, e = web_env, web_env.env
    a = setup(w)
    e.fake.fail_next(r"POST /channels/%s/messages" % a, 403, {"message": "Missing Access", "code": 50001})
    assert w.call("POST", "/api/feedback/%s/send" % a, {"add": [FIXED]}).status == 502
    assert not w.srv.app.tag_store.has(a, FIXED)


def test_the_post_view_offers_the_text_for_the_reply_box(web_env):
    w = web_env
    a = setup(w)
    d = w.ok("GET", "/api/feedback/%s" % a)
    assert list(d["tag_replies"]) == [FIXED] and d["tag_replies"][FIXED].startswith("Fixed in the new build, Mira.")
    assert "@everyone" not in d["tag_replies"][FIXED]
    w.env.cfg.tag_replies = {}
    assert w.ok("GET", "/api/feedback/%s" % a)["tag_replies"] == {}


def test_a_failed_post_is_logged_and_never_raised(web_env):
    w, e = web_env, web_env.env
    a = setup(w)
    e.fake.fail_next(r"POST /channels/%s/messages" % a, 403, {"message": "Missing Access", "code": 50001})
    change(w, a, ["Crash"], ["Crash", FIXED])
    assert bot_texts(w, a) == [] and len(events(w, "tag_reply_failed")) == 1
    assert not w.srv.app.tag_store.has(a, FIXED)
    change(w, a, ["Crash"], ["Crash", FIXED])                              # a later change may try again
    assert len(bot_texts(w, a)) == 1
