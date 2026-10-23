"""Regression tests for the review findings: mentions, tripwires, sync accuracy, downloads, claims, fences."""
from __future__ import annotations

import http.server
import json
import re
import threading
import time

import pytest

from conftest import fresh_reply, make_env
from hearback import cli, editor, mcp_server as ms, release, staff, templates
from hearback.config import Config, Variant, write_template
from hearback.discord import DiscordClient, DiscordError, DownloadTooLarge, msg_body, urllib_transport
from hearback.fake import FakeDiscord, make_png
from hearback.ledger import ClaimError, Ledger, LedgerError
from hearback.queue import Queue, QueueError
from hearback.sync import _unchanged
from hearback.util import HearbackError, split_text

ZW = chr(0x200B)
PING = re.compile(r"<[@#]|@(?:everyone|here)\b", re.I)         # anything Discord would turn into a mention
EVIL = "<@123456789> <@!42> <@&777> <#555> @everyone @here `tick`\nnew line"


def no_ping(text: str) -> bool:
    return not PING.search(text)


# ================================================================ 1. mentions

def test_msg_body_always_disables_mentions():
    assert msg_body("hi <@1> @everyone", [])["allowed_mentions"] == {"parse": []}
    assert msg_body("x", [{"filename": "a.png"}])["allowed_mentions"] == {"parse": []}
    assert msg_body("x", []) is not msg_body("x", [])


@pytest.mark.parametrize("raw", ["<@123>", "<@!123>", "<@&123>", "<#123>", "@everyone", "@here", "@EVERYONE", "x@here!"])
def test_sanitize_defangs_every_kind_of_ping(raw):
    out = templates.sanitize(raw)
    assert no_ping(out) and ZW in out and out.replace(ZW, "") == raw


def test_sanitize_strips_code_spans_newlines_and_caps_the_length():
    assert templates.sanitize("a`b``c\r\n\td\n\ne") == "abc d e"
    assert templates.sanitize("[click](http://evil.example)").count(ZW) == 1
    long = templates.sanitize("x" * 500)
    assert len(long) == 80 and long.endswith("…")
    assert templates.sanitize("y" * 500, 300).__len__() == 300
    assert templates.sanitize(None) == "" and templates.sanitize(42) == "42"
    assert templates.sanitize("Plain title, nothing special.") == "Plain title, nothing special."


def test_render_sanitizes_forum_values_but_not_trusted_ones():
    out = templates.render("ack", "en", author=EVIL, title="<@&9>`x`")
    assert no_ping(out) and "`" not in out and out.count("\n") == 0
    kept = templates.render("variant_block", "en", variant_name=templates.trusted("Free"),
                            download=templates.trusted("https://example.org/" + "a" * 120))
    assert "a" * 120 in kept
    assert "a" * 120 not in templates.render("variant_block", "en", variant_name="Free", download="a" * 120)


def hostile_release_env(tmp_path):
    e = make_env(tmp_path)
    e.threads["bad"] = e.fake.add_thread(e.forum, "Title %s" % EVIL.replace("\n", " "), "Evil %s" % EVIL.replace("\n", " "),
                                         "body", ["Free"])
    e.sync()
    e.triage("bad", status="fixed-pending-release", cluster="cluster <@&1> @everyone", variants=["free"],
             release_note="Fixed <@123> @here " + "n" * 400)
    return e


def test_release_notifications_and_announcement_cannot_ping(tmp_path):
    e = hostile_release_env(tmp_path)
    e.cfg.variants["free"] = Variant("free", "Free", "https://example.org/" + "d" * 100)
    plan = release.release("1.0", ["free"], e.cfg, e.ledger, e.queue, "agent:x")
    texts = [p.text for p in plan.proposals if p.kind in ("reply", "post")]
    assert len(texts) == 2 and all(no_ping(t) for t in texts)
    reply = next(p.text for p in plan.proposals if p.kind == "reply")
    assert "`" not in reply.replace("\n", " ") and "d" * 100 in reply         # config download text is not cut
    note = re.search(r"Fixed.*", reply).group(0)
    assert len(note) <= 300 and note.endswith("…")                            # release notes are capped, not unlimited


def test_cli_template_variables_are_sanitized_too(cli_ws):
    cli_ws.run("sync")
    rc, out, _ = cli_ws.run("propose", "reply", cli_ws.threads["oven"][-6:], "--template", "not_bug",
                            "--var", "explanation=read <@123> and @everyone")
    assert rc == 0 and no_ping(out) and "read <" in out


