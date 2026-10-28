"""Seed text for `hearback web --demo`: a fictional bakery game and its small community. Every value is a pair
(English, Chinese); `pick` takes one side. Nothing here is real: names, orders and reports are made up."""
from __future__ import annotations

from typing import Any

Pair = tuple[str, str]
LANGS = ("en", "zh")


def pick(v: Any, lang: str) -> Any:
    """The English or Chinese side of a pair; lists and dicts are walked."""
    i = LANGS.index(lang)
    if isinstance(v, tuple) and len(v) == 2 and all(isinstance(x, str) for x in v):
        return v[i]
    if isinstance(v, list):
        return [pick(x, lang) for x in v]
    if isinstance(v, dict):
        return {k: pick(x, lang) for k, x in v.items()}
    return v


GUILD: Pair = ("Demo Server", "演示服务器")
FORUM: Pair = ("bug-reports", "bug-反馈")
CHANNELS: dict[str, Pair] = {"general": ("general", "闲聊"), "news": ("announcements", "公告"),
                             "test": ("bot-test", "测试频道")}
MODEL_NAME: Pair = ("Demo model", "演示模型")
BOT_NAME: Pair = ("Demo Bot", "演示机器人")
ROLES: dict[str, Pair] = {"verified": ("Verified", "验证用户"), "supporter": ("Supporter", "支持者"),
                          "bot": ("Demo Bot", "演示机器人"), "mod": ("Moderator", "版主")}

# username, display name, roles
MEMBERS: list[tuple[str, Pair, list[str]]] = [
    ("dana_o", ("Dana", "丹娜"), ["mod", "verified"]), ("tom_d", ("Tom", "汤姆"), ["mod", "verified"]),
    ("mira_k", ("Mira", "米拉"), ["verified"]), ("jonny_w", ("Jon", "阿杰"), ["verified"]),
    ("lina_p", ("Lina", "林娜"), ["verified", "supporter"]), ("ben_t", ("Ben", "小本"), ["verified"]),
    ("zoe_c", ("Zoe", "小周"), ["verified"]), ("omar_h", ("Omar", "奥马"), ["verified"]),
    ("aiko_s", ("Aiko", "爱子"), ["verified", "supporter"]), ("pat_r", ("Pat", "阿派"), []),
    ("ravi_m", ("Ravi", "拉维"), ["verified"]), ("sana_l", ("Sana", "莎娜"), ["verified"]),
]

# forum tags: key -> name
TAGS: dict[str, Pair] = {
    "free": ("Free", "体验版"), "full": ("Full", "完整版"),
    "always": ("Every time", "必现"), "sometimes": ("Sometimes", "偶现"), "once": ("Once", "仅一次"),
    "investigating": ("Investigating", "处理中"), "need-info": ("Need info", "需要补充"),
    "fixed-pending-release": ("Fixed, awaiting release", "已修复待发布"), "released": ("Released", "已发布"),
    "not-bug": ("Not a bug", "不是缺陷"), "duplicate": ("Duplicate", "重复"), "wontfix": ("Won't fix", "不修"),
}
STATUS_TAGS = ("investigating", "need-info", "fixed-pending-release", "released", "not-bug", "duplicate", "wontfix")
VARIANTS: dict[str, Pair] = {"free": ("the pinned link in #announcements", "公告频道里置顶的链接"),
                             "full": ("the supporters channel", "支持者频道")}

