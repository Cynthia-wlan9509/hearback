"""Desktop GUI (Tkinter, stdlib): approve the queue and polish or delete the bot's own messages.

Tab 1 "Approval queue": list proposals, see exactly what will be posted, edit the text, approve/reject,
send the approved ones. Tab 2 "Bot messages": pick a channel or thread (or paste a link), list what the
bot posted there, edit (PATCH) or delete with a confirmation, and keep the ledger in step.

The logic lives in plain functions at the top so it can be tested without a display.
"""
from __future__ import annotations

import re
from typing import Any

from hearback import actions
from hearback.config import Config
from hearback.discord import DiscordClient, msg_body
from hearback.ledger import Ledger
from hearback.queue import STATES, Queue
from hearback.util import MSG_LIMIT, HearbackError, parse_iso, short, sid6

try:  # tkinter is optional on some Linux installs; only needed when the window opens
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # pragma: no cover
    tk = None  # type: ignore[assignment]

EDITABLE_KINDS = ("reply", "post", "newpost", "edit")


# ---------------------------------------------------------------- logic (no UI)

def parse_target(s: str | None) -> tuple[str | None, str | None]:
    """Channel/thread link or bare id -> (channel_id, message_id or None)."""
    ids = re.findall(r"\d{15,}", s or "")
    if "channels/" in (s or "") and len(ids) >= 2:
        return ids[1], (ids[2] if len(ids) >= 3 else None)
    return (ids[0], None) if ids else (None, None)


def local_time(iso_str: str | None) -> str:
    t = parse_iso(iso_str)
    return t.astimezone().strftime("%m-%d %H:%M") if t else (iso_str or "")


def places(cfg: Config, ledger: Ledger) -> list[tuple[str, str]]:
    """Where the bot may have posted: the staff/announce channels, then threads (guides first)."""
    out: list[tuple[str, str]] = []
    if cfg.announce_channel_id:
        out.append(("announcements channel", cfg.announce_channel_id))
    if cfg.staff_channel_id:
        out.append(("staff channel", cfg.staff_channel_id))
    threads = ledger.load().get("threads", {})
    guides = [(k, t) for k, t in threads.items() if (t.get("triage") or {}).get("status") == "guide"]
    rest = sorted((kt for kt in threads.items() if (kt[1].get("triage") or {}).get("status") != "guide"),
                  key=lambda kt: kt[1].get("created", ""), reverse=True)
    for k, t in guides + rest:
        out.append(("thread %s: %s" % (sid6(k), short(t.get("title", k), 40)), k))
    return out


def bot_id_of(client: DiscordClient, ledger: Ledger) -> str:
    return str(ledger.load().get("bot_user_id") or client.me()["id"])


def load_bot_messages(client: DiscordClient, ledger: Ledger, channel: str) -> list[dict]:
    return actions.list_bot_messages(client, channel, bot_id_of(client, ledger))


def save_message(client: DiscordClient, ledger: Ledger, channel: str, msg: dict, text: str, actor: str) -> dict:
    """Edit one bot message. Validates like the queue does; logs who changed it."""
    if len(text) > MSG_LIMIT:
        raise HearbackError("%d characters; Discord allows %d per message" % (len(text), MSG_LIMIT))
    if not text.strip() and not msg.get("attachments"):
        raise HearbackError("the text is empty; use delete if you do not want this message")
    new = client.request("PATCH", "/channels/%s/messages/%s" % (channel, msg["id"]), body=msg_body(text, []))
    ledger.sync_posted(channel, msg["id"], text)
    ledger.event(actor, "edit_message", channel, {"message": msg["id"]})
    return new


def remove_message(client: DiscordClient, ledger: Ledger, channel: str, msg: dict, actor: str) -> None:
    actions.delete_message(client, ledger, channel, msg["id"])
    ledger.event(actor, "delete_message", channel, {"message": msg["id"]})


# ---------------------------------------------------------------- window

