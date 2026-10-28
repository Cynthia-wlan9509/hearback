# Hearback — design

> Every player who reports a bug hears back.
> An agent-native, human-approved bug desk for Discord forum channels.

Status: draft v0.2 (2026-10-24; SWLink added). Extracted from a production workflow that handled a real
game community's bug forum (29 reports → 22 fixed, all reporters notified on release).

## 1. Problem

Small teams (indie / fan / revival game projects, open-source tools) collect bug reports in a
**Discord forum channel**. The pain is not *collecting* reports — Discord does that — it is the
loop after:

1. Reports pile up; the same root cause is reported 6 times in 6 different words.
2. Someone (now: an AI coding agent) re-reads the whole forum every session — slow and
   expensive in tokens, and already-handled posts get re-triaged.
3. Replies and tag changes are public. An agent should *draft* them, but a human must approve
   before anything is posted to the community.
4. When the fix ships, nobody goes back to tell the 22 people who reported it which build
   fixed it and how to update. Reporters never hear back, so they stop reporting.
5. Reporters don't know how to report well (no logs, no steps, wrong version tag).
6. Several agents (Claude Code in one terminal, Codex in another, a CI bot) step on each other.

## 2. Principles

- **Ledger is the memory.** A local JSON ledger is the single source of truth for "what we
  already know". Agents read *deltas*, not the forum.
- **Agents propose, humans dispose.** No agent-facing tool can post to Discord. Agents put
  proposals into an approval queue; a human approves (CLI, desktop GUI, or a ✅ reaction in a
  private staff channel); only then the bot sends. Safety by construction, not by prompt.
- **Close the loop.** Status `fixed-pending-release` is not the end. `hearback release` turns a
  shipped build into per-reporter, per-variant notifications.
- **Agent-agnostic.** Everything is exposed through a CLI *and* an MCP server, so Claude Code,
  OpenAI Codex, Cursor, or an OpenAI Agents SDK app share the same ledger and queue.
- **Stdlib core.** The core runs on plain Python 3.11 with no dependencies; MCP and LLM features
  are optional extras.
- **Secrets never leave the box.** The bot token is never printed, logged, or written to the
  ledger; attachments are downloaded only from Discord CDN hosts and without auth headers.

## 3. Architecture

```
            Discord forum + staff channel (REST v10, bot token)
                              │
                     hearback.discord  (client: retries, 429, multipart, CDN-only downloads)
                              │
  ┌───────────────────────────┼───────────────────────────────┐
  │ hearback.ledger   JSON ledger + file lock + atomic write    │
  │                   + events.jsonl audit log + claims (lease) │
  │ hearback.sync     incremental: only threads whose           │
  │                   last_message_id / tags changed            │
  │ hearback.queue    proposals: pending → approved → sent      │
  │                   (or rejected); executor + verify          │
  │ hearback.release  fixed-pending-release → notifications     │
  │ hearback.render   markdown dashboard                        │
  └───────────────────────────┬───────────────────────────────┘
         ┌────────────────────┼─────────────────────┬──────────────────┐
     CLI `hearback`     MCP server (stdio)     desktop GUI         staff-channel
     (humans, scripts)  (agents: read +        (approve queue,     approvals
                        triage + propose)      edit bot messages)  (✅ / ❌ reactions)
```

### 3.1 Ledger (`.hearback/ledger.json`)

```jsonc
{
  "schema": 1,
  "forum_id": "…", "synced_at": "…",
  "tags": {"<tag id>": "<tag name>"},
  "threads": {
    "<thread id>": {
      "title": "…", "author": "…", "created": "…", "tags": ["…"],
      "last_message_id": "…", "archived": false,
      "body": "…", "attachments": ["…local path or url…"],
      "replies": [{"id": "…", "author": "…", "time": "…", "content": "…", "attachments": []}],
      "triage": {
        "status": "new|investigating|need-info|fixed-pending-release|released|wontfix|not-bug|duplicate|guide",
        "cluster": "card-art-missing", "root_cause": "…", "fix": "…",
        "variants": ["free"],            // which build variants are affected
        "fixed_in": "2026.10.24",        // filled when fixed
        "reviewed": "2026-10-23", "reviewed_by": "agent:claude-1",
        "rev": 7,                         // optimistic concurrency counter
        "replies_posted": [{"message_id": "…", "time": "…", "content": "…", "proposal": "p-…"}]
      },
      "claim": {"by": "agent:codex-2", "until": "…"}   // lease, optional
    }
  }
}
```

`sync` never touches `triage` or `claim`. Unknown fields are preserved.

### 3.2 Incremental sync

