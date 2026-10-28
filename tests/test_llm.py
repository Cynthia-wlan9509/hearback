"""LLM profiles: storage without keys, key handling, provider adapters (fake SDKs), fallback, counters, scrubbing."""
from __future__ import annotations

import json
import sys
import types

import pytest

from hearback import llm
from hearback.llm import LLMError, LLMStore, complete, complete_any, scrub, set_provider_factory

KEY = "sk-test-SECRETKEY-0123456789"


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, pw):
        self.store[(service, user)] = pw

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


class FakeProvider:
    """Scripted provider: each call pops a result (a string, or an exception to raise)."""

    def __init__(self, script):
        self.script, self.calls = script, []

    def factory(self, profile, key, timeout):
        outer = self

        class P(llm.Provider):
            def complete(self, system, messages, max_tokens):
                outer.calls.append((profile["id"], key, system, messages, max_tokens))
                r = outer.script.pop(0) if outer.script else "ok"
                if isinstance(r, Exception):
                    raise r
                return r
        return P()


@pytest.fixture(autouse=True)
def _reset_providers():
    yield
    for p in llm.PROTOCOLS:
        set_provider_factory(p, None)


@pytest.fixture
def store(tmp_path):
    t = [1000.0]
    s = LLMStore(tmp_path / ".hearback", environ={}, keyring_mod=FakeKeyring(), clock=lambda: t[0],
                 sleep=lambda s: None)
    s.t = t
    return s


def add(store, name="Main", protocol="openai", **kw):
    return store.add_profile({"name": name, "protocol": protocol, "model": "m1", **kw})


def test_presets_cover_the_documented_providers():
    by = {p["id"]: p for p in llm.PRESETS}
    assert by["anthropic"]["model"] == "claude-opus-5-5" and by["anthropic"]["base_url"] == ""
    assert by["anthropic"]["models"] == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
    assert by["openai"]["base_url"] == "https://api.openai.com/v1"
    assert by["deepseek"]["base_url"] == "https://api.deepseek.com"
    assert by["openai-relay"]["base_url_required"] and by["anthropic-relay"]["base_url_required"]


def test_profile_crud_and_active(store):
    a, b = add(store, "One"), add(store, "Two", protocol="anthropic")
    assert a["id"] == "one" and store.load()["active"] == "one"
    store.activate(b["id"])
    assert store.active_public()["id"] == "two"
    up = store.update_profile("one", {"model": "m2", "max_tokens": 50})
    assert up["model"] == "m2" and up["max_tokens"] == 50
    store.delete_profile("two")
    assert store.load()["active"] == "one"
    assert [p["id"] for p in store.list_public()] == ["one"]


def test_validation(store):
    with pytest.raises(LLMError, match="protocol"):
        store.add_profile({"name": "x", "protocol": "nope", "model": "m"})
    with pytest.raises(LLMError, match="model"):
        store.add_profile({"name": "x", "protocol": "openai", "model": ""})
    with pytest.raises(LLMError, match="http"):
        add(store, base_url="ftp://x")
    with pytest.raises(LLMError, match="relay needs a base URL"):
        store.add_profile({"name": "x", "protocol": "openai", "model": "m"}, preset="openai-relay")
    assert add(store, "settings")["id"] != "settings"      # reserved route names are never used as ids


def test_keys_go_to_the_keyring_and_never_into_the_json_file(store):
    p = add(store)
    pub = store.set_key(p["id"], KEY)
    assert pub["key_hint"] == KEY[-4:] and pub["key_set"] is True
    assert KEY not in store.path.read_text(encoding="utf-8")
    assert store.get_key(p["id"]) == KEY
    assert KEY not in json.dumps(store.list_public())
    store.delete_profile(p["id"])
    assert store.get_key(p["id"]) == ""                      # removed from the keyring too


