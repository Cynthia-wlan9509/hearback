"""SWLink codes: a member with the role DMs a device code, the owner's command makes the code, the bot DMs it back.
The command is a fake runner; Discord is FakeDiscord; nothing runs a real process."""
from __future__ import annotations

import subprocess
import threading
import types

import pytest

from conftest import make_env
from hearback.codes import Codes, parse_codes
from hearback.config import parse_config
from hearback.ledger import Ledger
from hearback.llm import LLMStore
from hearback.supporters import Person
from hearback.util import HearbackError
from hearback.web import server as web
from test_web import SECRET, FakeKeyring, StubRuntime

ROLE = "77"
DEV_A, DEV_B = "1A2B-3C4D-5E6F-7A8B", "0000-1111-2222-3333"
SETTINGS = {"enabled": True, "role_id": ROLE, "command": ["issue", "--device", "{device}", "--note", "{note}"],
            "per_user": 1}


class Runner:
    def __init__(self, rc=0, out=None):
        self.calls, self.rc, self.out, self.n = [], rc, out, 0

    def __call__(self, args, **kw):
        assert kw.get("shell") is False and isinstance(args, list)
        self.calls.append(args)
        self.n += 1
        out = self.out if self.out is not None else "noise line\nMZ1.CODE-%d-%s\n" % (self.n, args[2])
        return types.SimpleNamespace(returncode=self.rc, stdout=out, stderr="boom" if self.rc else "")


def make(tmp_path, runner=None, **over):
    ledger = Ledger(tmp_path / "ledger.json")
    return Codes(parse_codes({**SETTINGS, **over}), tmp_path, ledger=ledger, runner=runner or Runner()), ledger


def member(uid="5", roles=(ROLE,), name="Mira"):
    return Person(id=uid, name=name, display=name, role_ids=set(roles))


# ---------------------------------------------------------------- config
def test_parse_codes():
    assert parse_codes(None)["enabled"] is False
    c = parse_codes(SETTINGS)
    assert c["per_user"] == 1 and c["command"][0] == "issue"
    for bad in ({"enabled": "yes"}, {"enabled": True, "command": ["x"]}, {"enabled": True, "role_id": "1"},
                {"role_id": "abc"}, {"command": "node x.js"}, {"pattern": "("}, {"per_user": 0}, ["x"]):
        with pytest.raises(HearbackError):
            parse_codes(bad)


def test_config_reads_codes_table(tmp_path):
    cfg = parse_config({"codes": SETTINGS}, tmp_path)
    assert cfg.codes["enabled"] and cfg.codes["role_id"] == ROLE
    assert parse_config({}, tmp_path).codes["enabled"] is False


def test_find_device_codes():
    c = Codes(parse_codes(SETTINGS), "unused")
    assert c.find("my code is 1a2b-3c4d-5e6f-7a8b thanks") == DEV_A
    assert c.find("order 202610300000000000000001") == "" and c.find("") == ""
    assert c.find("X1A2B-3C4D-5E6F-7A8B") == ""


# ---------------------------------------------------------------- decisions
def test_issue_then_same_device_again(tmp_path):
    r = Runner()
    c, ledger = make(tmp_path, r)
    reply, code, notes = c.handle(member(), DEV_A)
    assert code == "MZ1.CODE-1-" + DEV_A and code not in reply and DEV_A in reply   # the code goes alone, after
    assert r.calls == [["issue", "--device", DEV_A, "--note", "discord:5 Mira"]]
    assert notes and code not in notes[0]
    reply2, code2, notes2 = c.handle(member(), DEV_A.lower())
    assert code2 == code and code not in reply2 and notes2 == [] and len(r.calls) == 1   # not issued twice
    events = ledger.read_events(20)
    assert [e["action"] for e in events] == ["code_issued"]
    assert code not in repr(events) and DEV_A not in repr(events)
    assert c.count() == 1