def test_staff_previews_defang_forum_text_and_cannot_break_the_fence(tmp_path):
    e = make_env(tmp_path)
    t = e.fake.add_thread(e.forum, "T " + EVIL.replace("\n", " ") + " ```", "Eve", "body", ["Free"])
    e.sync()
    e.queue.propose("reply", actor="agent:x", thread=t, text="hello <@123> @everyone ```break```", why="w <@5>")
    e.queue.propose("newpost", actor="agent:x", title="Guide <@&3> @here", text="body", add=["Free"])
    res = staff.push(e.client, e.queue, e.cfg, sleep=lambda s: None, data=e.ledger.load())
    assert len(res.pushed) == 2
    for msg in e.fake.messages[e.staff]:
        assert no_ping(msg["content"]) and msg["content"].count("```") == 2
    posts = [c for c in e.fake.calls if c.method == "POST" and c.path == "/channels/%s/messages" % e.staff]
    assert posts and all(c.body["allowed_mentions"] == {"parse": []} for c in posts)


def test_every_message_the_bot_writes_carries_allowed_mentions(seeded, tmp_path):
    e = seeded
    t = e.threads["oven"]
    long_text = "\n\n".join(("paragraph %d <@123> " % i) * 60 for i in range(5))
    props = [e.queue.propose("reply", actor="a", thread=t, text=long_text),
             e.queue.propose("post", actor="a", thread=e.announce, text="hi @everyone"),
             e.queue.propose("newpost", actor="a", title="Hello", text=long_text, add=["UI"])]
    assert not e.approve_and_send(*props).failed
    first = e.queue.get(props[0].id).result["message_ids"][0]
    assert not e.approve_and_send(e.queue.propose("edit", actor="a", thread=t, message_id=first, text="edit @here")).failed
    e.queue.propose("reply", actor="a", thread=e.threads["flicker"], text="for the staff channel")
    staff.push(e.client, e.queue, e.cfg, sleep=lambda s: None, data=e.ledger.load())
    (msg,) = editor.load_bot_messages(e.client, e.ledger, e.announce)
    editor.save_message(e.client, e.ledger, e.announce, msg, "changed in the editor @everyone", "human:gui")
    checked = 0
    for c in e.fake.writes():
        if c.method in ("POST", "PATCH") and re.fullmatch(r"/channels/\d+/messages(/\d+)?", c.path):
            assert c.body["allowed_mentions"] == {"parse": []}, (c.method, c.path)
            checked += 1
        elif c.method == "POST" and c.path.endswith("/threads"):
            assert c.body["message"]["allowed_mentions"] == {"parse": []}
            checked += 1
    assert checked >= 8


# ================================================================ 3. tripwire for shell agents

HUMAN_ONLY = [("queue", "approve", "1"), ("queue", "approve", "--all", "--yes"), ("queue", "reject", "1"),
              ("queue", "retry", "1"), ("send",), ("send", "--yes"), ("staff", "poll")]


@pytest.mark.parametrize("argv", HUMAN_ONLY, ids=lambda a: " ".join(a))
def test_agents_cannot_approve_reject_send_or_poll(cli_ws, argv):
    cli_ws.run("sync")
    cli_ws.run("propose", "reply", cli_ws.threads["oven"][-6:], "--text", "hello")
    writes = len(cli_ws.fake.writes())
    rc, out, err = cli_ws.run("--as", "agent:claude-1", *argv)
    assert rc == 1 and "human-only" in err and "agent:claude-1" in err and "token" in err and "tripwire" in err
    assert len(cli_ws.fake.writes()) == writes
    rc, out, _ = cli_ws.run("queue", "list", "--state", "pending")
    assert "(1 proposals)" in out                                   # still pending: nothing was decided


def test_the_actor_environment_variable_trips_the_wire_too(cli_ws, monkeypatch):
    cli_ws.run("sync")
    cli_ws.run("propose", "reply", cli_ws.threads["oven"][-6:], "--text", "hello")
    monkeypatch.setenv("HEARBACK_ACTOR", "agent:from-env")
    rc, out, err = cli_ws.run("queue", "approve", "1")
    assert rc == 1 and "human-only" in err
    monkeypatch.setenv("HEARBACK_ACTOR", "human:owner")
    rc, out, err = cli_ws.run("queue", "approve", "1")
    assert rc == 0 and "approved p-" in out


def test_agents_can_still_propose_and_read(cli_ws):
    cli_ws.run("sync")
    for argv in (("list",), ("show", cli_ws.threads["oven"][-6:]), ("queue", "list"),
                 ("propose", "reply", cli_ws.threads["oven"][-6:], "--text", "ok"), ("log",)):
        rc, out, err = cli_ws.run("--as", "agent:x", *argv)
        assert rc == 0, (argv, err)


