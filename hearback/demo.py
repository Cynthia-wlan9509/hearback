"""`hearback demo`: the whole loop, offline, against an in-memory fake Discord.

A fictional bakery game ("Starlight Bakery") has a bug forum with 12 reports: duplicates, one report
that needs more info, one that is not a bug, screenshots, two build variants. Two scripted "agents"
triage it (no LLM involved), a human approves, the bot sends, the fix ships and every reporter hears back.
"""
from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from hearback import release as release_mod, staff as staff_mod, templates
from hearback.config import Config, Variant
from hearback.fake import FakeDiscord, make_png
from hearback.guide import propose_guide
from hearback.ledger import ClaimError, ConflictError, Ledger, triage_of
from hearback.queue import Queue
from hearback.render import render_dashboard
from hearback.sync import format_delta, sync
from hearback.util import sid6

# tag key -> (English name, Chinese name)
TAGS = {
    "crash": ("Crash", "崩溃"), "bug": ("Bug", "缺陷"), "ui": ("UI", "界面"), "text": ("Text", "文本"),
    "feature": ("Feature", "建议"), "free": ("Free", "体验版"), "full": ("Full", "完整版"),
    "investigating": ("In progress", "处理中"), "need-info": ("Need info", "需要补充"),
    "fixed": ("Fixed, awaiting release", "已修复待发布"), "released": ("Released", "已发布"),
}

# key, author, tag keys, screenshot?, (title en, zh), (body en, zh)
REPORTS: list[dict[str, Any]] = [
    dict(key="oven_a", who="Mira", tags=["bug", "free"], shot=True,
         title=("Oven timer freezes at 0:00", "烤箱计时器卡在 0:00"),
         body=("Second bake of the day: the timer stops at 0:00 and the bread never comes out. Android 14, build 2026.10.10.",
               "每天第二次烘焙时，计时器停在 0:00，面包出不来。安卓 14，版本 2026.10.10。")),
    dict(key="oven_b", who="Jon", tags=["bug", "full"], shot=False,
         title=("Baking stuck, cannot collect bread", "烘焙卡住，拿不到面包"),
         body=("The timer says 00:00 but the collect button stays grey. Every time after the first bake. Full build on PC.",
               "计时器显示 00:00，但收取按钮一直是灰的。第一次烘焙之后每次都这样。PC 完整版。")),
    dict(key="oven_c", who="Aiko", tags=["bug", "free"], shot=True,
         title=("Bread never finishes?", "面包一直烤不完？"),
         body=("Waited 20 minutes, the oven timer is frozen. Phone, free build.",
               "等了 20 分钟，烤箱计时器是冻住的。手机体验版。")),
    dict(key="oven_dup", who="Mira", tags=["bug", "free"], shot=False,
         title=("Oven timer bug (posting again with details)", "烤箱计时器问题（补充细节重发）"),
         body=("Sorry for the repost: it only happens when I close and reopen the oven screen.",
               "抱歉重发：只有关掉再打开烤箱界面时才会这样。")),
    dict(key="recipe_a", who="Leo", tags=["bug", "free"], shot=False,
         title=("Recipe book is blank after the update", "更新后食谱册是空白的"),
         body=("Opened the recipe book after updating and every page is empty.", "更新后打开食谱册，每一页都是空的。")),
    dict(key="recipe_b", who="Sana", tags=["bug", "free"], shot=False,
         title=("All my recipes are gone", "我的食谱全没了"),
         body=("Did I lose my save? The recipe list is empty but my coins are fine.",
               "我的存档是不是丢了？食谱列表是空的，但金币还在。")),
    dict(key="recipe_c", who="Omar", tags=["ui", "free"], shot=True,
         title=("Recipe page is empty on tablet", "平板上食谱页是空的"),
         body=("Same on my tablet, screenshot attached.", "平板上也一样，附截图。")),
    dict(key="crash_info", who="Pat", tags=["crash"], shot=False,
         title=("Game crashes sometimes", "游戏有时会崩溃"),
         body=("It just crashes. Please fix.", "就是会崩溃，请修复。")),
    dict(key="not_bug", who="Ravi", tags=["bug", "full"], shot=False,
         title=("Baguette takes 3 minutes to bake", "法棍要烤 3 分钟"),
         body=("Way too slow, is my oven broken?", "太慢了，是我的烤箱坏了吗？")),
    dict(key="flicker", who="Lina", tags=["ui", "full"], shot=True,
         title=("Customers flicker on tablet", "平板上顾客角色会闪烁"),
         body=("Customer sprites flicker while they walk to the counter.", "顾客走向柜台时角色会闪烁。")),
    dict(key="typo", who="Ben", tags=["text", "free"], shot=False,
         title=("Typo in the shop: Croissaint", "商店里的错别字"),
         body=("The shop lists 'Croissaint'.", "商店里把一个名字写错了。")),
    dict(key="dark", who="Zoe", tags=["feature"], shot=False,
         title=("Please add a dark mode", "希望增加夜间模式"),
         body=("The white background hurts at night.", "晚上白色背景太刺眼了。")),
]