def test_env_var_fallback_without_keyring(tmp_path):
    s = LLMStore(tmp_path, environ={"HEARBACK_LLM_KEY_MY_RELAY_2": KEY}, keyring_mod=None)
    p = s.add_profile({"name": "my relay 2", "protocol": "openai", "model": "m"})
    assert p["id"] == "my-relay-2" and p["key_set"] and p["key_hint"] == KEY[-4:]
    with pytest.raises(LLMError, match="HEARBACK_LLM_KEY_MY_RELAY_2"):
        s.set_key(p["id"], "other-key-value")
    assert KEY not in s.path.read_text(encoding="utf-8")


def test_set_key_rejects_junk(store):
    p = add(store)
    for bad in ("", "has space", "x" * 600):
        with pytest.raises(LLMError):
            store.set_key(p["id"], bad)


def test_anthropic_adapter_sends_only_the_plain_parameters(monkeypatch):
    seen = {}

    class Client:
        def __init__(self, **kw):
            seen["init"] = kw
            self.messages = types.SimpleNamespace(create=self.create)

        def create(self, **kw):
            seen["create"] = kw
            block = types.SimpleNamespace(type="text", text=" hello ")
            return types.SimpleNamespace(stop_reason="end_turn",
                                         content=[block, types.SimpleNamespace(type="thinking")])

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Client))
    prof = {"id": "a", "protocol": "anthropic", "base_url": "", "model": "claude-opus-5-5", "max_tokens": 77}
    assert complete(prof, "sys", [{"role": "user", "content": "hi"}], key=KEY, timeout=9) == "hello"
    assert seen["init"] == {"api_key": KEY, "base_url": None, "timeout": 9}
    assert seen["create"] == {"model": "claude-opus-5-5", "max_tokens": 77, "system": "sys",
                              "messages": [{"role": "user", "content": "hi"}]}
    for banned in ("temperature", "top_p", "thinking"):
        assert banned not in seen["create"]


def test_anthropic_refusal_raises(monkeypatch):
    class Client:
        def __init__(self, **kw):
            self.messages = types.SimpleNamespace(
                create=lambda **k: types.SimpleNamespace(stop_reason="refusal", content=[]))

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Client))
    prof = {"id": "a", "protocol": "anthropic", "base_url": "https://relay.example/v1", "model": "m"}
    with pytest.raises(LLMError, match="model refused"):
        complete(prof, "s", [{"role": "user", "content": "x"}], 10, key=KEY)


def test_openai_adapter_and_max_completion_tokens_retry(monkeypatch):
    calls = []

    class BadRequest(Exception):
        status_code = 400

    class Client:
        def __init__(self, **kw):
            calls.append(("init", kw))
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

        def create(self, **kw):
            calls.append(("create", kw))
            if "max_tokens" in kw:
                raise BadRequest("Unsupported parameter: 'max_tokens' is not supported, use 'max_completion_tokens'")
            msg = types.SimpleNamespace(content="done")
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=Client))
    prof = {"id": "o", "protocol": "openai", "base_url": "https://api.deepseek.com", "model": "m", "max_tokens": 5}
    assert complete(prof, "sys", [{"role": "user", "content": "q"}], key=KEY) == "done"
    assert calls[0] == ("init", {"api_key": KEY, "base_url": "https://api.deepseek.com", "timeout": 60})
    first, second = calls[1][1], calls[2][1]
    assert first["max_tokens"] == 5 and "max_completion_tokens" not in first
    assert second["max_completion_tokens"] == 5 and "max_tokens" not in second
    assert first["messages"][0] == {"role": "system", "content": "sys"}


def test_missing_sdk_gives_an_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", None)
    with pytest.raises(LLMError, match="hearback-desk"):
        complete({"id": "o", "protocol": "openai", "model": "m"}, "s", [], key=KEY)


def test_sdk_errors_are_scrubbed(store):
    p = add(store)
    store.set_key(p["id"], KEY)
    boom = RuntimeError("401 bad key %s for Authorization: Bearer abcdef123456789" % KEY)
    set_provider_factory("openai", FakeProvider([boom]).factory)
    r = store.test_profile(p["id"])
    assert r["ok"] is False and KEY not in r["error"] and "abcdef123456789" not in r["error"]
    pub = store.public(store.get(p["id"]))
    assert pub["status"] == "error" and KEY not in pub["last_error"]


