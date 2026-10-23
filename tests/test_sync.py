"""Incremental sync: records, skipping unchanged threads, deltas, bot replies, paging, attachments."""
from __future__ import annotations

import json

from conftest import fresh_reply, make_env
from hearback.fake import make_png
from hearback.sync import format_delta


def msg_fetches(e) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in e.fake.calls:
        if c.method == "GET" and c.path.endswith("/messages") and "/channels/" in c.path:
            cid = c.path.split("/")[2]
            out[cid] = out.get(cid, 0) + 1
    return out


def test_first_sync_builds_records(seeded):
    e = seeded
    data = e.ledger.load()
    assert set(data["threads"]) == set(e.threads.values())            # incl. the archived one (paged, page size 1)
    assert data["forum_id"] == e.forum and data["guild_id"] == e.fake.guild_id
    assert set(data["tags"].values()) >= {"Crash", "Free", "Full"}
    rec = data["threads"][e.threads["oven"]]
    assert rec["title"] == "Oven freezes" and rec["author"] == "Mira" and rec["tags"] == ["Crash", "Free"]
    assert rec["body"] == "Stops at 0:00" and rec["created"].endswith("Z") and rec["archived"] is False
    assert [r["author"] for r in rec["replies"]] == ["Jon"]           # starter message is the body, not a reply
    assert rec["triage"] == {"status": "new"}
    assert data["threads"][e.threads["typo"]]["archived"] is True
    assert data["bot_user_id"] == e.fake.bot["id"]


def test_attachments_are_downloaded_relative_to_the_data_dir(seeded):
    e = seeded
    rec = e.ledger.load()["threads"][e.threads["oven"]]
    (rel,) = rec["attachments"]
    assert rel.startswith("attachments/%s/" % e.threads["oven"]) and rel.endswith("_shot.png")
    assert (e.ledger.dir / rel).read_bytes() == make_png()
    assert e.ledger.abs_path(rel) == e.ledger.dir / rel
    assert e.fake.cdn_requests and not any("authorization" in k.lower() for _, h in e.fake.cdn_requests for k in h)


def test_non_media_attachments_are_skipped_and_no_download_keeps_url(tmp_path):
    e = make_env(tmp_path)
    t = e.fake.add_thread(e.forum, "t", "Mira", "b", [], files=[("a.apk", b"x" * 10, "application/vnd.android.package-archive"),
                                                               ("s.png", make_png())])
    d = e.sync(download=False)
    rec = e.ledger.load()["threads"][t]
    assert len(rec["attachments"]) == 1 and rec["attachments"][0].startswith("https://cdn.discordapp.com/")
    assert not (e.ledger.dir / "attachments").exists()
    assert d.new_threads[0].images == 1


def test_unchanged_threads_are_not_refetched(seeded):
    e = seeded
    e.fake.calls.clear()
    d = e.sync()
    assert msg_fetches(e) == {}                                       # no message fetch at all
    assert d.skipped == 4 and not d.entries
    assert format_delta(d).startswith("synced: 4 threads, 0 new, 0 with new replies, 0 tag changes, 4 unchanged")


def test_only_the_changed_thread_is_refetched_and_reported(seeded):
    e = seeded
    e.triage("oven", cluster="oven-timer")
    e.ledger.claim(e.threads["oven"], "agent:test")
    e.fake.add_reply(e.threads["flicker"], "Carol", "Also on Android 14")
    e.fake.calls.clear()
    d = e.sync()
    assert msg_fetches(e) == {e.threads["flicker"]: 1}
    assert [x.tid for x in d.with_replies] == [e.threads["flicker"]]
    assert d.entries[0].replies == [{"author": "Carol", "content": "Also on Android 14", "images": 0, "files": 0}]
    text = format_delta(d)
    assert "- Carol: Also on Android 14" in text and "0 new, 1 with new replies" in text
    # triage and claim survived; the unchanged thread was not touched
    rec = e.ledger.load()["threads"][e.threads["oven"]]
    assert rec["triage"]["cluster"] == "oven-timer" and rec["claim"]["by"] == "agent:test"


