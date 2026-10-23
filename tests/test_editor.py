"""The editor's logic (no display needed) plus a smoke test of the window when Tk can open one."""
from __future__ import annotations

import pytest

from conftest import fresh_reply
from hearback import editor
from hearback.util import HearbackError


def test_parse_target():
    gid, cid, mid = "1" * 18, "2" * 18, "3" * 18
    assert editor.parse_target(cid) == (cid, None)
    assert editor.parse_target("https://discord.com/channels/%s/%s" % (gid, cid)) == (cid, None)
    assert editor.parse_target("https://discord.com/channels/%s/%s/%s" % (gid, cid, mid)) == (cid, mid)
    assert editor.parse_target("no ids here") == (None, None)
    assert editor.parse_target(None) == (None, None)


def test_places_list_channels_then_guides_then_threads(seeded):
    e = seeded
    guide = e.queue.propose("newpost", actor="a", title="Guide", text="body")
    e.approve_and_send(guide)
    e.sync()
    pl = editor.places(e.cfg, e.ledger)
    labels = [p[0] for p in pl]
    assert labels[0] == "announcements channel" and labels[1] == "staff channel"
    assert "Guide" in labels[2]                                       # guide posts come first among threads
    assert {p[1] for p in pl} >= set(e.threads.values())


def test_only_the_bots_own_messages_are_listed_and_editable(seeded):
    e = seeded
    p = fresh_reply(e, "oven", "bot says hi")
    e.approve_and_send(p)
    msgs = editor.load_bot_messages(e.client, e.ledger, e.threads["oven"])
    assert [m["content"] for m in msgs] == ["bot says hi"]              # Mira's and Jon's messages are not listed
    new = editor.save_message(e.client, e.ledger, e.threads["oven"], msgs[0], "bot says hello", "human:gui")
    assert new["content"] == "bot says hello"
    posted = e.ledger.load()["threads"][e.threads["oven"]]["triage"]["replies_posted"]
    assert posted[0]["content"] == "bot says hello"
    assert e.ledger.read_events()[-1]["action"] == "edit_message" and e.ledger.read_events()[-1]["actor"] == "human:gui"


def test_edit_validation_and_delete(seeded):
    e = seeded
    e.approve_and_send(fresh_reply(e, "oven", "to be removed"))
    (msg,) = editor.load_bot_messages(e.client, e.ledger, e.threads["oven"])
    with pytest.raises(HearbackError, match="2000"):
        editor.save_message(e.client, e.ledger, e.threads["oven"], msg, "x" * 2001, "human:gui")
    with pytest.raises(HearbackError, match="empty"):
        editor.save_message(e.client, e.ledger, e.threads["oven"], msg, "   ", "human:gui")
    editor.remove_message(e.client, e.ledger, e.threads["oven"], msg, "human:gui")
    assert editor.load_bot_messages(e.client, e.ledger, e.threads["oven"]) == []
    assert e.ledger.load()["threads"][e.threads["oven"]]["triage"]["replies_posted"] == []
    assert e.ledger.read_events()[-1]["action"] == "delete_message"


def test_window_smoke(seeded):
    tk = pytest.importorskip("tkinter")
    e = seeded
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available for Tk")
    root.withdraw()
    try:
        p = fresh_reply(e, "oven", "from the gui test")
        app = editor.create_app(e.cfg, e.ledger, e.queue, "human:gui", e.client, root)
        assert app.qlist.size() == 1
        app.qlist.selection_set(0)
        app.pick_proposal()
        assert p.id in app.preview.get("1.0", "end")
        app.qtext.delete("1.0", "end")
        app.qtext.insert("1.0", "polished in the gui")
        app.save_text()
        assert e.queue.get(p.id).text == "polished in the gui"
        app.approve()
        assert e.queue.get(p.id).state == "approved"
        e.queue.send(e.client, e.ledger, delay=0, sleep=lambda s: None)
        app.load_messages(e.threads["oven"])
        assert app.mlist.size() == 1
        app.mlist.selection_set(0)
        app.pick_message()
        assert app.mtext.get("1.0", "end-1c") == "polished in the gui"
        app.mtext.delete("1.0", "end")
        app.mtext.insert("1.0", "edited in tab two")
        app.save_message()
        assert e.fake.message(e.threads["oven"], e.queue.get(p.id).result["message_ids"][0])["content"] == "edited in tab two"
        root.update()
    finally:
        root.destroy()
