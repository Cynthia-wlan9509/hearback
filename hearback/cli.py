"""Command line interface: `hearback <command>` / `python -m hearback <command>`.

Output is deliberately compact (it is read by LLM agents as well as humans). Anything that writes
to Discord goes through the approval queue: `propose` -> `queue approve` -> `send --yes`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from hearback import __version__, guide as guide_mod, release as release_mod, staff as staff_mod, templates
from hearback.config import Config, load_config, write_template
from hearback.discord import DiscordClient
from hearback.ledger import Ledger, filter_threads, triage_of
from hearback.queue import STATES, Queue
from hearback.render import render_dashboard, thread_line as render_thread_line
from hearback.sync import format_delta, sync as run_sync
from hearback.util import HearbackError, atomic_write_text, setup_stdio, short, sid6


def make_client(cfg: Config) -> DiscordClient:
    """The one place a real client is built (tests replace this with a FakeDiscord-backed one)."""
    return DiscordClient(cfg.token())


@dataclass
class Workspace:
    cfg: Config
    ledger: Ledger
    queue: Queue
    actor: str


def open_workspace(args: argparse.Namespace) -> Workspace:
    cfg = load_config(getattr(args, "config", None))
    ledger = Ledger(cfg.ledger_path)
    actor = getattr(args, "actor", None) or os.environ.get("HEARBACK_ACTOR") or "human:cli"
    return Workspace(cfg, ledger, Queue(cfg.queue_dir, ledger, protected=cfg.protected_paths()), actor)


def require_human(ws: Workspace, what: str) -> None:
    """Tripwire: approving, rejecting and sending are human decisions. Not a security wall (a shell agent can
    claim any actor name); the real boundary is keeping the bot token out of an agent's environment."""
    if ws.actor.startswith("agent:"):
        raise HearbackError(
            "`%s` is a human-only step and is refused for %s. Agents propose; a person reviews the queue and "
            "decides under their own identity (--as human:<name>). This check is a tripwire, not a wall: the bot "
            "token must not be available in an agent's environment (env vars, token file) in the first place."
            % (what, ws.actor))


# ---------------------------------------------------------------- helpers

def read_text(args: argparse.Namespace) -> str:
    if getattr(args, "file", None):
        return Path(args.file).read_text(encoding="utf-8-sig").rstrip("\r\n")
    return getattr(args, "text", None) or ""


def parse_kv(items: Sequence[str] | None) -> dict[str, str]:
    out = {}
    for it in items or []:
        if "=" not in it:
            raise HearbackError("expected key=value, got %r" % it)
        k, v = it.split("=", 1)
        out[k.strip()] = v
    return out


def thread_line(ws: Workspace, tid: str, rec: dict) -> str:
    return render_thread_line(tid, rec, ws.ledger.active_claim(rec))


# ---------------------------------------------------------------- commands

def cmd_init(args: argparse.Namespace) -> int:
    f = write_template(args.dir, args.force)
    print("wrote %s and %s/ (self-ignoring)" % (f, f.parent / ".hearback"))
    print("next: fill in forum_id, put the bot token in $DISCORD_BOT_TOKEN or .hearback/token.txt, "
          "then `hearback sync`")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    delta = run_sync(make_client(ws.cfg), ws.ledger, ws.cfg, download=not args.no_download, actor=ws.actor,
                     full=args.full)
    print(format_delta(delta, json_out=args.json))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    data = ws.ledger.load()
    rows = filter_threads(data, args.status, args.cluster, args.unreviewed, args.limit)
    if args.json:
        print(json.dumps([{"id": t, "title": r.get("title"), "author": r.get("author"), "tags": r.get("tags"),
                           "triage": triage_of(r), "claim": ws.ledger.active_claim(r)} for t, r in rows],
                         ensure_ascii=False, indent=1))
        return 0
    for tid, rec in rows:
        print(thread_line(ws, tid, rec))
    print("(%d of %d threads)" % (len(rows), len(data["threads"])))
    return 0


