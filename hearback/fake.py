"""FakeDiscord: an in-memory Discord REST surface for tests and `hearback demo`.

It plugs into `DiscordClient` as the transport, so the real client code (headers, JSON, multipart,
429 handling, CDN downloads) runs against it. It implements exactly the calls Hearback makes and
enforces the Discord limits that matter (2000 characters, 5 tags, 100-character titles, auth).

    fake = FakeDiscord()
    forum = fake.add_forum("bug-reports", ["Crash", "UI"])
    tid = fake.add_thread(forum, "Oven freezes", "Mira", "It stops at 0:00", tags=["Crash"])
    client = fake.client()
"""
from __future__ import annotations

import datetime as dt
import json
import mimetypes
import re
import struct
import urllib.parse
import zlib
from collections import deque
from typing import Any, NamedTuple, Sequence

from hearback.discord import API_BASE, DiscordClient, DownloadTooLarge
from hearback.util import make_snowflake

START_MS = int(dt.datetime(2026, 10, 20, 8, 0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)


class Call(NamedTuple):
    method: str
    path: str
    params: dict
    body: Any


class _HTTP(Exception):
    def __init__(self, status: int, message: str, code: int = 0):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def make_png(width: int = 24, height: int = 16, rgb: tuple[int, int, int] = (240, 120, 60)) -> bytes:
    """A small valid PNG (solid colour) for fake screenshots."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def parse_multipart(content_type: str, data: bytes) -> tuple[dict, list[tuple[str, bytes, str]]]:
    """-> (payload_json dict, [(filename, bytes, content_type), ...] ordered by files[i])."""
    m = re.search(r'boundary=([^;]+)', content_type)
    if not m:
        raise _HTTP(400, "multipart without boundary")
    boundary = m.group(1).strip('"').encode()
    payload: dict = {}
    files: dict[int, tuple[str, bytes, str]] = {}
    for part in data.split(b"--" + boundary)[1:]:
        if part.startswith(b"--"):
            break
        part = part[2:] if part.startswith(b"\r\n") else part
        part = part[:-2] if part.endswith(b"\r\n") else part
        head, _, body = part.partition(b"\r\n\r\n")
        headers = head.decode("utf-8", "replace")
        name = re.search(r'name="([^"]*)"', headers)
        if not name:
            continue
        if name.group(1) == "payload_json":
            payload = json.loads(body.decode("utf-8"))
        else:
            idx = re.fullmatch(r"files\[(\d+)\]", name.group(1))
            fn = re.search(r'filename="([^"]*)"', headers)
            ct = re.search(r"Content-Type:\s*([^\r\n]+)", headers, re.I)
            if idx and fn:
                files[int(idx.group(1))] = (fn.group(1), body, ct.group(1).strip() if ct else "application/octet-stream")
    return payload, [files[i] for i in sorted(files)]


class FakeDiscord:
    """Callable transport + seeding helpers + call log."""

    def __init__(self, token: str = "fake-token-not-a-secret", page_size: int = 1):
        self.token = token
        self.page_size = page_size            # archived-thread page size (small on purpose: exercises paging)
        self._ms = START_MS
        self._seq = 0
        self.bot = self._user("Hearback Bot", bot=True)
        self.users: dict[str, dict] = {self.bot["id"]: self.bot}
        self.guild_id = self.new_id()
        self.channels: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}
        self.blobs: dict[str, bytes] = {}
        self.reactions: dict[tuple[str, str, str], list[str]] = {}
        self.calls: list[Call] = []
        self.cdn_requests: list[tuple[str, dict]] = []
        self.faults: deque[dict] = deque()

    # ------------------------------------------------------------ ids / time
    def new_id(self) -> str:
        self._ms += 1000
        self._seq = (self._seq + 1) % 4096
        return make_snowflake(self._ms, self._seq)

    def tick(self, seconds: float = 60) -> None:
        self._ms += int(seconds * 1000)

    def _now(self) -> str:
        return dt.datetime.fromtimestamp(self._ms / 1000.0, tz=dt.timezone.utc).isoformat(timespec="microseconds")

    def _user(self, name: str, bot: bool = False) -> dict:
        u = {"id": self.new_id(), "username": name.lower().replace(" ", ""),
             "global_name": name}
        if bot:
            u["bot"] = True
        return u

    # ------------------------------------------------------------ seeding
    def add_user(self, name: str) -> dict:
        for u in self.users.values():
            if u["global_name"] == name:
                return u
        u = self._user(name)
        self.users[u["id"]] = u
        return u

    def add_forum(self, name: str = "bug-reports", tags: Sequence[str] = ()) -> str:
        cid = self.new_id()
        self.channels[cid] = {"id": cid, "type": 15, "name": name, "guild_id": self.guild_id,
                              "available_tags": [{"id": self.new_id(), "name": t, "moderated": False,
                                                  "emoji_id": None, "emoji_name": None} for t in tags]}
        return cid

    def add_channel(self, name: str) -> str:
        cid = self.new_id()
        self.channels[cid] = {"id": cid, "type": 0, "name": name, "guild_id": self.guild_id}
        self.messages[cid] = []
        return cid

    def tag_id(self, forum_id: str, name: str) -> str:
        return next(t["id"] for t in self.channels[forum_id]["available_tags"] if t["name"] == name)

    def _blob(self, channel_id: str, file: tuple) -> dict:
        name, data = file[0], file[1]
        ctype = file[2] if len(file) > 2 else (mimetypes.guess_type(name)[0] or "application/octet-stream")
        aid = self.new_id()
        url = "https://cdn.discordapp.com/attachments/%s/%s/%s" % (channel_id, aid, urllib.parse.quote(name))
        self.blobs[url] = data
        return {"id": aid, "filename": name, "size": len(data), "content_type": ctype, "url": url}

    def _add_message(self, channel_id: str, author: dict, content: str, files: Sequence[tuple] = (),
                     mid: str | None = None) -> dict:
        mid = mid or self.new_id()
        msg = {"id": mid, "channel_id": channel_id, "type": 0, "content": content, "timestamp": self._now(),
               "edited_timestamp": None, "author": author,
               "attachments": [self._blob(channel_id, f) for f in files]}
        self.messages.setdefault(channel_id, []).append(msg)
        th = self.channels.get(channel_id)
        if th and th["type"] == 11:
            th["last_message_id"] = mid
            th["message_count"] = max(len(self.messages[channel_id]) - 1, 0)
            th["total_message_sent"] = th.get("total_message_sent", 0) + 1
        return msg

    def _thread(self, forum_id: str, title: str, owner: dict, tag_ids: Sequence[str], tid: str | None = None) -> dict:
        tid = tid or self.new_id()
        th = {"id": tid, "type": 11, "name": title, "guild_id": self.guild_id, "parent_id": forum_id,
              "owner_id": owner["id"], "applied_tags": list(tag_ids), "flags": 0, "message_count": 0,
              "last_message_id": tid, "thread_metadata": {"archived": False, "archive_timestamp": self._now()}}
        self.channels[tid] = th
        self.messages[tid] = []
        return th

    def add_thread(self, forum_id: str, title: str, author: str | dict, body: str, tags: Sequence[str] = (),
                   files: Sequence[tuple] = (), archived: bool = False, gap: float = 600) -> str:
        """Seed a forum post (the starter message has the same id as the thread)."""
        self.tick(gap)
        owner = self.add_user(author) if isinstance(author, str) else author
        th = self._thread(forum_id, title, owner, [self.tag_id(forum_id, t) for t in tags])
        self._add_message(th["id"], owner, body, files, mid=th["id"])
        if archived:
            self.set_archived(th["id"], True)
        return th["id"]

    def add_reply(self, thread_id: str, author: str | dict, content: str, files: Sequence[tuple] = (),
                  gap: float = 300) -> str:
        self.tick(gap)
        user = self.add_user(author) if isinstance(author, str) else author
        return self._add_message(thread_id, user, content, files)["id"]

    def set_archived(self, tid: str, archived: bool = True) -> None:
        meta = self.channels[tid]["thread_metadata"]
        meta["archived"] = archived
        meta["archive_timestamp"] = self._now()

    def react(self, channel_id: str, message_id: str, emoji: str, user_id: str) -> None:
        self.reactions.setdefault((channel_id, message_id, emoji), []).append(user_id)

    def message(self, channel_id: str, mid: str) -> dict | None:
        return next((m for m in self.messages.get(channel_id, []) if m["id"] == mid), None)

    def fail_next(self, match: str, status: int, body: dict | None = None, times: int = 1, after: int = 0) -> None:
        """Make `times` requests whose 'METHOD /path' matches the regex fail with `status`,
        after letting the first `after` matching requests through."""
        self.faults.append({"match": re.compile(match), "status": status, "body": body or {"message": "injected"},
                            "times": times, "after": after})

    def client(self, **kw: Any) -> DiscordClient:
        kw.setdefault("sleep", lambda s: None)
        return DiscordClient(self.token, transport=self, **kw)

    # ------------------------------------------------------------ introspection
    def writes(self) -> list[Call]:
        return [c for c in self.calls if c.method in ("POST", "PUT", "PATCH", "DELETE")]

    def count(self, method: str, path_regex: str) -> int:
        return sum(1 for c in self.calls if c.method == method and re.fullmatch(path_regex, c.path))

    # ------------------------------------------------------------ transport
    def __call__(self, method: str, url: str, headers: dict, data: bytes | None = None,
                 timeout: float = 30, max_bytes: int | None = None) -> tuple[int, bytes]:
        p = urllib.parse.urlsplit(url)
        if p.hostname and p.hostname.endswith(("discordapp.com", "discordapp.net")):
            self.cdn_requests.append((url, dict(headers)))
            if any(k.lower() == "authorization" for k in headers):
                return 403, b'{"message": "credentials must never be sent to the CDN"}'
            if url in self.blobs:
                if max_bytes is not None and len(self.blobs[url]) > max_bytes:
                    raise DownloadTooLarge("body went past the cap")
                return 200, self.blobs[url]
            return 404, b'{"message": "no such attachment"}'
        base = urllib.parse.urlsplit(API_BASE)
        if not p.path.startswith(base.path):
            return 404, b'{"message": "unknown host/path"}'
        path = p.path[len(base.path):]
        params = {k: v[0] for k, v in urllib.parse.parse_qs(p.query).items()}
        ctype = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
        body: Any = None
        files: list = []
        if data:
            if ctype.startswith("multipart/"):
                body, files = parse_multipart(ctype, data)
            else:
                body = json.loads(data.decode("utf-8"))
        self.calls.append(Call(method, path, params, body))
        auth = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
        try:
            if auth != "Bot " + self.token:
                raise _HTTP(401, "401: Unauthorized", 0)
            for f in list(self.faults):
                if f["match"].search("%s %s" % (method, path)):
                    if f["after"] > 0:
                        f["after"] -= 1
                        continue
                    f["times"] -= 1
                    if f["times"] <= 0:
                        self.faults.remove(f)
                    return f["status"], json.dumps(f["body"]).encode()
            result = self._route(method, urllib.parse.unquote(path), params, body, files)
        except _HTTP as e:
            return e.status, json.dumps({"message": e.message, "code": e.code}).encode()
        return (204, b"") if result is None else (200, json.dumps(result).encode())

    # ------------------------------------------------------------ routes
    def _route(self, method: str, path: str, params: dict, body: Any, files: list) -> Any:
        if method == "GET" and path == "/users/@me":
            return self.bot
        if method == "GET" and path == "/users/@me/guilds":
            return [{"id": self.guild_id, "name": "Fake Guild"}]
        m = re.fullmatch(r"/guilds/(\d+)/channels", path)
        if method == "GET" and m:
            if m.group(1) != self.guild_id:
                raise _HTTP(404, "Unknown Guild", 10004)
            return [c for c in self.channels.values() if c["type"] != 11]
        m = re.fullmatch(r"/guilds/(\d+)/threads/active", path)
        if method == "GET" and m:
            return {"threads": [t for t in self.channels.values()
                                if t["type"] == 11 and not t["thread_metadata"]["archived"]], "members": []}
        m = re.fullmatch(r"/channels/(\d+)/threads/archived/public", path)
        if method == "GET" and m:
            arch = sorted((t for t in self.channels.values() if t["type"] == 11 and t["parent_id"] == m.group(1)
                           and t["thread_metadata"]["archived"]),
                          key=lambda t: t["thread_metadata"]["archive_timestamp"], reverse=True)
            if params.get("before"):
                arch = [t for t in arch if t["thread_metadata"]["archive_timestamp"] < params["before"]]
            n = min(int(params.get("limit", 100)), self.page_size)
            return {"threads": arch[:n], "members": [], "has_more": len(arch) > n}
        m = re.fullmatch(r"/channels/(\d+)/threads", path)
        if method == "POST" and m:
            return self._create_thread(m.group(1), body or {}, files)
        m = re.fullmatch(r"/channels/(\d+)/messages", path)
        if m:
            cid = self._channel(m.group(1))["id"]
            if method == "GET":
                msgs = sorted(self.messages.get(cid, []), key=lambda x: int(x["id"]), reverse=True)
                if params.get("before"):
                    msgs = [x for x in msgs if int(x["id"]) < int(params["before"])]
                return msgs[:min(int(params.get("limit", 50)), 100)]
            if method == "POST":
                content = (body or {}).get("content", "")
                self._check_message(content, files)
                return self._add_message(cid, self.bot, content, files)
        m = re.fullmatch(r"/channels/(\d+)/messages/(\d+)", path)
        if m:
            cid, mid = self._channel(m.group(1))["id"], m.group(2)
            msg = self.message(cid, mid)
            if msg is None:
                raise _HTTP(404, "Unknown Message", 10008)
            if method == "GET":
                return msg
            if method == "PATCH":
                content = (body or {}).get("content", msg["content"])
                self._check_message(content, msg["attachments"])
                msg["content"], msg["edited_timestamp"] = content, self._now()
                return msg
            if method == "DELETE":
                self.messages[cid].remove(msg)
                th = self.channels[cid]
                if th["type"] == 11:   # like Discord: message_count drops, last_message_id and total_message_sent do not
                    th["message_count"] = max(len(self.messages[cid]) - 1, 0)
                return None
        m = re.fullmatch(r"/channels/(\d+)/messages/(\d+)/reactions/(.+)/@me", path)
        if method == "PUT" and m:
            if self.message(m.group(1), m.group(2)) is None:
                raise _HTTP(404, "Unknown Message", 10008)
            self.react(m.group(1), m.group(2), m.group(3), self.bot["id"])
            return None
        m = re.fullmatch(r"/channels/(\d+)/messages/(\d+)/reactions/([^/]+)", path)
        if method == "GET" and m:
            if self.message(m.group(1), m.group(2)) is None:
                raise _HTTP(404, "Unknown Message", 10008)
            ids = self.reactions.get((m.group(1), m.group(2), m.group(3)), [])
            return [self.users[i] for i in dict.fromkeys(ids)][: int(params.get("limit", 25))]
        m = re.fullmatch(r"/channels/(\d+)", path)
        if m:
            ch = self._channel(m.group(1))
            if method == "GET":
                return ch
            if method == "PATCH":
                return self._patch_channel(ch, body or {})
        raise AssertionError("FakeDiscord does not implement: %s %s" % (method, path))

    def _channel(self, cid: str) -> dict:
        if cid not in self.channels:
            raise _HTTP(404, "Unknown Channel", 10003)
        return self.channels[cid]

    @staticmethod
    def _check_message(content: str, files: Sequence) -> None:
        if len(content) > 2000:
            raise _HTTP(400, "Invalid Form Body: content: Must be 2000 or fewer in length.", 50035)
        if not content.strip() and not files:
            raise _HTTP(400, "Cannot send an empty message", 50006)

    def _check_tags(self, forum_id: str, ids: Sequence[Any]) -> list[str]:
        ids = [str(i) for i in ids]
        if len(ids) > 5:
            raise _HTTP(400, "Invalid Form Body: applied_tags: Must be 5 or fewer in length.", 50035)
        valid = {t["id"] for t in self.channels[forum_id]["available_tags"]}
        if any(i not in valid for i in ids):
            raise _HTTP(400, "Invalid Form Body: applied_tags: unknown tag", 50035)
        return ids

    def _create_thread(self, forum_id: str, body: dict, files: list) -> dict:
        forum = self._channel(forum_id)
        if forum["type"] != 15:
            raise _HTTP(400, "Channel is not a forum", 50024)
        name = body.get("name", "")
        if not name or len(name) > 100:
            raise _HTTP(400, "Invalid Form Body: name: must be 1-100 in length.", 50035)
        msg = body.get("message") or {}
        self._check_message(msg.get("content", ""), files)
        tags = self._check_tags(forum_id, body.get("applied_tags") or [])
        th = self._thread(forum_id, name, self.bot, tags)
        starter = self._add_message(th["id"], self.bot, msg.get("content", ""), files, mid=th["id"])
        return dict(th, message=starter)

    def _patch_channel(self, ch: dict, body: dict) -> dict:
        if ch["type"] != 11:
            raise _HTTP(400, "Only threads can be patched in the fake", 50035)
        if "applied_tags" in body:
            ch["applied_tags"] = self._check_tags(ch["parent_id"], body["applied_tags"])
        if "flags" in body:
            ch["flags"] = int(body["flags"])
        if "name" in body:
            ch["name"] = body["name"][:100]
        if "archived" in body:
            self.set_archived(ch["id"], bool(body["archived"]))
        return ch
