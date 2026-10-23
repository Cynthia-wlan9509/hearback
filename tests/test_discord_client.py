"""DiscordClient: retries, 429, multipart, CDN allow-list, secrets."""
from __future__ import annotations

import email.parser
import email.policy
import json

import pytest

from hearback.discord import (DiscordClient, DiscordError, build_multipart, is_cdn_url, load_attachments,
                              msg_body)
from hearback.fake import FakeDiscord, make_png, parse_multipart
from hearback.util import HearbackError


def test_request_sends_auth_and_user_agent():
    seen = {}

    def transport(method, url, headers, data, timeout):
        seen.update(method=method, url=url, headers=headers, data=data)
        return 200, b'{"id": "1"}'

    c = DiscordClient("tok", transport=transport)
    assert c.request("POST", "/x", {"a": 1}, {"k": "v"}) == {"id": "1"}
    assert seen["headers"]["Authorization"] == "Bot tok"
    assert seen["headers"]["User-Agent"].startswith("DiscordBot (hearback")
    assert seen["headers"]["Content-Type"] == "application/json"
    assert seen["url"] == "https://discord.com/api/v10/x?a=1"
    assert json.loads(seen["data"]) == {"k": "v"}


def test_429_waits_retry_after_then_succeeds():
    fake = FakeDiscord()
    fake.fail_next(r"GET /users/@me", 429, {"retry_after": 2.5}, times=2)
    sleeps: list[float] = []
    c = fake.client(sleep=sleeps.append)
    assert c.me()["id"] == fake.bot["id"]
    assert sleeps == [pytest.approx(2.6), pytest.approx(2.6)]
    assert fake.count("GET", "/users/@me") == 3


def test_429_gives_up_after_max_retry():
    fake = FakeDiscord()
    fake.fail_next(r"GET /users/@me", 429, {"retry_after": 0.1}, times=99)
    sleeps: list[float] = []
    c = fake.client(sleep=sleeps.append, max_retry=2)
    with pytest.raises(DiscordError) as ei:
        c.me()
    assert ei.value.status == 429 and len(sleeps) == 2


def test_error_status_raises_with_short_body_and_hint():
    fake = FakeDiscord()
    fake.fail_next(r"GET /users/@me", 403, {"message": "x" * 2000})
    with pytest.raises(DiscordError) as ei:
        fake.client().me()
    msg = str(ei.value)
    assert ei.value.status == 403 and "HTTP 403 GET /users/@me" in msg and len(msg) < 500
    assert "permission" in msg


def test_wrong_token_is_401():
    fake = FakeDiscord()
    with pytest.raises(DiscordError) as ei:
        DiscordClient("not-the-token", transport=fake).me()
    assert ei.value.status == 401 and "token" in str(ei.value)


def test_token_never_appears_in_errors_or_repr():
    secret = "tok_SECRET_value_12345"

    def echo(method, url, headers, data, timeout):  # a hostile server that echoes the Authorization header
        return 400, json.dumps({"message": "bad", "echo": headers["Authorization"]}).encode()

    c = DiscordClient(secret, transport=echo)
    with pytest.raises(DiscordError) as ei:
        c.me()
    assert secret not in str(ei.value) and "***" in str(ei.value)
    assert secret not in repr(c) and secret not in str(vars(c).get("base_url"))


def test_multipart_is_valid_and_binary_safe(tmp_path):
    blob = tmp_path / "a b.bin"
    blob.write_bytes(b"\x00\r\n--not-a-boundary\r\n\xff" * 3)
    png = tmp_path / "s.png"
    png.write_bytes(make_png())
    atts = load_attachments([blob, png])
    body, ctype = build_multipart(msg_body("héllo ✅", atts), atts)
    # independent check with the stdlib MIME parser
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body)
    parts = list(msg.iter_parts())
    assert [p.get_param("name", header="content-disposition") for p in parts] == ["payload_json", "files[0]", "files[1]"]
    assert json.loads(parts[0].get_content())["content"] == "héllo ✅"
    assert parts[1].get_filename() == "a b.bin" and parts[1].get_payload(decode=True) == blob.read_bytes()
    assert parts[2].get_content_type() == "image/png"
    # and with the fake's own parser
    payload, files = parse_multipart(ctype, body)
    assert payload["attachments"] == [{"id": 0, "filename": "a b.bin"}, {"id": 1, "filename": "s.png"}]
    assert files[0][1] == blob.read_bytes()