# key, author (username), tags, screenshot?, title, body, replies [(username, text)]
THREADS: list[dict[str, Any]] = [
    dict(key="shop", who="mira_k", tags=["free", "always"], shot=True,
         title=("Shop screen freezes after buying twice", "连续买两次东西后商店界面卡死"),
         body=("Open the shop, buy two items in a row, and the whole screen stops responding. I have to force-close "
               "the game. Android 14, build 2026.10.18.",
               "打开商店连续买两件东西，整个画面就没反应了，只能强行关闭游戏。安卓 14，版本 2026.10.18。"),
         replies=[("jonny_w", ("Same on my tablet, right after the second purchase.", "我的平板也是，刚买完第二件就卡住。")),
                  ("tom_d", ("Thanks both. Does it also happen with the very first purchase?",
                             "谢谢两位。第一次买东西的时候也会这样吗？"))]),
    dict(key="shop_dup", who="jonny_w", tags=["free", "sometimes"], shot=False,
         title=("Stuck on the shop loading spinner", "商店一直在转圈"),
         body=("Tapped the shop and the spinner never goes away. It works again after a restart.",
               "点进商店后一直转圈，重启游戏才恢复。"), replies=[]),
    dict(key="crash", who="pat_r", tags=["sometimes"], shot=False,
         title=("Game crashes sometimes", "游戏有时会崩溃"), body=("It just crashes. Please fix.", "就是会崩溃，请修复。"),
         replies=[("dana_o", ("Which device are you on, and what were you doing right before?",
                              "你用的是什么设备？崩溃前在做什么？"))]),
    dict(key="recipe", who="sana_l", tags=["full", "always"], shot=False,
         title=("Recipe book is blank after the update", "更新后食谱册是空白的"),
         body=("After updating to the latest build every page of the recipe book is empty. My coins are fine.",
               "更新到最新版本后，食谱册每一页都是空的。金币还在。"),
         replies=[("omar_h", ("Mine too, but only on the tablet.", "我也是，不过只有平板上这样。"))]),
    dict(key="oven", who="lina_p", tags=["free", "always", "fixed-pending-release"], shot=True,
         title=("Oven timer stops at 0:00", "烤箱计时器卡在 0:00"),
         body=("Second bake of the day: the timer stops at 0:00 and the bread never comes out.",
               "每天第二次烘焙时，计时器停在 0:00，面包出不来。"),
         replies=[("tom_d", ("Found it, the fix ships in the next build. Thanks for the clear report!",
                             "找到原因了，修复会在下个版本发布。谢谢你写得这么清楚！"))]),
    dict(key="baguette", who="ravi_m", tags=["full", "always", "not-bug"], shot=False,
         title=("Baguette takes 3 minutes to bake", "法棍要烤 3 分钟"),
         body=("Way too slow, is my oven broken?", "太慢了，是我的烤箱坏了吗？"),
         replies=[("tom_d", ("That is on purpose: baguettes bake for 3 minutes. The Convection upgrade halves it.",
                             "这是设计如此：法棍就是要烤 3 分钟，对流烤箱升级后会减半。"))]),
    dict(key="dark", who="zoe_c", tags=[], shot=False,
         title=("Please add a dark mode", "希望增加夜间模式"),
         body=("The white background hurts at night.", "晚上白色背景太刺眼了。"), replies=[]),
    dict(key="flicker", who="ben_t", tags=["full", "sometimes"], shot=True,
         title=("Customers flicker on tablet", "平板上顾客角色会闪烁"),
         body=("Customer sprites flicker while they walk to the counter. Only on my tablet.",
               "顾客走向柜台时角色会闪烁。只在我的平板上出现。"), replies=[]),
]

# what a person decided (written as human:web); the others stay "new"
HUMAN_TRIAGE: dict[str, dict[str, Pair]] = {
    "crash": {"status": ("need-info", "need-info"), "cluster": ("crash-no-details", "crash-no-details")},
    "recipe": {"status": ("investigating", "investigating"), "cluster": ("recipe-book-blank", "recipe-book-blank")},
    "oven": {"status": ("fixed-pending-release", "fixed-pending-release"),
             "cluster": ("oven-timer-freeze", "oven-timer-freeze"),
             "root_cause": ("the oven timer state is dropped when the oven screen is reopened",
                            "重新打开烤箱界面时计时器状态被丢弃"),
             "fix": ("keep the timer state across screen reopen", "跨界面保留计时器状态")},
    "baguette": {"status": ("not-bug", "not-bug"), "cluster": ("by-design", "by-design"),
                 "root_cause": ("works as designed", "按设计工作")},
}
AI_APPLIED = ("shop", "shop_dup", "crash", "recipe")        # threads that already carry an AI review

