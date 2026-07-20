# 项目交接说明

更新时间：2026-07-20（Asia/Shanghai）

本文是当前运行实例的交接快照。长期架构、配置、命令和部署方式见 [README.md](README.md)，跨入口一致性约束见 [DEVELOPMENT.md](DEVELOPMENT.md)。新会话应先读完这三份文件，再查看 `git status`、systemd、管理面板和 SQLite；不要仅凭本文中的动态数字操作线上任务。

## 一、当前结论

- 服务：`tg-save-helper.service` 正在运行，入口为 `.venv/bin/python -m src.main`。
- 分支：`main`；远程仓库为公开 GitHub 项目。提交前必须再次检查敏感信息。
- 当前工作树在本次文档修改前是干净的。
- 最近代码提交：
  - `97a6a6c`：增加资源 Bot 连续多页成功回归测试；
  - `84de89f`：分页轮询始终保留本轮 `/start` 的完整响应窗口；
  - `2e005f7`：点击分页后验证页面确实推进；
  - `3942c84` / `0c6f595` / `d27d98e`：资源失效、无媒体、延迟响应分类。
- 测试：最近一次运行 56 项，全部通过。
- 管理面板嵌在主进程中，本机地址为 `127.0.0.1:8790`，路径前缀 `/tghelper`；公网由 nginx HTTPS 反代并要求 Basic Auth。

## 二、当前运行任务快照

检查时间：2026-07-20 09:54 CST 左右。以下断点之后会继续变化，接手时必须重新查询。

活跃手动任务共 5 个：

1. `/last https://t.me/toupai866 from https://t.me/toupai866/13741`
2. `/watch -2312388706 from https://t.me/c/2312388706/2069`
3. `/resource https://t.me/mijianqjlj from https://t.me/mijianqjlj/641`
4. `/watch https://t.me/loveyouokba from https://t.me/loveyouokba/297`
5. `/watch https://t.me/chiguaog from https://t.me/chiguaog/164`

暂停但保留现场的任务共 1 个：

```text
/resource https://t.me/utwtda from https://t.me/utwtda/775
```

这一个任务合并了同一来源的全部历史补发，不能拆成每条原帖一个 `/resource ... one` 任务。

持久监听：

- `watched_sources` 共 31 条：普通监听 27、资源监听 3、评论监听 1。
- 资源监听来源包括 `jibahenyanga`、`papashipin8`、`utwtda`。
- 评论监听包括 `@OFbozhu` 及其关联讨论区。
- 收藏完整备份监听和收藏视频转换监听均启用；快照时两者 `last_message_id` 均为 `290955`。

快速核对：

```bash
sudo systemctl status tg-save-helper --no-pager -l
sudo journalctl -u tg-save-helper -n 200 --no-pager
```

在控制 Bot 中发送 `/tasks`，或打开 HTTPS 管理面板查看内存任务。真实恢复现场以 SQLite 中的 `pending_manual_commands` 为准。不要用另一个 `--live-session` CLI 进程读取在线状态。

## 三、本轮已经完成的工作

### 1. 资源 Bot 延迟与分页

- 资源链接可以来自原帖、回复原帖的后续消息或关联评论区。
- 资源链接检测与资源提取分成队列，避免一个慢 Bot 阻塞所有新消息检测。
- 原帖与资源的固定顺序是：原帖 1 → 它的资源 → 原帖 2 → 它的资源。
- 支持“全部获取”、下一页、数字页码、分段页码导航，以及导航消息晚于媒体消息出现。
- 分页点击后比较完整消息签名；页面没有实际变化时抛出“资源机器人分页点击后未推进”，不再重复点击同一个按钮。
- 分页轮询不缩小 `/start` 响应窗口，避免把消息集合变化误判为翻页成功。
- 资源 Bot 明确失效、完整等待后无媒体、callback 不推进分别记录为不同错误。

### 2. 任务与重启恢复

- 手动长任务会把精确命令断点写入 `app_state.pending_manual_commands`。
- 任务完成或明确不可恢复时删除现场；限流、网络、超时等可恢复异常保留现场。
- watch 自动积压按“每个来源一个顺序范围”合并，不再每条消息创建一个长期 pending 任务。
- 服务重启后恢复扫描、转发、资源等待和收藏处理现场。
- `/tasks` 和管理面板显示当前阶段、当前消息、进度和恢复断点。

### 3. 转发一致性与限流

- `/last`、`/watch` 等历史补扫按旧到新边扫描边转发。
- 所有普通转发请求共用账号级随机间隔和批量静默期。
- `FloodWait` 通知包含原消息链接、完整触发命令、等待时间和预计恢复时间。
- 默认不重复转发成功原帖；`force` 才重做。

### 4. 收藏功能

- `/syncsaved` 将收藏复制到私有完整备份群，不依赖原频道继续存在。
- `/watchsaved` 补扫后持续备份新收藏。
- `/streamsaved` / `/watchstreamsaved` 将收藏视频封装或转码为可在线播放 MP4。
- 所有收藏历史任务边扫描边处理，并记录相册、下载、转换、上传和断点。

### 5. 控制入口

- BotFather 控制 Bot 已支持完整命令菜单；菜单与解析器有回归测试防止再次漏项。
- CLI 默认复制 session 到 `/tmp`，可做解析和真实命令调试。
- HTTPS 管理面板支持查看、启动、暂停、停止和重启任务，并已适配电脑和手机布局。

## 四、当前未完成与已知问题

