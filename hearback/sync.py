"""Incremental sync: Discord forum -> ledger. Never touches triage or claims.

Threads whose last_message_id, tags, title and archived flag match the ledger are skipped without
fetching their messages. The result is a Delta sized for an LLM context.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any

from hearback.config import Config
from hearback.discord import DiscordClient
from hearback.ledger import Ledger
from hearback.util import (IMAGE_EXT, HearbackError, is_image, norm_ts, now_iso, safe_name, short,
                           snowflake_time, warn)

OK_MSG_TYPES = (0, 19, 20, 21)  # default / reply / slash command / thread starter; the rest are system messages
DOWNLOAD_EXT = IMAGE_EXT + (".txt", ".log", ".json")   # images and plain-text logs only
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


@dataclass
class DeltaEntry:
    tid: str
    title: str
    author: str
    tags: list[str]
    body: str
    images: int = 0
    is_new: bool = False
    replies: list[dict] = field(default_factory=list)   # new human replies: author, content, images
    tag_change: tuple[list[str], list[str]] | None = None  # (old, new)


@dataclass
class Delta:
    total: int = 0
    skipped: int = 0
    entries: list[DeltaEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def new_threads(self) -> list[DeltaEntry]:
        return [e for e in self.entries if e.is_new]

    @property
    def with_replies(self) -> list[DeltaEntry]:
        return [e for e in self.entries if e.replies]

    @property
    def tag_changes(self) -> list[DeltaEntry]:
        return [e for e in self.entries if e.tag_change]

    def to_dict(self) -> dict:
        return {"total": self.total, "skipped": self.skipped, "new": len(self.new_threads),
                "with_new_replies": len(self.with_replies), "tag_changes": len(self.tag_changes),
                "warnings": self.warnings, "entries": [asdict(e) for e in self.entries]}


# ---------------------------------------------------------------- fetching

def _author(msg: dict | None) -> str:
    a = (msg or {}).get("author") or {}
    return a.get("global_name") or a.get("username") or "?"


def fetch_threads(client: DiscordClient, guild_id: str, forum_id: str) -> dict[str, dict]:
    """All threads of the forum: active + archived public (paged). {thread_id: thread object}."""
    found: dict[str, dict] = {}
    data = client.request("GET", "/guilds/%s/threads/active" % guild_id)
    for t in data.get("threads", []):
        if str(t.get("parent_id")) == forum_id:
            found[str(t["id"])] = t
    before = None
    while True:
        params: dict[str, Any] = {"limit": 100}
        if before:
            params["before"] = before
        data = client.request("GET", "/channels/%s/threads/archived/public" % forum_id, params)
        threads = data.get("threads", [])
        for t in threads:
            found.setdefault(str(t["id"]), t)
        if not data.get("has_more") or not threads:
            break
        nxt = (threads[-1].get("thread_metadata") or {}).get("archive_timestamp")
        if not nxt or nxt == before:
            break
        before = nxt
    return found


def fetch_messages(client: DiscordClient, channel_id: str) -> list[dict]:
    """Every message of a thread, oldest first (100 per page, paging back with `before`)."""
    msgs: dict[str, dict] = {}
    before = None
    for _ in range(200):
        params: dict[str, Any] = {"limit": 100}
        if before:
            params["before"] = before
        batch = client.request("GET", "/channels/%s/messages" % channel_id, params)
        if not batch:
            break
        for m in batch:
            msgs[str(m["id"])] = m
        if len(batch) < 100:
            break
        before = str(min(int(m["id"]) for m in batch))
    return sorted(msgs.values(), key=lambda m: int(m["id"]))


def _want_download(att: dict) -> bool:
    """Only images and .txt/.log/.json are fetched: no archives, videos or arbitrary text types."""
    return os.path.splitext((att.get("filename") or "").lower())[1] in DOWNLOAD_EXT


class _Attachments:
    """Resolve attachments to ledger strings: a path relative to the data dir, or the URL."""

    def __init__(self, client: DiscordClient, ledger: Ledger, download: bool):
        self.client, self.ledger, self.download = client, ledger, download
        self.failures = 0     # downloads that were attempted and failed

    def one(self, tid: str, att: dict) -> tuple[str | None, bool]:
        """(ledger string or None, download failed). A failed download keeps the URL."""
        if not _want_download(att):
            return None, False
        rel = "attachments/%s/%s_%s" % (tid, att["id"], safe_name(att.get("filename")))
        dest = self.ledger.dir / rel
        size = att.get("size")
        if dest.exists() and (size is None or dest.stat().st_size == size):
            return rel, False
        if not self.download or (size or 0) > MAX_ATTACHMENT_BYTES:
            return att["url"], False          # by choice or policy, not a failure
        try:
            self.client.download(att["url"], dest)
        except HearbackError as e:
            warn(str(e))
            self.failures += 1
            return att["url"], True
        return rel, False

    def many(self, tid: str, msg: dict | None) -> tuple[list[str], bool]:
        """(ledger strings, whether any download failed)."""
        out, failed = [], False
        for a in (msg or {}).get("attachments") or []:
            p, bad = self.one(tid, a)
            failed = failed or bad
            if p:
                out.append(p)
        return out, failed


def build_record(t: dict, msgs: list[dict], tag_map: dict[str, str], atts: _Attachments,
                 bot_id: str) -> dict:
    """Thread object + messages -> a fresh ledger record (without triage)."""
    tid = str(t["id"])
    msgs = [m for m in msgs if m.get("type", 0) in OK_MSG_TYPES]
    starter = next((m for m in msgs if str(m["id"]) == tid), None) or (msgs[0] if msgs else None)
    replies = []
    any_failed = False
    for m in msgs:
        if m is starter:
            continue
        files, failed = atts.many(tid, m)
        r = {"id": str(m["id"]), "author": _author(m), "author_id": str((m.get("author") or {}).get("id", "")),
             "time": norm_ts(m.get("timestamp")), "content": m.get("content") or "", "attachments": files}
        if r["author_id"] == bot_id:
            r["bot"] = True
        if failed:
            r["download_failed"] = True
            any_failed = True
        replies.append(r)
    meta = t.get("thread_metadata") or {}
    body_files, body_failed = atts.many(tid, starter)
    rec = {
        "title": t.get("name") or "",
        "author": _author(starter) if starter else "?",
        "author_id": str(((starter or {}).get("author") or {}).get("id", "")),
        "created": snowflake_time(tid),
        "tags": [tag_map.get(str(i), "?" + str(i)) for i in t.get("applied_tags") or []],
        "message_count": t.get("message_count", max(len(msgs) - 1, 0)),
        "last_message_id": str(t.get("last_message_id") or (msgs[-1]["id"] if msgs else "")),
        "archived": bool(meta.get("archived")),
        "pinned": bool(int(t.get("flags") or 0) & 2),
        "body": (starter.get("content") or "") if starter else "",
        "attachments": body_files,
        "replies": replies,
    }
    if "total_message_sent" in t:
        rec["total_message_sent"] = t["total_message_sent"]
    if body_failed or any_failed:
        rec["download_failed"] = True     # URLs kept; `sync --full` tries the downloads again
    return rec


def merge_record(old: dict | None, new: dict) -> dict:
    """New Discord data wins; triage, claim and any field Hearback does not know are kept from `old`."""
    rec = dict(new)
    old = old or {}
    rec["triage"] = old.get("triage") or {"status": "new"}
    for k, v in old.items():
        if k != "download_failed":       # re-derived on every fetch
            rec.setdefault(k, v)
    return rec


def _unchanged(old: dict | None, t: dict, tags: list[str]) -> bool:
    if not old:
        return False
    meta = t.get("thread_metadata") or {}
    return (str(old.get("last_message_id")) == str(t.get("last_message_id") or "")
            and set(old.get("tags", [])) == set(tags)
            and old.get("title") == (t.get("name") or "")
            and bool(old.get("archived")) == bool(meta.get("archived"))
            # a deleted or added message that is not the last one changes the count but not last_message_id
            and all(old.get(k) == t[k] for k in ("message_count", "total_message_sent") if k in t))


# ---------------------------------------------------------------- the sync itself

def sync(client: DiscordClient, ledger: Ledger, cfg: Config, *, download: bool = True,
         actor: str = "human:cli", full: bool = False) -> Delta:
    """Pull changes from Discord into the ledger and report what is new.

    `full` refetches every thread (catches edits and deletions the cheap change check cannot see, and
    retries attachment downloads that failed earlier).

    Network work (and attachment downloads) happens outside the ledger lock; the lock is only held
    to merge the fetched records into the freshest copy of the ledger.
    """
    forum_id = cfg.require_forum()
    forum = client.request("GET", "/channels/%s" % forum_id)
    tag_map = {str(t["id"]): t["name"] for t in forum.get("available_tags", [])}
    guild_id = cfg.guild_id or str(forum.get("guild_id") or "")
    if not guild_id:
        raise HearbackError("guild_id is not set and could not be read from the forum channel")
    snapshot = ledger.load()
    bot_id = str(snapshot.get("bot_user_id") or client.me()["id"])

    threads = fetch_threads(client, guild_id, forum_id)
    atts = _Attachments(client, ledger, download)
    fetched: dict[str, dict] = {}
    skipped = 0
    for tid in sorted(threads, key=int):
        t = threads[tid]
        tags = [tag_map.get(str(i), "?" + str(i)) for i in t.get("applied_tags") or []]
        if not full and _unchanged(snapshot["threads"].get(tid), t, tags):
            skipped += 1
            continue
        fetched[tid] = build_record(t, fetch_messages(client, tid), tag_map, atts, bot_id)

    delta = Delta(total=0, skipped=skipped)
    with ledger.locked():
        data = ledger.load()
        data.update(forum_id=forum_id, guild_id=guild_id, tags=tag_map, bot_user_id=bot_id)
        for tid, new in fetched.items():
            old = data["threads"].get(tid)
            rec = merge_record(old, new)
            data["threads"][tid] = rec
            seen = {r.get("id") for r in (old or {}).get("replies", [])}
            seen |= {p.get("message_id") for p in (rec.get("triage") or {}).get("replies_posted", [])}
            new_replies = [r for r in rec["replies"] if r["id"] not in seen and not r.get("bot")]
            tag_change = None
            if old is not None and set(old.get("tags", [])) != set(rec["tags"]):
                tag_change = (list(old.get("tags", [])), list(rec["tags"]))
            if old is not None and not new_replies and not tag_change:
                continue
            show = rec["replies"] if old is None else new_replies
            delta.entries.append(DeltaEntry(
                tid=tid, title=rec["title"], author=rec["author"], tags=list(rec["tags"]), body=rec["body"],
                images=sum(1 for p in rec["attachments"] if is_image(p)), is_new=old is None,
                replies=[{"author": r["author"], "content": r["content"],
                          "images": sum(1 for p in r["attachments"] if is_image(p)),
                          "files": len(r["attachments"])} for r in show if not r.get("bot")],
                tag_change=tag_change))
        data["synced_at"] = now_iso()
        delta.total = len(data["threads"])
        if data["threads"] and all(not r.get("body") and not r.get("attachments") for r in data["threads"].values()):
            delta.warnings.append("every post body is empty: MESSAGE CONTENT INTENT is probably off "
                                  "(Bot page in the developer portal)")
        if atts.failures:
            delta.warnings.append("%d attachment download(s) failed; their URLs are kept (download_failed). "
                                  "Run a full sync to retry." % atts.failures)
        ledger.save(data)
        ledger.event(actor, "sync", None, {"total": delta.total, "new": len(delta.new_threads),
                                           "replies": len(delta.with_replies), "tags": len(delta.tag_changes),
                                           "skipped": skipped})
    return delta


# ---------------------------------------------------------------- output

def _thread_line(e: DeltaEntry) -> str:
    s = "%s [%s] %s — %s: %s" % (e.tid, ",".join(e.tags), e.title, e.author, short(e.body, 80))
    return s + (" (+%d img)" % e.images if e.images else "")


def _reply_line(r: dict) -> str:
    txt = short(r["content"], 100) or ("(attachment only)" if r.get("files") else "")
    return "  - %s: %s%s" % (r["author"], txt, " (+%d img)" % r["images"] if r.get("images") else "")


def format_delta(delta: Delta, json_out: bool = False) -> str:
    """Compact text for humans and LLMs, or the same data as JSON."""
    if json_out:
        return json.dumps(delta.to_dict(), ensure_ascii=False, indent=1)
    lines = ["synced: %d threads, %d new, %d with new replies, %d tag changes, %d unchanged"
             % (delta.total, len(delta.new_threads), len(delta.with_replies), len(delta.tag_changes),
                delta.skipped)]
    for e in delta.entries:
        lines.append(_thread_line(e))
        if e.tag_change:
            lines.append("  tags: %s -> %s" % (",".join(e.tag_change[0]), ",".join(e.tag_change[1])))
        lines += [_reply_line(r) for r in e.replies]
    lines += ["warning: " + w for w in delta.warnings]
    return "\n".join(lines)
