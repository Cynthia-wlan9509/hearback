"""Approval queue: state machine, rendering, idempotent send, failure handling, verify."""
from __future__ import annotations

import json
import os
import shutil

import pytest

from conftest import fresh_reply, make_env
from hearback import actions
from hearback.ledger import ClaimError
from hearback.queue import Proposal, QueueError
from hearback.util import MSG_LIMIT


def posted_ids(e, key="oven"):
    return [m["id"] for m in e.fake.messages[e.threads[key]] if m["author"]["id"] == e.fake.bot["id"]]


# ---------------------------------------------------------------- propose

def test_proposals_get_sequential_ids_and_one_file_each(seeded):
    e = seeded
    a, b = fresh_reply(e, "oven", "one"), fresh_reply(e, "flicker", "two")
    assert a.id.endswith("-0001") and b.id.endswith("-0002") and a.id.startswith("p-")
    files = sorted(p.name for p in (e.cfg.queue_dir / "pending").iterdir())
    assert files == [a.id + ".json", b.id + ".json"]
    saved = json.loads((e.cfg.queue_dir / "pending" / (a.id + ".json")).read_text(encoding="utf-8"))
    assert saved["kind"] == "reply" and saved["state"] == "pending" and saved["actor"] == "agent:test"
    assert saved["thread"] == e.threads["oven"] and saved["why"] == "test"


def test_nothing_is_sent_by_proposing(seeded):
    e = seeded
    e.fake.calls.clear()
    fresh_reply(e)
    e.queue.propose("tags", actor="agent:test", thread=e.threads["oven"], add=["In progress"])
    assert e.fake.writes() == []


@pytest.mark.parametrize("kw,msg", [
    (dict(kind="nope", thread="x"), "unknown kind"),
    (dict(kind="reply", thread="oven", text="  "), "text or an attachment"),
    (dict(kind="reply", thread=""), "needs a target"),
    (dict(kind="tags", thread="oven"), "at least one tag"),
    (dict(kind="newpost", title="", text="x"), "needs a title"),
    (dict(kind="newpost", title="t" * 101, text="x"), "Discord allows 100"),
    (dict(kind="newpost", title="t", text="x", add=list("abcdef")), "allows 5"),
    (dict(kind="edit", thread="oven", message_id="1", text="x" * 2001), "at most 2000"),
    (dict(kind="edit", thread="oven", text="x"), "needs a message id"),
    (dict(kind="delete", thread="oven"), "needs a message id"),
])
def test_propose_validation(seeded, kw, msg):
    e = seeded
    kw = dict(kw)
    kw["thread"] = e.threads.get(kw.get("thread"), kw.get("thread", ""))
    with pytest.raises(QueueError, match=msg):
        e.queue.propose(kw.pop("kind"), actor="agent:test", **kw)


def test_propose_rejects_missing_attachment(seeded, tmp_path):
    with pytest.raises(Exception, match="not found"):
        seeded.queue.propose("reply", actor="a", thread=seeded.threads["oven"], text="x",
                             attachments=[tmp_path / "nope.png"])


def test_identical_proposal_is_refused_even_after_it_was_sent(seeded):
    e = seeded
    p = fresh_reply(e)
    with pytest.raises(QueueError, match="identical proposal already exists"):
        fresh_reply(e)
    e.approve_and_send(p)
    with pytest.raises(QueueError, match="identical"):
        fresh_reply(e)
    assert fresh_reply(e, text="different") and e.queue.propose(
        "reply", actor="a", thread=e.threads["oven"], text="Hello Mira", force=True)


def test_claim_by_someone_else_blocks_proposals_unless_forced(seeded):
    e = seeded
    e.ledger.claim(e.threads["oven"], "agent:codex-2")
    with pytest.raises(ClaimError, match="codex-2"):
        e.queue.propose("reply", actor="agent:claude-1", thread=e.threads["oven"], text="x")
    assert e.queue.propose("reply", actor="agent:codex-2", thread=e.threads["oven"], text="mine")
    assert e.queue.propose("reply", actor="agent:claude-1", thread=e.threads["oven"], text="x", force=True)


