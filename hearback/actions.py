"""Low-level executors: the only code that writes to Discord.

Each function talks to the REST client and keeps the ledger's bookkeeping (tags, replies_posted)
in step. Nothing here decides *whether* to act: that is the approval queue's job.
"""
from __future__ import annotations

from typing import Any, Callable, Sequence

from hearback.discord import DiscordClient, DiscordError, load_attachments, msg_body
from hearback.ledger import Ledger
from hearback.util import (FLAG_PINNED, MAX_TAGS, MSG_LIMIT, TITLE_LIMIT, HearbackError, chunks_for, norm_ts,
                           now_iso, short, sid6, snowflake_time)

Pause = Callable[[], None]


class PartialSend(HearbackError):
    """Some chunks of a multi-message action were posted before an error. `result` lists them."""

    def __init__(self, message: str, result: dict, messages: list[dict] | None = None):
        super().__init__(message)
        self.result = result
        self.messages = messages or []   # the Discord message objects that did go out


# ---------------------------------------------------------------- tags

def tag_lookup(table: dict[str, str], name: str) -> str | None:
    """Tag name -> id: exact, then case-insensitive, then the '?<id>' placeholder sync writes."""
    for i, nm in table.items():
        if nm == name:
            return i
    for i, nm in table.items():
        if nm.lower() == name.lower():
            return i
    if name.startswith("?") and name[1:].isdigit():
        return name[1:]
    return None


def resolve_tag_ids(client: DiscordClient, ledger: Ledger, forum_id: str, names: Sequence[str]) -> list[str]:
    """Names -> ids using the ledger's tag table; refreshes it from the forum once if a name is missing."""
    names = list(dict.fromkeys(names))
    own = forum_id == ledger.load().get("forum_id")      # the ledger only caches its own forum's tags
    table = ledger.load()["tags"] if own else {}
    if not own or any(tag_lookup(table, n) is None for n in names):
        forum = client.request("GET", "/channels/%s" % forum_id)
        table = {str(t["id"]): t["name"] for t in forum.get("available_tags", [])}
        if own:
            with ledger.locked():
                data = ledger.load()
                data["tags"] = table
                ledger.save(data)
    out = []
    for n in names:
        i = tag_lookup(table, n)
        if i is None:
            raise HearbackError("no forum tag named %r. Available: %s" % (n, ", ".join(table.values()) or "(none)"))
        out.append(i)
    return out


def forum_of(ledger: Ledger, forum_id: str = "") -> str:
    fid = forum_id or ledger.load().get("forum_id", "")
    if not fid:
        raise HearbackError("forum id unknown: set [discord] forum_id in hearback.toml")
    return fid


def set_tags(client: DiscordClient, ledger: Ledger, thread: str, add: Sequence[str], remove: Sequence[str],
             forum_id: str = "") -> dict:
    """Add/remove forum tags on a thread (at most 5 in total). Reads the live tag list first."""
    fid = forum_of(ledger, forum_id)
    add_ids = resolve_tag_ids(client, ledger, fid, add)
    rem_ids = resolve_tag_ids(client, ledger, fid, remove)
    live = client.request("GET", "/channels/%s" % thread)
    cur = [str(i) for i in live.get("applied_tags") or []]
    new = [i for i in cur if i not in rem_ids]
    new += [i for i in add_ids if i not in new]
    if len(new) > MAX_TAGS:
        raise HearbackError("thread %s would have %d tags; Discord allows %d. Remove some first."
                            % (sid6(thread), len(new), MAX_TAGS))
    changed = set(new) != set(cur)
    if changed:
        client.request("PATCH", "/channels/%s" % thread, body={"applied_tags": new})
    table = ledger.load()["tags"]
    with ledger.locked():
        data = ledger.load()
        rec = data["threads"].get(thread)
        if rec is not None:
            rec["tags"] = [table.get(i, "?" + i) for i in new]
            ledger.save(data)
    return {"tag_ids": new, "changed": changed}


# ---------------------------------------------------------------- messages