def test_test_profile_ok_and_counters(store):
    p = add(store)
    store.set_key(p["id"], KEY)
    fp = FakeProvider(["OK"])
    set_provider_factory("openai", fp.factory)
    assert store.public(store.get(p["id"]))["status"] == "untested"
    assert store.test_profile(p["id"]) == {"ok": True, "text": "OK"}
    pub = store.public(store.get(p["id"]))
    assert pub["status"] == "ok" and pub["calls_s"] == 1 and pub["calls_min"] == 1
    assert fp.calls[0][3] == [{"role": "user", "content": "Reply with OK."}]
    store.t[0] += 30
    pub = store.public(store.get(p["id"]))
    assert pub["calls_s"] == 0 and pub["calls_min"] == 1
    store.t[0] += 40
    assert store.public(store.get(p["id"]))["calls_min"] == 0


def test_complete_any_uses_active_then_preferred_profile(store):
    a, b = add(store, "A"), add(store, "B")
    for p in (a, b):
        store.set_key(p["id"], "key-%s-xxxxxx" % p["id"])
    fp = FakeProvider(["from-a", "from-b"])
    set_provider_factory("openai", fp.factory)
    assert complete_any(store, "s", [{"role": "user", "content": "q"}]) == ("from-a", "a")
    assert complete_any(store, "s", [], prefer="b") == ("from-b", "b")
    assert complete_any(store, "s", [], prefer="deleted-profile")[1] == "a"      # unknown preference -> active


def test_fallback_only_when_enabled(store):
    a, b = add(store, "A"), add(store, "B")
    for p in (a, b):
        store.set_key(p["id"], "key-%s-xxxxxx" % p["id"])
    store.set_settings({"retries": 0})
    fp = FakeProvider([RuntimeError("down"), "from-b"])
    set_provider_factory("openai", fp.factory)
    with pytest.raises(LLMError, match="down"):
        complete_any(store, "s", [])
    assert [c[0] for c in fp.calls] == ["a"]
    store.set_settings({"fallback": True})
    fp.script, fp.calls = [RuntimeError("down"), "from-b"], []
    assert complete_any(store, "s", []) == ("from-b", "b")
    assert [c[0] for c in fp.calls] == ["a", "b"]


def test_retries_then_success_and_no_retry_on_refusal(store):
    a = add(store)
    store.set_key(a["id"], "key-a-xxxxxx")
    store.set_settings({"retries": 2})
    fp = FakeProvider([RuntimeError("blip"), RuntimeError("blip"), "fine"])
    set_provider_factory("openai", fp.factory)
    assert complete_any(store, "s", []) == ("fine", a["id"])
    assert len(fp.calls) == 3
    fp.script, fp.calls = [LLMError("model refused")], []
    with pytest.raises(LLMError, match="refused"):
        complete_any(store, "s", [])
    assert len(fp.calls) == 1


def test_missing_key_and_no_profiles(store):
    with pytest.raises(LLMError, match="no model profile"):
        complete_any(store, "s", [])
    add(store)
    with pytest.raises(LLMError, match="no API key"):
        complete_any(store, "s", [])


def test_settings_validation(store):
    assert store.set_settings({"fallback": True, "retries": 3, "timeout_s": 20}) == {
        "active": "", "fallback": True, "retries": 3, "timeout_s": 20}
    with pytest.raises(LLMError):
        store.set_settings({"retries": 99})
    with pytest.raises(LLMError):
        store.set_settings({"timeout_s": "x"})


