"""Approve by reaction in a private staff channel (REST only, no gateway connection).

`push` posts each pending proposal there with its exact preview and adds the reactions;
`poll` reads the reactions and approves/rejects when the reacting user is on the allow-list.

A reaction only counts on a message that this bot posted for exactly this proposal (author, proposal id
and a content digest are checked), so an edited proposal file or a swapped message id cannot borrow
someone else's approval. Forum-derived text in previews is defanged: it can never ping anyone.
"""
from __future__ import annotations

import dataclasses
import hashlib
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable

from hearback.config import Config
from hearback.discord import DiscordClient, DiscordError, msg_body
from hearback.queue import Proposal, Queue
from hearback.templates import defang_mentions, sanitize
from hearback.util import MSG_LIMIT, HearbackError

APPROVE = "✅"
REJECT = "❌"


@dataclass
class PushResult:
    pushed: list[Proposal] = field(default_factory=list)
    refused: list[tuple[Proposal, int]] = field(default_factory=list)   # (proposal, characters in the full preview)

    def lines(self) -> list[str]:
        out = ["pushed %d proposal(s) to the staff channel" % len(self.pushed)] + ["  " + p.id for p in self.pushed]
        out += ["  REFUSED %s: the preview (%d characters) would be truncated in the staff channel. Review it with "
                "`hearback queue show %s`, or push anyway with --allow-truncated" % (p.id, n, p.id)
                for p, n in self.refused]
        return out


@dataclass
class PollResult:
    approved: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)      # message missing/foreign, or text changed after it was posted
    errors: list[str] = field(default_factory=list)     # why a proposal was marked stale
    ignored: int = 0                                     # reactions from users who are not approvers

    def lines(self) -> list[str]:
        out = ["staff poll: %d approved, %d rejected, %d stale, %d ignored reaction(s)"
               % (len(self.approved), len(self.rejected), len(self.stale), self.ignored)]
        out += ["  approved " + i for i in self.approved] + ["  rejected " + i for i in self.rejected]
        out += ["  stale    " + e for e in self.errors]
        return out


def _digest(queue: Queue, p: Proposal) -> str:
    return hashlib.sha1(queue.render(p, {}).encode("utf-8")).hexdigest()[:12]


def _safe_data(data: dict | None, p: Proposal) -> dict:
    """The ledger slice render() needs, with forum-derived strings (thread title, tags) sanitised."""
    rec = ((data or {}).get("threads") or {}).get(p.thread)
    if not rec:
        return {}
    return {"threads": {p.thread: {**rec, "title": sanitize(rec.get("title"), 80),
                                   "tags": [sanitize(t, 40) for t in rec.get("tags", [])]}}}


def build_preview(queue: Queue, p: Proposal, data: dict | None = None) -> tuple[str, bool]:
    """(message text, truncated?). Titles and tags are sanitised, mentions are defanged, and the message
    carries the proposal id plus a digest of the proposal so `poll` can tell it is the right message."""
    safe = dataclasses.replace(p, title=sanitize(p.title, 100), add=[sanitize(t, 40) for t in p.add],
                               remove=[sanitize(t, 40) for t in p.remove])
    body = defang_mentions(queue.render(safe, _safe_data(data, p))).replace("```", "'''")
    tail = ("\nReact %s to approve or %s to reject. Full text: `hearback queue show %s` ref %s"
            % (APPROVE, REJECT, p.id, _digest(queue, p)))
    head = "Proposal `%s` needs a decision\n```\n" % p.id
    room = MSG_LIMIT - len(head) - len("\n```") - len(tail) - 20
    truncated = len(body) > room
    if truncated:
        body = body[:room].rstrip() + "\n... (truncated)"
    return head + body + "\n```" + tail, truncated


def preview(queue: Queue, p: Proposal, data: dict | None = None) -> str:
    return build_preview(queue, p, data)[0]


