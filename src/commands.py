from __future__ import annotations

import shlex
from dataclasses import dataclass


MAX_ID_RANGE = 500
MAX_SYNC_SAVED_COUNT = 1000


class CommandError(ValueError):
    pass


@dataclass(frozen=True)
class Command:
    name: str
    args: tuple[str, ...] = ()


def parse_command(text: str) -> Command | None:
    text = text.strip()
    if not text.startswith("/"):
        return None
    try:
        parts = shlex.split(text)
    except ValueError as exc:
        raise CommandError(f"指令格式错误：{exc}") from exc
    if not parts:
        return None
    name = parts[0].split("@", 1)[0].lower()
    if name == "/syncsaved_download":
        name = "/syncsaved-download"
    known = {
        "/help", "/stop", "/last", "/unread", "/between", "/link", "/watch", "/unwatch",
        "/watchcomments", "/unwatchcomments", "/lastcomments",
        "/unreadcomments",
        "/resourcebot", "/resourcelink", "/resource", "/watchresource",
        "/unwatchresource", "/code", "/watchcode", "/unwatchcode", "/mixed",
        "/listwatch", "/status", "/tasks", "/stats",
        "/syncsaved",
        "/syncsaved-download",
        "/streamsaved", "/watchstreamsaved", "/unwatchstreamsaved",
        "/watchsaved", "/unwatchsaved", "/messageid",
    }
    if name not in known:
        raise CommandError("未知指令，请发送 /help 查看用法。")
    args = tuple(parts[1:])
    fixed_expected = {
        "/help": 0, "/stop": 0,
        "/unwatch": 1,
        "/unwatchcomments": 1,
        "/unwatchresource": 1,
        "/unwatchcode": 1,
        "/listwatch": 0, "/status": 0, "/tasks": 0,
        "/unwatchstreamsaved": 0, "/unwatchsaved": 0, "/messageid": 0,
    }
    variable_expected = {
        "/last": (1, 20),
        "/between": (3, 4),
        "/link": (1, 2),
        "/watch": (1, 20),
        "/watchcomments": (1, 20),
        "/watchresource": (1, 20),
        "/unread": (1, 20),
        "/lastcomments": (1, 20),
        "/unreadcomments": (1, 20),
        "/resourcelink": (1, 2),
        "/resource": (1, 20),
        "/code": (2, 20),
        "/watchcode": (2, 20),
        "/mixed": (2, 20),
        "/syncsaved": (1, 4),
        "/syncsaved-download": (1, 2),
        "/streamsaved": (1, 4),
        "/watchstreamsaved": (1, 4),
        "/watchsaved": (1, 4),
        "/stats": (0, 1),
    }
    if name in fixed_expected and len(args) != fixed_expected[name]:
        raise CommandError(f"参数数量错误，请发送 /help 查看 {name} 的用法。")
    if name in variable_expected:
        min_args, max_args = variable_expected[name]
        if not min_args <= len(args) <= max_args:
            raise CommandError(f"参数数量错误，请发送 /help 查看 {name} 的用法。")
    if name == "/last":
        _validate_selector_args(args, 1, "/last <source> <count|all|unread|from <message_link>> [force]")
    elif name == "/watch":
        _validate_selector_args(
            args, 1, f"{name} <source> [count|all|unread|from <message_link>] [force]", allow_empty=True
        )
    elif name == "/unread":
        _validate_selector_args(
            args,
            1,
            "/unread <source> [count|all|from <message_link>] [force]",
            allow_unread=False,
        )
    elif name == "/lastcomments":
        _validate_selector_args(
            args, 1, "/lastcomments <source> <count|all|unread|from <message_link>> [force]"
        )
    elif name == "/resource":
        _validate_resource_args(args)
    elif name == "/code":
        _validate_code_args(args)
    elif name == "/mixed":
        _validate_selector_args(
            args, 1, "/mixed <source> <count|all|from <message_link>> [force]", allow_unread=False
        )
    elif name == "/resourcelink":
        if len(args) == 2 and args[1].lower() != "force":
            raise CommandError("用法：/resourcelink <bot_deep_link> [force]")
    elif name == "/resourcebot":
        if not args or args[0].lower() not in {"add", "remove", "list"}:
            raise CommandError("用法：/resourcebot add|remove|list [username]")
        if args[0].lower() == "list" and len(args) != 1:
            raise CommandError("用法：/resourcebot list")
        if args[0].lower() in {"add", "remove"} and len(args) != 2:
            raise CommandError("用法：/resourcebot add|remove <username>")
    elif name == "/watchcomments":
        _validate_selector_args(
            args, 1, f"{name} <source> [count|all|unread|from <message_link>] [force]", allow_empty=True
        )
    elif name == "/watchresource":
        _validate_selector_args(
            args, 1, f"{name} <source> [count|all|unread|from <message_link>] [force]", allow_empty=True
        )
    elif name == "/unreadcomments":
        _validate_selector_args(
            args,
            1,
            "/unreadcomments <source> [count|all|from <message_link>] [force]",
            allow_unread=False,
        )
    elif name == "/watchcode":
        _validate_watchcode_args(args)
    elif name == "/between":
        start_id = _positive_int(args[1], "start_id")
        end_id = _positive_int(args[2], "end_id")
        if start_id > end_id:
            raise CommandError("start_id 不能大于 end_id。")
        if end_id - start_id + 1 > MAX_ID_RANGE:
            raise CommandError(f"一次最多处理 {MAX_ID_RANGE} 个 message id。")
        _validate_force_tail(args, "/between <source> <start_id> <end_id> [force]", 3)
    elif name == "/link":
        _validate_force_tail(args, "/link <telegram_message_link> [force]", 1)
    elif name == "/stats":
        if args and args[0].lower() not in {"day", "today", "month", "year"}:
            raise CommandError("用法：/stats [day|month|year]")
    elif name in {"/syncsaved", "/streamsaved", "/watchstreamsaved", "/watchsaved"}:
        _validate_saved_selector_args(args, name)
    elif name == "/syncsaved-download":
        if args[0].lower() != "all":
            count = _positive_int(args[0], "count")
            if count > MAX_SYNC_SAVED_COUNT:
                raise CommandError(
                    f"一次最多扫描 {MAX_SYNC_SAVED_COUNT} 条收藏消息，或使用 all。"
                )
    return Command(name=name, args=args)