OVEN = ("oven-timer-freeze", ("the oven timer state is dropped when the oven screen is reopened",
                             "重新打开烤箱界面时计时器状态被丢弃"),
        ("keep the timer state across screen reopen", "跨界面保留计时器状态"),
        ("The oven timer no longer freezes on the second bake.", "烤箱计时器不会再在第二次烘焙时卡住。"))
RECIPE = ("recipe-book-blank", ("the save migration skipped the recipe list for older saves",
                                "存档迁移漏掉了旧存档里的食谱列表"),
          ("migrate the recipe list for old saves", "为旧存档补迁食谱列表"),
          ("Your recipes are back; your save was never touched.", "食谱已恢复，你的存档从未被改动。"))

# key -> (status, cluster-info or None, variants)
TRIAGE: dict[str, tuple[str, Any, list[str]]] = {
    "oven_a": ("fixed-pending-release", OVEN, ["free"]), "oven_b": ("fixed-pending-release", OVEN, ["full"]),
    "oven_c": ("fixed-pending-release", OVEN, ["free"]),
    "recipe_a": ("fixed-pending-release", RECIPE, ["free"]), "recipe_b": ("fixed-pending-release", RECIPE, ["free"]),
    "recipe_c": ("fixed-pending-release", RECIPE, ["free"]),
    "flicker": ("fixed-pending-release", ("customer-flicker", ("the customer sprite atlas is rebuilt every frame on tablets",
                                                             "平板上顾客图集每帧重建"),
                                         ("cache the sprite atlas", "缓存图集"),
                                         ("Customers no longer flicker on tablets.", "平板上顾客不会再闪烁。")), ["full"]),
    "typo": ("fixed-pending-release", ("shop-typo", ("misspelled item name", "道具名写错"), ("fix the spelling", "改正拼写"),
                                       ("The typo in the shop is fixed.", "商店里的错别字已改正。")), ["free"]),
}