def _send_chunks(client: DiscordClient, channel: str, chunks: list[str], atts: list[dict], pause: Pause,
                 first_index: int = 0) -> list[dict]:
    """Post chunks in order; attachments go on the last one. Raises PartialSend after a partial failure."""
    sent: list[dict] = []
    for i, text in enumerate(chunks):
        if i or first_index:
            pause()
        a = atts if i == len(chunks) - 1 else []
        try:
            sent.append(client.request("POST", "/channels/%s/messages" % channel, body=msg_body(text, a),
                                      files=a or None))
        except DiscordError as e:
            raise PartialSend("message %d/%d failed (%d already posted): %s" % (i + 1, len(chunks), len(sent), e),
                              {"message_ids": [str(m.get("id")) for m in sent], "failed_at": i + 1}, sent)
    return sent


def _posted_entries(sent: list[dict], chunks: list[str], atts: list[dict], proposal_id: str) -> list[dict]:
    out = []
    for i, m in enumerate(sent):
        e = {"message_id": str(m.get("id", "")), "time": norm_ts(m.get("timestamp")) or now_iso(),
             "content": chunks[i], "proposal": proposal_id}
        if atts and i == len(sent) - 1:
            e["attachments"] = [a["filename"] for a in atts]
        out.append(e)
    return out


def _send_recorded(client: DiscordClient, ledger: Ledger, channel: str, chunks: list[str], atts: list[dict],
                   proposal_id: str, pause: Pause) -> list[dict]:
    """_send_chunks + replies_posted bookkeeping, also for the chunks that went out before a failure."""
    try:
        sent = _send_chunks(client, channel, chunks, atts, pause)
    except PartialSend as e:
        if e.messages:
            ledger.add_posted(channel, _posted_entries(e.messages, chunks, [], proposal_id))
        raise
    ledger.add_posted(channel, _posted_entries(sent, chunks, atts, proposal_id))
    return sent


def reply(client: DiscordClient, ledger: Ledger, thread: str, text: str, attachments: Sequence[str] = (),
          proposal_id: str = "", pause: Pause = lambda: None) -> dict:
    """Reply in a thread (text over 2000 characters becomes several messages) and record it."""
    atts = load_attachments(attachments)
    chunks = chunks_for(text)
    if not text.strip() and not atts:
        raise HearbackError("reply is empty (needs text or an attachment)")
    sent = _send_recorded(client, ledger, thread, chunks, atts, proposal_id, pause)
    return {"message_ids": [str(m.get("id")) for m in sent], "chunks": len(chunks)}


def post(client: DiscordClient, ledger: Ledger, channel: str, text: str, attachments: Sequence[str] = (),
         proposal_id: str = "", pause: Pause = lambda: None) -> dict:
    """Send to any channel or thread. If it is a thread in the ledger it is recorded like a reply."""
    atts = load_attachments(attachments)
    chunks = chunks_for(text)
    if not text.strip() and not atts:
        raise HearbackError("message is empty (needs text or an attachment)")
    sent = _send_recorded(client, ledger, channel, chunks, atts, proposal_id, pause)
    return {"message_ids": [str(m.get("id")) for m in sent], "chunks": len(chunks)}


def newpost(client: DiscordClient, ledger: Ledger, forum: str, title: str, text: str, tags: Sequence[str] = (),
            attachments: Sequence[str] = (), pin: bool = False, proposal_id: str = "",
            pause: Pause = lambda: None) -> dict:
    """Create a forum post (first chunk = body, the rest = follow-up messages), optionally pin it."""
    title = title.strip()
    if not title:
        raise HearbackError("post title is empty")
    if len(title) > TITLE_LIMIT:
        raise HearbackError("title is %d characters; Discord allows %d" % (len(title), TITLE_LIMIT))
    atts = load_attachments(attachments)
    chunks = chunks_for(text)
    if not text.strip() and not atts:
        raise HearbackError("post is empty (needs text or an attachment)")
    tag_ids = resolve_tag_ids(client, ledger, forum, tags) if tags else []
    if len(tag_ids) > MAX_TAGS:
        raise HearbackError("%d tags; Discord allows %d per post" % (len(tag_ids), MAX_TAGS))
    first_atts = atts if len(chunks) == 1 else []
    th = client.request("POST", "/channels/%s/threads" % forum, files=first_atts or None,
                        body={"name": title, "applied_tags": tag_ids, "message": msg_body(chunks[0], first_atts)})
    tid = str(th.get("id", ""))
    if not tid:
        raise HearbackError("Discord created the post but returned no id: %s" % short(str(th), 200))
    result: dict[str, Any] = {"thread_id": tid, "message_ids": [tid], "tag_ids": tag_ids}
    table = ledger.load()["tags"]
    in_ledger = ledger.load().get("forum_id") == forum
    if in_ledger:
        with ledger.locked():
            data = ledger.load()
            data["threads"][tid] = {
                "title": title, "author": "(bot)", "created": snowflake_time(tid),
                "tags": [table.get(i, "?" + i) for i in tag_ids], "message_count": len(chunks) - 1,
                "last_message_id": str(th.get("last_message_id") or tid), "archived": False, "pinned": False,
                "body": chunks[0], "attachments": [], "replies": [],
                "triage": {"status": "guide", "cluster": "guide", "root_cause": "post by the bot itself, not a bug",
                           "reviewed": now_iso(), "rev": 1,
                           "replies_posted": [{"message_id": tid, "time": snowflake_time(tid),
                                               "content": chunks[0], "proposal": proposal_id}]}}
            ledger.save(data)
    try:
        if pin:
            pause()
            set_pinned(client, tid, True)
            result["pinned"] = True
        if len(chunks) > 1:
            more = _send_chunks(client, tid, chunks[1:], atts, pause, first_index=1)
            result["message_ids"] += [str(m.get("id")) for m in more]
            if in_ledger:
                entries = _posted_entries(more, chunks[1:], atts, proposal_id)
                ledger.add_posted(tid, entries)
    except HearbackError as e:
        partial = getattr(e, "result", {})
        result["message_ids"] += partial.get("message_ids", [])
        raise PartialSend("post %s was created (thread %s) but a later step failed: %s" % (title, tid, e), result)
    return result


