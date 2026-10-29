"""SWLink codes: a member with a given role DMs the bot a device code and gets back what the owner's issuing command
prints (an activation or license code bound to that device). The bot sends the reply, then the code alone in its own
message (a long-press copy on a phone takes the whole message).

Per workspace, in hearback.toml:

    [codes]
    enabled = true
    role_id = "123..."          # who may ask: members with this role (for example the one the supporter flow gives)
    command = ["node", "issue.js", "--device", "{device}", "--note", "{note}"]
    pattern = "..."             # optional regex for the device code (default: four groups of 4 hex digits with dashes)
    per_user = 1                # devices each member may get a code for; more goes to the owner

The command runs without a shell. `{device}` is the matched code in upper case (it matched `pattern`, so it carries no
shell or path syntax), `{note}` names who asked (`discord:<id> <name>`, commas and line breaks removed). The code is
the last non-empty line of stdout. Codes are kept in `.hearback/codes.json` so the same device gets the same code
again; they never go into events, logs or owner notices, and one device code is never issued to two members.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from hearback import templates
from hearback.llm import register_secret, scrub
from hearback.util import HearbackError, atomic_write_json, ensure_data_dir, read_json_strict

DEFAULT_PATTERN = r"(?<![0-9A-Za-z])[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}(?![0-9A-Za-z])"
TIMEOUT_S = 60
CODE_MAX = 2000
ACTOR = "bot:swlink"


class CodesError(HearbackError):
    """Issuing failed. The message is already scrubbed."""


def parse_codes(raw: Any) -> dict:
    """The [codes] table, validated. Missing table = off."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise HearbackError("[codes] must be a table")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise HearbackError("[codes] enabled must be true or false")
    role = str(raw.get("role_id") or "").strip()
    if role and not role.isdigit():
        raise HearbackError("[codes] role_id must be a Discord role id")
    cmd = raw.get("command", [])
    if not isinstance(cmd, list) or not all(isinstance(x, str) for x in cmd):
        raise HearbackError("[codes] command must be a list of strings (no shell)")
    pattern = str(raw.get("pattern") or DEFAULT_PATTERN)
    try:
        re.compile(pattern)
    except re.error as e:
        raise HearbackError("[codes] pattern is not a valid regex: %s" % e) from None
    try:
        per_user = int(raw.get("per_user", 1))
    except (TypeError, ValueError):
        raise HearbackError("[codes] per_user must be a whole number") from None
    if not 1 <= per_user <= 20:
        raise HearbackError("[codes] per_user must be between 1 and 20")
    if enabled and (not role or not cmd):
        raise HearbackError("[codes] needs role_id and command when enabled")
    return {"enabled": enabled, "role_id": role, "command": cmd, "pattern": pattern, "per_user": per_user}


def mask(device: str) -> str:
    """First and last group of a device code (what events and notices show)."""
    s = str(device)
    return s[:4] + "…" + s[-4:] if len(s) > 8 else "…"


def clean_note(text: str) -> str:
    return re.sub(r"[,\r\n\t]+", " ", str(text or "")).strip()[:100]