TEXT: dict[str, dict[str, str]] = {
    "en": {
        "h1": "1/7  Sync the forum into the ledger", "h2": "2/7  Two agents triage (nothing is sent yet)",
        "h3": "3/7  The approval queue", "h4": "4/7  Human approval: staff-channel reactions + CLI",
        "h5": "5/7  Send the approved proposals and verify them against Discord",
        "h6": "6/7  Incremental sync: only changed threads are fetched", "h7": "7/7  The fix ships: close the loop",
        "intro": "Hearback demo: fictional game 'Starlight Bakery', 12 bug reports, fake in-memory Discord (offline).",
        "workspace": "workspace: %s",
        "claim": "agent:codex-2 claims %s (crash report without details) for 30 min",
        "refused": "agent:claude-1 tries to propose on it -> refused: %s",
        "conflict": "agent:claude-1 saves with a stale revision -> %s",
        "retry": "agent:claude-1 re-reads (rev %d) and saves again -> ok",
        "triaged": "triaged %d threads into %d clusters; replies and tag changes are only drafted",
        "queue": "queue: %d pending proposals (replies, tag changes, the pinned reporter guide)",
        "preview": "--- preview, exactly what will be posted ---",
        "staff": "staff channel: %d proposals pushed; Dana (approver) reacted on %s, Troll (not an approver) on %s",
        "poll": "poll -> approved %d, ignored %d reaction(s) from non-approvers",
        "approve_cli": "human:demo approves the remaining %d pending proposals with `queue approve --all --yes`",
        "double": "second `send` right away -> %s (a sent proposal is never sent twice)",
        "nothing": "nothing approved to send",
        "player": "a player replies in %s: 'Thanks, I will wait for the update.'",
        "release": "Free build 2026.10.24 ships. `hearback release 2026.10.24 --variant free`",
        "release2": "Full build 2026.10.26 ships later. `hearback release 2026.10.26 --variant full`",
        "approve_n": "human:demo approves %d proposals, `send` -> %d sent, %d mismatches",
        "final": "`hearback release <version> --finalize` -> %d threads are now 'released' with fixed_in set",
        "dash": "dashboard (`hearback render`):", "done": "Done in %.1fs. Every one of the %d reporters was answered.",
        "kept": "workspace kept at %s",
        "not_bug_expl": "baguettes bake for 3 minutes on purpose; the Convection upgrade halves it.",
        "wontfix_expl": "a dark theme needs a full asset pass; it is on the long-term list, not the next release.",
        "why_dup": "same root cause as cluster oven-timer-freeze (posted by the same player)",
        "why_info": "no build, device or steps; ask before triaging",
        "why_nb": "works as designed", "why_wf": "feature request out of scope for now",
    },
    "zh": {
        "h1": "1/7  把论坛同步进台账", "h2": "2/7  两个 agent 分诊（此时什么都没发出去）",
        "h3": "3/7  审批队列", "h4": "4/7  人工审批：staff 频道表情 + 命令行",
        "h5": "5/7  发送已批准的提案，并回读 Discord 校验",
        "h6": "6/7  增量同步：只拉取有变化的帖子", "h7": "7/7  修复发布：把闭环做完",
        "intro": "Hearback 演示：虚构游戏「Starlight Bakery」，12 条 bug 反馈，内存里的假 Discord（完全离线）。",
        "workspace": "工作目录：%s",
        "claim": "agent:codex-2 认领 %s（没写任何细节的崩溃反馈），租期 30 分钟",
        "refused": "agent:claude-1 想在这帖上提案 -> 被拒绝：%s",
        "conflict": "agent:claude-1 用过期的版本号保存 -> %s",
        "retry": "agent:claude-1 重新读取（rev %d）后再保存 -> 成功",
        "triaged": "已分诊 %d 个帖子、归并成 %d 个问题簇；回复和标签变更只是起草，尚未发出",
        "queue": "队列里有 %d 条待审批提案（回复、标签变更、置顶的反馈指南）",
        "preview": "--- 预览，和实际发出去的一模一样 ---",
        "staff": "已推送 %d 条提案到 staff 频道；Dana（审批人）在 %s 上点了表情，Troll（不是审批人）在 %s 上点了表情",
        "poll": "轮询 -> 批准 %d 条，忽略非审批人的 %d 个表情",
        "approve_cli": "human:demo 用 `queue approve --all --yes` 批准剩下的 %d 条",
        "double": "紧接着再 `send` 一次 -> %s（已发送的提案绝不会重发）",
        "nothing": "没有已批准待发送的提案",
        "player": "有玩家在 %s 里回复：「谢谢，我等更新。」",
        "release": "体验版 2026.10.24 发布。`hearback release 2026.10.24 --variant free`",
        "release2": "完整版 2026.10.26 随后发布。`hearback release 2026.10.26 --variant full`",
        "approve_n": "human:demo 批准 %d 条，`send` -> 发送 %d 条，校验不一致 %d 条",
        "final": "`hearback release <版本> --finalize` -> %d 个帖子已标为 released，并记录 fixed_in",
        "dash": "看板（`hearback render`）：", "done": "用时 %.1f 秒。%d 位反馈者全部得到了回复。",
        "kept": "工作目录已保留：%s",
        "not_bug_expl": "法棍就是要烤 3 分钟，对流烤箱升级后会减半。",
        "wontfix_expl": "夜间模式需要整套素材重做，已列入长期计划，不会出现在下个版本。",
        "why_dup": "与问题簇 oven-timer-freeze 同根因（同一玩家重发）",
        "why_info": "没有版本、设备和步骤，先追问再分诊",
        "why_nb": "按设计工作", "why_wf": "功能建议，暂不在范围内",
    },
}


@dataclass
class World:
    fake: FakeDiscord
    cfg: Config
    ids: dict[str, str]
    forum: str
    staff: str
    announce: str
    dana: dict
    troll: dict


