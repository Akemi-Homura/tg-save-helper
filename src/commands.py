from __future__ import annotations

import shlex
from dataclasses import dataclass


MAX_LAST_COUNT = 200
MAX_ID_RANGE = 500


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
    known = {"/help", "/last", "/between", "/link", "/watch", "/unwatch", "/listwatch", "/status"}
    if name not in known:
        raise CommandError("未知指令，请发送 /help 查看用法。")
    args = tuple(parts[1:])
    expected = {
        "/help": 0, "/last": 2, "/between": 3, "/link": 1,
        "/watch": 1, "/unwatch": 1, "/listwatch": 0, "/status": 0,
    }
    if len(args) != expected[name]:
        raise CommandError(f"参数数量错误，请发送 /help 查看 {name} 的用法。")
    if name == "/last":
        count = _positive_int(args[1], "count")
        if count > MAX_LAST_COUNT:
            raise CommandError(f"count 不能超过 {MAX_LAST_COUNT}。")
    elif name == "/between":
        start_id = _positive_int(args[1], "start_id")
        end_id = _positive_int(args[2], "end_id")
        if start_id > end_id:
            raise CommandError("start_id 不能大于 end_id。")
        if end_id - start_id + 1 > MAX_ID_RANGE:
            raise CommandError(f"一次最多处理 {MAX_ID_RANGE} 个 message id。")
    return Command(name=name, args=args)


def _positive_int(value: str, label: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise CommandError(f"{label} 必须是整数。") from exc
    if number <= 0:
        raise CommandError(f"{label} 必须大于 0。")
    return number


HELP_TEXT = """Telegram 收藏助手

/help - 显示帮助
/last <source> <count> - 转发最近消息（最多 200 条）
/between <source> <start_id> <end_id> - 按 ID 范围转发（最多 500 个 ID）
/link <telegram_message_link> - 转发消息链接
/watch <source> - 监听新消息
/unwatch <source> - 取消监听
/listwatch - 列出监听源
/status - 查看运行状态

source 可使用 @username、公开链接或 Telegram 可识别的聊天 ID。每批最多 50 条，受保护或无权访问的消息会跳过。"""