class Codes:
    """The store, the decision and the issuing command for one workspace."""

    def __init__(self, settings: Mapping[str, Any], data_dir: str | Path, *, lang: str = "en",
                 templates_dir: Path | None = None, ledger: Any = None, clock: Callable[[], float] = time.time,
                 runner: Callable[..., Any] = subprocess.run):
        self.s = dict(settings)
        self.path = Path(data_dir) / "codes.json"
        self.lang, self.templates_dir, self.ledger = lang, templates_dir, ledger
        self.clock, self.runner = clock, runner
        self.lock = threading.Lock()          # one issue at a time: issuing commands often bump a serial number
        self._rx = re.compile(self.s.get("pattern") or DEFAULT_PATTERN)

    @property
    def enabled(self) -> bool:
        return bool(self.s.get("enabled"))

    @property
    def role_id(self) -> str:
        return str(self.s.get("role_id") or "")

    def find(self, text: str) -> str:
        """The first device code in a message, upper case, or ''."""
        m = self._rx.search(text or "")
        return m.group(0).upper() if m else ""

    def text(self, name: str, **values: Any) -> str:
        return templates.render("codes_" + name, self.lang, self.templates_dir, **values)

    def event(self, action: str, detail: Any = None) -> None:
        if self.ledger is not None:
            self.ledger.event(ACTOR, action, None, detail)

    # ---- store
    def load(self) -> dict:
        raw = read_json_strict(self.path, None, "the codes file")
        if raw is not None and not isinstance(raw, dict):
            raise CodesError("the codes file is damaged (not an object): fix or delete %s" % self.path.name)
        raw = raw or {}
        issued = raw.get("issued") if isinstance(raw.get("issued"), dict) else {}
        return {"issued": issued}

    def _save(self, d: dict) -> None:
        ensure_data_dir(self.path.parent)
        atomic_write_json(self.path, d)

    def count(self) -> int:
        try:
            return sum(len(v) for v in self.load()["issued"].values())
        except HearbackError:
            return 0

    # ---- issuing
    def _run(self, device: str, note: str) -> str:
        args = [a.replace("{device}", device).replace("{note}", note) for a in self.s["command"]]
        try:
            r = self.runner(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=TIMEOUT_S, shell=False)
        except (OSError, subprocess.SubprocessError) as e:
            raise CodesError(scrub("%s: %s" % (type(e).__name__, e))[:200]) from None
        if r.returncode != 0:
            raise CodesError(scrub((r.stderr or r.stdout or "exit %s" % r.returncode).strip())[-200:])
        lines = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
        code = lines[-1] if lines else ""
        if not code or len(code) > CODE_MAX or any(c.isspace() for c in code):
            raise CodesError("the command printed no usable code")
        return code

    def handle(self, person: Any, device: str) -> tuple[str, str, list[str]]:
        """(reply, code, owner notes) for `person` asking for `device`. `code` is '' when none is given.
        The caller sends `code` as a message of its own after the reply; notes never carry it."""
        uid, dev = str(person.id), device.upper()
        who = "%s (id %s)" % (person.display or person.name or "?", uid)
        if self.role_id not in {str(r) for r in person.role_ids}:
            return self.text("need_role"), "", []
        with self.lock:
            d = self.load()
            mine = d["issued"].get(uid, [])
            hit = next((x for x in mine if x.get("device") == dev), None)
            if hit:
                register_secret(hit["code"])
                return self.text("again", device=dev, code=templates.trusted(hit["code"])), hit["code"], []
            if any(x.get("device") == dev for u, xs in d["issued"].items() if u != uid for x in xs):
                self.event("code_refused", {"user": uid, "device": mask(dev), "why": "taken"})
                return self.text("taken"), "", [self.text("note_taken", who=who, device=mask(dev))]
            if len(mine) >= int(self.s.get("per_user") or 1):
                self.event("code_refused", {"user": uid, "device": mask(dev), "why": "limit"})
                return self.text("limit"), "", [self.text("note_limit", who=who, device=dev)]
            try:
                code = self._run(dev, clean_note("discord:%s %s" % (uid, person.display or person.name or "")))
            except CodesError as e:
                self.event("code_failed", {"user": uid, "device": mask(dev), "error": str(e)})
                return self.text("failed"), "", [self.text("note_failed", who=who, device=mask(dev), error=templates.safe(e, 160))]
            register_secret(code)
            mine.append({"device": dev, "code": code, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))})
            d["issued"][uid] = mine
            self._save(d)
            self.event("code_issued", {"user": uid, "device": mask(dev)})
        return (self.text("ok", device=dev, code=templates.trusted(code)), code,
                [self.text("note_issued", who=who, device=mask(dev))])