def test_approve_all_shows_everything_and_needs_yes(cli_ws):
    cli_ws.run("sync")
    a, b = cli_ws.threads["oven"][-6:], cli_ws.threads["flicker"][-6:]
    cli_ws.run("propose", "reply", a, "--text", "first proposal text")
    cli_ws.run("propose", "tags", b, "--add", "In progress")
    rc, out, _ = cli_ws.run("queue", "approve", "--all")
    assert rc == 0 and "first proposal text" in out and "tags: " in out and "NOTHING was approved" in out
    assert "--all --yes" in out and out.count("p-") >= 2
    rc, out, _ = cli_ws.run("queue", "list", "--state", "pending")
    assert "(2 proposals)" in out
    rc, out, _ = cli_ws.run("queue", "approve", "--all", "--yes")
    assert rc == 0 and "first proposal text" in out and out.count("approved p-") == 2
    rc, out, _ = cli_ws.run("queue", "list", "--state", "approved")
    assert "(2 proposals)" in out


def test_approving_by_id_does_not_need_yes(cli_ws):
    cli_ws.run("sync")
    cli_ws.run("propose", "reply", cli_ws.threads["oven"][-6:], "--text", "x")
    rc, out, _ = cli_ws.run("queue", "approve", "1")
    assert rc == 0 and "approved p-" in out


# ================================================================ 4. attachments from Hearback's own files (CLI)

def test_cli_refuses_to_attach_the_ledger_the_token_file_or_the_config(cli_ws):
    cli_ws.run("sync")
    token = cli_ws.root / ".hearback" / "token.txt"
    token.write_text("not-a-real-token", encoding="utf-8")
    t = cli_ws.threads["oven"][-6:]
    for target in (cli_ws.root / ".hearback" / "ledger.json", token, cli_ws.root / "hearback.toml",
                   cli_ws.root / "." / ".hearback" / "events.jsonl"):
        rc, out, err = cli_ws.run("propose", "reply", t, "--text", "see file", "--attach", str(target))
        assert rc == 1 and "inside Hearback's own files" in err, target
        rc, out, err = cli_ws.run("propose", "post", cli_ws.announce, "--text", "see file", "--attach", str(target))
        assert rc == 1 and "inside Hearback's own files" in err
    ok = cli_ws.root / "screenshot.png"
    ok.write_bytes(make_png())
    rc, out, err = cli_ws.run("propose", "reply", t, "--text", "see file", "--attach", str(ok))
    assert rc == 0, err
    rc, out, _ = cli_ws.run("queue", "list")
    assert "(1 proposals)" in out


def test_a_custom_token_file_location_is_protected_too(tmp_path):
    secret = tmp_path / "elsewhere" / "bot-token.txt"
    secret.parent.mkdir()
    secret.write_text("tok", encoding="utf-8")
    cfg = Config(root=tmp_path, token_file=str(secret))
    assert secret in cfg.protected_paths() and cfg.data_dir in cfg.protected_paths()
    q = Queue(cfg.queue_dir, protected=cfg.protected_paths())
    with pytest.raises(QueueError, match="inside Hearback's own files"):
        q.propose("post", actor="a", thread="1" * 16, text="x", attachments=[secret])


# ================================================================ 5. staff poll vets the message

@pytest.fixture
def staffed(seeded):
    e = seeded
    e.dana, e.troll = e.fake.add_user("Dana"), e.fake.add_user("Troll")
    e.cfg.approvers = [e.dana["id"]]
    return e


def push_all(e):
    return staff.push(e.client, e.queue, e.cfg, sleep=lambda s: None, data=e.ledger.load()).pushed


def test_a_swapped_message_id_pointing_at_a_human_message_is_not_trusted(staffed):
    e = staffed
    p = fresh_reply(e, "oven", "dangerous text")
    push_all(e)
    fake_mid = e.fake.add_reply(e.staff, "Dana", "looks fine to me: %s" % p.id)          # a human message, not the bot's
    e.fake.react(e.staff, fake_mid, "✅", e.dana["id"])
    q = e.queue.get(p.id)
    q.staff_message_id = fake_mid                                                       # someone edits the proposal file
    e.queue.update(q)
    res = staff.poll(e.client, e.queue, e.cfg)
    assert res.approved == [] and res.stale == [p.id] and "not posted by this bot" in res.errors[0]
    assert e.queue.get(p.id).state == "pending" and e.queue.get(p.id).staff_message_id == ""
    assert "stale" in "\n".join(res.lines())


