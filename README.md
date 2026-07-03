# Telegram 收藏助手

[English](README_EN.md) | 简体中文

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Telethon](https://img.shields.io/badge/Telethon-1.40-2AABEE?logo=telegram&logoColor=white)](https://github.com/LonamiWebs/Telethon)
[![GitHub stars](https://img.shields.io/github/stars/Akemi-Homura/tg-save-helper?style=flat)](https://github.com/Akemi-Homura/tg-save-helper/stargazers)

一个运行在 Linux 服务器上的 Telegram 收藏助手。通过登录账号自己的 Saved Messages（收藏夹）发送指令，即可批量原样转发有权访问的频道、群组和聊天消息。

项目基于 Telethon，不需要网页面板，不开放公网端口，也不会下载后重新上传受保护内容。

## 功能

- 通过 Saved Messages 控制，仅响应登录账号本人发出的指令
- 转发最近消息、指定 ID 范围或单条消息链接
- 长期监听频道、群组和聊天的新消息
- 自动监听频道关联评论区，转发主帖及评论
- 识别白名单资源机器人链接，自动触发、翻页并转发返回媒体
- 保持 Telegram 媒体相册组合以及原 caption、文字、格式和链接
- SQLite 持久化监听源、转发日志和运行状态
- 自动处理 FloodWait，并对批量任务限速
- 将收藏夹里的频道媒体按原频道归档到本地，并重新上传到同名私有频道
- 跳过受保护、无权限、已删除或无效消息，不尝试绕过 Telegram 限制
- 提供一键安装脚本和 systemd 服务

## 环境要求

- Ubuntu 22.04/24.04 或其他支持 Python 3.10+ 的 Linux
- 能够连接 Telegram 的网络环境
- 从 [my.telegram.org](https://my.telegram.org/apps) 获取的个人 `api_id` 和 `api_hash`
- 一个正常使用的 Telegram 用户账号；这不是 BotFather Bot

## 快速开始

```bash
git clone https://github.com/Akemi-Homura/tg-save-helper.git
cd tg-save-helper
./setup.sh
```

脚本会：

1. 创建 Python 虚拟环境并安装依赖；
2. 提示输入 `api_id` 和 `api_hash`；
3. 完成交互式 Telegram 登录并保存 session；
4. 按当前路径和用户生成、安装并启动 systemd 服务。

登录验证码通常发送到手机 Telegram 中的官方 `Telegram` 会话，而不是短信。完成后在收藏夹发送：

```text
/help
```

> [!CAUTION]
> `.env` 和 `.session` 文件都是敏感凭据。不要提交、截图、发送或上传它们。

## 配置文件

复制配置模板进行手动配置：

```bash
cp .env.example .env
chmod 600 .env
```

| 配置项 | 必填 | 说明 |
| --- | --- | --- |
| `TG_API_ID` | 是 | 从 my.telegram.org 获取的数字 ID |
| `TG_API_HASH` | 是 | 从 my.telegram.org 获取的 API Hash |
| `TG_SESSION_NAME` | 是 | Telethon session 路径，默认 `data/tg_save_helper` |
| `OWNER_ID` | 否 | 登录账号的用户 ID；设置后可在启动时校验账号 |
| `BOT_TOKEN` | 否 | BotFather 创建的控制 Bot token；设置后可在 Bot 聊天窗口输入命令 |
| `BOT_OWNER_ID` | 否 | 允许使用控制 Bot 的 Telegram 用户 ID；默认使用登录账号 ID |
| `TG_DATABASE_PATH` | 否 | SQLite 路径，默认 `data/tg_save_helper.sqlite3` |
| `TG_SAVED_MEDIA_PATH` | 否 | 收藏媒体下载目录，默认 `data/saved_media` |
| `LOG_LEVEL` | 否 | 日志级别，默认 `INFO` |
| `TG_PANEL_ENABLED` | 否 | 是否启动本机管理面板，默认关闭 |
| `TG_PANEL_HOST` | 否 | 管理面板监听地址，默认 `127.0.0.1` |
| `TG_PANEL_PORT` | 否 | 管理面板监听端口，默认 `8790` |
| `TG_PANEL_BASE_PATH` | 否 | 管理面板路径前缀，默认 `/tghelper` |
| `TG_PANEL_USERNAME` / `TG_PANEL_PASSWORD` | 否 | 管理面板 Basic Auth 账号密码；启用面板时必填 |
| `TG_PANEL_PASSWORD_FILE` | 否 | 从文件读取管理面板密码；未设置 `TG_PANEL_PASSWORD` 时使用 |

首次手动登录：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m src.main --login-only
```

直接运行：

```bash
.venv/bin/python -m src.main
```

## 指令说明

| 指令 | 说明 |
| --- | --- |
| `/help` | 显示帮助 |
| `/stop` | 停止当前正在执行的手动命令 |
| `/last <source> <count\|all\|unread> [force]` | 原样转发最近指定数量、全部或未读逻辑帖子 |
| `/unread <source> [count\|all] [force]` | 转发未读消息；省略数量等同 `all` |
| `/between <source> <start_id> <end_id> [force]` | 转发消息 ID 范围，最多 500 个 ID |
| `/link <message_link> [force]` | 转发一条公开或 `t.me/c/...` 消息链接 |
| `/watch <source> [count\|all\|unread\|from <message_link>] [force]` | 监听并原样转发新消息；可选补扫 |
| `/unwatch <source>` | 取消普通监听 |
| `/watchcomments <source> [count\|all\|unread\|from <message_link>] [force]` | 监听频道主帖及其关联评论区；可选补扫 |
| `/unwatchcomments <source>` | 取消频道及评论区监听 |
| `/watchresource <source> [count\|all\|unread\|from <message_link>] [force]` | 监听频道新帖中的资源机器人链接；可选补扫 |
| `/unwatchresource <source>` | 取消资源监听 |
| `/lastcomments <source> <count\|all\|unread> [force]` | 转发最近、全部或未读主帖及其评论 |
| `/unreadcomments <source> [count\|all] [force]` | 转发未读主帖及评论区未读评论；省略数量等同 `all` |
| `/resourcebot add\|remove\|list [username]` | 管理资源机器人白名单 |
| `/resourcelink <bot_deep_link> [force]` | 触发单个资源机器人链接；`force` 强制重拉已处理资源 |
| `/resource <source> <count\|all\|unread\|from <message_link>\|one from <message_link>> [force]` | 扫描资源机器人链接；`from` 从指定原帖含该条开始，`one` 只处理指定原帖 |
| `/mixed <source> <count\|all> [force]` | 自动按 resource / lastcomments / last 混合转发 |
| `/listwatch` | 列出持久化监听源 |
| `/status` | 显示登录、监听、转发和错误状态 |
| `/stats [day\|month\|year]` | 统计当天、当月或当年的转发和同步情况 |
| `/syncsaved <count\|all> [source\|unknown]` | 按原频道在 Telegram 内复制收藏媒体到同名私有频道，不下载文件；`all` 处理全部收藏 |
| `/syncsaved-download <count\|all> [source\|unknown]` | 下载收藏媒体后重新上传；`all` 处理全部收藏 |

启用控制 Bot 后，Telegram 的命令提示不支持横杠命令名，可在 Bot 聊天窗口使用 `/syncsaved_download <count|all>`，程序会映射为 `/syncsaved-download`。

除同步类命令外，转发类命令默认会按 `source + message_id` 跳过已经成功转发过的消息；需要重复转发时，在命令末尾追加 `force`。

示例：

```text
/last @example_channel 20
/between @example_channel 1000 1100
/link https://t.me/example_channel/123
/link https://t.me/c/123456789/123
/watch @example_channel
/watchcomments @example_channel
/watchresource @example_channel
/lastcomments @example_channel 3
/resourcebot add seliu
/resourcelink https://t.me/seliu?start=j_2bfc3620
/resourcelink https://t.me/seliu?start=j_2bfc3620 force
/resource @example_channel 10
/resource @example_channel all
/resource @example_channel all from https://t.me/example_channel/4734
/resource @example_channel one from https://t.me/example_channel/4734 force
/syncsaved 500
/syncsaved all
/syncsaved all @example_channel
/syncsaved all unknown
/syncsaved-download 100
```

## 命令行调试

可以直接在服务器命令行执行控制命令，输出会打印到终端；转发类命令仍会真实操作 Telegram：

```bash
.venv/bin/python -m src.cli /status
.venv/bin/python -m src.cli --parse-only /last -3337589510 all
.venv/bin/python -m src.cli /last -3337589510 3
```

默认会复制一份 Telegram session 到 `/tmp` 后执行，避免和 systemd 服务抢 session 锁。需要强制使用原 session 时可追加 `--live-session`。不带命令会进入交互模式：

```bash
.venv/bin/python -m src.cli
```

## 管理面板

设置 `TG_PANEL_ENABLED=1` 后，程序会在本机启动一个管理面板，默认监听：

```text
http://127.0.0.1:8790/tghelper/
```

面板提供：

- 仪表盘：活跃任务、监听数量、最近 24 小时成功/失败/跳过汇总、最近错误；
- 手动任务：查看活跃和待恢复任务，启动、暂停、停止、重新启动；
- 监听任务：查看 `/watch`、`/watchcomments`、`/watchresource`、`/watchcode`，暂停、恢复或停止；
- 控制台：输入现有 Telegram 指令并在后台执行。

面板只应监听 `127.0.0.1`，公网 HTTPS 访问建议通过 nginx 反代到明确路径，例如：

```text
https://quals.site/tghelper/
```

面板操作等同控制 Telegram 账号，必须配置 Basic Auth，不要裸露公网端口。

## 收藏媒体迁移

`/syncsaved <count>` 会扫描最近的指定数量收藏媒体（纯文字命令和回复不占用数量），`/syncsaved all` 会遍历全部收藏媒体。数字范围如果刚好截断媒体相册，程序会自动扩展读取到该相册的边界，避免只迁移半个相册。程序会按来源频道优先复用账号已有的同名自建广播频道，否则创建一个同名私有频道，然后直接复用 Telegram 已有的媒体引用发送，不把文件下载到运行服务器。caption 和媒体相册会尽量保持。无法识别原频道的收藏媒体会同步到兜底频道 `收藏媒体_未知来源`。

两个同步命令都支持追加来源过滤参数：`/syncsaved all @example_channel` 只同步指定来源，`/syncsaved all unknown` 只同步未知来源兜底媒体。

## 资源机器人链接

资源机器人功能默认只处理白名单内的 bot。固定白名单可写入 `.env`：

```env
TG_RESOURCE_BOTS=seliu
MAX_RESOURCE_BOT_PAGES=100
MAX_RESOURCE_BOT_WAIT_SECONDS=120
MAX_RESOURCE_BOT_MESSAGES=2000
```

运行期白名单用 `/resourcebot add|remove|list` 管理，无需重启。程序会从消息文本、隐藏链接和按钮中识别 `https://t.me/<bot>?start=<payload>`，触发后收集 bot 返回的媒体消息，识别“下一页/next”、页码按钮和纯文本分页导航，最后转发到我的收藏。

每个资源链接的处理现场会记录在 SQLite 中，包括来源原帖、payload、发给资源 bot 的 `/start` 消息 ID、bot 响应范围以及收集/转发数量。后续如果遇到资源 bot 已返回第 1/N 页但没有继续翻页，可以用这些记录回溯到原帖和资源链接。

## 开发文档

维护转发一致性、资源 bot 现场记录和数据库字段时，见 [DEVELOPMENT.md](DEVELOPMENT.md)。

媒体同步到各来源私有频道后，程序会再创建或复用 `收藏媒体汇总` 私有频道，并把刚同步到来源私有频道的消息转发到汇总频道。汇总频道里的消息会显示“转发自”对应的来源私有频道，方便从一个频道里按来源查看。

只有 `/syncsaved-download <count>` 会先把媒体按原频道保存到 `TG_SAVED_MEDIA_PATH`，再从本地重新上传，因此会占用磁盘并产生媒体下载、上传流量。

来源频道与目标频道的映射、已成功同步的收藏消息 ID 都保存在 SQLite 中，两个命令共用去重记录，重复执行或切换模式都不会再次上传成功项。纯文字、网页预览、受保护内容会跳过；无法识别原频道名称的媒体会进入未知来源兜底频道。Telegram 对账号可创建频道数量和频率有限制；来源很多时建议分批执行。

### source 格式

```text
@example_channel
https://t.me/example_channel
-1001234567890
```

对于 `https://t.me/c/1234567890/456`，source 通常是 `-1001234567890`。账号必须已经加入并有权访问目标聊天。

## 原样转发与评论区

`/last`、`/watch`、`/lastcomments` 和 `/watchcomments` 使用 Telegram 原生转发：

- 同一次发送的多张图片/视频保持为一个媒体相册；
- 原 caption、文字、实体格式和链接保持不变；
- 不下载媒体，也不通过重新上传复制消息；
- 遇到禁止转发或受保护内容时记录并跳过。

`/watchcomments` 会自动发现频道的关联讨论群。频道主帖只转发一次，讨论群中的自动镜像主帖会被跳过，真实评论会持续转发。为了稳定接收评论更新，登录账号应先加入关联讨论群。

`/lastcomments` 会获取所选主帖的全部已有评论。热门帖子可能需要较长时间处理，程序仍会分批限速。

## 限速与错误处理

- 每批最多处理 50 条消息；
- 批次间随机等待 2–5 秒，条目间也有短暂等待；
- 收到 `FloodWaitError` 后按 Telegram 要求自动等待；
- 单条或单个媒体组失败不会终止后续任务；
- 任务结束后在收藏夹汇总成功、失败、跳过数量及最近错误。

这只能降低风险，不能保证账号永远不会受到 Telegram 限制。请勿用于垃圾信息、批量骚扰或绕过平台规则。

## systemd 部署

`setup.sh` 会自动按当前目录生成服务。也可以修改仓库中的 `tg-save-helper.service` 示例后手动安装：

```bash
sudo cp tg-save-helper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg-save-helper
sudo systemctl status tg-save-helper
```

查看日志：

```bash
sudo journalctl -u tg-save-helper -f
```

更新代码后：

```bash
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart tg-save-helper
```

## 数据与安全

默认运行数据位于 `data/`：

- Telethon session：等同于账号登录凭据；
- SQLite：保存监听源、转发日志、资源 bot 处理现场和运行状态；
- `.env`：保存 API 凭据。

这些路径均已写入 `.gitignore`。公开仓库前仍应执行 `git status`，确认没有误提交敏感文件。服务器失陷时，请立即在 Telegram“设置 → 设备”中终止对应会话。

程序只响应 Saved Messages 中登录账号本人发出的 outgoing 指令，不响应其他聊天中的命令。不要让多个进程同时使用同一个 session 文件。

## 常见问题

### 收藏夹发送 `/help` 没有响应

```bash
sudo systemctl status tg-save-helper
sudo journalctl -u tg-save-helper -n 100 --no-pager
```

确认首次登录已完成，并且 systemd 使用的 `WorkingDirectory`、`.env` 和虚拟环境路径正确。

### 收不到登录验证码

先在终端输入带国家区号的手机号，例如 `+<国家代码><手机号>`。验证码通常发送到手机 Telegram 的官方会话中；如果启用了两步验证，还需要输入两步验证密码。

### `/watchcomments` 收不到评论

确认频道存在关联讨论群，并先使用登录账号加入该讨论群。然后重新发送 `/watchcomments <source>`。

### 私密链接无法解析

`t.me/c/...` 链接只有在当前账号已经加入对应聊天且 Telethon session 能解析该实体时才可用。先在 Telegram 客户端打开目标聊天，再重试。

## 本地检查

```bash
python3 -m py_compile src/*.py
```

## Star History

<a href="https://star-history.com/#Akemi-Homura/tg-save-helper&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=Akemi-Homura/tg-save-helper&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=Akemi-Homura/tg-save-helper&type=Date" />
    <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=Akemi-Homura/tg-save-helper&type=Date" />
  </picture>
</a>
