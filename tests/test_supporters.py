"""SWLink supporters: the Afdian client, the settings store, the decision logic, link/unlink, remark matching, texts.

Everything is offline: Afdian is `hearback.fake.FakeAfdian`, the keyring is a dict.
"""
from __future__ import annotations

import hashlib
import json
import urllib.error

import pytest

from hearback import supporters as sp
from hearback import templates
from hearback.fake import FakeAfdian
from hearback.ledger import Ledger
from hearback.llm import scrub
from hearback.supporters import (AfdianClient, Person, SupporterError, Supporters, SupporterStore, mask, match_remarks)

ROLE, PLAN_ROLE, VERIFIED = "7001", "7002", "7003"
TOKEN = "afd-SECRET-token-0123456789"
UNSEEN = "20269999000000000000000001"          # an order number Afdian does not know


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, pw):
        self.store[(service, user)] = pw


class Clock:
    def __init__(self, t: float = 1_790_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Rig:
    """A configured supporter service over a fake Afdian."""

    def __init__(self, tmp_path, **settings):
        self.clock = Clock()
        self.afdian = FakeAfdian(token=TOKEN, clock=self.clock)
        self.keyring = FakeKeyring()
        self.store = SupporterStore(tmp_path / ".hearback", environ={}, keyring_mod=self.keyring)
        self.ledger = Ledger(tmp_path / ".hearback" / "ledger.json")
        self.sup = Supporters(self.store, ledger=self.ledger, clock=self.clock, lang="en", opener=self.afdian)
        self.store.set_token(TOKEN)
        self.store.update({"enabled": True, "afdian_user_id": self.afdian.user_id, "role_id": ROLE, **settings})

    def person(self, uid="501", name="mira", roles=()):
        return Person(id=uid, name=name, display=name.title(), role_ids=set(roles))

    def order(self, **kw):
        return self.afdian.add_order(**kw)["out_trade_no"]

    def cfg(self):
        return self.store.load()

    def events(self, action=None):
        return [e for e in self.ledger.read_events() if action is None or e["action"] == action]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ---------------------------------------------------------------- the Afdian client

def test_the_signature_matches_the_example_in_afdian_s_developer_guide():
    # the worked example from https://guide.afdian.com/creator/developer
    assert sp.sign("123", '{"a":333}', 1624339905, "abc") == "a4acc28b81598b7e5d84ebdc3e91710c"
    assert hashlib.md5(b'123params{"a":333}ts1624339905user_idabc').hexdigest() == "a4acc28b81598b7e5d84ebdc3e91710c"


def test_the_request_body_is_user_id_params_ts_sign_and_never_carries_the_token():
    c = AfdianClient("abc", "123", clock=lambda: 1624339905.9)
    body = c.body({"a": 333})
    assert body == {"user_id": "abc", "params": '{"a":333}', "ts": 1624339905, "sign": "a4acc28b81598b7e5d84ebdc3e91710c"}
    assert "123" not in json.dumps({k: v for k, v in body.items() if k != "ts"})
    assert "123" not in repr(c)


def test_calls_go_to_afdian_only_with_a_json_post_and_a_timeout(rig):
    seen = []
    inner = rig.afdian

    def spy(req, timeout=None):
        seen.append((req.full_url, req.get_method(), dict(req.header_items()), timeout))
        return inner(req, timeout)

    c = AfdianClient(inner.user_id, TOKEN, opener=spy, clock=rig.clock)
    c.ping()
    assert c.find_order(UNSEEN) is None
    assert [s[0] for s in seen] == ["https://afdian.com/api/open/ping", "https://afdian.com/api/open/query-order"]
    assert all(s[1] == "POST" and s[2]["Content-type"] == "application/json" and s[3] == 20 for s in seen)
    assert all(u.startswith("https://afdian.com/") for u in inner.urls)
    with pytest.raises(SupporterError, match="unknown Afdian endpoint"):
        c.call("../../elsewhere", {})
    assert len(seen) == 2


def test_find_order_returns_the_exact_order_and_recent_orders_page_through_the_list(rig):
    nos = [rig.order(total_amount="1.00") for _ in range(60)]
    c = rig.sup.make_client()
    assert c.find_order(nos[7])["out_trade_no"] == nos[7]
    assert c.find_order(UNSEEN) is None
    first = c.recent_orders(pages=1)
    assert [o["out_trade_no"] for o in first] == nos[::-1][:50]          # newest first, 50 per page
    assert len(c.recent_orders(pages=2)) == 60 and len(c.recent_orders(pages=9)) == 60      # stops at the last page
    assert len(c.recent_orders(pages=1, per_page=10)) == 10              # per_page only trims: the API cannot resize
    assert all("per_page" not in p for _n, p in rig.afdian.calls)


def test_comma_separated_numbers_are_a_documented_afdian_feature_the_fake_serves(rig):
    a, b = rig.order(), rig.order()
    data = rig.sup.make_client().call("query-order", {"page": 1, "out_trade_no": "%s,%s" % (a, b)})
    assert {o["out_trade_no"] for o in data["list"]} == {a, b}


def test_the_token_never_appears_in_an_error_message(rig):
    for fault in (urllib.error.URLError("refused by %s" % TOKEN), OSError("boom token=%s" % TOKEN),
                  TimeoutError("timed out %s" % TOKEN)):
        rig.afdian.fail_next(fault)
        with pytest.raises(SupporterError) as e:
            rig.sup.make_client().ping()
        assert TOKEN not in str(e.value) and "could not reach Afdian" in str(e.value)
    rig.afdian.fail_next({"ec": 500, "em": "oops %s" % TOKEN, "data": {}})
    with pytest.raises(SupporterError) as e:
        rig.sup.make_client().ping()
    assert TOKEN not in str(e.value) and scrub(str(e.value)) == str(e.value)
    rig.afdian.fail_next(urllib.error.HTTPError("https://afdian.com/api/open/ping", 502, "bad", {}, None))
    with pytest.raises(SupporterError, match="HTTP 502"):
        rig.sup.make_client().ping()


def test_afdian_error_codes_get_plain_explanations(rig):
    wrong_token = AfdianClient(rig.afdian.user_id, "not-the-token", opener=rig.afdian, clock=rig.clock)
    with pytest.raises(SupporterError, match="rejected the signature"):
        wrong_token.ping()
    wrong_user = AfdianClient("someone-else", TOKEN, opener=rig.afdian, clock=rig.clock)
    with pytest.raises(SupporterError, match="does not know this user_id"):
        wrong_user.ping()
    skewed = AfdianClient(rig.afdian.user_id, TOKEN, opener=rig.afdian, clock=lambda: rig.clock() + 7200)
    with pytest.raises(SupporterError, match="clock"):
        skewed.ping()
    rig.afdian.fail_next({"ec": 400003, "em": "x", "data": {}})
    with pytest.raises(SupporterError, match="could not read"):
        rig.sup.make_client().ping()


def test_garbage_answers_are_errors_not_crashes(rig):
    class Junk:
        def __init__(self, raw):
            self.raw = raw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.raw

    for raw in (b"<html>", b"[1, 2]", b'{"ec": 200, "data": [1]}'):
        c = AfdianClient("u", TOKEN, opener=lambda req, timeout=None, raw=raw: Junk(raw), clock=rig.clock)
        if raw.startswith(b'{"ec"'):
            assert c.call("ping", {}) == {}
        else:
            with pytest.raises(SupporterError):
                c.ping()


# ---------------------------------------------------------------- the store

def test_defaults_when_there_is_no_file(tmp_path):
    st = SupporterStore(tmp_path, environ={}, keyring_mod=None)
    d = st.load()
    assert d["enabled"] is False and d["dm_handling"] is False and d["lookups_per_day"] == 10
    assert d["min_amount"] == "0" and d["plan_roles"] == {} and d["require_role_ids"] == []
    assert d["notify"] == {"channel_id": "", "user_ids": []} and d["claims"] == {} and d["retry"] == [] and d["lookups"] == {}
    assert not (tmp_path / "supporters.json").exists()


@pytest.mark.parametrize("content", ["{not json", "[1]", '{"claims": []}', '{"retry": [{"user": "1"}]}',
                                     '{"lookups_per_day": "lots"}', '{"role_id": "abc"}'])
def test_a_damaged_file_is_an_error_not_a_silent_reset(tmp_path, content):
    (tmp_path / "supporters.json").write_text(content, encoding="utf-8")
    st = SupporterStore(tmp_path, environ={}, keyring_mod=None)
    with pytest.raises(SupporterError, match="supporters"):
        st.load()
    assert (tmp_path / "supporters.json").read_text(encoding="utf-8") == content          # untouched


def test_settings_are_validated_and_unknown_keys_refused(tmp_path):
    st = SupporterStore(tmp_path, environ={}, keyring_mod=None)
    out = st.update({"role_id": " 7001 ", "min_amount": "5.50", "require_role_ids": "7003, 7004 7003",
                     "plan_roles": {"plan-a": "7002"}, "lookups_per_day": "3", "afdian_user_id": "abc_DEF-1",
                     "notify": {"channel_id": "123", "user_ids": ["8", "9"]}, "enabled": True, "dm_handling": True})
    assert out["role_id"] == "7001" and out["min_amount"] == "5.5" and out["require_role_ids"] == ["7003", "7004"]
    assert out["lookups_per_day"] == 3 and out["notify"] == {"channel_id": "123", "user_ids": ["8", "9"]}
    assert st.load()["plan_roles"] == {"plan-a": "7002"}
    for bad in ({"nope": 1}, {"role_id": "role"}, {"min_amount": "-1"}, {"min_amount": "NaN"}, {"min_amount": "x"},
                {"lookups_per_day": 0}, {"lookups_per_day": 999}, {"enabled": "yes"}, {"require_role_ids": [1, "x"]},
                {"plan_roles": {"": "1"}}, {"plan_roles": {"a b": "1"}}, {"plan_roles": {"a": "x"}},
                {"notify": {"channel_id": "x"}}, {"notify": {"other": 1}}, {"afdian_user_id": "bad id"}):
        with pytest.raises(SupporterError):
            st.update(bad)
    assert st.load()["role_id"] == "7001"                                # a refused update changes nothing


def test_min_amount_is_stored_as_plain_text_of_the_number(tmp_path):
    st = SupporterStore(tmp_path, environ={}, keyring_mod=None)
    assert st.update({"min_amount": "100"})["min_amount"] == "100"
    assert st.update({"min_amount": "0.00"})["min_amount"] == "0"
    assert st.update({"min_amount": ""})["min_amount"] == "0"


def test_the_token_lives_in_the_keyring_or_the_environment_never_in_the_file(tmp_path):
    kr = FakeKeyring()
    st = SupporterStore(tmp_path, environ={}, keyring_mod=kr)
    assert not st.has_token()
    st.set_token(TOKEN)
    assert kr.store == {("hearback-swlink", "afdian-token"): TOKEN} and st.get_token() == TOKEN and st.has_token()
    st.update({"afdian_user_id": "abc"})
    st.save({**st.load(), "claims": {UNSEEN: {"user_id": "1"}}})
    assert TOKEN not in (tmp_path / "supporters.json").read_text(encoding="utf-8")
    env_only = SupporterStore(tmp_path, environ={"HEARBACK_AFDIAN_TOKEN": " env-token-0123456789 "}, keyring_mod=None)
    assert env_only.get_token() == "env-token-0123456789" and env_only.has_token() and not env_only.keyring_available()
    with pytest.raises(SupporterError, match="HEARBACK_AFDIAN_TOKEN"):
        env_only.set_token("abcdef")
    for bad in ("", "has space", "x" * 300):
        with pytest.raises(SupporterError):
            st.set_token(bad)


def test_a_refusing_keyring_gets_the_environment_variable_hint(tmp_path):
    class Refuses(FakeKeyring):
        def set_password(self, *a):
            raise RuntimeError("locked %s" % TOKEN)

    st = SupporterStore(tmp_path, environ={}, keyring_mod=Refuses())
    with pytest.raises(SupporterError) as e:
        st.set_token(TOKEN)
    assert "HEARBACK_AFDIAN_TOKEN" in str(e.value) and TOKEN not in str(e.value)


def test_public_view_has_no_claims_and_no_token(rig):
    rig.order()
    pub = rig.store.public(rig.cfg())
    assert set(pub) == set(sp.SETTING_KEYS) and TOKEN not in json.dumps(pub)


# ---------------------------------------------------------------- order numbers

def test_order_numbers_are_16_to_40_digits_found_in_free_text():
    n27 = "202610240000000000000000012"
    assert sp.extract_order_numbers("my order is %s, thanks" % n27) == [n27]
    assert sp.extract_order_numbers("订单号：%s" % n27) == [n27]
    assert sp.extract_order_numbers("a %s b %s c %s" % (n27, n27, "1" * 16)) == [n27, "1" * 16]
    assert sp.extract_order_numbers("1" * 15) == [] and sp.extract_order_numbers("1" * 41) == []
    assert sp.extract_order_numbers("12 345 678 and no order") == [] and sp.extract_order_numbers("") == []


def test_masking_keeps_the_first_and_last_four_digits():
    assert mask("202610240000000000000000012") == "2026…0012"
    assert mask("12345") == "…"


# ---------------------------------------------------------------- the decision

def test_a_paid_order_grants_the_role_and_records_the_claim(rig):
    no = rig.order(total_amount="12.50", plan_id="plan-a")
    out = rig.sup.handle_order(rig.person(), no)
    assert out.reply_key == "ok" and out.grant_role_ids == [ROLE] and out.order == mask(no)
    claim = rig.cfg()["claims"][no]
    assert claim["user_id"] == "501" and claim["user_name"] == "mira" and claim["plan_id"] == "plan-a"
    assert claim["amount"] == "12.5" and claim["afdian_user"].startswith("afdian-user-") and claim["at"].endswith("Z")
    assert set(claim) == {"user_id", "user_name", "at", "plan_id", "amount", "afdian_user"}
    assert len(out.notify_texts) == 1 and no not in out.notify_texts[0] and mask(no) in out.notify_texts[0]
    assert "12.5" not in out.notify_texts[0] and "mira" in out.notify_texts[0].lower()


def test_a_person_who_already_has_the_role_gets_the_again_reply(rig):
    no = rig.order()
    out = rig.sup.handle_order(rig.person(roles=[ROLE]), no)
    assert out.reply_key == "ok_again" and out.grant_role_ids == [] and rig.cfg()["claims"][no]["user_id"] == "501"


def test_sending_the_same_order_again_regrants_only_a_missing_role(rig):
    no = rig.order()
    rig.sup.handle_order(rig.person(), no)
    lookups = len(rig.afdian.calls)
    again = rig.sup.handle_order(rig.person(roles=[ROLE]), no)
    assert again.reply_key == "ok_again" and again.grant_role_ids == [] and again.notify_texts == []
    lost = rig.sup.handle_order(rig.person(roles=[]), no)                   # the role was taken away meanwhile
    assert lost.reply_key == "ok" and lost.grant_role_ids == [ROLE]
    assert len(rig.afdian.calls) == lookups                                # claimed orders are not looked up again


def test_an_order_claimed_by_someone_else_is_refused_and_the_owner_hears_without_the_amount(rig):
    no = rig.order(total_amount="88.00")
    rig.sup.handle_order(rig.person("501", "mira"), no)
    out = rig.sup.handle_order(rig.person("502", "jon"), no)
    assert out.reply_key == "claimed_by_other" and out.grant_role_ids == []
    note = out.notify_texts[0]
    assert "jon" in note.lower() and "mira" in note and mask(no) in note and no not in note and "88" not in note
    assert rig.cfg()["claims"][no]["user_id"] == "501"
    assert rig.sup.handle_order(rig.person("502", "jon"), no).notify_texts == []      # one notice a day per try
    assert rig.sup.handle_order(rig.person("503", "kim"), no).notify_texts != []


def test_require_roles_gate_comes_first(tmp_path):
    rig = Rig(tmp_path, require_role_ids=[VERIFIED, "7004"])
    no = rig.order()
    out = rig.sup.handle_order(rig.person(roles=[]), no)
    assert out.reply_key == "need_verify_first" and out.grant_role_ids == [] and rig.afdian.calls == []
    assert rig.cfg()["lookups"] == {}                                      # it cost no lookup
    assert rig.sup.handle_order(rig.person(roles=["7004"]), no).reply_key == "ok"


def test_unknown_and_unpaid_orders_are_polite_not_found(rig):
    unpaid = rig.order(status=1)
    for no in (UNSEEN, unpaid):
        out = rig.sup.handle_order(rig.person(), no)
        assert out.reply_key == "not_found" and out.grant_role_ids == [] and out.notify_texts == []
    assert rig.cfg()["claims"] == {}


def test_below_the_minimum_is_a_polite_refusal_with_a_notice(tmp_path):
    rig = Rig(tmp_path, min_amount="10")
    cheap, exact = rig.order(total_amount="9.99"), rig.order(total_amount="10.00")
    out = rig.sup.handle_order(rig.person(), cheap)
    assert out.reply_key == "below_min" and out.grant_role_ids == [] and rig.cfg()["claims"] == {}
    assert mask(cheap) in out.notify_texts[0] and "9.99" not in out.notify_texts[0] and "10" not in out.notify_texts[0].replace(mask(cheap), "")
    assert rig.sup.handle_order(rig.person(), exact).reply_key == "ok"


def test_plan_roles_add_the_plan_s_role_to_the_general_one(tmp_path):
    rig = Rig(tmp_path, plan_roles={"plan-gold": PLAN_ROLE})
    gold, plain = rig.order(plan_id="plan-gold"), rig.order(plan_id="other")
    out = rig.sup.handle_order(rig.person("501"), gold)
    assert out.grant_role_ids == [ROLE, PLAN_ROLE]
    assert rig.sup.handle_order(rig.person("502", "jon"), plain).grant_role_ids == [ROLE]
    assert rig.sup.handle_order(rig.person("501", roles=[ROLE]), gold).grant_role_ids == [PLAN_ROLE]


def test_the_daily_lookup_limit_counts_lookups_not_claimed_orders(tmp_path):
    rig = Rig(tmp_path, lookups_per_day=3)
    me = rig.person()
    for i in range(3):
        assert rig.sup.handle_order(me, "2026%024d" % (i + 1)).reply_key == "not_found"
    out = rig.sup.handle_order(me, "2026%024d" % 4)
    assert out.reply_key == "limit" and out.notify_texts == []
    assert len(rig.afdian.calls) == 3
    assert rig.sup.handle_order(rig.person("502", "jon"), UNSEEN).reply_key == "not_found"      # per person
    rig.clock.advance(86400 + 1)
    assert rig.sup.handle_order(me, UNSEEN).reply_key == "not_found"                           # a day later


def test_old_lookup_stamps_are_dropped_when_the_file_is_written(tmp_path):
    rig = Rig(tmp_path)
    rig.sup.handle_order(rig.person("502", "jon"), UNSEEN)
    rig.clock.advance(3 * 86400)
    rig.sup.handle_order(rig.person("501"), UNSEEN)
    assert list(rig.cfg()["lookups"]) == ["501"]


def test_not_set_up_yet_answers_off_and_tells_the_owner_once(tmp_path):
    clock = Clock()
    st = SupporterStore(tmp_path, environ={}, keyring_mod=None)
    sup = Supporters(st, clock=clock, lang="en")
    me = Person(id="501", name="mira", display="Mira")
    out = sup.handle_order(me, UNSEEN)
    assert out.reply_key == "off" and mask(UNSEEN) in out.notify_texts[0]
    assert sup.handle_order(me, UNSEEN).notify_texts == []
    st.update({"afdian_user_id": "abc", "role_id": ROLE})                  # still no token
    assert sup.handle_order(Person(id="9"), UNSEEN).reply_key == "off"


def test_a_dry_run_changes_nothing_at_all(rig):
    no = rig.order()
    rig.sup.handle_order(rig.person("501"), rig.order())                    # some real state
    before = rig.store.path.read_bytes()
    events = len(rig.events())
    out = rig.sup.handle_order(rig.person("502", "jon"), no, dry_run=True)
    assert out.reply_key == "ok" and out.grant_role_ids == [ROLE]
    rig.afdian.fail_next(urllib.error.URLError("down"))
    assert rig.sup.handle_order(rig.person("502", "jon"), no, dry_run=True).reply_key == "retry_later"
    assert rig.sup.handle_order(rig.person("502", "jon"), UNSEEN, dry_run=True).reply_key == "not_found"
    assert rig.store.path.read_bytes() == before and len(rig.events()) == events
    # a dry run does not use up the daily allowance either
    for _ in range(30):
        assert rig.sup.handle_order(rig.person("503", "kim"), UNSEEN, dry_run=True).reply_key == "not_found"


def test_simulate_returns_what_the_bot_would_say(rig):
    no = rig.order()
    sim = rig.sup.simulate("here: %s thanks" % no, rig.person("502", "jon"))
    assert sim["reply_key"] == "ok" and sim["reply"] == templates.render("supporter_ok", "en")
    assert sim["grant_role_ids"] == [ROLE] and sim["order"] == mask(no) and rig.cfg()["claims"] == {}
    assert rig.sup.simulate("hello, no number here", rig.person()) is None


# ---------------------------------------------------------------- failures and the retry queue

def test_afdian_down_queues_a_retry_and_a_quiet_second_try(rig):
    no = rig.order()
    rig.afdian.fail_next(urllib.error.URLError("down"))
    out = rig.sup.handle_order(rig.person(), no)
    assert out.reply_key == "retry_later" and out.grant_role_ids == []
    assert mask(no) in out.notify_texts[0] and no not in out.notify_texts[0] and "down" in out.notify_texts[0]
    assert rig.cfg()["retry"] == [{"user": "501", "order": no, "since": rig.clock()}]
    rig.afdian.fail_next(urllib.error.URLError("still down"))
    again = rig.sup.handle_order(rig.person(), no)                         # the person asks again meanwhile
    assert again.reply_key == "retry_later" and again.notify_texts == [] and len(rig.cfg()["retry"]) == 1
    rig.afdian.fail_next(urllib.error.URLError("still down"))
    quiet = rig.sup.handle_order(rig.person(), no, retry=True)             # the background re-check says nothing
    assert quiet.reply_key == "" and quiet.notify_texts == [] and len(rig.cfg()["retry"]) == 1


def test_a_successful_retry_claims_the_order_and_clears_the_queue(rig):
    no = rig.order()
    rig.afdian.fail_next(urllib.error.URLError("down"))
    rig.sup.handle_order(rig.person(), no)
    rig.clock.advance(600)
    out = rig.sup.handle_order(rig.person(), no, retry=True)
    assert out.reply_key == "ok" and out.grant_role_ids == [ROLE]
    assert rig.cfg()["retry"] == [] and rig.cfg()["claims"][no]["user_id"] == "501"
    assert rig.sup.retry_items() == []


def test_retries_do_not_use_the_daily_allowance(tmp_path):
    rig = Rig(tmp_path, lookups_per_day=1)
    no = rig.order()
    rig.afdian.fail_next(urllib.error.URLError("down"))
    rig.sup.handle_order(rig.person(), no)
    assert rig.sup.handle_order(rig.person(), no, retry=True).reply_key == "ok"


def test_after_three_days_it_gives_up_and_tells_the_owner(rig):
    no = rig.order()
    rig.afdian.fail_next(urllib.error.URLError("down"))
    rig.sup.handle_order(rig.person(), no)
    rig.clock.advance(3 * 86400 - 60)
    rig.afdian.fail_next(urllib.error.URLError("down"))
    assert rig.sup.handle_order(rig.person(), no, retry=True).reply_key == ""      # not yet
    rig.clock.advance(120)
    rig.afdian.fail_next(urllib.error.URLError("down"))
    out = rig.sup.handle_order(rig.person(), no, retry=True)
    assert out.reply_key == "gave_up" and mask(no) in out.notify_texts[0] and no not in out.notify_texts[0]
    assert rig.cfg()["retry"] == [] and rig.cfg()["claims"] == {}
    assert [e["action"] for e in rig.events() if e["action"].startswith("supporter_")] == [
        "supporter_retry_queued", "supporter_gave_up"]


def test_a_found_but_unpaid_order_after_a_retry_leaves_the_queue(rig):
    no = rig.order(status=3)
    rig.afdian.fail_next(urllib.error.URLError("down"))
    rig.sup.handle_order(rig.person(), no)
    assert rig.sup.handle_order(rig.person(), no, retry=True).reply_key == "not_found"
    assert rig.cfg()["retry"] == []


# ---------------------------------------------------------------- the log

def test_events_use_the_bot_actor_and_never_hold_a_full_order_number(rig):
    no = rig.order()
    rig.sup.handle_order(rig.person(), no)
    ev = rig.events("supporter_claim")
    assert len(ev) == 1 and ev[0]["actor"] == "bot:swlink" and ev[0]["detail"]["order"] == mask(no)
    raw = (rig.ledger.dir / "events.jsonl").read_text(encoding="utf-8")
    assert no not in raw and TOKEN not in raw


# ---------------------------------------------------------------- link / unlink

def test_link_and_unlink_by_the_owner_are_logged_as_human_web(rig):
    no = rig.order(total_amount="3.00", plan_id="plan-a")
    claim = rig.sup.link_order(no, "501", sp.ACTOR_WEB, user_name="mira", order=rig.afdian.orders[0])
    assert claim["user_id"] == "501" and claim["plan_id"] == "plan-a" and claim["amount"] == "3"
    assert rig.cfg()["claims"][no] == claim
    assert rig.sup.link_order(no, "501", sp.ACTOR_WEB)["plan_id"] == "plan-a"      # linking again keeps what it knew
    assert rig.sup.handle_order(rig.person("501", roles=[ROLE]), no).reply_key == "ok_again"
    removed = rig.sup.unlink(no, sp.ACTOR_WEB)
    assert removed["user_id"] == "501" and removed["order"] == no and rig.cfg()["claims"] == {}
    ev = [e for e in rig.events() if e["action"] in ("supporter_link", "supporter_unlink")]
    assert [(e["actor"], e["action"], e["detail"]) for e in ev] == [
        ("human:web", "supporter_link", {"order": mask(no), "user": "501"}),
        ("human:web", "supporter_link", {"order": mask(no), "user": "501"}),
        ("human:web", "supporter_unlink", {"order": mask(no), "user": "501"})]


def test_linking_an_order_someone_else_holds_is_refused_and_bad_input_too(rig):
    no = rig.order()
    rig.sup.handle_order(rig.person("501", "mira"), no)
    with pytest.raises(SupporterError, match="already linked to mira"):
        rig.sup.link_order(no, "502", sp.ACTOR_WEB)
    for args in (("12345", "501"), (no, "mira"), (no, "")):
        with pytest.raises(SupporterError):
            rig.sup.link_order(*args, sp.ACTOR_WEB)
    with pytest.raises(SupporterError, match="not linked"):
        rig.sup.unlink(UNSEEN, sp.ACTOR_WEB)
    with pytest.raises(SupporterError):
        rig.sup.unlink("nope", sp.ACTOR_WEB)


def test_linking_clears_a_queued_retry_for_that_order(rig):
    no = rig.order()
    rig.afdian.fail_next(urllib.error.URLError("down"))
    rig.sup.handle_order(rig.person(), no)
    rig.sup.link_order(no, "501", sp.ACTOR_WEB)
    assert rig.cfg()["retry"] == []


def test_removing_a_role_keeps_what_another_claim_of_the_same_person_still_justifies(tmp_path):
    rig = Rig(tmp_path, plan_roles={"gold": PLAN_ROLE})
    gold, plain = rig.order(plan_id="gold"), rig.order(plan_id="")
    rig.sup.link_order(gold, "501", sp.ACTOR_WEB, order=rig.afdian.orders[1])
    rig.sup.link_order(plain, "501", sp.ACTOR_WEB, order=rig.afdian.orders[0])
    assert rig.sup.roles_to_remove(rig.sup.unlink(gold, sp.ACTOR_WEB)) == [PLAN_ROLE]       # the plain order keeps ROLE
    assert rig.sup.roles_to_remove(rig.sup.unlink(plain, sp.ACTOR_WEB)) == [ROLE]


def test_the_claims_view_hides_amounts_unless_asked(rig):
    no = rig.order(total_amount="42.00")
    rig.sup.handle_order(rig.person(), no)
    rows = rig.sup.claims_view()
    assert rows[0]["order"] == no and rows[0]["masked"] == mask(no) and "amount" not in rows[0]
    assert rig.sup.claims_view(amounts=True)[0]["amount"] == "42"
    assert rig.sup.summary() == {"enabled": True, "claims": 1, "retries": 0}


# ---------------------------------------------------------------- remark matching

def mid(n: int) -> str:
    return "9" * 17 + str(n)               # built at run time: a real-looking Discord id, never written in the source


MEMBERS = [{"id": mid(1), "username": "mira_k", "display": "Mira"},
           {"id": mid(2), "username": "jonathan", "display": "Jon"},
           {"id": mid(3), "username": "al", "display": "Al"}]


def _o(no, remark, status=2):
    return {"out_trade_no": no, "remark": remark, "status": status}


def test_remarks_that_name_a_member_become_suggestions():
    orders = [_o("A1", "from mira_k, thank you"), _o("A2", "Jonathan here"), _o("A3", "id %s please" % mid(3)),
              _o("A4", "just a note"), _o("A5", "")]
    got = match_remarks(orders, MEMBERS)
    assert [(g["order"], g["user_id"], g["match"]) for g in got] == [
        ("A1", mid(1), "username"), ("A2", mid(2), "username"), ("A3", mid(3), "id")]
    assert got[0]["name"] == "Mira"


def test_short_names_unpaid_and_claimed_orders_are_skipped_and_the_longest_match_wins():
    assert match_remarks([_o("B1", "al and bo here")], MEMBERS) == []                    # 2-letter names never match
    assert match_remarks([_o("B2", "mira_k", status=1)], MEMBERS) == []
    assert match_remarks([_o("B3", "mira_k")], MEMBERS, claimed=["B3"]) == []
    people = MEMBERS + [{"id": mid(4), "username": "mira", "display": "Mira2"}]
    got = match_remarks([_o("B4", "this is mira_k")], people)
    assert [g["user_id"] for g in got] == [mid(1)]
    tie = match_remarks([_o("B5", "mira")], [{"id": "1", "username": "mira"}, {"id": "2", "username": "mira"}])
    assert sorted(g["user_id"] for g in tie) == ["1", "2"]                              # the owner decides
    assert match_remarks([_o("B6", "MIRA_K says hi")], MEMBERS)[0]["user_id"] == mid(1)


# ---------------------------------------------------------------- the texts

REPLY_KEYS = ["ok", "ok_again", "claimed_by_other", "need_verify_first", "not_found", "below_min", "limit",
              "retry_later", "gave_up", "off", "not_member", "grant_failed"]


def test_every_reply_exists_in_both_languages_is_short_and_never_mentions_money():
    for key in REPLY_KEYS:
        en, zh = templates.EN["supporter_" + key], templates.ZH["supporter_" + key]
        assert 20 < len(en) < 260 and 10 < len(zh) < 160
        for text in (en, zh):
            assert "$" not in text and "¥" not in text and "amount of" not in text.lower()
    assert templates.EN["supporter_not_found"].count("minutes") == 1 and "几分钟" in templates.ZH["supporter_not_found"]


def test_the_language_setting_picks_the_reply(tmp_path):
    rig = Rig(tmp_path)
    zh = Supporters(rig.store, clock=rig.clock, lang="zh", opener=rig.afdian)
    assert zh.reply_text("ok") == templates.ZH["supporter_ok"] and rig.sup.reply_text("ok") == templates.EN["supporter_ok"]
    assert zh.reply_text("") == ""
    no = rig.order()
    note = zh.handle_order(rig.person(), no).notify_texts[0]
    assert "认领了订单" in note and mask(no) in note


def test_a_workspace_template_file_overrides_a_reply(tmp_path):
    rig = Rig(tmp_path)
    tdir = tmp_path / "templates"
    tdir.mkdir()
    (tdir / "supporter_ok.txt").write_text("Welcome aboard!\n", encoding="utf-8")
    sup = Supporters(rig.store, clock=rig.clock, lang="en", templates_dir=tdir, opener=rig.afdian)
    assert sup.reply_text("ok") == "Welcome aboard!"


def test_owner_notices_cannot_ping_anyone(rig):
    no = rig.order()
    evil = Person(id="501", name="x", display="@everyone <@123> `hi`", role_ids=set())
    note = rig.sup.handle_order(evil, no).notify_texts[0]
    assert "@everyone" not in note and "<@" not in note and "`" not in note


# ---------------------------------------------------------------- roles the bot may hand out

def test_describe_roles_marks_only_roles_below_the_bot_s_top_role_assignable():
    roles = [{"id": "1", "name": "@everyone", "position": 0, "permissions": "0"},
             {"id": "2", "name": "Bot", "position": 5, "permissions": str(sp.MANAGE_ROLES)},
             {"id": "3", "name": "Low", "position": 3, "permissions": "0"},
             {"id": "4", "name": "High", "position": 7, "permissions": "0"},
             {"id": "5", "name": "Same", "position": 5, "permissions": "0"},
             {"id": "6", "name": "Integration", "position": 1, "permissions": "0", "managed": True}]
    info = sp.describe_roles(roles, {"roles": ["2"]}, "1")
    ok = {r["name"]: r["assignable"] for r in info["items"]}
    assert ok == {"High": False, "Bot": False, "Same": False, "Low": True, "Integration": False}
    assert info["bot_top"] == {"id": "2", "name": "Bot", "position": 5} and info["can_manage_roles"] is True
    assert [r["name"] for r in info["items"]] == ["High", "Bot", "Same", "Low", "Integration"]       # top of the list first
    no_perm = sp.describe_roles(roles, {"roles": ["5"]}, "1")
    assert no_perm["can_manage_roles"] is False and not any(r["assignable"] for r in no_perm["items"])
    admin = sp.describe_roles([{"id": "1", "name": "@everyone", "position": 0, "permissions": "0"},
                               {"id": "2", "name": "Boss", "position": 4, "permissions": str(sp.ADMINISTRATOR)},
                               {"id": "3", "name": "Low", "position": 1, "permissions": "0"}], {"roles": ["2"]}, "1")
    assert [r["assignable"] for r in admin["items"]] == [False, True]
    assert sp.describe_roles(roles, {"roles": []}, "1")["bot_top"] is None
