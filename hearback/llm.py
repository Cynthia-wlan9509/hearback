"""LLM profiles for SWLink: store, secret handling, provider calls (Anthropic and OpenAI style), fallback.

The profile file `.hearback/llm.json` never holds a key. Keys live in the OS keyring (service
`hearback-swlink`, user = profile id) when the `keyring` package works, else in the environment variable
`HEARBACK_LLM_KEY_<ID>`. Provider SDKs are imported inside functions; tests plug in fakes through
`set_provider_factory`.
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from hearback.util import HearbackError, atomic_write_json, ensure_data_dir, read_json_strict

KEYRING_SERVICE = "hearback-swlink"
ENV_PREFIX = "HEARBACK_LLM_KEY_"
PROTOCOLS = ("openai", "anthropic")
RESERVED_IDS = ("settings", "presets")
DEFAULT_MAX_TOKENS = 1024
TEST_PROMPT = "Reply with OK."


class LLMError(HearbackError):
    """A model call failed (the message is already scrubbed of keys). `status` is the HTTP status when known."""

    def __init__(self, message: str = "", status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- secret scrubbing

_SECRETS: set[str] = set()
_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),                                      # OpenAI / Anthropic / DeepSeek style
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\b[\w-]{23,28}\.[\w-]{6,7}\.[\w-]{27,}\b"),                     # Discord bot token shape
    re.compile(r"(?i)\b(api[_-]?key|token|authorization|x-api-key)(\"?\s*[:=]\s*\"?)[^\s\",;}]{6,}"),
]


def register_secret(value: str) -> None:
    """Remember a secret so scrub() removes it from every message from now on."""
    if value and len(value) >= 6:
        _SECRETS.add(value)


def scrub(text: Any) -> str:
    """Remove registered secrets and anything shaped like a key or token."""
    s = str(text)
    for sec in sorted(_SECRETS, key=len, reverse=True):
        s = s.replace(sec, "***")
    for rx in _PATTERNS:
        s = rx.sub(lambda m: (m.group(1) + m.group(2) + "***") if m.lastindex and m.lastindex >= 2 else "***", s)
    return s


def scrub_obj(obj: Any) -> Any:
    """scrub() applied to every string inside nested lists and dicts."""
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, list):
        return [scrub_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub_obj(v) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------- presets

PRESETS: list[dict] = [
    {"id": "anthropic", "name": "Anthropic", "protocol": "anthropic", "base_url": "", "base_url_required": False,
     "model": "claude-opus-5-5", "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]},
    {"id": "openai", "name": "OpenAI", "protocol": "openai", "base_url": "https://api.openai.com/v1",
     "base_url_required": False, "model": "", "models": []},
    {"id": "deepseek", "name": "DeepSeek", "protocol": "openai", "base_url": "https://api.deepseek.com",
     "base_url_required": False, "model": "", "models": []},
    {"id": "openai-relay", "name": "OpenAI-compatible relay", "protocol": "openai", "base_url": "",
     "base_url_required": True, "model": "", "models": []},
    {"id": "anthropic-relay", "name": "Anthropic-compatible relay", "protocol": "anthropic", "base_url": "",
     "base_url_required": True, "model": "", "models": []},
]


# ---------------------------------------------------------------- providers

def image_block(media_type: str, data: str) -> dict:
    """A neutral image block for a message's content list (`data` is base64). Text stays a plain string."""
    return {"type": "image", "media_type": media_type, "data": data}


def _anthropic_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    out = []
    for b in content:
        if b.get("type") == "image":
            out.append({"type": "image", "source": {"type": "base64", "media_type": b["media_type"],
                                                    "data": b["data"]}})
        else:
            out.append({"type": "text", "text": str(b.get("text", ""))})
    return out


def _openai_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    out = []
    for b in content:
        if b.get("type") == "image":
            out.append({"type": "image_url",
                        "image_url": {"url": "data:%s;base64,%s" % (b["media_type"], b["data"])}})
        else:
            out.append({"type": "text", "text": str(b.get("text", ""))})
    return out