1. `GET /channels/{forum}` → tag table.
2. Active threads (`/guilds/{g}/threads/active`) + archived public threads (paged).
3. For each thread: if `last_message_id`, `applied_tags`, `message_count`, title and archived flag equal
   the ledger copy → skip (no message fetch). Otherwise fetch messages, rebuild the record, keep triage.
   Edits to existing messages don't change these fields, so `sync --full` re-reads everything (also retries
   failed attachment downloads).
4. Output a **delta report** sized for an LLM context: new threads, threads with new
   *human* replies (bot's own replies excluded), tag changes. `--json` for machines.

### 3.3 Multi-agent coordination

- **Lock**: `ledger.json.lock` created with `O_CREAT|O_EXCL`; stale after 30 s; all writers use
  `with ledger.locked(): load → mutate → atomic replace`.
- **Claims**: `claim(thread, agent, minutes=30)`; others see it in `list`. Triage updates and proposals on a
  thread claimed by someone else are refused (the CLI has `--force` for humans; MCP tools have no force).
- **Optimistic concurrency**: `update_triage(thread, patch, expected_rev)` fails with a clear
  message if another agent changed it meanwhile.
- **Audit**: every mutation appends `{"t","actor","action","thread","detail"}` to
  `.hearback/events.jsonl`. `hearback log` shows who did what.
- **Actor identity**: `HEARBACK_ACTOR` env / `--as` flag / MCP client name, e.g. `agent:codex-2`,
  `human:owner`.

### 3.4 Approval queue

Proposal = one outward action with rationale:

```jsonc
{"id": "p-20261024-0007", "actor": "agent:claude-1", "created": "…",
 "kind": "reply|tags|post|newpost|pin|edit|delete",
 "thread": "…", "text": "…", "add": [], "remove": [], "attachments": [],
 "why": "same root cause as cluster card-art-missing; fixed in 2026.10.24",
 "state": "pending|approved|rejected|sent|failed", "result": {…}}
```

- Agents create proposals (`propose_reply`, `propose_tags`, …). Dry-run rendering is shown to the
  human exactly as it will appear.
- Humans approve via `hearback queue approve <id…|--all>`, the GUI, or reactions in the staff
  channel (`hearback staff push` posts each pending proposal there; `hearback staff poll` reads
  ✅/❌ reactions from allow-listed user ids and marks proposals approved/rejected — REST only,
  no gateway connection needed).
- `hearback send` claims each approved proposal by atomically moving it to `sending/` (a proposal
  rejected in the meantime is skipped), executes it with rate-limit spacing, records message ids in
  `triage.replies_posted`, then **verifies** by re-reading the thread (content present, tags
  equal) and reports mismatches. Idempotent: a proposal in state `sent` is never sent twice
  (this exact double-send happened in the original workflow and motivated the state machine);
  an interrupted send is parked in `failed/` for a human to check, never retried automatically.
- Approve / reject / send / staff poll refuse `agent:` actors. This is a tripwire for honest shell
  agents; the hard boundary is that agents do not hold the bot token (README, "Safety model").
- Every outgoing message sets `allowed_mentions: {"parse": []}`; forum-derived template values are
  sanitised (mentions neutralised, no newlines/backticks, length-capped).

### 3.5 Release closure

```
hearback release 2026.10.24 --variant free --variant full
```

- Selects threads with status `fixed-pending-release` whose `variants` intersect the release
  (or all, by default; with `--variant`, threads with no variant info are listed for a human instead of
  being notified), drafts one notification per thread from a per-variant template
  (download location, "install over the old version, your save stays"), and a release
  announcement listing the fixed clusters, grouped by variant.
- Everything lands in the approval queue. After sending, status → `released`, `fixed_in` set.

### 3.6 Reporter onboarding kit

`hearback guide` creates (as a proposal) a pinned "how to report a bug" forum post from a
template: required tags, a copy-paste report form, how to attach logs/screenshots on phone and
desktop, what happens after posting. Optional annotated screenshot strip (`hearback guide
--steps img1.png:x,y,r:"caption" …`) rendered with Pillow if available.

### 3.7 Human tools

- `hearback editor` — Tkinter GUI: pick channel/thread, list the bot's messages, edit or delete
  them (AI drafts, human polishes wording), plus an approval-queue tab.
- `hearback render` — markdown dashboard grouped by status and cluster.

### 3.8 MCP server (`hearback mcp`)

Read/triage/propose tools only — **no send tool**:

| tool | purpose |
|---|---|
| `sync(download_attachments)` | run incremental sync, return delta |
| `list_threads(status?, cluster?, unreviewed_only?, limit?)` | compact list |
| `get_thread(id)` | full record incl. attachment paths |
| `claim(id, minutes)` / `release_claim(id)` | coordination |
| `update_triage(id, patch, expected_rev)` | write triage fields |
| `propose_reply(id, text, why)` / `propose_tags(id, add, remove, why)` | queue proposals |
| `list_proposals(state?)` | see queue |
| `release_preview(version, variants)` | draft closure notifications into the queue |
| `dashboard()` | rendered markdown overview |

Config snippets for Claude Code (`.mcp.json`), Codex (`~/.codex/config.toml`
`[mcp_servers.hearback]`), and the OpenAI Agents SDK (`MCPServerStdio`) ship in `examples/`.

### 3.9 Demo mode (no Discord needed)

`hearback demo` spins up an in-process fake Discord (same REST surface) seeded with a fictional
game's forum ("Starlight Bakery", 12 reports incl. duplicates, a need-info, screenshots), runs
the whole loop — sync → (scripted or LLM) triage → proposals → approve → send → verify → release
→ notify — and prints a transcript. Judges and contributors can try it in 30 seconds.

### 3.10 SWLink: web console and chat bot (`hearback web`, optional)

A local console plus an opt-in always-on bot. It reuses the ledger, the queue and the REST client; nothing in
the agent-facing MCP surface changes.

```
 browser ──(127.0.0.1, X-SWLink-Key)──▶ hearback.web.server (stdlib http.server, JSON API + static page)
                                          │
        hearback.chatbot  rules · decide · prompt · inbox ─┬─▶ queue.propose("post", actor agent:swlink)   (draft)
        hearback.agents   system prompts                   └─▶ actions.post (REST, mentions off)           (auto)
        hearback.llm      profiles, keys, providers, fallback
        BotRuntime        discord.py gateway in its own thread (lazy import, optional extra `bot`)
```

- **Rules** (`.hearback/channels.json`): per channel `{mode: off|draft|auto, agent, trigger: mention|all,
  cooldown_s}`, plus a DM default and a server default. Everything defaults to `off`. `decide()` ignores bots and the
  bot itself, answers in guild channels only when mentioned (or replied to) under `trigger = mention`, honours the
  cooldown, and only records once the hourly cap of model calls is reached (`[swlink] hourly_cap`, default 60;
  drafts and auto replies both count, and the cooldown starts before the model call).
- **Prompting**: the agent's system prompt plus a security suffix; the last 20 messages and the new one are fenced as
  `UNTRUSTED DISCORD TEXT` with a random id, the same idea as the MCP forum fence. Replies are split with `split_text`
  and sent with `allowed_mentions: {"parse": []}`.
- **Drafts are proposals** by `agent:swlink` (kind `post`, target = channel id). Approve, reject and send stay human
  steps; the console performs them as `human:web`. Auto replies are logged as events by `bot:swlink`.
- **LLM profiles** (`.hearback/llm.json`): Anthropic or OpenAI protocol, any base URL (official, DeepSeek, relays).
  Keys are kept in the OS keyring (service `hearback-swlink`) or `HEARBACK_LLM_KEY_<ID>`; the file holds a four
  character hint only. One active profile, optional fallback to the others in order, retries, per-profile call
  counters. All error text passes through a scrubber that removes keys and tokens. New profiles default to 2048 output
  tokens (an empty answer that stopped on the token limit is an error, never a silent `""`; the Test button asks for 512
  and reports an empty answer). Each profile has `max_concurrent` (default 2, 1 to 16): a semaphore around every provider
  call, shared by chat, drafts and review. After a 429 or 503 the call waits for the relay's Retry-After (else 2 s, then
  5 s), up to `retries` times, then falls back; the profile card's last error counts these.