def test_scrub_removes_registered_and_key_shaped_text():
    llm.register_secret("my-very-secret-value")
    out = scrub("a my-very-secret-value b sk-ant-abcdefghijklmnop c Bearer tok_1234567890 d api_key=abcdef12345")
    assert "my-very-secret-value" not in out and "sk-ant" not in out and "tok_1234567890" not in out
    assert "abcdef12345" not in out
    assert scrub("plain text, max_tokens too big") == "plain text, max_tokens too big"
    assert llm.scrub_obj({"a": ["sk-abcdefghijk"]}) == {"a": ["***"]}


def test_image_blocks_become_each_providers_own_format(monkeypatch):
    seen = {}

    class AClient:
        def __init__(self, **kw):
            self.messages = types.SimpleNamespace(create=self.create)

        def create(self, **kw):
            seen["anthropic"] = kw["messages"]
            return types.SimpleNamespace(stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text="a")])

    class OClient:
        def __init__(self, **kw):
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

        def create(self, **kw):
            seen["openai"] = kw["messages"]
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="o"))])

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=AClient))
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=OClient))
    content = [llm.image_block("image/png", "QUJD"), {"type": "text", "text": "what is this"}]
    msgs = [{"role": "user", "content": content}, {"role": "assistant", "content": "plain text stays a string"}]
    complete({"id": "a", "protocol": "anthropic", "model": "m"}, "s", msgs, 10, key=KEY)
    complete({"id": "o", "protocol": "openai", "model": "m"}, "s", msgs, 10, key=KEY)
    assert seen["anthropic"][0]["content"] == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
        {"type": "text", "text": "what is this"}]
    assert seen["openai"][1]["content"] == [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        {"type": "text", "text": "what is this"}]
    assert seen["anthropic"][1]["content"] == seen["openai"][2]["content"] == "plain text stays a string"
    assert content[0] == {"type": "image", "media_type": "image/png", "data": "QUJD"}      # the caller's blocks are not changed


def test_http_status_survives_complete_and_complete_any(store):
    class Bad(Exception):
        status_code = 400

    a = add(store)
    store.set_key(a["id"], "key-a-xxxxxx")
    store.set_settings({"retries": 0})
    set_provider_factory("openai", FakeProvider([Bad("no images please")]).factory)
    with pytest.raises(LLMError) as ei:
        complete_any(store, "s", [])
    assert ei.value.status == 400 and "no images please" in str(ei.value)
    set_provider_factory("openai", FakeProvider([RuntimeError("plain")]).factory)
    with pytest.raises(LLMError) as ei:
        complete_any(store, "s", [])
    assert ei.value.status is None


# ---------------------------------------------------------------- round 3: empty answers, defaults, concurrency, 429/503

class Limited(Exception):
    """What an SDK raises for a relay 429/503, with an optional Retry-After header."""

    def __init__(self, msg, status=429, retry_after=None):
        super().__init__(msg)
        self.status_code = status
        self.response = types.SimpleNamespace(headers={"retry-after": str(retry_after)} if retry_after is not None else {})


@pytest.fixture
def napping(tmp_path):
    """A store whose sleeps are recorded instead of slept."""
    s = LLMStore(tmp_path / ".hearback", environ={}, keyring_mod=FakeKeyring(), sleep=lambda x: s.naps.append(x))
    s.naps = []
    return s


def test_profile_test_asks_for_512_tokens_and_flags_empty_text(store):
    p = add(store)
    store.set_key(p["id"], KEY)
    fp = FakeProvider(["", "OK"])
    set_provider_factory("openai", fp.factory)
    r = store.test_profile(p["id"])
    assert r == {"ok": False, "error": llm.EMPTY_TEXT_ERROR} and "2048+" in r["error"]
    pub = store.public(store.get(p["id"]))
    assert pub["status"] == "error" and pub["last_error"] == llm.EMPTY_TEXT_ERROR
    assert store.test_profile(p["id"])["ok"] is True
    assert [c[4] for c in fp.calls] == [512, 512]


def _openai_client(monkeypatch, content, finish_reason):
    class Client:
        def __init__(self, **kw):
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

        def create(self, **kw):
            msg = types.SimpleNamespace(content=content)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason=finish_reason)])
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=Client))


