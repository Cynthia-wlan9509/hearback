"""Repository hygiene: nothing project-specific leaks, the core is stdlib-only, packaging is sane."""
from __future__ import annotations

import ast
import fnmatch
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import hearback

ROOT = Path(__file__).resolve().parent.parent
# directories git would not track (see .gitignore); everything else is scanned
IGNORED_DIRS = {".venv", "venv", ".git", "__pycache__", ".pytest_cache", ".hearback", "dist", "build",
                "node_modules", ".mypy_cache", ".ruff_cache"}
INTERNAL_SPECS = "docs/BUILD-SPEC-*.md"       # build briefs that name the reference project; removed before publishing

# assembled from pieces so this file does not contain the strings it forbids
def _u(*codes: int) -> str:
    return "".join(chr(c) for c in codes)


FORBIDDEN = [
    "ling" + "mao", "sw" + "lab", "pix" + "dance",
    _u(0x7075, 0x732b),                                   # the game's Chinese name
    _u(0x514d, 0x8d39, 0x7248), _u(0x5168, 0x8d44, 0x6e90, 0x7248), _u(0x5168, 0x6d3b, 0x52a8, 0x7248),   # its package names
    "private" + chr(92) + "discord", "05-" + _u(0x79bb, 0x7ebf), "F:" + chr(92) + "ling",
]
FORBIDDEN_RE = [re.compile(r"\bsw" + r"l\b", re.I)]
SNOWFLAKE = re.compile(r"(?<![\w.])\d{17,20}(?![\w.])")          # real Discord ids look like this


def repo_files():
    """Every text file in the tree that git would track, except the internal build briefs."""
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.endswith(".egg-info")]
        for name in files:
            p = Path(base) / name
            if fnmatch.fnmatch(p.relative_to(ROOT).as_posix(), INTERNAL_SPECS):
                continue
            try:
                p.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                continue                       # binary
            yield p


def test_no_project_specific_strings_or_ids_anywhere():
    problems = []
    for p in repo_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        low = text.lower()
        for bad in FORBIDDEN:
            if bad.lower() in low:
                problems.append("%s contains %r" % (p.relative_to(ROOT), bad))
        for rx in FORBIDDEN_RE:
            if rx.search(text):
                problems.append("%s matches %s" % (p.relative_to(ROOT), rx.pattern))
        if SNOWFLAKE.search(text):
            problems.append("%s contains a Discord-id-like number" % p.relative_to(ROOT))
    assert not problems, "\n".join(problems)


def test_the_scan_actually_scans_something():
    rel = {p.relative_to(ROOT).as_posix() for p in repo_files()}
    assert {"hearback/cli.py", "hearback/mcp_server.py", "docs/DESIGN.md", "pyproject.toml", "LICENSE", "AGENTS.md",
            ".github/workflows/ci.yml", "integrations/AGENT_PLAYBOOK.md", "examples/agents_sdk_minimal.py",
            "tests/test_hygiene.py"} <= rel
    assert not [r for r in rel if r.startswith((".venv/", "docs/BUILD-SPEC-")) or ".egg-info/" in r]


def _modules():
    return sorted((ROOT / "hearback").glob("*.py"))


def test_core_imports_are_stdlib_only():
    stdlib = set(sys.stdlib_module_names)
    optional = {"PIL", "mcp"}
    bad = []
    for f in _modules():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        nested = {id(n) for fn in funcs for n in ast.walk(fn) if isinstance(n, (ast.Import, ast.ImportFrom))}
        for n in ast.walk(tree):
            if not isinstance(n, (ast.Import, ast.ImportFrom)):
                continue
            names = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""]
            for name in names:
                top = name.split(".")[0]
                if top in stdlib or top == "hearback" or (n.level and not n.module):
                    continue
                if top in optional and id(n) in nested:       # optional extras only inside functions
                    continue
                bad.append("%s imports %s at module level" % (f.name, name))
    assert not bad, "\n".join(bad)


def test_every_spec_module_exists():
    expected = {"__init__", "__main__", "config", "discord", "ledger", "sync", "queue", "actions", "release", "staff",
                "templates", "guide", "render", "editor", "fake", "demo", "cli", "mcp_server", "util"}
    assert expected <= {f.stem for f in _modules()}


def test_pyproject_matches_the_spec():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    proj = meta["project"]
    assert proj["name"] == "hearback-desk" and proj["requires-python"] == ">=3.11" and proj["dependencies"] == []
    assert proj["scripts"] == {"hearback": "hearback.cli:main"}
    assert proj["optional-dependencies"] == {"mcp": ["mcp>=1.19,<3"], "images": ["pillow"], "dev": ["pytest"]}
    assert meta["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "hearback.__version__"}
    assert hearback.__version__ == "0.1.0"
    lic = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert lic.startswith("MIT License") and "Copyright (c) 2026 Cynthia-wlan9509 and contributors" in lic


def test_gitignore_covers_the_basics():
    lines = set((ROOT / ".gitignore").read_text(encoding="utf-8").split())
    assert {".venv/", "__pycache__/", ".hearback/", "*.egg-info/", "dist/", "build/"} <= lines


def test_version_flag_runs_as_a_module():
    r = subprocess.run([sys.executable, "-m", "hearback", "--version"], cwd=ROOT, capture_output=True, timeout=20)
    assert r.returncode == 0 and r.stdout.decode().strip() == "hearback 0.1.0"


def test_sources_use_lf_line_endings():
    crlf = [str(p.relative_to(ROOT)) for p in repo_files()
            if p.suffix in {".py", ".toml", ".yml", ".json"} and b"\r\n" in p.read_bytes()]
    assert not crlf, crlf


def test_sources_parse_as_python_3_11():
    """CI runs 3.11 too: reject syntax newer than that (e.g. PEP 701 f-strings) even when run on 3.13."""
    bad = []
    for p in repo_files():
        if p.suffix == ".py":
            try:
                ast.parse(p.read_text(encoding="utf-8"), filename=str(p), feature_version=(3, 11))
            except SyntaxError as e:
                bad.append("%s: %s" % (p.relative_to(ROOT), e))
    assert not bad, "\n".join(bad)


def test_gitattributes_keeps_checkouts_lf_so_windows_ci_matches():
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in attrs
