"""SWLink supporters: turn Afdian payments into a Discord role.

A supporter pays on Afdian, sends the bot the order number in a DM, and gets the supporter role. The pieces here need
no Discord: the Afdian open-API client (stdlib urllib, `afdian.com` only), the settings store
(`.hearback/supporters.json`; the API token lives in the OS keyring or `HEARBACK_AFDIAN_TOKEN`, never in the file),
the decision logic (`Supporters.handle_order`), link/unlink for the owner and remark matching.
`SupporterBot` is the thin REST layer that applies a decision on Discord (member lookup, role grant with an audit-log
reason, DM replies, owner notices, the retry loop).

Amounts never appear in replies or notices, and order numbers are masked (first 4 + ... + last 4) in notices and logs.
"""
from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from hearback import __version__, templates
from hearback.discord import DiscordClient, DiscordError, msg_body
from hearback.llm import KEYRING_SERVICE, register_secret, scrub
from hearback.util import HearbackError, atomic_write_json, ensure_data_dir, iso, read_json_strict

AFDIAN_BASE = "https://afdian.com/api/open/"
ENDPOINTS = ("ping", "query-order", "query-sponsor")
TOKEN_USER = "afdian-token"                 # keyring user, under the service shared with the model keys
ENV_TOKEN = "HEARBACK_AFDIAN_TOKEN"
USER_AGENT = "hearback-swlink/%s" % __version__
TIMEOUT_S = 20
REMARK_SHOWN = 300                         # characters of an order remark shown to the owner
ORDER_PAGE_SIZE = 50                        # fixed by Afdian: there is no per-page parameter
STATUS_PAID = "2"
DAY_S = 86400
RETRY_EVERY_S = 600
GIVE_UP_S = 3 * DAY_S
ACTOR_WEB = "human:web"
ACTOR_BOT = "bot:swlink"
AUDIT_REASON = "Afdian supporter (SWLink)"
ORDER_RE = re.compile(r"(?<!\d)\d{21,40}(?!\d)")      # Afdian numbers have 26-29 digits; Discord ids 17-20 do not match
MANAGE_ROLES, ADMINISTRATOR = 1 << 28, 1 << 3

SETTING_KEYS = ("enabled", "afdian_user_id", "role_id", "require_role_ids", "min_amount", "plan_roles",
                "dm_handling", "lookups_per_day", "notify")
_ID = re.compile(r"^\d{1,20}$")
_USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class SupporterError(HearbackError):
    """A supporter operation failed. The message is already scrubbed of tokens."""


# ---------------------------------------------------------------- small helpers

def mask(order_no: str) -> str:
    """First 4 + ... + last 4 of an order number (what notices and logs may show)."""
    s = str(order_no)
    return s[:4] + "…" + s[-4:] if len(s) > 8 else "…"


def extract_order_numbers(text: str) -> list[str]:
    """Order numbers (21 to 40 digits) in free text, in order of appearance, without repeats."""
    return list(dict.fromkeys(ORDER_RE.findall(text or "")))


def sign(token: str, params: str, ts: int, user_id: str) -> str:
    """md5(token + "params" + params + "ts" + ts + "user_id" + user_id), Afdian's documented signature."""
    return hashlib.md5(("%sparams%sts%suser_id%s" % (token, params, ts, user_id)).encode("utf-8")).hexdigest()


def parse_amount(v: Any) -> decimal.Decimal:
    s = str(v if v is not None else "").strip() or "0"
    try:
        d = decimal.Decimal(s)
    except decimal.InvalidOperation:
        raise SupporterError("the amount must be a number like 5 or 9.9") from None
    if not d.is_finite() or d < 0 or d > 10 ** 9:
        raise SupporterError("the amount must be between 0 and 1000000000")
    return d


def _amount_of(order: Mapping[str, Any]) -> decimal.Decimal:
    try:
        return parse_amount(order.get("total_amount"))
    except SupporterError:
        return decimal.Decimal(0)


def check_id(v: Any, what: str) -> str:
    s = str(v if v is not None else "").strip()
    if not _ID.match(s):
        raise SupporterError("%s must be a number (a Discord id)" % what)
    return s


def check_ids(v: Any, what: str, limit: int) -> list[str]:
    if isinstance(v, str):
        v = [x for x in re.split(r"[,\s]+", v) if x]
    if not isinstance(v, (list, tuple)):
        raise SupporterError("%s must be a list of ids" % what)
    out = list(dict.fromkeys(check_id(x, what) for x in v))
    if len(out) > limit:
        raise SupporterError("%s: at most %d ids" % (what, limit))
    return out


def _flag(v: Any, what: str) -> bool:
    if not isinstance(v, bool):
        raise SupporterError("%s must be true or false" % what)
    return v


def check_order_no(v: Any) -> str:
    s = str(v if v is not None else "").strip()
    if not ORDER_RE.fullmatch(s):
        raise SupporterError("an order number is 21 to 40 digits")
    return s


def _iso_at(now: float) -> str:
    return iso(dt.datetime.fromtimestamp(now, dt.timezone.utc))


# ---------------------------------------------------------------- the Afdian client

