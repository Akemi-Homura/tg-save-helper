from __future__ import annotations

import asyncio
import logging
import random
import re
from pathlib import Path
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
    MsgIdInvalidError,
    RPCError,
)
from telethon.tl.custom.message import Message
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.types import Channel, PeerChannel

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
        self.saved_sync_lock = asyncio.Lock()
        self.valid_comment_roots: set[tuple[int, int, int]] = set()

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
        self.client.add_event_handler(self._handle_watched_album, events.Album())
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
        elif command.name == "/watchcomments":
            await self._watch_comments(event, command.args[0])
        elif command.name == "/unwatchcomments":
            await self._unwatch_comments(event, command.args[0])
        elif command.name == "/lastcomments":
            await self._forward_last_comments(event, command.args[0], int(command.args[1]))
        elif command.name == "/listwatch":
            await self._list_watches(event)
        elif command.name == "/status":
            await self._status(event)
        elif command.name == "/syncsaved":
            await self._sync_saved_media(
                event, self._saved_sync_limit(command.args[0]), download_upload=False
            )
        elif command.name == "/syncsaved-download":
            await self._sync_saved_media(
                event, self._saved_sync_limit(command.args[0]), download_upload=True
            )

    async def _forward_last(self, event: events.NewMessage.Event, source: str, count: int) -> None:
        entity = await self._resolve_source(source)
        groups = await self._recent_message_groups(entity, count)
        messages = [message for group in groups for message in group]
        result = await self._forward_many(source, messages)
        await self._reply(event, result.summary() + f"\n逻辑帖子 {len(groups)} 个。")

    async def _forward_between(
        self, event: events.NewMessage.Event, source: str, start_id: int, end_id: int
    ) -> None:
        entity = await self._resolve_source(source)
        ids = list(range(start_id, end_id + 1))
        messages = await self.client.get_messages(entity, ids=ids)
        result = await self._forward_many(source, messages, expected_ids=ids)
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

    async def _watch_comments(self, event: events.NewMessage.Event, source: str) -> None:
        entity = await self._resolve_source(source)
        linked_entity = await self._get_linked_discussion(entity)

        peer_id = int(utils.get_peer_id(entity))
        linked_peer_id = int(utils.get_peer_id(linked_entity))
        title = utils.get_display_name(entity) or source
        linked_title = utils.get_display_name(linked_entity) or str(linked_peer_id)
        self.db.add_watch(
            source,
            peer_id,
            title,
            mode="comments",
            linked_peer_id=linked_peer_id,
            linked_title=linked_title,
        )
        membership_note = ""
        if getattr(linked_entity, "left", False):
            membership_note = "\n请先加入该讨论群，否则 Telegram 可能不会推送新评论。"
        await self._reply(
            event,
            f"已监听频道及评论区：{title}（{source}）\n关联讨论群：{linked_title}{membership_note}",
        )

    async def _forward_last_comments(
        self, event: events.NewMessage.Event, source: str, count: int
    ) -> None:
        entity = await self._resolve_source(source)
        await self._get_linked_discussion(entity)
        channel_peer_id = int(utils.get_peer_id(entity))
        post_groups = await self._recent_message_groups(entity, count)

        messages: list[Message] = []
        comment_count = 0
        for post_group in post_groups:
            messages.extend(post_group)
            comments: list[Message] | None = None
            for post in post_group:
                try:
                    comments = [
                        message
                        async for message in self.client.iter_messages(
                            entity, reply_to=post.id, limit=None
                        )
                    ]
                    break
                except MsgIdInvalidError:
                    continue
            if comments is None:
                continue
            comments.reverse()
            for comment in comments:
                if await self._is_channel_comment(comment, channel_peer_id):
                    messages.append(comment)
                    comment_count += 1

        result = await self._forward_many(f"{source}#with-comments", messages)
        details = f"\n主帖 {len(post_groups)} 个，评论 {comment_count} 条。"
        await self._reply(event, result.summary() + details)

    async def _recent_message_groups(self, entity: Any, count: int) -> list[list[Message]]:
        order: list[tuple[str, int]] = []
        grouped: dict[tuple[str, int], list[Message]] = {}
        async for message in self.client.iter_messages(entity, limit=None):
            key = (
                ("album", int(message.grouped_id))
                if message.grouped_id is not None
                else ("message", int(message.id))
            )
            if key not in grouped:
                if len(order) >= count:
                    break
                order.append(key)
                grouped[key] = []
            grouped[key].append(message)
        return [list(reversed(grouped[key])) for key in reversed(order)]

    async def _get_linked_discussion(self, entity: Any) -> Any:
        if not isinstance(entity, Channel) or entity.megagroup:
            raise CommandError("该指令仅支持带关联评论区的频道。")
        full = await self.client(GetFullChannelRequest(entity))
        linked_id = full.full_chat.linked_chat_id
        if linked_id is None:
            raise CommandError("该频道没有关联讨论群，无法处理评论区。")
        linked_entity = next(
            (chat for chat in full.chats if getattr(chat, "id", None) == linked_id),
            None,
        )
        if linked_entity is not None:
            return linked_entity
        return await self.client.get_entity(PeerChannel(linked_id))

    async def _unwatch(self, event: events.NewMessage.Event, source: str) -> None:
        removed = self.db.remove_watch(source=source)
        if not removed:
            try:
                entity = await self._resolve_source(source)
                removed = self.db.remove_watch(peer_id=int(utils.get_peer_id(entity)))
            except (CommandError, ValueError, RPCError):
                pass
        await self._reply(event, "已取消监听。" if removed else "未找到该监听源。")

    async def _unwatch_comments(self, event: events.NewMessage.Event, source: str) -> None:
        removed = self.db.remove_watch(source=source, mode="comments")
        if not removed:
            try:
                entity = await self._resolve_source(source)
                removed = self.db.remove_watch(
                    peer_id=int(utils.get_peer_id(entity)), mode="comments"
                )
            except (CommandError, ValueError, RPCError):
                pass
        await self._reply(
            event,
            "已取消频道及评论区监听。" if removed else "未找到该评论区监听。",
        )

    async def _list_watches(self, event: events.NewMessage.Event) -> None:
        watches = self.db.list_watches()
        if not watches:
            await self._reply(event, "当前没有监听源。")
            return
        lines = []
        for index, item in enumerate(watches, 1):
            suffix = f" + 评论区（{item.linked_title}）" if item.mode == "comments" else ""
            lines.append(f"{index}. {item.title}（{item.source}）{suffix}")
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
            f"\n- 收藏媒体已同步：{self.db.saved_sync_count()}"
        )
        await self._reply(event, text)

    async def _sync_saved_media(
        self,
        event: events.NewMessage.Event,
        count: int | None,
        *,
        download_upload: bool,
    ) -> None:
        """Copy Saved Messages media by source channel, optionally via local files."""
        result = ForwardResult()
        channel_count = 0
        async with self.saved_sync_lock:
            messages = await self._saved_messages_for_sync(count)
            scanned_count = len(messages)
            messages.reverse()
            groups = self._group_messages(messages)
            destinations: dict[int, Any] = {}

            for group in groups:
                media_group = [message for message in group if message.file is not None]
                if not media_group:
                    for message in group:
                        self._record_saved_skip(result, message.id, "不是媒体消息")
                    continue

                unsynced = [
                    message
                    for message in media_group
                    if not self.db.saved_message_was_synced(message.id)
                ]
                result.skipped += len(media_group) - len(unsynced)
                if not unsynced:
                    continue

                protected = [
                    message for message in unsynced if getattr(message, "noforwards", False)
                ]
                if protected:
                    protected_ids = {message.id for message in protected}
                    for message in protected:
                        self._record_saved_skip(result, message.id, "消息受保护，禁止保存或转发")
                    unsynced = [
                        message for message in unsynced if message.id not in protected_ids
                    ]
                    if not unsynced:
                        continue

                source = await self._saved_forward_source(unsynced[0])
                if source is None:
                    for message in unsynced:
                        self._record_saved_skip(
                            result, message.id, "无法识别原转发频道（可能是匿名来源或非频道消息）"
                        )
                    continue
                source_peer_id, source_title = source

                try:
                    destination = destinations.get(source_peer_id)
                    if destination is None:
                        destination, created = await self._saved_destination(
                            source_peer_id, source_title
                        )
                        destinations[source_peer_id] = destination
                        channel_count += int(created)
                    paths: list[Path] | None = None
                    if download_upload:
                        paths = await self._download_saved_group(
                            unsynced, source_peer_id, source_title
                        )
                        await self._upload_saved_group(destination, unsynced, paths)
                    else:
                        await self._copy_saved_group(destination, unsynced)
                    destination_peer_id = int(utils.get_peer_id(destination))
                    for index, message in enumerate(unsynced):
                        local_path = str(paths[index]) if paths is not None else None
                        self.db.mark_saved_message_synced(
                            message.id, source_peer_id, destination_peer_id, local_path
                        )
                    result.success += len(unsynced)
                    self.db.set_state(
                        "last_saved_sync_at",
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    )
                except Exception as exc:
                    LOGGER.exception("Saved media sync failed for %s", source_title)
                    for message in unsynced:
                        self._record_failure(
                            result, f"saved:{source_peer_id}", message.id, exc
                        )

                await asyncio.sleep(random.uniform(0.5, 1.2))

        scope = "全部收藏消息" if count is None else f"最近 {count} 条收藏消息"
        detail = (
            f"\n扫描范围：{scope}；实际读取 {scanned_count} 条；"
            f"新建目标频道 {channel_count} 个。"
        )
        if download_upload:
            detail += f"\n模式：下载后上传；下载目录：{self.config.saved_media_path}"
        else:
            detail += "\n模式：Telegram 服务器端复制；未下载媒体文件。"
        await self._reply(event, result.summary() + detail)

    @staticmethod
    def _saved_sync_limit(value: str) -> int | None:
        return None if value.lower() == "all" else int(value)

    async def _saved_messages_for_sync(self, count: int | None) -> list[Message]:
        """Read Saved Messages and complete an album cut by a numeric limit."""
        messages = [
            message async for message in self.client.iter_messages("me", limit=count)
        ]
        if count is None or not messages or messages[-1].grouped_id is None:
            return messages

        boundary_group_id = messages[-1].grouped_id
        oldest_id = messages[-1].id
        async for message in self.client.iter_messages(
            "me", offset_id=oldest_id, limit=10
        ):
            if message.grouped_id != boundary_group_id:
                break
            messages.append(message)
        return messages

    @staticmethod
    def _group_messages(messages: list[Message]) -> list[list[Message]]:
        groups: list[list[Message]] = []
        for message in messages:
            if (
                groups
                and message.grouped_id is not None
                and groups[-1][0].grouped_id == message.grouped_id
            ):
                groups[-1].append(message)
            else:
                groups.append([message])
        return groups

    async def _saved_forward_source(self, message: Message) -> tuple[int, str] | None:
        forward = message.fwd_from
        if forward is None or not isinstance(forward.from_id, PeerChannel):
            return None
        peer_id = int(utils.get_peer_id(forward.from_id))
        entity = getattr(getattr(message, "forward", None), "chat", None)
        if entity is None:
            try:
                entity = await self.client.get_entity(forward.from_id)
            except (ValueError, RPCError):
                async for dialog in self.client.iter_dialogs():
                    if int(dialog.id) == peer_id:
                        entity = dialog.entity
                        break
        title = utils.get_display_name(entity) if entity is not None else ""
        return (peer_id, title) if title else None

    async def _saved_destination(
        self, source_peer_id: int, source_title: str
    ) -> tuple[Any, bool]:
        mapping = self.db.get_saved_channel_mapping(source_peer_id)
        if mapping is not None:
            try:
                entity = await self._resolve_source(str(mapping["destination_peer_id"]))
                return entity, False
            except CommandError:
                LOGGER.warning("Stored destination channel is unavailable; recreating it")

        async for dialog in self.client.iter_dialogs():
            candidate = dialog.entity
            if (
                isinstance(candidate, Channel)
                and candidate.broadcast
                and candidate.creator
                and int(utils.get_peer_id(candidate)) != source_peer_id
                and utils.get_display_name(candidate) == source_title
            ):
                self.db.save_channel_mapping(
                    source_peer_id, source_title, int(utils.get_peer_id(candidate))
                )
                return candidate, False

        created = await self.client(
            CreateChannelRequest(
                title=source_title[:128],
                about=f"由收藏夹同步；原频道：{source_title}"[:255],
                broadcast=True,
                megagroup=False,
            )
        )
        destination = next(
            (chat for chat in created.chats if isinstance(chat, Channel)), None
        )
        if destination is None:
            raise RuntimeError(f"创建目标频道失败：{source_title}")
        self.db.save_channel_mapping(
            source_peer_id, source_title, int(utils.get_peer_id(destination))
        )
        return destination, True

    async def _download_saved_group(
        self, messages: list[Message], source_peer_id: int, source_title: str
    ) -> list[Path]:
        safe_title = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", source_title).strip("._")
        folder = self.config.saved_media_path / f"{safe_title[:80] or 'channel'}_{abs(source_peer_id)}"
        folder.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for message in messages:
            original_name = getattr(message.file, "name", None)
            if original_name:
                clean_name = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", original_name)
                target = folder / f"{message.id}_{clean_name}"
            else:
                extension = getattr(message.file, "ext", None) or ".bin"
                target = folder / f"{message.id}{extension}"
            if not target.exists():
                downloaded = await self.client.download_media(message, file=str(target))
                if downloaded is None:
                    raise RuntimeError(f"消息 {message.id} 的媒体下载失败")
                target = Path(downloaded)
            paths.append(target)
        return paths

    async def _upload_saved_group(
        self, destination: Any, messages: list[Message], paths: list[Path]
    ) -> None:
        captions = [message.message or "" for message in messages]
        entities = [message.entities or [] for message in messages]
        await self.client.send_file(
            destination,
            [str(path) for path in paths],
            caption=captions,
            formatting_entities=entities,
            supports_streaming=True,
        )

    async def _copy_saved_group(
        self, destination: Any, messages: list[Message]
    ) -> None:
        captions = [message.message or "" for message in messages]
        entities = [message.entities or [] for message in messages]
        await self.client.send_file(
            destination,
            [message.media for message in messages],
            caption=captions,
            formatting_entities=entities,
            supports_streaming=True,
        )

    def _record_saved_skip(
        self, result: ForwardResult, message_id: int, reason: str
    ) -> None:
        result.skipped += 1
        if len(result.errors) < 20:
            result.errors.append(f"消息 {message_id}: {reason}")

    async def _handle_watched_message(self, event: events.NewMessage.Event) -> None:
        if event.chat_id is None or event.chat_id == self.owner_id:
            return
        if event.message.grouped_id is not None:
            return  # Album events forward the complete media group once.
        if int(event.chat_id) not in self.db.watched_peer_ids():
            return
        source = await self._watched_source_for_message(int(event.chat_id), event.message)
        if source is None:
            return
        result = await self._forward_many(source, [event.message])
        if result.failed or result.skipped:
            await self.client.send_message("me", f"监听源 {source} 的消息 {event.id} 未能转发。\n{result.summary()}")

    async def _handle_watched_album(self, event: events.Album.Event) -> None:
        if event.chat_id is None or event.chat_id == self.owner_id or not event.messages:
            return
        if int(event.chat_id) not in self.db.watched_peer_ids():
            return
        source = await self._watched_source_for_message(
            int(event.chat_id), event.messages[0]
        )
        if source is None:
            return
        result = await self._forward_many(source, event.messages)
        if result.failed or result.skipped:
            await self.client.send_message(
                "me",
                f"监听源 {source} 的媒体组 {event.messages[0].id} 未能完整转发。\n{result.summary()}",
            )

    async def _watched_source_for_message(
        self, chat_id: int, message: Message
    ) -> str | None:
        match = self.db.find_watch_for_peer(chat_id)
        if match is None:
            return None
        watch, is_linked_discussion = match
        if not is_linked_discussion:
            return watch.source
        if watch.mode != "comments" or not await self._is_channel_comment(
            message, watch.peer_id
        ):
            return None
        return f"{watch.source}#comments"

    async def _is_channel_comment(self, message: Message, channel_peer_id: int) -> bool:
        reply = message.reply_to
        if reply is None:
            return False
        root_id = reply.reply_to_top_id or reply.reply_to_msg_id
        if root_id is None or message.chat_id is None:
            return False
        cache_key = (int(message.chat_id), int(root_id), channel_peer_id)
        if cache_key in self.valid_comment_roots:
            return True
        root = await self.client.get_messages(message.chat_id, ids=root_id)
        if root is None or root.fwd_from is None or root.fwd_from.from_id is None:
            return False
        if int(utils.get_peer_id(root.fwd_from.from_id)) != channel_peer_id:
            return False
        self.valid_comment_roots.add(cache_key)
        return True

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
            index = 0
            processed_in_batch = 0
            while index < len(items):
                message = items[index]
                if message is None:
                    message_id = ids[index] if index < len(ids) else 0
                    self._record_skip(result, source, message_id, "消息不存在、已删除或无权访问")
                    group_size = 1
                    index += 1
                else:
                    group = [message]
                    index += 1
                    if message.grouped_id is not None:
                        while index < len(items):
                            next_message = items[index]
                            if (
                                next_message is None
                                or next_message.grouped_id != message.grouped_id
                            ):
                                break
                            group.append(next_message)
                            index += 1
                    await self._forward_group(source, group, result)
                    group_size = len(group)
                processed_in_batch += group_size
                if index < len(items):
                    if processed_in_batch >= BATCH_SIZE:
                        await asyncio.sleep(random.uniform(2.0, 5.0))
                        processed_in_batch = 0
                    else:
                        await asyncio.sleep(random.uniform(0.25, 0.6))
        return result

    async def _forward_group(
        self, source: str, messages: list[Message], result: ForwardResult
    ) -> None:
        for attempt in range(3):
            try:
                payload: Message | list[Message] = messages[0] if len(messages) == 1 else messages
                await self.client.forward_messages("me", payload)
                result.success += len(messages)
                for message in messages:
                    self.db.log_forward(source, message.id, "success")
                self.db.set_state("last_forward_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
                return
            except FloodWaitError as exc:
                wait_seconds = int(exc.seconds) + 1
                LOGGER.warning("FloodWait for %d seconds", wait_seconds)
                self._remember_error(exc)
                if attempt == 2:
                    for message in messages:
                        self._record_failure(result, source, message.id, exc)
                    return
                await asyncio.sleep(wait_seconds)
            except (ChatForwardsRestrictedError, MessageIdInvalidError, ChannelPrivateError, ChatAdminRequiredError) as exc:
                for message in messages:
                    self._record_skip(result, source, message.id, self._error_text(exc))
                return
            except RPCError as exc:
                for message in messages:
                    self._record_failure(result, source, message.id, exc)
                return
            except Exception as exc:
                LOGGER.exception(
                    "Unexpected forwarding failure for %s/%s",
                    source,
                    ",".join(str(message.id) for message in messages),
                )
                for message in messages:
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