class EditorApp:
    """The Tk window. Create with create_app(); call .root.mainloop() to run."""

    def __init__(self, cfg: Config, ledger: Ledger, queue: Queue, actor: str, client: DiscordClient, root: Any = None):
        if tk is None:
            raise HearbackError("tkinter is not available in this Python installation")
        self.cfg, self.ledger, self.queue, self.actor, self.client = cfg, ledger, queue, actor, client
        self.root = root or tk.Tk()
        self.root.title("Hearback desk")
        self.root.geometry("1100x700")
        self.items: list = []        # proposals in the list
        self.cur = None              # selected proposal
        self.msgs: list[dict] = []   # bot messages in tab 2
        self.msg: dict | None = None
        self.channel: str | None = None
        self.places = places(cfg, ledger)
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)
        self.tab_q, self.tab_m = ttk.Frame(nb, padding=8), ttk.Frame(nb, padding=8)
        nb.add(self.tab_q, text="Approval queue")
        nb.add(self.tab_m, text="Bot messages")
        self.status = ttk.Label(self.root, text="", padding=(8, 4), foreground="#555")
        self.status.pack(fill="x")
        self._build_queue_tab()
        self._build_messages_tab()
        self.refresh_queue()

    # ---- helpers
    def say(self, s: str) -> None:
        self.status.config(text=s)
        self.root.update_idletasks()

    def fail(self, e: Exception) -> None:
        messagebox.showerror("Hearback", str(e))
        self.say("error: " + short(str(e), 120))

    # ---- tab 1
    def _build_queue_tab(self) -> None:
        top = ttk.Frame(self.tab_q)
        top.pack(fill="x")
        ttk.Label(top, text="State:").pack(side="left")
        self.state_var = tk.StringVar(value="pending")
        box = ttk.Combobox(top, textvariable=self.state_var, state="readonly", width=10, values=list(STATES) + ["all"])
        box.pack(side="left", padx=4)
        box.bind("<<ComboboxSelected>>", lambda e: self.refresh_queue())
        ttk.Button(top, text="Refresh", command=self.refresh_queue).pack(side="left")
        ttk.Button(top, text="Send approved...", command=self.send_approved).pack(side="right")
        body = ttk.PanedWindow(self.tab_q, orient="horizontal")
        body.pack(fill="both", expand=True, pady=6)
        left = ttk.Frame(body)
        self.qlist = tk.Listbox(left, activestyle="none", exportselection=False)
        self.qlist.pack(fill="both", expand=True)
        self.qlist.bind("<<ListboxSelect>>", lambda e: self.pick_proposal())
        body.add(left, weight=2)
        right = ttk.Frame(body)
        ttk.Label(right, text="Exactly what will happen:").pack(anchor="w")
        self.preview = tk.Text(right, height=11, wrap="word", state="disabled", background="#f6f6f6")
        self.preview.pack(fill="x")
        ttk.Label(right, text="Text (edit before approving):").pack(anchor="w", pady=(8, 0))
        self.qtext = tk.Text(right, wrap="word", undo=True)
        self.qtext.pack(fill="both", expand=True)
        row = ttk.Frame(right)
        row.pack(fill="x", pady=6)
        ttk.Button(row, text="Approve", command=self.approve).pack(side="left")
        ttk.Button(row, text="Reject", command=self.reject).pack(side="left", padx=6)
        ttk.Button(row, text="Save text", command=self.save_text).pack(side="left")
        body.add(right, weight=3)

    def refresh_queue(self) -> None:
        st = self.state_var.get()
        self.items = self.queue.list(None if st == "all" else st)
        data = self.ledger.load()
        self.qlist.delete(0, "end")
        for p in self.items:
            self.qlist.insert("end", self.queue.summary_line(p, data))
        self.cur = None
        self._show_proposal(None)
        self.say("%d proposal(s) %s" % (len(self.items), st))

    def pick_proposal(self) -> None:
        sel = self.qlist.curselection()
        if sel:
            self._show_proposal(self.items[sel[0]])

    def _show_proposal(self, p) -> None:
        self.cur = p
        self.preview.config(state="normal")
        self.preview.delete("1.0", "end")
        self.qtext.delete("1.0", "end")
        if p is not None:
            self.preview.insert("1.0", self.queue.render(p, self.ledger.load()))
            if p.kind in EDITABLE_KINDS:
                self.qtext.insert("1.0", p.text)
        self.preview.config(state="disabled")
        self.qtext.config(state="normal" if p is not None and p.state == "pending" and p.kind in EDITABLE_KINDS
                          else "disabled")

    def approve(self) -> None:
        if self.cur is None:
            return
        try:
            self.queue.approve([self.cur.id], self.actor)
            self.refresh_queue()
            self.say("approved; use 'Send approved...' when you are ready")
        except HearbackError as e:
            self.fail(e)

    def reject(self) -> None:
        if self.cur is None:
            return
        try:
            self.queue.reject([self.cur.id], self.actor, "rejected in the GUI")
            self.refresh_queue()
        except HearbackError as e:
            self.fail(e)

    def save_text(self) -> None:
        if self.cur is None or self.cur.state != "pending" or self.cur.kind not in EDITABLE_KINDS:
            return
        try:
            p = self.queue.set_text(self.cur.id, self.qtext.get("1.0", "end-1c"), self.actor)
            self._show_proposal(p)
            self.say("text saved; the preview above is what will be posted")
        except HearbackError as e:
            self.fail(e)

    def send_approved(self) -> None:
        n = len(self.queue.list("approved"))
        if not n:
            self.say("nothing approved to send")
            return
        if not messagebox.askyesno("Send to Discord", "Send %d approved proposal(s) to Discord now?" % n):
            return
        try:
            self.say("sending...")
            rep = self.queue.send(self.client, self.ledger, forum_id=self.cfg.forum_id, delay=self.cfg.send_delay,
                                  actor=self.actor)
            messagebox.showinfo("Send result", "\n".join(rep.lines()))
            self.refresh_queue()
        except HearbackError as e:
            self.fail(e)

    # ---- tab 2
    def _build_messages_tab(self) -> None:
        top = ttk.Frame(self.tab_m)
        top.pack(fill="x")
        ttk.Label(top, text="Where:").pack(side="left")
        self.place_var = tk.StringVar()
        self.place_box = ttk.Combobox(top, textvariable=self.place_var, state="readonly", width=46,
                                      values=[p[0] for p in self.places])
        self.place_box.pack(side="left", padx=4)
        self.place_box.bind("<<ComboboxSelected>>", lambda e: self.load_messages(self.places[self.place_box.current()][1]))
        ttk.Label(top, text="  or paste a link:").pack(side="left")
        self.link_var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.link_var, width=34)
        ent.pack(side="left")
        ent.bind("<Return>", lambda e: self.open_link())
        ttk.Button(top, text="Open", command=self.open_link).pack(side="left", padx=4)
        ttk.Button(top, text="Refresh", command=lambda: self.channel and self.load_messages(self.channel)).pack(side="left")
        body = ttk.PanedWindow(self.tab_m, orient="horizontal")
        body.pack(fill="both", expand=True, pady=6)
        left = ttk.Frame(body)
        ttk.Label(left, text="Messages the bot posted here (newest first)").pack(anchor="w")
        self.mlist = tk.Listbox(left, activestyle="none", exportselection=False)
        self.mlist.pack(fill="both", expand=True)
        self.mlist.bind("<<ListboxSelect>>", lambda e: self.pick_message())
        body.add(left, weight=2)
        right = ttk.Frame(body)
        self.minfo = ttk.Label(right, text="Pick a message on the left")
        self.minfo.pack(anchor="w")
        self.mtext = tk.Text(right, wrap="word", undo=True)
        self.mtext.pack(fill="both", expand=True)
        self.mtext.bind("<<Modified>>", self._count)
        row = ttk.Frame(right)
        row.pack(fill="x", pady=6)
        self.mcount = ttk.Label(row, text="")
        self.mcount.pack(side="left")
        ttk.Button(row, text="Delete", command=self.delete_message).pack(side="right")
        ttk.Button(row, text="Revert", command=lambda: self._show_message(self.msg)).pack(side="right", padx=6)
        ttk.Button(row, text="Save", command=self.save_message).pack(side="right")
        body.add(right, weight=3)

    def _count(self, _e: Any = None) -> None:
        n = len(self.mtext.get("1.0", "end-1c"))
        self.mcount.config(text="%d / %d%s" % (n, MSG_LIMIT, "  (too long)" if n > MSG_LIMIT else ""))
        self.mtext.edit_modified(False)

    def open_link(self) -> None:
        cid, mid = parse_target(self.link_var.get())
        if not cid:
            messagebox.showinfo("Hearback", "Paste a Discord 'Copy link' URL or a channel/thread id.")
            return
        self.load_messages(cid, mid)

    def load_messages(self, channel: str, select: str | None = None) -> None:
        try:
            self.say("loading...")
            self.msgs = load_bot_messages(self.client, self.ledger, channel)
            self.channel = channel
            self.mlist.delete(0, "end")
            for m in self.msgs:
                first = ((m.get("content") or "(attachment only)").strip().splitlines() or [""])[0][:40]
                self.mlist.insert("end", "%s  %s" % (local_time(m.get("timestamp")), first))
            self._show_message(None)
            self.say("%d bot message(s) among the latest 100" % len(self.msgs))
            for i, m in enumerate(self.msgs):
                if select and m["id"] == select:
                    self.mlist.selection_set(i)
                    self.pick_message()
        except HearbackError as e:
            self.fail(e)

    def pick_message(self) -> None:
        sel = self.mlist.curselection()
        if sel:
            self._show_message(self.msgs[sel[0]])

    def _show_message(self, m: dict | None) -> None:
        self.msg = m
        self.mtext.delete("1.0", "end")
        if m is None:
            self.minfo.config(text="Pick a message on the left")
        else:
            att = [a.get("filename", "") for a in m.get("attachments", [])]
            self.minfo.config(text="posted %s%s" % (local_time(m.get("timestamp")),
                                                    "   attachments (not changed): " + ", ".join(att) if att else ""))
            self.mtext.insert("1.0", m.get("content") or "")
        self.mtext.edit_reset()
        self._count()

    def save_message(self) -> None:
        if self.msg is None or self.channel is None:
            return
        text = self.mtext.get("1.0", "end-1c")
        if text == (self.msg.get("content") or ""):
            self.say("no changes")
            return
        try:
            new = save_message(self.client, self.ledger, self.channel, self.msg, text, self.actor)
            i = self.msgs.index(self.msg)
            self.msgs[i] = new
            self.load_messages(self.channel, new["id"])
            self.say("saved; Discord now shows the new text")
        except HearbackError as e:
            self.fail(e)

    def delete_message(self) -> None:
        if self.msg is None or self.channel is None:
            return
        first = ((self.msg.get("content") or "").strip().splitlines() or [""])[0][:60]
        if not messagebox.askyesno("Delete message?", "Delete this message from Discord for good?\n\n" + first):
            return
        try:
            remove_message(self.client, self.ledger, self.channel, self.msg, self.actor)
            self.load_messages(self.channel)
            self.say("deleted")
        except HearbackError as e:
            self.fail(e)


def create_app(cfg: Config, ledger: Ledger, queue: Queue, actor: str, client: DiscordClient,
               root: Any = None) -> EditorApp:
    return EditorApp(cfg, ledger, queue, actor, client, root)


def run_editor(cfg: Config, ledger: Ledger, queue: Queue, actor: str, client: DiscordClient) -> int:
    create_app(cfg, ledger, queue, actor, client).root.mainloop()
    return 0