def test_another_proposals_staff_message_cannot_approve_this_one(staffed):
    e = staffed
    a, b = fresh_reply(e, "oven", "first"), fresh_reply(e, "flicker", "second")
    push_all(e)
    approved_message = e.queue.get(a.id).staff_message_id
    e.fake.react(e.staff, approved_message, "✅", e.dana["id"])
    q = e.queue.get(b.id)
    q.staff_message_id = approved_message                                               # point b at a's approved message
    e.queue.update(q)
    res = staff.poll(e.client, e.queue, e.cfg)
    assert b.id not in res.approved and "does not carry that proposal id" in " ".join(res.errors)
    assert e.queue.get(b.id).state == "pending"


def test_the_digest_in_the_message_must_match_the_proposal_even_if_the_metadata_is_forged(staffed):
    e = staffed
    p = fresh_reply(e, "oven", "original wording")
    push_all(e)
    e.fake.react(e.staff, e.queue.get(p.id).staff_message_id, "✅", e.dana["id"])
    q = e.queue.get(p.id)
    q.text = "malicious rewrite"                                                        # edit the file directly...
    q.meta["staff_digest"] = staff._digest(e.queue, q)                                  # ...and forge the stored digest
    e.queue.update(q)
    res = staff.poll(e.client, e.queue, e.cfg)
    assert res.approved == [] and "was changed after its staff message was posted" in res.errors[0]


# ================================================================ 6. sync accuracy

def msg_gets(e) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in e.fake.calls:
        m = re.fullmatch(r"/channels/(\d+)/messages", c.path)
        if c.method == "GET" and m:
            out[m.group(1)] = out.get(m.group(1), 0) + 1
    return out


def test_a_deleted_middle_message_changes_message_count_and_forces_a_refetch(seeded):
    e = seeded
    oven = e.threads["oven"]
    jon = e.ledger.load()["threads"][oven]["replies"][0]["id"]
    e.fake.add_reply(oven, "Carol", "later reply")
    e.sync()
    assert [r["author"] for r in e.ledger.load()["threads"][oven]["replies"]] == ["Jon", "Carol"]
    e.client.request("DELETE", "/channels/%s/messages/%s" % (oven, jon))               # last_message_id does not change
    e.fake.calls.clear()
    d = e.sync()
    assert msg_gets(e) == {oven: 1} and d.skipped == 3
    assert [r["author"] for r in e.ledger.load()["threads"][oven]["replies"]] == ["Carol"]


def test_unchanged_compares_message_count_and_total_message_sent():
    old = {"last_message_id": "5", "tags": [], "title": "t", "archived": False, "message_count": 1,
           "total_message_sent": 2}
    t = {"last_message_id": "5", "name": "t", "message_count": 1, "total_message_sent": 2,
         "thread_metadata": {"archived": False}}
    assert _unchanged(old, t, [])
    assert not _unchanged(old, {**t, "message_count": 0}, [])
    assert not _unchanged(old, {**t, "total_message_sent": 3}, [])
    assert _unchanged(old, {k: v for k, v in t.items() if k != "total_message_sent"}, [])    # API did not send it


def test_full_sync_refetches_everything_and_sees_edits_the_cheap_check_cannot(seeded):
    e = seeded
    oven = e.threads["oven"]
    mid = e.ledger.load()["threads"][oven]["replies"][0]["id"]
    e.fake.message(oven, mid)["content"] = "Jon edited this later"                    # no id or count change
    e.fake.calls.clear()
    e.sync()
    assert msg_gets(e) == {} and e.ledger.load()["threads"][oven]["replies"][0]["content"] == "Same here"
    e.fake.calls.clear()
    d = e.sync(full=True)
    assert set(msg_gets(e)) == set(e.threads.values()) and d.skipped == 0
    assert e.ledger.load()["threads"][oven]["replies"][0]["content"] == "Jon edited this later"
    assert [x.tid for x in d.entries] == []                                           # an edit is not a "new reply"


def test_full_sync_keeps_triage_and_claims(seeded):
    e = seeded
    e.triage("oven", cluster="c")
    e.ledger.claim(e.threads["oven"], "agent:test")
    e.sync(full=True)
    rec = e.ledger.load()["threads"][e.threads["oven"]]
    assert rec["triage"]["cluster"] == "c" and rec["claim"]["by"] == "agent:test"