def _validate_saved_selector_args(args: tuple[str, ...], name: str) -> None:
    usage = f"{name} <count|all|from [message_id|message_link]> [force]"
    core = _strip_tail_flags(args, {"force"})
    if not core:
        raise CommandError(f"用法：{usage}")
    if core[0].lower() == "from":
        if len(core) > 2:
            raise CommandError(f"用法：{usage}")
        return
    if len(core) != 1:
        raise CommandError(f"用法：{usage}")
    if core[0].lower() != "all":
        _positive_int(core[0], "count")


def _positive_int(value: str, label: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise CommandError(f"{label} 必须是整数。") from exc
    if number <= 0:
        raise CommandError(f"{label} 必须大于 0。")
    return number


def _validate_force_tail(args: tuple[str, ...], usage: str, base_count: int) -> None:
    if len(args) == base_count:
        return
    if len(args) == base_count + 1 and args[-1].lower() == "force":
        return
    raise CommandError(f"用法：{usage}")


def _strip_tail_flags(args: tuple[str, ...], flags: set[str]) -> tuple[str, ...]:
    core = list(args)
    while core and core[-1].lower() in flags:
        core.pop()
    return tuple(core)


def _validate_selector_args(
    args: tuple[str, ...],
    prefix_len: int,
    usage: str,
    *,
    allow_empty: bool = False,
    allow_one: bool = False,
    allow_unread: bool = True,
) -> None:
    core = _strip_tail_flags(args, {"force"})
    selector = core[prefix_len:]
    if len(core) < prefix_len:
        raise CommandError(f"用法：{usage}")
    if not selector:
        if allow_empty:
            return
        raise CommandError(f"用法：{usage}")
    lowered = [item.lower() for item in selector]
    if lowered[0] == "from":
        if len(selector) != 2:
            raise CommandError(f"用法：{usage}")
        return
    if allow_one and lowered[0] == "one":
        if len(selector) != 3 or lowered[1] != "from":
            raise CommandError(f"用法：{usage}")
        return
    if len(selector) != 1:
        raise CommandError(f"用法：{usage}")
    allowed_words = {"all"} | ({"unread"} if allow_unread else set())
    if lowered[0] not in allowed_words:
        _positive_int(selector[0], "count")


def _validate_resource_args(args: tuple[str, ...]) -> None:
    usage = "/resource <source> <count|all|unread|from <message_link>|one from <message_link>> [force]"
    _validate_selector_args(args, 1, usage, allow_one=True)


def _validate_code_args(args: tuple[str, ...]) -> None:
    usage = "/code <source> <extract_channel> <count|all|unread|from <message_link>> [force]"
    _validate_selector_args(args, 2, usage)


def _validate_watchcode_args(args: tuple[str, ...]) -> None:
    usage = "/watchcode <source> <extract_channel> [count|all|unread|from <message_link>] [force]"
    _validate_selector_args(args, 2, usage, allow_empty=True)


HELP_TEXT = """Telegram 收藏助手

/help - 显示帮助
/stop - 停止当前正在执行的手动命令
/last <source> <count|all|unread|from <message_link>> [force] - 原样转发最近/未读帖子，媒体相册保持组合
/unread <source> [count|all|from <message_link>] [force] - 转发未读消息；省略数量等同 all
/between <source> <start_id> <end_id> [force] - 按 ID 范围转发（最多 500 个 ID）
/link <telegram_message_link> [force] - 转发消息链接
/watch <source> [count|all|unread|from <message_link>] [force] - 监听新消息；可选补扫
/unwatch <source> - 取消监听
/watchcomments <source> [count|all|unread|from <message_link>] [force] - 监听频道主帖及评论区；可选补扫
/unwatchcomments <source> - 取消主帖及评论区监听
/watchresource <source> [count|all|unread|from <message_link>] [force] - 监听频道新帖资源链接；可选补扫
/unwatchresource <source> - 取消资源监听
/code <source> <extract_channel> <count|all|unread|from <message_link>> [force] - 转发提取码并收集提取频道机器人返回资源
/watchcode <source> <extract_channel> [count|all|unread|from <message_link>] [force] - 监听提取码消息；可选补扫
/unwatchcode <source> - 取消提取码监听
/lastcomments <source> <count|all|unread|from <message_link>> [force] - 转发最近/未读主帖及评论
/unreadcomments <source> [count|all|from <message_link>] [force] - 转发未读主帖及评论区未读评论；省略数量等同 all
/resourcebot add|remove|list [username] - 管理资源机器人白名单
/resourcelink <bot_deep_link> [force] - 触发单个资源机器人链接；force 强制重拉
/resource <source> <count|all|unread|from <message_link>|one from <message_link>> [force] - 扫描资源链接；one 只处理指定原帖
/mixed <source> <count|all> [force] - 自动选择 last/lastcomments/resource 混合转发
/listwatch - 列出监听源
/status - 查看运行状态
/tasks - 查看当前长任务进度
/stats [day|month|year] - 统计当天/当月/当年的转发和同步
/messageid - 回复一条收藏消息，查看消息 ID
/streamsaved <count|all|from [message_id|message_link]> [force] - 将收藏视频转换为可在线播放视频
/watchstreamsaved <count|all|from [message_id|message_link]> [force] - 补处理并监听收藏视频
/unwatchstreamsaved - 停止监听收藏视频
/syncsaved <count|all|from [message_id|message_link]> [force] - 完整复制收藏到私有备份群
/watchsaved <count|all|from [message_id|message_link]> [force] - 补备份并监听新收藏
/unwatchsaved - 停止监听收藏备份
/syncsaved-download <count|all> [source|unknown] - 下载收藏媒体后重新上传（会消耗磁盘和流量）

source 可使用 @username、公开链接或 Telegram 可识别的聊天 ID。syncsaved 的 source 还可使用 unknown 表示未知来源兜底频道。每批最多 50 条，受保护或无权访问的消息会跳过。

/syncsaved 会复制文字和媒体到“我的收藏_完整备份”，媒体不依赖原频道继续存在。from 不带值时请回复目标收藏消息。/syncsaved-download 保留旧的按来源下载上传模式。"""
