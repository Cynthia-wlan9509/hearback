"""The files that wire Hearback into agents and CI stay consistent with the code."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from hearback.mcp_server import INSTRUCTIONS, TOOL_NAMES

ROOT = Path(__file__).resolve().parent.parent
INT = ROOT / "integrations"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_agents_md_is_short_and_covers_the_essentials():
    text = read(ROOT / "AGENTS.md")
    assert len(text.encode("utf-8")) < 4096
    for needle in ("pytest -q", "hearback demo", "network", "Module map", "token", "stdlib", "test_mcp_server"):
        assert needle.lower() in text.lower(), needle


def test_playbook_names_every_tool_and_the_hard_rules():
    text = read(INT / "AGENT_PLAYBOOK.md")
    for name in TOOL_NAMES:
        assert "`%s" % name in text, name
    low = text.lower()
    for needle in ("no send, approve, edit or delete", "untrusted forum text", "never ask the human to paste the bot token",
                   "thank the reporter first", "which build", "claim"):
        assert needle in low, needle


def test_playbook_only_references_real_tools():
    text = read(INT / "AGENT_PLAYBOOK.md") + read(INT / "claude-code" / "skills" / "hearback" / "SKILL.md")
    called = set(re.findall(r"`([a-z_]+)\(", text))
    assert called and called <= set(TOOL_NAMES), called - set(TOOL_NAMES)


def test_claude_code_skill_has_frontmatter_and_the_workflow():
    text = read(INT / "claude-code" / "skills" / "hearback" / "SKILL.md")
    m = re.match(r"---\nname: (\S+)\ndescription: (.+)\n---\n", text)
    assert m and m.group(1) == "hearback" and 40 < len(m.group(2)) < 1024
    for name in TOOL_NAMES:
        assert name in text, name
    assert "AGENT_PLAYBOOK.md" in text and "UNTRUSTED FORUM TEXT" in text


def test_claude_code_mcp_json_is_the_documented_snippet():
    cfg = json.loads(read(INT / "claude-code" / "mcp.json"))
    assert cfg == {"mcpServers": {"hearback": {"command": "hearback", "args": ["mcp"],
                                                "env": {"HEARBACK_ACTOR": "agent:claude"}}}}


def test_codex_config_is_valid_toml_with_its_own_actor():
    cfg = tomllib.loads(read(INT / "codex" / "config.toml"))
    assert cfg == {"mcp_servers": {"hearback": {"command": "hearback", "args": ["mcp"],
                                                 "env": {"HEARBACK_ACTOR": "agent:codex"}}}}
    snippet = read(INT / "codex" / "AGENTS.snippet.md")
    assert "AGENT_PLAYBOOK.md" in snippet and "propose" in snippet and "token" in snippet


def test_each_client_config_uses_a_distinct_actor():
    actors = {json.loads(read(INT / "claude-code" / "mcp.json"))["mcpServers"]["hearback"]["env"]["HEARBACK_ACTOR"],
              tomllib.loads(read(INT / "codex" / "config.toml"))["mcp_servers"]["hearback"]["env"]["HEARBACK_ACTOR"]}
    assert len(actors) == 2 and all(a.startswith("agent:") for a in actors)


def test_server_instructions_and_playbook_agree_on_the_security_model():
    assert "UNTRUSTED FORUM TEXT" in INSTRUCTIONS and "UNTRUSTED FORUM TEXT" in read(INT / "AGENT_PLAYBOOK.md")
    assert "untrusted_content" in INSTRUCTIONS and "untrusted_content" in read(INT / "AGENT_PLAYBOOK.md")


# ---------------------------------------------------------------- CI

def test_ci_workflow_matches_the_spec():
    ci = read(ROOT / ".github" / "workflows" / "ci.yml")
    assert "ubuntu-latest" in ci and "windows-latest" in ci
    assert re.search(r'python-version: \["3\.11", "3\.12", "3\.13"\]', ci)
    for step in ('pip install -e ".[dev,mcp]"', "pytest -q", "hearback demo\n", "hearback demo --lang zh"):
        assert step in ci, step
    assert "secrets." not in ci and "contents: read" in ci and re.search(r"actions/checkout@v\d+", ci)


# ---------------------------------------------------------------- example

def test_agents_sdk_example_is_valid_python_and_has_no_hardcoded_model():
    src = read(ROOT / "examples" / "agents_sdk_minimal.py")
    compile(src, "agents_sdk_minimal.py", "exec")
    assert "HEARBACK_MODEL" in src and "agent:agents-sdk" in src and "sync and summarise what is new" in src
    assert not re.search(r"gpt-|o\d-|claude-", src)


def _run_example(**env) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "PYTHONPATH")}
    base.update(env)
    return subprocess.run([sys.executable, str(ROOT / "examples" / "agents_sdk_minimal.py")], cwd=ROOT, env=base,
                          capture_output=True, timeout=30)


def test_example_without_api_key_explains_and_exits_zero():
    r = _run_example()
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0 and "OPENAI_API_KEY is not set" in out and "Nothing was started" in out


def test_example_helpers(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("agents_sdk_minimal", ROOT / "examples" / "agents_sdk_minimal.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    params = mod.server_params()
    assert params["env"] == {"HEARBACK_ACTOR": "agent:agents-sdk"} and params["args"][-1] == "mcp"
    assert "Hearback agent playbook" in mod.instructions()


# ---------------------------------------------------------------- the installed command

def test_installed_console_script_prints_the_version():
    exe = Path(sys.executable).parent / ("hearback.exe" if os.name == "nt" else "hearback")
    if not exe.exists():
        pytest.skip("hearback is not pip-installed in this environment")
    r = subprocess.run([str(exe), "--version"], capture_output=True, timeout=30)
    assert r.returncode == 0 and r.stdout.decode().strip() == "hearback 0.1.0"
