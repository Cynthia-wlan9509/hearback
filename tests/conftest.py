"""Shared fixtures: a FakeDiscord forum, a workspace in tmp_path, and helpers. Everything is offline."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pytest

from hearback.config import Config, Variant
from hearback.discord import DiscordClient
from hearback.fake import FakeDiscord, make_png
from hearback.ledger import Ledger
from hearback.queue import Queue
from hearback.sync import sync

TAGS = ["Crash", "UI", "Free", "Full", "In progress", "Fixed, awaiting release", "Released", "Need info"]


class Clock:
    """A controllable clock for claim-lease tests."""

    def __init__(self) -> None:
        self.now = dt.datetime(2026, 10, 24, 12, 0, 0, tzinfo=dt.timezone.utc)

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += dt.timedelta(**kw)


@dataclass
class Env:
    fake: FakeDiscord
    cfg: Config
    ledger: Ledger
    queue: Queue
    client: DiscordClient
    forum: str
    staff: str
    announce: str
    clock: Clock
    threads: dict[str, str] = field(default_factory=dict)

    def sync(self, **kw):
        return sync(self.client, self.ledger, self.cfg, **kw)

    def triage(self, key: str, **patch):
        patch.setdefault("status", "investigating")
        return self.ledger.update_triage(self.threads[key], patch, actor="agent:test")

    def approve_and_send(self, *proposals):
        self.queue.approve([p.id for p in proposals], "human:test")
        return self.queue.send(self.client, self.ledger, forum_id=self.cfg.forum_id, delay=0,
                               sleep=lambda s: None, actor="human:test")


def make_env(tmp_path, **cfg_kw) -> Env:
    fake = FakeDiscord()
    forum = fake.add_forum("bugs", TAGS)
    staff, announce = fake.add_channel("staff"), fake.add_channel("announce")
    cfg = Config(root=tmp_path, guild_id=fake.guild_id, forum_id=forum, staff_channel_id=staff,
                 announce_channel_id=announce, send_delay=0.0,
                 status_tags={"investigating": "In progress", "need-info": "Need info",
                              "fixed-pending-release": "Fixed, awaiting release", "released": "Released"},
                 variants={"free": Variant("free", "Free", "the pinned link"),
                           "full": Variant("full", "Full", "the supporters channel")},
                 **cfg_kw)
    clock = Clock()
    ledger = Ledger(cfg.ledger_path, clock=clock)
    queue = Queue(cfg.queue_dir, ledger)
    return Env(fake, cfg, ledger, queue, fake.client(), forum, staff, announce, clock)


@pytest.fixture
def env(tmp_path) -> Env:
    """Empty forum, nothing synced."""
    return make_env(tmp_path)


@pytest.fixture
def seeded(tmp_path) -> Env:
    """Four threads (one with a screenshot, one archived), already synced."""
    e = make_env(tmp_path)
    f = e.fake
    e.threads["oven"] = f.add_thread(e.forum, "Oven freezes", "Mira", "Stops at 0:00", ["Crash", "Free"],
                                     files=[("shot.png", make_png())])
    f.add_reply(e.threads["oven"], "Jon", "Same here")
    e.threads["flicker"] = f.add_thread(e.forum, "Customers flicker", "Lina", "On tablet", ["UI", "Full"])
    e.threads["typo"] = f.add_thread(e.forum, "Typo in shop", "Ben", "Croissaint", ["Free"], archived=True)
    e.threads["dark"] = f.add_thread(e.forum, "Dark mode please", "Zoe", "Night time", [])
    e.sync()
    return e


def fresh_reply(e: Env, key: str = "oven", text: str = "Hello Mira") -> object:
    """Queue a reply proposal on a seeded thread."""
    return e.queue.propose("reply", actor="agent:test", thread=e.threads[key], text=text, why="test")


@dataclass
class CliWs:
    """A hearback.toml in tmp_path wired to a FakeDiscord, plus a runner for `hearback ...`."""

    fake: FakeDiscord
    root: object
    forum: str
    staff: str
    announce: str
    capsys: object
    approver: dict = field(default_factory=dict)
    threads: dict = field(default_factory=dict)

    def run(self, *argv: str) -> tuple[int, str, str]:
        from hearback import cli
        self.capsys.readouterr()
        rc = cli.main(list(argv))
        out = self.capsys.readouterr()
        return rc, out.out, out.err


CLI_TOKEN = "tok_SECRET_9f8e7d6c5b4a"


@pytest.fixture
def cli_ws(tmp_path, monkeypatch, capsys) -> CliWs:
    from hearback import cli
    fake = FakeDiscord(token=CLI_TOKEN)
    forum = fake.add_forum("bugs", TAGS)
    staff, announce = fake.add_channel("staff"), fake.add_channel("announce")
    approver = fake.add_user("Dana")
    (tmp_path / "hearback.toml").write_text(f'''
[discord]
guild_id = "{fake.guild_id}"
forum_id = "{forum}"
staff_channel_id = "{staff}"
announce_channel_id = "{announce}"
approvers = ["{approver["id"]}"]
send_delay = 0
[status_tags]
investigating = "In progress"
fixed-pending-release = "Fixed, awaiting release"
released = "Released"
[variants.free]
tag = "Free"
download = "the pinned link"
[variants.full]
tag = "Full"
download = "the supporters channel"
''', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", CLI_TOKEN)
    monkeypatch.delenv("HEARBACK_ACTOR", raising=False)
    # the real token-loading path runs; only the network is swapped for the fake
    monkeypatch.setattr(cli, "make_client",
                        lambda cfg: DiscordClient(cfg.token(), transport=fake, sleep=lambda s: None))
    ws = CliWs(fake, tmp_path, forum, staff, announce, capsys, approver)
    ws.threads["oven"] = fake.add_thread(forum, "Oven freezes", "Mira", "Stops at 0:00", ["Crash", "Free"],
                                         files=[("shot.png", make_png())])
    ws.threads["flicker"] = fake.add_thread(forum, "Customers flicker", "Lina", "On tablet", ["UI", "Full"])
    return ws