def test_sync_never_touches_triage_or_unknown_fields(seeded):
    e = seeded
    e.triage("flicker", cluster="c", root_cause="r")
    with e.ledger.locked():
        data = e.ledger.load()
        data["threads"][e.threads["flicker"]]["my_note"] = {"keep": True}
        e.ledger.save(data)
    e.fake.add_reply(e.threads["flicker"], "Carol", "bump")
    e.sync()
    rec = e.ledger.load()["threads"][e.threads["flicker"]]
    assert rec["my_note"] == {"keep": True}
    assert rec["triage"]["cluster"] == "c" and rec["triage"]["root_cause"] == "r" and rec["triage"]["rev"] == 1


def test_bots_own_replies_are_not_new_replies(seeded):
    e = seeded
    p = fresh_reply(e, "oven")
    e.approve_and_send(p)
    e.fake.calls.clear()
    d = e.sync()
    assert msg_fetches(e) == {e.threads["oven"]: 1}                   # the thread changed, so it was fetched...
    assert not d.entries                                               # ...but the bot's own reply is not news
    rec = e.ledger.load()["threads"][e.threads["oven"]]
    assert [r.get("bot") for r in rec["replies"]] == [None, True]
    # a human replying after the bot is news
    e.fake.add_reply(e.threads["oven"], "Mira", "Thanks!")
    d = e.sync()
    assert [r["author"] for r in d.entries[0].replies] == ["Mira"]


def test_tag_change_is_detected_and_refetches_that_thread(seeded):
    e = seeded
    e.fake.channels[e.threads["dark"]]["applied_tags"] = [e.fake.tag_id(e.forum, "UI"), e.fake.tag_id(e.forum, "Full")]
    e.fake.calls.clear()
    d = e.sync()
    assert msg_fetches(e) == {e.threads["dark"]: 1}
    (entry,) = d.tag_changes
    assert entry.tag_change == ([], ["UI", "Full"])
    assert "tags:  -> UI,Full" in format_delta(d)


def test_title_and_archive_changes_are_picked_up(seeded):
    e = seeded
    e.fake.channels[e.threads["dark"]]["name"] = "Dark mode (renamed)"
    e.fake.set_archived(e.threads["flicker"], True)
    e.sync()
    data = e.ledger.load()["threads"]
    assert data[e.threads["dark"]]["title"] == "Dark mode (renamed)" and data[e.threads["flicker"]]["archived"] is True


def test_threads_of_other_forums_are_ignored(seeded):
    e = seeded
    other = e.fake.add_forum("other", ["X"])
    e.fake.add_thread(other, "Not ours", "Eve", "b", ["X"])
    d = e.sync()
    assert d.total == 4 and d.skipped == 4


def test_more_than_100_replies_are_paged(tmp_path):
    e = make_env(tmp_path)
    t = e.fake.add_thread(e.forum, "Long", "Mira", "b", [])
    for i in range(130):
        e.fake.add_reply(t, "U%d" % (i % 3), "reply %d" % i, gap=1)
    e.sync()
    replies = e.ledger.load()["threads"][t]["replies"]
    assert len(replies) == 130 and replies[0]["content"] == "reply 0" and replies[-1]["content"] == "reply 129"
    assert e.fake.count("GET", r"/channels/%s/messages" % t) == 2


def test_json_output_and_empty_body_warning(tmp_path):
    e = make_env(tmp_path)
    e.fake.add_thread(e.forum, "t", "Mira", "", [])
    d = e.sync()
    assert any("MESSAGE CONTENT INTENT" in w for w in d.warnings)
    data = json.loads(format_delta(d, json_out=True))
    assert data["new"] == 1 and data["entries"][0]["title"] == "t"


def test_sync_writes_an_audit_event(seeded):
    ev = seeded.ledger.read_events()
    assert ev[-1]["action"] == "sync" and ev[-1]["detail"]["total"] == 4
