"""Staff-channel approvals by reaction."""
from __future__ import annotations

import pytest

from conftest import fresh_reply
from hearback import staff
from hearback.util import HearbackError


@pytest.fixture
def staffed(seeded):
    e = seeded
    e.dana = e.fake.add_user("Dana")
    e.troll = e.fake.add_user("Troll")
    e.cfg.approvers = [e.dana["id"]]
    return e


def pushed(e, **kw):
    out = staff.push(e.client, e.queue, e.cfg, sleep=lambda s: None, data=e.ledger.load(), **kw)
    return {p.id: p for p in out.pushed}


def test_push_posts_preview_and_seeds_reactions(staffed):
    e = staffed
    p = fresh_reply(e, "oven", "Hello Mira")
    done = pushed(e)
    assert list(done) == [p.id]
    mid = e.queue.get(p.id).staff_message_id
    msg = e.fake.message(e.staff, mid)
    assert p.id in msg["content"] and "Hello Mira" in msg["content"] and "thread" in msg["content"]
    assert len(msg["content"]) <= 2000
    assert {k[2] for k in e.fake.reactions if k[1] == mid} == {"✅", "❌"}
    assert pushed(e) == {}                                     # idempotent: nothing is posted twice
    assert len(e.fake.messages[e.staff]) == 1


def test_previews_that_would_be_truncated_are_refused_unless_allowed(staffed):
    e = staffed
    p = fresh_reply(e, "oven", "word " * 2000)
    res = staff.push(e.client, e.queue, e.cfg, sleep=lambda s: None, data=e.ledger.load())
    assert res.pushed == [] and [x.id for x, n in res.refused] == [p.id] and res.refused[0][1] > 2000
    assert e.fake.messages[e.staff] == [] and e.queue.get(p.id).staff_message_id == ""
    text = "\n".join(res.lines())
    assert "REFUSED" in text and "--allow-truncated" in text
    done = pushed(e, allow_truncated=True)
    assert list(done) == [p.id]
    (msg,) = e.fake.messages[e.staff]
    assert len(msg["content"]) <= 2000 and "truncated" in msg["content"]


def test_approver_checkmark_approves(staffed):
    e = staffed
    p = fresh_reply(e)
    pushed(e)
    e.fake.react(e.staff, e.queue.get(p.id).staff_message_id, "✅", e.dana["id"])
    res = staff.poll(e.client, e.queue, e.cfg)
    assert res.approved == [p.id] and e.queue.get(p.id).state == "approved"
    assert e.queue.get(p.id).approved_by == "human:discord-" + e.dana["id"]


def test_reaction_from_non_approver_is_ignored(staffed):
    e = staffed
    p = fresh_reply(e)
    pushed(e)
    e.fake.react(e.staff, e.queue.get(p.id).staff_message_id, "✅", e.troll["id"])
    res = staff.poll(e.client, e.queue, e.cfg)
    assert res.approved == [] and res.ignored == 1                      # the bot's own seed reactions are not counted
    assert e.queue.get(p.id).state == "pending"


def test_approver_cross_rejects_and_reject_wins_over_approve(staffed):
    e = staffed
    a, b = fresh_reply(e, "oven", "a"), fresh_reply(e, "flicker", "b")
    pushed(e)
    e.fake.react(e.staff, e.queue.get(a.id).staff_message_id, "❌", e.dana["id"])
    e.fake.react(e.staff, e.queue.get(b.id).staff_message_id, "✅", e.dana["id"])
    e.fake.react(e.staff, e.queue.get(b.id).staff_message_id, "❌", e.dana["id"])
    res = staff.poll(e.client, e.queue, e.cfg)
    assert sorted(res.rejected) == sorted([a.id, b.id]) and res.approved == []
    assert e.queue.get(a.id).state == "rejected"


def test_text_changed_after_push_is_not_approved(staffed):
    e = staffed
    p = fresh_reply(e, "oven", "original wording")
    pushed(e)
    e.queue.set_text(p.id, "completely different wording", "human:owner")
    e.fake.react(e.staff, e.queue.get(p.id).staff_message_id, "✅", e.dana["id"])
    res = staff.poll(e.client, e.queue, e.cfg)
    assert res.stale == [p.id] and e.queue.get(p.id).state == "pending"
    assert e.queue.get(p.id).staff_message_id == ""                    # re-push shows the new text
    assert list(pushed(e)) == [p.id]


def test_poll_needs_approvers_and_a_channel(staffed):
    e = staffed
    e.cfg.approvers = []
    with pytest.raises(HearbackError, match="no approvers"):
        staff.poll(e.client, e.queue, e.cfg)
    e.cfg.staff_channel_id = ""
    with pytest.raises(HearbackError, match="staff_channel_id"):
        staff.push(e.client, e.queue, e.cfg)


def test_deleted_staff_message_is_skipped(staffed):
    e = staffed
    p = fresh_reply(e)
    pushed(e)
    e.fake.messages[e.staff].clear()
    res = staff.poll(e.client, e.queue, e.cfg)
    assert res.lines()[0].startswith("staff poll: 0 approved") and res.stale == [p.id]
    assert "was deleted" in res.errors[0]
    assert e.queue.get(p.id).state == "pending" and e.queue.get(p.id).staff_message_id == ""


def test_reaction_flow_end_to_end_then_send(staffed):
    e = staffed
    p = fresh_reply(e, "oven", "Thanks for the report")
    pushed(e)
    e.fake.react(e.staff, e.queue.get(p.id).staff_message_id, "✅", e.dana["id"])
    staff.poll(e.client, e.queue, e.cfg)
    rep = e.queue.send(e.client, e.ledger, delay=0, sleep=lambda s: None)
    assert rep.sent and e.fake.messages[e.threads["oven"]][-1]["content"] == "Thanks for the report"
