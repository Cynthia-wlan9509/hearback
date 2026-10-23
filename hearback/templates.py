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
