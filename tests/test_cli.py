"""The command line, end to end against the fake."""
from __future__ import annotations

import json

import pytest

from hearback import cli
from hearback.config import load_config


def test_init_writes_config_and_refuses_to_overwrite(tmp_path, capsys):
    d = tmp_path / "proj"
    assert cli.main(["init", "--dir", str(d)]) == 0
    assert (d / "hearback.toml").is_file() and (d / ".hearback" / ".gitignore").is_file()
    assert load_config(d / "hearback.toml").variants
    assert cli.main(["init", "--dir", str(d)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert cli.main(["init", "--dir", str(d), "--force"]) == 0


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 2
    assert "usage: hearback" in capsys.readouterr().out


@pytest.mark.parametrize("cmd", ["init", "sync", "list", "show", "claim", "unclaim", "triage", "propose", "queue",
                                 "send", "release", "staff", "guide", "render", "log", "editor", "demo", "mcp"])
def test_every_command_has_help(cmd, capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main([cmd, "--help"])
    assert ei.value.code == 0 and "usage: hearback %s" % cmd in capsys.readouterr().out


def test_sync_list_show(cli_ws):
    rc, out, _ = cli_ws.run("sync")
    assert rc == 0 and out.startswith("synced: 2 threads, 2 new") and "Oven freezes" in out and "(+1 img)" in out
    rc, out, _ = cli_ws.run("sync", "--json")
    assert json.loads(out)["skipped"] == 2
    rc, out, _ = cli_ws.run("list")
    assert rc == 0 and "Oven freezes" in out and "(2 of 2 threads)" in out
    rc, out, _ = cli_ws.run("list", "--status", "wontfix")
    assert "(0 of 2 threads)" in out
    rc, out, _ = cli_ws.run("list", "--json")
    assert {t["title"] for t in json.loads(out)} == {"Oven freezes", "Customers flicker"}
    tid = cli_ws.threads["oven"]
    rc, out, _ = cli_ws.run("show", tid[-6:])
    assert rc == 0 and "Stops at 0:00" in out and "status: new" in out and "attachment:" in out
    rc, out, _ = cli_ws.run("show", tid, "--json")
    assert json.loads(out)["id"] == tid


def test_claim_triage_and_conflict(cli_ws):
    cli_ws.run("sync")
    t = cli_ws.threads["oven"][-6:]
    rc, out, _ = cli_ws.run("--as", "agent:codex-2", "claim", t, "--minutes", "5")
    assert rc == 0 and "agent:codex-2" in out
    rc, out, err = cli_ws.run("--as", "agent:claude-1", "claim", t)
    assert rc == 1 and "already claimed by agent:codex-2" in err
    rc, out, _ = cli_ws.run("triage", t, "--set", "status=investigating", "cluster=oven", "variants=free,full",
                            "--as", "agent:codex-2")                       # --as after the subcommand works too
    assert rc == 0 and "rev 1" in out
    rc, out, err = cli_ws.run("triage", t, "--set", "status=wontfix", "--expect-rev", "0", "--as", "agent:codex-2")
    assert rc == 1 and "changed (rev 1" in err
    # another actor may not write triage while the claim is live, unless it forces
    rc, out, err = cli_ws.run("triage", t, "--set", "status=wontfix")
    assert rc == 1 and "claimed by agent:codex-2" in err
    rc, out, _ = cli_ws.run("triage", t, "--set", "root_cause=forced by a human", "--force")
    assert rc == 0 and "rev 2" in out
    rc, out, err = cli_ws.run("triage", t, "--set", "variants=free,gold", "--as", "agent:codex-2")
    assert rc == 1 and "unknown variant(s): gold" in err and "free, full" in err
    rc, out, _ = cli_ws.run("list", "--cluster", "oven")
    assert "@agent:codex-2" in out and "investigating" in out
    rc, out, _ = cli_ws.run("--as", "agent:codex-2", "unclaim", t)
    assert rc == 0 and "released" in out
    rc, out, err = cli_ws.run("triage", t)
    assert rc == 1 and "nothing to set" in err


def test_actor_comes_from_the_environment(cli_ws, monkeypatch):
    cli_ws.run("sync")
    monkeypatch.setenv("HEARBACK_ACTOR", "agent:from-env")
    cli_ws.run("triage", cli_ws.threads["oven"][-6:], "--set", "status=investigating")
    rc, out, _ = cli_ws.run("log")
    assert "agent:from-env" in out and "triage" in out


def test_propose_queue_send_flow(cli_ws):
    cli_ws.run("sync")
    t = cli_ws.threads["oven"][-6:]
    rc, out, _ = cli_ws.run("--as", "agent:claude-1", "propose", "reply", t, "--text", "Looking into it",
                            "--why", "ack")
    assert rc == 0 and "queued p-" in out and "| Looking into it" in out
    rc, out, _ = cli_ws.run("propose", "reply", t, "--template", "need_info", "--var", "x=y")
    assert rc == 0 and "tag the post with your variant" in out and "Hi Mira" in out
    rc, out, _ = cli_ws.run("propose", "tags", t, "--add", "In progress")
    rc, out, _ = cli_ws.run("queue", "list")
    assert out.count("pending") == 3 and "(3 proposals)" in out
    n_writes = len(cli_ws.fake.writes())
    rc, out, _ = cli_ws.run("send")
    assert rc == 0 and "0 approved" in out and len(cli_ws.fake.writes()) == n_writes
    rc, out, _ = cli_ws.run("queue", "approve", "--all", "--yes", "--as", "human:owner")
    assert out.count("approved p-") == 3
    rc, out, _ = cli_ws.run("send")                                           # dry run: previews, sends nothing
    assert "dry run: 3 approved" in out and "Nothing was sent" in out and len(cli_ws.fake.writes()) == n_writes
    rc, out, _ = cli_ws.run("send", "--yes", "--as", "human:owner")
    assert rc == 0 and out.count("sent    p-") == 3 and "verify: all 3 sent proposals match Discord" in out
    rc, out, _ = cli_ws.run("send", "--yes")
    assert "nothing approved to send" in out
    rc, out, _ = cli_ws.run("show", t)
    assert "sent by the bot (2)" in out
    rc, out, _ = cli_ws.run("queue", "show", "0001")
    assert "[sent]" in out and "verified" in out
    rc, out, _ = cli_ws.run("log", "--thread", t)
    assert "propose" in out and "approve" in out and "send" in out


def test_reject_edit_retry_and_verify_commands(cli_ws):
    cli_ws.run("sync")
    t = cli_ws.threads["oven"][-6:]
    cli_ws.run("propose", "reply", t, "--text", "first draft")
    rc, out, _ = cli_ws.run("queue", "edit", "1", "--text", "better wording")
    assert rc == 0 and "better wording" in out
    rc, out, _ = cli_ws.run("queue", "reject", "1", "--reason", "not needed")
    assert rc == 0 and "rejected p-" in out
    rc, out, err = cli_ws.run("queue", "approve", "1")
    assert rc == 1 and "only pending" in err
    rc, out, err = cli_ws.run("queue", "retry", "1")
    assert rc == 1 and "only failed" in err
    cli_ws.run("propose", "reply", t, "--text", "second")
    cli_ws.run("queue", "approve", "2")
    cli_ws.run("send", "--yes")
    rc, out, _ = cli_ws.run("queue", "verify", "2")
    assert rc == 0 and "all match Discord" in out


def test_text_from_a_file_and_newpost_pin_edit_delete(cli_ws, tmp_path):
    cli_ws.run("sync")
    f = tmp_path / "body.md"
    f.write_text("Line one\nLine two — ✅", encoding="utf-8")
    t = cli_ws.threads["flicker"][-6:]
    assert cli_ws.run("propose", "newpost", "--title", "Rules", "--file", str(f), "--tag", "UI", "--pin")[0] == 0
    assert cli_ws.run("propose", "post", cli_ws.announce, "--text", "hello channel")[0] == 0
    assert cli_ws.run("propose", "pin", t)[0] == 0
    cli_ws.run("queue", "approve", "--all", "--yes")
    rc, out, _ = cli_ws.run("send", "--yes")
    assert rc == 0 and "MISMATCH" not in out
    posts = list(cli_ws.fake.messages[cli_ws.announce])
    assert posts[0]["content"] == "hello channel"
    new = next(c for c in cli_ws.fake.channels.values() if c.get("name") == "Rules")
    assert cli_ws.fake.message(new["id"], new["id"])["content"] == "Line one\nLine two — ✅" and new["flags"] & 2
    mid = posts[0]["id"]
    assert cli_ws.run("propose", "edit", cli_ws.announce, mid, "--text", "hello again")[0] == 0
    cli_ws.run("queue", "approve", "--all", "--yes")
    cli_ws.run("send", "--yes")
    assert cli_ws.fake.message(cli_ws.announce, mid)["content"] == "hello again"
    assert cli_ws.run("propose", "delete", cli_ws.announce, mid)[0] == 0
    cli_ws.run("queue", "approve", "--all", "--yes")
    cli_ws.run("send", "--yes")
    assert cli_ws.fake.messages[cli_ws.announce] == []


def test_release_flow_via_cli(cli_ws):
    cli_ws.run("sync")
    t = cli_ws.threads["oven"][-6:]
    cli_ws.run("triage", t, "--set", "status=fixed-pending-release", "cluster=oven", "variants=free",
               "release_note=Fixed.", "--tag-proposal")
    rc, out, _ = cli_ws.run("release", "2026.10.24", "--variant", "free")
    assert rc == 0 and "release 2026.10.24: " in out and "hearback send --yes" in out
    rc, out, _ = cli_ws.run("release", "2026.10.24", "--finalize")
    assert "0 marked released" in out and "waiting" in out
    cli_ws.run("queue", "approve", "--all", "--yes")
    cli_ws.run("send", "--yes")
    rc, out, _ = cli_ws.run("release", "2026.10.24", "--finalize")
    assert rc == 0 and "1 marked released" in out
    rc, out, _ = cli_ws.run("list", "--status", "released")
    assert "(1 of 2 threads)" in out
    rc, out, err = cli_ws.run("release", "1.0", "--variant", "nope")
    assert rc == 1 and "unknown variant" in err


def test_staff_commands(cli_ws):
    cli_ws.run("sync")
    cli_ws.run("propose", "reply", cli_ws.threads["oven"][-6:], "--text", "hello")
    rc, out, _ = cli_ws.run("staff", "push")
    assert rc == 0 and "pushed 1 proposal" in out
    pid = json.loads(next((cli_ws.root / ".hearback" / "queue" / "pending").glob("*.json")).read_text(encoding="utf-8"))
    cli_ws.fake.react(cli_ws.staff, pid["staff_message_id"], "✅", cli_ws.approver["id"])
    rc, out, _ = cli_ws.run("staff", "poll")
    assert rc == 0 and "1 approved" in out


def test_guide_render_and_errors(cli_ws):
    cli_ws.run("sync")
    rc, out, _ = cli_ws.run("guide", "--tag", "UI")
    assert rc == 0 and "How to report a bug" in out
    rc, out, _ = cli_ws.run("render", "--stdout")
    assert rc == 0 and "# Bug desk" in out and "Oven freezes" in out
    rc, out, _ = cli_ws.run("render")
    assert rc == 0 and (cli_ws.root / ".hearback" / "DASHBOARD.md").is_file()
    rc, out, err = cli_ws.run("show", "999999")
    assert rc == 1 and err.startswith("error: no thread ending in 999999")
    rc, out, err = cli_ws.run("propose", "reply", "999999", "--text", "x")
    assert rc == 1 and "error:" in err


def test_missing_token_is_a_helpful_error(cli_ws, monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)       # the fixture's client factory still calls cfg.token()
    rc, out, err = cli_ws.run("sync")
    assert rc == 1 and "No Discord bot token found" in err


def test_guide_steps_accept_several_values_per_flag():
    args = cli.build_parser().parse_args(["guide", "--steps", "a.png:1,2,3:x", "b.png:4,5,6:y", "--steps", "c.png:7,8,9:z"])
    assert [s for g in args.steps for s in g] == ["a.png:1,2,3:x", "b.png:4,5,6:y", "c.png:7,8,9:z"]


def test_release_accepts_finalize_before_the_version():
    args = cli.build_parser().parse_args(["release", "--finalize", "2026.10.24"])
    assert args.finalize and args.version == "2026.10.24"


def test_send_only_flag(cli_ws):
    cli_ws.run("sync")
    cli_ws.run("propose", "reply", cli_ws.threads["oven"][-6:], "--text", "one")
    cli_ws.run("propose", "reply", cli_ws.threads["flicker"][-6:], "--text", "two")
    cli_ws.run("queue", "approve", "--all", "--yes")
    rc, out, _ = cli_ws.run("send", "--yes", "--only", "2")
    assert rc == 0 and out.count("sent    p-") == 1
    rc, out, _ = cli_ws.run("queue", "list", "--state", "approved")
    assert "(1 proposals)" in out


def test_editor_without_a_token_can_still_open_the_queue(cli_ws, monkeypatch):
    seen = {}

    def fake_run(cfg, ledger, queue, actor, client):
        seen["client"] = client
        return 0

    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr("hearback.editor.run_editor", fake_run)
    rc, out, err = cli_ws.run("editor")
    assert rc == 0 and "No Discord bot token found" in err
    with pytest.raises(Exception, match="No Discord bot token found"):
        seen["client"].request("GET", "/users/@me")


def test_approve_needs_ids_or_all(cli_ws):
    rc, out, err = cli_ws.run("queue", "approve")
    assert rc == 1 and "--all" in err
    rc, out, err = cli_ws.run("queue", "approve", "--all", "--yes")
    assert rc == 0 and "nothing pending" in out