class AfdianClient:
    """Afdian open API: POST JSON {user_id, params (a JSON string), ts, sign} to `https://afdian.com/api/open/<name>`.

    The token is only used to compute the signature and is never sent, stored in an error message or repr'd.
    `opener(request, timeout=...)` defaults to `urllib.request.urlopen`; tests pass `hearback.fake.FakeAfdian`.
    """

    def __init__(self, user_id: str, token: str, opener: Callable[..., Any] = urllib.request.urlopen,
                 clock: Callable[[], float] = time.time):
        self.user_id = str(user_id).strip()
        self._token = str(token).strip()
        self._opener, self._clock = opener, clock
        register_secret(self._token)

    def __repr__(self) -> str:
        return "AfdianClient(user_id=%r)" % self.user_id

    def _clean(self, text: Any) -> str:
        s = str(text)
        if self._token:
            s = s.replace(self._token, "***")
        return scrub(s)

    def body(self, params: Mapping[str, Any]) -> dict:
        p = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
        ts = int(self._clock())
        return {"user_id": self.user_id, "params": p, "ts": ts, "sign": sign(self._token, p, ts, self.user_id)}

    def call(self, name: str, params: Mapping[str, Any]) -> dict:
        """One API call; returns the `data` object. Raises SupporterError (scrubbed) on any failure."""
        if name not in ENDPOINTS:
            raise SupporterError("unknown Afdian endpoint %r" % name)
        req = urllib.request.Request(AFDIAN_BASE + name, method="POST",
                                     data=json.dumps(self.body(params)).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with self._opener(req, timeout=TIMEOUT_S) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise SupporterError("Afdian answered HTTP %d" % e.code) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            raise SupporterError(self._clean("could not reach Afdian: %s" % getattr(e, "reason", e))[:200]) from None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, AttributeError):
            raise SupporterError("Afdian sent an answer that is not JSON") from None
        if not isinstance(data, dict):
            raise SupporterError("Afdian sent an unexpected answer")
        if data.get("ec") != 200:
            raise SupporterError(self._explain(data.get("ec"), data.get("em")))
        out = data.get("data")
        return out if isinstance(out, dict) else {}

    def _explain(self, ec: Any, em: Any) -> str:
        known = {400001: "Afdian says the request was incomplete",
                 400002: "Afdian says the request time is off by more than an hour: check this computer's clock",
                 400003: "Afdian could not read the request",
                 400004: "Afdian does not know this user_id or token: check both on the developer page",
                 400005: "Afdian rejected the signature: the user_id or the API token is wrong"}
        return known.get(ec) or self._clean("Afdian answered %s %s" % (ec, em))[:200]

    def ping(self) -> None:
        self.call("ping", {"a": 1})

    def find_order(self, order_no: str) -> dict | None:
        """The order with this exact number, or None."""
        data = self.call("query-order", {"page": 1, "out_trade_no": str(order_no)})
        for o in data.get("list") or []:
            if isinstance(o, dict) and str(o.get("out_trade_no")) == str(order_no):
                return o
        return None

    def recent_orders(self, pages: int = 1, per_page: int = ORDER_PAGE_SIZE) -> list[dict]:
        """Newest orders first. Afdian pages hold 50 orders and cannot be resized, so `per_page` only trims."""
        out: list[dict] = []
        for page in range(1, max(1, int(pages)) + 1):
            data = self.call("query-order", {"page": page})
            out.extend(o for o in (data.get("list") or [])[:per_page] if isinstance(o, dict))
            if page >= int(data.get("total_page") or 0):
                break
        return out


# ---------------------------------------------------------------- the store

def default_settings() -> dict:
    return {"enabled": False, "afdian_user_id": "", "role_id": "", "require_role_ids": [], "min_amount": "0",
            "plan_roles": {}, "dm_handling": False, "lookups_per_day": 10,
            "notify": {"channel_id": "", "user_ids": []}}


def clean_settings(fields: Mapping[str, Any], base: Mapping[str, Any]) -> dict:
    """Validate a partial settings update on top of `base`; unknown keys are an error, never ignored."""
    unknown = sorted(set(fields) - set(SETTING_KEYS))
    if unknown:
        raise SupporterError("unknown setting: %s" % ", ".join(unknown))
    out = json.loads(json.dumps({k: base[k] for k in SETTING_KEYS}))
    if "enabled" in fields:
        out["enabled"] = _flag(fields["enabled"], "enabled")
    if "dm_handling" in fields:
        out["dm_handling"] = _flag(fields["dm_handling"], "dm_handling")
    if "afdian_user_id" in fields:
        v = str(fields["afdian_user_id"] or "").strip()
        if v and not _USER_ID.match(v):
            raise SupporterError("the Afdian user_id has letters, digits, - and _ only")
        out["afdian_user_id"] = v
    if "role_id" in fields:
        v = str(fields["role_id"] or "").strip()
        out["role_id"] = check_id(v, "role_id") if v else ""
    if "require_role_ids" in fields:
        out["require_role_ids"] = check_ids(fields["require_role_ids"] or [], "require_role_ids", 25)
    if "min_amount" in fields:
        out["min_amount"] = format(parse_amount(fields["min_amount"]).normalize(), "f")
    if "plan_roles" in fields:
        pr = fields["plan_roles"] or {}
        if not isinstance(pr, dict) or len(pr) > 50:
            raise SupporterError("plan_roles must be an object with at most 50 plans")
        clean: dict[str, str] = {}
        for plan, role in pr.items():
            plan = str(plan).strip()
            if not plan or len(plan) > 64 or any(c.isspace() for c in plan):
                raise SupporterError("a plan id is 1 to 64 characters without spaces")
            clean[plan] = check_id(role, "a plan role")
        out["plan_roles"] = clean
    if "lookups_per_day" in fields:
        try:
            n = int(fields["lookups_per_day"])
        except (TypeError, ValueError):
            raise SupporterError("lookups_per_day must be a number") from None
        if not 1 <= n <= 200:
            raise SupporterError("lookups_per_day must be between 1 and 200")
        out["lookups_per_day"] = n
    if "notify" in fields:
        nt = fields["notify"] or {}
        if not isinstance(nt, dict) or set(nt) - {"channel_id", "user_ids"}:
            raise SupporterError("notify takes channel_id and user_ids")
        ch = str(nt.get("channel_id") or "").strip()
        out["notify"] = {"channel_id": check_id(ch, "channel_id") if ch else "",
                         "user_ids": check_ids(nt.get("user_ids") or [], "user_ids", 10)}
    return out