def test_short_thread_ids_resolve_at_propose_time(seeded):
    e = seeded
    p = e.queue.propose("reply", actor="a", thread=e.threads["oven"][-6:], text="hi")
    assert p.thread == e.threads["oven"]


# ---------------------------------------------------------------- state machine

def test_approve_reject_move_files_between_state_dirs(seeded):
    e = seeded
    a, b = fresh_reply(e, "oven", "one"), fresh_reply(e, "flicker", "two")
    e.queue.approve([a.id], "human:owner")
    e.queue.reject([b.id], "human:owner", "not now")
    q = e.cfg.queue_dir
    assert (q / "approved" / (a.id + ".json")).exists() and not (q / "pending" / (a.id + ".json")).exists()
    assert (q / "rejected" / (b.id + ".json")).exists()
    pa = e.queue.get(a.id)
    assert pa.state == "approved" and pa.approved_by == "human:owner" and pa.approved_at
    assert e.queue.get(b.id).reject_reason == "not now"
    assert [p.id for p in e.queue.list("approved")] == [a.id] and len(e.queue.list()) == 2
    with pytest.raises(QueueError, match="only pending"):
        e.queue.approve([b.id], "human:owner")
    assert e.queue.approve([a.id], "human:owner")[0].state == "approved"      # no-op, not an error


def test_get_by_suffix_and_ambiguity(seeded):
    e = seeded
    a, b = fresh_reply(e, "oven", "one"), fresh_reply(e, "flicker", "two")
    assert e.queue.get("0002").id == b.id and e.queue.get("1").id == a.id
    with pytest.raises(QueueError, match="no proposal"):
        e.queue.get("0099")


def test_set_text_only_while_pending(seeded):
    e = seeded
    p = fresh_reply(e)
    e.queue.set_text(p.id, "Polished wording", "human:owner")
    assert e.queue.get(p.id).text == "Polished wording" and e.queue.get(p.id).meta["edited_by"] == "human:owner"
    e.queue.approve([p.id], "human:owner")
    with pytest.raises(QueueError, match="only pending"):
        e.queue.set_text(p.id, "late edit", "human:owner")


def test_audit_log_records_every_step(seeded):
    e = seeded
    p = fresh_reply(e)
    e.approve_and_send(p)
    actions = [(x["actor"], x["action"]) for x in e.ledger.read_events() if x["action"] in ("propose", "approve", "send")]
    assert actions == [("agent:test", "propose"), ("human:test", "approve"), ("human:test", "send")]


# ---------------------------------------------------------------- render

def test_render_shows_exactly_what_will_be_posted(seeded):
    e = seeded
    long_text = ("para one " * 150) + "\n\n" + ("para two " * 150)
    p = fresh_reply(e, "oven", long_text)
    out = e.queue.render(p)
    assert "reply in thread" in out and "Oven freezes" in out and "why: test" in out
    assert "message 1/2" in out and "message 2/2" in out        # > 2000 characters becomes two messages
    p2 = e.queue.propose("tags", actor="a", thread=e.threads["oven"], add=["In progress"], remove=["Crash"])
    assert "Crash,Free -> Free,In progress" in e.queue.render(p2)
    p3 = e.queue.propose("newpost", actor="a", title="Guide", text="Body", add=["UI"], pin=True)
    assert 'new forum post "Guide"' in e.queue.render(p3) and "pin: yes" in e.queue.render(p3)


# ---------------------------------------------------------------- send

def test_send_posts_records_and_verifies(seeded):
    e = seeded
    p = fresh_reply(e, "oven", "Hello Mira")
    rep = e.approve_and_send(p)
    sent = e.queue.get(p.id)
    assert sent.state == "sent" and sent.result["verified"] is True and len(sent.result["message_ids"]) == 1
    assert e.fake.message(e.threads["oven"], sent.result["message_ids"][0])["content"] == "Hello Mira"
    posted = e.ledger.load()["threads"][e.threads["oven"]]["triage"]["replies_posted"]
    assert posted[0]["message_id"] == sent.result["message_ids"][0] and posted[0]["proposal"] == p.id
    assert rep.sent and not rep.failed and not rep.mismatches
    assert any("verify: all 1 sent proposals match Discord" in ln for ln in rep.lines())


