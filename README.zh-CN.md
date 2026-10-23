# Hearback（回音）

**每个报 bug 的玩家，都能收到回音。**

Hearback 是给 **Discord 论坛频道**用的「AI 分诊 + 人工把关」bug 工作台：多个 AI 编程助手（Claude Code、
OpenAI Codex、Agents SDK 应用……）通过共享台账和 MCP 服务一起分诊报告；所有公开发言都要人批准；
修复发布后，每个报告人都会在自己的帖子里收到「哪个版本修好了、怎么更新」的回复。

[English](README.md) · [设计文档](docs/DESIGN.md) · [同类项目对比](docs/LANDSCAPE.md) · [Agent 使用手册](integrations/AGENT_PLAYBOOK.md)

## 解决什么问题

小团队（独立游戏、同人 / 复刻游戏、开源工具）常用 Discord 论坛收 bug。收集不难，难的是之后的闭环：

- 同一个根因被六个人用六种说法报了六遍；
- AI 每次都把整个论坛重读一遍，又慢又费 token，还会把处理过的帖子重新分诊；
- 回复是公开的，AI 不能自己乱发；但一条条人工批准又太累；
- 修好发包以后，没人回头告诉那 22 个报告人 —— 玩家觉得石沉大海，就不再报 bug 了。

这个项目是从一个真实游戏社区的 bug 专区流程里抽出来的：29 个帖子、22 个修复，发包当天所有相关报告人都收到了通知。

## 和现有工具的区别

| | 一般的 Discord 机器人 / MCP 服务 | Hearback |
|---|---|---|
| AI 的记忆 | 每次重读论坛 | **台账**：没变化的帖子不用逐帖重新拉取，AI 也不花 token；AI 只看几行增量 |
| 安全 | AI 工具可以直接发帖 | **AI 的工具里没有发送**，只能提议；人批准（命令行、桌面窗口、或私密频道里点 ✅）；发送幂等、发完回读核对。能直接跑命令的 AI（Claude Code、Codex）要靠「不给它机器人令牌」来隔离，见英文 README 的 Safety model |
| 修好以后 | 关 issue，报告人被遗忘 | **发版闭环**：`hearback release 1.2.0` 给每个相关报告人起草一条、按他用的版本写的回复 |
| 多个 AI | 互相打架 | **认领租约 + 分诊版本号 + 审计日志**：Claude 分诊的同时 Codex 修 bug |
| 报告质量 | 玩家写什么是什么 | **报告人引导包**：自动生成置顶「怎么报 bug」帖，带标注截图 |
| 人的温度 | 机器人发了就定了 | **桌面工作台**：事后改 / 删机器人的消息 |

## 30 秒体验（不需要 Discord）

```bash
git clone https://github.com/Cynthia-wlan9509/hearback
cd hearback
python -m pip install -e .
hearback demo --lang zh
```

演示用内存里的假 Discord，装了一个虚构游戏《Starlight Bakery》的 12 个 bug 帖，把整个流程跑一遍：
同步 → 两个 AI 分诊（一个撞上别人的认领、一个版本号过期被拒）→ 14 条提议 → 私密频道点反应批准（非管理员的 ✅ 被忽略）
→ 发送 → 核对 → 增量同步 → 体验版发版通知、完整版发版通知 → 总表。大约 1 秒跑完。

## 在自己的服务器上用

需要 Python 3.11+。核心**零依赖**；MCP 服务需要装 `mcp` 扩展。

1. 在 <https://discord.com/developers/applications> 建 bot：Bot → Reset Token；打开 **Message Content Intent**；
   OAuth2 → URL Generator 勾 `bot` 和 View Channels、Read Message History、Send Messages、Send Messages in Threads、
   Create Public Threads、Manage Threads、Attach Files、Add Reactions，用生成的链接把 bot 拉进服务器。
2. `hearback init` 生成带注释的 `hearback.toml`，填 `forum_id`、你发的版本（比如体验版 / 完整版）和状态对应的论坛标签名。
3. 令牌放环境变量 `DISCORD_BOT_TOKEN` 或 `.hearback/token.txt`（已在 .gitignore 里）。Hearback 从不打印它。
4. 日常流程见 [README.md](README.md#use-it-on-your-server) 的命令示例；`hearback editor` 打开桌面工作台。

## 接入 AI

`pip install -e ".[mcp]"` 后，让 MCP 客户端运行 `hearback mcp`。每个 AI 用自己的名字（`HEARBACK_ACTOR`），
审计日志和认领才分得清是谁。Claude Code、Codex、OpenAI Agents SDK 的配置示例见 [README.md](README.md#plug-in-your-agents)
和 `integrations/`。MCP 工具里**故意没有**发送、批准、编辑、删除；论坛内容一律包在「不可信数据」的标记里返回，
降低有人在帖子里写指令操纵 AI 的风险（降低不等于杜绝，所以 AI 写的任何东西都要人批准才会公开）。
机器人发出的每条消息都关掉了 @ 提醒，帖子标题里塞的 @everyone 也会被处理掉。

## 路线图

- 看图分诊 agent：理解截图和日志、把重复报告归类（Agents SDK）
- 用报告人的语言回复（英文 / 马来文 / 中文社区）
- Codex 当修复 agent：认领一个 bug 簇、在游戏仓库里修、把修复说明和版本号写回台账 → 发版通知
- 导出到 GitHub Issues / Linear

## 许可

MIT © 2026 Cynthia-wlan9509 and contributors
