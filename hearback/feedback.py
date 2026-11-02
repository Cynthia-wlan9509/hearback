"""The feedback inbox of the SWLink console: every forum post of a server in one list, worked through by a person.

Nothing here spends model tokens. A post is "todo" when someone other than the staff spoke last; a reply from the bot,
the server owner or an approver settles it, and a later message from anyone else brings it back. A person can override
that with a mark ("no reply needed" or "back to todo") that holds until someone new writes. Pinned posts and guides are
not feedback. Which agent a forum's AI help uses, and which forums to leave out, live in `.hearback/forums.json`.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Mapping

from hearback.ledger import Ledger, triage_of
from hearback.llm import price_of
from hearback.review import _later, ai_is_stale
from hearback.util import atomic_write_json, ensure_data_dir, read_json_strict, short

NO_AGENT = "none"                  # a forum whose posts get no AI help
CLOSED_STATUSES = ("released", "wontfix", "not-bug", "duplicate")
ESTIMATE_TOKENS = (8000, 500)      # what one post usually takes (input, output), for the "about $x" before a bulk run


def _num(ident: Any) -> int:
    s = str(ident or "")
    return int(s) if s.isdigit() else 0


def staff_ids(data: Mapping[str, Any], approvers: Any = ()) -> set[str]:
    """Who counts as the staff of this server: the bot, the owner and the configured approvers."""
    return {str(x) for x in (data.get("bot_user_id"), data.get("owner_id"), *approvers) if x}


def _last_ids(tid: str, rec: Mapping[str, Any], staff: set[str]) -> tuple[int, str, int]:
    """(id and time of the last message from someone outside the staff, id of the last staff message); ids are 0 when none."""
    tri = triage_of(dict(rec))
    posted = {str(p.get("message_id")) for p in tri.get("replies_posted") or []}
    player, when, own = 0, "", max(map(_num, posted), default=0)
    if str(rec.get("author_id") or "") in staff:
        own = max(own, _num(tid))
    else:
        player, when = _num(tid), str(rec.get("created") or "")
    for r in rec.get("replies") or []:
        rid = _num(r.get("id"))
        if r.get("bot") or str(r.get("id")) in posted or str(r.get("author_id") or "") in staff:
            own = max(own, rid)
        elif rid > player:
            player, when = rid, str(r.get("time") or "")
    return player, when, own


def inbox_state(tid: str, rec: Mapping[str, Any], staff: set[str]) -> str | None:
    """"todo", "done", or None for what is not feedback at all."""
    tri = triage_of(dict(rec))
    if rec.get("pinned") or tri.get("status") == "guide":
        return None
    player, when, own = _last_ids(tid, rec, staff)
    mark = tri.get("inbox")
    if isinstance(mark, dict) and _num(mark.get("at")) >= player:
        return "todo" if mark.get("state") == "open" else "done"
    if not player or own > player:
        return "done"
    if tri.get("status") in CLOSED_STATUSES and not _later(when, tri.get("reviewed")):
        return "done"
    return "todo"


def set_mark(ledger: Ledger, tid: str, state: str | None) -> None:
    """A person's say on one post: "done", "open", or None to go back to what the messages say."""
    with ledger.locked():
        data = ledger.load()
        rec = data["threads"][tid]
        tri = rec.get("triage") if isinstance(rec.get("triage"), dict) else rec.setdefault("triage", {"status": "new"})
        if state is None:
            tri.pop("inbox", None)
        else:
            tri["inbox"] = {"state": state, "at": str(rec.get("last_message_id") or tid)}
        ledger.save(data)


def has_draft(rec: Mapping[str, Any]) -> bool:
    ai = triage_of(dict(rec)).get("ai")
    return isinstance(ai, dict) and bool(ai.get("reply_draft")) and not ai_is_stale(rec)


def row(tid: str, rec: Mapping[str, Any], state: str, staff: set[str], forum: str) -> dict:
    """One post as the list shows it."""
    player, when, own = _last_ids(tid, rec, staff)
    last = next((r for r in reversed(rec.get("replies") or []) if _num(r.get("id")) == player), None)
    return {"id": tid, "forum": forum, "title": rec.get("title", ""), "author": rec.get("author", ""), "state": state,
            "time": when or rec.get("created", ""), "fresh": "" if state != "todo" else ("reply" if own else "new"),
            "preview": short((last or {}).get("content") or rec.get("body") or "", 120), "has_draft": has_draft(rec),
            "tags": list(rec.get("tags") or [])}


class ForumSettings:
    """{forum id: {"agent": agent id or "none", "hidden": bool}}. A forum that is not listed uses the default agent."""

    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.path = self.dir / "forums.json"
        self._lock = threading.RLock()

    def load(self) -> dict[str, dict]:
        d = read_json_strict(self.path, {}, "the forums file")
        return d if isinstance(d, dict) else {}

    def get(self, forum_id: str) -> dict:
        v = self.load().get(forum_id)
        return v if isinstance(v, dict) else {}

    def set(self, forum_id: str, **fields: Any) -> dict:
        with self._lock:
            d = self.load()
            d[forum_id] = {**(d.get(forum_id) if isinstance(d.get(forum_id), dict) else {}), **fields}
            ensure_data_dir(self.dir)
            atomic_write_json(self.path, d)
            return d[forum_id]

    def hidden(self) -> set[str]:
        return {f for f, v in self.load().items() if isinstance(v, dict) and v.get("hidden")}


class Usage:
    """What the model calls cost, month by month, in `.hearback/usage.json`. Only calls a known price covers have a cost."""

    def __init__(self, data_dir: str | Path, clock: Any = time.time):
        self.dir = Path(data_dir)
        self.path = self.dir / "usage.json"
        self.clock = clock
        self._lock = threading.Lock()

    def _month(self) -> str:
        return time.strftime("%Y-%m", time.localtime(self.clock()))

    def add(self, model: str, tokens: tuple[int, int] | None) -> None:
        price = price_of(model)
        with self._lock:
            d = read_json_strict(self.path, {}, "the usage file")
            d = d if isinstance(d, dict) else {}
            m = d.setdefault(self._month(), {"calls": 0, "unpriced": 0, "cost": 0.0})
            m["calls"] += 1
            if price and tokens:
                m["cost"] = round(m["cost"] + (tokens[0] * price[0] + tokens[1] * price[1]) / 1e6, 6)
            else:
                m["unpriced"] += 1
            ensure_data_dir(self.dir)
            atomic_write_json(self.path, d)

    def this_month(self) -> dict:
        d = read_json_strict(self.path, {}, "the usage file")
        m = d.get(self._month()) if isinstance(d, dict) else None
        return m if isinstance(m, dict) else {"calls": 0, "unpriced": 0, "cost": 0.0}


def estimate(model: str) -> float | None:
    """About what one post costs with this model; None when its price is not known."""
    price = price_of(model)
    return (ESTIMATE_TOKENS[0] * price[0] + ESTIMATE_TOKENS[1] * price[1]) / 1e6 if price else None