def build_world(lang: str, root: Path) -> World:
    """Seed the fake forum and write a matching Config (no file needed)."""
    i = 0 if lang == "en" else 1
    name = lambda k: TAGS[k][i]  # noqa: E731
    fake = FakeDiscord(page_size=3)
    forum = fake.add_forum("bug-reports" if lang == "en" else "bug-反馈", [name(k) for k in TAGS])
    staff, announce = fake.add_channel("staff-approvals"), fake.add_channel("announcements")
    ids: dict[str, str] = {}
    for n, r in enumerate(REPORTS):
        files = [("screenshot.png", make_png(24, 16, (200 - 12 * n, 120 + 6 * n, 80 + 10 * n)))] if r["shot"] else []
        ids[r["key"]] = fake.add_thread(forum, r["title"][i], r["who"], r["body"][i], [name(t) for t in r["tags"]],
                                        files=files, archived=r["key"] in ("typo", "dark"))
    fake.add_reply(ids["oven_a"], "Jon", "Confirming on my phone too." if lang == "en" else "我手机上也是这样。")
    fake.add_reply(ids["recipe_b"], "Tom", "Same here, my recipes vanished as well." if lang == "en" else "我也一样，食谱全没了。")
    dana, troll = fake.add_user("Dana"), fake.add_user("Troll")
    cfg = Config(root=root, guild_id=fake.guild_id, forum_id=forum, staff_channel_id=staff, announce_channel_id=announce,
                 approvers=[dana["id"]], send_delay=0.0, language=lang,
                 status_tags={"investigating": name("investigating"), "need-info": name("need-info"),
                              "fixed-pending-release": name("fixed"), "released": name("released")},
                 variants={"free": Variant("free", name("free"), "the pinned link in #downloads" if lang == "en" else "#下载 频道的置顶链接"),
                           "full": Variant("full", name("full"), "the #supporters channel" if lang == "en" else "#supporters 频道")})
    return World(fake, cfg, ids, forum, staff, announce, dana, troll)


