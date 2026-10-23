"""The bot token is never printed, logged, or written to the ledger / queue / events."""
from __future__ import annotations

from conftest import CLI_TOKEN


def flow(ws) -> str:
    """Run a representative session through the CLI and return everything it printed."""
    seen = []

    def run(*a):
        rc, out, err = ws.run(*a)
        seen.append(out + err)
        return rc

    run("sync")
    run("sync", "--json")
    run("list")
    run("show", ws.threads["oven"][-6:])
    t = ws.threads["oven"][-6:]
    run("claim", t)
    run("triage", t, "--set", "status=fixed-pending-release", "cluster=oven", "variants=free", "--tag-proposal")
    run("propose", "reply", t, "--text", "hello")
    run("propose", "reply", ws.threads["flicker"][-6:], "--text", "this one fails")
    ws.fake.fail_next(r"POST /channels/\d+/messages", 403, {"message": "Missing Permissions"}, after=1)
    run("queue", "list")
    run("queue", "approve", "--all")
    run("send")
    run("send", "--yes")
    run("queue", "show", "0001")
    run("staff", "push")
    run("release", "2026.10.24", "--variant", "free")
    run("release", "2026.10.24", "--finalize")
    run("render", "--stdout")
    run("log")
    run("guide")
    return "\n".join(seen)


def all_files_text(root, skip=()) -> str:
    parts = []
    for p in root.rglob("*"):
        if p.is_file() and p.name not in skip and ".pytest_cache" not in str(p):
            parts.append(p.read_bytes().decode("utf-8", "replace"))
    return "\n".join(parts)


def test_token_from_env_never_leaks(cli_ws):
    out = flow(cli_ws)
    assert "Missing Permissions" in out or "FAILED" in out        # the failure path was exercised too
    assert CLI_TOKEN not in out
    on_disk = all_files_text(cli_ws.root)
    assert (cli_ws.root / ".hearback" / "ledger.json").exists() and (cli_ws.root / ".hearback" / "events.jsonl").exists()
    assert (cli_ws.root / ".hearback" / "queue").is_dir()
    assert CLI_TOKEN not in on_disk


def test_token_from_file_never_leaks_beyond_that_file(cli_ws, monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN")
    f = cli_ws.root / ".hearback" / "token.txt"
    f.parent.mkdir(exist_ok=True)
    f.write_text("Bot " + CLI_TOKEN + "\n", encoding="utf-8")
    out = flow(cli_ws)
    assert "queued p-" in out and CLI_TOKEN not in out            # the file token worked (flow ran) and stayed secret
    assert CLI_TOKEN not in all_files_text(cli_ws.root, skip={"token.txt"})


def test_a_rejected_token_error_does_not_echo_it(cli_ws, monkeypatch):
    wrong = "tok_WRONG_0123456789abcdef"
    monkeypatch.setenv("DISCORD_BOT_TOKEN", wrong)
    rc, out, err = cli_ws.run("sync")
    assert rc == 1 and "401" in err and "token was rejected" in err
    assert wrong not in out + err and CLI_TOKEN not in out + err


def test_self_ignoring_data_dir_keeps_secrets_out_of_git(tmp_path, capsys):
    from hearback import cli
    cli.main(["init", "--dir", str(tmp_path)])
    assert (tmp_path / ".hearback" / ".gitignore").read_text() == "*\n"
