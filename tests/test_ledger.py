"""Ledger: locking across processes, atomic saves, claims with leases, revision conflicts, events."""
from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time

import pytest

import _workers
from conftest import Clock
from hearback import util
from hearback.ledger import (ClaimError, ConflictError, Ledger, LedgerError, LockTimeout, filter_threads,
                             is_unreviewed)


def make_ledger(tmp_path, **kw) -> Ledger:
    return Ledger(tmp_path / ".hearback" / "ledger.json", **kw)


def add_threads(ledger: Ledger, *ids: str) -> None:
    with ledger.locked():
        data = ledger.load()
        for i in ids:
            data["threads"][i] = {"title": "t" + i[-3:], "author": "a", "tags": [], "replies": []}
        ledger.save(data)


# ---------------------------------------------------------------- locking

def test_two_processes_never_lose_updates(tmp_path):
    path = str(tmp_path / "ledger.json")
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(3) as pool:
        done = pool.starmap(_workers.bump, [(path, 15)] * 3)
    assert sum(done) == 45
    assert json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))["counter"] == 45
    assert not (tmp_path / "ledger.json.lock").exists()


def test_lock_blocks_until_released_then_times_out(tmp_path):
    a = make_ledger(tmp_path)
    b = make_ledger(tmp_path, lock_timeout=0.2)
    entered = threading.Event()
    release = threading.Event()

    def holder():
        with a.locked():
            entered.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    entered.wait(5)
    with pytest.raises(LockTimeout):
        with b.locked():
            pass
    release.set()
    t.join()
    with b.locked():  # free again
        pass


def test_stale_lock_is_broken_after_30_seconds(tmp_path):
    ledger = make_ledger(tmp_path, lock_timeout=2)
    ledger.dir.mkdir(parents=True)
    ledger.lock_path.write_text("99999 dead process")
    old = time.time() - 45
    os.utime(ledger.lock_path, (old, old))
    t0 = time.monotonic()
    with ledger.locked():
        assert ledger.lock_path.exists()
    assert time.monotonic() - t0 < 1.5 and not ledger.lock_path.exists()


def test_fresh_foreign_lock_is_respected(tmp_path):
    ledger = make_ledger(tmp_path, lock_timeout=0.15)
    ledger.dir.mkdir(parents=True)
    ledger.lock_path.write_text("1 other")
    with pytest.raises(LockTimeout):
        with ledger.locked():
            pass


def test_locked_is_reentrant(tmp_path):
    ledger = make_ledger(tmp_path)
    with ledger.locked():
        with ledger.locked():
            ledger.save(ledger.load())
        assert ledger.lock_path.exists()  # inner exit must not release
    assert not ledger.lock_path.exists()


# ---------------------------------------------------------------- saving

def test_save_requires_lock(tmp_path):
    with pytest.raises(LedgerError, match="locked"):
        make_ledger(tmp_path).save({})


def test_save_is_atomic_lf_utf8_and_leaves_no_temp_files(tmp_path):
    ledger = make_ledger(tmp_path)
    with ledger.locked():
        data = ledger.load()
        data["threads"]["1"] = {"title": "日本語 ✅\nline2"}
        ledger.save(data)
    raw = ledger.path.read_bytes()
    assert b"\r\n" not in raw and "日本語".encode() in raw and raw.endswith(b"\n")
    assert sorted(p.name for p in ledger.dir.iterdir()) == [".gitignore", "ledger.json"]   # no temp files
    assert ledger.load()["threads"]["1"]["title"] == "日本語 ✅\nline2"


def test_failed_save_keeps_old_file_and_cleans_up(tmp_path, monkeypatch):
    ledger = make_ledger(tmp_path)
    with ledger.locked():
        ledger.save({"threads": {"1": {"title": "old"}}})
    before = ledger.path.read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(util, "replace_retry", boom)
    with pytest.raises(OSError):
        with ledger.locked():
            ledger.save({"threads": {"1": {"title": "new"}}})
    assert ledger.path.read_bytes() == before
    assert sorted(p.name for p in ledger.dir.iterdir()) == [".gitignore", "ledger.json"]   # no temp files


def test_load_defaults_and_keeps_unknown_fields(tmp_path):
    ledger = make_ledger(tmp_path)
    assert ledger.load() == {"schema": 1, "forum_id": "", "synced_at": "", "tags": {}, "threads": {}}
    with ledger.locked():
        d = ledger.load()
        d["something_new"] = {"x": 1}
        ledger.save(d)
    assert ledger.load()["something_new"] == {"x": 1}


# ---------------------------------------------------------------- resolve

def test_resolve_by_full_id_and_unique_suffix(tmp_path):
    ledger = make_ledger(tmp_path)
    add_threads(ledger, "1000000000000111", "2000000000000222")
    assert ledger.resolve("1000000000000111") == "1000000000000111"
    assert ledger.resolve("222") == "2000000000000222"
    with pytest.raises(LedgerError, match="no thread ending"):
        ledger.resolve("999")
    with pytest.raises(LedgerError, match="not in the ledger"):
        ledger.resolve("3000000000000333")
    assert ledger.resolve("3000000000000333", must_exist=False) == "3000000000000333"


def test_resolve_ambiguous_suffix(tmp_path):
    ledger = make_ledger(tmp_path)
    add_threads(ledger, "1000000000000123", "2000000000000123")
    with pytest.raises(LedgerError, match="several"):
        ledger.resolve("123")


# ---------------------------------------------------------------- claims

