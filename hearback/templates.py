"""Built-in reply / release / guide templates (English and Chinese), overridable per workspace.

Templates are `string.Template` texts ($name placeholders). To override one, drop a file named
`<name>.txt` (or `<name>.<lang>.txt`) into `.hearback/templates/`.
"""
from __future__ import annotations

import re
from pathlib import Path
from string import Template

from hearback.util import HearbackError

EN: dict[str, str] = {
    "ack": (
        "Hi $author, thanks for the report. We are looking into \"$title\" and will update this thread "
        "as soon as we know more."
    ),
    "need_info": (
        "Hi $author, thanks for reporting this. We could not pin it down yet. Could you add:\n"
        "1. The build you play (tag the post with your variant) and your device or OS\n"
        "2. The exact steps that lead to the problem\n"
        "3. A screenshot, screen recording or log file if you can attach one\n"
        "Once we have that we will take another look."
    ),
    "duplicate": (
        "Hi $author, thanks for the report. This is the same problem as $original, so we track it "
        "there. You will hear back in this thread too when it ships."
    ),
    "not_bug": (
        "Hi $author, thanks for asking. This is how the game is meant to work: $explanation"
    ),
    "wontfix": (
        "Hi $author, thanks for the suggestion. We looked at it and decided not to do it for now: "
        "$explanation"
    ),
    "release_notify": (
        "Hi $author, good news: the problem you reported (\"$title\") is fixed in version $version.\n"
        "$fix_line"
        "$variant_blocks\n"
        "Thank you for reporting it. Reports like yours are how these get found."
    ),
    "variant_block": (
        "- $variant_name: get it from $download. Install it over your old version; your save stays."
    ),
    "release_announce": (
        "**Version $version is out**\n\n"
        "$groups\n\n"
        "Thanks to everyone who reported these. Reporters have been notified in their threads."
    ),
    "release_announce_group": "**$variant_name** ($download)\n$items",
    "release_announce_item": "- $cluster: $summary ($count report(s))",
    "guide_title": "How to report a bug (read this first)",
    "guide_body": (
        "**Before you post**\n"
        "- Search the forum: your problem may already be reported. Add your details there.\n"
        "- One problem per post.\n\n"
        "**Tags**\n"
        "Pick the tag for the build you play: $variant_tags. Add a topic tag if one fits.\n\n"
        "**Copy this form into your post**\n"
        "```\n"
        "What happened:\n"
        "What you expected:\n"
        "Steps to reproduce:\n"
        "1.\n"
        "2.\n"
        "Build / version:\n"
        "Device / OS:\n"
        "```\n\n"
        "**Attaching evidence**\n"
        "- Phone: tap the + in the message box, choose a screenshot, recording or log file.\n"
        "- Desktop: drag the file into the message box, or paste a screenshot with Ctrl+V.\n"
        "- Screen recordings under 10 MB are best; for longer ones share a link.\n\n"
        "**What happens next**\n"
        "A maintainer triages your post, merges duplicates and tags it. When the fix ships you get a "
        "reply in your own thread telling you which build has it and how to update."
    ),
    "supporter_ok": "Thank you for your support! Your order is confirmed and you now have the supporter role.",
    "supporter_ok_again": "Thanks again! This order is confirmed and you already have the supporter role.",
    "supporter_claimed_by_other": (
        "That order number has already been used by someone else, so it cannot be used again. "
        "If you think this is a mistake, please contact the server owner."
    ),
    "supporter_need_verify_first": "Please finish the server's verification first, then send me the order number again.",
    "supporter_not_found": (
        "I could not find a paid order with that number yet. Please check the number. "
        "A new order can take a few minutes to show up, so you can also try again shortly."
    ),
    "supporter_below_min": (
        "Thank you! This order does not reach the amount needed for the supporter role, so I cannot give it for "
        "this one. The server owner has been told in case something is off."
    ),
    "supporter_limit": "You have checked quite a few numbers today. Please try again tomorrow, or contact the server owner.",
    "supporter_retry_later": (
        "Afdian is not answering right now. I saved your order number and will try again automatically. "
        "You will get a message here when it is done."
    ),
    "supporter_gave_up": (
        "I still could not reach Afdian for your order, so I stopped trying. "
        "The server owner has been told and will check it by hand."
    ),
    "supporter_off": "Supporter checks are not set up yet. The server owner has been told.",
    "supporter_not_member": (
        "I cannot find you in the server, so I cannot give you a role. "
        "Please join the server first, then send me the number again."
    ),
    "supporter_grant_failed": (
        "Your order is confirmed, but I could not hand out the role just now. The server owner has been told. "
        "Send the number again later and it will be given."
    ),
    "supporter_note_claimed": "[SWLink] $who linked order $order and received the supporter role.",
    "supporter_note_claimed_had": "[SWLink] $who linked order $order. They already had the supporter role.",
    "supporter_note_other": "[SWLink] $who sent order $order, but it is already linked to $first.",
    "supporter_note_below_min": "[SWLink] $who sent order $order, which is below your minimum amount. No role was given.",
    "supporter_note_retry": (
        "[SWLink] Afdian could not be checked ($error). $who's order $order will be retried every 10 minutes."
    ),
    "supporter_note_gave_up": "[SWLink] Afdian could not be reached for 3 days. Please check $who's order $order by hand.",
    "supporter_note_off": (
        "[SWLink] $who sent order $order, but supporter checks are not set up yet (see the Supporters page)."
    ),
    "supporter_note_hierarchy": (
        "[SWLink] Could not give the role $role to $who. In Server Settings > Roles, drag the bot's role above $role."
    ),
    "supporter_note_no_permission": (
        "[SWLink] Could not give the role $role to $who: the bot has no Manage Roles permission. "
        "Turn it on for the bot's role in Server Settings > Roles."
    ),
    "supporter_note_grant_failed": "[SWLink] Could not give the role $role to $who: $error",
    "supporter_note_left": "[SWLink] $who left the server before order $order could be checked. That check was dropped.",
    "supporter_claimed_elsewhere": (
        "This order number was already used in another server. One order works in one server only. "
        "If this looks wrong, please contact the server owner."),
    "supporter_note_elsewhere": "[SWLink] $who sent order $order, but it is already used in $server.",
    "codes_ok": ("Here is the code for this device ($device): it is the next message, on its own.\n"
                 "On a phone, long-press it and copy, then paste it into the app. It works on this device only; "
                 "please keep it to yourself."),
    "codes_again": "This device ($device) already has a code. It is the next message again: long-press it and copy.",
    "codes_need_role": "Please finish the server's verification first, then send me your device code again.",
    "codes_not_member": "I cannot find you in the server. Please join and finish verification, then send me your device code.",
    "codes_taken": "This device code already has a code. If it is your device, please contact the server owner.",
    "codes_limit": ("Each member gets a code for one device automatically. For another device, please contact "
                    "the server owner; they have been told."),
    "codes_failed": "The code could not be made just now. The server owner has been told and will get back to you.",
    "codes_note_issued": "[SWLink] Sent $who a code for device $device.",
    "codes_note_taken": "[SWLink] $who sent device $device, which already has a code for someone else. Nothing was sent.",
    "codes_note_limit": "[SWLink] $who already has a code and sent another device $device (new phone?). Nothing was sent.",
    "codes_note_failed": "[SWLink] Making a code for $who (device $device) failed: $error",
}

