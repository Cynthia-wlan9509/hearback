"""The feedback inbox over HTTP: every forum in one list, a person answers in place, the AI only when asked."""
from __future__ import annotations

import json

from test_web import KEY, web_env  # noqa: F401  (the fixture)
from hearback import feedback, llm
from hearback.llm import set_provider_factory
from hearback.web import server as web

ANSWER = {"summary": "Oven stops.", "cluster": "oven", "severity": "low", "suggest_status": "investigating", "confidence": 0.5,
          "duplicate_of": "", "likely_cause": "timer", "where_to_look": [], "need_info": "", "reply_draft": "Thanks, looking.",
          "tags_add": ["Later"], "tags_remove": []}


def add_model(w, calls=None):
    w.ok("POST", "/api/llm", {"name": "Main", "protocol": "openai", "model": "big-model", "key": KEY})

    class P(llm.Provider):
        def complete(self, system, messages, max_tokens):
            if calls is not None:
                calls.append(system)
            return json.dumps(ANSWER)
    set_provider_factory("openai", lambda profile, key, timeout: P())


def forums(w):
    """The fixture's bug forum plus an ideas forum with its own tags; both pulled."""
    e = w.env
    ideas = e.fake.add_forum("ideas", ["Later", "Done"])
    a = e.fake.add_thread(e.forum, "Oven freezes", "Mira", "Stops at 0:00", ["Crash"])
    b = e.fake.add_thread(ideas, "Dark menu", "Lina", "Too bright")
    assert w.ok("POST", "/api/feedback/sync")["new"] == 2
    return ideas, a, b


def item(w, tid):
    return next((r for r in w.ok("GET", "/api/feedback")["items"] if r["id"] == tid), None)


def test_every_forum_of_the_server_lands_in_one_list(web_env):
    w = web_env
    ideas, a, b = forums(w)
    r = w.ok("GET", "/api/feedback")
    assert [f["name"] for f in r["forums"]][-1] == "ideas" and ideas in [f["id"] for f in r["forums"]]
    assert {x["id"]: x["forum"] for x in r["items"]}[b] == ideas
    assert item(w, a)["state"] == "todo" and item(w, a)["fresh"] == "new"
    counts = w.ok("GET", "/api/status")["inbox"]
    assert counts["todo"] == 2 and {f["id"]: f["todo"] for f in counts["forums"]}[ideas] == 1


def test_a_new_forum_shows_up_on_the_next_pull_and_pulling_costs_no_model_call(web_env):
    w = web_env
    calls = []
    add_model(w, calls)
    forums(w)
    third = w.env.fake.add_forum("fan-art")
    t = w.env.fake.add_thread(third, "My drawing", "Mira", "Look")
    assert w.ok("POST", "/api/feedback/sync")["new"] == 1
    assert item(w, t)["forum"] == third and calls == []


def test_tags_with_the_same_name_in_two_forums_go_to_the_right_one(web_env):
    w = web_env
    e = w.env
    twin = e.fake.add_forum("twin", ["Crash"])
    t = e.fake.add_thread(twin, "Twin post", "Mira", "Hi")
    w.ok("POST", "/api/feedback/sync")
    w.ok("POST", "/api/feedback/%s/send" % t, {"add": ["Crash"]})
    assert e.fake.channels[t]["applied_tags"] == [e.fake.tag_id(twin, "Crash")]


def test_send_replies_in_place_with_no_approval_step_and_settles_the_post(web_env):
    w = web_env
    e = w.env
    _ideas, a, _b = forums(w)
    r = w.ok("POST", "/api/feedback/%s/send" % a, {"text": "Thanks!", "add": ["UI"]})
    assert r["sent"] and not r["mismatches"]
    assert e.fake.messages[a][-1]["content"] == "Thanks!"
    assert e.fake.tag_id(e.forum, "UI") in e.fake.channels[a]["applied_tags"]
    assert item(w, a)["state"] == "done"
    assert not e.queue.list("pending") and not e.queue.list("approved")
    d = w.ok("GET", "/api/feedback/%s" % a)
    assert d["messages"][-1]["bot"] and d["messages"][-1]["content"] == "Thanks!"       # before the next pull too
    e.fake.add_reply(a, "Mira", "Still broken")
    w.ok("POST", "/api/feedback/sync")
    assert item(w, a)["state"] == "todo" and item(w, a)["fresh"] == "reply"
    assert w.ok("GET", "/api/feedback")["handled_today"] == 1