def test_openai_empty_answer_that_ran_out_of_tokens_is_an_error(monkeypatch):
    prof = {"id": "o", "protocol": "openai", "model": "m"}
    _openai_client(monkeypatch, None, "length")
    with pytest.raises(LLMError, match="ran out of output tokens.*raise Max output tokens"):
        complete(prof, "s", [], 5, key=KEY)
    _openai_client(monkeypatch, "  ", "length")
    with pytest.raises(LLMError, match="ran out of output tokens"):
        complete(prof, "s", [], 5, key=KEY)
    _openai_client(monkeypatch, "cut off but", "length")           # some text came back: keep it
    assert complete(prof, "s", [], 5, key=KEY) == "cut off but"
    _openai_client(monkeypatch, "", "stop")                        # an empty stop is the caller's business
    assert complete(prof, "s", [], 5, key=KEY) == ""


def test_anthropic_empty_answer_that_hit_max_tokens_is_an_error(monkeypatch):
    def client(blocks, stop):
        class Client:
            def __init__(self, **kw):
                self.messages = types.SimpleNamespace(
                    create=lambda **k: types.SimpleNamespace(stop_reason=stop, content=blocks))
        monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Client))

    prof = {"id": "a", "protocol": "anthropic", "model": "m"}
    client([types.SimpleNamespace(type="thinking")], "max_tokens")
    with pytest.raises(LLMError, match="ran out of output tokens"):
        complete(prof, "s", [], 5, key=KEY)
    client([types.SimpleNamespace(type="text", text="partial")], "max_tokens")
    assert complete(prof, "s", [], 5, key=KEY) == "partial"
    client([], "end_turn")
    assert complete(prof, "s", [], 5, key=KEY) == ""


def test_new_profiles_default_to_2048_tokens_and_2_concurrent_old_files_keep_theirs(store):
    assert llm.DEFAULT_MAX_TOKENS == 2048
    new = add(store, "New")
    assert new["max_tokens"] == 2048 and new["max_concurrent"] == 2
    store.path.write_text(json.dumps({"active": "old", "profiles": [
        {"id": "old", "name": "Old", "protocol": "openai", "model": "m", "max_tokens": 1024, "enabled": True}]}),
        encoding="utf-8")
    old = store.public(store.get("old"))
    assert old["max_tokens"] == 1024 and old["max_concurrent"] == 2          # a file without the field gets the default
    assert store.update_profile("old", {"name": "Old two"})["max_tokens"] == 1024


def test_max_concurrent_is_validated_and_editable(store):
    p = add(store, max_concurrent=5)
    assert p["max_concurrent"] == 5
    assert store.update_profile(p["id"], {"max_concurrent": 16})["max_concurrent"] == 16
    for bad in (0, 17, "x", None):
        with pytest.raises(LLMError, match="max_concurrent"):
            store.update_profile(p["id"], {"max_concurrent": bad})
    with pytest.raises(LLMError, match="max_concurrent"):
        add(store, "Other", max_concurrent=99)
    assert store.get(p["id"])["max_concurrent"] == 16


def test_a_semaphore_caps_calls_in_flight_per_profile(store):
    import threading
    import time

    p = add(store, max_concurrent=2)
    other = add(store, "Other", max_concurrent=1)
    for x in (p, other):
        store.set_key(x["id"], "key-%s-xxxxxx" % x["id"])
    lock, gate = threading.Lock(), threading.Event()
    seen = {"now": 0, "peak": 0, "done": 0}

    class Slow(llm.Provider):
        def complete(self, system, messages, max_tokens):
            with lock:
                seen["now"] += 1
                seen["peak"] = max(seen["peak"], seen["now"])
            gate.wait(5)
            with lock:
                seen["now"] -= 1
                seen["done"] += 1
            return "ok"
    set_provider_factory("openai", lambda profile, key, timeout: Slow())
    threads = [threading.Thread(target=store.call_profile, args=(store.get(p["id"]), "s", [])) for _ in range(5)]
    for t in threads:
        t.start()
    end = time.monotonic() + 3
    while seen["peak"] < 2 and time.monotonic() < end:
        time.sleep(0.01)
    time.sleep(0.15)                                  # give a third call the chance to (wrongly) get in
    assert seen["now"] == 2 and seen["peak"] == 2
    assert store.public(store.get(p["id"]))["calls_min"] == 2        # waiting calls are not counted yet
    second = threading.Thread(target=store.call_profile, args=(store.get(other["id"]), "s", []))
    second.start()                                    # another profile has its own slots
    time.sleep(0.1)
    assert seen["now"] == 3
    gate.set()
    for t in threads + [second]:
        t.join(5)
    assert seen["done"] == 6 and seen["peak"] <= 3 and seen["now"] == 0