class Provider:
    """What a provider object offers: one blocking call. A message's content is a string, or a list of
    {"type": "text", "text"} and image blocks (see image_block) when pictures are sent."""

    def complete(self, system: str, messages: Sequence[Mapping[str, Any]], max_tokens: int) -> str:
        raise NotImplementedError


class AnthropicProvider(Provider):
    def __init__(self, profile: Mapping[str, Any], key: str, timeout: float):
        import anthropic   # optional extra: hearback-desk[llm]
        self.model = profile["model"]
        self.client = anthropic.Anthropic(api_key=key, base_url=profile.get("base_url") or None, timeout=timeout)

    def complete(self, system: str, messages: Sequence[Mapping[str, Any]], max_tokens: int) -> str:
        msgs = [{**m, "content": _anthropic_content(m["content"])} for m in messages]
        resp = self.client.messages.create(model=self.model, max_tokens=max_tokens, system=system, messages=msgs)
        if getattr(resp, "stop_reason", None) == "refusal":
            raise LLMError("model refused")
        return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()


class OpenAIProvider(Provider):
    def __init__(self, profile: Mapping[str, Any], key: str, timeout: float):
        import openai   # optional extra: hearback-desk[llm]
        self.model = profile["model"]
        self.client = openai.OpenAI(api_key=key, base_url=profile.get("base_url") or None, timeout=timeout)

    @staticmethod
    def _max_tokens_unsupported(e: Exception) -> bool:
        low = str(e).lower()
        return getattr(e, "status_code", None) == 400 and "max_tokens" in low and (
            "unsupported" in low or "max_completion_tokens" in low or "not supported" in low)

    def complete(self, system: str, messages: Sequence[Mapping[str, Any]], max_tokens: int) -> str:
        msgs = [{"role": "system", "content": system}] + [{**m, "content": _openai_content(m["content"])}
                                                          for m in messages]
        try:
            r = self.client.chat.completions.create(model=self.model, messages=msgs, max_tokens=max_tokens)
        except Exception as e:
            if not self._max_tokens_unsupported(e):
                raise
            r = self.client.chat.completions.create(model=self.model, messages=msgs,
                                                    max_completion_tokens=max_tokens)
        return (r.choices[0].message.content or "").strip()


ProviderFactory = Callable[[Mapping[str, Any], str, float], Provider]
_FACTORIES: dict[str, ProviderFactory] = {"anthropic": AnthropicProvider, "openai": OpenAIProvider}


def set_provider_factory(protocol: str, factory: ProviderFactory | None) -> None:
    """Swap the provider for a protocol (tests). None restores the real SDK-backed one."""
    if protocol not in PROTOCOLS:
        raise ValueError("unknown protocol %r" % protocol)
    _FACTORIES[protocol] = factory or {"anthropic": AnthropicProvider, "openai": OpenAIProvider}[protocol]


def complete(profile: Mapping[str, Any], system: str, messages: Sequence[Mapping[str, Any]],
             max_tokens: int | None = None, *, key: str, timeout: float = 60) -> str:
    """One call to one profile. Raises LLMError (scrubbed) on any failure. `key` is the raw API key."""
    try:
        prov = _FACTORIES[profile["protocol"]](profile, key, timeout)
        return prov.complete(system, messages, int(max_tokens or profile.get("max_tokens") or DEFAULT_MAX_TOKENS))
    except LLMError as e:
        raise LLMError(scrub(e), e.status) from None
    except ImportError:
        raise LLMError("the %s SDK is not installed: pip install \"hearback-desk[llm]\"" % profile["protocol"]) from None
    except Exception as e:
        status = getattr(e, "status_code", None)
        raise LLMError("%s: %s" % (type(e).__name__, scrub(e)[:300]),
                       status if isinstance(status, int) else None) from None


# ---------------------------------------------------------------- the store