class SupporterStore:
    """`.hearback/supporters.json` plus the token in the keyring (service `hearback-swlink`, user `afdian-token`) or
    the environment variable `HEARBACK_AFDIAN_TOKEN`. A damaged file is an error, never a silent reset."""

    def __init__(self, data_dir: str | Path, environ: Mapping[str, str] | None = None, keyring_mod: Any = "auto"):
        self.dir = Path(data_dir)
        self.path = self.dir / "supporters.json"
        self.environ = os.environ if environ is None else environ
        self._keyring = keyring_mod          # "auto": import keyring lazily; None: never use it
        self.lock = threading.RLock()

    # ---- file
    def load(self) -> dict:
        with self.lock:
            try:
                raw = read_json_strict(self.path, None, "the supporters file")
            except SupporterError:
                raise
            except HearbackError as e:
                raise SupporterError(str(e)) from None
            if raw is not None and not isinstance(raw, dict):
                raise SupporterError("the supporters file is damaged (not an object): fix or delete %s" % self.path.name)
            return self._shape(raw or {})

    def _shape(self, raw: dict) -> dict:
        try:
            d = clean_settings({k: raw[k] for k in SETTING_KEYS if k in raw}, default_settings())
        except SupporterError as e:
            raise SupporterError("the supporters file is damaged (%s): fix or delete %s" % (e, self.path.name)) from None
        claims, retry, lookups = raw.get("claims", {}), raw.get("retry", []), raw.get("lookups", {})
        if (not isinstance(claims, dict) or not all(isinstance(v, dict) for v in claims.values())
                or not isinstance(retry, list) or not all(isinstance(x, dict) and "user" in x and "order" in x and "since" in x
                                                          for x in retry)
                or not isinstance(lookups, dict) or not all(isinstance(v, list) for v in lookups.values())):
            raise SupporterError("the supporters file is damaged (claims, retry or lookups): fix or delete %s"
                                 % self.path.name)
        d["claims"], d["retry"], d["lookups"] = claims, retry, lookups
        return d

    def save(self, d: Mapping[str, Any]) -> None:
        with self.lock:
            ensure_data_dir(self.dir)
            atomic_write_json(self.path, dict(d))

    def public(self, d: Mapping[str, Any]) -> dict:
        """The settings the UI may see: no claims, no queue, no token."""
        return {k: d[k] for k in SETTING_KEYS}

    def update(self, fields: Mapping[str, Any]) -> dict:
        with self.lock:
            d = self.load()
            d.update(clean_settings(fields, d))
            self.save(d)
            return self.public(d)

    # ---- token
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

    def get_token(self) -> str:
        kr = self._kr()
        if kr is not None:
            try:
                k = kr.get_password(KEYRING_SERVICE, TOKEN_USER)
                if k:
                    register_secret(k)
                    return k
            except Exception:
                pass
        k = (self.environ.get(ENV_TOKEN) or "").strip()
        register_secret(k)
        return k

    def has_token(self) -> bool:
        return bool(self.get_token())

    def set_token(self, token: str) -> None:
        token = (token or "").strip()
        if not token or any(c.isspace() for c in token) or len(token) > 200:
            raise SupporterError("the token is empty or contains spaces")
        kr = self._kr()
        if kr is None:
            raise SupporterError("no OS keyring available. Set the environment variable %s instead (then restart)"
                                 % ENV_TOKEN)
        try:
            kr.set_password(KEYRING_SERVICE, TOKEN_USER, token)
        except Exception as e:
            raise SupporterError("the OS keyring refused the token (%s). Use the environment variable %s instead"
                                 % (type(e).__name__, ENV_TOKEN)) from None
        register_secret(token)


# ---------------------------------------------------------------- the brain

@dataclass
class Person:
    """The member a decision is about (what the brain needs; no Discord objects)."""

    id: str
    name: str = ""
    display: str = ""
    role_ids: set = field(default_factory=set)


