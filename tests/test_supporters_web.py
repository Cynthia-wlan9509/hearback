"""SWLink supporters over the console API and the bot: settings, token, roles, orders, link/unlink, simulate, the DM
path and the retry loop. Afdian is `FakeAfdian`, Discord is `FakeDiscord`, the gateway is not started.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error

import pytest

from conftest import make_env
from hearback import llm, templates
from hearback.chatbot import Message
from hearback.fake import FakeAfdian
from hearback.llm import LLMStore, set_provider_factory
from hearback.supporters import SupporterStore, mask
from hearback.web import server as web
from test_web import SECRET, FakeKeyring, StubRuntime, Web

TOKEN = "afd-WEB-SECRET-token-0123456789"
UNSEEN = "20269999000000000000000002"
EN = templates.EN


class Clock:
    def __init__(self, t: float = 1_790_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Rig:
    def __init__(self, w: Web, afdian: FakeAfdian, clock: Clock, store: SupporterStore):
        self.w, self.afdian, self.clock, self.store = w, afdian, clock, store
        self.e, self.fake, self.app = w.env, w.env.fake, w.srv.app
        f = self.fake
        self.bot_role = f.give_bot_role(position=10)
        self.supporter = f.add_role("Supporter", position=3)
        self.gold = f.add_role("Gold", position=4)
        self.verified = f.add_role("Verified", position=2)
        self.boss = f.add_role("Boss", position=20)
        self.owner = f.add_user("Dana")
        self.mira = f.add_member("Mira", roles=[self.verified])
        self.jon = f.add_member("Jon", roles=[self.verified])

    # ---- setup
    def configure(self, **settings) -> dict:
        self.w.ok("POST", "/api/supporters/token", {"token": TOKEN})
        body = {"afdian_user_id": self.afdian.user_id, "role_id": self.supporter, "enabled": True, **settings}
        return self.w.ok("PUT", "/api/supporters/settings", body)

    def go_live(self, **settings) -> dict:
        return self.configure(dm_handling=True, notify={"user_ids": [self.owner["id"]]}, **settings)

    def order(self, **kw) -> str:
        return self.afdian.add_order(**kw)["out_trade_no"]

    # ---- the bot side
    def dm(self, user: dict, text: str) -> str:
        ch = self.fake._open_dm(user["id"])["id"]
        msg = Message(id="1", channel_id=ch, author_id=user["id"], author_name=user["global_name"], content=text,
                      guild_id=None)
        self.app.on_message(msg, self.fake.bot["id"])
        return ch

    def said(self, user: dict) -> list[str]:
        ch = self.fake.dms.get(user["id"])
        return [m["content"] for m in self.fake.messages.get(ch, [])] if ch else []

    def roles_of(self, user: dict) -> list[str]:
        return list(self.fake.members[user["id"]]["roles"])

    def puts(self) -> list:
        return [c for c in self.fake.calls if c.method == "PUT" and "/roles/" in c.path]

    def events(self, action=None) -> list[dict]:
        return [x for x in self.e.ledger.read_events() if action is None or x["action"] == action]


@pytest.fixture
def sup(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    e = make_env(tmp_path)
    clock = Clock()
    afdian = FakeAfdian(token=TOKEN, clock=clock)
    store = SupporterStore(e.cfg.data_dir, environ={}, keyring_mod=FakeKeyring())
    llm_store = LLMStore(e.cfg.data_dir, environ={}, keyring_mod=FakeKeyring())
    srv = web.create_server(e.cfg, 0, client_factory=lambda: e.fake.client(), llm_store=llm_store,
                            runtime=StubRuntime(), secret=SECRET, clock=clock, supporters_store=store,
                            afdian_opener=afdian)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    rig = Rig(Web(e, srv), afdian, clock, store)
    yield rig
    srv.app.supporter_bot.stop()
    srv.shutdown()
    srv.server_close()
    for p in llm.PROTOCOLS:
        set_provider_factory(p, None)


# ---------------------------------------------------------------- settings and the token

def test_settings_start_empty_and_save_what_is_valid(sup):
    d = sup.w.ok("GET", "/api/supporters/settings")
    assert d["settings"]["enabled"] is False and d["settings"]["role_id"] == "" and d["token_set"] is False
    assert d["guild"]["id"] == sup.fake.guild_id and d["counts"] == {"claims": 0, "retries": 0}
    assert d["dev_url"] == "https://afdian.com/dashboard/dev" and d["issues"] == []
    out = sup.w.ok("PUT", "/api/supporters/settings", {"role_id": sup.supporter, "min_amount": "5",
                                                       "require_role_ids": [sup.verified], "lookups_per_day": 4})
    assert out["settings"]["min_amount"] == "5" and out["settings"]["require_role_ids"] == [sup.verified]
    assert sup.w.ok("GET", "/api/supporters/settings")["settings"]["lookups_per_day"] == 4
    assert [e["actor"] for e in sup.events("supporters_settings")] == ["human:web"]


@pytest.mark.parametrize("body", [{"role_id": "Supporter"}, {"min_amount": "-3"}, {"nope": 1}, {"enabled": "yes"},
                                  {"notify": {"channel_id": "x"}}, {"lookups_per_day": 0}])
def test_bad_settings_are_a_400_and_change_nothing(sup, body):
    before = sup.w.ok("GET", "/api/supporters/settings")["settings"]
    assert sup.w.call("PUT", "/api/supporters/settings", body).status == 400
    assert sup.w.ok("GET", "/api/supporters/settings")["settings"] == before


def test_dm_checking_cannot_be_switched_on_before_the_connection_is_set_up(sup):
    r = sup.w.call("PUT", "/api/supporters/settings", {"enabled": True, "dm_handling": True})
    assert r.status == 400 and r.json["error"] == "dm_needs_setup"
    sup.w.ok("POST", "/api/supporters/token", {"token": TOKEN})
    sup.w.ok("PUT", "/api/supporters/settings", {"afdian_user_id": "abc", "role_id": sup.supporter})
    assert sup.w.ok("PUT", "/api/supporters/settings", {"enabled": True, "dm_handling": True})["settings"]["dm_handling"]


def test_the_token_is_write_only_everywhere(sup, tmp_path):
    r = sup.w.ok("POST", "/api/supporters/token", {"token": " %s " % TOKEN})
    assert r == {"token_set": True}
    assert sup.store.get_token() == TOKEN
    sup.configure()
    assert sup.w.ok("GET", "/api/supporters/settings")["token_set"] is True
    sup.w.ok("POST", "/api/supporters/test", {})
    assert sup.w.call("POST", "/api/supporters/token", {"token": "has space"}).status == 400
    assert sup.w.call("POST", "/api/supporters/token", {}).status == 400
    assert all(TOKEN.encode() not in b for b in sup.w.bodies)
    on_disk = "\n".join(p.read_bytes().decode("utf-8", "replace") for p in tmp_path.rglob("*") if p.is_file())
    assert TOKEN not in on_disk and "supporters.json" in str(sorted(p.name for p in tmp_path.rglob("*.json")))
    token_events = sup.events("supporters_token_update")
    assert [(e["actor"], e["detail"]) for e in token_events] == [("human:web", None)] * 2
    assert all(TOKEN not in json.dumps(e) for e in sup.events())


def test_every_supporter_route_needs_the_key_and_json(sup):
    routes = [("GET", "/api/supporters/settings"), ("PUT", "/api/supporters/settings"), ("POST", "/api/supporters/token"),
              ("POST", "/api/supporters/test"), ("GET", "/api/supporters/orders"), ("GET", "/api/supporters/claims"),
              ("POST", "/api/supporters/link"), ("POST", "/api/supporters/unlink"), ("POST", "/api/supporters/simulate"),
              ("GET", "/api/guilds/%s/roles" % sup.fake.guild_id), ("GET", "/api/guilds/%s/members" % sup.fake.guild_id)]
    for method, path in routes:
        assert sup.w.call(method, path, {} if method != "GET" else None, key=None).status == 403, path
        if method != "GET":
            assert sup.w.call(method, path, b"{}", ctype="text/plain").status == 415, path


def test_a_damaged_supporters_file_is_reported_not_reset(sup):
    sup.configure()
    sup.store.path.write_text("{oops", encoding="utf-8")
    r = sup.w.call("GET", "/api/supporters/settings")
    assert r.status == 400 and "damaged" in r.json["error"]
    assert sup.w.ok("GET", "/api/status")["supporters"] == {"claims": 0, "retries": 0, "damaged": True}
    assert sup.store.path.read_text(encoding="utf-8") == "{oops"


# ---------------------------------------------------------------- connection test

def test_the_test_button_pings_afdian_and_explains_a_wrong_token(sup):
    assert sup.w.call("POST", "/api/supporters/test", {}).json["error"] == "afdian_not_set"
    sup.configure()
    assert sup.w.ok("POST", "/api/supporters/test", {}) == {"ok": True}
    assert sup.afdian.calls[-1][0] == "ping"
    sup.w.ok("POST", "/api/supporters/token", {"token": "a-different-token-98765"})
    r = sup.w.ok("POST", "/api/supporters/test", {})
    assert r["ok"] is False and "signature" in r["error"] and "a-different-token-98765" not in r["error"]
    sup.w.ok("POST", "/api/supporters/token", {"token": TOKEN})
    sup.afdian.fail_next(urllib.error.URLError("no route"))
    assert sup.w.ok("POST", "/api/supporters/test", {})["error"].startswith("could not reach Afdian")


# ---------------------------------------------------------------- roles the bot may hand out

def test_the_roles_list_says_which_roles_the_bot_can_assign(sup):
    r = sup.w.ok("GET", "/api/guilds/%s/roles" % sup.fake.guild_id)
    by = {x["name"]: x for x in r["items"]}
    assert "@everyone" not in by
    assert by["Supporter"]["assignable"] and by["Gold"]["assignable"] and by["Verified"]["assignable"]
    assert not by["Boss"]["assignable"] and not by["Hearback"]["assignable"]                     # above / equal to the bot
    assert r["bot_top"]["name"] == "Hearback" and r["can_manage_roles"] is True
    assert [x["position"] for x in r["items"]] == sorted((x["position"] for x in r["items"]), reverse=True)
    assert sup.w.call("GET", "/api/guilds/%s/roles" % ("9" * 5)).status == 400


def test_a_bot_without_manage_roles_has_nothing_assignable(sup):
    sup.fake.roles[sup.bot_role]["permissions"] = "0"
    r = sup.w.ok("GET", "/api/guilds/%s/roles" % sup.fake.guild_id)
    assert r["can_manage_roles"] is False and not any(x["assignable"] for x in r["items"])


def test_the_member_picker_searches_by_name_and_skips_bots(sup):
    items = sup.w.ok("GET", "/api/guilds/%s/members?q=mir" % sup.fake.guild_id)["items"]
    assert [m["id"] for m in items] == [sup.mira["id"]] and items[0]["display"] == "Mira"
    everyone = sup.w.ok("GET", "/api/guilds/%s/members" % sup.fake.guild_id)["items"]
    assert sup.fake.bot["id"] not in [m["id"] for m in everyone]
    sup.fake.fail_next(r"GET /guilds/\d+/members/search", 403)
    r = sup.w.call("GET", "/api/guilds/%s/members?q=mir" % sup.fake.guild_id)
    assert r.status == 400 and r.json["error"] == "members_unavailable"


# ---------------------------------------------------------------- orders, claims, link, unlink

def test_the_orders_table_hides_amounts_and_shows_claim_state(sup):
    sup.configure()
    paid = sup.order(total_amount="12.50", title="Monthly", plan_id="plan-a")
    sup.order(status=1)
    sup.w.ok("POST", "/api/supporters/link", {"order": paid, "user_id": sup.mira["id"]})
    r = sup.w.ok("GET", "/api/supporters/orders")
    assert "amount" not in json.dumps(r) and "12.5" not in json.dumps(r)
    first = next(o for o in r["items"] if o["id"] == paid)
    assert first["masked"] == mask(paid) and first["title"] == "Monthly" and first["plan_id"] == "plan-a" and first["paid"]
    assert first["claim"]["user_id"] == sup.mira["id"] and first["claim"]["user_name"] == "mira"
    unpaid = next(o for o in r["items"] if not o["paid"])
    assert unpaid["claim"] is None
    shown = sup.w.ok("GET", "/api/supporters/orders?amounts=1")
    assert next(o for o in shown["items"] if o["id"] == paid)["amount"] == "12.50"


def test_unclaimed_orders_whose_remark_names_a_member_get_a_suggestion_only(sup):
    sup.configure()
    hint = sup.order(remark="for mira please")
    other = sup.order(remark="no names here")
    before = sup.roles_of(sup.mira)
    r = sup.w.ok("GET", "/api/supporters/orders")
    by = {o["id"]: o for o in r["items"]}
    assert by[hint]["suggestions"] == [{"user_id": sup.mira["id"], "name": "Mira", "match": "username"}]
    assert by[other]["suggestions"] == [] and r["members_available"] is True
    assert sup.roles_of(sup.mira) == before and sup.store.load()["claims"] == {}           # nothing granted by itself
    assert "remark" not in json.dumps(r)
    sup.app._members_cache = None
    sup.fake.fail_next(r"GET /guilds/\d+/members$", 403)
    r2 = sup.w.ok("GET", "/api/supporters/orders")
    assert r2["members_available"] is False and all(o["suggestions"] == [] for o in r2["items"])


def test_orders_need_a_connection_first(sup):
    r = sup.w.call("GET", "/api/supporters/orders")
    assert r.status == 400 and r.json["error"] == "afdian_not_set"
    sup.configure()
    sup.afdian.fail_next(urllib.error.URLError("down"))
    r = sup.w.call("GET", "/api/supporters/orders")
    assert r.status == 400 and "could not reach Afdian" in r.json["error"]


def test_linking_by_hand_records_the_claim_gives_the_role_with_a_reason_and_logs_the_actor(sup):
    sup.configure(plan_roles={"plan-gold": sup.gold})
    no = sup.order(plan_id="plan-gold", total_amount="30.00")
    r = sup.w.ok("POST", "/api/supporters/link", {"order": no, "user_id": sup.mira["id"]})
    assert r["lookup"] == "ok" and r["failed"] == [] and set(r["granted"]) == {sup.supporter, sup.gold}
    assert r["claim"]["masked"] == mask(no) and r["claim"]["plan_id"] == "plan-gold" and "amount" not in json.dumps(r)
    assert set(sup.roles_of(sup.mira)) == {sup.verified, sup.supporter, sup.gold}
    assert [c.reason for c in sup.puts()] == ["Afdian supporter (SWLink)"] * 2
    ev = sup.events("supporter_link")
    assert [(e["actor"], e["detail"]) for e in ev] == [("human:web", {"order": mask(no), "user": sup.mira["id"]})]
    assert [e["actor"] for e in sup.events("supporter_grant")] == ["human:web", "human:web"]
    claims = sup.w.ok("GET", "/api/supporters/claims")["items"]
    assert claims[0]["order"] == no and claims[0]["masked"] == mask(no) and "amount" not in claims[0]
    assert sup.w.ok("GET", "/api/supporters/claims?amounts=1")["items"][0]["amount"] == "30"
    assert sup.w.ok("GET", "/api/status")["supporters"] == {"claims": 1, "retries": 0}


def test_linking_is_refused_for_bad_input_and_for_an_order_someone_else_holds(sup):
    sup.configure()
    no = sup.order()
    for body, code in (({"order": "12", "user_id": sup.mira["id"]}, 400), ({"order": no, "user_id": "mira"}, 400),
                       ({"order": no, "user_id": "9" * 5}, 400), ({"order": UNSEEN, "user_id": sup.mira["id"]}, 400),
                       ({"order": sup.order(status=1), "user_id": sup.mira["id"]}, 400)):
        r = sup.w.call("POST", "/api/supporters/link", body)
        assert r.status == code, (body, r.raw)
    assert sup.w.call("POST", "/api/supporters/link", {"order": no, "user_id": "9" * 5}).json["error"] == "not_member"
    assert sup.w.call("POST", "/api/supporters/link", {"order": UNSEEN, "user_id": sup.mira["id"]}).json["error"] == "order_not_found"
    sup.w.ok("POST", "/api/supporters/link", {"order": no, "user_id": sup.mira["id"]})
    r = sup.w.call("POST", "/api/supporters/link", {"order": no, "user_id": sup.jon["id"]})
    assert r.status == 400 and "already linked" in r.json["error"]
    assert sup.roles_of(sup.jon) == [sup.verified]


def test_a_link_still_works_when_afdian_cannot_be_asked(sup):
    sup.configure()
    no = sup.order()
    sup.afdian.fail_next(urllib.error.URLError("down"))
    r = sup.w.ok("POST", "/api/supporters/link", {"order": no, "user_id": sup.mira["id"]})
    assert r["lookup"] == "failed" and sup.supporter in sup.roles_of(sup.mira)


def test_a_link_that_the_bot_cannot_finish_says_which_role_to_move(sup):
    sup.configure(role_id=sup.boss)
    no = sup.order()
    r = sup.w.ok("POST", "/api/supporters/link", {"order": no, "user_id": sup.mira["id"]})
    assert r["granted"] == [] and r["failed"] == [{"kind": "hierarchy", "role_name": "Boss", "bot_role": "Hearback"}]
    assert sup.w.ok("GET", "/api/supporters/settings")["issues"][0]["role_name"] == "Boss"
    assert sup.puts() == []                                                  # no doomed request was even sent


def test_revoking_a_claim_can_take_the_role_back(sup):
    sup.configure()
    a, b = sup.order(), sup.order()
    sup.w.ok("POST", "/api/supporters/link", {"order": a, "user_id": sup.mira["id"]})
    sup.w.ok("POST", "/api/supporters/link", {"order": b, "user_id": sup.jon["id"]})
    keep = sup.w.ok("POST", "/api/supporters/unlink", {"order": a})
    assert keep == {"unlinked": mask(a), "removed_roles": [], "failed_roles": []} and sup.supporter in sup.roles_of(sup.mira)
    r = sup.w.ok("POST", "/api/supporters/unlink", {"order": b, "remove_role": True})
    assert r["removed_roles"] == [sup.supporter] and sup.supporter not in sup.roles_of(sup.jon)
    deletes = [c for c in sup.fake.calls if c.method == "DELETE" and "/roles/" in c.path]
    assert len(deletes) == 1 and deletes[0].reason == "Afdian supporter (SWLink)"
    assert [(e["actor"], e["action"]) for e in sup.events() if e["action"] in ("supporter_unlink", "supporter_role_removed")] == [
        ("human:web", "supporter_unlink"), ("human:web", "supporter_unlink"), ("human:web", "supporter_role_removed")]
    assert sup.w.call("POST", "/api/supporters/unlink", {"order": b}).status == 400
    assert sup.w.ok("GET", "/api/supporters/claims")["items"] == []


def test_revoking_one_of_two_claims_keeps_the_role_that_the_other_still_justifies(sup):
    sup.configure()
    a, b = sup.order(), sup.order()
    for no in (a, b):
        sup.w.ok("POST", "/api/supporters/link", {"order": no, "user_id": sup.mira["id"]})
    r = sup.w.ok("POST", "/api/supporters/unlink", {"order": a, "remove_role": True})
    assert r["removed_roles"] == [] and sup.supporter in sup.roles_of(sup.mira)


# ---------------------------------------------------------------- simulate

def test_simulating_a_dm_is_side_effect_free(sup):
    sup.configure(require_role_ids=[sup.verified], notify={"user_ids": [sup.owner["id"]]})
    no = sup.order(total_amount="9.00")
    file_before = sup.store.path.read_bytes()
    writes_before = len(sup.fake.writes())
    lookups_before = sup.store.load()["lookups"]
    for _ in range(25):                                                      # far past the daily limit of 10
        r = sup.w.ok("POST", "/api/supporters/simulate", {"text": "my number is %s thanks" % no})
    assert r["reply_key"] == "ok" and r["reply"] == EN["supporter_ok"] and r["assumed"] is True
    assert r["grant"] == [{"id": sup.supporter, "name": "Supporter"}] and r["order"] == mask(no)
    assert "9.00" not in json.dumps(r) and no not in json.dumps(r)
    as_mira = sup.w.ok("POST", "/api/supporters/simulate", {"text": no, "user_id": sup.mira["id"]})
    assert as_mira["assumed"] is False and as_mira["reply_key"] == "ok"
    assert sup.store.path.read_bytes() == file_before and sup.store.load()["lookups"] == lookups_before == {}
    assert len(sup.fake.writes()) == writes_before and sup.roles_of(sup.mira) == [sup.verified]
    assert sup.events("supporter_claim") == [] and sup.store.load()["claims"] == {}


def test_simulation_shows_refusals_and_what_the_owner_would_be_told(sup):
    sup.configure(min_amount="20", notify={"user_ids": [sup.owner["id"]]})
    cheap = sup.order(total_amount="3.00")
    r = sup.w.ok("POST", "/api/supporters/simulate", {"text": cheap})
    assert r["reply_key"] == "below_min" and r["grant"] == [] and mask(cheap) in r["notify"][0]
    assert sup.w.ok("POST", "/api/supporters/simulate", {"text": UNSEEN})["reply_key"] == "not_found"
    none = sup.w.ok("POST", "/api/supporters/simulate", {"text": "hello, nothing to see"})
    assert none["no_order"] is True and none["reply"] == ""
    assert sup.w.call("POST", "/api/supporters/simulate", {"text": cheap, "user_id": "9" * 5}).json["error"] == "not_member"
    assert sup.w.call("POST", "/api/supporters/simulate", {"text": cheap, "user_id": "abc"}).status == 400
    assert sup.fake.writes() == []


def test_simulation_before_setup_says_off(sup):
    r = sup.w.ok("POST", "/api/supporters/simulate", {"text": UNSEEN})
    assert r["reply_key"] == "off"


# ---------------------------------------------------------------- the DM path

def test_a_dm_with_an_order_number_gets_the_template_reply_the_role_and_the_owner_hears(sup):
    sup.go_live()
    no = sup.order(total_amount="66.00")
    sup.dm(sup.mira, "hi! my order number is %s :)" % no)
    assert sup.said(sup.mira) == [EN["supporter_ok"]]                                # a fixed template, no model
    assert sup.supporter in sup.roles_of(sup.mira)
    put = sup.puts()
    assert len(put) == 1 and put[0].reason == "Afdian supporter (SWLink)" and put[0].path.endswith("/roles/" + sup.supporter)
    note = sup.said(sup.owner)
    assert len(note) == 1 and mask(no) in note[0] and "66.00" not in note[0] and no not in note[0]
    assert sup.store.load()["claims"][no]["user_id"] == sup.mira["id"]
    assert sup.app.inbox.conversations() == [] and sup.app.queue.list("pending") == []      # the AI rules never saw it
    ev = [e for e in sup.events() if e["actor"] == "bot:swlink"]
    assert {e["action"] for e in ev} == {"supporter_claim", "supporter_grant"}
    assert all(no not in json.dumps(e) and TOKEN not in json.dumps(e) for e in sup.events())


def test_the_reply_follows_the_templates_language(sup):
    sup.app.supporters.lang = "zh"
    sup.go_live()
    sup.dm(sup.mira, sup.order())
    assert sup.said(sup.mira) == [templates.ZH["supporter_ok"]]
    assert "认领了订单" in sup.said(sup.owner)[0]


def test_other_dms_and_other_places_follow_the_normal_rules(sup):
    sup.go_live()
    no = sup.order()
    sup.dm(sup.mira, "hello, a question without a number")
    assert sup.said(sup.mira) == [] and len(sup.app.inbox.conversations()) == 1       # recorded by the normal path
    guild_msg = Message(id="2", channel_id=sup.fake.add_channel("chat"), author_id=sup.mira["id"], author_name="Mira",
                        content="order %s" % no, guild_id=sup.fake.guild_id)
    sup.app.on_message(guild_msg, sup.fake.bot["id"])
    assert sup.store.load()["claims"] == {} and sup.puts() == []                    # channels never reach the supporter flow
    bot_dm = Message(id="3", channel_id="1", author_id=sup.fake.bot["id"], author_name="bot", content=no, guild_id=None,
                     is_bot=True)
    sup.app.on_message(bot_dm, sup.fake.bot["id"])
    assert sup.store.load()["claims"] == {}


def test_dm_handling_off_or_feature_disabled_leaves_dms_alone(sup):
    sup.configure()                                           # enabled, but dm_handling stays off
    no = sup.order()
    sup.dm(sup.mira, no)
    assert sup.said(sup.mira) == [] and sup.store.load()["claims"] == {} and sup.afdian.calls == []
    sup.w.ok("PUT", "/api/supporters/settings", {"dm_handling": True, "enabled": False})
    sup.dm(sup.mira, no)
    assert sup.said(sup.mira) == [] and sup.store.load()["claims"] == {}


def test_a_second_person_with_the_same_number_is_refused_and_the_owner_is_told(sup):
    sup.go_live()
    no = sup.order()
    sup.dm(sup.mira, no)
    sup.dm(sup.jon, no)
    assert sup.said(sup.jon) == [EN["supporter_claimed_by_other"]] and sup.supporter not in sup.roles_of(sup.jon)
    assert len(sup.said(sup.owner)) == 2 and "already linked to mira" in sup.said(sup.owner)[1]
    sup.dm(sup.jon, no)
    assert len(sup.said(sup.owner)) == 2 and len(sup.said(sup.jon)) == 2          # the owner is not flooded by repeats


def test_the_verification_gate_and_the_daily_limit_answer_without_asking_afdian(sup):
    sup.go_live(require_role_ids=[sup.verified], lookups_per_day=2)
    stranger = sup.fake.add_member("Newbie")
    sup.dm(stranger, sup.order())
    assert sup.said(stranger) == [EN["supporter_need_verify_first"]] and sup.afdian.calls == []
    for i in range(3):
        sup.dm(sup.mira, "2026%024d" % (i + 1))
    assert sup.said(sup.mira) == [EN["supporter_not_found"]] * 2 + [EN["supporter_limit"]]
    assert len(sup.afdian.calls) == 2


def test_not_found_and_below_minimum_are_polite_and_the_owner_hears_about_the_second(sup):
    sup.go_live(min_amount="15")
    cheap = sup.order(total_amount="5.00")
    sup.dm(sup.mira, UNSEEN)
    sup.dm(sup.mira, cheap)
    assert sup.said(sup.mira) == [EN["supporter_not_found"], EN["supporter_below_min"]]
    assert len(sup.said(sup.owner)) == 1 and "below your minimum" in sup.said(sup.owner)[0] and "5.00" not in sup.said(sup.owner)[0]
    assert sup.puts() == []


def test_someone_who_is_not_in_the_server_is_told_so(sup):
    sup.go_live()
    ghost = sup.fake.add_user("Ghost")                       # a user, but not a member
    sup.dm(ghost, sup.order())
    assert sup.said(ghost) == [EN["supporter_not_member"]] and sup.puts() == []


def test_a_role_above_the_bot_gives_the_exact_fix_and_the_person_a_calm_message(sup):
    sup.go_live(role_id=sup.boss)
    no = sup.order()
    sup.dm(sup.mira, no)
    assert sup.said(sup.mira) == [EN["supporter_grant_failed"]] and sup.puts() == []
    note = sup.said(sup.owner)[-1]
    assert "drag the bot's role above Boss" in note and "Server Settings > Roles" in note
    issues = sup.w.ok("GET", "/api/supporters/settings")["issues"]
    assert issues[0]["kind"] == "hierarchy" and issues[0]["role_name"] == "Boss" and issues[0]["bot_role"] == "Hearback"
    assert [e["action"] for e in sup.events() if e["action"] == "supporter_grant_failed"] == ["supporter_grant_failed"]
    sup.fake.roles[sup.boss]["position"] = 5                 # the owner moved the bot's role up (here: Boss down)
    sup.dm(sup.mira, no)                                      # the person tries again: the claim exists, the role is missing
    assert sup.said(sup.mira)[-1] == EN["supporter_ok"] and sup.boss in sup.roles_of(sup.mira)
    assert sup.w.ok("GET", "/api/supporters/settings")["issues"] == []


def test_a_bot_without_manage_roles_is_told_apart_from_a_hierarchy_problem(sup):
    sup.go_live()
    sup.fake.roles[sup.bot_role]["permissions"] = "0"
    sup.dm(sup.mira, sup.order())
    assert "Manage Roles" in sup.said(sup.owner)[-1] and sup.said(sup.mira) == [EN["supporter_grant_failed"]]
    assert sup.w.ok("GET", "/api/supporters/settings")["issues"][0]["kind"] == "no_permission"


def test_a_discord_error_during_the_grant_is_scrubbed_and_reported(sup):
    sup.go_live()
    sup.fake.fail_next(r"PUT /guilds/\d+/members/\d+/roles/\d+", 500, {"message": "boom %s" % TOKEN})
    sup.dm(sup.mira, sup.order())
    assert sup.said(sup.mira) == [EN["supporter_grant_failed"]]
    assert TOKEN not in "\n".join(sup.said(sup.owner)) and TOKEN not in json.dumps(sup.events())
    assert sup.w.ok("GET", "/api/supporters/settings")["issues"] == []                  # not a configuration problem


def test_the_owner_gets_notices_in_a_channel_when_one_is_set_without_pings(sup):
    sup.go_live()
    chan = sup.fake.add_channel("owner-notes")
    sup.w.ok("PUT", "/api/supporters/settings", {"notify": {"channel_id": chan, "user_ids": [sup.owner["id"]]}})
    sup.dm(sup.mira, sup.order())
    assert len(sup.fake.messages[chan]) == 1 and sup.said(sup.owner) == []
    posts = [c for c in sup.fake.calls if c.method == "POST" and c.path.endswith("/messages")]
    assert all(c.body["allowed_mentions"] == {"parse": []} for c in posts)


def test_without_any_notify_target_nothing_is_sent_to_the_owner_and_nothing_breaks(sup):
    sup.configure(dm_handling=True)
    sup.dm(sup.mira, sup.order())
    assert sup.said(sup.mira) == [EN["supporter_ok"]]
    assert not [c for c in sup.fake.calls if c.path == "/users/@me/channels"]


def test_a_discord_failure_in_the_dm_path_is_logged_not_raised(sup):
    sup.go_live()
    sup.fake.fail_next(r"GET /guilds/\d+/members/\d+", 500, {"message": "down"})
    sup.dm(sup.mira, sup.order())                              # must not raise
    assert [e["action"] for e in sup.events("supporter_error")] == ["supporter_error"] and sup.said(sup.mira) == []


def test_a_damaged_file_is_logged_and_the_dm_falls_back_to_the_normal_rules(sup):
    sup.go_live()
    sup.store.path.write_text("{oops", encoding="utf-8")
    sup.dm(sup.mira, "any text")
    assert sup.events("supporter_error") == []                # no order number: the supporter flow is not even asked
    sup.dm(sup.mira, sup.order())
    assert [e["action"] for e in sup.events("supporter_error")] == ["supporter_error"]
    assert sup.said(sup.mira) == [] and len(sup.app.inbox.conversations()) == 1       # it fell through to the normal rules
    assert "damaged" in sup.events("supporter_error")[0]["detail"]["error"]


# ---------------------------------------------------------------- the retry loop

def test_afdian_down_queues_the_number_and_the_loop_finishes_the_job_later(sup):
    sup.go_live()
    no = sup.order()
    sup.afdian.fail_next(urllib.error.URLError("down"))
    sup.dm(sup.mira, no)
    assert sup.said(sup.mira) == [EN["supporter_retry_later"]] and sup.puts() == []
    assert "retried every 10 minutes" in sup.said(sup.owner)[0]
    assert sup.w.ok("GET", "/api/status")["supporters"] == {"claims": 0, "retries": 1}
    sup.afdian.fail_next(urllib.error.URLError("still down"))
    sup.clock.advance(600)
    assert sup.app.supporter_bot.retry_tick() == 1
    assert sup.said(sup.mira) == [EN["supporter_retry_later"]]                       # still quiet toward the person
    sup.clock.advance(600)
    assert sup.app.supporter_bot.retry_tick() == 1
    assert sup.said(sup.mira) == [EN["supporter_retry_later"], EN["supporter_ok"]]
    assert sup.supporter in sup.roles_of(sup.mira) and sup.store.load()["retry"] == []
    assert sup.app.supporter_bot.retry_tick() == 0


def test_the_loop_gives_up_after_three_days_and_says_so(sup):
    sup.go_live()
    no = sup.order()
    sup.afdian.fail_next(urllib.error.URLError("down"))
    sup.dm(sup.mira, no)
    for _ in range(4):
        sup.afdian.fail_next(urllib.error.URLError("down"))
    sup.clock.advance(2 * 86400)
    sup.app.supporter_bot.retry_tick()
    assert sup.said(sup.mira) == [EN["supporter_retry_later"]]
    sup.clock.advance(86400 + 600)
    sup.app.supporter_bot.retry_tick()
    assert sup.said(sup.mira)[-1] == EN["supporter_gave_up"] and sup.store.load()["retry"] == []
    assert "by hand" in sup.said(sup.owner)[-1] and sup.puts() == []
    assert sup.app.supporter_bot.retry_tick() == 0


def test_a_person_who_left_before_the_retry_is_dropped_with_a_note(sup):
    sup.go_live()
    no = sup.order()
    sup.afdian.fail_next(urllib.error.URLError("down"))
    sup.dm(sup.mira, no)
    del sup.fake.members[sup.mira["id"]]
    sup.app.supporter_bot.retry_tick()
    assert sup.store.load()["retry"] == [] and "left the server" in sup.said(sup.owner)[-1]


def test_a_queued_person_who_lost_the_required_role_is_told_once_and_dropped(sup):
    sup.go_live(require_role_ids=[sup.verified])
    sup.afdian.fail_next(urllib.error.URLError("down"))
    sup.dm(sup.mira, sup.order())
    sup.fake.members[sup.mira["id"]]["roles"].remove(sup.verified)
    sup.app.supporter_bot.retry_tick()
    assert sup.said(sup.mira)[-1] == EN["supporter_need_verify_first"] and sup.store.load()["retry"] == []
    assert sup.app.supporter_bot.retry_tick() == 0 and len(sup.said(sup.mira)) == 2


def test_the_loop_needs_no_gateway_and_does_nothing_when_disabled(sup):
    sup.go_live()
    sup.afdian.fail_next(urllib.error.URLError("down"))
    sup.dm(sup.mira, sup.order())
    assert sup.app.runtime.online is False
    sup.w.ok("PUT", "/api/supporters/settings", {"enabled": False, "dm_handling": False})
    assert sup.app.supporter_bot.retry_tick() == 0 and len(sup.store.load()["retry"]) == 1


def test_the_background_thread_runs_the_check_on_its_own_and_stops(sup):
    sup.go_live()
    sup.afdian.fail_next(urllib.error.URLError("down"))
    sup.dm(sup.mira, sup.order())
    bot = sup.app.supporter_bot
    bot.start(interval=0.02)
    bot.start(interval=0.02)                                   # a second start does not add a second thread
    deadline = time.time() + 5
    while time.time() < deadline and sup.supporter not in sup.roles_of(sup.mira):
        time.sleep(0.02)
    bot.stop()
    assert sup.supporter in sup.roles_of(sup.mira) and bot._thread is not None and not bot._thread.is_alive()


def test_the_loop_survives_an_unexpected_error(sup):
    sup.go_live()
    bot = sup.app.supporter_bot
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("bug %s" % TOKEN)
    bot.retry_tick = boom                                       # type: ignore[method-assign]
    bot.start(interval=0.01)
    deadline = time.time() + 5
    while time.time() < deadline and len(calls) < 3:
        time.sleep(0.01)
    bot.stop()
    assert len(calls) >= 3 and TOKEN not in json.dumps(sup.events())


# ---------------------------------------------------------------- logs, dashboard, secrets

def test_supporter_events_show_up_on_the_logs_page_without_secrets(sup):
    sup.go_live()
    no = sup.order()
    sup.dm(sup.mira, no)
    sup.w.ok("POST", "/api/supporters/unlink", {"order": no})
    log = sup.w.ok("GET", "/api/logs?limit=200")["items"]
    actions = {(e["actor"], e["action"]) for e in log if e["action"].startswith("supporter")}
    assert {("bot:swlink", "supporter_claim"), ("bot:swlink", "supporter_grant"), ("human:web", "supporter_unlink")} <= actions
    raw = json.dumps(log, ensure_ascii=False)
    assert no not in raw and TOKEN not in raw and mask(no) in raw


def test_the_dashboard_card_appears_only_when_the_feature_is_on(sup):
    assert sup.w.ok("GET", "/api/status")["supporters"] is None
    sup.configure()
    assert sup.w.ok("GET", "/api/status")["supporters"] == {"claims": 0, "retries": 0}
    sup.w.ok("PUT", "/api/supporters/settings", {"enabled": False})
    assert sup.w.ok("GET", "/api/status")["supporters"] is None


def test_no_response_or_file_anywhere_holds_the_afdian_token(sup, tmp_path):
    sup.go_live()
    no = sup.order()
    sup.dm(sup.mira, no)
    sup.afdian.fail_next(urllib.error.URLError("x %s" % TOKEN))
    sup.w.call("GET", "/api/supporters/orders")
    sup.w.call("POST", "/api/supporters/test", {})
    for path in ("/api/supporters/settings", "/api/supporters/claims", "/api/status", "/api/logs?limit=500"):
        sup.w.ok("GET", path)
    assert all(TOKEN.encode() not in b for b in sup.w.bodies)
    on_disk = "\n".join(p.read_bytes().decode("utf-8", "replace") for p in tmp_path.rglob("*") if p.is_file())
    assert TOKEN not in on_disk


# ---------------------------------------------------------------- the page

def _texts():
    src = (web.STATIC_DIR / "i18n.js").read_text(encoding="utf-8")
    zh, en = src.split("\n  en: {")
    return {lang: dict(re.findall(r'"([\w.\-]+)": "((?:[^"\\]|\\.)*)"', block)) for lang, block in (("zh", zh), ("en", en))}


def test_the_supporters_page_copy_is_the_agreed_wording():
    zh = _texts()["zh"]
    assert zh["nav.supporters"] == "支持者"
    assert zh["sub.supporters"] == "支持者在爱发电付款后，私信机器人订单号，自动拿到支持者身份组。金额不会公开。"
    assert "afdian.com/dashboard/dev" in zh["howto.supporters.1"] and "user_id" in zh["howto.supporters.1"] and "API Token" in zh["howto.supporters.1"]
    assert "「模拟私信」" in zh["howto.supporters.3"] and "「私信核对」" in zh["howto.supporters.3"]
    assert zh["sup.dmWarn"] == "如果同一个机器人还被别的程序用来处理订单号，不要同时打开，否则会回两次"
    assert zh["sup.showAmounts"] == "显示金额" and zh["sup.claimed"] == "已认领" and zh["sup.unclaimed"] == "未认领"
    assert zh["sup.claimTo"] == "认领给…" and zh["sup.revoke"] == "撤销" and zh["sup.require"].startswith("先要有这些身份组之一")
    assert zh["sup.sim"] == "模拟私信" and zh["sup.dm"] == "私信核对"


def test_the_page_is_in_the_menu_after_bot_messages_and_wired_to_the_api():
    js = (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert '"botmsgs", "supporters"]' in js and "supporters: pageSupporters" in js
    for route in ("/api/supporters/settings", "/api/supporters/token", "/api/supporters/test", "/api/supporters/orders",
                  "/api/supporters/claims", "/api/supporters/link", "/api/supporters/unlink", "/api/supporters/simulate",
                  "/roles", "/members?q="):
        assert route in js, route
    assert "amounts=1" in js and "s.supporters" in js
    assert "innerHTML" not in js and "alert(" not in js
    assert 'type: "password"' in js and "tok.value = \"\"" in js                  # the token field is write-only and cleared
