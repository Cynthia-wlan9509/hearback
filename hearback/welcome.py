"""SWLink welcome: a DM the bot sends once to each person who joins the server (and, on connect, to members who have
not had it yet: they joined while SWLink was closed, or their DMs were closed last time).

Per workspace, in hearback.toml:

    [welcome]
    enabled = true
    file = "welcome.txt"        # the message, relative to this folder; read each time, so edits apply at once

Needs the Server Members intent (Developer Portal, Bot page): SWLink asks for it only when [welcome] is on. Who has
had it is kept in `.hearback/welcome.json`; bots and the server owner never get it.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from hearback.llm import scrub
from hearback.util import HearbackError, atomic_write_json, ensure_data_dir, read_json_strict

ACTOR = "bot:swlink"
TEXT_MAX = 2000


def parse_welcome(raw: Any) -> dict:
    """The [welcome] table, validated. Missing table = off."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise HearbackError("[welcome] must be a table")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise HearbackError("[welcome] enabled must be true or false")
    file = str(raw.get("file") or "").strip()
    if enabled and not file:
        raise HearbackError("[welcome] needs file when enabled")
    return {"enabled": enabled, "file": file}


class Welcome:
    def __init__(self, cfg: dict, root: Path, data_dir: Path, ledger: Any = None):
        self.enabled = bool(cfg.get("enabled"))
        self.file = (root / cfg["file"]) if cfg.get("file") else None
        self.path = data_dir / "welcome.json"
        self.data_dir = data_dir
        self.ledger = ledger
        self._lock = threading.Lock()

    def _load(self) -> dict:
        d = read_json_strict(self.path, {}, "welcome.json") or {}
        return {"greeted": [str(x) for x in d.get("greeted", [])]}

    def greeted(self) -> set[str]:
        with self._lock:
            return set(self._load()["greeted"])

    def mark(self, uids: list[str]) -> None:
        """Count these people as already greeted (for example: greeted by an earlier program)."""
        with self._lock:
            d = self._load()
            d["greeted"] = list(dict.fromkeys(d["greeted"] + [str(u) for u in uids]))
            ensure_data_dir(self.data_dir)
            atomic_write_json(self.path, d)

    def text(self) -> str:
        if self.file is None or not self.file.is_file():
            return ""
        return self.file.read_text(encoding="utf-8").strip()[:TEXT_MAX]

    def greet(self, send: Callable[[str, str], Any], uid: str, *, is_bot: bool = False, owner_id: str = "") -> bool:
        """DM `uid` the welcome once. `send(uid, text)` opens the DM and sends. True when sent now; a failure (DMs
        closed) is left for the next try."""
        uid = str(uid)
        if not self.enabled or is_bot or (owner_id and uid == str(owner_id)) or uid in self.greeted():
            return False
        text = self.text()
        if not text:
            return False
        try:
            send(uid, text)
        except HearbackError as e:
            if self.ledger is not None:
                self.ledger.event(ACTOR, "welcome_failed", None, {"user": uid, "error": scrub(e)[:200]})
            return False
        self.mark([uid])
        if self.ledger is not None:
            self.ledger.event(ACTOR, "welcome_sent", None, {"user": uid})
        return True