@dataclass
class Outcome:
    """What to do: the reply template key ("" = say nothing), roles to add, texts for the owner, the masked order."""

    reply_key: str = ""
    grant_role_ids: list = field(default_factory=list)
    notify_texts: list = field(default_factory=list)
    order: str = ""


class Supporters:
    """The supporter rules. Reads and writes the store; never talks to Discord (see SupporterBot)."""

    def __init__(self, store: SupporterStore, ledger: Any = None, clock: Callable[[], float] = time.time,
                 lang: str = "en", templates_dir: Path | None = None, opener: Callable[..., Any] | None = None):
        self.store, self.ledger, self.clock = store, ledger, clock
        self.lang, self.templates_dir = lang, templates_dir
        self.opener = opener or urllib.request.urlopen
        self._told: dict[tuple, float] = {}

    # ---- plumbing
    def make_client(self, cfg: Mapping[str, Any] | None = None) -> AfdianClient | None:
        cfg = cfg or self.store.load()
        token = self.store.get_token()
        if not cfg["afdian_user_id"] or not token:
            return None
        return AfdianClient(cfg["afdian_user_id"], token, opener=self.opener, clock=self.clock)

    def event(self, actor: str, action: str, detail: Any = None) -> None:
        if self.ledger is not None:
            try:
                self.ledger.event(actor, action, None, detail)
            except OSError:
                pass

    def text(self, name: str, **values: Any) -> str:
        return templates.render("supporter_" + name, self.lang, self.templates_dir, **values)

    def reply_text(self, key: str) -> str:
        return self.text(key) if key else ""

    @staticmethod
    def who(p: Person) -> str:
        return "%s (id %s)" % (p.display or p.name or "?", p.id)

    def _once(self, key: tuple, now: float, dry_run: bool) -> bool:
        """True the first time per day: a person repeating a bad number must not flood the owner."""
        if dry_run:
            return True
        if now - self._told.get(key, -DAY_S) < DAY_S:
            return False
        if len(self._told) > 2000:
            self._told = {k: t for k, t in self._told.items() if now - t < DAY_S}
        self._told[key] = now
        return True

    @staticmethod
    def roles_for(cfg: Mapping[str, Any], plan_id: Any) -> list[str]:
        """The role for the whole programme plus the plan's own role, if one is mapped."""
        roles = [cfg["role_id"], cfg["plan_roles"].get(str(plan_id or ""), "")]
        return list(dict.fromkeys(r for r in roles if r))

    def _commit(self, cfg: dict, now: float) -> None:
        cfg["lookups"] = {u: [t for t in ts if now - float(t) < DAY_S] for u, ts in cfg["lookups"].items()}
        cfg["lookups"] = {u: ts for u, ts in cfg["lookups"].items() if ts}
        self.store.save(cfg)

    @staticmethod
    def _drop_retry(cfg: dict, uid: str, no: str) -> bool:
        before = len(cfg["retry"])
        cfg["retry"] = [x for x in cfg["retry"] if not (str(x["user"]) == uid and str(x["order"]) == no)]
        return len(cfg["retry"]) != before

    # ---- the decision
    def handle_order(self, person: Person, order_no: str, now: float | None = None, *, retry: bool = False,
                     dry_run: bool = False) -> Outcome:
        """Decide what to do when `person` sends `order_no`. Every number a person sends uses one of their daily lookups,
        claimed or not (so claimed numbers cannot be probed). `retry=True` is the background re-check and uses none;
        `dry_run=True` changes nothing (no claim, no count, no queue)."""
        now = float(self.clock() if now is None else now)
        no = check_order_no(order_no)
        uid, masked = str(person.id), mask(no)
        with self.store.lock:
            cfg = self.store.load()
            need = set(cfg["require_role_ids"])
            if need and not need & {str(r) for r in person.role_ids}:
                return Outcome("need_verify_first", order=masked)
            if not retry:
                looks = [t for t in cfg["lookups"].get(uid, []) if now - float(t) < DAY_S]
                if len(looks) >= cfg["lookups_per_day"]:
                    return Outcome("limit", order=masked)
                if not dry_run:
                    cfg["lookups"][uid] = looks + [now]
                    self._commit(cfg, now)
            claim = cfg["claims"].get(no)
            if claim:
                return self._known(cfg, claim, person, no, now, dry_run)
            client = self.make_client(cfg)
            if client is None or not cfg["role_id"]:
                notes = ([self.text("note_off", who=self.who(person), order=masked)]
                         if self._once(("off", uid, no), now, dry_run) else [])
                return Outcome("off", notify_texts=notes, order=masked)
        try:
            order = client.find_order(no)
        except SupporterError as e:
            return self._api_down(person, no, now, retry, dry_run, str(e))
        return self._settle(person, no, order, now, dry_run)

    def _known(self, cfg: dict, claim: Mapping[str, Any], person: Person, no: str, now: float, dry_run: bool) -> Outcome:
        uid, masked = str(person.id), mask(no)
        if str(claim.get("user_id")) != uid:
            first = "%s (id %s)" % (claim.get("user_name") or "?", claim.get("user_id"))
            notes = ([self.text("note_other", who=self.who(person), order=masked, first=first)]
                     if self._once(("other", uid, no), now, dry_run) else [])
            return Outcome("claimed_by_other", notify_texts=notes, order=masked)
        if not dry_run and self._drop_retry(cfg, uid, no):
            self.store.save(cfg)
        missing = [r for r in self.roles_for(cfg, claim.get("plan_id")) if r not in {str(x) for x in person.role_ids}]
        return Outcome("ok" if missing else "ok_again", grant_role_ids=missing, order=masked)

    def _api_down(self, person: Person, no: str, now: float, retry: bool, dry_run: bool, error: str) -> Outcome:
        uid, masked = str(person.id), mask(no)
        with self.store.lock:
            cfg = self.store.load()
            item = next((x for x in cfg["retry"] if str(x["user"]) == uid and str(x["order"]) == no), None)
            who = self.who(person)
            if item is not None and now - float(item["since"]) > GIVE_UP_S:
                if not dry_run:
                    self._drop_retry(cfg, uid, no)
                    self.store.save(cfg)
                    self.event(ACTOR_BOT, "supporter_gave_up", {"order": masked, "user": uid})
                return Outcome("gave_up", notify_texts=[self.text("note_gave_up", who=who, order=masked)], order=masked)
            if item is None:
                if not dry_run:
                    cfg["retry"].append({"user": uid, "order": no, "since": now})
                    self.store.save(cfg)
                    self.event(ACTOR_BOT, "supporter_retry_queued", {"order": masked, "user": uid})
                return Outcome("retry_later", order=masked, notify_texts=[
                    self.text("note_retry", who=who, order=masked, error=templates.safe(error, 120))])
        return Outcome("" if retry else "retry_later", order=masked)

    def _settle(self, person: Person, no: str, order: Mapping[str, Any] | None, now: float, dry_run: bool) -> Outcome:
        uid, masked = str(person.id), mask(no)
        with self.store.lock:
            cfg = self.store.load()
            claim = cfg["claims"].get(no)
            if claim:                                       # someone claimed it while we were asking Afdian
                return self._known(cfg, claim, person, no, now, dry_run)
            dropped = False if dry_run else self._drop_retry(cfg, uid, no)
            if order is None or str(order.get("status")) != STATUS_PAID:
                if dropped:
                    self.store.save(cfg)
                return Outcome("not_found", order=masked)
            who = self.who(person)
            if _amount_of(order) < parse_amount(cfg["min_amount"]):
                if dropped:
                    self.store.save(cfg)
                notes = ([self.text("note_below_min", who=who, order=masked)]
                         if self._once(("min", uid, no), now, dry_run) else [])
                return Outcome("below_min", notify_texts=notes, order=masked)
            plan = str(order.get("plan_id") or "")
            if not dry_run:
                cfg["claims"][no] = {"user_id": uid, "user_name": person.name or person.display, "at": _iso_at(now),
                                     "plan_id": plan, "amount": format(_amount_of(order).normalize(), "f"),
                                     "afdian_user": str(order.get("user_id") or "")}
                self.store.save(cfg)
                self.event(ACTOR_BOT, "supporter_claim", {"order": masked, "user": uid, "plan": plan})
            have = {str(r) for r in person.role_ids}
            missing = [r for r in self.roles_for(cfg, plan) if r not in have]
            note = self.text("note_claimed" if missing else "note_claimed_had", who=who, order=masked)
            return Outcome("ok" if missing else "ok_again", grant_role_ids=missing, notify_texts=[note], order=masked)

    def simulate(self, text: str, person: Person, now: float | None = None) -> dict | None:
        """The dry run behind the console's "simulate a DM": what the bot would answer. None: no order number in `text`."""
        nums = extract_order_numbers(text)
        if not nums:
            return None
        out = self.handle_order(person, nums[0], now, dry_run=True)
        return {"order": out.order, "reply_key": out.reply_key, "reply": self.reply_text(out.reply_key),
                "grant_role_ids": list(out.grant_role_ids), "notify_texts": list(out.notify_texts)}

    # ---- owner actions
    def link_order(self, order_no: str, user_id: str, actor: str, *, user_name: str = "",
                   order: Mapping[str, Any] | None = None, now: float | None = None) -> dict:
        """Claim an order for a person by hand (the owner confirmed it). Fails if someone else holds the order."""
        now = float(self.clock() if now is None else now)
        no, uid = check_order_no(order_no), check_id(user_id, "user id")
        with self.store.lock:
            cfg = self.store.load()
            cur = cfg["claims"].get(no)
            if cur and str(cur.get("user_id")) != uid:
                raise SupporterError("order %s is already linked to %s: unlink it first"
                                     % (mask(no), cur.get("user_name") or cur.get("user_id")))
            old = cur or {}
            claim = {"user_id": uid, "user_name": user_name or old.get("user_name", ""), "at": old.get("at") or _iso_at(now),
                     "plan_id": str((order or {}).get("plan_id") or old.get("plan_id") or ""),
                     "amount": format(_amount_of(order).normalize(), "f") if order else old.get("amount", "0"),
                     "afdian_user": str((order or {}).get("user_id") or old.get("afdian_user") or "")}
            cfg["claims"][no] = claim
            cfg["retry"] = [x for x in cfg["retry"] if str(x["order"]) != no]
            self.store.save(cfg)
        self.event(actor, "supporter_link", {"order": mask(no), "user": uid})
        return claim

    def unlink(self, order_no: str, actor: str) -> dict:
        """Remove a claim. Returns the removed claim (with its `order`)."""
        no = check_order_no(order_no)
        with self.store.lock:
            cfg = self.store.load()
            claim = cfg["claims"].pop(no, None)
            if claim is None:
                raise SupporterError("order %s is not linked to anyone" % mask(no))
            self.store.save(cfg)
        self.event(actor, "supporter_unlink", {"order": mask(no), "user": str(claim.get("user_id"))})
        return {**claim, "order": no}

    def roles_to_remove(self, claim: Mapping[str, Any]) -> list[str]:
        """Roles a revoked claim gave that no other claim of the same person still justifies."""
        cfg = self.store.load()
        keep: set[str] = set()
        for other in cfg["claims"].values():
            if str(other.get("user_id")) == str(claim.get("user_id")):
                keep |= set(self.roles_for(cfg, other.get("plan_id")))
        return [r for r in self.roles_for(cfg, claim.get("plan_id")) if r not in keep]

    # ---- views
    def retry_items(self) -> list[dict]:
        return [dict(x) for x in self.store.load()["retry"]]

    def drop_retry(self, uid: str, no: str) -> None:
        with self.store.lock:
            cfg = self.store.load()
            if self._drop_retry(cfg, str(uid), str(no)):
                self.store.save(cfg)

    def summary(self) -> dict:
        cfg = self.store.load()
        return {"enabled": cfg["enabled"], "claims": len(cfg["claims"]), "retries": len(cfg["retry"])}

    def claims_view(self, amounts: bool = False) -> list[dict]:
        rows = []
        for no, c in self.store.load()["claims"].items():
            row = {"order": no, "masked": mask(no), "user_id": str(c.get("user_id", "")),
                   "user_name": str(c.get("user_name", "")), "at": str(c.get("at", "")), "plan_id": str(c.get("plan_id", ""))}
            if amounts:
                row["amount"] = str(c.get("amount", ""))
            rows.append(row)
        return sorted(rows, key=lambda r: r["at"], reverse=True)