### P0：`@arbbanyunbot` 历史多页资源未补齐

来源频道：`https://t.me/utwtda`

已知异常共 15 条：

- 13 条旧记录只收集/转发了第一页 10 个媒体，却曾被错误记为 `done`；目前已改回 `failed`。
- 消息 801：历史仅完成第 1/3 页；重复 `/start` 不重新发送完整资源。
- 消息 804：首次产生第 1/7 页，但 Bot 不响应下一页 callback。

异常消息 ID：

```text
775 778 781 786 792 795 798 801 804 925 928 933 936 942 945
```

已尝试把它们合并成一个任务从 775 重跑。首条 775 重试时，Bot 只返回 10 个媒体并直接显示“发送完毕”，没有重新提供原来的 1/3 分页。因此任务已暂停，775 保持 `failed`，不能宣称历史数据已经补齐。

下一步：

1. 先用一个新的、确认未获取过且确有多页的 `@arbbanyunbot` payload 做真实测试。
2. 验证 callback 能从第 1 页推进到最后一页，并核对每页媒体数量。
3. 只有确认第三方 Bot 恢复后，才从面板恢复唯一的 `from 775` 合并任务。
4. 补发后按 `collected_count`、`forwarded_count`、Bot 页数和收藏实际消息共同验收，不能只看 Bot 的“发送完毕”文本。

### P1：数据库仍有 processing 记录

快照时 `resource_bot_links` 汇总为：`done=1127`、`empty=4`、`failed=17`、`processing=2`。接手后应把两条 `processing` 与面板活跃任务逐一对应；只有确认没有活跃任务且现场明确陈旧，才能改为 `failed`，不能直接删除。

### P1：第三方 Bot 行为不可由单元测试保证

代码测试能验证点击策略、等待窗口和状态分类，但 Bot 可能延迟发送、对重复 payload 只返回提示文字、callback 静默不响应，或产生 FloodWait。真实修复必须用新 payload 做端到端验证。

## 五、下一步建议顺序

1. 重新读取面板和 pending 列表，确认上述 5+1 任务是否已变化。
2. 对照活跃任务清理或保留两条 `processing` 资源记录。
3. 找一个新的 `@arbbanyunbot` 多页链接做单条真实回归。
4. Bot 恢复后，只恢复一个 `utwtda from 775` 合并任务并持续观察。
5. 验收 15 条异常记录；成功后再将其标记为 `done`。
6. 运行完整测试，检查 Git diff 与敏感信息，再提交并推送文档/代码。

## 六、绝对不能碰

以下是线上安全和数据完整性边界：

- 绝不能提交、打印或复制 `.env`、`*.session*`、数据库、Bot token、API hash、Basic Auth 密码或密码文件内容。
- 绝不能同时运行两个使用同一 Telethon session 的进程；服务运行时不要使用 CLI `--live-session`。
- 绝不能为了“清爽”批量删除 `pending_manual_commands`、资源现场、转发日志或失败记录。
- 绝不能把可恢复异常的现场删除；只有消息明确不存在、永久无权访问、参数错误等不可恢复情况才可清理。
- 绝不能把同一频道的历史补发拆成数百个 `/resource ... one` 任务；必须合并为一个来源顺序任务。
- 绝不能对历史合并补发盲目加 `force`，否则会重复转发已经成功的原帖和资源。
- 绝不能仅因第三方 Bot 显示“发送完毕”就把已知多页记录标记完成；必须验证页数和实际媒体数量。
- 绝不能在分页未推进时转发第一页部分结果并记为成功。
- 绝不能只修改 `/resource`、`/watchresource`、`/resourcelink` 中的一个入口；三者共享语义。评论和普通转发入口同理。
- 绝不能清空 `resource_bot_links` 的 `start_message_id`、响应范围和计数字段；这些是回溯 `/start` 原帖的现场。
- 绝不能通过独立面板进程管理任务；面板必须留在主进程，才能操作真实 asyncio task。
- 绝不能让管理面板直接监听公网地址，或取消 nginx/面板 Basic Auth。
- 修改 nginx 前必须先备份现有站点配置，不能影响同域名其他服务。
- 不要使用破坏性 Git 命令覆盖用户改动，不要提交 `data/` 或运行日志。

## 七、开发与验证约束

修改前先写或确认能复现问题的测试，修复后至少运行：

```bash
.venv/bin/python -m unittest discover -s tests -v
python3 -m py_compile src/*.py
git diff --check
git status --short
```

涉及真实 Telegram 行为时：

- 优先用单条新消息或新 payload 验证。
- 控制 Bot 启动间隔和转发间隔，避免制造 FloodWait。
- 只查看相关 `journalctl` 片段，不要把全量日志灌入会话。
- 操作线上任务前记录命令、来源、当前消息和恢复断点。
- 操作后同时核对面板、SQLite 和 Telegram 实际结果。

## 八、常用定位入口

- 命令解析与帮助：`src/commands.py`
- 命令执行、watch、分页、FloodWait：`src/telegram_client.py`
- 任务面板：`src/panel.py`
- 数据库与状态：`src/db.py`
- 资源/恢复回归：`tests/test_watchcomments_recheck.py`
- 收藏功能回归：`tests/test_saved_features.py`
- 长期设计约束：`DEVELOPMENT.md`

如果本文与运行状态冲突，以“当前代码 + SQLite + 主进程面板 + Telegram 实际消息”四者核对后的结论为准。

