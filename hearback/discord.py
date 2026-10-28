"""Minimal Discord REST v10 client (stdlib only): retries on 429, multipart uploads, CDN-only downloads.

All network I/O goes through a `transport` callable so tests and the demo can plug in
`hearback.fake.FakeDiscord`:

    transport(method, url, headers, data, timeout) -> (status_code, response_bytes)
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Sequence

from hearback import __version__
from hearback.util import (MAX_FILES, MAX_UPLOAD, HearbackError, fmt_size, warn)

API_BASE = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (hearback, %s)" % __version__
CDN_HOSTS = ("discordapp.com", "discordapp.net", "discord.com")
MAX_DOWNLOAD = 25 * 1024 * 1024

MIME_FIXED = {  # Windows' registry mime table is unreliable; pin the common ones
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp", ".txt": "text/plain", ".log": "text/plain",
    ".md": "text/plain", ".json": "application/json", ".zip": "application/zip",
    ".pdf": "application/pdf", ".mp4": "video/mp4",
}


# transport(method, url, headers, data, timeout[, max_bytes]) -> (status, body). Only downloads pass
# max_bytes: the transport must stop reading once the body exceeds it (raising DownloadTooLarge).
Transport = Callable[..., "tuple[int, bytes]"]


class DiscordError(HearbackError):
    """An HTTP error answer from Discord (or a network failure when status == 0)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class DownloadTooLarge(DiscordError):
    """A download went past its byte cap and was abandoned without reading the rest."""