def match_remarks(orders: Iterable[Mapping[str, Any]], members: Sequence[Mapping[str, Any]],
                  claimed: Iterable[str] = ()) -> list[dict]:
    """Suggest who an unclaimed paid order belongs to: members whose Discord username, display name or id appears in
    the order's remark. Names must be 3+ characters; the longest match wins and ties are all returned. Suggestions
    only: the owner confirms each one, nothing is granted from here. Each suggestion carries the order's remark and
    the buyer's Afdian nickname (plain text, cut short) so the owner can see why it was suggested."""
    taken = {str(x) for x in claimed}
    out: list[dict] = []
    for o in orders:
        no = str(o.get("out_trade_no") or "")
        remark = str(o.get("remark") or "")
        if not no or no in taken or str(o.get("status")) != STATUS_PAID or not remark.strip():
            continue
        low = remark.lower()
        best: list[tuple[int, dict, str]] = []
        for m in members:
            mid = str(m.get("id") or "")
            hits = []
            if len(mid) >= 15 and re.search(r"(?<!\d)%s(?!\d)" % re.escape(mid), remark):
                hits.append((1000 + len(mid), "id"))
            for how in ("username", "display"):
                val = str(m.get(how) or "").strip().lower()
                if len(val) >= 3 and val in low:
                    hits.append((len(val), how))
            if hits:
                score, how = max(hits)
                best.append((score, m, how))
        if not best:
            continue
        top = max(s for s, _m, _h in best)
        for score, m, how in best:
            if score == top:
                out.append({"order": no, "user_id": str(m.get("id")), "name": str(m.get("display") or m.get("username") or ""),
                            "match": how, "remark": remark.strip()[:REMARK_SHOWN], "buyer": str(o.get("user_name") or "")[:80]})
    return out


