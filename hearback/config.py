"""Configuration: hearback.toml discovery, defaults, token loading, and the `init` template."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from hearback.util import HearbackError, ensure_data_dir

CONFIG_NAME = "hearback.toml"
DATA_DIR = ".hearback"
LANGUAGES = ("en", "zh")

SETUP_HINT = (
    "No Discord bot token found. One-time setup:\n"
    "  1) https://discord.com/developers/applications -> New Application -> Bot -> Reset Token\n"
    "  2) On the same Bot page enable MESSAGE CONTENT INTENT (otherwise post bodies are empty)\n"
    "  3) OAuth2 -> URL Generator: scope 'bot'; permissions View Channels, Read Message History,\n"
    "     Send Messages, Send Messages in Threads, Create Public Threads, Manage Threads,\n"
    "     Attach Files, Add Reactions; use the link to invite the bot\n"
    "  4) Put the token in the environment variable named by token_env (default DISCORD_BOT_TOKEN)\n"
    "     or in the file named by token_file (default .hearback/token.txt). Never commit it."
)

TEMPLATE = '''\
# Hearback configuration. Everything is optional except forum_id (and guild_id, which is
# looked up from the forum channel when left empty).
# Right-click a server or channel in Discord (Developer Mode on) -> "Copy ID".

[discord]
guild_id = ""
forum_id = ""                  # the bug-report forum channel
staff_channel_id = ""          # optional: private channel where proposals are approved with reactions
announce_channel_id = ""       # optional: where release announcements are posted
approvers = []                 # user ids allowed to approve by reaction, as a list of strings
token_env = "DISCORD_BOT_TOKEN"        # environment variable that holds the bot token
token_file = ".hearback/token.txt"     # used when the environment variable is not set
send_delay = 1.0               # seconds between writes to Discord when sending

# Triage status -> forum tag name. Used when you ask Hearback to mirror a status change as a tag.
[status_tags]
investigating = "In progress"
need-info = "Need info"
fixed-pending-release = "Fixed, awaiting release"
released = "Released"

# One table per build variant you ship. `tag` is the forum tag reporters pick; `download`
# is where they get the fixed build (shown in release notifications).
[variants.free]
tag = "Free"
download = "the pinned link in #downloads"

[variants.full]
tag = "Full"
download = "the #supporters channel"

[templates]
language = "en"                # en | zh. Override any text in .hearback/templates/<name>.txt
'''


@dataclass
class Variant:
    name: str
    tag: str = ""
    download: str = ""


@dataclass
class Config:
    """Settings for one Hearback workspace. The bot token is never stored here."""

    root: Path = field(default_factory=Path.cwd)
    path: Path | None = None
    guild_id: str = ""
    forum_id: str = ""
    staff_channel_id: str = ""
    announce_channel_id: str = ""
    approvers: list[str] = field(default_factory=list)
    token_env: str = "DISCORD_BOT_TOKEN"
    token_file: str = ".hearback/token.txt"
    send_delay: float = 1.0
    status_tags: dict[str, str] = field(default_factory=dict)
    variants: dict[str, Variant] = field(default_factory=dict)
    language: str = "en"
    swlink_hourly_cap: int = 60      # [swlink] hourly_cap: auto replies per hour before drafts are forced

    # ---- paths
    @property
    def data_dir(self) -> Path:
        return self.root / DATA_DIR

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ledger.json"

    @property
    def queue_dir(self) -> Path:
        return self.data_dir / "queue"

    @property
    def templates_dir(self) -> Path:
        return self.data_dir / "templates"

    def ensure_data_dir(self) -> Path:
        """Create .hearback/ with its self-ignoring .gitignore (idempotent)."""
        return ensure_data_dir(self.data_dir)

    @property
    def token_path(self) -> Path:
        f = Path(self.token_file)
        return f if f.is_absolute() else self.root / f

    def protected_paths(self) -> list[Path]:
        """Files and directories that must never be uploaded to Discord: the data dir, the token file, the config."""
        return [p for p in (self.data_dir, self.token_path, self.path) if p]

    def validate_variants(self, names: list[str]) -> list[str]:
        """Check variant names against [variants.*]. No variants configured -> anything goes."""
        bad = [n for n in names if self.variants and n not in self.variants]
        if bad:
            raise HearbackError("unknown variant(s): %s. Configured variants: %s"
                                % (", ".join(bad), ", ".join(self.variants)))
        return names

    # ---- token
    def token(self, environ: Mapping[str, str] | None = None) -> str:
        """Read the token from the env var, then the token file. Never print the result."""
        env = os.environ if environ is None else environ
        tok = (env.get(self.token_env) or "").strip()
        if not tok:
            f = self.token_path
            if f.is_file():
                tok = f.read_text(encoding="utf-8-sig").strip()
        if tok.lower().startswith("bot "):
            tok = tok[4:].strip()
        if not tok:
            raise HearbackError(SETUP_HINT)
        return tok

    def require_forum(self) -> str:
        if not self.forum_id:
            raise HearbackError("forum_id is not set. Run `hearback init` and fill in hearback.toml "
                                "([discord] forum_id).")
        return self.forum_id

    def variant_for_tag(self, tag_name: str) -> str | None:
        for v in self.variants.values():
            if v.tag and v.tag.lower() == tag_name.lower():
                return v.name
        return None


def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()


def find_config(start: Path) -> Path | None:
    for d in [start, *start.parents]:
        f = d / CONFIG_NAME
        if f.is_file():
            return f
    return None


def parse_config(raw: Mapping[str, Any], root: Path, path: Path | None = None) -> Config:
    d = raw.get("discord") or {}
    cfg = Config(
        root=root, path=path,
        guild_id=_s(d.get("guild_id")), forum_id=_s(d.get("forum_id")),
        staff_channel_id=_s(d.get("staff_channel_id")),
        announce_channel_id=_s(d.get("announce_channel_id")),
        approvers=[_s(x) for x in d.get("approvers") or [] if _s(x)],
        token_env=_s(d.get("token_env")) or "DISCORD_BOT_TOKEN",
        token_file=_s(d.get("token_file")) or ".hearback/token.txt",
    )
    try:
        cfg.send_delay = float(d.get("send_delay", 1.0))
    except (TypeError, ValueError):
        raise HearbackError("[discord] send_delay must be a number")
    cfg.status_tags = {_s(k): _s(v) for k, v in (raw.get("status_tags") or {}).items() if _s(v)}
    for name, v in (raw.get("variants") or {}).items():
        v = v if isinstance(v, dict) else {}
        cfg.variants[str(name)] = Variant(str(name), tag=_s(v.get("tag")), download=_s(v.get("download")))
    lang = _s((raw.get("templates") or {}).get("language")) or "en"
    if lang not in LANGUAGES:
        raise HearbackError("[templates] language must be one of: %s" % ", ".join(LANGUAGES))
    cfg.language = lang
    try:
        cfg.swlink_hourly_cap = max(1, int((raw.get("swlink") or {}).get("hourly_cap", 60)))
    except (TypeError, ValueError):
        raise HearbackError("[swlink] hourly_cap must be a whole number")
    return cfg


def load_config(path: str | os.PathLike[str] | None = None, cwd: str | os.PathLike[str] | None = None) -> Config:
    """Load hearback.toml (explicit path, else searched from cwd upwards). No file -> defaults."""
    start = Path(cwd or os.getcwd()).resolve()
    if path:
        f = Path(path).resolve()
        if not f.is_file():
            raise HearbackError("config file not found: %s" % path)
    else:
        f = find_config(start)
    if f is None:
        return Config(root=start)
    try:
        with open(f, "rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise HearbackError("%s is not valid TOML: %s" % (f.name, e))
    return parse_config(raw, f.parent, f)


def write_template(directory: str | os.PathLike[str], force: bool = False) -> Path:
    """`hearback init`: write a commented hearback.toml and a self-ignoring .hearback/ directory."""
    d = Path(directory)
    f = d / CONFIG_NAME
    if f.exists() and not force:
        raise HearbackError("%s already exists (use --force to overwrite)" % f)
    d.mkdir(parents=True, exist_ok=True)
    f.write_text(TEMPLATE, encoding="utf-8", newline="\n")
    ensure_data_dir(d / DATA_DIR)   # self-ignoring: token + ledger stay out of git
    return f
