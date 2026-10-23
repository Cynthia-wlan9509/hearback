"""Config discovery, parsing, token loading, init template."""
from __future__ import annotations

import pytest

from hearback.config import SETUP_HINT, TEMPLATE, Config, find_config, load_config, parse_config, write_template
from hearback.util import HearbackError


def test_init_template_is_valid_and_parses(tmp_path):
    f = write_template(tmp_path)
    assert f.name == "hearback.toml" and (tmp_path / ".hearback" / ".gitignore").read_text() == "*\n"
    cfg = load_config(f)
    assert cfg.language == "en" and cfg.token_env == "DISCORD_BOT_TOKEN" and cfg.send_delay == 1.0
    assert set(cfg.variants) == {"free", "full"} and cfg.variants["free"].tag == "Free"
    assert cfg.status_tags["fixed-pending-release"] == "Fixed, awaiting release"
    assert cfg.forum_id == "" and cfg.approvers == [] and cfg.root == tmp_path.resolve()
    assert "approvers" in TEMPLATE and "token_file" in TEMPLATE


def test_init_refuses_to_overwrite_without_force(tmp_path):
    write_template(tmp_path)
    with pytest.raises(HearbackError, match="already exists"):
        write_template(tmp_path)
    write_template(tmp_path, force=True)


def test_config_is_found_in_a_parent_directory(tmp_path):
    write_template(tmp_path)
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert find_config(sub) == tmp_path / "hearback.toml"
    cfg = load_config(cwd=sub)
    assert cfg.root == tmp_path.resolve() and cfg.ledger_path == tmp_path.resolve() / ".hearback" / "ledger.json"


def test_no_config_gives_defaults(tmp_path):
    cfg = load_config(cwd=tmp_path)
    assert cfg.path is None and cfg.root == tmp_path.resolve() and cfg.forum_id == ""
    with pytest.raises(HearbackError, match="forum_id"):
        cfg.require_forum()


def test_bad_files_are_clear_errors(tmp_path):
    bad = tmp_path / "hearback.toml"
    bad.write_text("[discord\nforum_id=", encoding="utf-8")
    with pytest.raises(HearbackError, match="not valid TOML"):
        load_config(bad)
    with pytest.raises(HearbackError, match="not found"):
        load_config(tmp_path / "missing.toml")
    bad.write_text('[templates]\nlanguage = "fr"\n', encoding="utf-8")
    with pytest.raises(HearbackError, match="language"):
        load_config(bad)
    bad.write_text('[discord]\nsend_delay = "soon"\n', encoding="utf-8")
    with pytest.raises(HearbackError, match="send_delay"):
        load_config(bad)


def test_ids_may_be_written_as_numbers_or_strings(tmp_path):
    cfg = parse_config({"discord": {"guild_id": 1234, "forum_id": "5678", "approvers": [1, "2"]}}, tmp_path)
    assert (cfg.guild_id, cfg.forum_id, cfg.approvers) == ("1234", "5678", ["1", "2"])


def test_token_precedence_env_then_file(tmp_path):
    (tmp_path / ".hearback").mkdir()
    (tmp_path / ".hearback" / "token.txt").write_text("﻿file-token\n", encoding="utf-8")
    cfg = Config(root=tmp_path)
    assert cfg.token({}) == "file-token"                                   # BOM and newline stripped
    assert cfg.token({"DISCORD_BOT_TOKEN": "  env-token "}) == "env-token"
    assert cfg.token({"DISCORD_BOT_TOKEN": "Bot prefixed"}) == "prefixed"
    cfg.token_env = "MY_VAR"
    assert cfg.token({"MY_VAR": "custom", "DISCORD_BOT_TOKEN": "ignored"}) == "custom"


def test_missing_token_explains_the_setup(tmp_path):
    with pytest.raises(HearbackError) as ei:
        Config(root=tmp_path).token({})
    assert str(ei.value) == SETUP_HINT and "MESSAGE CONTENT INTENT" in str(ei.value)


def test_token_file_may_be_absolute(tmp_path):
    f = tmp_path / "elsewhere.txt"
    f.write_text("abs-token", encoding="utf-8")
    assert Config(root=tmp_path / "x", token_file=str(f)).token({}) == "abs-token"