ZH: dict[str, str] = {
    "ack": "你好 $author，感谢反馈。我们正在排查「$title」，有进展会第一时间在这个帖子里回复你。",
    "need_info": (
        "你好 $author，感谢反馈。目前还没能定位到问题，麻烦补充：\n"
        "1. 你玩的版本（请用对应版本的标签）和设备/系统\n"
        "2. 触发问题的具体步骤\n"
        "3. 如果可以，附上截图、录屏或日志文件\n"
        "信息齐了我们会马上再看。"
    ),
    "duplicate": "你好 $author，感谢反馈。这个问题和 $original 是同一个，我们在那边统一跟进。修复发布时这个帖子里也会通知你。",
    "not_bug": "你好 $author，感谢提问。这是游戏本来的设计：$explanation",
    "wontfix": "你好 $author，感谢建议。我们评估后暂时不会做这个：$explanation",
    "release_notify": (
        "你好 $author，好消息：你反馈的问题（「$title」）已在 $version 版本修复。\n"
        "$fix_line"
        "$variant_blocks\n"
        "谢谢你的反馈，正是有你这样的报告我们才能找到这些问题。"
    ),
    "variant_block": "- $variant_name：请到 $download 获取。直接覆盖安装即可，存档会保留。",
    "release_announce": (
        "**$version 版本已发布**\n\n"
        "$groups\n\n"
        "感谢所有反馈的朋友，反馈者已在各自的帖子里收到通知。"
    ),
    "release_announce_group": "**$variant_name**（$download）\n$items",
    "release_announce_item": "- $cluster：$summary（$count 个反馈）",
    "guide_title": "如何反馈问题（发帖前请先看）",
    "guide_body": (
        "**发帖前**\n"
        "- 先搜一下论坛，你的问题可能已经有人反馈了，补充你的情况即可。\n"
        "- 一个帖子只写一个问题。\n\n"
        "**标签**\n"
        "请选择你所玩版本对应的标签：$variant_tags。如有合适的类别标签也一并选上。\n\n"
        "**把下面的表格复制到帖子里**\n"
        "```\n"
        "发生了什么：\n"
        "你期望的是：\n"
        "复现步骤：\n"
        "1.\n"
        "2.\n"
        "版本：\n"
        "设备/系统：\n"
        "```\n\n"
        "**怎么附上证据**\n"
        "- 手机：点消息框左边的 +，选择截图、录屏或日志文件。\n"
        "- 电脑：把文件拖进消息框，或用 Ctrl+V 粘贴截图。\n"
        "- 录屏最好小于 10 MB，更长的请发链接。\n\n"
        "**之后会怎样**\n"
        "维护者会分诊你的帖子、合并重复问题并打标签。修复发布后，你会在自己的帖子里收到回复，"
        "告诉你哪个版本已修复、怎么更新。"
    ),
    "supporter_ok": "谢谢你的支持！订单已确认，支持者身份组已经加上了。",
    "supporter_ok_again": "再次感谢！这笔订单已确认，你已经有支持者身份组了。",
    "supporter_claimed_by_other": "这个订单号已经被别人用过了，不能再用。如果你觉得不对，请联系群主。",
    "supporter_need_verify_first": "请先完成群里的验证，再把订单号发给我。",
    "supporter_not_found": (
        "暂时没查到这个订单号对应的已付款订单。请检查一下号码有没有发错；"
        "刚付款的订单可能要几分钟才查得到，可以过一会儿再发一次。"
    ),
    "supporter_below_min": "谢谢你！这笔订单没有达到领取支持者身份组的金额，这次没法给。已经告诉群主了，如果有问题会联系你。",
    "supporter_limit": "你今天查的次数有点多，请明天再试，或者直接联系群主。",
    "supporter_retry_later": "爱发电现在连不上。我记下了你的订单号，会自动重试，有结果会在这里告诉你。",
    "supporter_gave_up": "我一直连不上爱发电，已经不再自动重试了。已经告诉群主，会有人手动帮你核对。",
    "supporter_off": "支持者核对还没设置好，已经告诉群主了。",
    "supporter_not_member": "我在群里找不到你，没法给身份组。请先加入群，再把订单号发给我。",
    "supporter_grant_failed": "订单已确认，但身份组这会儿没能加上，已经告诉群主了。稍后把订单号再发一次就会补上。",
    "supporter_note_claimed": "[SWLink] $who 认领了订单 $order，已给支持者身份组。",
    "supporter_note_claimed_had": "[SWLink] $who 认领了订单 $order。对方原来就有支持者身份组。",
    "supporter_note_other": "[SWLink] $who 发来订单 $order，但它已经被 $first 用过了。",
    "supporter_note_below_min": "[SWLink] $who 发来订单 $order，低于你设的最低金额，没有给身份组。",
    "supporter_note_retry": "[SWLink] 爱发电查不了（$error）。$who 的订单 $order 会每 10 分钟自动重查。",
    "supporter_note_gave_up": "[SWLink] 爱发电连续 3 天都没查成。请人工核对 $who 的订单 $order。",
    "supporter_note_off": "[SWLink] $who 发来订单 $order，但支持者核对还没设置好（见「支持者」页面）。",
    "supporter_note_hierarchy": "[SWLink] 没能把身份组 $role 给 $who。到服务器设置 > 身份组，把机器人的身份组拖到 $role 上面。",
    "supporter_note_no_permission": (
        "[SWLink] 没能把身份组 $role 给 $who：机器人没有「管理身份组」权限。"
        "到服务器设置 > 身份组，给机器人的身份组打开它。"
    ),
    "supporter_note_grant_failed": "[SWLink] 没能把身份组 $role 给 $who：$error",
    "supporter_note_left": "[SWLink] $who 在订单 $order 查完之前离开了服务器，这次核对已取消。",
    "supporter_claimed_elsewhere": "这个订单号已经在另一个服务器用过了，一个订单只能用在一个服务器。有问题请联系群主。",
    "supporter_note_elsewhere": "[SWLink] $who 发来订单 $order，但它已经在 $server 用过了。",
    "codes_ok": ("你这台手机（本机码 $device）的授权码在下一条消息里，单独一条。\n"
                 "长按那条消息 → 复制，回到游戏点「粘贴授权码」→「验证并进入」。这个码只能这台手机用，请不要外传。"),
    "codes_again": "这台手机（本机码 $device）之前已经发过授权码，下一条消息再发一次：长按它 → 复制，回游戏点「粘贴授权码」。",
    "codes_need_role": "请先完成验证，再把本机码发给我。",
    "codes_not_member": "我在群里找不到你。请先加入服务器并完成验证，再把本机码发给我。",
    "codes_taken": "这个本机码已经领过授权码了。如果是你的手机，请联系群主。",
    "codes_limit": "每人只能自动领一台手机的授权码。换手机或多一台手机，请联系群主，已经告诉群主了。",
    "codes_failed": "授权码这会儿没能生成，已经告诉群主了，稍后会有人联系你。",
    "codes_note_issued": "[SWLink] 给 $who 发了授权码（本机码 $device）。",
    "codes_note_taken": "[SWLink] $who 发来的本机码 $device 已经发给别人了，这次没有发。",
    "codes_note_limit": "[SWLink] $who 已经领过码，又发来新的本机码 $device（换手机？），没有自动发。",
    "codes_note_failed": "[SWLink] 给 $who 生成授权码失败（本机码 $device）：$error",
}