def test_send_only_touches_approved_proposals(seeded):
    e = seeded
    approved, pending = fresh_reply(e, "oven", "a"), fresh_reply(e, "flicker", "b")
    e.fake.calls.clear()
    e.approve_and_send(approved)
    assert e.queue.get(pending.id).state == "pending"
    assert len(e.fake.messages[e.threads["flicker"]]) == 1


def test_a_sent_proposal_is_never_sent_twice(seeded):
    e = seeded
    p = fresh_reply(e)
    e.approve_and_send(p)
    n = len(e.fake.messages[e.threads["oven"]])
    again = e.queue.send(e.client, e.ledger, delay=0, sleep=lambda s: None)
    assert again.sent == [] and len(e.fake.messages[e.threads["oven"]]) == n


def test_stale_approved_copy_of_a_sent_proposal_is_not_executed(seeded):
    e = seeded
    p = fresh_reply(e)
    e.approve_and_send(p)
    n = len(e.fake.messages[e.threads["oven"]])
    # simulate a crash between "executed" and "cleaned up": the same file is back in approved/
    (e.cfg.queue_dir / "approved").mkdir(exist_ok=True)
    shutil.copy(e.cfg.queue_dir / "sent" / (p.id + ".json"), e.cfg.queue_dir / "approved" / (p.id + ".json"))
    rep = e.queue.send(e.client, e.ledger, delay=0, sleep=lambda s: None)
    assert rep.sent == [] and [w for _, w in rep.skipped] == ["already sent"]
    assert len(e.fake.messages[e.threads["oven"]]) == n
    assert not (e.cfg.queue_dir / "approved" / (p.id + ".json")).exists()


def test_interrupted_send_is_parked_not_repeated(seeded):
    e = seeded
    p = fresh_reply(e)
    e.queue.approve([p.id], "human:test")
    # a previous send died while running this one: its proposal is left in sending/ with the started marker
    (e.cfg.queue_dir / "sending").mkdir(exist_ok=True)
    os.replace(e.cfg.queue_dir / "approved" / (p.id + ".json"), e.cfg.queue_dir / "sending" / (p.id + ".json"))
    q = e.queue.get(p.id)
    assert q.state == "sending"
    q.result = {"started": "2026-10-24T00:00:00Z"}
    e.queue.update(q)
    n = len(e.fake.messages[e.threads["oven"]])
    rep = e.queue.send(e.client, e.ledger, delay=0, sleep=lambda s: None)
    assert [x.id for x in rep.failed] == [p.id] and len(e.fake.messages[e.threads["oven"]]) == n
    assert e.queue.get(p.id).state == "failed" and "interrupted" in e.queue.get(p.id).error
    with pytest.raises(QueueError, match="partly posted"):
        e.queue.retry(p.id, "human:test")
    e.queue.retry(p.id, "human:test", force=True)
    assert e.queue.get(p.id).state == "approved"


class Crash(BaseException):
    """Stands in for a killed process: not an Exception, so send() cannot tidy up after it."""


def test_a_crash_while_sending_leaves_the_proposal_in_sending_and_the_next_send_parks_it(seeded, monkeypatch):
    e = seeded
    p, other = fresh_reply(e, "oven", "first"), fresh_reply(e, "flicker", "second")
    e.queue.approve([p.id, other.id], "human:test")

    def die(*a, **k):
        raise Crash()

    monkeypatch.setattr(actions, "execute", die)
    with pytest.raises(Crash):
        e.queue.send(e.client, e.ledger, delay=0, sleep=lambda s: None)
    q = e.cfg.queue_dir
    assert (q / "sending" / (p.id + ".json")).exists() and not (q / "approved" / (p.id + ".json")).exists()
    assert json.loads((q / "sending" / (p.id + ".json")).read_text(encoding="utf-8"))["result"]["started"]
    assert not (q / "send.lock").exists()                          # the lock was released on the way out
    monkeypatch.undo()
    n = len(e.fake.messages[e.threads["oven"]])
    rep = e.queue.send(e.client, e.ledger, delay=0, sleep=lambda s: None)
    parked = e.queue.get(p.id)
    assert parked.state == "failed" and "interrupted" in parked.error
    assert [x.id for x in rep.sent] == [other.id]                  # the untouched approved one still goes out
    assert len(e.fake.messages[e.threads["oven"]]) == n            # the interrupted one was NOT re-sent
    with pytest.raises(QueueError, match="partly posted"):
        e.queue.retry(p.id, "human:test")