def test_cli_sync_full_flag(cli_ws):
    cli_ws.run("sync")
    cli_ws.fake.calls.clear()
    rc, out, _ = cli_ws.run("sync", "--full")
    assert rc == 0 and out.startswith("synced: 2 threads, 0 new") and "0 unchanged" in out
    assert sum(1 for c in cli_ws.fake.calls if c.path.endswith("/messages")) == 2


def test_failed_downloads_keep_the_url_and_are_flagged_until_a_full_sync_succeeds(tmp_path):
    e = make_env(tmp_path)
    t = e.fake.add_thread(e.forum, "t", "Mira", "b", [], files=[("s.png", make_png())])
    e.fake.add_reply(t, "Jon", "also", files=[("r.png", make_png(8, 8))])
    saved = dict(e.fake.blobs)
    e.fake.blobs.clear()                                                              # the CDN now answers 404
    d = e.sync()
    rec = e.ledger.load()["threads"][t]
    assert rec["download_failed"] is True and rec["replies"][0]["download_failed"] is True
    assert rec["attachments"][0].startswith("https://cdn.discordapp.com/")
    assert rec["replies"][0]["attachments"][0].startswith("https://cdn.discordapp.com/")
    assert any("2 attachment download(s) failed" in w and "full sync" in w for w in d.warnings)
    e.fake.blobs.update(saved)
    e.sync()                                                                          # nothing changed: not retried
    assert e.ledger.load()["threads"][t]["download_failed"] is True
    e.sync(full=True)                                                                 # retried
    rec = e.ledger.load()["threads"][t]
    assert "download_failed" not in rec and "download_failed" not in rec["replies"][0]
    assert rec["attachments"][0].startswith("attachments/") and (e.ledger.dir / rec["attachments"][0]).is_file()


def test_not_downloading_by_choice_is_not_a_failure(tmp_path):
    e = make_env(tmp_path)
    t = e.fake.add_thread(e.forum, "t", "Mira", "b", [], files=[("s.png", make_png())])
    e.sync(download=False)
    assert "download_failed" not in e.ledger.load()["threads"][t]


# ================================================================ 7. downloads

def test_only_images_and_plain_logs_are_downloaded(tmp_path):
    e = make_env(tmp_path)
    files = [("a.png", make_png()), ("b.JPG", b"jpg"), ("c.txt", b"t"), ("d.log", b"l"), ("e.json", b"{}"),
             ("f.zip", b"zip"), ("g.mp4", b"mp4"), ("h.html", b"<script>"), ("i.pdf", b"%PDF"), ("j.xml", b"<x/>"),
             ("k", b"no extension")]
    t = e.fake.add_thread(e.forum, "t", "Mira", "b", [], files=[(n, d, "text/plain") for n, d in files])
    e.sync()
    names = [a.rsplit("_", 1)[-1] for a in e.ledger.load()["threads"][t]["attachments"]]
    assert sorted(names) == ["a.png", "b.JPG", "c.txt", "d.log", "e.json"]
    assert len(e.fake.cdn_requests) == 5                                              # nothing else was even requested


def test_download_passes_the_cap_to_the_transport_and_rechecks_the_result(tmp_path):
    seen = []

    def transport(*a):
        seen.append(a)
        return 200, b"x" * 50

    c = DiscordClient("tok", transport=transport)
    c.download("https://cdn.discordapp.com/a/b/c.png", tmp_path / "ok.png", max_bytes=100)
    assert seen[0][5] == 100 and (tmp_path / "ok.png").read_bytes() == b"x" * 50
    with pytest.raises(DiscordError, match="too large"):        # a transport that ignores the cap is still caught
        c.download("https://cdn.discordapp.com/a/b/c.png", tmp_path / "big.png", max_bytes=10)
    assert not (tmp_path / "big.png").exists()

    def stops_early(*a):
        raise DownloadTooLarge("body went past the cap")

    with pytest.raises(DiscordError, match=r"too large \(over 10 B\): big2.png"):
        DiscordClient("tok", transport=stops_early).download("https://cdn.discordapp.com/a/b/c.png",
                                                             tmp_path / "big2.png", max_bytes=10)


class _Blast(http.server.BaseHTTPRequestHandler):
    """Streams up to server.total bytes (optionally declaring them in Content-Length) and counts what it sent."""

    def do_GET(self):
        self.send_response(200)
        if self.server.declare:
            self.send_header("Content-Length", str(self.server.total))
        self.send_header("Connection", "close")
        self.end_headers()
        chunk = b"x" * 65536
        try:
            while self.server.sent < self.server.total:
                self.wfile.write(chunk)
                self.wfile.flush()
                self.server.sent += len(chunk)
        except OSError:
            pass

    def log_message(self, *a):
        pass


