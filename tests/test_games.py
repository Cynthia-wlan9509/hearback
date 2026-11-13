"""Several games in one Discord server, one SWLink workspace each, one bot ([swlink] game_keys): order numbers and
device codes go to the right game, the bot asks when it cannot tell, the owner's [dm_hook] program answers e-mails
and its own device codes, and roles kept in restore_roles.json come back on join. Offline: FakeDiscord, FakeAfdian."""
from __future__ import annotations

import json
import subprocess

import pytest

from conftest import make_env
from hearback import games
from hearback.chatbot import Message
from hearback.config import load_config, parse_config
from hearback.fake import FakeAfdian
from hearback.llm import LLMStore, scrub
from hearback.supporters import SupporterStore
from hearback.util import HearbackError
from hearback.web import server as web
from test_web import SECRET, FakeKeyring, StubRuntime

AFD = "afd-GAMES-SECRET-token-0123456789"


class Rig:
    def __init__(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.setenv("HEARBACK_AFDIAN_TOKEN", AFD)
        monkeypatch.setattr(SupporterStore, "get_token", lambda self: AFD)   # never the real keyring
        e = self.e = make_env(tmp_path / "one")
        e.cfg.language = "zh"
        f = self.fake = e.fake
        f.give_bot_role(position=10)
        self.r1, self.r2, self.r3 = f.add_role("One", position=3), f.add_role("Two", position=4), f.add_role("Three", position=5)
        self.owner = f.add_user("Dana")
        self.afdian = FakeAfdian(token=AFD)
        self.hook_calls, self.code_calls = [], []
        self.hook_answer = {"reply": "verified", "grant": [self.r1]}
        e.cfg.swlink_game_keys = ["一号", "猫铃"]
        e.cfg.dm_hook = games.parse_dm_hook({"enabled": True, "command": ["hook"], "device_role_ids": [self.r1]})
        spaces = []
        for name, keys, role in (("two", '["二号", "月契"]', self.r2), ("three", '["三号", "拾花"]', self.r3)):
            d = tmp_path / name
            d.mkdir()
            (d / "hearback.toml").write_text(
                '[discord]\nguild_id = "%s"\nforum_id = "%s"\n[swlink]\ngame_keys = %s\n'
                '[codes]\nenabled = true\nrole_id = "%s"\ncommand = ["issue", "{device}", "%s"]\n'
                % (f.guild_id, e.forum, keys, role, name), encoding="utf-8")
            spaces.append(load_config(d / "hearback.toml"))
        self.app = web.App(e.cfg, client_factory=lambda: f.client(), runtime=StubRuntime(), secret=SECRET,
                           workspaces=spaces, afdian_opener=self.afdian, code_runner=self.run,
                           llm_store=LLMStore(e.cfg.data_dir, environ={}, keyring_mod=FakeKeyring()),
                           supporters_store=SupporterStore(e.cfg.data_dir, keyring_mod=None))
        for ws, role in zip(self.app.spaces, (self.r1, self.r2, self.r3)):
            ws.supporters_store.update({"enabled": True, "dm_handling": True, "afdian_user_id": self.afdian.user_id,
                                        "role_id": role, "notify": {"channel_id": "", "user_ids": [self.owner["id"]]}})

    def run(self, args, **kw):
        if args[0] == "hook":
            self.hook_calls.append(json.loads(kw["input"]))
            return subprocess.CompletedProcess(args, 0, json.dumps(self.hook_answer, ensure_ascii=False) + "\n", "")
        self.code_calls.append(args)
        return subprocess.CompletedProcess(args, 0, "CODE-%s-%s\n" % (args[2], args[1]), "")

    def dm(self, user, text):
        ch = self.fake._open_dm(user["id"])["id"]
        self.app.on_message(Message(id=str(len(self.fake.calls) + 1), channel_id=ch, author_id=user["id"],
                                    author_name=user["global_name"], content=text, guild_id=None), self.fake.bot["id"])

    def said(self, user):
        ch = self.fake.dms.get(user["id"])
        return [m["content"] for m in self.fake.messages.get(ch, [])] if ch else []

    def roles(self, user):
        return set(self.fake.members[user["id"]]["roles"])

    def claimed(self, i):
        return set(self.app.spaces[i].supporters_store.load()["claims"])


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def test_config_reads_game_keys_forum_ids_and_the_hook(tmp_path):
    cfg = parse_config({"swlink": {"game_keys": ["二号", " 月契 "]}, "discord": {"forum_ids": ["12", 34]},
                        "dm_hook": {"enabled": True, "command": ["x"], "device_role_ids": ["5"], "tick_minutes": 10}}, tmp_path)
    assert cfg.swlink_game_keys == ["二号", "月契"] and cfg.forum_ids == ["12", "34"]
    assert cfg.dm_hook["device_role_ids"] == ["5"] and cfg.dm_hook["tick_minutes"] == 10
    for bad in ({"swlink": {"game_keys": "二号"}}, {"discord": {"forum_ids": ["x"]}}, {"dm_hook": {"enabled": True}},
                {"dm_hook": {"enabled": True, "command": ["x"], "tick_minutes": 5000}}):
        with pytest.raises(HearbackError):
            parse_config(bad, tmp_path)


def test_an_order_naming_its_game_goes_there(rig):
    mira = rig.fake.add_member("Mira")
    no = rig.afdian.add_order(total_amount="20")["out_trade_no"]
    rig.dm(mira, "二号 " + no)
    assert rig.r2 in rig.roles(mira) and rig.r1 not in rig.roles(mira) and rig.claimed(1) == {no}


def test_the_shop_model_or_the_note_names_the_game(rig):
    mira, jon = rig.fake.add_member("Mira"), rig.fake.add_member("Jon")
    a = rig.afdian.add_order(total_amount="20", product_type=1, sku_detail=[{"name": "三号 · 拾花", "count": 1}])["out_trade_no"]
    b = rig.afdian.add_order(total_amount="20", remark="给月契的")["out_trade_no"]
    rig.dm(mira, a)
    rig.dm(jon, b)
    assert rig.r3 in rig.roles(mira) and rig.r2 in rig.roles(jon)


def test_an_order_that_says_nothing_is_asked_about_then_finished_by_the_answer(rig):
    mira = rig.fake.add_member("Mira")
    no = rig.afdian.add_order(total_amount="20")["out_trade_no"]
    rig.dm(mira, no)
    assert "一号「猫铃」" in rig.said(mira)[-1] and not rig.roles(mira) - set()
    assert not any(rig.claimed(i) for i in range(3))
    rig.dm(mira, "三号")
    assert rig.r3 in rig.roles(mira) and rig.claimed(2) == {no}
    rig.dm(mira, "三号")                       # nothing waiting any more: the answer alone does nothing
    assert rig.claimed(2) == {no}


def test_an_order_claimed_for_one_game_stays_there(rig):
    mira = rig.fake.add_member("Mira")
    no = rig.afdian.add_order(total_amount="20")["out_trade_no"]
    rig.dm(mira, "二号 " + no)
    rig.fake.members[mira["id"]]["roles"] = []
    rig.dm(mira, no)                            # sent again, no game named: the role comes back from game two
    assert rig.roles(mira) == {rig.r2}
    rig.dm(mira, "三号 " + no)                  # one order, one game
    assert rig.claimed(2) == set() and rig.r3 not in rig.roles(mira)


def test_a_device_code_goes_to_the_only_game_the_member_holds(rig):
    mira = rig.fake.add_member("Mira", roles=[rig.r3])
    rig.dm(mira, "1A2B-3C4D-5E6F-7A8B")
    assert rig.code_calls and rig.code_calls[-1][2] == "three" and "CODE-three-1A2B-3C4D-5E6F-7A8B" in rig.said(mira)


def test_several_games_held_the_bot_asks_and_a_named_game_decides(rig):
    mira = rig.fake.add_member("Mira", roles=[rig.r2, rig.r3])
    rig.dm(mira, "1A2B-3C4D-5E6F-7A8B")
    assert not rig.code_calls and "前面加上编号" in rig.said(mira)[-1]
    rig.dm(mira, "月契 1A2B-3C4D-5E6F-7A8B")
    assert rig.code_calls[-1][2] == "two"


def test_the_hook_game_gets_its_device_codes_and_emails(rig):
    mira = rig.fake.add_member("Mira", roles=[rig.r1])
    rig.hook_answer = {"reply": "your licence: L1.abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH", "notify": ["signed one"]}
    rig.dm(mira, "设备码：1A2B-3C4D-5E6F-7A8B")
    assert rig.hook_calls[-1]["text"].endswith("1A2B-3C4D-5E6F-7A8B") and rig.hook_calls[-1]["roles"] == [rig.r1]
    assert rig.said(mira)[-1].startswith("your licence") and not rig.code_calls
    page = rig.app.api_conv_messages(web.Request("GET", "", {}, {}, (rig.fake.dms[mira["id"]],)))[1]
    assert page["items"][-1]["masked"] and "abcdefghijklmnopqrstuvwxyz0123456789" not in scrub(json.dumps(page, ensure_ascii=False))
    jon = rig.fake.add_member("Jon")
    rig.hook_answer = {"reply": "verified", "grant": [rig.r1]}
    rig.dm(jon, "邮箱：jon@example.com")
    assert rig.hook_calls[-1]["text"] == "邮箱：jon@example.com" and rig.r1 in rig.roles(jon) and rig.said(jon) == ["verified"]


def test_a_failing_hook_leaves_the_message_to_the_normal_path(rig):
    jon = rig.fake.add_member("Jon")
    rig.run = lambda args, **kw: subprocess.CompletedProcess(args, 3, "", "boom")
    rig.app.home.hook.runner = rig.run
    rig.dm(jon, "邮箱：jon@example.com")
    assert not rig.roles(jon) and any(e["action"] == "dm_hook_error" for e in rig.e.ledger.read_events())


def test_roles_kept_for_someone_come_back_when_they_join_once(rig):
    two = rig.app.spaces[1]
    mira = rig.fake.add_member("Mira")
    two.restore.path.parent.mkdir(parents=True, exist_ok=True)
    two.restore.path.write_text(json.dumps({"users": {mira["id"]: [rig.r2]}}), encoding="utf-8")
    rig.app.on_join(rig.fake.guild_id, mira["id"], False, rig.owner["id"])
    assert rig.r2 in rig.roles(mira) and two.restore.due(mira["id"]) == []
    rig.fake.members[mira["id"]]["roles"] = []
    rig.app.on_join(rig.fake.guild_id, mira["id"], False, rig.owner["id"])     # used once
    assert rig.roles(mira) == set()


def test_a_workspace_with_forum_ids_pulls_only_those(tmp_path, monkeypatch):
    from hearback.sync import sync_all
    e = make_env(tmp_path)
    other = e.fake.add_forum("ideas", ["Later"])
    e.fake.add_thread(e.forum, "Bug", "Mira", "x")
    e.fake.add_thread(other, "Idea", "Lina", "y")
    e.cfg.forum_ids = [other]
    sync_all(e.fake.client(), e.ledger, e.cfg, download=False)
    assert [r["title"] for r in e.ledger.load()["threads"].values()] == ["Idea"]


def test_copy_for_agent_names_each_posts_own_game_folder(rig, tmp_path):
    from hearback import feedback
    f = rig.fake
    ideas = f.add_forum("ideas-two", ["Later"])
    two = rig.app.spaces[1]
    two.cfg.forum_ids, two.cfg.swlink_project_dir = [ideas], str(tmp_path / "game-two")
    rig.e.cfg.swlink_project_dir = str(tmp_path / "game-one")
    a = f.add_thread(rig.e.forum, "Crash", "Mira", "x")
    b = f.add_thread(ideas, "Wish", "Lina", "y")
    from hearback.sync import sync_all
    sync_all(f.client(), rig.e.ledger, rig.e.cfg, download=False)
    text = rig.app.api_feedback_handoff(web.Request("POST", "", {}, {"ids": [a, b]}, ()))[1]["text"]
    assert "每帖不一样" in text and str(tmp_path / "game-one") in text and str(tmp_path / "game-two") in text
    one = rig.app.api_feedback_handoff(web.Request("POST", "", {}, {"ids": [b]}, ()))[1]["text"]
    assert "项目目录：%s" % (tmp_path / "game-two") in one
    names = [i.get("game") for i in rig.app.api_workspaces(web.Request("GET", "", {}, {}, ()))[1]["items"]]
    assert names == ["一号 · 猫铃", "二号 · 月契", "三号 · 拾花"]
    assert feedback.HANDOFF_WORDS[True]["where_each"]
    rows = {r["id"]: r["name"] for r in rig.app._forum_rows(rig.e.ledger.load())}   # one inbox: each forum says its game
    assert rows[ideas] == "月契 · ideas-two" and rows[rig.e.forum].startswith("猫铃 · ")