def test_a_failed_send_leaves_nothing_waiting_and_says_why(web_env):
    w = web_env
    e = w.env
    _ideas, a, _b = forums(w)
    e.fake.fail_next(r"POST /channels/%s/messages" % a, 403, {"message": "Missing Access", "code": 50001})
    res = w.call("POST", "/api/feedback/%s/send" % a, {"text": "Hello"})
    assert res.status == 502 and "Missing Access" in res.json["error"]
    assert not e.queue.list("failed") and not e.queue.list("pending") and item(w, a)["state"] == "todo"


def test_a_status_tag_keeps_the_release_status_in_step(web_env):
    w = web_env
    e = w.env
    e.cfg.status_tags = {"investigating": "UI"}
    _ideas, a, _b = forums(w)
    w.ok("POST", "/api/feedback/%s/send" % a, {"add": ["UI"]})
    assert e.ledger.load()["threads"][a]["triage"]["status"] == "investigating"


def test_tags_of_another_forum_are_refused(web_env):
    w = web_env
    _ideas, a, _b = forums(w)
    assert w.call("POST", "/api/feedback/%s/send" % a, {"add": ["Later"]}).status == 400
    assert w.call("POST", "/api/feedback/%s/send" % a, {}).status == 400


def test_no_reply_needed_can_be_undone_and_a_new_message_brings_it_back(web_env):
    w = web_env
    e = w.env
    _ideas, a, _b = forums(w)
    assert w.ok("POST", "/api/feedback/%s/mark" % a, {"state": "done"})["state"] == "done"
    assert w.ok("POST", "/api/feedback/%s/mark" % a, {"state": "auto"})["state"] == "todo"
    w.ok("POST", "/api/feedback/%s/mark" % a, {"state": "done"})
    e.fake.add_reply(a, "Mira", "One more thing")
    w.ok("POST", "/api/feedback/sync")
    assert item(w, a)["state"] == "todo"
    assert w.call("POST", "/api/feedback/%s/mark" % a, {"state": "x"}).status == 400


def test_a_handled_post_can_go_back_to_todo(web_env):
    w = web_env
    _ideas, a, _b = forums(w)
    w.ok("POST", "/api/feedback/%s/send" % a, {"text": "Thanks"})
    assert w.ok("POST", "/api/feedback/%s/mark" % a, {"state": "open"})["state"] == "todo"


def test_pinned_posts_are_not_feedback_and_a_staff_post_needs_no_answer():
    staff = {"9"}
    assert feedback.inbox_state("10", {"pinned": True, "author_id": "1"}, staff) is None
    assert feedback.inbox_state("10", {"triage": {"status": "guide"}, "author_id": "1"}, staff) is None
    assert feedback.inbox_state("10", {"author_id": "9"}, staff) == "done"
    owner = {"author_id": "1", "replies": [{"id": "11", "author_id": "9", "content": "ok"}]}
    assert feedback.inbox_state("10", owner, staff) == "done"
    closed = {"author_id": "1", "created": "2026-01-01T00:00:00Z",
              "triage": {"status": "released", "reviewed": "2026-02-01T00:00:00Z"}}
    assert feedback.inbox_state("10", closed, staff) == "done"


def test_ai_runs_only_when_asked_writes_a_draft_and_counts_the_call(web_env):
    w = web_env
    calls = []
    add_model(w, calls)
    _ideas, a, b = forums(w)
    assert calls == [] and not item(w, a)["has_draft"]
    r = w.ok("POST", "/api/feedback/%s/ai" % b, {})
    assert r["ai"]["reply_draft"] == "Thanks, looking." and r["ai"]["tags_add"] == ["Later"]      # the ideas forum's tags
    assert len(calls) == 1 and item(w, b)["has_draft"] and item(w, b)["state"] == "todo"
    assert not w.env.fake.messages[b][1:]                                                          # nothing was sent
    assert w.ok("GET", "/api/feedback")["spend"]["calls"] == 1