def set_pinned(client: DiscordClient, thread: str, pinned: bool) -> dict:
    """Pin/unpin by flipping only the PINNED bit of the thread's flags."""
    live = client.request("GET", "/channels/%s" % thread)
    flags = int(live.get("flags") or 0)
    new = flags | FLAG_PINNED if pinned else flags & ~FLAG_PINNED
    if new != flags:
        client.request("PATCH", "/channels/%s" % thread, body={"flags": new})
    return {"pinned": pinned, "changed": new != flags}


def edit_message(client: DiscordClient, ledger: Ledger, channel: str, message_id: str, text: str) -> dict:
    """Replace the text of a message the bot sent; keeps replies_posted in step."""
    if not text.strip():
        raise HearbackError("new text is empty; delete the message instead")
    if len(text) > MSG_LIMIT:
        raise HearbackError("text is %d characters; a single message holds at most %d" % (len(text), MSG_LIMIT))
    client.request("PATCH", "/channels/%s/messages/%s" % (channel, message_id), body=msg_body(text, []))
    ledger.sync_posted(channel, message_id, text)
    return {"message_ids": [message_id]}


def delete_message(client: DiscordClient, ledger: Ledger, channel: str, message_id: str) -> dict:
    client.request("DELETE", "/channels/%s/messages/%s" % (channel, message_id))
    ledger.sync_posted(channel, message_id, None)
    return {"message_ids": [message_id], "deleted": True}


def list_bot_messages(client: DiscordClient, channel: str, bot_id: str, limit: int = 100) -> list[dict]:
    """The bot's own recent messages in a channel or thread, newest first."""
    raw = client.request("GET", "/channels/%s/messages" % channel, {"limit": limit})
    return [m for m in raw if str((m.get("author") or {}).get("id")) == str(bot_id)]


# ---------------------------------------------------------------- dispatch

def execute(p: Any, client: DiscordClient, ledger: Ledger, forum_id: str = "",
            pause: Pause = lambda: None) -> dict:
    """Run one approved proposal. Returns the result dict stored in the proposal."""
    k = p.kind
    if k == "reply":
        return reply(client, ledger, p.thread, p.text, p.attachments, p.id, pause)
    if k == "post":
        return post(client, ledger, p.thread, p.text, p.attachments, p.id, pause)
    if k == "tags":
        return set_tags(client, ledger, p.thread, p.add, p.remove, forum_id)
    if k == "newpost":
        return newpost(client, ledger, p.thread or forum_of(ledger, forum_id), p.title, p.text, p.add,
                       p.attachments, p.pin, p.id, pause)
    if k == "pin":
        return set_pinned(client, p.thread, p.pin)
    if k == "edit":
        return edit_message(client, ledger, p.thread, p.message_id, p.text)
    if k == "delete":
        return delete_message(client, ledger, p.thread, p.message_id)
    raise HearbackError("unknown proposal kind %r" % k)