def _fmt(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def cmd_show(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    tid, rec = ws.ledger.get(args.thread)
    if args.json:
        print(json.dumps(rec | {"id": tid}, ensure_ascii=False, indent=1))
        return 0
    data = ws.ledger.load()
    print(rec.get("title", ""))
    guild = data.get("guild_id") or ws.cfg.guild_id
    print("thread %s%s" % (tid, "  https://discord.com/channels/%s/%s" % (guild, tid) if guild else ""))
    print("author %s  created %s  %s  messages %s" % (rec.get("author", ""), rec.get("created", ""),
                                                      "archived" if rec.get("archived") else "active",
                                                      rec.get("message_count", "")))
    print("tags: " + ", ".join(rec.get("tags", [])))
    claim = ws.ledger.active_claim(rec)
    if claim:
        print("claimed by %s until %s" % (claim["by"], claim["until"]))
    print("triage:")
    for k, v in triage_of(rec).items():
        if k != "replies_posted":
            print("  %s: %s" % (k, _fmt(v)))
    print("--- body ---")
    print(rec.get("body", ""))
    for p in rec.get("attachments", []):
        print("attachment: %s" % ws.ledger.abs_path(p))
    replies = rec.get("replies", [])
    print("--- replies (%d) ---" % len(replies))
    for r in replies:
        who = r.get("author", "") + (" (bot)" if r.get("bot") else "")
        print("[%s] %s: %s" % (r.get("time", ""), who, " ".join(r.get("content", "").split())))
        for p in r.get("attachments", []):
            print("    attachment: %s" % ws.ledger.abs_path(p))
    posted = triage_of(rec).get("replies_posted", [])
    if posted:
        print("--- sent by the bot (%d) ---" % len(posted))
        for r in posted:
            print("[%s] %s%s" % (r.get("time", ""), short(r.get("content", ""), 100),
                                 "  (%s)" % r["proposal"] if r.get("proposal") else ""))
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    c = ws.ledger.claim(args.thread, ws.actor, args.minutes, args.force)
    print("claimed %s for %s until %s" % (sid6(ws.ledger.resolve(args.thread)), c["by"], c["until"]))
    return 0


def cmd_unclaim(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    done = ws.ledger.release_claim(args.thread, ws.actor, args.force)
    print("claim released" if done else "no claim on that thread")
    return 0


def cmd_triage(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    patch: dict[str, Any] = {}
    for k, v in parse_kv([x for group in args.set for x in group]).items():
        patch[k] = None if v in ("", "null") else v
    if not patch:
        raise HearbackError("nothing to set: use --set key=value (e.g. --set status=investigating)")
    if patch.get("variants"):
        ws.cfg.validate_variants([x.strip() for x in patch["variants"].split(",") if x.strip()])
    tid = ws.ledger.resolve(args.thread)
    before = triage_of(ws.ledger.load()["threads"][tid]).get("status", "new")
    tri = ws.ledger.update_triage(tid, patch, actor=ws.actor, expected_rev=args.expect_rev,
                                  force_claim=args.force)
    print("triage %s updated: rev %d, status %s, cluster %s" % (sid6(tid), tri["rev"], tri.get("status"),
                                                               tri.get("cluster", "-")))
    if args.tag_proposal and tri.get("status") != before:
        rec = ws.ledger.load()["threads"][tid]
        p = release_mod.propose_status_tags(ws.queue, ws.cfg, rec, tid, before, tri["status"], ws.actor)
        print("queued tag change %s" % p.id if p else "no [status_tags] mapping for that change")
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    kind = args.kind
    kw: dict[str, Any] = dict(actor=ws.actor, why=args.why or "", force=args.force)
    text = read_text(args)
    if kind == "reply":
        tid = ws.ledger.resolve(args.thread)
        if args.template:
            rec = ws.ledger.load()["threads"][tid]
            text = templates.render(args.template, ws.cfg.language, ws.cfg.templates_dir,
                                    author=rec.get("author", ""), title=rec.get("title", ""),
                                    **{k: templates.safe(v) for k, v in parse_kv(args.var).items()})
        kw.update(thread=tid, text=text, attachments=args.attach)
    elif kind == "tags":
        kw.update(thread=args.thread, add=args.add, remove=args.remove)
    elif kind == "post":
        kw.update(thread=args.channel, text=text, attachments=args.attach)
    elif kind == "newpost":
        kw.update(thread=args.forum or ws.cfg.forum_id, title=args.title, text=text, add=args.tag,
                  attachments=args.attach, pin=args.pin)
    elif kind == "pin":
        kw.update(thread=args.thread, pin=not args.unpin)
    elif kind == "edit":
        kw.update(thread=args.thread, message_id=args.message_id, text=text)
    elif kind == "delete":
        kw.update(thread=args.thread, message_id=args.message_id)
    p = ws.queue.propose(kind, **kw)
    print(ws.queue.render(p))
    print("queued %s (pending). A human approves with: hearback queue approve %s" % (p.id, p.id))
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    q, act = ws.queue, args.queue_cmd
    if act == "list":
        data = ws.ledger.load()
        items = q.list(args.state)
        for p in items:
            print(q.summary_line(p, data))
        print("(%d proposals)" % len(items))
    elif act == "show":
        for ident in args.ids:
            print(q.render(q.get(ident)))
    elif act == "approve":
        require_human(ws, "queue approve")
        if not args.all and not args.ids:
            raise HearbackError("name the proposals to approve, or use --all --yes for every pending one")
        if args.all:
            pending = q.list("pending")
            if not pending:
                print("nothing pending")
                return 0
            data = ws.ledger.load()
            for p in pending:                       # approve-all means "I read all of these": show all of them
                print(q.render(p, data))
                print()
            if not args.yes:
                print("%d pending proposal(s) shown in full above; NOTHING was approved. Read them, then run "
                      "`hearback queue approve --all --yes` (or approve by id)." % len(pending))
                return 0
            ids = [p.id for p in pending]
        else:
            ids = args.ids
        for p in q.approve(ids, ws.actor):
            print("approved %s (%s)" % (p.id, p.kind))
    elif act == "reject":
        require_human(ws, "queue reject")
        for p in q.reject(args.ids, ws.actor, args.reason or ""):
            print("rejected %s (%s)" % (p.id, p.kind))
    elif act == "retry":
        require_human(ws, "queue retry")
        p = q.retry(args.id, ws.actor, args.force)
        print("%s is approved again; run `hearback send --yes`" % p.id)
    elif act == "edit":
        p = q.set_text(args.id, read_text(args), ws.actor)
        print(q.render(p))
    elif act == "verify":
        ps = [q.get(i) for i in args.ids] if args.ids else None
        bad = q.verify(make_client(ws.cfg), ps, ws.actor)
        print("verify: %s" % ("all match Discord" if not bad else "%d mismatch(es)" % len(bad)))
        for m in bad:
            print("  mismatch: " + m)
        return 1 if bad else 0
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    require_human(ws, "send")
    approved = ws.queue.list("approved")
    if args.only:
        wanted = {ws.queue.get(i).id for i in args.only}
        approved = [p for p in approved if p.id in wanted]
    if not args.yes:
        data = ws.ledger.load()
        for p in approved:
            print(ws.queue.render(p, data))
            print()
        print("dry run: %d approved proposal(s) would be sent. Nothing was sent; add --yes to send."
              % len(approved))
        return 0
    rep = ws.queue.send(make_client(ws.cfg), ws.ledger, forum_id=ws.cfg.forum_id, delay=ws.cfg.send_delay,
                        actor=ws.actor, ids=args.only or None)
    print("\n".join(rep.lines()))
    return 1 if rep.failed or rep.mismatches else 0


def cmd_release(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    if args.finalize:
        rep = release_mod.finalize(args.version, ws.ledger, ws.queue, ws.actor)
        print("\n".join(rep.lines()))
        return 0
    plan = release_mod.release(args.version, args.variant or [], ws.cfg, ws.ledger, ws.queue, ws.actor)
    print("\n".join(plan.lines()))
    if plan.proposals:
        print("review with `hearback queue list --state pending`, approve, `hearback send --yes`, "
              "then `hearback release %s --finalize`" % args.version)
    return 0


def cmd_staff(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    if args.staff_cmd == "poll":
        require_human(ws, "staff poll")
    client = make_client(ws.cfg)
    if args.staff_cmd == "push":
        res = staff_mod.push(client, ws.queue, ws.cfg, data=ws.ledger.load(), allow_truncated=args.allow_truncated)
        print("\n".join(res.lines()))
        return 1 if res.refused else 0
    print("\n".join(staff_mod.poll(client, ws.queue, ws.cfg).lines()))
    return 0


def cmd_guide(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    p = guide_mod.propose_guide(ws.queue, ws.cfg, ws.actor, steps=[s for group in args.steps or [] for s in group], tags=args.tag or [],
                                pin=not args.no_pin)
    print(ws.queue.render(p))
    print("queued %s (pending)" % p.id)
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    md = render_dashboard(ws.ledger.load(), ws.cfg)
    if args.stdout:
        print(md)
        return 0
    out = Path(args.out) if args.out else ws.cfg.data_dir / "DASHBOARD.md"
    atomic_write_text(out, md + "\n")
    print("wrote %s" % out)
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    ws = open_workspace(args)
    events = ws.ledger.read_events(args.limit, args.thread)
    if args.json:
        print(json.dumps(events, ensure_ascii=False, indent=1))
        return 0
    for e in events:
        d = e.get("detail")
        print("%s %-22s %-14s %-6s %s" % (e.get("t", ""), e.get("actor", ""), e.get("action", ""),
                                          sid6(e["thread"]) if e.get("thread") else "-",
                                          short(_fmt(d), 90) if d else ""))
    print("(%d events)" % len(events))
    return 0


class _NoClient:
    """Stands in for a client when no token is configured: any use of Discord raises the original error."""

    def __init__(self, err: HearbackError):
        self.err = err

    def request(self, *a: Any, **k: Any) -> Any:
        raise self.err

    def me(self) -> Any:
        raise self.err


def cmd_editor(args: argparse.Namespace) -> int:
    from hearback.editor import run_editor
    ws = open_workspace(args)
    try:
        client: Any = make_client(ws.cfg)
    except HearbackError as e:  # still useful to review the queue offline
        print("note: %s" % str(e).splitlines()[0], file=sys.stderr)
        client = _NoClient(e)
    return run_editor(ws.cfg, ws.ledger, ws.queue, ws.actor, client)


def cmd_demo(args: argparse.Namespace) -> int:
    from hearback.demo import run_demo
    return run_demo(args.lang, keep=args.keep)


def cmd_mcp(args: argparse.Namespace) -> int:
    from hearback import mcp_server  # the SDK itself is imported lazily inside, with an install hint
    return mcp_server.main(getattr(args, "config", None), getattr(args, "actor", None), args.fake_seed)


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help="path to hearback.toml (default: search upwards)")
    common.add_argument("--as", dest="actor", default=argparse.SUPPRESS, metavar="ACTOR",
                        help="who is acting, e.g. agent:codex-2 or human:owner (default: $HEARBACK_ACTOR or human:cli)")
    ap = argparse.ArgumentParser(prog="hearback", parents=[common],
                                 description="Every player who reports a bug hears back.")
    ap.add_argument("--version", action="version", version="hearback " + __version__)
    sub = ap.add_subparsers(dest="cmd", metavar="<command>")

    def add(name: str, fn: Any, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, parents=[common], help=help_, description=help_)
        sp.set_defaults(func=fn)
        return sp

    p = add("init", cmd_init, "write a commented hearback.toml")
    p.add_argument("--dir", default=".")
    p.add_argument("--force", action="store_true")

    p = add("sync", cmd_sync, "pull new/changed threads from Discord into the ledger")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-download", action="store_true", help="keep attachment URLs, do not download")
    p.add_argument("--full", action="store_true",
                   help="refetch every thread (catches edits/deletions, retries failed downloads)")

    p = add("list", cmd_list, "list threads from the ledger")
    p.add_argument("--status")
    p.add_argument("--cluster")
    p.add_argument("--unreviewed", action="store_true", help="no review yet, or a human replied after it")
    p.add_argument("--limit", type=int)
    p.add_argument("--json", action="store_true")

    p = add("show", cmd_show, "show one thread in full")
    p.add_argument("thread", help="thread id or its last digits")
    p.add_argument("--json", action="store_true")

    p = add("claim", cmd_claim, "take a short lease on a thread so other agents leave it alone")
    p.add_argument("thread")
    p.add_argument("--minutes", type=float, default=30)
    p.add_argument("--force", action="store_true")

    p = add("unclaim", cmd_unclaim, "drop your claim on a thread")
    p.add_argument("thread")
    p.add_argument("--force", action="store_true")

    p = add("triage", cmd_triage, "write triage fields (status, cluster, root_cause, fix, variants, ...)")
    p.add_argument("thread")
    p.add_argument("--set", action="append", nargs="+", default=[], metavar="KEY=VALUE",
                   help="empty value or 'null' removes the field; variants=free,full")
    p.add_argument("--expect-rev", type=int, help="fail if the triage revision is not this")
    p.add_argument("--tag-proposal", action="store_true", help="also queue the matching [status_tags] tag change")
    p.add_argument("--force", action="store_true", help="write even if another actor holds a live claim")

    p = add("propose", cmd_propose, "queue an outward action for human approval (never sends)")
    ps = p.add_subparsers(dest="kind", metavar="<kind>", required=True)

    def pk(kind: str, help_: str) -> argparse.ArgumentParser:
        sp = ps.add_parser(kind, parents=[common], help=help_)
        sp.add_argument("--why", help="rationale shown to the approver")
        sp.add_argument("--force", action="store_true", help="ignore someone else's claim / duplicate check")
        return sp

    def text_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--text")
        sp.add_argument("--file", help="read the text from a UTF-8 file")
        sp.add_argument("--attach", action="append", default=[], metavar="PATH")

    sp = pk("reply", "reply in a thread")
    sp.add_argument("thread")
    text_opts(sp)
    sp.add_argument("--template", choices=templates.names(), help="use a built-in/overridden template")
    sp.add_argument("--var", action="append", default=[], metavar="KEY=VALUE", help="template variable")
    sp = pk("tags", "add/remove forum tags")
    sp.add_argument("thread")
    sp.add_argument("--add", action="append", default=[], metavar="TAG")
    sp.add_argument("--remove", action="append", default=[], metavar="TAG")
    sp = pk("post", "send to any channel or thread")
    sp.add_argument("channel")
    text_opts(sp)
    sp = pk("newpost", "create a forum post")
    sp.add_argument("--title", required=True)
    sp.add_argument("--forum", help="forum channel id (default: the configured forum)")
    sp.add_argument("--tag", action="append", default=[])
    sp.add_argument("--pin", action="store_true")
    text_opts(sp)
    sp = pk("pin", "pin or unpin a thread")
    sp.add_argument("thread")
    sp.add_argument("--unpin", action="store_true")
    sp = pk("edit", "edit a message the bot sent")
    sp.add_argument("thread")
    sp.add_argument("message_id")
    text_opts(sp)
    sp = pk("delete", "delete a message the bot sent")
    sp.add_argument("thread")
    sp.add_argument("message_id")

    p = add("queue", cmd_queue, "inspect and decide on proposals")
    qs = p.add_subparsers(dest="queue_cmd", metavar="<action>", required=True)

    def qa(name: str, help_: str) -> argparse.ArgumentParser:
        return qs.add_parser(name, parents=[common], help=help_)

    sp = qa("list", "list proposals")
    sp.add_argument("--state", choices=STATES)
    sp = qa("show", "show proposals exactly as they will be sent")
    sp.add_argument("ids", nargs="+")
    sp = qa("approve", "approve proposals (a human decision)")
    sp.add_argument("ids", nargs="*")
    sp.add_argument("--all", action="store_true",
                    help="show every pending proposal in full; with --yes also approve them all")
    sp.add_argument("--yes", action="store_true", help="with --all: really approve after showing them")
    sp = qa("reject", "reject proposals")
    sp.add_argument("ids", nargs="+")
    sp.add_argument("--reason")
    sp = qa("retry", "move a failed proposal back to approved")
    sp.add_argument("id")
    sp.add_argument("--force", action="store_true", help="even if it may have been partly posted")
    sp = qa("edit", "change the text of a pending proposal")
    sp.add_argument("id")
    sp.add_argument("--text")
    sp.add_argument("--file")
    sp = qa("verify", "re-read sent proposals from Discord and report differences")
    sp.add_argument("ids", nargs="*")

    p = add("send", cmd_send, "send approved proposals (dry run unless --yes)")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--only", nargs="+", metavar="ID")

    p = add("release", cmd_release, "draft closure notifications for a shipped build")
    p.add_argument("version")
    p.add_argument("--variant", action="append", metavar="NAME")
    p.add_argument("--finalize", action="store_true", help="after sending: mark notified threads released")

    p = add("staff", cmd_staff, "approvals by reaction in the staff channel")
    p.add_argument("staff_cmd", choices=["push", "poll"])
    p.add_argument("--allow-truncated", action="store_true",
                   help="push previews even when they do not fit in one staff message")

    p = add("guide", cmd_guide, "queue the pinned 'how to report a bug' post")
    p.add_argument("--steps", action="append", nargs="+", metavar="IMG:X,Y,R:CAPTION",
                   help="annotated steps, one or more per flag (needs Pillow)")
    p.add_argument("--tag", action="append")
    p.add_argument("--no-pin", action="store_true")

    p = add("render", cmd_render, "write the markdown dashboard")
    p.add_argument("--out")
    p.add_argument("--stdout", action="store_true")

    p = add("log", cmd_log, "show the audit log (who did what)")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--thread")
    p.add_argument("--json", action="store_true")

    add("editor", cmd_editor, "desktop GUI: approval queue + edit/delete bot messages")

    p = add("demo", cmd_demo, "run the whole loop offline against a fake Discord")
    p.add_argument("--lang", choices=["en", "zh"], default="en")
    p.add_argument("--keep", action="store_true", help="keep the temporary workspace and print its path")

    p = add("mcp", cmd_mcp, "run the MCP server (stdio) for coding agents; actor from --as or $HEARBACK_ACTOR "
                            "(default agent:mcp)")
    p.add_argument("--fake-seed", action="store_true", help=argparse.SUPPRESS)  # tests/demos: in-memory Discord
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        setup_stdio()
        argv = sys.argv[1:]
    ap = build_parser()
    args = ap.parse_args(list(argv))
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except HearbackError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        if os.environ.get("HEARBACK_DEBUG"):
            traceback.print_exc()
        print("internal error: %s: %s (set HEARBACK_DEBUG=1 for a traceback)" % (type(e).__name__, e),
              file=sys.stderr)
        return 70


if __name__ == "__main__":
    sys.exit(main())
