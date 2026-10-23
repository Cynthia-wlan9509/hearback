"""Release closure: per-variant notifications, announcement, finalize."""
from __future__ import annotations

import pytest

from conftest import make_env
from hearback.release import finalize, release, thread_variants
from hearback.util import HearbackError


@pytest.fixture
def fixed(tmp_path):
    """Three fixed threads (free, full, both) + one still being investigated."""
    e = make_env(tmp_path)
    f = e.fake
    e.threads["a"] = f.add_thread(e.forum, "Oven freezes", "Mira", "b", ["Crash", "Free"])
    e.threads["b"] = f.add_thread(e.forum, "Oven stuck", "Jon", "b", ["Crash", "Full"])
    e.threads["c"] = f.add_thread(e.forum, "Flicker", "Lina", "b", ["UI"])
    e.threads["d"] = f.add_thread(e.forum, "Open issue", "Zoe", "b", ["UI", "Free"])
    e.sync()
    e.triage("a", status="fixed-pending-release", cluster="oven", fix="keep timer state", variants=["free"],
             release_note="The oven timer no longer freezes.")
    e.triage("b", status="fixed-pending-release", cluster="oven", fix="keep timer state", variants=["full"],
             release_note="The oven timer no longer freezes.")
    e.triage("c", status="fixed-pending-release", cluster="flicker", fix="cache atlas", variants=["free", "full"])
    e.triage("d", status="investigating")
    return e


def drafts(e, plan, kind="reply"):
    return {p.thread: p for p in plan.proposals if p.kind == kind}


def test_release_drafts_per_variant_notifications(fixed):
    e = fixed
    plan = release("2026.10.24", ["free"], e.cfg, e.ledger, e.queue, "agent:test")
    replies = drafts(e, plan)
    assert set(replies) == {e.threads["a"], e.threads["c"]}              # b is full-only, d is not fixed
    assert [sid[-6:] for sid, _ in plan.skipped] == [e.threads["b"][-6:]]
    a = replies[e.threads["a"]]
    assert "Hi Mira" in a.text and "2026.10.24" in a.text and "Oven freezes" in a.text
    assert "The oven timer no longer freezes." in a.text
    assert "Free: get it from the pinned link" in a.text and "your save stays" in a.text
    assert "supporters channel" not in a.text                             # only the variant that applies
    assert a.meta == {"release": "2026.10.24", "variants": ["free"]}
    c = replies[e.threads["c"]]
    assert "supporters channel" not in c.text                             # release is free-only, so only free is listed


def test_release_for_both_variants_lists_each_threads_variants(fixed):
    e = fixed
    plan = release("2026.10.24", [], e.cfg, e.ledger, e.queue, "agent:test")     # default: all variants
    replies = drafts(e, plan)
    assert set(replies) == {e.threads[k] for k in "abc"}
    c = replies[e.threads["c"]].text
    assert "Free: get it from the pinned link" in c and "Full: get it from the supporters channel" in c
    assert "The oven timer" not in c


def test_announcement_groups_clusters_by_variant(fixed):
    e = fixed
    plan = release("2026.10.24", [], e.cfg, e.ledger, e.queue, "agent:test")
    (ann,) = [p for p in plan.proposals if p.kind == "post"]
    assert ann.thread == e.announce and ann.meta["announce"] is True
    text = ann.text
    assert text.startswith("**Version 2026.10.24 is out**")
    free_part, full_part = text.split("**Full**")
    assert "oven: The oven timer no longer freezes. (1 report(s))" in free_part and "flicker" in free_part
    assert "oven" in full_part and "flicker" in full_part


def test_status_tags_swap_is_proposed_with_each_notification(fixed):
    e = fixed
    # give the threads their "fixed" tag so the swap has something to remove
    for k in "ac":
        p = e.queue.propose("tags", actor="a", thread=e.threads[k], add=["Fixed, awaiting release"])
        e.approve_and_send(p)
    plan = release("2026.10.24", ["free"], e.cfg, e.ledger, e.queue, "agent:test")
    tags = drafts(e, plan, "tags")
    assert set(tags) == {e.threads["a"], e.threads["c"]}
    p = tags[e.threads["a"]]
    assert p.add == ["Released"] and p.remove == ["Fixed, awaiting release"]


def test_running_the_release_twice_does_not_duplicate(fixed):
    e = fixed
    first = release("2026.10.24", ["free"], e.cfg, e.ledger, e.queue, "agent:test")
    second = release("2026.10.24", ["free"], e.cfg, e.ledger, e.queue, "agent:test")
    assert [p for p in second.proposals if p.kind == "reply"] == []
    assert sum("already drafted" in why for _, why in second.skipped) == 2
    assert len([p for p in e.queue.list() if p.kind == "reply"]) == 2
    assert first.proposals


