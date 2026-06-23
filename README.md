# Telegram 收藏助手

一个长期运行的 Telethon 用户客户端。它只接收登录账号在 Saved Messages 中发出的指令，批量转发该账号本来就有权访问的消息。项目不会尝试绕过禁止转发或私密内容保护。

## 环境要求

- Ubuntu 22.04/24.04，Python 3.10+
- 从 <https://my.telegram.org> 获取自己的 `api_id` 和 `api_hash`
- 服务器能够连接 Telegram（请自行确认所在地区及网络环境）

## 安装与首次登录

```bash
sudo apt update
sudo apt install -y python3 python3-venv
cd /opt
sudo git clone <your-repository-url> tg-save-helper
sudo chown -R "$USER":"$USER" /opt/tg-save-helper
cd /opt/tg-save-helper
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，设置 `TG_API_ID`、`TG_API_HASH`。`OWNER_ID` 可留空；若设置，它必须与登录账号 ID 一致，否则程序拒绝启动。

首次登录必须在交互式终端运行：

```bash
.venv/bin/python -m src.main
```

按提示输入手机号、Telegram 验证码以及两步验证密码。成功后会生成 `.session` 文件。确认能在 Saved Messages 发送 `/help` 并收到回复，再按 `Ctrl+C` 停止并配置 systemd。

## 指令

```text
/help
/last @example_channel 50
/between @example_channel 1000 1100
/link https://t.me/example_channel/123
/link https://t.me/c/123456789/123
/watch @example_channel
/unwatch @example_channel
/listwatch
/status
```

`/last` 最大 200 条，`/between` 一次最大 500 个 ID。程序逐条转发并记日志，每 50 条为一批，批次间随机等待 2–5 秒，条目间也有短暂等待。遇到 Telegram `FloodWait` 会按服务端要求等待。受保护、已删除、无权限或无效的消息会记录并跳过，不会下载后重新上传。

`t.me/c/...` 链接只有在当前登录账号已经加入该聊天且 Telethon 会话能够解析该实体时才能使用。

## systemd 部署

示例服务假定项目位于 `/opt/tg-save-helper`、运行用户是 `ubuntu`。如果实际路径或用户不同，先修改 `tg-save-helper.service`。服务通过 `EnvironmentFile` 读取 `.env`，没有硬编码凭据。

```bash
sudo cp tg-save-helper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg-save-helper
sudo systemctl status tg-save-helper
```

查看实时日志：

```bash
sudo journalctl -u tg-save-helper -f
```

更新配置后执行：

```bash
sudo systemctl restart tg-save-helper
```

## 数据与安全

默认数据位于 `data/`：Telethon session 和 SQLite 数据库。`.env`、session、数据库已加入 `.gitignore`。

**session 文件等同于账号登录凭据。** 不要提交、发送或公开它；建议将项目目录和备份权限限制为仅服务用户可读。服务器失陷时，应立即在 Telegram 的“设备”设置中终止对应会话。不要同时运行两个使用同一 session 文件的进程。

SQLite 保存 `watched_sources`、`forwarding_logs` 和 `app_state`，因此 `/watch` 状态可跨重启恢复。程序仅处理 Saved Messages 中登录账号本人发出的 outgoing 指令；其他聊天中的命令会被忽略。

## 本地检查

```bash
.venv/bin/python -m py_compile src/*.py
```