# The model's answer per thread (same keys as review.py's contract). duplicate_of is a thread key.
REVIEWS: dict[str, dict[str, Any]] = {
    "shop": dict(
        summary=("The shop freezes after two purchases in a row; the game has to be force-closed.",
                 "连续购买两次后商店界面卡死，只能强行关闭游戏。"),
        cluster="shop-freeze", severity="high", suggest_status="investigating", confidence=0.74, duplicate_of="",
        likely_cause=("Guess: the purchase confirmation waits for a state that the second purchase never sets.",
                      "推测：第二次购买没有触发确认窗口等待的状态，界面一直在等。"),
        where_to_look=["ui/ShopView", "shop/PurchaseHandler"],
        need_info=("", ""),
        reply_draft=("Thanks for the clear report! We can reproduce a freeze in the shop and are looking into it. "
                     "We will post here when there is news.",
                     "谢谢你写得这么清楚！商店卡死的问题我们已经在排查，有进展会在这里告诉你。"),
        tags_add=["investigating"]),
    "shop_dup": dict(
        summary=("The shop shows a spinner forever; a restart fixes it.", "商店一直转圈，重启后恢复。"),
        cluster="shop-freeze", severity="medium", suggest_status="duplicate", confidence=0.62, duplicate_of="shop",
        likely_cause=("Same symptom as the freeze after two purchases (a guess from the description).",
                      "症状和「连续买两次后卡死」一样（根据描述推测）。"),
        where_to_look=["ui/ShopView"], need_info=("", ""),
        reply_draft=("Thanks! This looks like the same problem as another report, so we will follow it there. "
                     "Please tell us if it behaves differently for you.",
                     "谢谢！这看起来和另一条反馈是同一个问题，我们会在那边跟进。如果你这边表现不一样，请告诉我们。"),
        tags_add=["duplicate"]),
    "crash": dict(
        summary=("The game crashes now and then, but the report has no device, build or steps.",
                 "游戏偶尔崩溃，但没有写设备、版本和操作步骤。"),
        cluster="crash-no-details", severity="medium", suggest_status="need-info", confidence=0.25, duplicate_of="",
        likely_cause=("Cannot tell from this report.", "光看这条反馈判断不了。"), where_to_look=[],
        need_info=("Device model, game build, what you did right before, and the log file if you can find it.",
                   "设备型号、游戏版本、崩溃前的操作，能找到日志文件的话也请附上。"),
        reply_draft=("Thanks for telling us! To find the cause we need your device model, the game build and what "
                     "you did right before the crash. A log file helps a lot.",
                     "谢谢你告诉我们！要找原因，需要你的设备型号、游戏版本，以及崩溃前做了什么。有日志文件的话帮助很大。"),
        tags_add=["need-info"]),
    "recipe": dict(
        summary=("After the update the recipe book is empty, mostly on tablets; coins are unaffected.",
                 "更新后食谱册是空的，主要在平板上；金币不受影响。"),
        cluster="recipe-book-blank", severity="high", suggest_status="investigating", confidence=0.7, duplicate_of="",
        likely_cause=("Guess: the save migration skipped the recipe list for older saves.",
                      "推测：存档迁移时漏掉了旧存档里的食谱列表。"),
        where_to_look=["save/Migration", "ui/RecipeBook"], need_info=("", ""),
        reply_draft=("Thanks, and sorry for the scare. Your save is not lost; we are checking why the recipe list is "
                     "empty after the update.",
                     "谢谢反馈，让你虚惊一场了。存档没有丢，我们正在查更新后食谱列表为什么是空的。"),
        tags_add=["investigating"]),
    "dark": dict(
        summary=("A feature request: a dark theme for night play.", "功能建议：希望有夜间模式。"),
        cluster="dark-mode-request", severity="low", suggest_status="not-bug", confidence=0.8, duplicate_of="",
        likely_cause=("Not a bug; the game has no dark theme.", "不是缺陷，游戏本来就没有夜间主题。"),
        where_to_look=["ui/Theme"], need_info=("", ""),
        reply_draft=("Thanks for the idea! A dark mode is on the wishlist, but there is no date yet.",
                     "谢谢你的建议！夜间模式在愿望清单上，不过暂时没有时间表。"),
        tags_add=["not-bug"]),
    "flicker": dict(
        summary=("Customer sprites flicker on a tablet while they walk to the counter.",
                 "平板上顾客走向柜台时角色闪烁。"),
        cluster="customer-flicker", severity="low", suggest_status="investigating", confidence=0.55, duplicate_of="",
        likely_cause=("Guess: the sprite atlas is rebuilt every frame on some tablet GPUs.",
                      "推测：部分平板 GPU 上每一帧都在重建角色图集。"),
        where_to_look=["render/SpriteAtlas"],
        need_info=("The tablet model and Android version.", "平板型号和安卓版本。"),
        reply_draft=("Thanks! Could you tell us the tablet model and Android version? That helps us reproduce it.",
                     "谢谢！能告诉我们平板型号和安卓版本吗？这样我们更容易复现。"),
        tags_add=["investigating"]),
}

# proposals in the queue
QUEUE_FAILED: dict[str, Any] = dict(
    thread="baguette", text=("Hi! Baguettes bake for 3 minutes on purpose. The Convection upgrade in the shop halves "
                             "that. Thanks for asking!",
                             "你好！法棍就是要烤 3 分钟，商店里的对流烤箱升级可以减半。谢谢你的提问！"),
    error="Missing Access", why=("Answer for a not-a-bug report", "给「不是缺陷」反馈的回复"))
QUEUE_APPROVED: dict[str, Any] = dict(
    thread="oven", text=("Good news: the oven timer fix ships in the next build. Thanks again for the report!",
                         "好消息：烤箱计时器的修复会在下个版本发布。再次感谢你的反馈！"),
    why=("Closing the loop on a fixed bug", "告诉玩家问题已修复"))
QUEUE_PENDING_REPLY: dict[str, Any] = dict(thread="shop", why=("AI review draft, edited by a human", "AI 审核草稿，人工改过"))
QUEUE_PENDING_TAGS: dict[str, Any] = dict(thread="shop_dup", add=["duplicate"], why=("Same symptom as another report",
                                                                                     "和另一条反馈症状相同"))