@pytest.fixture
def blast_server(monkeypatch):
    monkeypatch.setenv("no_proxy", "*")                       # loopback only: never via a proxy
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Blast)
    srv.sent, srv.total, srv.declare = 0, 64 * 1024 * 1024, False
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def settle(srv) -> int:
    last = -1
    while srv.sent != last:
        last = srv.sent
        time.sleep(0.25)
    return srv.sent


def test_the_real_transport_stops_reading_once_the_cap_is_passed(blast_server):
    url = "http://127.0.0.1:%d/big" % blast_server.server_address[1]
    with pytest.raises(DownloadTooLarge):
        urllib_transport("GET", url, {}, None, 10, 256 * 1024)
    assert settle(blast_server) < blast_server.total // 4      # the server was abandoned long before it finished


def test_the_real_transport_refuses_a_declared_oversize_body_without_reading_it(blast_server):
    blast_server.declare = True
    url = "http://127.0.0.1:%d/big" % blast_server.server_address[1]
    with pytest.raises(DownloadTooLarge, match="declared size"):
        urllib_transport("GET", url, {}, None, 10, 256 * 1024)
    assert settle(blast_server) < blast_server.total // 4


def test_the_real_transport_returns_small_bodies_and_uncapped_calls_still_work(blast_server):
    blast_server.total = 3 * 65536
    url = "http://127.0.0.1:%d/small" % blast_server.server_address[1]
    status, body = urllib_transport("GET", url, {}, None, 10, 10 * 1024 * 1024)
    assert status == 200 and len(body) == 3 * 65536
    blast_server.sent = 0
    status, body = urllib_transport("GET", url, {}, None, 10)
    assert status == 200 and len(body) == 3 * 65536


# ================================================================ 8. full ids

def test_cli_list_and_sync_show_full_thread_ids(cli_ws):
    rc, out, _ = cli_ws.run("sync")
    assert cli_ws.threads["oven"] in out
    rc, out, _ = cli_ws.run("list")
    assert all(any(line.startswith(tid + " ") for line in out.splitlines()) for tid in cli_ws.threads.values())


def test_ambiguous_suffix_lists_the_full_ids(tmp_path):
    ledger = Ledger(tmp_path / ".hearback" / "ledger.json")
    with ledger.locked():
        d = ledger.load()
        for tid in ("1000000000000123", "2000000000000123"):
            d["threads"][tid] = {"title": "t", "replies": []}
        ledger.save(d)
    with pytest.raises(LedgerError) as ei:
        ledger.resolve("123")
    assert "use the full id" in str(ei.value) and "1000000000000123" in str(ei.value) and "2000000000000123" in str(ei.value)


# ================================================================ 9. claims on triage, variants

def test_update_triage_respects_other_actors_claims(seeded):
    e = seeded
    tid = e.threads["oven"]
    e.ledger.claim(tid, "agent:a")
    with pytest.raises(ClaimError, match="claimed by agent:a"):
        e.ledger.update_triage(tid, {"status": "wontfix"}, actor="agent:b")
    assert e.ledger.update_triage(tid, {"status": "investigating"}, actor="agent:a")["rev"] == 1       # the holder
    assert e.ledger.update_triage(tid, {"status": "wontfix"}, actor="human:owner", force_claim=True)["rev"] == 2
    e.clock.advance(minutes=45)                                                                         # lease expired
    assert e.ledger.update_triage(tid, {"status": "need-info"}, actor="agent:b")["rev"] == 3


def mcp_tools(e, actor):
    return ms.Tools(e.cfg, e.ledger, e.queue, actor, lambda cfg: e.client)


def test_mcp_update_triage_is_refused_while_another_agent_holds_the_claim(seeded):
    e = seeded
    tid = e.threads["oven"]
    a, b = mcp_tools(e, "agent:a"), mcp_tools(e, "agent:b")
    a.claim(tid, 30)
    with pytest.raises(HearbackError) as ei:
        b.update_triage(tid, {"status": "wontfix"}, 0)
    assert "claimed by agent:a" in str(ei.value) and "force" not in str(ei.value)
    assert a.update_triage(tid, {"status": "investigating"}, 0)["rev"] == 1