def run_demo(lang: str = "en", keep: bool = False, out: Callable[[str], None] = print) -> int:
    """Run the full loop and print a transcript. Returns 0 on success."""
    t0 = time.monotonic()
    T = TEXT[lang]
    i = 0 if lang == "en" else 1
    root = Path(tempfile.mkdtemp(prefix="hearback-demo-"))
    try:
        w = build_world(lang, root)
        cfg, ids = w.cfg, w.ids
        ledger, queue = Ledger(cfg.ledger_path), None
        queue = Queue(cfg.queue_dir, ledger)
        client = w.fake.client()
        nosleep: Callable[[float], None] = lambda s: None  # noqa: E731

        def head(key: str) -> None:
            out("")
            out("== " + T[key] + " ==")

        def agent_say(who: str, text: str) -> None:
            out("  [%s] %s" % (who, text))

        out(T["intro"])
        out(T["workspace"] % (root if keep else "<temp dir>"))

        # ---- 1. sync
        head("h1")
        out(format_delta(sync(client, ledger, cfg, actor="agent:claude-1")))

        # ---- 2. triage
        head("h2")
        a1, a2 = "agent:claude-1", "agent:codex-2"
        ledger.claim(ids["crash_info"], a2, 30)
        out("  " + T["claim"] % sid6(ids["crash_info"]))
        try:
            queue.propose("reply", actor=a1, thread=ids["crash_info"], text="x", why="")
        except ClaimError as e:
            out("  " + T["refused"] % e)
        ledger.update_triage(ids["crash_info"], {"status": "need-info", "cluster": "no-details"}, actor=a2)
        queue.propose("reply", actor=a2, thread=ids["crash_info"], why=T["why_info"],
                      text=templates.render("need_info", lang, cfg.templates_dir, author="Pat", title=""))
        queue.propose("tags", actor=a2, thread=ids["crash_info"], add=[cfg.status_tags["need-info"]],
                      why=T["why_info"])
        ledger.release_claim(ids["crash_info"], a2)

        ledger.update_triage(ids["oven_dup"], {"status": "investigating", "cluster": TRIAGE["oven_a"][1][0]}, actor=a2)
        try:
            ledger.update_triage(ids["oven_dup"], {"status": "duplicate"}, actor=a1, expected_rev=0)
        except ConflictError as e:
            out("  " + T["conflict"] % e)
        rev = triage_of(ledger.load()["threads"][ids["oven_dup"]])["rev"]
        out("  " + T["retry"] % rev)
        ledger.update_triage(ids["oven_dup"], {"status": "duplicate", "cluster": OVEN[0], "variants": ["free"]},
                             actor=a1, expected_rev=rev)
        queue.propose("reply", actor=a1, thread=ids["oven_dup"], why=T["why_dup"],
                      text=templates.render("duplicate", lang, cfg.templates_dir, author="Mira",
                                            original=sid6(ids["oven_a"])))
        for key, (status, info, variants) in TRIAGE.items():
            cluster, cause, fix, note = info
            ledger.update_triage(ids[key], {"status": status, "cluster": cluster, "root_cause": cause[i], "fix": fix[i],
                                            "release_note": note[i], "variants": variants}, actor=a1)
            rec = ledger.load()["threads"][ids[key]]
            release_mod.propose_status_tags(queue, cfg, rec, ids[key], "new", status, a1)
        for key, status, tmpl, why, expl in (("not_bug", "not-bug", "not_bug", "why_nb", "not_bug_expl"),
                                             ("dark", "wontfix", "wontfix", "why_wf", "wontfix_expl")):
            who = next(r["who"] for r in REPORTS if r["key"] == key)
            ledger.update_triage(ids[key], {"status": status, "cluster": key.replace("_", "-")}, actor=a1)
            queue.propose("reply", actor=a1, thread=ids[key], why=T[why],
                          text=templates.render(tmpl, lang, cfg.templates_dir, author=who,
                                                explanation=templates.safe(T[expl])))
        n_clusters = len({triage_of(r).get("cluster") for r in ledger.load()["threads"].values() if triage_of(r).get("cluster")})
        out("  " + T["triaged"] % (sum(1 for r in ledger.load()["threads"].values() if triage_of(r).get("reviewed")), n_clusters))

        # ---- 3. queue
        head("h3")
        propose_guide(queue, cfg, a1, tags=[])
        pending = queue.list("pending")
        out("  " + T["queue"] % len(pending))
        out("  " + T["preview"])
        for p in (next(p for p in pending if p.kind == "reply" and p.actor == a2),
                  next(p for p in pending if p.kind == "reply" and p.thread == ids["oven_dup"])):
            out(queue.render(p, ledger.load()))
        out("  " + "-" * 8)
        for p in pending[:6]:
            out("  " + queue.summary_line(p, ledger.load()))
        out("  ...")

        # ---- 4. approval
        head("h4")
        shown = staff_mod.push(client, queue, cfg, sleep=nosleep, data=ledger.load()).pushed
        first, second = shown[0], shown[1]
        w.fake.react(w.staff, first.staff_message_id, staff_mod.APPROVE, w.dana["id"])
        w.fake.react(w.staff, second.staff_message_id, staff_mod.APPROVE, w.troll["id"])
        out("  " + T["staff"] % (len(shown), first.id, second.id))
        res = staff_mod.poll(client, queue, cfg)
        out("  " + T["poll"] % (len(res.approved), res.ignored))
        left = [p.id for p in queue.list("pending")]
        queue.approve(left, "human:demo")
        out("  " + T["approve_cli"] % len(left))

        # ---- 5. send
        head("h5")
        rep = queue.send(client, ledger, forum_id=cfg.forum_id, delay=0, sleep=nosleep, actor="human:demo")
        out("\n".join("  " + ln for ln in rep.lines()))
        again = queue.send(client, ledger, forum_id=cfg.forum_id, delay=0, sleep=nosleep, actor="human:demo")
        out("  " + T["double"] % (again.lines()[0] if again.lines() else T["nothing"]))

        # ---- 6. incremental sync
        head("h6")
        w.fake.add_reply(ids["oven_c"], "Aiko", "Thanks, I will wait for the update." if lang == "en" else "谢谢，我等更新。")
        out("  " + T["player"] % sid6(ids["oven_c"]))
        calls_before = w.fake.count("GET", r"/channels/\d+/messages")
        out(format_delta(sync(client, ledger, cfg, actor="agent:claude-1")))
        out("  (GET messages calls in this sync: %d)" % (w.fake.count("GET", r"/channels/\d+/messages") - calls_before))

        # ---- 7. release
        head("h7")
        for key, version, variant in (("release", "2026.10.24", "free"), ("release2", "2026.10.26", "full")):
            out("  " + T[key])
            plan = release_mod.release(version, [variant], cfg, ledger, queue, "agent:claude-1")
            out("\n".join("  " + ln for ln in plan.lines()[:1]))
            if key == "release":
                out("  " + T["preview"])
                out(queue.render(next(p for p in plan.proposals if p.kind == "reply"), ledger.load()))
                out(queue.render(next(p for p in plan.proposals if p.kind == "post"), ledger.load()))
            queue.approve([p.id for p in plan.proposals], "human:demo")
            r = queue.send(client, ledger, forum_id=cfg.forum_id, delay=0, sleep=nosleep, actor="human:demo")
            out("  " + T["approve_n"] % (len(plan.proposals), len(r.sent), len(r.mismatches)))
            fin = release_mod.finalize(version, ledger, queue, "human:demo")
            out("  " + T["final"] % len(fin.finalized))
        out("")
        out(T["dash"])
        out(render_dashboard(ledger.load(), cfg))
        out("")
        out(T["done"] % (time.monotonic() - t0, len(REPORTS)))
        if keep:
            out(T["kept"] % root)
        return 0
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