def test_each_forum_uses_its_own_agent_and_none_means_no_ai(web_env):
    w = web_env
    calls = []
    add_model(w, calls)
    ideas, a, b = forums(w)
    agent = w.ok("POST", "/api/agents", {"name": "Idea desk", "system_prompt": "IDEA PROMPT"})
    w.ok("PUT", "/api/feedback/forums/%s" % ideas, {"agent": agent["id"]})
    w.ok("POST", "/api/feedback/%s/ai" % b, {})
    assert "IDEA PROMPT" in calls[-1]
    w.ok("PUT", "/api/feedback/forums/%s" % w.env.forum, {"agent": "none"})
    assert w.call("POST", "/api/feedback/%s/ai" % a, {}).status == 400
    assert w.call("PUT", "/api/feedback/forums/%s" % ideas, {"agent": "ghost"}).status == 400


def test_go_through_them_all_drafts_only_and_skips_forums_without_ai(web_env):
    w = web_env
    calls = []
    add_model(w, calls)
    ideas, a, b = forums(w)
    w.ok("PUT", "/api/feedback/forums/%s" % w.env.forum, {"agent": "none"})
    r = w.ok("POST", "/api/feedback/review", {})
    assert r["total"] == 1
    w.srv.app.review.wait()
    assert item(w, b)["has_draft"] and not item(w, a)["has_draft"] and len(calls) == 1
    assert w.call("POST", "/api/feedback/review", {"forum": ideas}).status == 400        # nothing left there


def test_a_forum_left_out_is_not_listed_or_pulled_but_can_come_back(web_env):
    w = web_env
    ideas, _a, b = forums(w)
    w.ok("PUT", "/api/feedback/forums/%s" % ideas, {"hidden": True})
    assert item(w, b) is None
    w.ok("POST", "/api/feedback/sync")
    row = next(f for f in w.ok("GET", "/api/feedback")["forums"] if f["id"] == ideas)
    assert row["hidden"] is True
    w.ok("PUT", "/api/feedback/forums/%s" % ideas, {"hidden": False})
    assert item(w, b) is not None


def test_a_forum_the_bot_may_not_read_is_a_warning_not_a_failure(web_env):
    w = web_env
    e = w.env
    closed = e.fake.add_forum("staff-only")
    e.fake.fail_next(r"GET /channels/%s/threads/archived/public" % closed, 403, {"message": "Missing Access", "code": 50001})
    a = e.fake.add_thread(e.forum, "Oven freezes", "Mira", "Stops")
    r = w.ok("POST", "/api/feedback/sync")
    assert r["new"] == 1 and "staff-only" in r["warnings"][0] and item(w, a)


def test_pictures_come_back_as_data_links_and_only_from_the_data_folder(web_env):
    w = web_env
    e = w.env
    from hearback.fake import make_png
    t = e.fake.add_thread(e.forum, "Shot", "Mira", "See", files=[("shot.png", make_png(4, 4, (1, 2, 3)))])
    w.ok("POST", "/api/feedback/sync")
    d = w.ok("GET", "/api/feedback/%s" % t)
    assert d["files"][0]["image"] and d["files"][0]["name"] == "shot.png"
    assert w.ok("GET", "/api/feedback/%s/file/0" % t)["data"].startswith("data:image/png;base64,")
    assert w.call("GET", "/api/feedback/%s/file/9" % t).status == 404
    with e.ledger.locked():
        data = e.ledger.load()
        data["threads"][t]["attachments"] = ["../../secret.png"]
        e.ledger.save(data)
    assert w.call("GET", "/api/feedback/%s/file/0" % t).status == 404


def test_usage_prices_only_known_models(tmp_path):
    u = feedback.Usage(tmp_path, clock=lambda: 1800000000)
    u.add("claude-haiku-5-5", (1_000_000, 1_000_000))
    assert u.this_month() == {"calls": 1, "unpriced": 0, "cost": 0.6}
    u.add("some-relay-model", (10, 10))
    assert u.this_month()["unpriced"] == 1 and feedback.estimate("some-relay-model") is None
    assert 0 < feedback.estimate("claude-haiku-5-5") < 0.01


def test_the_page_has_the_cat_and_never_builds_markup_from_data():
    js = (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "/swlink.svg" in js and "innerHTML" not in js
    assert 'ICONS[name]' in js                      # only fixed icon markup goes through the parser
    for route in ("/api/feedback", "/api/feedback/sync", "/api/feedback/review", "/mark", "/send", "/ai", "/file/"):
        assert route in js