def test_variants_are_validated_against_the_configuration(seeded):
    e = seeded
    tid = e.threads["oven"]
    t = mcp_tools(e, "agent:a")
    with pytest.raises(HearbackError) as ei:
        t.update_triage(tid, {"variants": ["free", "gold"]}, 0)
    assert "unknown variant(s): gold" in str(ei.value) and "free, full" in str(ei.value)
    with pytest.raises(HearbackError, match="unknown variant"):
        t.update_triage(tid, {"variants": "free,gold"}, 0)
    assert t.update_triage(tid, {"variants": ["free", "full"]}, 0)["triage"]["variants"] == ["free", "full"]
    assert t.update_triage(tid, {"variants": "free"}, 1)["triage"]["variants"] == ["free"]
    assert t.update_triage(tid, {"variants": None}, 2)["rev"] == 3                                    # removing is fine
    e.cfg.variants.clear()
    assert t.update_triage(tid, {"variants": ["anything"]}, 3)["rev"] == 4                            # none configured


def test_release_with_a_variant_filter_skips_threads_of_unknown_variant(tmp_path):
    e = make_env(tmp_path)
    t_known = e.fake.add_thread(e.forum, "Known", "Mira", "b", ["Free"])
    t_blind = e.fake.add_thread(e.forum, "No variant info", "Jon", "b", ["Crash"])
    e.threads.update(known=t_known, blind=t_blind)
    e.sync()
    for key in ("known", "blind"):
        e.triage(key, status="fixed-pending-release", cluster="c")
    plan = release.release("1.0", ["free"], e.cfg, e.ledger, e.queue, "agent:x")
    notified = {p.thread for p in plan.proposals if p.kind == "reply"}
    assert notified == {t_known}
    assert (t_blind, "no variant info") in plan.skipped
    assert any("NOT notified" in n and t_blind in n for n in plan.notes)
    everyone = release.release("2.0", [], e.cfg, e.ledger, e.queue, "agent:x")                       # no filter: everyone
    assert {p.thread for p in everyone.proposals if p.kind == "reply"} == {t_known, t_blind}


def test_finalize_works_even_if_an_agent_still_holds_a_claim(seeded):
    e = seeded
    tid = e.threads["oven"]
    e.triage("oven", status="fixed-pending-release", cluster="c", variants=["free"])
    plan = release.release("1.0", ["free"], e.cfg, e.ledger, e.queue, "agent:x")
    e.approve_and_send(*plan.proposals)
    e.ledger.claim(tid, "agent:slowpoke")
    rep = release.finalize("1.0", e.ledger, e.queue, "human:owner")
    assert rep.finalized == [tid] and e.ledger.load()["threads"][tid]["triage"]["status"] == "released"


# ================================================================ 10. the data dir ignores itself

def test_data_dir_gets_its_gitignore_whenever_anything_creates_it(tmp_path):
    for n, make in enumerate((
        lambda d: _first_ledger_write(d),
        lambda d: _first_event(d),
        lambda d: _first_proposal(d),
        lambda d: Config(root=d).ensure_data_dir(),
        lambda d: write_template(d),
    )):
        d = tmp_path / ("case%d" % n)
        d.mkdir()
        make(d)
        assert (d / ".hearback" / ".gitignore").read_text(encoding="utf-8") == "*\n", n


def _first_ledger_write(d):
    ledger = Ledger(d / ".hearback" / "ledger.json")
    with ledger.locked():
        ledger.save(ledger.load())


def _first_event(d):
    Ledger(d / ".hearback" / "ledger.json").event("a", "x")


def _first_proposal(d):
    Queue(d / ".hearback" / "queue").propose("post", actor="a", thread="1" * 16, text="hello")


def test_an_existing_gitignore_is_left_alone(tmp_path):
    (tmp_path / ".hearback").mkdir()
    (tmp_path / ".hearback" / ".gitignore").write_text("token.txt\n", encoding="utf-8")
    Config(root=tmp_path).ensure_data_dir()
    Ledger(tmp_path / ".hearback" / "ledger.json").event("a", "x")
    assert (tmp_path / ".hearback" / ".gitignore").read_text(encoding="utf-8") == "token.txt\n"


def test_cli_commands_that_write_create_it_without_init(cli_ws):
    import shutil
    shutil.rmtree(cli_ws.root / ".hearback", ignore_errors=True)
    cli_ws.run("sync")
    assert (cli_ws.root / ".hearback" / ".gitignore").read_text(encoding="utf-8") == "*\n"


# ================================================================ 11. urls and code fences

def test_get_thread_url_uses_the_ledger_guild_then_the_config_then_is_omitted(seeded):
    e = seeded
    tid = e.threads["oven"]
    t = mcp_tools(e, "agent:a")
    assert t.get_thread(tid)["url"] == "https://discord.com/channels/%s/%s" % (e.fake.guild_id, tid)
    with e.ledger.locked():
        d = e.ledger.load()
        d["guild_id"] = ""
        e.ledger.save(d)
    assert t.get_thread(tid)["url"] == "https://discord.com/channels/%s/%s" % (e.cfg.guild_id, tid)   # from the config
    e.cfg.guild_id = ""
    rec = t.get_thread(tid)
    assert "url" not in rec and rec["id"] == tid and "@me" not in json.dumps(rec)


