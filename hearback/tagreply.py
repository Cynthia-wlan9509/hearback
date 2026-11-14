"""[tag_replies]: which (post, tag) pairs already got their automatic reply, so a tag change posts at most once.

One small JSON file in the home workspace's data dir: {thread id: {tag name: {"t": iso time, "by": "gateway" | "send",
"message_id": "..."}}}. The App writes it from the gateway thread and from web requests, hence the lock.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from hearback.util import atomic_write_json, now_iso, read_json

FILE = "tag_replies.json"


class TagReplyStore:
    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / FILE
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            d = read_json(self.path, {})
        except ValueError:          # damaged file: start again rather than stop the bot
            d = {}
        return d if isinstance(d, dict) else {}

    def has(self, thread: str, tag: str) -> bool:
        with self._lock:
            return tag in (self._load().get(str(thread)) or {})

    def claim(self, thread: str, tag: str, by: str, message_id: str = "") -> bool:
        """Record (thread, tag) unless it is already recorded. True when this call recorded it."""
        with self._lock:
            d = self._load()
            row = d.get(str(thread))
            if not isinstance(row, dict):
                row = d[str(thread)] = {}
            if tag in row:
                return False
            row[tag] = {"t": now_iso(), "by": by, "message_id": message_id}
            atomic_write_json(self.path, d)
            return True

    def set_message(self, thread: str, tag: str, message_id: str) -> None:
        with self._lock:
            d = self._load()
            entry: Any = (d.get(str(thread)) or {}).get(tag)
            if isinstance(entry, dict):
                entry["message_id"] = message_id
                atomic_write_json(self.path, d)

    def drop(self, thread: str, tag: str) -> None:
        """Forget a record (the post never went out)."""
        with self._lock:
            d = self._load()
            row = d.get(str(thread)) or {}
            if row.pop(tag, None) is not None:
                if not row:
                    d.pop(str(thread), None)
                atomic_write_json(self.path, d)
