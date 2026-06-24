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
| `TG_DATABASE_PATH` | 否 | SQLite 路径，默认 `data/tg_save_helper.sqlite3` |
| `TG_SAVED_MEDIA_PATH` | 否 | 收藏媒体下载目录，默认 `data/saved_media` |
| `LOG_LEVEL` | 否 | 日志级别，默认 `INFO` |

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
| `/last <source> <count>` | 原样转发最近帖子，最多 200 个逻辑帖子 |
| `/between <source> <start_id> <end_id>` | 转发消息 ID 范围，最多 500 个 ID |
| `/link <message_link>` | 转发一条公开或 `t.me/c/...` 消息链接 |
| `/watch <source>` | 监听并原样转发新消息 |
| `/unwatch <source>` | 取消普通监听 |
| `/watchcomments <source>` | 监听频道主帖及其关联评论区 |
| `/unwatchcomments <source>` | 取消频道及评论区监听 |
| `/lastcomments <source> <count>` | 转发最近主帖及其全部已有评论，最多 10 个主帖 |
| `/listwatch` | 列出持久化监听源 |
| `/status` | 显示登录、监听、转发和错误状态 |
| `/syncsaved <count\|all>` | 按原频道在 Telegram 内复制收藏媒体到同名私有频道，不下载文件；`all` 处理全部收藏 |
| `/syncsaved-download <count\|all>` | 下载收藏媒体后重新上传；`all` 处理全部收藏 |

示例：

```text
/last @example_channel 20
/between @example_channel 1000 1100
/link https://t.me/example_channel/123
/link https://t.me/c/123456789/123
/watch @example_channel
/watchcomments @example_channel
/lastcomments @example_channel 3
/syncsaved 500
/syncsaved all
/syncsaved-download 100
```

## 收藏媒体迁移

`/syncsaved <count>` 会扫描最近的指定数量收藏消息，`/syncsaved all` 会遍历全部收藏消息。数字范围如果刚好截断媒体相册，程序会自动扩展读取到该相册的边界，避免只迁移半个相册。程序只处理“从频道转发而来且带可复制媒体”的消息，优先复用账号已有的同名自建广播频道，否则创建一个同名私有频道，然后直接复用 Telegram 已有的媒体引用发送，不把文件下载到运行服务器。caption 和媒体相册会尽量保持。

只有 `/syncsaved-download <count>` 会先把媒体按原频道保存到 `TG_SAVED_MEDIA_PATH`，再从本地重新上传，因此会占用磁盘并产生媒体下载、上传流量。

来源频道与目标频道的映射、已成功同步的收藏消息 ID 都保存在 SQLite 中，两个命令共用去重记录，重复执行或切换模式都不会再次上传成功项。纯文字、网页预览、用户或群组转发、隐藏来源、受保护内容以及无法识别原频道名称的消息会跳过。Telegram 对账号可创建频道数量和频率有限制；来源很多时建议分批执行。

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
- SQLite：保存 `watched_sources`、`forwarding_logs` 和 `app_state`；
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