def test_claim_blocks_others_until_expiry(tmp_path):
    clock = Clock()
    ledger = make_ledger(tmp_path, clock=clock)
    add_threads(ledger, "1000000000000111")
    c = ledger.claim("111", "agent:claude-1", minutes=30)
    assert c["by"] == "agent:claude-1" and c["until"] == "2026-10-24T12:30:00Z"
    with pytest.raises(ClaimError, match="claude-1"):
        ledger.claim("111", "agent:codex-2")
    with pytest.raises(ClaimError):
        ledger.check_claim("111", "agent:codex-2")
    ledger.check_claim("111", "agent:claude-1")           # the owner is fine
    ledger.check_claim("111", "agent:codex-2", force=True)
    clock.advance(minutes=31)                              # lease ran out
    ledger.check_claim("111", "agent:codex-2")
    assert ledger.claim("111", "agent:codex-2")["by"] == "agent:codex-2"


def test_owner_can_extend_and_release_others_cannot(tmp_path):
    clock = Clock()
    ledger = make_ledger(tmp_path, clock=clock)
    add_threads(ledger, "1000000000000111")
    ledger.claim("111", "agent:a", minutes=10)
    clock.advance(minutes=5)
    assert ledger.claim("111", "agent:a", minutes=10)["until"] == "2026-10-24T12:15:00Z"
    with pytest.raises(ClaimError):
        ledger.release_claim("111", "agent:b")
    assert ledger.release_claim("111", "agent:a") is True
    assert ledger.release_claim("111", "agent:a") is False
    assert "claim" not in ledger.load()["threads"]["1000000000000111"]


# ---------------------------------------------------------------- triage

def test_update_triage_bumps_rev_and_stamps_reviewer(tmp_path):
    ledger = make_ledger(tmp_path)
    add_threads(ledger, "1000000000000111")
    t1 = ledger.update_triage("111", {"status": "investigating", "cluster": "oven"}, actor="agent:a")
    assert t1["rev"] == 1 and t1["reviewed_by"] == "agent:a" and t1["reviewed"].endswith("Z")
    t2 = ledger.update_triage("111", {"variants": "free, full", "cluster": None}, actor="agent:b", expected_rev=1)
    assert t2["rev"] == 2 and t2["variants"] == ["free", "full"] and "cluster" not in t2


def test_stale_rev_is_a_clear_conflict(tmp_path):
    ledger = make_ledger(tmp_path)
    add_threads(ledger, "1000000000000111")
    ledger.update_triage("111", {"status": "investigating"}, actor="agent:a")
    with pytest.raises(ConflictError) as ei:
        ledger.update_triage("111", {"status": "wontfix"}, actor="agent:b", expected_rev=0)
    assert "rev 1" in str(ei.value) and "expected 0" in str(ei.value)
    assert ledger.load()["threads"]["1000000000000111"]["triage"]["status"] == "investigating"


def test_triage_rejects_bad_status_and_managed_fields(tmp_path):
    ledger = make_ledger(tmp_path)
    add_threads(ledger, "1000000000000111")
    with pytest.raises(LedgerError, match="unknown status"):
        ledger.update_triage("111", {"status": "done"}, actor="x")
    with pytest.raises(LedgerError, match="managed by Hearback"):
        ledger.update_triage("111", {"rev": 99}, actor="x")
    with pytest.raises(LedgerError, match="managed by Hearback"):
        ledger.update_triage("111", {"replies_posted": []}, actor="x")


def test_events_are_appended_and_filterable(tmp_path):
    ledger = make_ledger(tmp_path)
    add_threads(ledger, "1000000000000111", "1000000000000222")
    ledger.claim("111", "agent:a")
    ledger.update_triage("222", {"status": "wontfix"}, actor="human:owner")
    ev = ledger.read_events()
    assert [e["action"] for e in ev] == ["claim", "triage"]
    assert ev[1]["actor"] == "human:owner" and ev[1]["thread"] == "1000000000000222"
    assert [e["action"] for e in ledger.read_events(thread="111")] == ["claim"]
    assert len(ledger.read_events(limit=1)) == 1
    assert set(ev[0]) == {"t", "actor", "action", "thread", "detail"}


def test_sync_posted_edit_and_delete(tmp_path):
    ledger = make_ledger(tmp_path)
    add_threads(ledger, "1000000000000111")
    ledger.add_posted("1000000000000111", [{"message_id": "m1", "content": "old"}, {"message_id": "m2", "content": "x"}])
    assert ledger.sync_posted("1000000000000111", "m1", "new") is True
    assert ledger.sync_posted("1000000000000111", "m2", None) is True
    assert ledger.sync_posted("1000000000000111", "nope", "z") is False
    posted = ledger.load()["threads"]["1000000000000111"]["triage"]["replies_posted"]
    assert posted == [{"message_id": "m1", "content": "new"}]


def test_filter_and_unreviewed(tmp_path):
    ledger = make_ledger(tmp_path)
    add_threads(ledger, "1000000000000111", "1000000000000222", "1000000000000333")
    ledger.update_triage("222", {"status": "need-info", "cluster": "c1"}, actor="a")
    data = ledger.load()
    assert [t for t, _ in filter_threads(data, status="need-info")] == ["1000000000000222"]
    assert [t for t, _ in filter_threads(data, cluster="c1")] == ["1000000000000222"]
    assert [t for t, _ in filter_threads(data, unreviewed_only=True)] == ["1000000000000111", "1000000000000333"]
    assert len(filter_threads(data, limit=2)) == 2
    # a human reply after the review makes it unreviewed again; a bot reply does not
    rec = data["threads"]["1000000000000222"]
    rec["replies"] = [{"time": "2999-01-01T00:00:00Z", "bot": True}]
    assert not is_unreviewed(rec)
    rec["replies"].append({"time": "2999-01-01T00:00:00Z"})
    assert is_unreviewed(rec)