BUILTIN = {"en": EN, "zh": ZH}


def names() -> list[str]:
    return sorted(EN)


def load_text(name: str, lang: str, overrides_dir: Path | None = None) -> str:
    """Template text: workspace override first, then the built-in for `lang`, then English."""
    if overrides_dir:
        for fname in ("%s.%s.txt" % (name, lang), "%s.txt" % name):
            f = Path(overrides_dir) / fname
            if f.is_file():
                return f.read_text(encoding="utf-8-sig").rstrip("\r\n")
    text = BUILTIN.get(lang, EN).get(name) or EN.get(name)
    if text is None:
        raise HearbackError("unknown template %r; available: %s" % (name, ", ".join(names())))
    return text


ZWSP = chr(0x200B)   # zero-width space
_MENTION_OPEN = re.compile(r"<(?=[@#])")                          # <@id>  <@!id>  <@&id>  <#id>
_BROADCAST = re.compile(r"@(?=(?:everyone|here)\b)", re.I)       # @everyone  @here
_LINK_MARKUP = re.compile(r"\]\(")                                # [text](url) masked links


class Trusted(str):
    """Text built by Hearback itself (config values, multi-line blocks): inserted as it is."""


def defang_mentions(text: str) -> str:
    """Put a zero-width space inside anything Discord would turn into a ping or a masked link."""
    text = _MENTION_OPEN.sub("<" + ZWSP, text)
    text = _BROADCAST.sub("@" + ZWSP, text)
    return _LINK_MARKUP.sub("]" + ZWSP + "(", text)


def sanitize(value: object, limit: int = 80) -> str:
    """Make forum-derived text safe to put into a message: no pings, no code spans, one line, at most `limit` chars."""
    s = defang_mentions(" ".join(str(value if value is not None else "").replace("`", "").split()))
    if len(s) > limit:                       # cap last, so the result is never longer than `limit`
        s = s[: max(limit - 1, 0)].rstrip() + "…"
    return s


def safe(value: object, limit: int = 300) -> Trusted:
    """Sanitised like `sanitize`, with a longer cap, for free-text values that should not be cut at 80."""
    return Trusted(sanitize(value, limit))


def trusted(value: object) -> Trusted:
    return Trusted("" if value is None else str(value))


def render(name: str, lang: str = "en", overrides_dir: Path | None = None, **values: object) -> str:
    """Fill a template. Unknown placeholders are left as written (safe_substitute).

    Every value is sanitised (see `sanitize`) unless the caller marks it `trusted()` / `safe()`: text that
    came from the forum ($author, $title, ...) can then never smuggle in a ping, a code fence or a line break.
    """
    return Template(load_text(name, lang, overrides_dir)).safe_substitute(
        {k: ("" if v is None else v if isinstance(v, Trusted) else sanitize(v)) for k, v in values.items()})
