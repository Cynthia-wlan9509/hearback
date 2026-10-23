"""The ledger: one JSON file that is the memory of what the team already knows.

* every writer does `with ledger.locked(): load -> mutate -> save` (cross-process file lock);
* saves are atomic (temp file + os.replace, LF newlines);
* every mutation appends one line to events.jsonl (who did what, when);
* claims are short leases so several agents do not work on the same thread;
* triage updates use an optimistic revision counter.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import threading
from pathlib import Path
from typing import Any, Callable, Iterator

from hearback.util import (HearbackError, LockTimeout, atomic_write_json, ensure_data_dir, file_lock, iso,
                           now_iso, parse_iso, read_json, sid6, utcnow)

SCHEMA = 1
STATUSES = ("new", "investigating", "need-info", "fixed-pending-release", "released",
            "wontfix", "not-bug", "duplicate", "guide")
PROTECTED_TRIAGE = ("rev", "replies_posted")  # managed by Hearback, not settable through a patch

__all__ = ["Ledger", "LedgerError", "ConflictError", "ClaimError", "LockTimeout", "STATUSES",
           "filter_threads", "is_unreviewed", "triage_of"]


class LedgerError(HearbackError):
    """Bad ledger operation (unknown thread, invalid status, save without lock...)."""


class ConflictError(LedgerError):
    """Someone changed the triage between your read and your write."""

    def __init__(self, message: str, current: int | None = None, expected: int | None = None):
        super().__init__(message)
        self.current = current    # the revision stored now
        self.expected = expected  # the revision the caller thought it was editing


class ClaimError(LedgerError):
    """The thread is claimed by someone else."""


def triage_of(rec: dict) -> dict:
    return rec.get("triage") or {}


def is_unreviewed(rec: dict) -> bool:
    """No review stamp yet, or a human reply arrived after the last review."""
    tri = triage_of(rec)
    reviewed = tri.get("reviewed")
    if not reviewed:
        return tri.get("status", "new") != "guide"
    return any(not r.get("bot") and (r.get("time") or "") > reviewed for r in rec.get("replies") or [])


def filter_threads(data: dict, status: str | None = None, cluster: str | None = None,
                   unreviewed_only: bool = False, limit: int | None = None) -> list[tuple[str, dict]]:
    """Threads (oldest first) matching the filters, as (id, record) pairs."""
    out = []
    for tid in sorted(data.get("threads", {}), key=int):
        rec = data["threads"][tid]
        tri = triage_of(rec)
        if status and tri.get("status", "new") != status:
            continue
        if cluster and tri.get("cluster") != cluster:
            continue
        if unreviewed_only and not is_unreviewed(rec):
            continue
        out.append((tid, rec))
    return out[:limit] if limit else out


class Ledger:
    """Access to `.hearback/ledger.json` and `events.jsonl`."""

    def __init__(self, path: str | Path, *, clock: Callable[[], dt.datetime] | None = None,
                 lock_timeout: float = 15.0, lock_stale: float = 30.0):
        self.path = Path(path)
        self.dir = self.path.parent
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.events_path = self.dir / "events.jsonl"
        self.clock = clock or utcnow
        self.lock_timeout = lock_timeout
        self.lock_stale = lock_stale
        self._rlock = threading.RLock()
        self._depth = 0

    # ------------------------------------------------------------ locking / io
    @contextlib.contextmanager
    def locked(self) -> Iterator["Ledger"]:
        """Exclusive access across threads and processes. Re-entrant within one Ledger object."""
        with self._rlock:
            if self._depth:
                self._depth += 1
                try:
                    yield self
                finally:
                    self._depth -= 1
                return
            ensure_data_dir(self.dir)
            with file_lock(self.lock_path, stale=self.lock_stale, timeout=self.lock_timeout):
                self._depth = 1
                try:
                    yield self
                finally:
                    self._depth = 0

    def load(self) -> dict:
        data = read_json(self.path, None)
        if not isinstance(data, dict):
            data = {}
        data.setdefault("schema", SCHEMA)
        data.setdefault("forum_id", "")
        data.setdefault("synced_at", "")
        data.setdefault("tags", {})
        data.setdefault("threads", {})
        return data

    def save(self, data: dict) -> None:
        if not self._depth:
            raise LedgerError("Ledger.save() must be called inside `with ledger.locked():`")
        ensure_data_dir(self.dir)
        atomic_write_json(self.path, data)

    def abs_path(self, rel: str) -> Path | str:
        """Attachment entries are stored relative to the data dir (or are URLs)."""
        return rel if rel.startswith(("http://", "https://")) else self.dir / rel

    # ------------------------------------------------------------ events
    def event(self, actor: str, action: str, thread: str | None = None, detail: Any = None) -> None:
        line = {"t": now_iso(), "actor": actor, "action": action, "thread": thread, "detail": detail}
        ensure_data_dir(self.dir)
        with self._rlock, open(self.events_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def read_events(self, limit: int | None = None, thread: str | None = None) -> list[dict]:
        out: list[dict] = []
        try:
            with open(self.events_path, encoding="utf-8") as f:
                for ln in f:
                    try:
                        e = json.loads(ln)
                    except ValueError:
                        continue
                    if thread and not str(e.get("thread") or "").endswith(thread):
                        continue
                    out.append(e)
        except FileNotFoundError:
            pass
        return out[-limit:] if limit else out

    # ------------------------------------------------------------ lookup
    def resolve(self, ident: str, data: dict | None = None, must_exist: bool = True) -> str:
        """Full thread id or a unique suffix (e.g. the last six digits) -> full id."""
        ident = str(ident).strip()
        data = data if data is not None else self.load()
        threads = data["threads"]
        if ident in threads:
            return ident
        if ident.isdigit() and len(ident) < 15:
            hits = [t for t in threads if t.endswith(ident)]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise LedgerError("suffix %s matches several threads; use the full id: %s" % (ident, ", ".join(hits)))
            raise LedgerError("no thread ending in %s in the ledger (run `hearback sync`?)" % ident)
        if ident.isdigit() and not must_exist:
            return ident
        raise LedgerError("thread %s is not in the ledger (run `hearback sync`?)" % ident)

    def get(self, ident: str) -> tuple[str, dict]:
        data = self.load()
        tid = self.resolve(ident, data)
        return tid, data["threads"][tid]

    # ------------------------------------------------------------ claims
    def active_claim(self, rec: dict) -> dict | None:
        c = rec.get("claim")
        until = parse_iso(c.get("until")) if isinstance(c, dict) else None
        return c if until and until > self.clock() else None

    def check_claim(self, thread: str, actor: str, force: bool = False) -> None:
        """Raise ClaimError if someone else holds a live claim on the thread."""
        data = self.load()
        tid = self.resolve(thread, data, must_exist=False)
        rec = data["threads"].get(tid)
        c = self.active_claim(rec) if rec else None
        if c and c.get("by") != actor and not force:
            raise ClaimError("thread %s is claimed by %s until %s (use force to override)"
                             % (sid6(tid), c.get("by"), c.get("until")))

    def claim(self, thread: str, actor: str, minutes: float = 30, force: bool = False) -> dict:
        with self.locked():
            data = self.load()
            tid = self.resolve(thread, data)
            rec = data["threads"][tid]
            c = self.active_claim(rec)
            if c and c.get("by") != actor and not force:
                raise ClaimError("thread %s is already claimed by %s until %s"
                                 % (sid6(tid), c.get("by"), c.get("until")))
            rec["claim"] = {"by": actor, "until": iso(self.clock() + dt.timedelta(minutes=minutes))}
            self.save(data)
            self.event(actor, "claim", tid, {"until": rec["claim"]["until"]})
            return dict(rec["claim"])

    def release_claim(self, thread: str, actor: str, force: bool = False) -> bool:
        """Drop the claim. Returns False if there was none. Only the owner (or force) may drop a live claim."""
        with self.locked():
            data = self.load()
            tid = self.resolve(thread, data)
            rec = data["threads"][tid]
            c = rec.get("claim")
            if not c:
                return False
            live = self.active_claim(rec)
            if live and live.get("by") != actor and not force:
                raise ClaimError("thread %s is claimed by %s; only they (or force) can release it"
                                 % (sid6(tid), live.get("by")))
            del rec["claim"]
            self.save(data)
            self.event(actor, "release_claim", tid)
            return True

    # ------------------------------------------------------------ triage
    def update_triage(self, thread: str, patch: dict, *, actor: str,
                      expected_rev: int | None = None, force_claim: bool = False) -> dict:
        """Merge `patch` into triage, bump rev, stamp reviewed/reviewed_by. A None value deletes a key.

        Raises ConflictError if expected_rev is given and no longer matches, and ClaimError if another
        actor holds a live claim on the thread (unless force_claim).
        """
        bad = [k for k in patch if k in PROTECTED_TRIAGE]
        if bad:
            raise LedgerError("triage field(s) managed by Hearback, cannot be set: %s" % ", ".join(bad))
        if "status" in patch and patch["status"] not in STATUSES:
            raise LedgerError("unknown status %r; use one of: %s" % (patch["status"], ", ".join(STATUSES)))
        patch = dict(patch)
        if "variants" in patch and patch["variants"] is not None:
            v = patch["variants"]
            patch["variants"] = [x.strip() for x in v.split(",") if x.strip()] if isinstance(v, str) else list(v)
        with self.locked():
            data = self.load()
            tid = self.resolve(thread, data)
            rec = data["threads"][tid]
            live = self.active_claim(rec)
            if live and live.get("by") != actor and not force_claim:
                raise ClaimError("thread %s is claimed by %s until %s; leave its triage to them "
                                 "(use force to override)" % (sid6(tid), live.get("by"), live.get("until")))
            tri = rec.setdefault("triage", {"status": "new"})
            rev = int(tri.get("rev", 0))
            if expected_rev is not None and int(expected_rev) != rev:
                raise ConflictError("triage of %s changed (rev %d, you expected %s); re-read it with "
                                    "`hearback show %s` and retry" % (sid6(tid), rev, expected_rev, sid6(tid)),
                                    current=rev, expected=int(expected_rev))
            for k, v in patch.items():
                if v is None:
                    tri.pop(k, None)
                else:
                    tri[k] = v
            tri["rev"] = rev + 1
            tri["reviewed"] = now_iso()
            tri["reviewed_by"] = actor
            self.save(data)
            self.event(actor, "triage", tid, {"rev": tri["rev"], **{k: patch[k] for k in patch}})
            return dict(tri)

    # ------------------------------------------------------------ bot-posted bookkeeping
    def add_posted(self, thread: str, entries: list[dict]) -> None:
        """Append to triage.replies_posted (messages the bot sent into the thread)."""
        with self.locked():
            data = self.load()
            rec = data["threads"].get(thread)
            if rec is None:
                return
            rec.setdefault("triage", {"status": "new"}).setdefault("replies_posted", []).extend(entries)
            self.save(data)

    def sync_posted(self, thread: str, message_id: str, new_content: str | None) -> bool:
        """After an edit (new_content) or delete (None) of a bot message, keep replies_posted in step."""
        with self.locked():
            data = self.load()
            rec = data["threads"].get(thread)
            if rec is None:
                return False
            tri = rec.get("triage") or {}
            posted = tri.get("replies_posted") or []
            hit = [r for r in posted if r.get("message_id") == message_id]
            if not hit:
                return False
            if new_content is None:
                tri["replies_posted"] = [r for r in posted if r.get("message_id") != message_id]
            else:
                for r in hit:
                    r["content"] = new_content
            self.save(data)
            return True