def test_upload_through_client_reaches_fake_with_attachment(tmp_path):
    fake = FakeDiscord()
    ch = fake.add_channel("c")
    f = tmp_path / "log.txt"
    f.write_bytes(b"line1\nline2")
    atts = load_attachments([f])
    m = fake.client().request("POST", "/channels/%s/messages" % ch, body=msg_body("see log", atts), files=atts)
    assert m["content"] == "see log" and m["attachments"][0]["filename"] == "log.txt"
    assert fake.blobs[m["attachments"][0]["url"]] == b"line1\nline2"


def test_load_attachments_validates_before_any_request(tmp_path):
    with pytest.raises(HearbackError, match="not found"):
        load_attachments([tmp_path / "missing.png"])
    big = tmp_path / "big.bin"
    big.write_bytes(bytes(10 * 1024 * 1024 + 1))
    with pytest.raises(HearbackError, match="upload limit"):
        load_attachments([big])
    small = tmp_path / "s.txt"
    small.write_text("x")
    with pytest.raises(HearbackError, match="allows 10"):
        load_attachments([small] * 11)


@pytest.mark.parametrize("url,ok", [
    ("https://cdn.discordapp.com/attachments/1/2/a.png", True),
    ("https://media.discordapp.net/attachments/1/2/a.png", True),
    ("https://discord.com/x", True),
    ("http://cdn.discordapp.com/attachments/1/2/a.png", False),
    ("https://evil.example/cdn.discordapp.com/a.png", False),
    ("https://cdn.discordapp.com.evil.example/a.png", False),
    ("https://notdiscordapp.com/a.png", False),
    ("https://cdn.discordapp.com@evil.example/a.png", False),
    ("file:///etc/passwd", False),
])
def test_cdn_allow_list(url, ok):
    assert is_cdn_url(url) is ok


def test_download_refuses_foreign_hosts_without_any_request(tmp_path):
    calls = []
    c = DiscordClient("tok", transport=lambda *a: calls.append(a) or (200, b"x"))
    with pytest.raises(DiscordError, match="non-Discord host"):
        c.download("https://evil.example/a.png", tmp_path / "a.png")
    assert calls == [] and not (tmp_path / "a.png").exists()


def test_download_has_no_authorization_header(tmp_path):
    fake = FakeDiscord()
    forum = fake.add_forum("bugs", ["Crash"])
    fake.add_thread(forum, "t", "Mira", "b", [], files=[("s.png", make_png())])
    url = next(iter(fake.blobs))
    dest = tmp_path / "out" / "s.png"
    fake.client().download(url, dest)
    assert dest.read_bytes() == make_png()
    assert fake.cdn_requests and not any(k.lower() == "authorization" for k in fake.cdn_requests[0][1])
    assert not list(dest.parent.glob("*.part"))


def test_download_http_error_and_size_cap(tmp_path):
    fake = FakeDiscord()
    c = fake.client()
    with pytest.raises(DiscordError, match="HTTP 404"):
        c.download("https://cdn.discordapp.com/attachments/1/2/missing.png", tmp_path / "m.png")
    forum = fake.add_forum("b", [])
    fake.add_thread(forum, "t", "x", "b", [], files=[("big.bin", bytes(100))])
    with pytest.raises(DiscordError, match="too large"):
        c.download(next(iter(fake.blobs)), tmp_path / "big.bin", max_bytes=10)