def env_name(profile_id: str) -> str:
    return ENV_PREFIX + re.sub(r"[^A-Za-z0-9]", "_", profile_id).upper()


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:32] or "model"


class LLMStore:
    """Profiles on disk, keys in the keyring or environment, call counters in memory."""

    def __init__(self, data_dir: str | Path, environ: Mapping[str, str] | None = None, keyring_mod: Any = "auto",
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        import os
        self.dir = Path(data_dir)
        self.path = self.dir / "llm.json"
        self.environ = os.environ if environ is None else environ
        self._keyring = keyring_mod          # "auto": import keyring lazily; None: never use it
        self.clock, self.sleep = clock, sleep
        self._lock = threading.RLock()
        self._calls: dict[str, deque[float]] = {}
        self._state: dict[str, dict] = {}     # id -> {status, error}

    # ---- file
    def load(self) -> dict:
        d = read_json_strict(self.path, None, "the model profile file")
        d = d if isinstance(d, dict) else {}
        d.setdefault("active", "")
        d.setdefault("fallback", False)
        d.setdefault("retries", 1)
        d.setdefault("timeout_s", 60)
        if not isinstance(d.get("profiles"), list):
            d["profiles"] = []
        return d

    def _save(self, d: dict) -> None:
        ensure_data_dir(self.dir)
        atomic_write_json(self.path, d)

    # ---- keys
    def _kr(self) -> Any:
        if self._keyring == "auto":
            try:
                import keyring   # optional extra: hearback-desk[keyring]
                return keyring
            except Exception:
                return None
        return self._keyring

    def keyring_available(self) -> bool:
        return self._kr() is not None

    def get_key(self, profile_id: str) -> str:
        kr = self._kr()
        if kr is not None:
            try:
                k = kr.get_password(KEYRING_SERVICE, profile_id)
                if k:
                    register_secret(k)
                    return k
            except Exception:
                pass
        k = (self.environ.get(env_name(profile_id)) or "").strip()
        register_secret(k)
        return k

    def has_key(self, profile_id: str) -> bool:
        return bool(self.get_key(profile_id))

    def set_key(self, profile_id: str, key: str) -> dict:
        """Store a key (keyring only; never the JSON file). Returns the updated public profile."""
        key = (key or "").strip()
        if not key or any(c.isspace() for c in key) or len(key) > 500:
            raise LLMError("the key is empty or contains spaces")
        with self._lock:
            d = self.load()
            p = self._find(d, profile_id)
            kr = self._kr()
            if kr is None:
                raise LLMError("no OS keyring available. Set the environment variable %s instead (then restart)"
                               % env_name(profile_id))
            try:
                kr.set_password(KEYRING_SERVICE, profile_id, key)
            except Exception as e:
                raise LLMError("the OS keyring refused the key (%s). Use the environment variable %s instead"
                               % (type(e).__name__, env_name(profile_id))) from None
            register_secret(key)
            p["key_hint"] = key[-4:]
            self._save(d)
            self._state.pop(profile_id, None)
            return self.public(p)

    def _drop_key(self, profile_id: str) -> None:
        kr = self._kr()
        if kr is not None:
            try:
                kr.delete_password(KEYRING_SERVICE, profile_id)
            except Exception:
                pass

    # ---- profiles
    @staticmethod
    def _find(d: dict, profile_id: str) -> dict:
        for p in d["profiles"]:
            if p["id"] == profile_id:
                return p
        raise LLMError("no model profile %r" % profile_id)

    def get(self, profile_id: str) -> dict:
        return self._find(self.load(), profile_id)

    def public(self, p: Mapping[str, Any]) -> dict:
        """What the UI may see: the profile, a key hint, and live counters. Never the key."""
        pid = p["id"]
        now = self.clock()
        with self._lock:
            q = self._calls.get(pid, deque())
            st = self._state.get(pid, {})
        hint = p.get("key_hint") or ""
        if not hint:
            k = self.get_key(pid)
            hint = k[-4:] if k else ""
        return {**{k: p.get(k) for k in ("id", "name", "protocol", "base_url", "model", "max_tokens", "enabled",
                                          "order")},
                "key_set": bool(self.get_key(pid)), "key_hint": hint,
                "calls_s": sum(1 for t in q if now - t < 1), "calls_min": sum(1 for t in q if now - t < 60),
                "status": st.get("status", "untested"), "last_error": st.get("error", "")}

    def list_public(self) -> list[dict]:
        d = self.load()
        return [self.public(p) for p in sorted(d["profiles"], key=lambda p: (p.get("order", 0), p["id"]))]

    def _clean(self, f: Mapping[str, Any], base: Mapping[str, Any] | None = None, preset: str = "") -> dict:
        out = dict(base or {})
        for k in ("name", "protocol", "base_url", "model"):
            if k in f:
                out[k] = str(f[k] or "").strip()
        if "max_tokens" in f:
            try:
                out["max_tokens"] = int(f["max_tokens"])
            except (TypeError, ValueError):
                raise LLMError("max_tokens must be a number") from None
        if "enabled" in f:
            out["enabled"] = bool(f["enabled"])
        if out.get("protocol") not in PROTOCOLS:
            raise LLMError("protocol must be one of: %s" % ", ".join(PROTOCOLS))
        if not out.get("name"):
            raise LLMError("the profile needs a name")
        if not out.get("model"):
            raise LLMError("the profile needs a model name")
        bu = out.get("base_url", "")
        if bu and not re.match(r"^https?://[^\s/]+", bu):
            raise LLMError("base_url must start with http:// or https://")
        need = next((x["base_url_required"] for x in PRESETS if x["id"] == preset), False)
        if need and not bu:
            raise LLMError("this kind of relay needs a base URL")
        if not 1 <= int(out.get("max_tokens") or 0) <= 200000:
            raise LLMError("max_tokens must be between 1 and 200000")
        return out

    def add_profile(self, fields: Mapping[str, Any], preset: str = "") -> dict:
        with self._lock:
            d = self.load()
            f = {"max_tokens": DEFAULT_MAX_TOKENS, "enabled": True, **fields}
            p = self._clean(f, preset=preset)
            base, n = _slug(p["name"]), 1
            ids = {x["id"] for x in d["profiles"]} | set(RESERVED_IDS)
            pid = base
            while pid in ids:
                n += 1
                pid = "%s-%d" % (base, n)
            p["id"], p["key_hint"] = pid, ""
            p["order"] = max([x.get("order", 0) for x in d["profiles"]] + [0]) + 1
            d["profiles"].append(p)
            if not d["active"]:
                d["active"] = pid
            self._save(d)
            return self.public(p)

    def update_profile(self, profile_id: str, fields: Mapping[str, Any]) -> dict:
        with self._lock:
            d = self.load()
            p = self._find(d, profile_id)
            new = self._clean({k: v for k, v in fields.items() if k != "id"}, base=p)
            if "order" in fields:
                new["order"] = int(fields["order"])
            p.update(new)
            self._save(d)
            self._state.pop(profile_id, None)
            return self.public(p)

    def delete_profile(self, profile_id: str) -> None:
        with self._lock:
            d = self.load()
            self._find(d, profile_id)
            d["profiles"] = [p for p in d["profiles"] if p["id"] != profile_id]
            if d["active"] == profile_id:
                left = [p for p in sorted(d["profiles"], key=lambda p: p.get("order", 0)) if p.get("enabled", True)]
                d["active"] = left[0]["id"] if left else ""
            self._save(d)
            self._drop_key(profile_id)

    def activate(self, profile_id: str) -> None:
        with self._lock:
            d = self.load()
            self._find(d, profile_id)
            d["active"] = profile_id
            self._save(d)

    def set_settings(self, fields: Mapping[str, Any]) -> dict:
        with self._lock:
            d = self.load()
            if "fallback" in fields:
                d["fallback"] = bool(fields["fallback"])
            for k, lo, hi in (("retries", 0, 5), ("timeout_s", 5, 600)):
                if k in fields:
                    try:
                        v = int(fields[k])
                    except (TypeError, ValueError):
                        raise LLMError("%s must be a number" % k) from None
                    if not lo <= v <= hi:
                        raise LLMError("%s must be between %d and %d" % (k, lo, hi))
                    d[k] = v
            self._save(d)
            return self.settings(d)

    def settings(self, d: dict | None = None) -> dict:
        d = d or self.load()
        return {"active": d["active"], "fallback": d["fallback"], "retries": d["retries"],
                "timeout_s": d["timeout_s"]}

    def active_public(self) -> dict | None:
        d = self.load()
        for p in d["profiles"]:
            if p["id"] == d["active"]:
                return self.public(p)
        return None

    # ---- counters
    def _note_call(self, pid: str) -> None:
        with self._lock:
            q = self._calls.setdefault(pid, deque(maxlen=600))
            q.append(self.clock())

    def _note_result(self, pid: str, error: str = "") -> None:
        with self._lock:
            self._state[pid] = {"status": "error" if error else "ok", "error": error}

    # ---- calls
    def call_profile(self, p: Mapping[str, Any], system: str, messages: Sequence[Mapping[str, Any]],
                     max_tokens: int | None = None, timeout: float | None = None) -> str:
        """One attempt against one profile, with counters. Raises LLMError."""
        pid = p["id"]
        key = self.get_key(pid)
        if not key:
            err = "no API key for %s (add one in the LLM page)" % p.get("name", pid)
            self._note_result(pid, err)
            raise LLMError(err)
        self._note_call(pid)
        try:
            text = complete(p, system, messages, max_tokens, key=key,
                            timeout=timeout or self.load()["timeout_s"])
        except LLMError as e:
            self._note_result(pid, str(e))
            raise
        self._note_result(pid)
        return text

    def test_profile(self, profile_id: str) -> dict:
        """One tiny request. Returns {ok, text} or {ok: False, error}; the error is scrubbed."""
        p = self.get(profile_id)
        try:
            text = self.call_profile(p, "You are a connectivity test.", [{"role": "user", "content": TEST_PROMPT}],
                                     max_tokens=16)
        except LLMError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "text": scrub(text)[:200]}

    def candidates(self, prefer: str | None = None) -> list[dict]:
        d = self.load()
        ordered = sorted((p for p in d["profiles"] if p.get("enabled", True)), key=lambda p: p.get("order", 0))
        first = [p for p in d["profiles"] if p["id"] == prefer] or [p for p in d["profiles"] if p["id"] == d["active"]]
        if not first:
            return []
        first_id = first[0]["id"]
        rest = [p for p in ordered if p["id"] != first_id] if d["fallback"] else []
        return first + rest


def complete_any(store: LLMStore, system: str, messages: Sequence[Mapping[str, Any]],
                 max_tokens: int | None = None, prefer: str | None = None) -> tuple[str, str]:
    """Answer with the active (or preferred) profile; with fallback on, try the other enabled ones in order.

    Each profile gets 1 + retries attempts (a refusal or a missing key is not retried). Returns (text, profile_id).
    """
    cands = store.candidates(prefer)
    if not cands:
        raise LLMError("no model profile is set up (add one in the LLM page)")
    retries = int(store.load()["retries"])
    errors: list[str] = []
    statuses: list[int | None] = []
    for p in cands:
        for attempt in range(retries + 1):
            try:
                return store.call_profile(p, system, messages, max_tokens), p["id"]
            except LLMError as e:
                errors.append("%s: %s" % (p.get("name", p["id"]), e))
                statuses.append(e.status)
                if "refused" in str(e) or str(e).startswith("no API key"):
                    break
                if attempt < retries:
                    store.sleep(0.5 * (attempt + 1))
    raise LLMError("; ".join(errors)[:600], statuses[0] if len(set(statuses)) == 1 else None)
