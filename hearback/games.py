"""SWLink games: several games in one Discord server, one SWLink workspace (folder) per game, one bot.

Each workspace says which words name its game:

    [swlink]
    game_keys = ["二号", "月契", "猫之"]    # the first one or two are shown when the bot asks

When workspaces share a server, a DM with an Afdian order number or a device code must be routed to one game. The
order goes to the workspace that already claimed it, else the one named in the message, else the one named in the
order itself (the shop item's model, the plan title or the buyer's note); otherwise the bot asks and remembers the
order for a day, and a reply naming a game finishes it. A device code goes to the game named in the message, else
the only game whose role the member has; with several, the bot asks for the game to be named.

Two more pieces live here:

- `[dm_hook]`: an owner's program that decides some DMs (e-mail checks, device codes for members with given roles).
  SWLink sends it one JSON object on stdin and applies the JSON it prints: a reply, roles to give, owner notes.
- `RoleRestore`: `.hearback/restore_roles.json`, roles to give back to people when they join (for example after
  moving to a merged server). Each entry is used once.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from hearback.llm import register_secret, scrub
from hearback.util import HearbackError, atomic_write_json, ensure_data_dir, read_json_strict

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
DEVICE_RE = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}(?![0-9A-Za-z])")
PENDING_S = 86400
HOOK_TIMEOUT_S = 90
SECRETISH = re.compile(r"\S{40,}")          # long unbroken strings in a hook reply (licence codes): kept off the page


def parse_keys(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        raise HearbackError("[swlink] game_keys must be a list of words")
    return [x.strip() for x in raw]


def names(text: str, keys: Sequence[str]) -> bool:
    low = (text or "").lower()
    return any(k.lower() in low for k in keys if k)


def pick(spaces: Sequence[Any], text: str, keys_of: Callable[[Any], Sequence[str]]) -> Any:
    """The one workspace whose game the text names; None when none or several are named."""
    hits = [ws for ws in spaces if names(text, keys_of(ws))]
    return hits[0] if len(hits) == 1 else None


def label(keys: Sequence[str]) -> str:
    if not keys:
        return ""
    return "%s「%s」" % (keys[0], keys[1]) if len(keys) > 1 else keys[0]


def order_words(order: Mapping[str, Any] | None) -> str:
    """What an Afdian order says about itself: shop item models, the plan title and the buyer's note."""
    if not order:
        return ""
    skus = " ".join(str(s.get("name") or "") for s in order.get("sku_detail") or [] if isinstance(s, Mapping))
    return " ".join(x for x in (skus, str(order.get("plan_title") or ""), str(order.get("remark") or "")) if x)


class Pending:
    """Order numbers waiting for the person to say which game (in memory; a day at most)."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._d: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def put(self, uid: str, order: str) -> None:
        with self._lock:
            self._d[str(uid)] = (order, self.clock())

    def get(self, uid: str) -> str:
        with self._lock:
            hit = self._d.get(str(uid))
            if hit and self.clock() - hit[1] <= PENDING_S:
                return hit[0]
            self._d.pop(str(uid), None)
            return ""

    def drop(self, uid: str) -> None:
        with self._lock:
            self._d.pop(str(uid), None)


def parse_dm_hook(raw: Any) -> dict:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise HearbackError("[dm_hook] must be a table")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise HearbackError("[dm_hook] enabled must be true or false")
    cmd = raw.get("command", [])
    if not isinstance(cmd, list) or not all(isinstance(x, str) for x in cmd):
        raise HearbackError("[dm_hook] command must be a list of strings (no shell)")
    emails = raw.get("emails", True)
    if not isinstance(emails, bool):
        raise HearbackError("[dm_hook] emails must be true or false")
    roles = raw.get("device_role_ids", [])
    if not isinstance(roles, list) or not all(str(x).strip().isdigit() for x in roles):
        raise HearbackError("[dm_hook] device_role_ids must be a list of Discord role ids")
    try:
        tick = int(raw.get("tick_minutes", 0))
    except (TypeError, ValueError):
        raise HearbackError("[dm_hook] tick_minutes must be a whole number") from None
    if not 0 <= tick <= 1440:
        raise HearbackError("[dm_hook] tick_minutes must be between 0 and 1440")
    if enabled and not cmd:
        raise HearbackError("[dm_hook] needs command when enabled")
    return {"enabled": enabled, "command": cmd, "emails": emails, "device_role_ids": [str(x).strip() for x in roles],
            "tick_minutes": tick}


class DmHook:
    """Runs the owner's deciding program. Input: one JSON object on stdin. Output: the last line of stdout that is a
    JSON object: {"reply": str, "grant": [role ids], "notify": [str]} (all optional)."""

    def __init__(self, cfg: Mapping[str, Any], runner: Callable[..., Any] = subprocess.run):
        self.s = dict(cfg or {})
        self.runner = runner
        self.lock = threading.Lock()

    enabled = property(lambda self: bool(self.s.get("enabled")))
    emails = property(lambda self: self.enabled and bool(self.s.get("emails", True)))
    device_roles = property(lambda self: set(self.s.get("device_role_ids") or []) if self.enabled else set())
    tick_minutes = property(lambda self: int(self.s.get("tick_minutes") or 0) if self.enabled else 0)

    def call(self, payload: Mapping[str, Any]) -> dict:
        with self.lock:            # the program keeps its own files; one call at a time
            try:
                r = self.runner(list(self.s["command"]), input=json.dumps(payload, ensure_ascii=False), capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=HOOK_TIMEOUT_S, shell=False)
            except (OSError, subprocess.SubprocessError) as e:
                raise HearbackError(scrub("dm_hook: %s: %s" % (type(e).__name__, e))[:200]) from None
        if r.returncode != 0:
            raise HearbackError(scrub("dm_hook exit %s: %s" % (r.returncode, (r.stderr or r.stdout or "").strip()[-200:])))
        out = None
        for line in reversed((r.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    out = json.loads(line)
                    break
                except ValueError:
                    continue
        if not isinstance(out, dict):
            raise HearbackError("dm_hook printed no JSON object")
        reply = str(out.get("reply") or "")
        for s in SECRETISH.findall(reply):
            register_secret(s)
        grant = [str(x) for x in out.get("grant") or [] if str(x).isdigit()]
        notify = [str(x) for x in out.get("notify") or [] if str(x).strip()]
        return {"reply": reply[:4000], "grant": grant, "notify": notify}


class RoleRestore:
    """{"users": {user id: [role ids]}}: given back once when that person is in the server."""

    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.path = self.dir / "restore_roles.json"
        self._lock = threading.Lock()

    def _load(self) -> dict:
        d = read_json_strict(self.path, {}, "restore_roles.json") or {}
        users = d.get("users") if isinstance(d, dict) else None
        return {"users": {str(k): [str(r) for r in v] for k, v in (users or {}).items() if isinstance(v, list)}}

    def due(self, uid: str) -> list[str]:
        if not self.path.exists():
            return []
        with self._lock:
            return list(self._load()["users"].get(str(uid), []))

    def done(self, uid: str, given: Iterable[str]) -> None:
        given = {str(r) for r in given}
        with self._lock:
            d = self._load()
            left = [r for r in d["users"].get(str(uid), []) if r not in given]
            if left:
                d["users"][str(uid)] = left
            else:
                d["users"].pop(str(uid), None)
            ensure_data_dir(self.dir)
            atomic_write_json(self.path, d)

    def count(self) -> int:
        try:
            return len(self._load()["users"]) if self.path.exists() else 0
        except HearbackError:
            return 0