def test_sending_state_is_visible_and_cannot_be_rejected_or_approved(seeded):
    e = seeded
    p = fresh_reply(e)
    e.queue.approve([p.id], "human:test")
    (e.cfg.queue_dir / "sending").mkdir(exist_ok=True)
    os.replace(e.cfg.queue_dir / "approved" / (p.id + ".json"), e.cfg.queue_dir / "sending" / (p.id + ".json"))
    assert [x.id for x in e.queue.list("sending")] == [p.id] and "sending" in e.queue.render(e.queue.get(p.id))
    with pytest.raises(QueueError, match="only pending, approved or failed"):
        e.queue.reject([p.id], "human:test")
    with pytest.raises(QueueError, match="only pending"):
        e.queue.approve([p.id], "human:test")


def test_reject_between_the_snapshot_and_the_claim_wins_the_race(seeded, monkeypatch):
    """A human rejects a proposal after send() listed the approved ones but before it executed it."""
    e = seeded
    keep, doomed = fresh_reply(e, "oven", "keep"), fresh_reply(e, "flicker", "doomed")
    e.queue.approve([keep.id, doomed.id], "human:test")
    real_list = type(e.queue).list
    fired = []

    def list_then_reject(self, state=None):
        snapshot = real_list(self, state)
        if state == "approved" and not fired:
            fired.append(1)
            self.reject([doomed.id], "human:owner", "changed my mind")      # lands after the snapshot
        return snapshot

    monkeypatch.setattr(type(e.queue), "list", list_then_reject)
    rep = e.queue.send(e.client, e.ledger, delay=0, sleep=lambda s: None)
    monkeypatch.undo()
    assert [x.id for x in rep.sent] == [keep.id]
    assert [(p.id, why.split(" (")[0]) for p, why in rep.skipped] == [(doomed.id, "no longer approved")]
    assert e.queue.get(doomed.id).state == "rejected"
    assert e.fake.messages[e.threads["flicker"]][-1]["author"]["id"] != e.fake.bot["id"]   # never posted
    assert any("skipped" in ln and doomed.id in ln for ln in rep.lines())


def test_reject_after_the_claim_is_refused_instead_of_silently_ignored(seeded, monkeypatch):
    e = seeded
    p = fresh_reply(e)
    e.queue.approve([p.id], "human:test")
    outcome = {}

    def reject_mid_send(*a, **k):
        try:
            e.queue.reject([p.id], "human:owner")
        except QueueError as err:
            outcome["error"] = str(err)
        return {"message_ids": []}

    monkeypatch.setattr(actions, "execute", reject_mid_send)
    e.queue.send(e.client, e.ledger, delay=0, sleep=lambda s: None, verify=False)
    assert "only pending, approved or failed" in outcome["error"]            # it is 'sending', and the human is told
    assert e.queue.get(p.id).state == "sent"


def test_attachments_inside_hearbacks_own_files_are_refused(seeded):
    e = seeded
    e.cfg.data_dir.mkdir(exist_ok=True)
    token, config = e.cfg.root / "secret-token.txt", e.cfg.root / "hearback.toml"
    token.write_text("tok", encoding="utf-8")
    config.write_text("[discord]\n", encoding="utf-8")
    q = type(e.queue)(e.cfg.queue_dir, e.ledger, protected=[token, config])
    inside = [e.cfg.ledger_path, e.ledger.dir / "events.jsonl", e.ledger.dir / "attachments" / e.threads["oven"],
              token, config]
    (e.ledger.dir / "attachments" / e.threads["oven"]).mkdir(parents=True, exist_ok=True)
    stored = next((e.ledger.dir / "attachments" / e.threads["oven"]).glob("*"), None)
    for target in [e.cfg.ledger_path, e.ledger.dir / "events.jsonl", token, config] + ([stored] if stored else []):
        for kind in ("reply", "post"):
            with pytest.raises(QueueError, match="inside Hearback's own files"):
                q.propose(kind, actor="agent:x", thread=e.threads["oven"] if kind == "reply" else e.announce,
                          text="here is a file", attachments=[target])
    assert q.list() == [] and inside


