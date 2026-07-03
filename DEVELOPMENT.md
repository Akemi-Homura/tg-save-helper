# 项目开发文档

## 验证原则

改转发逻辑时必须同时验证对应的手动命令和监听命令。最小检查：

```bash
.venv/bin/python -m py_compile src/*.py
```

涉及 Telegram 真实行为时，用 `src.cli` 或一次性脚本验证实际消息流；不要只靠静态推断。

## 转发入口一致性

成对或成组指令必须保持同一条原始消息上的转发语义一致：

- `/lastcomments`、`/watchcomments`、`/unreadcomments` 共用评论区识别和读取规则。
- `/resource`、`/resourcelink`、`/watchresource` 共用资源链接识别、去重、翻页和转发规则。
- 修改其中一个入口时，同步检查同组入口，避免“手动命令能转，监听命令不能转”。

资源转发顺序固定为：

```text
原帖 1 → 原帖 1 的资源 → 原帖 2 → 原帖 2 的资源
```

不能先批量转发所有原帖，再批量处理所有资源。

## 资源机器人现场记录

`resource_bot_links` 是资源链接的去重和回溯表。除 `bot_username + payload` 外，还记录：

- `source`、`source_message_id`：资源链接来自哪条原帖；
- `start_message_id`：发给资源 bot 的 `/start` 消息 ID；
- `first_response_id`、`last_response_id`：本轮 bot 响应范围；
- `collected_count`、`forwarded_count`：本轮收集和转发的媒体数量。

这些字段用于排查“资源 bot 已返回第 1/N 页但没有继续翻页”的情况。后续看到 bot 会话里的 `/start` 或分页消息时，应先用这些字段回溯原帖和 payload。

`upsert_resource_link()` 会保留已有现场字段；不要在失败或 processing 状态更新时清空这些字段。

## 资源机器人翻页规则

分页状态来自消息文本里的 `第 n/m 页`，不要求该消息带媒体文件。很多 bot 会把分页导航作为纯文本消息发送，例如：

```text
📄 全部文件
分页导航（第 1/2 页）
```

因此翻页逻辑必须在所有响应消息上记录当前页，而不是只看媒体消息。否则会重复点击当前页按钮，导致后续页漏转。

## 评论区规则

`iter_messages(entity, reply_to=post.id)` 已经限定到该频道主帖的讨论串。不要再用易碎的二次过滤丢弃评论。`watchcomments` 对主频道新帖也应走“原帖 + 已有评论”的同一套读取逻辑；讨论群里的真实新评论继续按评论事件转发。

## 收藏媒体同步

`/syncsaved` 使用 Telegram 服务端复制，不下载媒体；`/syncsaved-download` 才会下载并重新上传。两者共享已同步消息记录。

无法识别来源的收藏媒体进入 `收藏媒体_未知来源`。同步到各来源私有频道后，再转发到 `收藏媒体汇总`，让汇总频道保留“转发自”对应来源私有频道。

## 管理面板部署

管理面板嵌在 `TelegramSaveHelper` 主进程中，才能直接访问内存里的 active tasks 并复用 `_execute_command()` / task cancel 逻辑。不要另起一个只能读 SQLite 的面板进程，否则停止/暂停活跃任务会失真。

默认路径前缀是 `/tghelper`，服务只监听 `127.0.0.1`。公网 HTTPS 由 nginx 反代，Basic Auth 必须只作用在该 location，不能影响同域名其它项目。

密码不要写进 systemd unit，也不要提交。线上环境当前使用：

```text
/home/quals/tgbot/.env              # TG_PANEL_* 运行配置，权限 600
/etc/nginx/.tghelper.htpasswd       # nginx Basic Auth 哈希
/etc/nginx/.tghelper.credentials    # 固定登录账号密码，root-only
```

修改 nginx 前先备份 `/etc/nginx/sites-enabled/quals.site`，新增路径后同步更新 `/home/quals/HTTPS-DEPLOYMENT.md` 的“已占用 URL”。
