# Landscape — what already exists, and where Hearback is different

Survey date: 2026-10-24. Star counts / activity from the GitHub API at that date; feature claims
from each project's README or product page (not verified by running the code).

Feature keys used below:

| key | Hearback feature |
|---|---|
| F1 | Incremental ledger as agent memory (agents read deltas, reviewed posts never re-triaged) |
| F2 | Root-cause clusters; triage status mapped to forum tags |
| F3 | Agent drafts → human approves a batch → bot sends → re-read verifies 0 mismatches; idempotent |
| F4 | Release closure: every "fixed, awaiting release" reporter gets a version- and variant-specific reply |
| F5 | Attachments (screenshots, logs) downloaded for multimodal agents, Discord-CDN allow-list |
| F6 | Reporter onboarding kit: pinned "how to report" post with annotated screenshots |
| F7 | Human desk: GUI to approve the queue and edit/delete the bot's own messages |
| F8 | Secrets never printed; agents have no send capability at all |
| F9 | Multi-agent: one ledger shared by Claude Code, Codex, Agents SDK apps (MCP), with claims, revisions, audit log |

## Closest projects

| project | what it does | has | missing |
|---|---|---|---|
| [GitHub-Issues-Discord-Threads-Bot](https://github.com/holmityd/GitHub-Issues-Discord-Threads-Bot) (67★, 2024) | forum post ↔ GitHub issue, reply mirroring | F5 (png/jpeg) | no AI, no approval, no release loop |
| [discord-github-sync-bot](https://github.com/Sillhouette/discord-github-sync-bot) | two-way sync, tag→label, R2 attachment mirror | F5 partly | F1–F4, F6–F9 |
| [discord-forum-issues-bot](https://github.com/JordiMa/discord-forum-issues-bot) | status labels; "Released in vX" in GitHub release notes | F4 on the GitHub side only | reporters in Discord are not notified |
| [Fixed-Bot](https://github.com/FAForeverRustClient/Fixed-Bot) | `/issue`, `/fixed` posts an embed in the thread | per-thread "fixed" | no AI, manual per thread, no versions |
| [isobot](https://github.com/CodyBontecou/isobot) | agent drafts issues/replies, asks follow-ups, dry-run | F3 partly, F8 partly | no batch queue, no ledger, no release loop |
| [soray-bug-triage](https://github.com/louisjcb/soray-bug-triage) | Claude classifies channel reports, moderator approves issue creation | single-item approval | no forum write-back, no dedupe |
| [issue-butler](https://github.com/blissmediastudio/issue-butler) | moderator-approved issues, "Fixed!" status pings | per-issue notice | no versions/variants, no ledger |
| [BetaHub](https://betahub.io/features/) (commercial) | forum → reports, AI similarity dedupe, [triage queue](https://betahub.io/docs/features/bug-triage-queue/), status back to Discord, MCP | F2, F3 (web queue), F5 | closed SaaS; daily batches; no release closure; no onboarding kit |
| [Threadline](https://www.hunted.space/product/threadline) (commercial) | forum ↔ GitHub, AI labels | F2 (labels) | no approval, no release loop |
| [mcp-discord](https://github.com/barryyip0625/mcp-discord) (108★) and other Discord MCP servers | raw Discord tools for agents | — | an agent with these can post anything immediately; no memory, no workflow |
| [github/gh-aw](https://github.com/github/gh-aw) (5.4k★) | agentic workflows on GitHub with validated "safe outputs" | the propose-then-validate idea (inspiration for F8) | GitHub only |
| [AnswerOverflow](https://github.com/AnswerOverflow/AnswerOverflow), [kapa.ai](https://docs.kapa.ai/integrations/discord-bot/) | index / auto-answer Discord threads | — | support Q&A, not bug triage |

Release-notification prior art outside Discord: Sentry's "resolve in next release", semantic-release
commenting the version on resolved issues, Upvoty notifying voters. None of them talk to the people who
reported the bug in a community forum.

## What Hearback does differently (and what it doesn't)

Honest scoping first. Others already do parts of this: BetaHub has AI similarity dedupe, a human triage
queue, status written back to Discord and an MCP endpoint; issue-butler and Fixed-Bot post a "fixed" message
in the thread; sync bots mirror threads into GitHub. Hearback's contribution is the combination below, open
source and agent-agnostic:

1. **Release closure per release, per variant (F4).** Existing "fixed" notices are per issue and say *that*
   something was fixed. `hearback release 1.2.0 --variant free` turns one shipped build into one reply per
   affected report, worded for the build the reporter runs (where to download it, "install over the old one,
   your save stays"), queued for one batch approval, then the threads move to `released`.
2. **Agents propose, a human disposes — enforced where it can be (F3 + F8).** MCP agents have no write path at
   all; sending is a separate human step with an idempotent state machine and read-back verification; approvals
   can come from a phone (✅ in a private staff channel, allow-listed, bound to the exact preview). For agents
   that also have a shell, the honest boundary is credential separation — the README's threat model says so.
3. **Memory for agents, not a mirror for humans (F1).** The ledger exists to make the *next agent session*
   cheap: unchanged threads need no per-thread fetch, the delta report is a few lines, reviewed posts are not
   re-triaged.
4. **Many agents, one desk (F9).** Claims with leases (enforced on triage and proposals), triage revisions with
   conflict errors, and an audit log let a Claude Code session triage while a Codex session fixes.
5. **Better reports at the source (F6).** A generated, pinned "how to report a bug" post, including how to export
   logs on a phone, with an annotated screenshot strip.
6. **Human polish after the fact (F7).** The owner can edit or delete the bot's messages from a desktop GUI —
   tone matters in small communities.

## Positioning in one line

BetaHub-style triage, but open source and agent-agnostic (MCP), with a human approval boundary, and it
finishes the job: when the fix ships, every reporter hears back about the build they run.