def is_cdn_url(url: str) -> bool:
    """True only for https URLs whose host is a Discord CDN domain (or a subdomain of one)."""
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host == h or host.endswith("." + h) for h in CDN_HOSTS)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to https Discord hosts, so a header can never be forwarded elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if not is_cdn_url(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urllib_transport(method: str, url: str, headers: dict, data: bytes | None = None,
                     timeout: float = 30, max_bytes: int | None = None) -> tuple[int, bytes]:
    """The real transport. HTTP error statuses are returned, not raised; network errors raise.

    With max_bytes the body is read in chunks and abandoned as soon as it exceeds the cap (a declared
    Content-Length over the cap is refused before reading anything).
    """
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(_SafeRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            if max_bytes is None:
                return r.status, r.read()
            declared = r.headers.get("Content-Length") or ""
            if declared.isdigit() and int(declared) > max_bytes:
                raise DownloadTooLarge("declared size %s is over the cap" % declared)
            buf = bytearray()
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > max_bytes:
                    raise DownloadTooLarge("body went past the cap")
            return r.status, bytes(buf)
    except urllib.error.HTTPError as e:
        return e.code, e.read(64 * 1024) if max_bytes is not None else e.read()
    except urllib.error.URLError as e:
        raise DiscordError("network error: %s" % (e.reason,))
    except OSError as e:
        raise DiscordError("network error: %s" % (e,))


def guess_mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return MIME_FIXED.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"


def load_attachments(paths: Sequence[str | os.PathLike[str]] | None) -> list[dict]:
    """Validate local files for upload -> [{path, filename, size, ctype}]. Raises before any request."""
    out = []
    for p in paths or []:
        p = str(p)
        if not os.path.isfile(p):
            raise HearbackError("attachment not found or not a file: %s" % p)
        size = os.path.getsize(p)
        if size > MAX_UPLOAD:
            raise HearbackError("attachment %s is %s, over the %d MB bot upload limit"
                                % (os.path.basename(p), fmt_size(size), MAX_UPLOAD // 1024 // 1024))
        out.append({"path": p, "filename": re.sub(r'["\r\n]', "_", os.path.basename(p)),
                    "size": size, "ctype": guess_mime(p)})
    if len(out) > MAX_FILES:
        raise HearbackError("%d attachments; Discord allows %d per message" % (len(out), MAX_FILES))
    if sum(a["size"] for a in out) > MAX_UPLOAD:
        warn("attachments total %s, over %d MB; Discord may refuse them"
             % (fmt_size(sum(a["size"] for a in out)), MAX_UPLOAD // 1024 // 1024))
    return out


def msg_body(text: str, atts: Sequence[dict]) -> dict:
    """JSON body of one message (also used for edits); with files, the attachments array pairs with files[i].

    Always carries allowed_mentions with an empty parse list, so no text can ping users, roles or @everyone.
    """
    body: dict[str, Any] = {"content": text, "allowed_mentions": {"parse": []}}   # nothing may ever ping anyone
    if atts:
        body["attachments"] = [{"id": i, "filename": a["filename"]} for i, a in enumerate(atts)]
    return body


def build_multipart(payload: dict, files: Sequence[dict]) -> tuple[bytes, str]:
    """multipart/form-data with payload_json + files[i]. Returns (body, Content-Type header)."""
    boundary = "----hearback" + os.urandom(12).hex()
    out: list[bytes] = []

    def part(header_lines: list[str], content: bytes) -> None:
        out.append(("--%s\r\n" % boundary).encode("ascii")
                   + "\r\n".join(header_lines).encode("utf-8") + b"\r\n\r\n" + content + b"\r\n")

    part(['Content-Disposition: form-data; name="payload_json"', "Content-Type: application/json"],
         json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    for i, f in enumerate(files):
        data = Path(f["path"]).read_bytes()
        part(['Content-Disposition: form-data; name="files[%d]"; filename="%s"' % (i, f["filename"]),
              "Content-Type: " + f["ctype"]], data)
    out.append(("--%s--\r\n" % boundary).encode("ascii"))
    return b"".join(out), "multipart/form-data; boundary=" + boundary


_HINTS = {
    401: "the token was rejected: reset it in the developer portal and update your token source",
    403: "the bot lacks permission here: check channel overrides and the invite permissions",
    404: "not found: wrong id, deleted, or the bot cannot see that channel",
}


class DiscordClient:
    """Thin REST client. The token lives only in a private attribute and never appears in errors."""

    def __init__(self, token: str, base_url: str = API_BASE, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_retry: int = 5):
        self._token = token
        self.base_url = base_url.rstrip("/")
        self.transport: Transport = transport or urllib_transport
        self.sleep = sleep
        self.max_retry = max_retry

    def __repr__(self) -> str:
        return "DiscordClient(base_url=%r)" % self.base_url

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def request(self, method: str, path: str, params: dict | None = None, body: Any = None,
                files: Sequence[dict] | None = None, reason: str | None = None) -> Any:
        """Call the API. 429 waits retry_after and retries; other errors >= 400 raise DiscordError.

        With `files`, `body` becomes payload_json and the request is multipart/form-data. `reason` goes into the
        server's audit log (X-Audit-Log-Reason, URL-encoded) for the calls that support it, e.g. role changes.
        """
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Authorization": "Bot " + self._token, "User-Agent": USER_AGENT}
        if reason:
            headers["X-Audit-Log-Reason"] = urllib.parse.quote(reason[:512], safe="")
        data: bytes | None = None
        if files:
            data, headers["Content-Type"] = build_multipart(body or {}, files)
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        for attempt in range(self.max_retry + 1):
            status, raw = self.transport(method, url, headers, data, 30)
            text = self._redact(raw.decode("utf-8", "replace")) if raw else ""
            if status == 429:
                try:
                    wait = float(json.loads(text).get("retry_after", 1))
                except (ValueError, AttributeError, TypeError):
                    wait = 1.0
                if attempt >= self.max_retry:
                    raise DiscordError("HTTP 429 rate limited; still limited after %d retries: %s"
                                       % (self.max_retry, text[:200]), 429)
                print("429 rate limited; waiting %.1fs (retry %d/%d)" % (wait, attempt + 1, self.max_retry),
                      file=sys.stderr)
                self.sleep(min(wait, 60.0) + 0.1)
                continue
            if status >= 400:
                hint = _HINTS.get(status)
                raise DiscordError("HTTP %d %s %s: %s%s" % (status, method, path, text[:300],
                                                            " (%s)" % hint if hint else ""), status)
            return json.loads(text) if text.strip() else {}
        raise DiscordError("request failed: %s %s" % (method, path))

    def me(self) -> dict:
        return self.request("GET", "/users/@me")

    def download(self, url: str, dest: str | os.PathLike[str], max_bytes: int = MAX_DOWNLOAD) -> None:
        """Download an attachment. Discord CDN hosts only, and never with an Authorization header."""
        if not is_cdn_url(url):
            host = urllib.parse.urlparse(url).hostname or "?"
            raise DiscordError("refusing to download from a non-Discord host: %s" % host)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            status, raw = self.transport("GET", url, {"User-Agent": USER_AGENT}, None, 60, max_bytes)
        except DownloadTooLarge:
            raise DiscordError("download too large (over %s): %s" % (fmt_size(max_bytes), dest.name)) from None
        if status >= 400:
            raise DiscordError("download failed (HTTP %d): %s" % (status, dest.name), status)
        if len(raw) > max_bytes:   # a transport that ignored the cap
            raise DiscordError("download too large (%s): %s" % (fmt_size(len(raw)), dest.name))
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(raw)
        os.replace(tmp, dest)
