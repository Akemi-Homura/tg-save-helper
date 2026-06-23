from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from telethon import TelegramClient, events, utils
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    ChatForwardsRestrictedError,
    FloodWaitError,
    MessageIdInvalidError,
    RPCError,
)
from telethon.tl.custom.message import Message

from .commands import Command, CommandError, HELP_TEXT, parse_command
from .config import Config
from .db import Database


LOGGER = logging.getLogger(__name__)
BATCH_SIZE = 50
LINK_RE = re.compile(
    r"^https?://(?:www\.)?t\.me/(?:(?:s/)?(?P<username>[A-Za-z0-9_]+)/|c/(?P<internal>\d+)/)(?P<message_id>\d+)(?:\?.*)?$"
)


@dataclass
class ForwardResult:
    success: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = f"处理完成：成功 {self.success}，失败 {self.failed}，跳过 {self.skipped}。"
        if self.errors:
            text += "\n最近的失败/跳过原因：\n" + "\n".join(f"- {item}" for item in self.errors[-5:])
        return text


class TelegramSaveHelper:
    def __init__(self, config: Config, database: Database) -> None:
        self.config = config
        self.db = database
        self.client = TelegramClient(config.session_name, config.api_id, config.api_hash)
        self.owner_id = 0
        self.forward_lock = asyncio.Lock()

    async def login_only(self) -> None:
        await self.client.start()
        try:
            me = await self.client.get_me()
            if me is None:
                raise RuntimeError("Telegram login failed")
            if self.config.owner_id is not None and self.config.owner_id != int(me.id):
                raise RuntimeError(
                    f"OWNER_ID={self.config.owner_id} does not match logged-in user {me.id}"
                )
            LOGGER.info("Login successful for Telegram user %s; session saved", me.id)
        finally:
            await self.client.disconnect()

    async def run(self) -> None:
        await self.client.start()
        me = await self.client.get_me()
        if me is None:
            raise RuntimeError("Telegram login failed")
        self.owner_id = int(me.id)
        if self.config.owner_id is not None and self.config.owner_id != self.owner_id:
            await self.client.disconnect()
            raise RuntimeError(
                f"OWNER_ID={self.config.owner_id} does not match logged-in user {self.owner_id}"
            )

        self.client.add_event_handler(self._handle_control_message, events.NewMessage(outgoing=True))
        self.client.add_event_handler(self._handle_watched_message, events.NewMessage())
        LOGGER.info("Logged in as user %s; loaded %d watched sources", self.owner_id, len(self.db.list_watches()))
        try:
            await self.client.run_until_disconnected()
        finally:
            await self.client.disconnect()

    async def _handle_control_message(self, event: events.NewMessage.Event) -> None:
        if event.chat_id != self.owner_id or event.sender_id != self.owner_id:
            return
        text = event.raw_text or ""
        if not text.lstrip().startswith("/"):
            return
        try:
            command = parse_command(text)
            if command is not None:
                await self._execute_command(command, event)
        except CommandError as exc:
            await self._reply(event, str(exc))
        except Exception as exc:  # Keep the event loop alive on unexpected API errors.
            LOGGER.exception("Command failed")
            self._remember_error(exc)
            await self._reply(event, f"执行失败：{self._error_text(exc)}")

    async def _execute_command(self, command: Command, event: events.NewMessage.Event) -> None:
        if command.name == "/help":
            await self._reply(event, HELP_TEXT)
        elif command.name == "/last":
            await self._forward_last(event, command.args[0], int(command.args[1]))
        elif command.name == "/between":
            await self._forward_between(event, command.args[0], int(command.args[1]), int(command.args[2]))
        elif command.name == "/link":
            await self._forward_link(event, command.args[0])
        elif command.name == "/watch":
            await self._watch(event, command.args[0])
        elif command.name == "/unwatch":
            await self._unwatch(event, command.args[0])
        elif command.name == "/listwatch":
            await self._list_watches(event)
        elif command.name == "/status":
            await self._status(event)

    async def _forward_last(self, event: events.NewMessage.Event, source: str, count: int) -> None:
        entity = await self._resolve_source(source)
        messages = [message async for message in self.client.iter_messages(entity, limit=count)]
        messages.reverse()
        result = await self._forward_many(source, messages, expected_ids=ids)
        await self._reply(event, result.summary())

    async def _forward_between(
        self, event: events.NewMessage.Event, source: str, start_id: int, end_id: int
    ) -> None:
        entity = await self._resolve_source(source)
        ids = list(range(start_id, end_id + 1))
        messages = await self.client.get_messages(entity, ids=ids)
        result = await self._forward_many(source, messages)
        await self._reply(event, result.summary())

    async def _forward_link(self, event: events.NewMessage.Event, link: str) -> None:
        match = LINK_RE.fullmatch(link.strip())
        if not match:
            raise CommandError("不支持的消息链接。请使用 https://t.me/channel/123 或 https://t.me/c/123/456。")
        if match.group("username"):
            source = "@" + match.group("username")
        else:
            source = "-100" + match.group("internal")
        message_id = int(match.group("message_id"))
        entity = await self._resolve_source(source)
        message = await self.client.get_messages(entity, ids=message_id)
        result = await self._forward_many(source, [message], expected_ids=[message_id])
        await self._reply(event, result.summary())

    async def _watch(self, event: events.NewMessage.Event, source: str) -> None:
        entity = await self._resolve_source(source)
        peer_id = int(utils.get_peer_id(entity))
        title = utils.get_display_name(entity) or source
        self.db.add_watch(source, peer_id, title)
        await self._reply(event, f"已监听：{title}（{source}）")

    async def _unwatch(self, event: events.NewMessage.Event, source: str) -> None:
        removed = self.db.remove_watch(source=source)
        if not removed:
            try:
                entity = await self._resolve_source(source)
                removed = self.db.remove_watch(peer_id=int(utils.get_peer_id(entity)))
            except (CommandError, ValueError, RPCError):
                pass
        await self._reply(event, "已取消监听。" if removed else "未找到该监听源。")

    async def _list_watches(self, event: events.NewMessage.Event) -> None:
        watches = self.db.list_watches()
        if not watches:
            await self._reply(event, "当前没有监听源。")
            return
        lines = [f"{index}. {item.title}（{item.source}）" for index, item in enumerate(watches, 1)]
        await self._reply(event, "当前监听源：\n" + "\n".join(lines))

    async def _status(self, event: events.NewMessage.Event) -> None:
        last_error = self.db.get_state("last_error", "无")
        last_forward = self.db.get_state("last_forward_at", "无")
        text = (
            "运行状态\n"
            f"- 已登录：是（{self.owner_id}）\n"
            f"- 监听数量：{len(self.db.list_watches())}\n"
            f"- 最近转发时间：{last_forward}\n"
            f"- 最近错误：{last_error}\n"
            f"- 已转发总数：{self.db.successful_count()}"
        )
        await self._reply(event, text)

    async def _handle_watched_message(self, event: events.NewMessage.Event) -> None:
        if event.chat_id is None or event.chat_id == self.owner_id:
            return
        if int(event.chat_id) not in self.db.watched_peer_ids():
            return
        source = next(
            (watch.source for watch in self.db.list_watches() if watch.peer_id == int(event.chat_id)),
            str(event.chat_id),
        )
        result = await self._forward_many(source, [event.message])
        if result.failed or result.skipped:
            await self.client.send_message("me", f"监听源 {source} 的消息 {event.id} 未能转发。\n{result.summary()}")

    async def _forward_many(
        self,
        source: str,
        messages: Iterable[Message | None],
        expected_ids: Iterable[int] | None = None,
    ) -> ForwardResult:
        items = list(messages)
        ids = list(expected_ids) if expected_ids is not None else []
        result = ForwardResult()
        async with self.forward_lock:
            for index, message in enumerate(items):
                if message is None:
                    message_id = ids[index] if index < len(ids) else 0
                    self._record_skip(result, source, message_id, "消息不存在、已删除或无权访问")
                else:
                    await self._forward_one(source, message, result)
                if index + 1 < len(items):
                    if (index + 1) % BATCH_SIZE == 0:
                        await asyncio.sleep(random.uniform(2.0, 5.0))
                    else:
                        await asyncio.sleep(random.uniform(0.25, 0.6))
        return result

    async def _forward_one(self, source: str, message: Message, result: ForwardResult) -> None:
        for attempt in range(3):
            try:
                await self.client.forward_messages("me", message)
                result.success += 1
                self.db.log_forward(source, message.id, "success")
                self.db.set_state("last_forward_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
                return
            except FloodWaitError as exc:
                wait_seconds = int(exc.seconds) + 1
                LOGGER.warning("FloodWait for %d seconds", wait_seconds)
                self._remember_error(exc)
                if attempt == 2:
                    self._record_failure(result, source, message.id, exc)
                    return
                await asyncio.sleep(wait_seconds)
            except (ChatForwardsRestrictedError, MessageIdInvalidError, ChannelPrivateError, ChatAdminRequiredError) as exc:
                self._record_skip(result, source, message.id, self._error_text(exc))
                return
            except RPCError as exc:
                self._record_failure(result, source, message.id, exc)
                return
            except Exception as exc:
                LOGGER.exception("Unexpected forwarding failure for %s/%s", source, message.id)
                self._record_failure(result, source, message.id, exc)
                return

    def _record_skip(self, result: ForwardResult, source: str, message_id: int, reason: str) -> None:
        result.skipped += 1
        result.errors.append(f"消息 {message_id}: {reason}")
        self.db.log_forward(source, message_id, "skipped", reason)
        self.db.set_state("last_error", reason)

    def _record_failure(self, result: ForwardResult, source: str, message_id: int, exc: Exception) -> None:
        reason = self._error_text(exc)
        result.failed += 1
        result.errors.append(f"消息 {message_id}: {reason}")
        self.db.log_forward(source, message_id, "failed", reason)
        self._remember_error(exc)

    def _remember_error(self, exc: Exception) -> None:
        self.db.set_state("last_error", self._error_text(exc))

    @staticmethod
    def _error_text(exc: Exception) -> str:
        text = str(exc).replace("\n", " ").strip()
        return f"{type(exc).__name__}: {text}"[:500]

    async def _resolve_source(self, source: str) -> Any:
        value: str | int = source.strip()
        public_match = re.fullmatch(r"https?://(?:www\.)?t\.me/(?:s/)?([A-Za-z0-9_]+)/?", value)
        if public_match:
            value = "@" + public_match.group(1)
        elif re.fullmatch(r"-?\d+", value):
            value = int(value)
        try:
            return await self.client.get_entity(value)
        except (ValueError, RPCError) as exc:
            if isinstance(value, int):
                async for dialog in self.client.iter_dialogs():
                    if int(dialog.id) == value:
                        return dialog.entity
            raise CommandError(f"无法访问 source {source}：{self._error_text(exc)}") from exc

    async def _reply(self, event: events.NewMessage.Event, text: str) -> None:
        await self.client.send_message("me", text, reply_to=event.id)