def test_attachments_that_reach_in_through_dotdot_or_a_symlink_are_refused(seeded, tmp_path):
    e = seeded
    sneaky = tmp_path / "elsewhere" / ".." / ".hearback" / "ledger.json"
    sneaky = e.cfg.root / "x" / ".." / ".hearback" / "ledger.json"
    with pytest.raises(QueueError, match="inside Hearback's own files"):
        e.queue.propose("reply", actor="agent:x", thread=e.threads["oven"], text="t", attachments=[sneaky])
    link = tmp_path / "innocent.png"
    try:
        link.symlink_to(e.cfg.ledger_path)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this machine")
    with pytest.raises(QueueError, match="inside Hearback's own files"):
        e.queue.propose("reply", actor="agent:x", thread=e.threads["oven"], text="t", attachments=[link])


def test_ordinary_files_can_still_be_attached(seeded, tmp_path):
    e = seeded
    shot = tmp_path / "evidence.png"
    shot.write_bytes(b"\x89PNG")
    p = e.queue.propose("reply", actor="agent:x", thread=e.threads["oven"], text="see attached", attachments=[shot])
    assert p.attachments == [str(shot.resolve())]


def test_the_data_dir_ignores_itself_as_soon_as_the_queue_creates_it(tmp_path):
    from hearback.queue import Queue
    root = tmp_path / ".hearback" / "queue"
    q = Queue(root)
    assert not (tmp_path / ".hearback").exists()
    q.propose("post", actor="a", thread="1" * 16, text="hello")
    assert (tmp_path / ".hearback" / ".gitignore").read_text(encoding="utf-8") == "*\n"


def test_failed_send_can_be_retried(seeded):
    e = seeded
    p = fresh_reply(e)
    e.fake.fail_next(r"POST /channels/\d+/messages", 403, {"message": "Missing Permissions"})
    rep = e.approve_and_send(p)
    assert rep.failed and "403" in e.queue.get(p.id).error and e.queue.get(p.id).state == "failed"
    assert "FAILED" in "\n".join(rep.lines())
    e.queue.retry(p.id, "human:test")                      # nothing was posted: safe without force
    rep = e.queue.send(e.client, e.ledger, delay=0, sleep=lambda s: None)
    assert e.queue.get(p.id).state == "sent" and rep.sent


def test_partial_send_keeps_what_went_out_and_needs_force_to_retry(seeded):
    e = seeded
    text = "\n\n".join(("block %d " % i) * 120 for i in range(8))        # ~ 8 blocks of ~1000 chars -> 4+ messages
    p = fresh_reply(e, "oven", text)
    e.fake.fail_next(r"POST /channels/\d+/messages", 500, times=1, after=1)
    rep = e.approve_and_send(p)
    q = e.queue.get(p.id)
    assert q.state == "failed" and len(q.result["message_ids"]) == 1 and "1 already posted" in q.error
    assert len(posted_ids(e)) == 1
    assert len(e.ledger.load()["threads"][e.threads["oven"]]["triage"]["replies_posted"]) == 1
    with pytest.raises(QueueError, match="partly posted"):
        e.queue.retry(p.id, "human:test")


def test_long_text_is_split_with_attachment_on_the_last_message(seeded, tmp_path):
    e = seeded
    img = tmp_path / "proof.png"
    img.write_bytes(b"\x89PNG fake")
    text = "\n\n".join(("block %d " % i) * 120 for i in range(4))
    p = e.queue.propose("reply", actor="a", thread=e.threads["oven"], text=text, attachments=[img])
    e.approve_and_send(p)
    ids = posted_ids(e)
    msgs = [e.fake.message(e.threads["oven"], i) for i in ids]
    assert len(msgs) >= 2 and all(len(m["content"]) <= MSG_LIMIT for m in msgs)
    assert "\n\n".join(m["content"] for m in msgs) == text
    assert [len(m["attachments"]) for m in msgs] == [0] * (len(msgs) - 1) + [1]
    assert e.queue.get(p.id).result["verified"] is True


