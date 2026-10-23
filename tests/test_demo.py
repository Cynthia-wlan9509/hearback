"""`hearback demo` runs the whole loop offline, fast, in both languages."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hearback.demo import REPORTS, run_demo

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_demo_runs_end_to_end(lang):
    lines: list[str] = []
    t0 = time.monotonic()
    assert run_demo(lang, out=lines.append) == 0
    assert time.monotonic() - t0 < 10
    text = "\n".join(lines)
    for n in range(1, 8):
        assert "%d/7" % n in text
    assert "synced: 12 threads, 12 new" in text
    assert "verify: all 14 sent proposals match Discord" in text
    assert ("0 mismatches" if lang == "en" else "校验不一致 0 条") in text and "MISMATCH" not in text
    assert "second `send`" in text or "紧接着再 `send`" in text
    assert "# Bug desk" in text and "released 8" in text


def test_demo_answers_every_reporter_and_never_double_sends():
    lines: list[str] = []
    assert run_demo("en", keep=True, out=lines.append) == 0
    root = Path(next(ln for ln in lines if ln.startswith("workspace kept at")).split("workspace kept at ")[1])
    try:
        ledger = json.loads((root / ".hearback" / "ledger.json").read_text(encoding="utf-8"))
        reports = [r for r in ledger["threads"].values() if r["triage"]["status"] != "guide"]
        assert len(reports) == len(REPORTS) == 12
        assert all(r["triage"]["replies_posted"] for r in reports), "every reporter must have been answered"
        sent = [json.loads(f.read_text(encoding="utf-8")) for f in (root / ".hearback" / "queue" / "sent").glob("*.json")]
        assert not list((root / ".hearback" / "queue" / "pending").glob("*.json"))
        assert not list((root / ".hearback" / "queue" / "failed").glob("*.json"))
        ids = [m for p in sent for m in p["result"].get("message_ids", [])]
        assert len(ids) == len(set(ids))                               # no message id appears twice
        assert all(p["result"]["verified"] is True for p in sent)
        events = [json.loads(ln) for ln in (root / ".hearback" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        assert {"agent:claude-1", "agent:codex-2", "human:demo"} <= {e["actor"] for e in events}
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize("args", [["demo"], ["demo", "--lang", "zh"]])
def test_demo_cli_with_stdlib_only(args):
    """python -m hearback demo works with site-packages disabled (-S): the core is stdlib only."""
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-S", "-X", "utf8", "-m", "hearback", *args], cwd=ROOT,
                       capture_output=True, timeout=10,
                       env={k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")})
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert time.monotonic() - t0 < 10
    out = r.stdout.decode("utf-8")
    assert "Starlight Bakery" in out and "== 7/7" in out and "traceback" not in (out + r.stderr.decode()).lower()