def test_unknown_variant_and_empty_version_are_errors(fixed):
    e = fixed
    with pytest.raises(HearbackError, match="unknown variant"):
        release("1.0", ["nope"], e.cfg, e.ledger, e.queue, "a")
    with pytest.raises(HearbackError, match="needs a version"):
        release(" ", [], e.cfg, e.ledger, e.queue, "a")


def test_no_announce_channel_means_no_announcement(fixed):
    e = fixed
    e.cfg.announce_channel_id = ""
    plan = release("2026.10.24", [], e.cfg, e.ledger, e.queue, "agent:test")
    assert not [p for p in plan.proposals if p.kind == "post"]
    assert any("announce_channel_id not set" in n for n in plan.notes)


def test_nothing_to_release(tmp_path):
    e = make_env(tmp_path)
    plan = release("1.0", [], e.cfg, e.ledger, e.queue, "a")
    assert plan.proposals == [] and plan.notes


def test_full_cycle_send_then_finalize(fixed):
    e = fixed
    plan = release("2026.10.24", ["free"], e.cfg, e.ledger, e.queue, "agent:test")
    waiting = finalize("2026.10.24", e.ledger, e.queue, "human:test")
    assert waiting.finalized == [] and len(waiting.waiting) == 2               # nothing sent yet: nothing marked released
    rep = e.approve_and_send(*plan.proposals)
    assert not rep.failed and not rep.mismatches
    announce_msgs = e.fake.messages[e.announce]
    assert len(announce_msgs) == 1 and "2026.10.24 is out" in announce_msgs[0]["content"]
    assert "Hi Mira" in [m["content"] for m in e.fake.messages[e.threads["a"]]][-1]
    fin = finalize("2026.10.24", e.ledger, e.queue, "human:test")
    assert sorted(fin.finalized) == sorted([e.threads["a"], e.threads["c"]])
    tri = e.ledger.load()["threads"][e.threads["a"]]["triage"]
    assert tri["status"] == "released" and tri["fixed_in"] == "2026.10.24"
    assert e.ledger.load()["threads"][e.threads["b"]]["triage"]["status"] == "fixed-pending-release"   # other variant
    again = finalize("2026.10.24", e.ledger, e.queue, "human:test")
    assert again.finalized == [] and len(again.already) == 2                    # idempotent
    # the second build ships later
    plan2 = release("2026.10.26", ["full"], e.cfg, e.ledger, e.queue, "agent:test")
    assert set(drafts(e, plan2)) == {e.threads["b"]}                           # c is already released
    e.approve_and_send(*plan2.proposals)
    finalize("2026.10.26", e.ledger, e.queue, "human:test")
    assert e.ledger.load()["threads"][e.threads["b"]]["triage"]["fixed_in"] == "2026.10.26"


def test_rejected_notification_is_reported_and_thread_stays_open(fixed):
    e = fixed
    plan = release("2026.10.24", ["free"], e.cfg, e.ledger, e.queue, "agent:test")
    note_a = drafts(e, plan)[e.threads["a"]]
    e.queue.reject([note_a.id], "human:test", "wording")
    keep = [p for p in plan.proposals if p.id != note_a.id]
    e.approve_and_send(*keep)
    fin = finalize("2026.10.24", e.ledger, e.queue, "human:test")
    assert e.threads["a"] not in fin.finalized and any("rejected" in p for p in fin.problems)
    assert e.ledger.load()["threads"][e.threads["a"]]["triage"]["status"] == "fixed-pending-release"


def test_finalize_without_a_release_is_an_error(fixed):
    with pytest.raises(HearbackError, match="no notifications were drafted"):
        finalize("9.9", fixed.ledger, fixed.queue, "a")


def test_chinese_templates_and_workspace_overrides(fixed):
    e = fixed
    e.cfg.language = "zh"
    plan = release("2026.10.24", ["free"], e.cfg, e.ledger, e.queue, "agent:test")
    text = drafts(e, plan)[e.threads["a"]].text
    assert "你好 Mira" in text and "已在 2026.10.24 版本修复" in text and "存档会保留" in text
    # a per-workspace override wins
    e.cfg.templates_dir.mkdir(parents=True)
    (e.cfg.templates_dir / "release_notify.txt").write_text("Custom for $author: $version", encoding="utf-8")
    plan = release("2026.10.30", ["free"], e.cfg, e.ledger, e.queue, "agent:test")
    assert drafts(e, plan)[e.threads["a"]].text == "Custom for Mira: 2026.10.30"


def test_thread_variants_inference(fixed):
    e = fixed
    rec = e.ledger.load()["threads"][e.threads["d"]]
    assert thread_variants(rec, e.cfg) == ["free"]                              # from the Free tag
    assert thread_variants({"tags": [], "triage": {}}, e.cfg) == ["free", "full"]  # unknown -> all
    assert thread_variants({"tags": ["Free"], "triage": {"variants": ["full"]}}, e.cfg) == ["full"]
