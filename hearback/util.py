"""Shared helpers: the error type, time and snowflake ids, text splitting, file locks, atomic writes."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator

DISCORD_EPOCH = 1420070400000
MSG_LIMIT = 2000          # Discord: characters per message
TITLE_LIMIT = 100         # Discord: forum post title
MAX_TAGS = 5              # Discord: tags per forum post
MAX_FILES = 10            # Discord: attachments per message
MAX_UPLOAD = 10 * 1024 * 1024
FLAG_PINNED = 2           # thread flag bit
DATA_DIR_NAME = ".hearback"
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


class HearbackError(Exception):
    """A problem the user can act on. The CLI prints the message and exits 1."""


Fatal = HearbackError


class LockTimeout(HearbackError):
    """Could not take a file lock in time."""


def setup_stdio() -> None:
    """Make stdout/stderr UTF-8 so Windows consoles never crash on non-ASCII text."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def warn(msg: str) -> None:
    print("warning: " + msg, file=sys.stderr)


# ---------------------------------------------------------------- time / ids

def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    """UTC, second precision: 2026-10-24T05:30:12Z."""
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(utcnow())


def parse_iso(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def norm_ts(s: str | None) -> str:
    """Discord timestamp -> UTC second-precision ISO; unparsable input is returned unchanged."""
    if not s:
        return ""
    t = parse_iso(s)
    return iso(t) if t else s


def make_snowflake(ms: int, seq: int = 0) -> str:
    """Build a Discord-style snowflake id from a Unix time in milliseconds."""
    return str(((ms - DISCORD_EPOCH) << 22) | seq)


def snowflake_ms(sid: str | int) -> int:
    return (int(sid) >> 22) + DISCORD_EPOCH


def snowflake_time(sid: str | int) -> str:
    return iso(dt.datetime.fromtimestamp(snowflake_ms(sid) / 1000.0, tz=dt.timezone.utc))


def sid6(tid: str) -> str:
    """Short display id: the last six characters."""
    return str(tid)[-6:]


# ---------------------------------------------------------------- text

def short(s: Any, n: int) -> str:
    """Collapse whitespace and truncate to n characters."""
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: max(n - 1, 0)] + "…"