# ---------------------------------------------------------------- Discord side

def person_from_member(m: Mapping[str, Any]) -> Person:
    u = m.get("user") or {}
    display = m.get("nick") or u.get("global_name") or u.get("username") or ""
    return Person(id=str(u.get("id") or ""), name=str(u.get("username") or ""), display=str(display),
                  role_ids={str(r) for r in m.get("roles") or []})


def describe_roles(roles: Sequence[Mapping[str, Any]], bot_member: Mapping[str, Any], guild_id: str) -> dict:
    """Which roles the bot may hand out: not @everyone, not managed by an integration, strictly below the bot's own top
    role, and only if the bot has Manage Roles (or Administrator). `bot_top` is the bot's highest role."""
    by_id = {str(r["id"]): r for r in roles}
    mine = [by_id[i] for i in (str(x) for x in bot_member.get("roles") or []) if i in by_id]
    perms = 0
    for r in mine + ([by_id[str(guild_id)]] if str(guild_id) in by_id else []):
        perms |= int(r.get("permissions") or 0)
    can_manage = bool(perms & (MANAGE_ROLES | ADMINISTRATOR))
    top = max(mine, key=lambda r: int(r.get("position", 0)), default=None)
    top_pos = int(top.get("position", 0)) if top else 0
    items = []
    for r in sorted(roles, key=lambda r: int(r.get("position", 0)), reverse=True):
        rid = str(r["id"])
        if rid == str(guild_id):
            continue
        managed = bool(r.get("managed"))
        items.append({"id": rid, "name": str(r.get("name") or ""), "position": int(r.get("position", 0)),
                      "managed": managed, "assignable": can_manage and not managed and int(r.get("position", 0)) < top_pos})
    return {"items": items, "can_manage_roles": can_manage,
            "bot_top": {"id": str(top["id"]), "name": str(top.get("name") or ""), "position": top_pos} if top else None}