def test_429_waits_2_then_5_seconds_then_gives_up(napping):
    p = add(napping, max_concurrent=1)               # one slot: a leaked slot would hang the retry
    napping.set_key(p["id"], "key-a-xxxxxx")
    napping.set_settings({"retries": 2})
    fp = FakeProvider([Limited("concurrent requests reached your tier limit (3)"), Limited("no route available", 503),
                       Limited("still limited")])
    set_provider_factory("openai", fp.factory)
    with pytest.raises(LLMError) as ei:
        complete_any(napping, "s", [])
    assert napping.naps == [2.0, 5.0] and len(fp.calls) == 3 and ei.value.status is None
    pub = napping.public(napping.get(p["id"]))
    assert pub["status"] == "error" and pub["last_error"].endswith("[HTTP 429 x3 since the last success]")
    fp.script = [Limited("busy", 503), "fine"]
    napping.naps.clear()
    assert complete_any(napping, "s", []) == ("fine", p["id"])
    assert napping.naps == [2.0]
    pub = napping.public(napping.get(p["id"]))
    assert pub["status"] == "ok" and pub["last_error"] == ""            # the count starts over after a success


def test_429_honours_retry_after_and_caps_it(napping):
    p = add(napping)
    napping.set_key(p["id"], "key-a-xxxxxx")
    napping.set_settings({"retries": 2})
    set_provider_factory("openai", FakeProvider([Limited("slow down", 429, retry_after=7), Limited("again", 429, 9999),
                                                 "fine"]).factory)
    assert complete_any(napping, "s", [])[0] == "fine"
    assert napping.naps == [7.0, llm.MAX_RETRY_WAIT]


def test_other_errors_keep_the_short_backoff(napping):
    p = add(napping)
    napping.set_key(p["id"], "key-a-xxxxxx")
    napping.set_settings({"retries": 2})
    set_provider_factory("openai", FakeProvider([RuntimeError("blip"), Limited("bad request", 400), "fine"]).factory)
    assert complete_any(napping, "s", [])[0] == "fine"
    assert napping.naps == [0.5, 1.0]


def test_after_the_retries_a_limited_profile_falls_back_to_the_next(napping):
    a, b = add(napping, "A"), add(napping, "B")
    for x in (a, b):
        napping.set_key(x["id"], "key-%s-xxxxxx" % x["id"])
    napping.set_settings({"retries": 1, "fallback": True})
    fp = FakeProvider([Limited("limit"), Limited("limit"), "from-b"])
    set_provider_factory("openai", fp.factory)
    assert complete_any(napping, "s", []) == ("from-b", "b")
    assert [c[0] for c in fp.calls] == ["a", "a", "b"] and napping.naps == [2.0]


def test_retry_after_is_read_from_headers_or_attributes():
    assert llm._retry_after(Limited("x", 429, retry_after=3)) == 3.0
    e = Exception("x")
    e.retry_after = 4
    assert llm._retry_after(e) == 4.0
    for junk in ("soon", -1, float("nan")):
        bad = Limited("x")
        bad.response.headers = {"Retry-After": junk}
        assert llm._retry_after(bad) is None
    assert llm._retry_after(Exception("plain")) is None
