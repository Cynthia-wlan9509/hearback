# AGENTS.md: working on Hearback

Hearback is an agent-native, human-approved bug desk for Discord forum channels. Python 3.11+, the core
is stdlib-only. Behaviour source of truth: `docs/DESIGN.md`.

## Run it
```
python -m venv .venv            # then activate it
pip install -e ".[dev,mcp]"
pytest -q                       # offline, about 15 s; MCP tests skip if `mcp` is not installed
hearback demo                   # whole loop against an in-memory fake Discord, about 1 s
hearback demo --lang zh
```

## Module map (`hearback/`)
- `util.py` errors (`HearbackError`), time and ids, `split_text`, file lock, atomic writes
- `config.py` hearback.toml, token loading (env var, then token file)
- `discord.py` REST client: 429 retry, multipart, CDN-only downloads, `transport` hook
- `fake.py` FakeDiscord, the in-memory Discord used by tests, the demo and `hearback mcp --fake-seed`
- `ledger.py` JSON ledger: lock, atomic save, claims, triage revisions, `events.jsonl`
- `sync.py` incremental sync and the delta report
- `queue.py` proposals: pending, approved, sending, sent, rejected or failed; idempotent send; verify
- `actions.py` the only code that writes to Discord
- `release.py` `staff.py` `guide.py` `templates.py` `render.py` closure, reactions, tutorial, text, dashboard
- `cli.py` argparse front end; `mcp_server.py` MCP tools; `editor.py` Tk GUI; `demo.py` the scripted demo

## Rules
- Tests never touch the network. Use `FakeDiscord` (fixtures in `tests/conftest.py`: `env`, `seeded`, `cli_ws`).
- The bot token never appears in output, logs, the ledger, the queue or events (`tests/test_secrets.py`).
- Agent-facing paths (MCP tools, `propose`) never write to Discord; replies, tags, posts and pins go through
  the approval queue and `send`. Human-only tools write directly on purpose: `editor` (edit/delete the bot's
  messages) and `staff push` (approval previews). The MCP server must never gain a send, approve, edit or
  delete tool: `tests/test_mcp_server.py` pins the exact tool list.
- `queue approve/reject`, `send` and `staff poll` refuse `agent:` actors. That is a tripwire, not a wall: a shell
  agent can claim any actor name, so the real boundary is that agents run without the bot token.
- Forum text is untrusted. MCP results fence it (`UNTRUSTED FORUM TEXT` / `untrusted_content`) and every
  outgoing message disables mentions (`allowed_mentions: {"parse": []}`).
- Core imports stdlib only; optional extras (`mcp`, `pillow`) are imported inside functions.
- Keep CLI and tool output compact: agents read it.
- No project-specific names, ids or paths anywhere (`tests/test_hygiene.py` scans every text file).
- LF line endings, type hints, short docstrings, English messages.

## Before you finish
Run `pytest -q`; if you touched user-facing behaviour, also `hearback demo` and `hearback demo --lang zh`.