def safe_name(name: str | None) -> str:
    """File name safe on Windows; long names are truncated but keep the extension."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "file").strip(" .") or "file"
    base, ext = os.path.splitext(name)
    return base[:100] + ext[:20]


def fmt_size(n: int) -> str:
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%.2f MB" % (n / 1024.0 / 1024.0)


def is_image(p: str) -> bool:
    return os.path.splitext(str(p).split("?")[0])[1].lower() in IMAGE_EXT


FENCE = "```"
_FENCE_OPEN = re.compile(r"\s*(```[\w+-]{0,16})")
_FENCE_RESERVE = len(FENCE) + 1 + len(FENCE) + 16 + 1   # closing "\n```" plus a re-opened "```lang\n"


def split_text(text: str, limit: int = MSG_LIMIT) -> list[str]:
    """Split on blank lines into chunks of at most `limit` characters, packing paragraphs together.

    A paragraph that is too long is split on newlines; a single line that is still too long
    is hard-cut (with a warning). A ``` code fence is never left open across chunks: it is closed at
    the end of a chunk and re-opened (same language tag) at the start of the next.
    """
    text = text.strip("\r\n")
    if len(text) <= limit:
        return [text]
    if FENCE not in text:
        return _split_plain(text, limit)
    return _balance_fences(_split_plain(text, limit - _FENCE_RESERVE))


def _balance_fences(chunks: list[str]) -> list[str]:
    out: list[str] = []
    opened: str | None = None   # the opening fence line while we are inside a code block
    for i, chunk in enumerate(chunks):
        prefix = opened + "\n" if opened else ""      # re-open the block the previous chunk had to close
        for line in chunk.split("\n"):               # (the prefix itself is not scanned: it only restores state)
            if line.count(FENCE) % 2:
                if opened:
                    opened = None
                else:
                    m = _FENCE_OPEN.match(line)
                    opened = m.group(1) if m else FENCE
        # close an open block at a chunk boundary; the very last chunk stays exactly as the author wrote it
        suffix = "\n" + FENCE if opened and i < len(chunks) - 1 else ""
        out.append(prefix + chunk + suffix)
    return out


def _split_plain(text: str, limit: int) -> list[str]:
    pieces: list[tuple[str, str]] = []  # (separator before this piece, content)
    for para in re.split(r"\n[ \t]*\n", text):
        if len(para) <= limit:
            pieces.append(("\n\n", para))
            continue
        for n, line in enumerate(para.split("\n")):
            sep = "\n\n" if n == 0 else "\n"
            while len(line) > limit:
                warn("a line is longer than %d characters; hard-cutting it" % limit)
                pieces.append((sep, line[:limit]))
                line, sep = line[limit:], ""
            pieces.append((sep, line))
    chunks: list[str] = []
    cur = ""
    for sep, piece in pieces:
        if not piece.strip():
            continue
        if cur and len(cur) + len(sep) + len(piece) > limit:
            chunks.append(cur)
            cur = ""
        cur += (sep if cur else "") + piece
    if cur:
        chunks.append(cur)
    return chunks


def chunks_for(text: str) -> list[str]:
    """The messages a piece of text becomes when sent (one empty message if there is no text)."""
    return split_text(text) if text.strip() else [""]


# ---------------------------------------------------------------- files

def replace_retry(src: str | Path, dst: str | Path, tries: int = 40) -> None:
    """os.replace that rides out transient PermissionErrors (Windows, file briefly open elsewhere)."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.025)


def ensure_data_dir(path: str | Path) -> Path:
    """Create a Hearback data directory together with a `.gitignore` that ignores everything in it,
    so the ledger, the queue, attachments and a token file can never be committed by accident.

    The `*` ignore file is only written into a directory Hearback just created or one named `.hearback`:
    pointing a ledger at an existing project folder must never make git ignore the whole project.
    """
    path = Path(path)
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    marker = path / ".gitignore"
    if not marker.exists() and (not existed or path.name == DATA_DIR_NAME):
        try:
            marker.write_text("*\n", encoding="utf-8", newline="\n")
        except OSError:
            pass
    return path


def atomic_write_text(path: str | Path, text: str) -> None:
    """Write via a temp file in the same directory, then replace. LF newlines, UTF-8."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.%d.%s.tmp" % (path.name, os.getpid(), os.urandom(3).hex()))
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        replace_retry(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_json(path: str | Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=1) + "\n")


def read_json(path: str | Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


@contextlib.contextmanager
def file_lock(path: str | Path, *, stale: float = 30.0, timeout: float = 15.0, poll: float = 0.02,
              sleep: Callable[[float], None] = time.sleep) -> Iterator[None]:
    """Cross-process lock: a file created with O_CREAT|O_EXCL.

    A lock file older than `stale` seconds is assumed to belong to a dead process and is removed.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(path)
            except FileNotFoundError:
                continue
            if age > stale:
                with contextlib.suppress(OSError):
                    os.unlink(path)
                continue
        except PermissionError:  # Windows: the file is being deleted right now
            pass
        if time.monotonic() >= deadline:
            raise LockTimeout("could not take lock %s within %.0fs (held by another process?)"
                              % (path.name, timeout))
        sleep(poll)
    try:
        os.write(fd, ("%d %s\n" % (os.getpid(), now_iso())).encode("ascii"))
    finally:
        os.close(fd)
    try:
        yield
    finally:
        for i in range(40):
            try:
                os.unlink(path)
                break
            except FileNotFoundError:
                break
            except PermissionError:
                time.sleep(0.025)