def test_send_lock_prevents_concurrent_senders(seeded):
    e = seeded
    p = fresh_reply(e)
    e.queue.approve([p.id], "human:test")
    (e.cfg.queue_dir / "send.lock").write_text("1 other sender")
    with pytest.raises(QueueError, match="another `hearback send` is running"):
        e.queue.send(e.client, e.ledger, delay=0, sleep=lambda s: None)
    assert e.queue.get(p.id).state == "approved"


def test_delay_between_writes_is_honoured(seeded):
    e = seeded
    a, b = fresh_reply(e, "oven", "a"), fresh_reply(e, "flicker", "b")
    e.queue.approve([a.id, b.id], "human:test")
    sleeps: list[float] = []
    e.queue.send(e.client, e.ledger, delay=1.5, sleep=sleeps.append)
    assert sleeps == [1.5]


# ---------------------------------------------------------------- every kind

def test_all_kinds_execute_and_verify(seeded, tmp_path):
    e = seeded
    t = e.threads["oven"]
    shot = tmp_path / "evidence.png"
    shot.write_bytes(b"\x89PNG")
    props = [
        e.queue.propose("reply", actor="a", thread=t, text="reply text"),
        e.queue.propose("tags", actor="a", thread=t, add=["In progress"], remove=["Crash"]),
        e.queue.propose("post", actor="a", thread=e.announce, text="announcement", attachments=[shot]),
        e.queue.propose("pin", actor="a", thread=t, pin=True),
        e.queue.propose("newpost", actor="a", title="How to report", text="body " * 500 + "\n\n" + "more " * 500,
                        add=["UI", "Full"], pin=True, attachments=[shot]),
    ]
    rep = e.approve_and_send(*props)
    assert not rep.failed and not rep.mismatches, rep.lines()
    assert e.fake.channels[t]["applied_tags"] == [e.fake.tag_id(e.forum, "Free"), e.fake.tag_id(e.forum, "In progress")]
    assert e.fake.channels[t]["flags"] & 2
    assert e.ledger.load()["threads"][t]["tags"] == ["Free", "In progress"]
    newpost = e.queue.get(props[-1].id).result
    th = e.fake.channels[newpost["thread_id"]]
    assert th["name"] == "How to report" and th["flags"] & 2 and len(th["applied_tags"]) == 2
    assert len(newpost["message_ids"]) >= 2                      # body + follow-up chunk(s)
    rec = e.ledger.load()["threads"][newpost["thread_id"]]
    assert rec["triage"]["status"] == "guide" and rec["tags"] == ["UI", "Full"]
    # unpin, edit, delete
    first = e.queue.get(props[0].id).result["message_ids"][0]
    more = [
        e.queue.propose("pin", actor="a", thread=t, pin=False),
        e.queue.propose("edit", actor="a", thread=t, message_id=first, text="edited text"),
    ]
    rep = e.approve_and_send(*more)
    assert not rep.mismatches and not (e.fake.channels[t]["flags"] & 2)
    assert e.fake.message(t, first)["content"] == "edited text"
    assert e.ledger.load()["threads"][t]["triage"]["replies_posted"][0]["content"] == "edited text"
    gone = e.queue.propose("delete", actor="a", thread=t, message_id=first)
    rep = e.approve_and_send(gone)
    assert not rep.mismatches and e.fake.message(t, first) is None
    assert e.ledger.load()["threads"][t]["triage"]["replies_posted"] == []