def test_cli_show_does_not_invent_a_guild(cli_ws):
    cli_ws.run("sync")
    with Ledger(cli_ws.root / ".hearback" / "ledger.json").locked() as led:
        d = led.load()
        d["guild_id"] = ""
        led.save(d)
    rc, out, _ = cli_ws.run("show", cli_ws.threads["oven"][-6:])
    assert "@me" not in out and ("discord.com/channels/%s/" % cli_ws.fake.guild_id) in out      # falls back to the config


def balanced(chunk: str) -> bool:
    return sum(1 for line in chunk.split("\n") if line.count("```") % 2) % 2 == 0


def code_block(n: int) -> str:
    return "Intro paragraph.\n\n```python\n" + "\n".join("value_%03d = %d  # some code" % (i, i * i) for i in range(n)) + \
        "\n\nafter_blank_line = True\n```\n\nClosing words."


def test_split_text_never_leaves_a_code_fence_open_across_chunks():
    text = code_block(250)
    chunks = split_text(text)
    assert len(chunks) >= 3 and all(len(c) <= 2000 for c in chunks)
    assert all(balanced(c) for c in chunks)
    assert chunks[1].startswith("```python\n") and chunks[0].rstrip().endswith("```")      # closed, then re-opened with the language
    original_lines = [ln for ln in text.split("\n") if ln.startswith(("value_", "after_"))]
    got = [ln for c in chunks for ln in c.split("\n") if ln.startswith(("value_", "after_"))]
    assert got == original_lines                                                             # nothing lost or reordered
    assert chunks[-1].rstrip().endswith("Closing words.") and balanced(chunks[-1])


def test_split_text_without_fences_is_unchanged_and_an_authors_own_unbalanced_fence_is_left_alone():
    plain = "\n\n".join(("paragraph %d " % i) * 80 for i in range(8))
    assert all(len(c) <= 2000 for c in split_text(plain)) and "```" not in "".join(split_text(plain))
    short = "```\nunclosed but short\n"
    assert split_text(short) == ["```\nunclosed but short"]
    unbalanced_tail = ("word " * 600) + "\n\n```\nthe author never closed this"
    last = split_text(unbalanced_tail)[-1]
    assert last.endswith("never closed this")                                              # no fence appended to the end


def test_split_text_handles_inline_triple_backticks_and_untagged_fences():
    text = "inline ```code``` here\n\n" + "```\n" + "\n".join("row %d" % i for i in range(600)) + "\n```"
    chunks = split_text(text)
    assert all(len(c) <= 2000 and balanced(c) for c in chunks)
    assert chunks[1].startswith("```\n")


# ================================================================ 12. install hint

def test_install_hint_does_not_pretend_there_is_a_pypi_release():
    hint = ms.EXTRA_HINT
    assert 'pip install -e ".[mcp]"' in hint and "not on PyPI" in hint
    assert 'pip install "hearback-desk[mcp] @ git+https://github.com/Cynthia-wlan9509/hearback"' in hint
    assert "pip install 'hearback-desk[mcp]'" not in hint


def test_the_ignore_file_never_lands_in_an_existing_project_folder(tmp_path):
    project = tmp_path / "my-game"                       # an existing folder someone points Hearback at
    project.mkdir()
    ledger = Ledger(project / "ledger.json")
    with ledger.locked():
        ledger.save(ledger.load())
    ledger.event("a", "x")
    Queue(project / "queue").propose("post", actor="a", thread="1" * 16, text="hello")
    assert not (project / ".gitignore").exists()         # '*' there would hide the whole project from git
    fresh = Ledger(tmp_path / "brand-new" / "state" / "ledger.json")
    fresh.event("a", "x")
    assert (tmp_path / "brand-new" / "state" / ".gitignore").read_text(encoding="utf-8") == "*\n"


def test_a_hand_made_data_dir_gets_its_ignore_file_before_a_token_is_put_in_it(tmp_path):
    (tmp_path / ".hearback").mkdir()
    (tmp_path / ".hearback" / "token.txt").write_text("secret", encoding="utf-8")
    Ledger(tmp_path / ".hearback" / "ledger.json").event("a", "x")
    assert (tmp_path / ".hearback" / ".gitignore").read_text(encoding="utf-8") == "*\n"