class SupporterBot:
    """Applies the brain's decisions on Discord over REST, so it works with or without the gateway."""

    def __init__(self, sup: Supporters, client_factory: Callable[[], DiscordClient], guild_fn: Callable[[], str],
                 bot_id_fn: Callable[[], str]):
        self.sup, self.client_factory, self.guild_fn, self.bot_id_fn = sup, client_factory, guild_fn, bot_id_fn
        self.issues: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- REST helpers
    def member(self, client: DiscordClient, guild: str, uid: str) -> Person | None:
        try:
            return person_from_member(client.request("GET", "/guilds/%s/members/%s" % (guild, uid)))
        except DiscordError as e:
            if e.status == 404:
                return None
            raise

    def roles_info(self, client: DiscordClient, guild: str) -> dict:
        roles = client.request("GET", "/guilds/%s/roles" % guild)
        bot = client.request("GET", "/guilds/%s/members/%s" % (guild, self.bot_id_fn()))
        return describe_roles(roles, bot, guild)

    @staticmethod
    def say(client: DiscordClient, channel: str, text: str) -> None:
        client.request("POST", "/channels/%s/messages" % channel, body=msg_body(text[:2000], []))

    @staticmethod
    def dm_channel(client: DiscordClient, uid: str) -> str:
        return str(client.request("POST", "/users/@me/channels", body={"recipient_id": str(uid)})["id"])

    # ---- granting
    def grant(self, client: DiscordClient, guild: str, uid: str, role_ids: Sequence[str],
              actor: str = ACTOR_BOT) -> tuple[list[str], list[dict]]:
        """PUT each role with the audit-log reason. Returns (given, failures); a failure is
        {kind: hierarchy | no_permission | managed | missing | discord, role_id, role_name, error?}."""
        info = self.roles_info(client, guild)
        by_id = {r["id"]: r for r in info["items"]}
        done: list[str] = []
        failed: list[dict] = []
        for rid in role_ids:
            role = by_id.get(str(rid))
            f: dict = {"role_id": str(rid), "role_name": role["name"] if role else str(rid),
                       "bot_role": (info["bot_top"] or {}).get("name", "")}
            if role is None:
                failed.append({**f, "kind": "missing"})
            elif role["managed"]:
                failed.append({**f, "kind": "managed"})
            elif not info["can_manage_roles"]:
                failed.append({**f, "kind": "no_permission"})
            elif not role["assignable"]:
                failed.append({**f, "kind": "hierarchy"})
            else:
                try:
                    client.request("PUT", "/guilds/%s/members/%s/roles/%s" % (guild, uid, rid), reason=AUDIT_REASON)
                    done.append(str(rid))
                except DiscordError as e:
                    failed.append({**f, "kind": "hierarchy" if e.status == 403 else "discord", "error": scrub(e)[:200]})
        self._track(done, failed)
        for rid in done:
            self.sup.event(actor, "supporter_grant", {"user": str(uid), "role": rid})
        for f in failed:
            self.sup.event(actor, "supporter_grant_failed", {"user": str(uid), "role": f["role_id"], "kind": f["kind"]})
        return done, failed

    def remove(self, client: DiscordClient, guild: str, uid: str, role_ids: Sequence[str],
               actor: str = ACTOR_WEB) -> tuple[list[str], list[str]]:
        """DELETE each role from the member. Returns (removed, failed role ids)."""
        done, failed = [], []
        for rid in role_ids:
            try:
                client.request("DELETE", "/guilds/%s/members/%s/roles/%s" % (guild, uid, rid), reason=AUDIT_REASON)
                done.append(str(rid))
            except DiscordError:
                failed.append(str(rid))
        for rid in done:
            self.sup.event(actor, "supporter_role_removed", {"user": str(uid), "role": rid})
        return done, failed

    def _track(self, done: Sequence[str], failed: Sequence[Mapping[str, Any]]) -> None:
        """Keep the console's list of "fix this" problems: add new ones, drop a role's once it worked."""
        with self._lock:
            self.issues = [i for i in self.issues if i["role_id"] not in set(done)]
            for f in failed:
                if f["kind"] == "discord" or any(i["role_id"] == f["role_id"] and i["kind"] == f["kind"] for i in self.issues):
                    continue
                self.issues.append({"kind": f["kind"], "role_id": f["role_id"], "role_name": f["role_name"],
                                    "bot_role": f.get("bot_role", ""), "at": _iso_at(self.sup.clock())})
            self.issues = self.issues[-10:]

    def issues_view(self) -> list[dict]:
        with self._lock:
            return [dict(i) for i in self.issues]

    def _failure_note(self, f: Mapping[str, Any], who: str) -> str:
        kind, role = f["kind"], f["role_name"]
        if kind == "hierarchy":
            return self.sup.text("note_hierarchy", who=who, role=role)
        if kind == "no_permission":
            return self.sup.text("note_no_permission", who=who, role=role)
        why = {"missing": "the role no longer exists: pick the supporter role again on the Supporters page",
               "managed": "roles that belong to an integration cannot be handed out"}.get(kind) or f.get("error", "unknown error")
        return self.sup.text("note_grant_failed", who=who, role=role, error=templates.safe(why, 120))

    # ---- applying a decision
    def notify(self, client: DiscordClient, texts: Sequence[str]) -> None:
        """Tell the owner: the notify channel if one is set, else a DM to each listed user. Mentions are off."""
        if not texts:
            return
        target = self.sup.store.load()["notify"]
        for text in texts:
            try:
                if target["channel_id"]:
                    self.say(client, target["channel_id"], text)
                else:
                    for uid in target["user_ids"]:
                        self.say(client, self.dm_channel(client, uid), text)
            except HearbackError as e:
                self.sup.event(ACTOR_BOT, "supporter_notify_failed", {"error": scrub(e)[:200]})

    def apply(self, client: DiscordClient, guild: str, person: Person, out: Outcome, channel: str) -> dict:
        """Give the roles first, then answer: a role that could not be given turns the reply into `grant_failed`."""
        key, notes = out.reply_key, list(out.notify_texts)
        done: list[str] = []
        failed: list[dict] = []
        if out.grant_role_ids:
            done, failed = self.grant(client, guild, person.id, out.grant_role_ids)
            if failed:
                key = "grant_failed"
                notes += [self._failure_note(f, self.sup.who(person)) for f in failed]
        if key:
            try:
                self.say(client, channel, self.sup.reply_text(key))
            except HearbackError as e:
                self.sup.event(ACTOR_BOT, "supporter_reply_failed", {"user": person.id, "error": scrub(e)[:200]})
        self.notify(client, notes)
        return {"reply_key": key, "granted": done, "failed": failed}

    def dm_on(self) -> bool:
        """Whether order-number DMs are handled here (on, and DM handling on). A damaged file says yes, so that
        `handle_dm` logs it and falls back to the normal rules."""
        try:
            cfg = self.sup.store.load()
        except SupporterError:
            return True
        return bool(cfg["enabled"] and cfg["dm_handling"])

    def handle_dm(self, author_id: str, channel_id: str, content: str) -> bool:
        """A DM with an order number. True when this handled it (the normal bot rules must then skip it)."""
        nums = extract_order_numbers(content)
        if not nums:
            return False
        try:
            cfg = self.sup.store.load()
        except SupporterError as e:
            self.sup.event(ACTOR_BOT, "supporter_error", {"error": scrub(e)[:300]})
            return False
        if not (cfg["enabled"] and cfg["dm_handling"]):
            return False
        try:
            client = self.client_factory()
            guild = self.guild_fn()
            if not guild:
                out = Outcome("off", notify_texts=[self.sup.text("note_off", who=self.sup.who(Person(id=str(author_id))),
                                                                order=mask(nums[0]))], order=mask(nums[0]))
                self.apply(client, "", Person(id=str(author_id)), out, channel_id)
                return True
            person = self.member(client, guild, author_id)
            if person is None:
                self.say(client, channel_id, self.sup.reply_text("not_member"))
                return True
            self.apply(client, guild, person, self.sup.handle_order(person, nums[0]), channel_id)
        except HearbackError as e:
            self.sup.event(ACTOR_BOT, "supporter_error", {"error": scrub(e)[:300]})
        return True

    # ---- the retry loop
    def retry_tick(self) -> int:
        """Re-check every queued lookup once. Returns how many were tried. Needs REST only (no gateway)."""
        try:
            cfg = self.sup.store.load()
        except SupporterError:
            return 0
        if not cfg["enabled"] or not cfg["retry"]:
            return 0
        tried = 0
        try:
            client, guild = self.client_factory(), self.guild_fn()
        except HearbackError:
            return 0
        for item in cfg["retry"]:
            uid, no = str(item["user"]), str(item["order"])
            try:
                person = self.member(client, guild, uid) if guild else None
                if person is None:
                    self.sup.drop_retry(uid, no)
                    self.notify(client, [self.sup.text("note_left", who="id %s" % uid, order=mask(no))])
                    continue
                out = self.sup.handle_order(person, no, retry=True)
                if out.reply_key in ("need_verify_first", "off"):
                    self.sup.drop_retry(uid, no)     # answered once; otherwise this would repeat every round
                self.apply(client, guild, person, out, self.dm_channel(client, uid) if out.reply_key else "")
                tried += 1
            except HearbackError as e:
                self.sup.event(ACTOR_BOT, "supporter_error", {"error": scrub(e)[:300]})
        return tried

    def start(self, interval: float = RETRY_EVERY_S) -> None:
        """Run `retry_tick` every `interval` seconds in a daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    self.retry_tick()
                except Exception as e:      # the loop must survive anything
                    self.sup.event(ACTOR_BOT, "supporter_error", {"error": scrub("%s: %s" % (type(e).__name__, e))[:300]})

        self._thread = threading.Thread(target=loop, name="swlink-supporters", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