def test_one_device_per_member_and_one_member_per_device(tmp_path):
    r = Runner()
    c, _ = make(tmp_path, r)
    c.handle(member(), DEV_A)
    reply, code, notes = c.handle(member(), DEV_B)                 # a second phone: the owner decides
    assert code == "" and "one device" in reply and DEV_B in notes[0]
    reply, code, notes = c.handle(member(uid="6", name="Jon"), DEV_A)   # someone else's device
    assert code == "" and "already has a code" in reply and len(r.calls) == 1
    c2, _ = make(tmp_path / "two", Runner(), per_user=2)
    c2.handle(member(), DEV_A)
    assert c2.handle(member(), DEV_B)[1]


def test_needs_the_role(tmp_path):
    r = Runner()
    c, _ = make(tmp_path, r)
    reply, code, notes = c.handle(member(roles=()), DEV_A)
    assert code == "" and "verification" in reply and notes == [] and r.calls == []


def test_command_failure_tells_the_owner(tmp_path):
    c, ledger = make(tmp_path, Runner(rc=1))
    reply, code, notes = c.handle(member(), DEV_A)
    assert code == "" and "could not be made" in reply and "boom" in notes[0]
    assert c.count() == 0 and [e["action"] for e in ledger.read_events(5)] == ["code_failed"]
    for out in ("", "two words\n"):
        c, _ = make(tmp_path / ("o%d" % len(out)), Runner(out=out))
        assert c.handle(member(), DEV_A)[1] == ""


def test_runner_errors_and_timeouts(tmp_path):
    def slow(args, **kw):
        raise subprocess.TimeoutExpired(args, 60)
    c, _ = make(tmp_path, slow)
    assert c.handle(member(), DEV_A)[1] == ""


def test_damaged_store_is_an_error(tmp_path):
    c, _ = make(tmp_path)
    (tmp_path / "codes.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(HearbackError):
        c.handle(member(), DEV_A)


# ---------------------------------------------------------------- over the bot
@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    e = make_env(tmp_path, codes=parse_codes(SETTINGS))
    r = Runner()
    app = web.App(e.cfg, client_factory=lambda: e.fake.client(), runtime=StubRuntime(), secret=SECRET,
                  llm_store=LLMStore(e.cfg.data_dir, environ={}, keyring_mod=FakeKeyring()), code_runner=r)
    owner = e.fake.add_user("Dana")
    app.supporters_store.update({"notify": {"user_ids": [owner["id"]]}})
    return types.SimpleNamespace(e=e, app=app, runner=r, owner=owner)


def dm(b, user, text):
    from hearback.chatbot import Message
    ch = b.e.fake._open_dm(user["id"])["id"]
    b.app.on_message(Message(id="1", channel_id=ch, author_id=user["id"], author_name=user["global_name"],
                             content=text), b.e.fake.bot["id"])
    return [m["content"] for m in b.e.fake.messages.get(ch, [])]


def test_member_with_role_gets_the_code_by_dm(bot):
    role = bot.e.fake.add_role("Verified", position=1)
    bot.app.home.codes.s["role_id"] = role
    mira = bot.e.fake.add_member("Mira", roles=[role])
    said = dm(bot, mira["user"] if "user" in mira else mira, "here: " + DEV_A)
    assert len(said) == 2 and "MZ1.CODE" not in said[0] and said[1] == "MZ1.CODE-1-" + DEV_A   # the code alone
    again = dm(bot, mira["user"] if "user" in mira else mira, DEV_A)
    assert len(again) == 4 and again[3] == said[1] and bot.runner.calls[1:] == []
    owner_dm = bot.e.fake.messages.get(bot.e.fake.dms.get(bot.owner["id"]), [])
    assert owner_dm and "MZ1.CODE" not in owner_dm[0]["content"]
    assert bot.app.inbox.conversations() == []                       # the chat rules never saw it


def test_without_role_or_not_in_server(bot):
    role = bot.e.fake.add_role("Verified", position=1)
    bot.app.home.codes.s["role_id"] = role
    jon = bot.e.fake.add_member("Jon")
    said = dm(bot, jon["user"] if "user" in jon else jon, DEV_A)
    assert len(said) == 1 and "verification" in said[0] and bot.runner.calls == []
    stranger = bot.e.fake.add_user("Zed")
    said = dm(bot, stranger, DEV_B)
    assert len(said) == 1 and "cannot find you" in said[0]
    assert dm(bot, stranger, "hello")[-1:] == said[-1:]           # no device code: not ours, nothing new said
