"""The approval queue: agents propose, humans approve, only then does the bot send.

Each proposal is one JSON file in `.hearback/queue/<state>/<id>.json`, so many agents can write at
the same time without sharing a file. The directory a file sits in *is* its state:

    pending -> approved -> sending -> sent | failed          pending -> rejected

`send` is idempotent: it claims each proposal with an atomic move approved/ -> sending/ before executing it
(so a proposal rejected meanwhile is skipped), a proposal that exists in sent/ is never executed again, one
found in sending/ by the next send was interrupted and is parked in failed/ instead of being re-sent
blindly, and only one `send` runs at a time.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from hearback import actions
from hearback.actions import PartialSend
from hearback.discord import DiscordClient, DiscordError, load_attachments
from hearback.ledger import Ledger
from hearback.util import (FLAG_PINNED, MSG_LIMIT, TITLE_LIMIT, HearbackError, LockTimeout, atomic_write_json,
                           chunks_for, ensure_data_dir, file_lock, fmt_size, iso, now_iso, read_json, replace_retry,
                           short, sid6, utcnow)

STATES = ("pending", "approved", "sending", "rejected", "sent", "failed")   # sending: claimed by a running send
KINDS = ("reply", "tags", "post", "newpost", "pin", "edit", "delete")
THREAD_KINDS = ("reply", "tags", "pin", "edit", "delete")   # target must be a thread in the ledger
NO_DUP_STATES = {"reply": ("pending", "approved", "sent"), "post": ("pending", "approved", "sent"),
                 "newpost": ("pending", "approved", "sent")}


class QueueError(HearbackError):
    """Bad queue operation (unknown id, wrong state, duplicate...)."""


@dataclass
class Proposal:
    """One outward action plus the reason for it.

    `thread` is the target: a thread id (reply, tags, pin, edit, delete), a channel or thread id
    (post), or the forum id (newpost; empty = the configured forum). For newpost, `add` holds the tag names.
    """

    id: str
    kind: str
    actor: str
    created: str
    thread: str = ""
    text: str = ""
    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    why: str = ""
    state: str = "pending"
    result: dict = field(default_factory=dict)
    title: str = ""
    pin: bool = False
    message_id: str = ""
    approved_by: str = ""
    approved_at: str = ""
    rejected_by: str = ""
    rejected_at: str = ""
    reject_reason: str = ""
    sent_at: str = ""
    staff_message_id: str = ""
    error: str = ""
    meta: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        extra = d.pop("extra")
        keep = ("id", "kind", "actor", "created", "state", "thread", "why", "text")
        out = {k: v for k, v in d.items() if k in keep or v not in ("", [], {}, False)}
        out.update(extra)
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "Proposal":
        names = {f.name for f in dataclasses.fields(cls)} - {"extra"}
        p = cls(**{k: v for k, v in d.items() if k in names})
        p.extra = {k: v for k, v in d.items() if k not in names}
        return p


@dataclass
class SendReport:
    sent: list[Proposal] = field(default_factory=list)
    failed: list[Proposal] = field(default_factory=list)
    skipped: list[tuple[Proposal, str]] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = []
        for p in self.sent:
            ids = ",".join(sid6(i) for i in p.result.get("message_ids", []))
            out.append("sent    %s %s %s%s" % (p.id, p.kind, sid6(p.thread) if p.thread else "-",
                                               " msg " + ids if ids else ""))
        for p in self.failed:
            out.append("FAILED  %s %s: %s" % (p.id, p.kind, short(p.error, 160)))
        for p, why in self.skipped:
            out.append("skipped %s: %s" % (p.id, why))
        if self.sent:
            out.append("verify: " + ("all %d sent proposals match Discord" % len(self.sent)
                                     if not self.mismatches else "%d MISMATCH(ES)" % len(self.mismatches)))
        out += ["  mismatch: " + m for m in self.mismatches]
        if not (self.sent or self.failed or self.skipped):
            out.append("nothing approved to send")
        return out


class Queue:
    def __init__(self, root: str | Path, ledger: Ledger | None = None,
                 clock: Callable[[], dt.datetime] | None = None, protected: Sequence[str | Path] = ()):
        """`protected`: files or directories that may never be attached to a proposal. The queue directory
        and the ledger's directory are always protected; Config.protected_paths() adds the data directory,
        the token file and the config file."""
        self.root = Path(root)
        self.ledger = ledger
        self.clock = clock or utcnow
        self.protected = [Path(p) for p in protected if p]

    def _ensure(self) -> None:
        ensure_data_dir(self.root.parent)   # .hearback/ with its self-ignoring .gitignore
        self.root.mkdir(parents=True, exist_ok=True)

    def _check_attachments(self, paths: Sequence[str]) -> None:
        """Refuse to upload Hearback's own files (ledger, queue, downloaded attachments, token, config)."""
        shielded = [self.root, *self.protected] + ([self.ledger.dir] if self.ledger else [])
        for p in paths:
            # lexical (POSIX can't walk `missing/..`) and symlink-resolved forms both count
            for real in {Path(os.path.abspath(p)), Path(p).resolve()}:
                for guard in shielded:
                    g = guard.resolve()
                    if real == g or g in real.parents:
                        raise QueueError("attachment %s is inside Hearback's own files (%s): refusing to upload it"
                                         % (Path(p).name, g.name or str(g)))

    # ------------------------------------------------------------ storage
    def _path(self, state: str, pid: str) -> Path:
        return self.root / state / (pid + ".json")

    def _read(self, path: Path, state: str) -> Proposal | None:
        try:
            d = read_json(path, None)
        except ValueError:
            return None
        if not isinstance(d, dict) or "id" not in d:
            return None
        p = Proposal.from_dict(d)
        p.state = state  # the directory is the truth
        return p

    def _write(self, p: Proposal) -> None:
        atomic_write_json(self._path(p.state, p.id), p.to_dict())

    def _move(self, p: Proposal, new_state: str, **fields: Any) -> Proposal:
        src, dst = self._path(p.state, p.id), self._path(new_state, p.id)
        self._ensure()
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            replace_retry(src, dst)
        except FileNotFoundError:
            raise QueueError("proposal %s is no longer in %s (someone else moved it)" % (p.id, p.state))
        p.state = new_state
        for k, v in fields.items():
            setattr(p, k, v)
        self._write(p)
        return p

    def list(self, state: str | None = None) -> list[Proposal]:
        if state and state not in STATES:
            raise QueueError("unknown state %r; use one of: %s" % (state, ", ".join(STATES)))
        out = []
        for s in [state] if state else STATES:
            d = self.root / s
            if d.is_dir():
                for f in d.glob("p-*.json"):
                    p = self._read(f, s)
                    if p:
                        out.append(p)
        return sorted(out, key=lambda p: p.id)

    def get(self, ident: str) -> Proposal:
        """By full id or by suffix (`0007`, `7`, `20261024-0007`)."""
        ident = ident.strip()
        allp = self.list()
        hits = [p for p in allp if p.id == ident]
        if not hits:
            tail = ident.zfill(4) if ident.isdigit() and len(ident) < 4 else ident
            hits = [p for p in allp if p.id.endswith("-" + tail) or p.id.endswith(tail)]
        if not hits:
            raise QueueError("no proposal %r (see `hearback queue list`)" % ident)
        if len(hits) > 1:
            raise QueueError("%r matches several proposals: %s" % (ident, ", ".join(p.id for p in hits)))
        return hits[0]

    def _new_id(self) -> str:
        prefix = "p-%s-" % self.clock().strftime("%Y%m%d")
        top = 0
        for s in STATES:
            d = self.root / s
            if d.is_dir():
                for f in d.glob(prefix + "*.json"):
                    tail = f.stem[len(prefix):]
                    if tail.isdigit():
                        top = max(top, int(tail))
        n = top + 1
        while True:
            pid = "%s%04d" % (prefix, n)
            path = self._path("pending", pid)
            self._ensure()
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))  # reserve the id
                return pid
            except FileExistsError:
                n += 1

    def _log(self, actor: str, action: str, thread: str | None, detail: Any = None) -> None:
        if self.ledger:
            self.ledger.event(actor, action, thread or None, detail)

    # ------------------------------------------------------------ propose
    def propose(self, kind: str, *, actor: str, why: str = "", thread: str = "", text: str = "",
                add: Sequence[str] = (), remove: Sequence[str] = (), attachments: Sequence[str] = (),
                title: str = "", pin: bool = False, message_id: str = "", meta: dict | None = None,
                force: bool = False, trusted_attachments: bool = False) -> Proposal:
        """Validate and queue one action as `pending`. Nothing is sent.

        Attachments inside Hearback's own files (data directory, token file, config file) are refused
        unless `trusted_attachments` (only for files Hearback generated itself, e.g. the guide image).
        """
        if kind not in KINDS:
            raise QueueError("unknown kind %r; use one of: %s" % (kind, ", ".join(KINDS)))
        thread = str(thread or "").strip()
        if kind != "newpost" and not thread:
            raise QueueError("%s needs a target thread/channel" % kind)
        if self.ledger and thread and kind != "newpost":
            thread = self.ledger.resolve(thread, must_exist=kind in ("reply", "tags", "pin"))
            self.ledger.check_claim(thread, actor, force)
        if not trusted_attachments:
            self._check_attachments(attachments)   # before touching the files at all
        atts = [str(Path(a["path"]).resolve()) for a in load_attachments(attachments)]
        if not trusted_attachments:
            self._check_attachments(atts)
        text = text.strip("\r\n") if text else ""
        add, remove = list(dict.fromkeys(add)), list(dict.fromkeys(remove))
        if kind in ("reply", "post", "newpost") and not text.strip() and not atts:
            raise QueueError("%s needs text or an attachment" % kind)
        if kind == "tags" and not add and not remove:
            raise QueueError("tags needs at least one tag to add or remove")
        if kind == "newpost":
            title = title.strip()
            if not title:
                raise QueueError("newpost needs a title")
            if len(title) > TITLE_LIMIT:
                raise QueueError("title is %d characters; Discord allows %d" % (len(title), TITLE_LIMIT))
            if len(add) > 5:
                raise QueueError("%d tags; Discord allows 5 per post" % len(add))
        if kind in ("edit", "delete") and not message_id:
            raise QueueError("%s needs a message id" % kind)
        if kind == "edit":
            if not text.strip():
                raise QueueError("edit needs the new text")
            if len(text) > MSG_LIMIT:
                raise QueueError("an edited message holds at most %d characters (got %d)" % (MSG_LIMIT, len(text)))
        if not force and kind in NO_DUP_STATES:
            for q in self.list():
                if (q.state in NO_DUP_STATES[kind] and q.kind == kind and q.thread == thread and q.text == text
                        and q.add == add and q.title == title):
                    raise QueueError("identical proposal already exists: %s (%s). Pass force to queue it anyway."
                                     % (q.id, q.state))
        pid = self._new_id()
        p = Proposal(id=pid, kind=kind, actor=actor, created=iso(self.clock()), thread=thread, text=text,
                     add=add, remove=remove, attachments=atts, why=why, title=title, pin=bool(pin),
                     message_id=str(message_id or ""), meta=dict(meta or {}))
        self._write(p)
        self._log(actor, "propose", thread, {"id": pid, "kind": kind})
        return p

    # ------------------------------------------------------------ approve / reject
    def approve(self, ids: Sequence[str], actor: str) -> list[Proposal]:
        out = []
        for ident in ids:
            p = self.get(ident)
            if p.state == "approved":
                out.append(p)
                continue
            if p.state != "pending":
                raise QueueError("%s is %s; only pending proposals can be approved" % (p.id, p.state))
            self._move(p, "approved", approved_by=actor, approved_at=now_iso())
            self._log(actor, "approve", p.thread, {"id": p.id})
            out.append(p)
        return out

    def reject(self, ids: Sequence[str], actor: str, reason: str = "") -> list[Proposal]:
        out = []
        for ident in ids:
            p = self.get(ident)
            if p.state == "rejected":
                out.append(p)
                continue
            if p.state not in ("pending", "approved"):
                raise QueueError("%s is %s; only pending or approved proposals can be rejected" % (p.id, p.state))
            self._move(p, "rejected", rejected_by=actor, rejected_at=now_iso(), reject_reason=reason)
            self._log(actor, "reject", p.thread, {"id": p.id, "reason": reason})
            out.append(p)
        return out

    def set_text(self, ident: str, text: str, actor: str) -> Proposal:
        """A human polishes the wording before approving (pending proposals only)."""
        p = self.get(ident)
        if p.state != "pending":
            raise QueueError("%s is %s; only pending proposals can be edited" % (p.id, p.state))
        if p.kind == "edit" and len(text) > MSG_LIMIT:
            raise QueueError("an edited message holds at most %d characters" % MSG_LIMIT)
        if p.kind not in ("reply", "post", "newpost", "edit"):
            raise QueueError("%s proposals have no text to edit" % p.kind)
        if not text.strip() and not p.attachments:
            raise QueueError("text cannot be empty")
        p.text = text.strip("\r\n")
        p.meta["edited_by"] = actor
        self._write(p)
        self._log(actor, "edit_proposal", p.thread, {"id": p.id})
        return p

    def update(self, p: Proposal) -> None:
        """Rewrite a proposal in place (state unchanged), e.g. after recording a staff message id."""
        self._write(p)

    def retry(self, ident: str, actor: str, force: bool = False) -> Proposal:
        """Move a failed proposal back to approved. A possibly half-sent one needs force."""
        p = self.get(ident)
        if p.state != "failed":
            raise QueueError("%s is %s; only failed proposals can be retried" % (p.id, p.state))
        if (p.result.get("message_ids") or p.result.get("started") or p.result.get("thread_id")) and not force:
            raise QueueError("%s may have been partly posted (%s). Check Discord first, then retry with force."
                             % (p.id, p.error or "interrupted"))
        self._move(p, "approved", result={}, error="")
        self._log(actor, "retry", p.thread, {"id": p.id})
        return p

    # ------------------------------------------------------------ preview
    def render(self, p: Proposal, data: dict | None = None) -> str:
        """The proposal exactly as a human will see it, and as it will be sent."""
        if data is None and self.ledger:
            data = self.ledger.load()
        threads = (data or {}).get("threads", {})
        rec = threads.get(p.thread) or {}
        where = "%s %s" % (sid6(p.thread) if p.thread else "-",
                           "\"%s\" [%s]" % (rec.get("title", ""), ",".join(rec.get("tags", []))) if rec else "")
        lines = ["%s  %s  [%s]  by %s  %s" % (p.id, p.kind, p.state, p.actor, p.created)]
        if p.kind == "newpost":
            lines.append("  new forum post \"%s\"  tags: %s  pin: %s" % (p.title, ", ".join(p.add) or "(none)",
                                                                      "yes" if p.pin else "no"))
        elif p.kind == "post":
            lines.append("  post to %s" % where.strip())
        elif p.kind == "tags":
            cur = rec.get("tags", [])
            new = [t for t in cur if t not in p.remove] + [t for t in p.add if t not in cur]
            lines.append("  thread %s" % where.strip())
            lines.append("  tags: %s -> %s   (add: %s; remove: %s)" % (
                ",".join(cur) or "(none)", ",".join(new) or "(none)", ",".join(p.add) or "-",
                ",".join(p.remove) or "-"))
        elif p.kind == "pin":
            lines.append("  %s thread %s" % ("pin" if p.pin else "unpin", where.strip()))
        elif p.kind in ("edit", "delete"):
            old = next((r for r in (rec.get("triage") or {}).get("replies_posted", [])
                        if r.get("message_id") == p.message_id), None)
            lines.append("  %s message %s in thread %s" % (p.kind, sid6(p.message_id), where.strip()))
            if old:
                lines.append("  current: " + short(old.get("content", ""), 160))
        else:
            lines.append("  reply in thread %s" % where.strip())
        if p.why:
            lines.append("  why: " + p.why)
        if p.kind in ("reply", "post", "newpost", "edit"):
            chunks = chunks_for(p.text) if p.kind != "edit" else [p.text]
            for i, c in enumerate(chunks, 1):
                lines.append("  message %d/%d (%d chars)%s:" % (i, len(chunks), len(c),
                                                              "  + attachments" if p.attachments and i == len(chunks)
                                                              else ""))
                lines += ["  | " + ln for ln in c.splitlines()] or ["  |"]
            for a in p.attachments:
                try:
                    lines.append("  attach: %s (%s)" % (os.path.basename(a), fmt_size(os.path.getsize(a))))
                except OSError:
                    lines.append("  attach: %s (MISSING)" % os.path.basename(a))
        if p.state == "approved":
            lines.append("  approved by %s at %s" % (p.approved_by, p.approved_at))
        elif p.state == "rejected":
            lines.append("  rejected by %s%s" % (p.rejected_by, ": " + p.reject_reason if p.reject_reason else ""))
        elif p.state == "failed":
            lines.append("  error: " + p.error)
        elif p.state == "sent":
            lines.append("  sent at %s%s" % (p.sent_at, "  verified" if p.result.get("verified") else ""))
        return "\n".join(lines)

    def summary_line(self, p: Proposal, data: dict | None = None) -> str:
        threads = (data or {}).get("threads", {})
        title = (threads.get(p.thread) or {}).get("title", p.title)
        body = p.text or ("+" + ",".join(p.add) if p.add else "") or ("-" + ",".join(p.remove) if p.remove else "")
        return "%s %-8s %-8s %s %s" % (p.id, p.state, p.kind, sid6(p.thread) if p.thread else "-",
                                       short("%s | %s" % (title, body), 70))

    # ------------------------------------------------------------ send
    def send(self, client: DiscordClient, ledger: Ledger | None = None, *, forum_id: str = "",
             delay: float = 1.0, sleep: Callable[[float], None] = time.sleep, actor: str = "human:cli",
             ids: Sequence[str] | None = None, verify: bool = True) -> SendReport:
        """Execute approved proposals in id order, then verify them against Discord."""
        ledger = ledger or self.ledger
        if ledger is None:
            raise QueueError("send needs a ledger")
        report = SendReport()
        wanted = {self.get(i).id for i in ids} if ids else None
        try:
            with file_lock(self.root / "send.lock", stale=600, timeout=0):
                self._send_locked(client, ledger, report, wanted, forum_id, delay, sleep, actor)
        except LockTimeout:
            raise QueueError("another `hearback send` is running (queue/send.lock)")
        if verify and report.sent:
            report.mismatches = self.verify(client, report.sent, actor=actor)
        return report

    def _send_locked(self, client: DiscordClient, ledger: Ledger, report: SendReport, wanted: set[str] | None,
                     forum_id: str, delay: float, sleep: Callable[[float], None], actor: str) -> None:
        # We hold send.lock, so nothing in sending/ is being worked on: a previous send died while running it.
        # Whether it reached Discord is unknown, so it is parked, never re-sent blindly.
        for p in self.list("sending"):
            if self._path("sent", p.id).exists():
                self._path("sending", p.id).unlink(missing_ok=True)
                report.skipped.append((p, "already sent"))
                continue
            self._move(p, "failed", error="a previous send was interrupted while running this; check "
                                          "Discord, then `hearback queue retry %s --force`" % p.id)
            report.failed.append(p)
        first = True
        for p in self.list("approved"):          # a snapshot: proposals can be rejected while we work
            if wanted is not None and p.id not in wanted:
                continue
            if self._path("sent", p.id).exists():  # crash between steps: never execute twice
                self._path("approved", p.id).unlink(missing_ok=True)
                report.skipped.append((p, "already sent"))
                continue
            claimed = self._claim_for_send(p)
            if claimed is None:
                report.skipped.append((p, "no longer approved (rejected or moved while sending)"))
                continue
            p = claimed
            if not first:
                sleep(delay)
            first = False
            p.result = {"started": now_iso()}
            self._write(p)
            try:
                result = actions.execute(p, client, ledger, forum_id, pause=lambda: sleep(delay))
            except PartialSend as e:
                self._fail(p, e, e.result, actor, report)
            except HearbackError as e:
                self._fail(p, e, {}, actor, report)
            except Exception as e:  # a bug must not leave the proposal looking sendable
                self._fail(p, HearbackError("internal error: %s: %s" % (type(e).__name__, e)), {}, actor, report)
            else:
                self._move(p, "sent", result=result, sent_at=now_iso(), error="")
                self._log(actor, "send", p.thread, {"id": p.id, "kind": p.kind,
                                                    "messages": result.get("message_ids", [])})
                report.sent.append(p)

    def _claim_for_send(self, p: Proposal) -> Proposal | None:
        """Atomically take an approved proposal (approved/ -> sending/). None if it is no longer approved,
        e.g. a human rejected it after the send took its snapshot: then it must not be executed."""
        dst = self._path("sending", p.id)
        self._ensure()
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            replace_retry(self._path("approved", p.id), dst)
        except FileNotFoundError:
            return None
        fresh = self._read(dst, "sending") or p
        fresh.state = "sending"
        return fresh

    def _fail(self, p: Proposal, e: Exception, result: dict, actor: str, report: SendReport) -> None:
        self._move(p, "failed", error=str(e), result=dict(result))
        self._log(actor, "send_failed", p.thread, {"id": p.id, "error": short(str(e), 200)})
        report.failed.append(p)

    # ------------------------------------------------------------ verify
    def verify(self, client: DiscordClient, proposals: Sequence[Proposal] | None = None,
               actor: str = "human:cli") -> list[str]:
        """Re-read what was sent and report every difference. Marks result.verified on each proposal."""
        if proposals is None:
            proposals = [p for p in self.list("sent") if "verified" not in p.result]
        mismatches: list[str] = []
        for p in proposals:
            problems = self._verify_one(client, p)
            p.result["verified"] = not problems
            if problems:
                p.result["mismatches"] = problems
            else:
                p.result.pop("mismatches", None)
            if p.state == "sent":
                self._write(p)
            mismatches += ["%s %s %s: %s" % (p.id, p.kind, sid6(p.thread) if p.thread else "-", m) for m in problems]
        if mismatches:
            self._log(actor, "verify_mismatch", None, {"count": len(mismatches)})
        return mismatches

    @staticmethod
    def _message(client: DiscordClient, channel: str, mid: str) -> dict | None:
        try:
            return client.request("GET", "/channels/%s/messages/%s" % (channel, mid))
        except DiscordError as e:
            if e.status == 404:
                return None
            raise

    def _verify_one(self, client: DiscordClient, p: Proposal) -> list[str]:
        r, bad = p.result, []
        try:
            if p.kind in ("reply", "post"):
                chunks, mids = chunks_for(p.text), r.get("message_ids", [])
                if len(mids) != len(chunks):
                    bad.append("expected %d message(s), recorded %d" % (len(chunks), len(mids)))
                for i, (mid, want) in enumerate(zip(mids, chunks)):
                    m = self._message(client, p.thread, mid)
                    if m is None:
                        bad.append("message %s is missing on Discord" % sid6(mid))
                    elif (m.get("content") or "") != want:
                        bad.append("message %s content differs from what was approved" % sid6(mid))
                    elif i == len(mids) - 1 and len(m.get("attachments") or []) != len(p.attachments):
                        bad.append("message %s has %d attachment(s), expected %d"
                                   % (sid6(mid), len(m.get("attachments") or []), len(p.attachments)))
            elif p.kind == "tags":
                live = client.request("GET", "/channels/%s" % p.thread)
                if {str(i) for i in live.get("applied_tags") or []} != set(r.get("tag_ids", [])):
                    bad.append("tags on Discord differ from what was applied")
            elif p.kind == "newpost":
                live = client.request("GET", "/channels/%s" % r.get("thread_id"))
                if live.get("name") != p.title:
                    bad.append("title differs")
                if {str(i) for i in live.get("applied_tags") or []} != set(r.get("tag_ids", [])):
                    bad.append("tags differ")
                if p.pin and not int(live.get("flags") or 0) & FLAG_PINNED:
                    bad.append("post is not pinned")
                first = self._message(client, r["thread_id"], r["thread_id"])
                if first is None or (first.get("content") or "") != chunks_for(p.text)[0]:
                    bad.append("first message differs or is missing")
            elif p.kind == "pin":
                live = client.request("GET", "/channels/%s" % p.thread)
                if bool(int(live.get("flags") or 0) & FLAG_PINNED) != p.pin:
                    bad.append("pinned state on Discord is %s" % ("pinned" if not p.pin else "not pinned"))
            elif p.kind == "edit":
                m = self._message(client, p.thread, p.message_id)
                if m is None or (m.get("content") or "") != p.text:
                    bad.append("message text on Discord differs from the edit")
            elif p.kind == "delete":
                if self._message(client, p.thread, p.message_id) is not None:
                    bad.append("message still exists on Discord")
        except HearbackError as e:
            bad.append("could not verify: %s" % short(str(e), 120))
        return bad