- **Console security**: loopback bind, per-run secret in the launch link (kept in sessionStorage, removed from the
  address bar), Host and Origin checks, JSON-only writes, static files only from the package, a strict CSP, no
  `innerHTML`. The bot token and API keys are write-only through the API.
- **AI review of the bug forum** (`review.py`): `needs_review` picks threads nobody has triaged, threads with human
  replies newer than the review, and threads whose `triage.ai` is older than the newest human activity (guides never;
  the bot's own replies never count). The prompt is the chosen agent's system prompt plus a fixed JSON output
  contract; thread text, replies, other thread titles and log excerpts are fenced as `UNTRUSTED FORUM TEXT`, capped,
  and up to three pictures are attached when the model takes them (a 400 with pictures is retried once without them).
  Invalid JSON is retried once. The answer is stored only as `triage.ai` (+ `at`, `model`, `profile`) under the ledger
  lock by `agent:swlink`; `status`, `cluster`, `root_cause`, `fix` and `reviewed` stay human. The console runs one job at
  a time in a background thread (progress, stop), lets a person adopt chosen fields (`ledger.update_triage`, actor
  `human:web`), turns an edited draft into ordinary approval-queue proposals, and exports a markdown fix list grouped by
  cluster under `.hearback/exports/`. The forum and server ids are entered in Settings and written into
  `hearback.toml` (other keys and comments kept).
- **Bot messages**: list, edit and delete what the bot itself posted in a thread or channel (the same human-only
  direct writes as the desktop editor; the server checks the author before touching a message and keeps
  `triage.replies_posted` in step).
- **Channels the bot cannot read**: with the gateway online `/api/conversations` adds `readable` (view channel and read
  message history) and `can_send` per channel from discord.py's `permissions_for(guild.me)`; the page greys unreadable
  ones, sorts them last and, like any 403 from the messages fetch (`{"error": "no_access", "bot_name": ...}`, status 403),
  shows a card that says which permissions to turn on.
- Without discord.py the console still works over REST (read channels, human send, AI draft, approvals); only the live
  gateway needs the `bot` extra. Without the forum configured, chat features need just the token.
- **Supporters** (`supporters.py`, the Supporters page): Afdian payments to a Discord role. A supporter DMs the bot an
  Afdian order number (21 to 40 digits found in free text; Discord ids have 17-20); `Supporters.handle_order` checks it with Afdian's open API
  (`https://afdian.com/api/open/`, `ping`, `query-order`, POST JSON `{user_id, params, ts, sign}` with
  `sign = md5(token + "params" + params + "ts" + ts + "user_id" + user_id)`; the token is never sent) and returns an
  `Outcome(reply_key, grant_role_ids, notify_texts)`. Rules in order: a required role first (`require_role_ids`), an
  order already claimed (by someone else: refuse and tell the owner; by the same person: re-grant a missing role),
  then the per-person daily lookup limit, then the lookup itself: API failure queues a retry (every 10 minutes, given up
  after 3 days with a note to the owner), unknown or unpaid (`status != 2`) is a polite "not found yet", an amount below
  `min_amount` is a polite refusal, otherwise the order is recorded as a claim and `role_id` (plus the plan's role from
  `plan_roles`) is granted. Replies are templates (`supporter_*`, zh and en, workspace-overridable) and never carry an
  amount; notices to the owner mask order numbers (first 4 + ... + last 4). State lives in `.hearback/supporters.json`
  (settings, `claims`, `retry`, `lookups`; a damaged file is an error, never a silent reset); the Afdian token lives
  only in the keyring (service `hearback-swlink`, user `afdian-token`) or `HEARBACK_AFDIAN_TOKEN`. The brain never
  touches Discord; `SupporterBot` applies a decision over REST (`PUT /guilds/{g}/members/{u}/roles/{r}` with
  `X-Audit-Log-Reason`), refuses to send a role the bot's top role is not above and records the exact fix for the page,
  and runs the retry loop without the gateway. DMs reach it from `App.on_message` only when `enabled and dm_handling`,
  before the AI rules. Owner actions (`link_order`, `unlink`) and bot actions are events (`human:web`, `bot:swlink`).
  `match_remarks` only suggests a member for an unclaimed order whose Afdian remark names them; the owner confirms.
  `POST /api/supporters/simulate` runs the same brain with `dry_run=True`: read-only Afdian and Discord calls, no claim,
  no lookup count, no queue entry, no write anywhere.

## 4. Config (`hearback.toml`)

```toml
[discord]
guild_id = "…"
forum_id = "…"
staff_channel_id = "…"        # optional: approvals by reaction
approvers = ["<user id>"]     # who may approve via reactions
token_env = "DISCORD_BOT_TOKEN"
token_file = ".hearback/token.txt"

[status_tags]                  # triage status -> forum tag name
investigating = "In progress"
fixed-pending-release = "Fixed, awaiting release"
need-info = "Need info"

[variants.free]
tag = "Free"
download = "#downloads pinned link"
[variants.full]
tag = "Full"
download = "#supporters channel"

[templates]
language = "en"               # en | zh — built-in reply templates

[swlink]
hourly_cap = 60               # model calls per hour (drafts + auto replies); past it messages are only recorded
autostart = false             # true: connect the bot as soon as `hearback web` starts
```

## 5. Non-goals (v0.1)

- The core has no always-on gateway bot: everything is pull-based and runs when an agent/human runs it. The chat bot in
  3.10 (SWLink) is optional and opt-in, and its replies obey the same approval rules.
- No automatic code fixing — that is the coding agent's job; Hearback gives it clean input
  (deduped clusters, attachments, logs) and takes its output (fix notes, build id).
- No GitHub Issues sync in v0.1 (planned as an export target).

## 6. Differentiators vs. existing tools

See `docs/LANDSCAPE.md` (filled from research).