def test_tag_names_resolve_case_insensitively_and_refresh_when_stale(seeded):
    e = seeded
    with e.ledger.locked():                                   # ledger knows no tags at all
        d = e.ledger.load()
        d["tags"] = {}
        e.ledger.save(d)
    p = e.queue.propose("tags", actor="a", thread=e.threads["flicker"], add=["need info"])
    e.approve_and_send(p)
    assert e.queue.get(p.id).state == "sent" and "Need info" in e.ledger.load()["threads"][e.threads["flicker"]]["tags"]
    bad = e.queue.propose("tags", actor="a", thread=e.threads["flicker"], add=["No such tag"])
    rep = e.approve_and_send(bad)
    assert rep.failed and "no forum tag named" in e.queue.get(bad.id).error and "Released" in e.queue.get(bad.id).error


def test_more_than_five_tags_is_refused_before_patching(seeded):
    e = seeded
    p = e.queue.propose("tags", actor="a", thread=e.threads["oven"],
                        add=["UI", "Full", "In progress", "Released", "Need info"])
    e.fake.calls.clear()
    rep = e.approve_and_send(p)
    assert rep.failed and "Discord allows 5" in e.queue.get(p.id).error
    assert not [c for c in e.fake.writes() if c.method == "PATCH"]


# ---------------------------------------------------------------- verify

def test_verify_detects_every_kind_of_drift(seeded):
    e = seeded
    t = e.threads["oven"]
    props = [e.queue.propose("reply", actor="a", thread=t, text="original"),
             e.queue.propose("reply", actor="a", thread=t, text="will be deleted"),
             e.queue.propose("tags", actor="a", thread=t, add=["In progress"]),
             e.queue.propose("pin", actor="a", thread=t, pin=True)]
    assert not e.approve_and_send(*props).mismatches
    r1, r2 = (e.queue.get(p.id).result["message_ids"][0] for p in props[:2])
    e.fake.message(t, r1)["content"] = "someone edited this"
    e.fake.messages[t].remove(e.fake.message(t, r2))
    e.fake.channels[t]["applied_tags"] = []
    e.fake.channels[t]["flags"] = 0
    bad = e.queue.verify(e.client, [e.queue.get(p.id) for p in props])
    joined = "\n".join(bad)
    assert len(bad) == 4
    assert "content differs" in joined and "missing on Discord" in joined
    assert "tags on Discord differ" in joined and "not pinned" in joined
    assert e.queue.get(props[0].id).result["verified"] is False
    assert e.queue.get(props[0].id).result["mismatches"]
    assert any(x["action"] == "verify_mismatch" for x in e.ledger.read_events())


def test_verify_without_ids_checks_only_unverified_sent_proposals(seeded):
    e = seeded
    p = fresh_reply(e)
    e.approve_and_send(p)
    e.fake.calls.clear()
    assert e.queue.verify(e.client) == []                    # already verified during send: nothing to do
    assert e.fake.calls == []


def test_proposal_roundtrip_keeps_unknown_fields():
    p = Proposal.from_dict({"id": "p-1", "kind": "reply", "actor": "a", "created": "t", "future_field": [1, 2]})
    assert p.to_dict()["future_field"] == [1, 2] and p.state == "pending"


def test_newpost_in_another_forum_uses_its_own_tags_and_leaves_the_ledger_alone(seeded):
    e = seeded
    other = e.fake.add_forum("announcements-forum", ["News", "Dev"])
    before = e.ledger.load()["tags"]
    p = e.queue.propose("newpost", actor="a", thread=other, title="Hello", text="body", add=["news"])
    rep = e.approve_and_send(p)
    assert not rep.failed and not rep.mismatches, rep.lines()
    tid = e.queue.get(p.id).result["thread_id"]
    assert e.fake.channels[tid]["applied_tags"] == [e.fake.tag_id(other, "News")]
    assert e.ledger.load()["tags"] == before                       # the ledger's own tag table was not overwritten
    assert tid not in e.ledger.load()["threads"]                   # and the post is not a ledger thread


def test_send_only_selected_ids(seeded):
    e = seeded
    a, b = fresh_reply(e, "oven", "a"), fresh_reply(e, "flicker", "b")
    e.queue.approve([a.id, b.id], "human:test")
    rep = e.queue.send(e.client, e.ledger, delay=0, sleep=lambda s: None, ids=[b.id])
    assert [p.id for p in rep.sent] == [b.id] and e.queue.get(a.id).state == "approved"