# chat: (channel key or "dm", username, text, is_bot)
CHAT: list[tuple[str, str, Pair, bool]] = [
    ("news", "dana_o", ("Build 2026.10.18 is out. Notes are in the pinned post; bugs go to the bug forum, please.",
                        "2026.10.18 版本已发布，更新说明见置顶帖；遇到问题请发到 bug 论坛。"), False),
    ("general", "zoe_c", ("Anyone else stuck on the day-3 lunch rush? The ovens never keep up.",
                          "有人卡在第 3 天的午餐高峰吗？烤箱怎么都不够用。"), False),
    ("general", "ben_t", ("Pre-heat the second oven before the crowd comes, it helps a lot.",
                          "客人来之前先把第二台烤箱预热，帮助很大。"), False),
    ("general", "mira_k", ("Is a dark mode planned? Night shifts are rough :)",
                           "夜间模式会做吗？晚上玩太刺眼了 :)"), False),
    ("test", "dana_o", ("Testing the bot in this channel.", "在这个频道测试机器人。"), False),
    ("test", "", ("Hi Dana! I am a demo bot, nothing here is real.", "你好丹娜！我是演示机器人，这里的一切都是假的。"), True),
    ("dm", "aiko_s", ("Hi! I just supported the game on Afdian. How do I get the Supporter role?",
                      "你好！我刚在爱发电支持了游戏，怎么拿到支持者身份组？"), False),
    ("dm", "", ("Thank you for the support! Send me your order number here and I will check it.",
                "谢谢你的支持！把订单号发给我，我来核对。"), True),
]

# Afdian: plans, orders (buyer nickname, plan index, days ago, remark, paid, claimed by)
PLANS: list[tuple[str, Pair]] = [("demo-plan-small", ("Coffee tier", "一杯咖啡")),
                                 ("demo-plan-full", ("Full-version tier", "完整版支持"))]
ORDERS: list[tuple[Pair, int, float, Pair, bool, str]] = [
    (("CocoaFan", "可可酱"), 0, 0.2, ("", ""), True, ""),
    (("midnight_baker", "夜班面包师"), 1, 0.6, ("Love the game!", "很喜欢这个游戏！"), True, ""),
    (("tartlover", "柠檬挞"), 0, 1.1, ("My Discord is mira_k, thanks!", "我的 Discord 是 mira_k，谢谢！"), True, ""),
    (("crumb42", "面包屑"), 0, 1.5, ("", ""), True, ""),
    (("sugarpine", "糖松"), 1, 2.0, ("Discord: omar_h", "Discord 是 omar_h"), True, ""),
    (("Aiko S.", "爱子"), 1, 2.4, ("", ""), True, "aiko_s"),
    (("whisk", "打蛋器"), 0, 3.0, ("", ""), True, ""),
    (("rye_and_shine", "黑麦"), 0, 3.6, ("Keep it up", "加油"), True, ""),
    (("bunbun", "小圆包"), 1, 4.2, ("", ""), False, ""),
    (("flourpower", "面粉力量"), 0, 5.0, ("", ""), True, ""),
]
FAKE_AFDIAN_USER = "demo-afdian-user"
FAKE_AFDIAN_TOKEN = "demo-afdian-token-0000"
FAKE_MODEL_KEY = "sk-demo-not-a-real-key-0000"
FAKE_BOT_TOKEN = "demo-bot-token-not-real"

# canned drafts for the chat page, matched by words in the new message: (words, reply)
DRAFTS: list[tuple[tuple[str, ...], Pair]] = [
    (("dark", "夜间", "night"), ("A dark mode is on the wishlist, but there is no date yet. You can add the idea in the "
                                 "bug forum so it gets counted.",
                                 "夜间模式在愿望清单上，不过暂时没有时间表。你可以在 bug 论坛里留一条建议，这样会被统计到。")),
    (("supporter", "afdian", "爱发电", "支持者", "order", "订单"),
     ("Thanks for the support! Send me the order number in a DM and I will check it.",
      "谢谢支持！把订单号私信发给我，我来核对。")),
    (("crash", "bug", "freeze", "崩溃", "卡"), ("Sorry about that! Please post the details in the bug forum: your device, "
                                              "the build number and what you did right before.",
                                              "抱歉给你添麻烦了！请到 bug 论坛发一帖，写上设备、版本号和出问题前的操作。")),
    (("oven", "rush", "烤箱", "高峰"), ("Pre-heating the second oven before the rush usually helps. Good luck!",
                                       "高峰来之前先预热第二台烤箱通常有帮助。加油！")),
]
DRAFT_DEFAULT: Pair = ("Thanks for asking! I am not sure about that one, but a moderator will see this soon.",
                       "谢谢你的提问！这个我不太确定，版主很快会看到的。")