def push(client: DiscordClient, queue: Queue, cfg: Config, *, sleep: Callable[[float], None] = time.sleep,
         data: dict | None = None, allow_truncated: bool = False) -> PushResult:
    """Post every pending proposal not yet shown in the staff channel.

    A preview that does not fit in one message is refused (a reviewer must not approve text they could
    not read) unless allow_truncated.
    """
    if not cfg.staff_channel_id:
        raise HearbackError("staff_channel_id is not set in hearback.toml [discord]")
    res = PushResult()
    for p in queue.list("pending"):
        if p.staff_message_id:
            continue
        text, truncated = build_preview(queue, p, data)
        if truncated and not allow_truncated:
            res.refused.append((p, len(queue.render(p, data))))
            continue
        msg = client.request("POST", "/channels/%s/messages" % cfg.staff_channel_id, body=msg_body(text, []))
        mid = str(msg["id"])
        for emoji in (APPROVE, REJECT):
            client.request("PUT", "/channels/%s/messages/%s/reactions/%s/@me"
                           % (cfg.staff_channel_id, mid, urllib.parse.quote(emoji)))
            sleep(0.3)
        p.staff_message_id = mid
        p.meta["staff_digest"] = _digest(queue, p)
        queue.update(p)
        res.pushed.append(p)
    return res


def _reactors(client: DiscordClient, channel: str, mid: str, emoji: str) -> list[str]:
    users = client.request("GET", "/channels/%s/messages/%s/reactions/%s"
                           % (channel, mid, urllib.parse.quote(emoji)), {"limit": 100})
    return [str(u["id"]) for u in users]


def _vet_message(client: DiscordClient, queue: Queue, cfg: Config, p: Proposal, bot_id: str) -> str | None:
    """Why the staff message cannot be trusted for this proposal, or None if it can."""
    try:
        msg = client.request("GET", "/channels/%s/messages/%s" % (cfg.staff_channel_id, p.staff_message_id))
    except DiscordError as e:
        if e.status == 404:
            return "staff message for %s was deleted; push again" % p.id
        raise
    if str((msg.get("author") or {}).get("id")) != bot_id:
        return "staff message for %s was not posted by this bot; ignoring its reactions" % p.id
    content = msg.get("content") or ""
    if p.id not in content:
        return "staff message for %s does not carry that proposal id; ignoring its reactions" % p.id
    if "ref " + _digest(queue, p) not in content:
        return "%s was changed after its staff message was posted; push again" % p.id
    return None


def poll(client: DiscordClient, queue: Queue, cfg: Config) -> PollResult:
    """Apply ✅/❌ reactions from approvers to pending proposals. ❌ wins if both are present."""
    if not cfg.staff_channel_id:
        raise HearbackError("staff_channel_id is not set in hearback.toml [discord]")
    if not cfg.approvers:
        raise HearbackError("no approvers configured: add user ids to [discord] approvers")
    allowed = set(cfg.approvers)
    bot_id = str(client.me()["id"])
    res = PollResult()
    for p in queue.list("pending"):
        if not p.staff_message_id:
            continue
        problem = _vet_message(client, queue, cfg, p, bot_id)
        if problem:
            p.staff_message_id = ""
            p.meta.pop("staff_digest", None)
            queue.update(p)
            res.stale.append(p.id)
            res.errors.append(problem)
            continue
        yes = _reactors(client, cfg.staff_channel_id, p.staff_message_id, APPROVE)
        no = _reactors(client, cfg.staff_channel_id, p.staff_message_id, REJECT)
        res.ignored += sum(1 for u in yes + no if u not in allowed and u != bot_id)
        yes_ok, no_ok = [u for u in yes if u in allowed], [u for u in no if u in allowed]
        if no_ok:
            queue.reject([p.id], "human:discord-" + no_ok[0], reason="rejected by reaction in the staff channel")
            res.rejected.append(p.id)
        elif yes_ok:
            queue.approve([p.id], "human:discord-" + yes_ok[0])
            res.approved.append(p.id)
    return res
