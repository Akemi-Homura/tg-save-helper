from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import shutil
import time
from urllib.parse import parse_qs, urlparse
from pathlib import Path
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
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
from telethon.tl.patched import MessageService
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.functions.messages import StartBotRequest
from telethon.tl.types import (
    BotCommand,
    BotCommandScopeDefault,
    Channel,
    DocumentAttributeVideo,
    MessageEntityTextUrl,
    MessageEntityUrl,
    PeerChannel,
)

from .commands import Command, CommandError, HELP_TEXT, parse_command
from .config import Config
from .db import Database
from .panel import PanelServer


LOGGER = logging.getLogger(__name__)
logging.getLogger("telethon.client.messageparse").setLevel(logging.ERROR)
CURRENT_COMMAND_CONTEXT: ContextVar[str | None] = ContextVar(
    "current_command_context", default=None
)
SAVED_SUMMARY_CHANNEL_TITLE = "收藏媒体汇总"
SAVED_BACKUP_TITLE = "我的收藏_完整备份"
RESOURCE_RECHECK_STATE_KEY = "watchresource_recheck_ranges"
RESOURCE_RECHECK_DONE_STATE_KEY = "watchresource_recheck_completed"
WATCH_FORWARD_STATE_KEY = "watch_forward_ranges"
WATCH_FORWARD_MIGRATION_KEY = "watch_forward_link_migration_v1"
MAX_STREAM_VIDEO_BYTES = 5 * 1024**3
UNKNOWN_SAVED_SOURCE_PEER_ID = 0
UNKNOWN_SAVED_SOURCE_TITLE = "收藏媒体_未知来源"
UNKNOWN_SAVED_SOURCE_TOKENS = {"unknown", "unknown-source", "未知", "未知来源"}
RESOURCE_ONE_LOOKAHEAD = 50
RESOURCE_BOT_PAYLOAD_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
RESOURCE_NEXT_BUTTON_RE = re.compile(r"下一页|next|➡|→|▶", re.IGNORECASE)
RESOURCE_PREV_BUTTON_RE = re.compile(r"上一页|prev|⬅|←|◀", re.IGNORECASE)
RESOURCE_ALL_BUTTON_RE = re.compile(r"全部")
CODE_RETRY_RE = re.compile(r"请求频繁|等待\s*\d+\s*秒")
CODE_PAGE_RE = re.compile(r"第\s*(\d+)\s*/\s*(\d+)")
RESOURCE_BOT_IDLE_SECONDS = 4.0
RESOURCE_COMMENT_READ_TIMEOUT_SECONDS = 30
FORWARD_REQUEST_TIMEOUT_SECONDS = 60
WATCHCOMMENTS_RECHECK_DELAYS = (60, 180, 420)
WATCHRESOURCE_RECHECK_DELAYS = (60, 180, 420)
WATCHRESOURCE_BUSY_RECHECK_DELAY_SECONDS = 60
WATCHRESOURCE_SWEEP_INTERVAL_SECONDS = 600
WATCHRESOURCE_SWEEP_GROUPS = 5
SAVED_WATCH_SWEEP_INTERVAL_SECONDS = 60
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


@dataclass(frozen=True)
class ResourceBotLink:
    bot_username: str
    payload: str
    url: str
    source: str
    source_message_id: int
    entry_message_id: int | None = None


@dataclass(frozen=True)
class ResourceBotOutcome:
    status: str
    text: str
    collected: int = 0
    forwarded: int = 0
    failed: int = 0
    skipped: int = 0


class StartupResumeEvent:
    id = 0
    client = None

    def __init__(self, helper: "TelegramSaveHelper") -> None:
        self.helper = helper

    async def respond(self, text: str, reply_to: int | None = None) -> None:
        if not await self.helper._notify_control_bot(text):
            await self.helper.client.send_message("me", text)


class TelegramSaveHelper:
    def __init__(self, config: Config, database: Database) -> None:
        self.config = config
        self.db = database
        self.client = TelegramClient(config.session_name, config.api_id, config.api_hash)
        self.bot_client: TelegramClient | None = None
        self.owner_id = 0
        self.bot_owner_id = config.bot_owner_id
        self.forward_lock = asyncio.Lock()
        self.forward_rate_lock = asyncio.Lock()
        self.watch_forward_semaphore = asyncio.Semaphore(1)
        self.next_forward_at: float = 0.0
        self.saved_sync_lock = asyncio.Lock()
        self.saved_backup_lock = asyncio.Lock()
        self.saved_stream_lock = asyncio.Lock()
        self.resource_start_lock = asyncio.Lock()
        self.resource_start_blocked_until: float = 0.0
        self.valid_comment_roots: set[tuple[int, int, int]] = set()
        self.handled_album_keys: set[tuple[int, int]] = set()
        self.pending_comment_rechecks: set[tuple[str, int]] = set()
        self.pending_resource_rechecks: dict[str, list[tuple[int, int]]] = {}
        self.completed_resource_rechecks: dict[str, list[tuple[int, int]]] = {}
        self.resource_recheck_tasks: dict[str, asyncio.Task[Any]] = {}
        self.pending_watch_forwards: dict[str, tuple[int, int]] = {}
        self.watch_forward_tasks: dict[str, asyncio.Task[Any]] = {}
        self.active_resource_watch_sources: set[str] = set()
        self.active_command_tasks: dict[asyncio.Task[Any], str] = {}
        self.active_pending_commands: dict[asyncio.Task[Any], str] = {}
        self.task_status: dict[asyncio.Task[Any], dict[str, Any]] = {}
        self.panel_server: PanelServer | None = None
        self.watchresource_sweep_task: asyncio.Task[Any] | None = None
        self.saved_watch_sweep_task: asyncio.Task[Any] | None = None
        self.saved_generated_message_ids: set[int] = set()
        self.saved_event_groups: set[int] = set()

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
        if self.bot_owner_id is None:
            self.bot_owner_id = self.owner_id

        self.client.add_event_handler(self._handle_control_message, events.NewMessage(outgoing=True))
        self.client.add_event_handler(self._handle_saved_message, events.NewMessage())
        self.client.add_event_handler(self._handle_watched_message, events.NewMessage())
        self.client.add_event_handler(self._handle_watched_album, events.Album())
        if self.config.bot_token:
            self.bot_client = TelegramClient(
                f"{self.config.session_name}_bot",
                self.config.api_id,
                self.config.api_hash,
            )
            await self.bot_client.start(bot_token=self.config.bot_token)
            self.bot_client.add_event_handler(
                self._handle_bot_control_message, events.NewMessage(incoming=True)
            )
            await self._set_bot_commands()
            bot_me = await self.bot_client.get_me()
            LOGGER.info(
                "Control bot @%s started for owner %s",
                getattr(bot_me, "username", None),
                self.bot_owner_id,
            )

        LOGGER.info(
            "Logged in as user %s; loaded %d watched sources",
            self.owner_id,
            len(self.db.list_watches()),
        )
        self._restore_forward_gate()
        self.panel_server = PanelServer(self)
        self.panel_server.start()
        if self.config.panel_enabled:
            LOGGER.info(
                "Panel listening on %s:%s%s",
                self.config.panel_host,
                self.config.panel_port,
                self.config.panel_base_path,
            )
        self._restore_resource_start_gate()
        self._resume_resource_rechecks()
        self._migrate_pending_watch_links()
        self._resume_watch_forward_ranges()
        self.watchresource_sweep_task = asyncio.create_task(self._watchresource_sweep_loop())
        self.saved_watch_sweep_task = asyncio.create_task(self._saved_watch_sweep_loop())
        await self._resume_pending_manual_commands()
        await self._resume_saved_watches()
        try:
            await self.client.run_until_disconnected()
        finally:
            if self.watchresource_sweep_task is not None:
                self.watchresource_sweep_task.cancel()
            if self.saved_watch_sweep_task is not None:
                self.saved_watch_sweep_task.cancel()
            if self.panel_server is not None:
                self.panel_server.stop()
            if self.bot_client is not None:
                await self.bot_client.disconnect()
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

    async def _handle_bot_control_message(self, event: events.NewMessage.Event) -> None:
        if event.sender_id != self.bot_owner_id:
            await self._reply(event, "无权使用这个控制 Bot。")
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
        except Exception as exc:  # Keep the bot control loop alive too.
            LOGGER.exception("Bot command failed")
            self._remember_error(exc)
            await self._reply(event, f"执行失败：{self._error_text(exc)}")

    async def _set_bot_commands(self) -> None:
        if self.bot_client is None:
            return
        commands = [
            ("help", "显示帮助"),
            ("status", "运行状态"),
            ("tasks", "任务进度"),
            ("stop", "停止当前命令"),
            ("last", "转发最近/未读"),
            ("unread", "转发未读消息"),
            ("between", "按消息 ID 范围转发"),
            ("link", "转发消息链接"),
            ("resource", "扫描资源链接"),
            ("resourcebot", "管理资源机器人白名单"),
            ("resourcelink", "处理单个资源链接"),
            ("watchresource", "监听资源链接"),
            ("unwatchresource", "取消资源监听"),
            ("watch", "监听频道"),
            ("unwatch", "取消频道监听"),
            ("watchcomments", "监听评论区"),
            ("unwatchcomments", "取消评论区监听"),
            ("lastcomments", "转发最近主帖及评论"),
            ("unreadcomments", "转发未读主帖及评论"),
            ("code", "处理提取码"),
            ("watchcode", "监听提取码"),
            ("unwatchcode", "取消提取码监听"),
            ("mixed", "混合转发"),
            ("stats", "转发统计"),
            ("listwatch", "监听列表"),
            ("streamsaved", "转换收藏视频"),
            ("watchstreamsaved", "监听收藏视频"),
            ("unwatchstreamsaved", "停止监听收藏视频"),
            ("syncsaved", "完整备份收藏"),
            ("syncsaved_download", "下载并上传收藏媒体"),
            ("watchsaved", "监听收藏备份"),
            ("unwatchsaved", "停止监听收藏备份"),
            ("messageid", "查看收藏消息 ID"),
        ]
        try:
            await self.bot_client(
                SetBotCommandsRequest(
                    scope=BotCommandScopeDefault(),
                    lang_code="",
                    commands=[
                        BotCommand(command=command, description=description)
                        for command, description in commands
                    ],
                )
            )
        except Exception:
            LOGGER.exception("Failed to set bot command list")

    async def _execute_command(self, command: Command, event: events.NewMessage.Event) -> None:
        if command.name == "/stop":
            await self._stop_active_commands(event)
            return
        should_log_to_chat = self._command_should_log_to_chat(command)
        current_task = asyncio.current_task()
        command_text = self._command_invocation_text(command)
        if current_task is not None:
            self.active_command_tasks[current_task] = command_text
            self.active_pending_commands[current_task] = command_text
        if self._command_should_persist(command):
            self.db.add_pending_manual_command(command_text)
        context_token = CURRENT_COMMAND_CONTEXT.set(command_text)
        started_at = time.monotonic()
        completed = False
        try:
            LOGGER.info("manual command start: %s args=%s", command.name, command.args)
            if should_log_to_chat:
                await self._reply(event, self._command_start_text(command))
            await self._run_command_body(command, event)
            LOGGER.info("manual command done: %s args=%s", command.name, command.args)
            completed = True
            if should_log_to_chat:
                elapsed = time.monotonic() - started_at
                await self._reply(event, f"执行完成：{command.name}，耗时 {elapsed:.1f}s")
        except asyncio.CancelledError:
            LOGGER.info("manual command cancelled: %s args=%s", command.name, command.args)
            if should_log_to_chat:
                elapsed = time.monotonic() - started_at
                await self._reply(event, f"已停止：{command.name}，已运行 {elapsed:.1f}s")
        except Exception as exc:
            LOGGER.exception("Manual command failed: %s args=%s", command.name, command.args)
            self._remember_error(exc)
            completed = True
            if should_log_to_chat:
                elapsed = time.monotonic() - started_at
                await self._reply(
                    event,
                    f"执行失败：{command.name}，耗时 {elapsed:.1f}s\n{self._error_text(exc)}",
                )
            else:
                await self._reply(event, f"执行失败：{self._error_text(exc)}")
        finally:
            if completed and self._command_should_persist(command):
                pending_text = (
                    self.active_pending_commands.get(current_task, command_text)
                    if current_task is not None
                    else command_text
                )
                self.db.remove_pending_manual_command(pending_text)
            CURRENT_COMMAND_CONTEXT.reset(context_token)
            if current_task is not None:
                self.active_command_tasks.pop(current_task, None)
                self.active_pending_commands.pop(current_task, None)
                self.task_status.pop(current_task, None)

    async def _run_command_body(
        self, command: Command, event: events.NewMessage.Event
    ) -> None:
        if command.name == "/help":
            await self._reply(event, HELP_TEXT)
        elif command.name == "/last":
            source, limit, start_message_id, force, unread = self._last_args(command.args)
            if self._has_unread_arg(command.args):
                await self._forward_unread(
                    event,
                    source,
                    limit,
                    force,
                    start_message_id=start_message_id,
                )
            else:
                await self._forward_last(
                    event,
                    source,
                    limit,
                    force,
                    start_message_id=start_message_id,
                )
        elif command.name == "/unread":
            source, limit, start_message_id, force, _ = self._last_args(command.args)
            await self._forward_unread(
                event,
                source,
                limit,
                force,
                start_message_id=start_message_id,
            )
        elif command.name == "/between":
            await self._forward_between(
                event,
                command.args[0],
                int(command.args[1]),
                int(command.args[2]),
                self._has_force_arg(command.args),
            )
        elif command.name == "/link":
            await self._forward_link(event, command.args[0], self._has_force_arg(command.args))
        elif command.name == "/watch":
            await self._watch(event, command.args[0])
            if self._has_backfill_arg(command.args):
                source, limit, start_message_id, force, unread = self._last_args(
                    command.args
                )
                if unread:
                    await self._forward_unread(
                        event,
                        source,
                        limit,
                        force,
                        start_message_id=start_message_id,
                    )
                else:
                    await self._forward_last(
                        event,
                        source,
                        limit,
                        force,
                        start_message_id=start_message_id,
                        checkpoint_command="/watch",
                    )
        elif command.name == "/unwatch":
            await self._unwatch(event, command.args[0])
        elif command.name == "/watchcomments":
            await self._watch_comments(event, command.args[0])
            if self._has_backfill_arg(command.args):
                source, limit, start_message_id, force, unread = self._last_args(
                    command.args
                )
                if unread:
                    await self._forward_unread_comments(
                        event,
                        source,
                        limit,
                        force,
                        start_message_id=start_message_id,
                    )
                else:
                    await self._forward_last_comments(
                        event,
                        source,
                        limit,
                        force,
                        start_message_id=start_message_id,
                    )
        elif command.name == "/unwatchcomments":
            await self._unwatch_comments(event, command.args[0])
        elif command.name == "/watchresource":
            await self._watch_resource(event, command.args[0])
            if self._has_backfill_arg(command.args):
                source, limit, start_message_id, force, one = self._resource_scan_args(
                    command.args
                )
                await self._process_resource_scan(
                    event,
                    source,
                    limit,
                    force,
                    start_message_id=start_message_id,
                    unread=self._has_unread_arg(command.args),
                    one=one,
                )
        elif command.name == "/unwatchresource":
            await self._unwatch_resource(event, command.args[0])
        elif command.name == "/code":
            source, extract_channel, limit, start_message_id, force, unread = self._code_args(
                command.args
            )
            await self._process_code_scan(
                event,
                source,
                extract_channel,
                limit,
                force,
                start_message_id=start_message_id,
                unread=unread,
            )
        elif command.name == "/watchcode":
            await self._watch_code(event, command.args[0], command.args[1])
            if self._watchcode_has_backfill_arg(command.args):
                source, extract_channel, limit, start_message_id, force, unread = self._code_args(
                    command.args
                )
                await self._process_code_scan(
                    event,
                    source,
                    extract_channel,
                    limit,
                    force,
                    start_message_id=start_message_id,
                    unread=unread,
                )
        elif command.name == "/unwatchcode":
            await self._unwatch_code(event, command.args[0])
        elif command.name == "/lastcomments":
            source, limit, start_message_id, force, unread = self._last_args(command.args)
            if self._has_unread_arg(command.args):
                await self._forward_unread_comments(
                    event,
                    source,
                    limit,
                    force,
                    start_message_id=start_message_id,
                )
            else:
                await self._forward_last_comments(
                    event,
                    source,
                    limit,
                    force,
                    start_message_id=start_message_id,
                )
        elif command.name == "/unreadcomments":
            source, limit, start_message_id, force, _ = self._last_args(command.args)
            await self._forward_unread_comments(
                event,
                source,
                limit,
                force,
                start_message_id=start_message_id,
            )
        elif command.name == "/resourcebot":
            await self._resource_bot_command(event, command.args)
        elif command.name == "/resourcelink":
            await self._process_resource_link_command(
                event, command.args[0], self._has_force_arg(command.args)
            )
        elif command.name == "/resource":
            source, limit, start_message_id, force, one = self._resource_scan_args(command.args)
            await self._process_resource_scan(
                event,
                source,
                limit,
                force,
                start_message_id=start_message_id,
                unread=self._has_unread_arg(command.args),
                one=one,
            )
        elif command.name == "/mixed":
            source, limit, start_message_id, force, _ = self._last_args(command.args)
            await self._process_mixed_forward(
                event, source, limit, force, start_message_id=start_message_id
            )
        elif command.name == "/listwatch":
            await self._list_watches(event)
        elif command.name == "/status":
            await self._status(event)
        elif command.name == "/tasks":
            await self._tasks(event)
        elif command.name == "/stats":
            await self._stats(event, command.args[0] if command.args else "day")
        elif command.name in {"/syncsaved", "/watchsaved"}:
            selector, start_message_id, force = await self._saved_selector(event, command.args)
            if command.name == "/watchsaved":
                self.db.set_saved_watch("backup", True, self._command_invocation_text(command))
            await self._run_saved_history(
                event, "backup", selector, start_message_id, force,
                watch=command.name == "/watchsaved",
            )
        elif command.name in {"/streamsaved", "/watchstreamsaved"}:
            selector, start_message_id, force = await self._saved_selector(event, command.args)
            if command.name == "/watchstreamsaved":
                self.db.set_saved_watch("stream", True, self._command_invocation_text(command))
            await self._run_saved_history(
                event, "stream", selector, start_message_id, force,
                watch=command.name == "/watchstreamsaved",
            )
        elif command.name == "/unwatchsaved":
            self.db.set_saved_watch("backup", False, command.name)
            await self._reply(event, "已停止监听收藏完整备份。")
        elif command.name == "/unwatchstreamsaved":
            self.db.set_saved_watch("stream", False, command.name)
            await self._reply(event, "已停止监听收藏视频转换。")
        elif command.name == "/messageid":
            reply = await event.get_reply_message()
            if reply is None or getattr(reply, "chat_id", self.owner_id) != self.owner_id:
                raise CommandError("请在“我的收藏”中回复目标消息后发送 /messageid。")
            await self._reply(event, f"收藏消息 ID：{int(reply.id)}")
        elif command.name == "/syncsaved-download":
            await self._sync_saved_media(
                event,
                self._saved_sync_limit(command.args[0]),
                source_filter=await self._saved_source_filter(command.args),
                download_upload=True,
            )

    @staticmethod
    def _command_invocation_text(command: Command) -> str:
        args = " ".join(command.args)
        return f"{command.name}{(' ' + args) if args else ''}"

    @staticmethod
    def _command_start_text(command: Command) -> str:
        return f"开始执行：{TelegramSaveHelper._command_invocation_text(command)}"

    @staticmethod
    def _command_should_log_to_chat(command: Command) -> bool:
        return command.name not in {"/help", "/status"}

    @staticmethod
    def _command_should_persist(command: Command) -> bool:
        return command.name not in {"/help", "/status", "/stop"}

    def _checkpoint_pending_command(self, command_text: str) -> None:
        task = asyncio.current_task()
        if task is None:
            return
        old = self.active_pending_commands.get(task)
        if old is None or old == command_text:
            return
        self.db.replace_pending_manual_command(old, command_text)
        self.active_pending_commands[task] = command_text

    async def _run_recoverable_text(
        self, command_text: str, awaitable: Any, dedupe_prefix: str | None = None
    ) -> Any:
        persist = not (
            dedupe_prefix
            and any(
                item.startswith(dedupe_prefix)
                for item in self.db.pending_manual_commands()
                if item != command_text
            )
        )
        if persist:
            self.db.add_pending_manual_command(command_text)
        task = asyncio.current_task()
        if task is not None:
            self.active_command_tasks[task] = command_text
            if persist:
                self.active_pending_commands[task] = command_text
        context_token = CURRENT_COMMAND_CONTEXT.set(command_text)
        completed = False
        try:
            result = await awaitable
            completed = True
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            completed = True
            raise
        finally:
            CURRENT_COMMAND_CONTEXT.reset(context_token)
            if completed and persist:
                self.db.remove_pending_manual_command(command_text)
            if task is not None:
                self.active_command_tasks.pop(task, None)
                self.active_pending_commands.pop(task, None)
                self.task_status.pop(task, None)

    def _set_task_status(self, **items: Any) -> None:
        task = asyncio.current_task()
        if task is None:
            return
        status = self.task_status.setdefault(task, {})
        status.update(items)
        status["updated_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    def _increment_state_counter(self, key: str, amount: int = 1) -> None:
        if amount <= 0:
            return
        try:
            current = int(self.db.get_state(key, "0") or "0")
        except ValueError:
            current = 0
        self.db.set_state(key, str(current + amount))

    def _record_watch_summary(
        self, mode: str, *, success: int = 0, failed: int = 0, skipped: int = 0
    ) -> None:
        prefix = f"watch_summary_{mode}"
        self._increment_state_counter(f"{prefix}_success", success)
        self._increment_state_counter(f"{prefix}_failed", failed)
        self._increment_state_counter(f"{prefix}_skipped", skipped)

    async def _resume_pending_manual_commands(self) -> None:
        pending = self.db.pending_manual_commands()
        if not pending:
            return
        await self._sleep_until_saved_floodwait()
        pending = self.db.pending_manual_commands()
        if not pending:
            return
        await self._notify_control_bot(
            "检测到重启前未完成的手动命令，准备恢复：\n"
            + "\n".join(f"- {item}" for item in pending)
        )
        for text in pending:
            try:
                command = parse_command(text)
            except CommandError as exc:
                self.db.remove_pending_manual_command(text)
                await self._notify_control_bot(f"恢复命令失败，已移除：{text}\n{exc}")
                continue
            if command is not None:
                asyncio.create_task(self._resume_command(command))

    async def _resume_command(self, command: Command) -> None:
        event = StartupResumeEvent(self)
        if command.name == "/link":
            async with self.watch_forward_semaphore:
                await self._execute_command(command, event)
            return
        await self._execute_command(command, event)

    async def _resume_saved_watches(self) -> None:
        pending = self.db.pending_manual_commands()
        for mode, command_name in (("backup", "/watchsaved"), ("stream", "/watchstreamsaved")):
            watch = self.db.saved_watch(mode)
            if watch is None or not bool(watch["enabled"]):
                continue
            if any(item.startswith(command_name + " ") for item in pending):
                continue
            last_message_id = watch["last_message_id"]
            command_text = (
                f"{command_name} from {int(last_message_id) + 1}"
                if last_message_id is not None else f"{command_name} all"
            )
            command = parse_command(command_text)
            if command is not None:
                asyncio.create_task(self._execute_command(command, StartupResumeEvent(self)))

    async def _stop_active_commands(self, event: events.NewMessage.Event) -> None:
        current_task = asyncio.current_task()
        targets = [
            (task, description)
            for task, description in list(self.active_command_tasks.items())
            if task is not current_task and not task.done()
        ]
        if not targets:
            await self._reply(event, "当前没有正在执行的手动命令。")
            return
        for task, description in targets:
            self.db.remove_pending_manual_command(
                self.active_pending_commands.get(task, description)
            )
            task.cancel()
        await self._reply(
            event,
            "已发送停止请求：\n" + "\n".join(f"- {description}" for _, description in targets),
        )

    @staticmethod
    def _has_force_arg(args: tuple[str, ...]) -> bool:
        return any(arg.lower() == "force" for arg in args)

    @staticmethod
    def _has_unread_arg(args: tuple[str, ...]) -> bool:
        return any(arg.lower() == "unread" for arg in args)

    @staticmethod
    def _strip_tail_flags(args: tuple[str, ...], flags: set[str]) -> tuple[str, ...]:
        core = list(args)
        while core and core[-1].lower() in flags:
            core.pop()
        return tuple(core)

    @staticmethod
    def _last_limit(args: tuple[str, ...]) -> int | None:
        core = TelegramSaveHelper._strip_tail_flags(args, {"force"})
        if len(core) < 2 or core[1].lower() == "all":
            return None
        return int(core[1])

    @staticmethod
    def _last_args(args: tuple[str, ...]) -> tuple[str, int | None, int | None, bool, bool]:
        force = TelegramSaveHelper._has_force_arg(args)
        core = TelegramSaveHelper._strip_tail_flags(args, {"force"})
        selector = core[1:]
        lowered = [item.lower() for item in selector]
        start_message_id: int | None = None
        unread = False
        if lowered and lowered[0] == "from":
            match = LINK_RE.fullmatch(selector[1].strip())
            if match is None:
                raise CommandError("from 后面必须是具体消息链接，例如 https://t.me/xxx/123")
            start_message_id = int(match.group("message_id"))
        elif lowered and lowered[0] == "unread":
            unread = True
        source = core[0]
        count_arg = selector[0] if selector and lowered[0] != "from" else "all"
        limit = None if count_arg.lower() in {"all", "unread"} else int(count_arg)
        return source, limit, start_message_id, force, unread

    @classmethod
    def _has_backfill_arg(cls, args: tuple[str, ...]) -> bool:
        return len(args) > 1 and not (len(args) == 2 and cls._has_force_arg(args))

    @classmethod
    def _watchcode_has_backfill_arg(cls, args: tuple[str, ...]) -> bool:
        tail = args[2:]
        return bool(tail) and not (len(tail) == 1 and cls._has_force_arg(tail))

    @staticmethod
    def _resource_scan_args(
        args: tuple[str, ...]
    ) -> tuple[str, int | None, int | None, bool, bool]:
        force = TelegramSaveHelper._has_force_arg(args)
        core = TelegramSaveHelper._strip_tail_flags(args, {"force"})
        selector = core[1:]
        lowered = [item.lower() for item in selector]
        start_message_id: int | None = None
        one = False
        if lowered and lowered[0] == "from":
            link = selector[1]
            match = LINK_RE.fullmatch(link.strip())
            if match is None:
                raise CommandError("from 后面必须是具体消息链接，例如 https://t.me/xxxpxe/4734")
            start_message_id = int(match.group("message_id"))
        elif lowered and lowered[0] == "one":
            one = True
            link = selector[2]
            match = LINK_RE.fullmatch(link.strip())
            if match is None:
                raise CommandError("from 后面必须是具体消息链接，例如 https://t.me/xxxpxe/4734")
            start_message_id = int(match.group("message_id"))
        count_arg = selector[0] if selector and lowered[0] not in {"from", "one"} else "all"
        source = core[0].strip()
        if not source:
            raise CommandError("用法：/resource <source> <count|all|unread|from <message_link>|one from <message_link>> [force]")
        if one and start_message_id is None:
            raise CommandError("one 必须配合 from 使用，例如 /resource <source> one from https://t.me/x/123")
        limit = None if count_arg.lower() in {"all", "unread", "one"} else int(count_arg)
        return source, limit, start_message_id, force, one

    @staticmethod
    def _code_args(
        args: tuple[str, ...]
    ) -> tuple[str, str, int | None, int | None, bool, bool]:
        force = TelegramSaveHelper._has_force_arg(args)
        core = TelegramSaveHelper._strip_tail_flags(args, {"force"})
        selector = core[2:]
        lowered = [item.lower() for item in selector]
        start_message_id: int | None = None
        unread = False
        if lowered and lowered[0] == "from":
            match = LINK_RE.fullmatch(selector[1].strip())
            if match is None:
                raise CommandError("from 后面必须是具体消息链接，例如 https://t.me/xxx/123")
            start_message_id = int(match.group("message_id"))
        elif lowered and lowered[0] == "unread":
            unread = True
        if len(core) < 2:
            raise CommandError(
                "用法：/code <source> <extract_channel> <count|all|unread|from <message_link>> [force]"
            )
        source, extract_channel = core[0], core[1]
        count_arg = selector[0] if selector and lowered[0] != "from" else "all"
        limit = None if count_arg.lower() in {"all", "unread"} else int(count_arg)
        return source, extract_channel, limit, start_message_id, force, unread

    async def _forward_last(
        self,
        event: events.NewMessage.Event,
        source: str,
        count: int | None,
        force: bool = False,
        start_message_id: int | None = None,
        checkpoint_command: str = "/last",
    ) -> None:
        entity = await self._resolve_source(source)
        if count is None:
            await self._forward_last_stream(
                event,
                entity,
                source,
                start_message_id or 1,
                force=force,
                checkpoint_command=checkpoint_command,
            )
            return
        await self._reply(
            event,
            f"开始读取 {source} 的{'全部' if count is None else count} 个逻辑帖子…",
        )
        async def progress(groups_seen: int, messages_seen: int) -> None:
            await self._reply(
                event,
                f"扫描进度：已读取逻辑帖子 {groups_seen} 个，消息 {messages_seen} 条。",
            )

        if start_message_id is None:
            groups = await self._recent_message_groups(
                entity, count, progress_callback=progress if count is None else None
            )
        else:
            groups = await self._message_groups_from(entity, start_message_id, count)
        await self._reply(
            event,
            f"扫描完成：逻辑帖子 {len(groups)} 个，开始转发。",
        )
        result = ForwardResult()
        for group in groups:
            self._checkpoint_from_command(
                checkpoint_command, source, int(group[0].id), count, force
            )
            item = await self._forward_many(source, group, force=force)
            self._merge_forward_result(result, item)
            await self._pause_after_forward(item)
        await self._reply(event, result.summary() + f"\n逻辑帖子 {len(groups)} 个。")

    async def _forward_last_stream(
        self,
        event: events.NewMessage.Event,
        entity: Any,
        source: str,
        start_message_id: int,
        *,
        force: bool,
        checkpoint_command: str = "/last",
    ) -> None:
        await self._reply(
            event,
            f"开始边扫描边转发 {source}，起点 {self._message_reference(source, start_message_id)}。",
        )
        result = ForwardResult()
        processed = 0
        async for group in self._iter_message_groups_from(entity, start_message_id):
            processed += 1
            current_id = int(group[0].id)
            next_id = max(int(message.id) for message in group) + 1
            self._checkpoint_from_command(
                checkpoint_command, source, current_id, None, force
            )
            item = await self._forward_many(source, group, force=force)
            self._merge_forward_result(result, item)
            await self._pause_after_forward(item)
            self._set_task_status(
                state="边扫描边转发",
                current=self._message_reference(source, current_id),
                processed=processed,
                total="未知",
                success=result.success,
                failed=result.failed,
                skipped=result.skipped,
            )
            if processed % 100 == 0:
                await self._reply(
                    event,
                    f"转发进度：已处理逻辑帖子 {processed} 个；"
                    f"成功 {result.success}，失败 {result.failed}，跳过 {result.skipped}。",
                )
            self._checkpoint_from_command(
                checkpoint_command, source, next_id, None, force
            )
        await self._reply(event, result.summary() + f"\n逻辑帖子 {processed} 个。")

    async def _forward_unread(
        self,
        event: events.NewMessage.Event,
        source: str,
        limit: int | None,
        force: bool = False,
        start_message_id: int | None = None,
    ) -> None:
        entity = await self._resolve_source(source)
        if start_message_id is not None:
            groups = await self._message_groups_from(entity, start_message_id, limit)
            result = ForwardResult()
            for group in groups:
                self._checkpoint_from_command("/unread", source, int(group[0].id), limit, force)
                item = await self._forward_many(source, group, force=force)
                self._merge_forward_result(result, item)
                await self._pause_after_forward(item)
            await self._reply(event, result.summary() + f"\n从断点恢复 {len(groups)} 个逻辑帖子。")
            return
        unread_count = await self._dialog_unread_count(entity)
        if unread_count <= 0:
            await self._reply(event, f"当前没有未读消息：{source}")
            return

        selected_count = unread_count if limit is None else min(limit, unread_count)
        messages = await self._recent_messages(entity, selected_count)
        result = ForwardResult()
        for message in messages:
            self._checkpoint_from_command("/unread", source, int(message.id), limit, force)
            item = await self._forward_many(source, [message], force=force)
            self._merge_forward_result(result, item)
            await self._pause_after_forward(item)
        if selected_count >= unread_count:
            await self.client.send_read_acknowledge(entity)
            read_note = "已标记为已读。"
        else:
            read_note = "仅处理部分未读，未自动标记已读。"
        await self._reply(
            event,
            result.summary()
            + f"\n未读 {unread_count} 条；本次读取 {selected_count} 条。{read_note}",
        )

    async def _forward_between(
        self,
        event: events.NewMessage.Event,
        source: str,
        start_id: int,
        end_id: int,
        force: bool = False,
    ) -> None:
        entity = await self._resolve_source(source)
        ids = list(range(start_id, end_id + 1))
        messages = await self.client.get_messages(entity, ids=ids)
        result = await self._forward_many(source, messages, expected_ids=ids, force=force)
        await self._reply(event, result.summary())

    async def _forward_link(
        self, event: events.NewMessage.Event, link: str, force: bool = False
    ) -> None:
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
        result = await self._forward_many(
            source, [message], expected_ids=[message_id], force=force
        )
        await self._reply(event, result.summary())

    async def _resource_bot_command(
        self, event: events.NewMessage.Event, args: tuple[str, ...]
    ) -> None:
        action = args[0].lower()
        if action == "list":
            env_bots = sorted(self.config.resource_bots)
            db_bots = self.db.list_resource_bots()
            text = "资源机器人白名单\n"
            text += "- 配置文件：\n"
            text += "\n".join(f"  {item}" for item in env_bots) if env_bots else "  无"
            text += "\n- 数据库：\n"
            text += "\n".join(f"  {item}" for item in db_bots) if db_bots else "  无"
            await self._reply(event, text)
            return

        username = self._normalize_bot_username(args[1])
        if action == "add":
            self.db.add_resource_bot(username)
            await self._reply(event, f"已加入资源机器人白名单：{username}")
        elif action == "remove":
            removed = self.db.remove_resource_bot(username)
            await self._reply(
                event,
                f"已移除资源机器人：{username}" if removed else f"数据库白名单中没有：{username}",
            )

    async def _process_resource_link_command(
        self, event: events.NewMessage.Event, link: str, force: bool = False
    ) -> None:
        parsed = self._parse_resource_bot_url(link, "manual", 0)
        if parsed is None:
            raise CommandError("不是支持的资源机器人 deep link。请使用 https://t.me/<bot>?start=<payload>")
        result = await self._process_resource_bot_link(parsed, force=force)
        await self._reply(event, result.text)

    async def _process_resource_scan(
        self,
        event: events.NewMessage.Event,
        source: str,
        count: int | None,
        force: bool = False,
        start_message_id: int | None = None,
        unread: bool = False,
        one: bool = False,
    ) -> None:
        entity = await self._resolve_source(source)
        self._set_task_status(
            state="扫描中",
            command=CURRENT_COMMAND_CONTEXT.get(),
            source=source,
            current=self._message_reference(source, start_message_id) if start_message_id else source,
            processed=0,
            total="未知" if count is None else count,
            success=0,
            failed=0,
            skipped=0,
            duplicate=0,
        )
        processed_all_unread = False
        if one:
            if start_message_id is None:
                raise CommandError("one 必须配合 from 使用。")
            groups = await self._resource_one_groups(entity, start_message_id)
        elif count is None and not unread:
            await self._process_resource_stream(
                event, entity, source, start_message_id or 1, force=force
            )
            return
        elif start_message_id is None:
            if unread:
                unread_count = await self._dialog_unread_count(entity)
                if unread_count <= 0:
                    await self._reply(event, f"当前没有未读消息：{source}")
                    return
                selected_count = unread_count if count is None else min(count, unread_count)
                processed_all_unread = selected_count >= unread_count
                groups = await self._recent_message_groups(entity, selected_count)
            else:
                groups = await self._recent_message_groups(entity, count)
        else:
            groups = await self._message_groups_from(entity, start_message_id, count)
        (
            grouped_links,
            ignored_links,
            scan_duplicate_links,
            direct_resource_count,
            reply_resource_count,
            missing_reply_count,
        ) = await self._resource_link_groups(entity, source, groups)
        link_count = sum(len(links) for _, links in grouped_links)
        await self._reply(
            event,
            f"资源扫描：逻辑帖子 {len(groups)} 个，"
            + (
                f"起点 {self._message_reference(source, start_message_id)}，"
                if start_message_id is not None
                else ""
            )
            + f"直接资源原帖 {direct_resource_count} 条，"
            f"回复资源原帖 {reply_resource_count} 条，"
            f"识别到白名单资源链接 {link_count} 个，"
            f"非白名单资源链接 {len(ignored_links)} 个，"
            f"扫描内重复 {scan_duplicate_links} 个，"
            f"回复原帖缺失 {missing_reply_count} 条。"
            + (" force：会强制重拉已处理链接。" if force else ""),
        )
        if ignored_links:
            ignored_by_bot: dict[str, ResourceBotLink] = {}
            ignored_counts: dict[str, int] = {}
            for link in ignored_links:
                ignored_counts[link.bot_username] = ignored_counts.get(link.bot_username, 0) + 1
                ignored_by_bot.setdefault(link.bot_username, link)
            bot_items = sorted(ignored_by_bot.items())
            preview = "\n".join(
                f"- @{bot}（{ignored_counts[bot]} 个）：{link.url}"
                for bot, link in bot_items[:20]
            )
            extra = (
                f"\n……另有 {len(bot_items) - 20} 个 bot 未显示。"
                if len(bot_items) > 20
                else ""
            )
            await self._reply(
                event,
                f"非白名单资源 bot（链接去重后 {len(ignored_links)} 个，bot {len(bot_items)} 个）：\n"
                f"{preview}{extra}",
            )
        original_forwarded = original_failed = original_skipped = 0
        success = skipped = failed = forwarded = collected = duplicate_done = 0
        errors: list[str] = []
        link_index = 0
        for group_index, (group, group_links) in enumerate(grouped_links, 1):
            self._set_task_status(
                state="处理中",
                current=self._message_reference(source, int(group[0].id)),
                processed=group_index,
                total=len(grouped_links),
                success=success,
                failed=failed,
                skipped=skipped,
                duplicate=duplicate_done,
            )
            self._checkpoint_resource_command(
                source,
                int(group[0].id),
                count,
                force=force,
                unread=unread,
                one=one,
            )
            forwardable_group = self._resource_forwardable_originals(group)
            dropped_plain = len(group) - len(forwardable_group)
            group_already_forwarded = (
                not force
                and bool(forwardable_group)
                and all(
                    self.db.forward_was_successful(source, int(message.id))
                    for message in forwardable_group
                )
            )
            original_group = [] if group_already_forwarded else forwardable_group
            already_forwarded = len(forwardable_group) - len(original_group)
            if original_group:
                original_result = await self._forward_many(source, original_group, force=force)
                original_forwarded += original_result.success
                original_failed += original_result.failed
                original_skipped += original_result.skipped
                await self._pause_after_forward(original_result)
            original_skipped += already_forwarded
            for link in group_links:
                link_index += 1
                self._set_task_status(
                    state="处理资源链接",
                    current=f"{self._message_reference(link.source, link.source_message_id)} @{link.bot_username} {link.payload}",
                    processed=link_index,
                    total=link_count,
                )
                outcome = await self._process_resource_bot_link(link, force=force)
                if outcome.status == "duplicate":
                    duplicate_done += 1
                elif outcome.status == "skipped":
                    skipped += 1
                elif outcome.status == "failed":
                    failed += 1
                    errors.append(outcome.text)
                else:
                    success += 1
                collected += outcome.collected
                forwarded += outcome.forwarded
        text = (
            f"资源处理完成：原帖成功 {original_forwarded}，"
            f"原帖失败 {original_failed}，原帖跳过 {original_skipped}；"
            f"链接 {link_count}，成功 {success}，重复 {duplicate_done}，"
            f"跳过 {skipped}，失败 {failed}；扫描内重复 {scan_duplicate_links}；"
            f"收集媒体 {collected} 条，转发媒体 {forwarded} 条。"
        )
        if errors:
            text += "\n最近错误：\n" + "\n".join(f"- {item}" for item in errors[-5:])
        await self._reply(event, text)
        if unread and start_message_id is None and processed_all_unread:
            await self.client.send_read_acknowledge(entity)

    async def _process_resource_stream(
        self,
        event: events.NewMessage.Event,
        entity: Any,
        source: str,
        start_message_id: int,
        *,
        force: bool,
    ) -> None:
        scanned = original_forwarded = original_failed = original_skipped = 0
        success = skipped = failed = forwarded = collected = duplicate_done = 0
        ignored_count = scan_duplicate_links = 0
        errors: list[str] = []
        seen_links: set[tuple[str, str]] = set()
        async for group in self._iter_message_groups_from(entity, start_message_id):
            scanned += 1
            current_id = int(group[0].id)
            next_id = max(int(message.id) for message in group) + 1
            self._checkpoint_resource_command(
                source, current_id, None, force=force, unread=False, one=False
            )
            self._set_task_status(
                state="扫描/处理中",
                current=self._message_reference(source, current_id),
                processed=scanned,
                total="未知",
                success=success,
                failed=failed,
                skipped=skipped,
                duplicate=duplicate_done,
            )
            grouped_links, ignored, *_ = await self._resource_link_groups(entity, source, [group])
            ignored_count += len(ignored)
            if not grouped_links:
                self._checkpoint_resource_command(
                    source, next_id, None, force=force, unread=False, one=False
                )
                continue
            for original_group, links in grouped_links:
                link_total = len(links)
                links = [
                    link
                    for link in links
                    if (link.bot_username, link.payload) not in seen_links
                ]
                scan_duplicate_links += link_total - len(links)
                seen_links.update((link.bot_username, link.payload) for link in links)
                if not links:
                    continue
                forwardable_group = self._resource_forwardable_originals(original_group)
                group_already_forwarded = (
                    not force
                    and bool(forwardable_group)
                    and all(
                        self.db.forward_was_successful(source, int(message.id))
                        for message in forwardable_group
                    )
                )
                original_group_to_forward = [] if group_already_forwarded else forwardable_group
                original_skipped += len(forwardable_group) - len(original_group_to_forward)
                if original_group_to_forward:
                    result = await self._forward_many(
                        source, original_group_to_forward, force=force
                    )
                    original_forwarded += result.success
                    original_failed += result.failed
                    original_skipped += result.skipped
                    await self._pause_after_forward(result)
                for link in links:
                    self._set_task_status(
                        state="处理资源链接",
                        current=f"{self._message_reference(link.source, link.source_message_id)} @{link.bot_username} {link.payload}",
                        processed=scanned,
                        total="未知",
                        success=success,
                        failed=failed,
                        skipped=skipped,
                        duplicate=duplicate_done,
                    )
                    outcome = await self._process_resource_bot_link(link, force=force)
                    if outcome.status == "duplicate":
                        duplicate_done += 1
                    elif outcome.status == "skipped":
                        skipped += 1
                    elif outcome.status == "failed":
                        failed += 1
                        errors.append(outcome.text)
                    else:
                        success += 1
                    collected += outcome.collected
                    forwarded += outcome.forwarded
                    self._set_task_status(
                        success=success,
                        failed=failed,
                        skipped=skipped,
                        duplicate=duplicate_done,
                    )
            self._checkpoint_resource_command(
                source, next_id, None, force=force, unread=False, one=False
            )
        text = (
            f"资源处理完成：扫描逻辑帖子 {scanned} 个；"
            f"原帖成功 {original_forwarded}，失败 {original_failed}，跳过 {original_skipped}；"
            f"链接成功 {success}，重复 {duplicate_done}，跳过 {skipped}，失败 {failed}；"
            f"非白名单 {ignored_count}，扫描内重复 {scan_duplicate_links}；"
            f"收集媒体 {collected} 条，转发媒体 {forwarded} 条。"
        )
        if errors:
            text += "\n最近错误：\n" + "\n".join(f"- {item}" for item in errors[-5:])
        await self._reply(event, text)

    def _checkpoint_resource_command(
        self,
        source: str,
        message_id: int,
        count: int | None,
        *,
        force: bool,
        unread: bool,
        one: bool,
    ) -> None:
        link = self._message_link(source, message_id)
        if link is None:
            return
        suffix = " force" if force else ""
        if one:
            command_text = f"/resource {source} one from {link}{suffix}"
        else:
            command_text = f"/resource {source} from {link}{suffix}"
        self._checkpoint_pending_command(
            command_text
        )

    def _checkpoint_from_command(
        self, command: str, source: str, message_id: int, count: int | None, force: bool
    ) -> None:
        link = self._message_link(source, message_id)
        if link is None:
            return
        suffix = " force" if force else ""
        self._checkpoint_pending_command(
            f"{command} {source} from {link}{suffix}"
        )

    async def _process_mixed_forward(
        self,
        event: events.NewMessage.Event,
        source: str,
        count: int | None,
        force: bool = False,
        start_message_id: int | None = None,
    ) -> None:
        entity = await self._resolve_source(source)
        channel_peer_id: int | None = None
        try:
            await self._get_linked_discussion(entity)
            channel_peer_id = int(utils.get_peer_id(entity))
        except CommandError:
            channel_peer_id = None
        if count is None:
            await self._process_mixed_stream(
                event, entity, source, channel_peer_id, start_message_id or 1, force=force
            )
            return

        groups = (
            await self._message_groups_from(entity, start_message_id, count)
            if start_message_id is not None
            else await self._recent_message_groups(entity, count)
        )

        await self._reply(
            event,
            f"混合扫描：逻辑帖子 {len(groups)} 个。优先级：resource > lastcomments > last。"
            + (" force：会强制重转。" if force else ""),
        )

        mode_counts = {"resource": 0, "lastcomments": 0, "last": 0}
        forward_total = ForwardResult()
        resource_success = resource_duplicate = resource_failed = resource_skipped = 0
        resource_collected = resource_forwarded = ignored_resource_links = 0
        errors: list[str] = []

        for index, group in enumerate(groups, 1):
            grouped_links, ignored, *_ = await self._resource_link_groups(
                entity, source, [group]
            )
            links = grouped_links[0][1] if grouped_links else []
            resource_group = (
                self._resource_forwardable_originals(grouped_links[0][0])
                if grouped_links
                else group
            )
            ignored_resource_links += len(ignored)
            if links:
                mode_counts["resource"] += 1
                await self._reply(
                    event,
                    f"混合进度：{index}/{len(groups)} 使用 resource，链接 {len(links)} 个。",
                )
                result = await self._forward_many(source, resource_group, force=force)
                self._merge_forward_result(forward_total, result)
                await self._pause_after_forward(result)
                for link in links:
                    outcome = await self._process_resource_bot_link(link, force=force)
                    if outcome.status == "duplicate":
                        resource_duplicate += 1
                    elif outcome.status == "failed":
                        resource_failed += 1
                        errors.append(outcome.text)
                    elif outcome.status == "skipped":
                        resource_skipped += 1
                    else:
                        resource_success += 1
                    resource_collected += outcome.collected
                    resource_forwarded += outcome.forwarded
                continue

            if channel_peer_id is not None:
                messages, comment_count = await self._post_groups_with_comments(
                    entity, channel_peer_id, [group]
                )
                if comment_count > 0:
                    mode_counts["lastcomments"] += 1
                    await self._reply(
                        event,
                        f"混合进度：{index}/{len(groups)} 使用 lastcomments，评论 {comment_count} 条。",
                    )
                    result = await self._forward_many(
                        f"{source}#with-comments", messages, force=force
                    )
                    self._merge_forward_result(forward_total, result)
                    await self._pause_after_forward(result)
                    continue

            mode_counts["last"] += 1
            await self._reply(event, f"混合进度：{index}/{len(groups)} 使用 last。")
            result = await self._forward_many(source, group, force=force)
            self._merge_forward_result(forward_total, result)
            await self._pause_after_forward(result)

        text = (
            "混合转发完成\n"
            f"- resource 原帖：{mode_counts['resource']} 个\n"
            f"- lastcomments 原帖：{mode_counts['lastcomments']} 个\n"
            f"- last 原帖：{mode_counts['last']} 个\n"
            f"- 普通转发：成功 {forward_total.success}，失败 {forward_total.failed}，跳过 {forward_total.skipped}\n"
            f"- 资源链接：成功 {resource_success}，重复 {resource_duplicate}，"
            f"失败 {resource_failed}，跳过 {resource_skipped}\n"
            f"- 资源媒体：收集 {resource_collected} 条，转发 {resource_forwarded} 条\n"
            f"- 非白名单资源链接：{ignored_resource_links} 个"
        )
        if errors:
            text += "\n最近错误：\n" + "\n".join(f"- {item}" for item in errors[-5:])
        await self._reply(event, text)

    async def _process_mixed_stream(
        self,
        event: events.NewMessage.Event,
        entity: Any,
        source: str,
        channel_peer_id: int | None,
        start_message_id: int,
        *,
        force: bool,
    ) -> None:
        await self._reply(
            event,
            f"开始边扫描边混合转发 {source}。优先级：resource > lastcomments > last。"
            + (" force：会强制重转。" if force else ""),
        )
        mode_counts = {"resource": 0, "lastcomments": 0, "last": 0}
        forward_total = ForwardResult()
        resource_success = resource_duplicate = resource_failed = resource_skipped = 0
        resource_collected = resource_forwarded = ignored_resource_links = 0
        errors: list[str] = []
        processed = 0
        async for group in self._iter_message_groups_from(entity, start_message_id):
            processed += 1
            current_id = int(group[0].id)
            next_id = max(int(message.id) for message in group) + 1
            self._checkpoint_from_command("/mixed", source, current_id, None, force)
            grouped_links, ignored, *_ = await self._resource_link_groups(
                entity, source, [group]
            )
            links = grouped_links[0][1] if grouped_links else []
            resource_group = (
                self._resource_forwardable_originals(grouped_links[0][0])
                if grouped_links
                else group
            )
            ignored_resource_links += len(ignored)
            if links:
                mode_counts["resource"] += 1
                result = await self._forward_many(source, resource_group, force=force)
                self._merge_forward_result(forward_total, result)
                await self._pause_after_forward(result)
                for link in links:
                    outcome = await self._process_resource_bot_link(link, force=force)
                    if outcome.status == "duplicate":
                        resource_duplicate += 1
                    elif outcome.status == "failed":
                        resource_failed += 1
                        errors.append(outcome.text)
                    elif outcome.status == "skipped":
                        resource_skipped += 1
                    else:
                        resource_success += 1
                    resource_collected += outcome.collected
                    resource_forwarded += outcome.forwarded
            elif channel_peer_id is not None:
                messages, comment_count = await self._post_groups_with_comments(
                    entity, channel_peer_id, [group]
                )
                if comment_count > 0:
                    mode_counts["lastcomments"] += 1
                    result = await self._forward_many(
                        f"{source}#with-comments", messages, force=force
                    )
                    self._merge_forward_result(forward_total, result)
                    await self._pause_after_forward(result)
                else:
                    mode_counts["last"] += 1
                    result = await self._forward_many(source, group, force=force)
                    self._merge_forward_result(forward_total, result)
                    await self._pause_after_forward(result)
            else:
                mode_counts["last"] += 1
                result = await self._forward_many(source, group, force=force)
                self._merge_forward_result(forward_total, result)
                await self._pause_after_forward(result)
            if processed % 50 == 0:
                await self._reply(
                    event,
                    f"混合进度：已处理 {processed} 个；"
                    f"resource {mode_counts['resource']}，"
                    f"lastcomments {mode_counts['lastcomments']}，last {mode_counts['last']}。",
                )
            self._checkpoint_from_command("/mixed", source, next_id, None, force)
        text = (
            "混合转发完成\n"
            f"- 逻辑帖子：{processed} 个\n"
            f"- resource 原帖：{mode_counts['resource']} 个\n"
            f"- lastcomments 原帖：{mode_counts['lastcomments']} 个\n"
            f"- last 原帖：{mode_counts['last']} 个\n"
            f"- 普通转发：成功 {forward_total.success}，失败 {forward_total.failed}，跳过 {forward_total.skipped}\n"
            f"- 资源链接：成功 {resource_success}，重复 {resource_duplicate}，"
            f"失败 {resource_failed}，跳过 {resource_skipped}\n"
            f"- 资源媒体：收集 {resource_collected} 条，转发 {resource_forwarded} 条\n"
            f"- 非白名单资源链接：{ignored_resource_links} 个"
        )
        if errors:
            text += "\n最近错误：\n" + "\n".join(f"- {item}" for item in errors[-5:])
        await self._reply(event, text)

    async def _resource_link_groups(
        self, entity: Any, source: str, groups: list[list[Message]]
    ) -> tuple[
        list[tuple[list[Message], list[ResourceBotLink]]],
        list[ResourceBotLink],
        int,
        int,
        int,
        int,
    ]:
        grouped: dict[tuple[int, ...], tuple[list[Message], list[ResourceBotLink]]] = {}
        order: list[tuple[int, ...]] = []
        group_by_message_id = {
            int(message.id): group for group in groups for message in group
        }
        ignored: list[ResourceBotLink] = []
        seen: set[tuple[str, str]] = set()
        ignored_seen: set[tuple[str, str]] = set()
        direct_groups: set[tuple[int, ...]] = set()
        reply_groups: set[tuple[int, ...]] = set()
        duplicate_links = 0
        missing_replies = 0
        for group in groups:
            for message in group:
                message_ignored: list[ResourceBotLink] = []
                extracted = self._extract_resource_bot_links(
                    message, source, ignored_links=message_ignored
                )
                if not extracted and not message_ignored:
                    continue
                original_group = group
                is_reply_resource = False
                if message.reply_to_msg_id is not None:
                    reply_group = await self._resource_reply_group(
                        entity, message, group_by_message_id
                    )
                    if reply_group is None:
                        missing_replies += 1
                    else:
                        original_group = reply_group
                        is_reply_resource = True
                        for item in reply_group:
                            group_by_message_id[int(item.id)] = reply_group
                group_key = self._message_group_key(original_group)
                if is_reply_resource:
                    reply_groups.add(group_key)
                else:
                    direct_groups.add(group_key)
                if group_key not in grouped:
                    grouped[group_key] = (original_group, [])
                    order.append(group_key)
                original_id = int(original_group[0].id)
                entry_id = int(message.id)
                for link in extracted:
                    key = (link.bot_username, link.payload)
                    if key in seen:
                        duplicate_links += 1
                        continue
                    seen.add(key)
                    grouped[group_key][1].append(
                        replace(
                            link,
                            source_message_id=original_id,
                            entry_message_id=entry_id,
                        )
                    )
                for link in message_ignored:
                    key = (link.bot_username, link.payload)
                    if key not in ignored_seen:
                        ignored_seen.add(key)
                        ignored.append(
                            replace(
                                link,
                                source_message_id=original_id,
                                entry_message_id=entry_id,
                            )
                        )
        for original_group in groups:
            if not original_group or original_group[0].reply_to_msg_id is not None:
                continue
            for message in await self._resource_comment_messages(entity, original_group):
                message_ignored: list[ResourceBotLink] = []
                extracted = self._extract_resource_bot_links(
                    message, source, ignored_links=message_ignored
                )
                if not extracted and not message_ignored:
                    continue
                group_key = self._message_group_key(original_group)
                reply_groups.add(group_key)
                if group_key not in grouped:
                    grouped[group_key] = (original_group, [])
                    order.append(group_key)
                original_id = int(original_group[0].id)
                for link in extracted:
                    key = (link.bot_username, link.payload)
                    if key in seen:
                        duplicate_links += 1
                        continue
                    seen.add(key)
                    grouped[group_key][1].append(
                        replace(
                            link,
                            source_message_id=original_id,
                            entry_message_id=int(message.id),
                        )
                    )
                for link in message_ignored:
                    key = (link.bot_username, link.payload)
                    if key not in ignored_seen:
                        ignored_seen.add(key)
                        ignored.append(
                            replace(
                                link,
                                source_message_id=original_id,
                                entry_message_id=int(message.id),
                            )
                        )
        return (
            [grouped[key] for key in order if grouped[key][1]],
            ignored,
            duplicate_links,
            len(direct_groups),
            len(reply_groups),
            missing_replies,
        )

    async def _resource_comment_messages(
        self, entity: Any, original_group: list[Message]
    ) -> list[Message]:
        for post in original_group:
            while True:
                try:
                    comments = await asyncio.wait_for(
                        self._resource_comments_for_post(entity, int(post.id)),
                        RESOURCE_COMMENT_READ_TIMEOUT_SECONDS,
                    )
                    comments.reverse()
                    return comments
                except TimeoutError:
                    LOGGER.warning(
                        "resource comment read timed out; retrying: post=%s",
                        int(post.id),
                    )
                    await asyncio.sleep(2)
                except FloodWaitError as exc:
                    await self._sleep_for_flood_wait(
                        f"读取资源评论区：主帖 {int(post.id)}", exc
                    )
                except MsgIdInvalidError:
                    break
        return []

    async def _resource_comments_for_post(
        self, entity: Any, post_id: int
    ) -> list[Message]:
        return [
            message
            async for message in self.client.iter_messages(
                entity, reply_to=post_id, limit=None
            )
        ]

    async def _resource_reply_group(
        self,
        entity: Any,
        message: Message,
        group_by_message_id: dict[int, list[Message]],
    ) -> list[Message] | None:
        reply_id = int(message.reply_to_msg_id or 0)
        if reply_id <= 0:
            return None
        reply = await self._resource_reply_target(entity, message, group_by_message_id)
        if reply is None:
            return None
        if reply.grouped_id is not None:
            return await self._nearby_grouped_messages(reply)
        return [reply]

    async def _resource_reply_target(
        self,
        entity: Any,
        message: Message,
        group_by_message_id: dict[int, list[Message]],
    ) -> Message | None:
        reply_id = int(message.reply_to_msg_id or 0)
        current: Message | None = None
        for _ in range(2):
            if reply_id <= 0:
                return current
            group = group_by_message_id.get(reply_id)
            current = group[0] if group else None
            if current is None:
                current = await self.client.get_messages(entity, ids=reply_id)
            if current is None:
                return None
            # 资源入口经常是 C->提取码纯文字 B->真实原帖 A；跳过 B。
            if current.file is not None or not current.reply_to_msg_id or self._message_urls(current):
                return current
            reply_id = int(current.reply_to_msg_id or 0)
        return current

    @staticmethod
    def _message_group_key(group: list[Message]) -> tuple[int, ...]:
        return tuple(int(message.id) for message in group)

    def _resource_forwardable_originals(self, group: list[Message]) -> list[Message]:
        return [
            message
            for message in group
            if message.file is not None or self._message_urls(message)
        ]

    @staticmethod
    def _merge_forward_result(target: ForwardResult, item: ForwardResult) -> None:
        target.success += item.success
        target.failed += item.failed
        target.skipped += item.skipped
        target.errors.extend(item.errors)

    async def _pause_after_forward(self, item: ForwardResult) -> None:
        if item.success <= 0 and item.failed <= 0:
            return
        await asyncio.sleep(
            random.uniform(
                self.config.forward_interval_min_seconds,
                self.config.forward_interval_max_seconds,
            )
        )

    async def _wait_for_forward_slot(self, *, batch: bool = False) -> None:
        """Serialize Telegram forwarding requests and reserve a quiet period.

        The limit belongs to the user account, not to an individual command.  A
        shared gate prevents a historical backup and a manual forwarding task
        from issuing ForwardMessagesRequest calls at the same time.
        """
        async with self.forward_rate_lock:
            delay = self.next_forward_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if batch:
                minimum = self.config.forward_batch_pause_min_seconds
                maximum = self.config.forward_batch_pause_max_seconds
            else:
                minimum = self.config.forward_interval_min_seconds
                maximum = self.config.forward_interval_max_seconds
            self.next_forward_at = time.monotonic() + random.uniform(minimum, maximum)

    async def _process_resource_bot_link(
        self, link: ResourceBotLink, force: bool = False
    ) -> ResourceBotOutcome:
        if link.bot_username not in self._resource_bot_whitelist():
            LOGGER.info(
                "resource link rejected: bot_not_whitelisted bot=%s payload=%s source=%s message=%s url=%s",
                link.bot_username,
                link.payload,
                link.source,
                link.source_message_id,
                link.url,
            )
            return ResourceBotOutcome(
                "failed", f"资源链接失败：@{link.bot_username} 不在白名单。"
            )
        existing = self.db.get_resource_link(link.bot_username, link.payload)
        if not force and existing is not None and existing["status"] == "done":
            return ResourceBotOutcome(
                "duplicate", f"重复资源：@{link.bot_username} {link.payload} 已处理。"
            )

        self.db.upsert_resource_link(
            link.bot_username, link.payload, link.source, link.source_message_id, "processing"
        )
        while True:
            try:
                bot = await self.client.get_entity(link.bot_username)
                before = await self.client.get_messages(bot, limit=1)
                before_id = int(before[0].id) if before else 0
                await self._start_resource_bot(
                    bot,
                    link.payload,
                    f"资源链接：{self._message_reference(link.source, link.source_message_id)} "
                    f"@{link.bot_username} {link.payload}",
                )
                await asyncio.sleep(1.5)
                started_messages = [
                    message
                    async for message in self.client.iter_messages(
                        bot, min_id=before_id, reverse=True
                    )
                ]
                first_messages = await self._wait_resource_bot_messages(bot, before_id)
                if not first_messages:
                    raise RuntimeError(
                        f"资源机器人 @{link.bot_username} 未在 "
                        f"{self.config.max_resource_bot_wait_seconds} 秒内回复"
                    )
                start_message_id = self._resource_start_message_id(started_messages)
                first_response_id = self._resource_first_response_id(first_messages)
                last_response_id = self._resource_last_response_id(first_messages)
                self.db.upsert_resource_link(
                    link.bot_username,
                    link.payload,
                    link.source,
                    link.source_message_id,
                    "processing",
                    start_message_id=start_message_id,
                    first_response_id=first_response_id,
                    last_response_id=last_response_id,
                )
                collect_after_id = before_id
                if await self._click_resource_all_button(first_messages):
                    LOGGER.info(
                        "resource bot clicked all: bot=%s payload=%s",
                        link.bot_username,
                        link.payload,
                    )
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                media_messages = await self._collect_resource_bot_media(
                    bot, collect_after_id
                )
                last_response_id = max(
                    (
                        int(message.id)
                        for message in [*first_messages, *media_messages]
                        if not message.out
                    ),
                    default=last_response_id,
                )
                result = await self._forward_many(
                    f"resource:@{link.bot_username}:{link.payload}", media_messages
                )
                self.db.upsert_resource_link(
                    link.bot_username,
                    link.payload,
                    link.source,
                    link.source_message_id,
                    "done",
                    start_message_id=start_message_id,
                    first_response_id=first_response_id,
                    last_response_id=last_response_id,
                    collected_count=len(media_messages),
                    forwarded_count=result.success,
                )
                text = (
                    f"资源链接完成：@{link.bot_username} {link.payload}；"
                    f"收集媒体 {len(media_messages)} 条，转发媒体 {result.success} 条，"
                    f"失败 {result.failed}，跳过 {result.skipped}。"
                )
                return ResourceBotOutcome(
                    "success",
                    text,
                    collected=len(media_messages),
                    forwarded=result.success,
                    failed=result.failed,
                    skipped=result.skipped,
                )
            except FloodWaitError as exc:
                error = self._error_text(exc)
                self.db.upsert_resource_link(
                    link.bot_username,
                    link.payload,
                    link.source,
                    link.source_message_id,
                    "processing",
                    error,
                )
                self._remember_error(exc)
            except Exception as exc:
                error = self._error_text(exc)
                self.db.upsert_resource_link(
                    link.bot_username,
                    link.payload,
                    link.source,
                    link.source_message_id,
                    "failed",
                    error,
                )
                return ResourceBotOutcome(
                    "failed", f"资源链接失败：@{link.bot_username} {link.payload}: {error}"
                )

    @staticmethod
    def _resource_start_message_id(messages: list[Message]) -> int | None:
        ids = [
            int(message.id)
            for message in messages
            if message.out and (message.raw_text or "").startswith("/start")
        ]
        return max(ids) if ids else None

    @staticmethod
    def _resource_first_response_id(messages: list[Message]) -> int | None:
        ids = [int(message.id) for message in messages if not message.out]
        return min(ids) if ids else None

    @staticmethod
    def _resource_last_response_id(messages: list[Message]) -> int | None:
        ids = [int(message.id) for message in messages if not message.out]
        return max(ids) if ids else None

    async def _start_resource_bot(self, bot: Any, payload: str, context: str) -> None:
        while True:
            async with self.resource_start_lock:
                delay = self.resource_start_blocked_until - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    await self.client(
                        StartBotRequest(bot=bot, peer=bot, start_param=payload)
                    )
                    self.db.set_state("resource_start_blocked_until", "")
                    self.resource_start_blocked_until = (
                        time.monotonic()
                        + self.config.resource_bot_start_interval_seconds
                    )
                    return
                except FloodWaitError as exc:
                    wait_seconds = int(exc.seconds) + 1
                    resume_at = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
                    self.resource_start_blocked_until = max(
                        self.resource_start_blocked_until,
                        time.monotonic() + wait_seconds,
                    )
                    self.db.set_state(
                        "resource_start_blocked_until",
                        resume_at.isoformat(timespec="seconds"),
                    )
                    self._remember_error(exc)
                    await self._notify_flood_wait(context, exc, will_retry=True)
                    await asyncio.sleep(wait_seconds)

    def _restore_resource_start_gate(self) -> None:
        value = self.db.get_state("resource_start_blocked_until", "")
        if not value:
            return
        try:
            resume_at = datetime.fromisoformat(value)
        except ValueError:
            return
        delay = (resume_at - datetime.now(timezone.utc)).total_seconds()
        if delay > 0:
            self.resource_start_blocked_until = time.monotonic() + delay

    def _restore_forward_gate(self) -> None:
        value = self.db.get_state("telegram_floodwait_until", "")
        if not value:
            return
        try:
            resume_at = datetime.fromisoformat(value)
        except ValueError:
            return
        delay = (resume_at - datetime.now(timezone.utc)).total_seconds()
        if delay > 0:
            self.next_forward_at = max(
                self.next_forward_at, time.monotonic() + delay
            )

    async def _sleep_until_saved_floodwait(self) -> None:
        value = self.db.get_state("telegram_floodwait_until", "")
        if not value:
            return
        try:
            resume_at = datetime.fromisoformat(value)
        except ValueError:
            return
        delay = (resume_at - datetime.now(timezone.utc)).total_seconds()
        if delay <= 0:
            self.db.set_state("telegram_floodwait_until", "")
            return
        pending = self.db.pending_manual_commands()
        preview = "\n".join(f"- {item}" for item in pending[:10])
        extra = f"\n……另有 {len(pending) - 10} 条未显示。" if len(pending) > 10 else ""
        pending_text = (
            f"\n待恢复任务（{len(pending)} 条）：\n{preview}{extra}"
            if pending
            else "\n待恢复任务：无。"
        )
        await self._notify_control_bot(
            "检测到重启前 Telegram 限流未解除，"
            f"将等待到 {resume_at.astimezone().isoformat(timespec='seconds')} 后恢复任务。"
            f"{pending_text}"
        )
        await asyncio.sleep(delay)
        self.db.set_state("telegram_floodwait_until", "")

    async def _process_code_scan(
        self,
        event: events.NewMessage.Event,
        source: str,
        extract_channel: str,
        count: int | None,
        force: bool = False,
        start_message_id: int | None = None,
        unread: bool = False,
    ) -> None:
        entity = await self._resolve_source(source)
        extract_entity = await self._resolve_source(extract_channel)
        processed_all_unread = False
        if count is None and not unread:
            await self._process_code_stream(
                event,
                entity,
                source,
                extract_channel,
                extract_entity,
                start_message_id or 1,
                force=force,
            )
            return
        if start_message_id is not None:
            groups = await self._message_groups_from(entity, start_message_id, count)
        elif unread:
            unread_count = await self._dialog_unread_count(entity)
            if unread_count <= 0:
                await self._reply(event, f"当前没有未读消息：{source}")
                return
            selected_count = unread_count if count is None else min(count, unread_count)
            processed_all_unread = selected_count >= unread_count
            groups = await self._recent_message_groups(entity, selected_count)
        else:
            groups = await self._recent_message_groups(entity, count)
        await self._reply(
            event,
            f"提取码扫描：逻辑帖子 {len(groups)} 个；提取频道 {extract_channel}。",
        )
        original = ForwardResult()
        resource = ForwardResult()
        for index, group in enumerate(groups, 1):
            code_message = group[0]
            self._checkpoint_code_command(
                source,
                extract_channel,
                int(code_message.id),
                count,
                force=force,
                unread=unread,
            )
            await self._reply(
                event,
                f"提取码进度：{index}/{len(groups)} "
                f"{self._message_reference(source, int(code_message.id))}",
            )
            item = await self._forward_many(source, [code_message], force=force)
            self._merge_forward_result(original, item)
            await self._pause_after_forward(item)
            resources = await self._extract_code_resources(
                source, code_message, extract_entity
            )
            item = await self._forward_many(
                f"code:{source}:{code_message.id}", resources, force=force
            )
            self._merge_forward_result(resource, item)
            await self._pause_after_forward(item)
        if unread and start_message_id is None and processed_all_unread:
            await self.client.send_read_acknowledge(entity)
        await self._reply(
            event,
            "提取码处理完成："
            f"原消息成功 {original.success}，失败 {original.failed}，跳过 {original.skipped}；"
            f"资源成功 {resource.success}，失败 {resource.failed}，跳过 {resource.skipped}。"
        )

    async def _process_code_stream(
        self,
        event: events.NewMessage.Event,
        entity: Any,
        source: str,
        extract_channel: str,
        extract_entity: Any,
        start_message_id: int,
        *,
        force: bool,
    ) -> None:
        await self._reply(
            event,
            f"开始边扫描边处理提取码 {source}；提取频道 {extract_channel}；"
            f"起点 {self._message_reference(source, start_message_id)}。",
        )
        original = ForwardResult()
        resource = ForwardResult()
        processed = 0
        async for group in self._iter_message_groups_from(entity, start_message_id):
            processed += 1
            code_message = group[0]
            next_id = max(int(message.id) for message in group) + 1
            self._checkpoint_code_command(
                source,
                extract_channel,
                int(code_message.id),
                None,
                force=force,
                unread=False,
            )
            item = await self._forward_many(source, [code_message], force=force)
            self._merge_forward_result(original, item)
            await self._pause_after_forward(item)
            resources = await self._extract_code_resources(
                source, code_message, extract_entity
            )
            item = await self._forward_many(
                f"code:{source}:{code_message.id}", resources, force=force
            )
            self._merge_forward_result(resource, item)
            await self._pause_after_forward(item)
            if processed % 50 == 0:
                await self._reply(
                    event,
                    f"提取码进度：已处理 {processed} 个；"
                    f"原消息成功 {original.success}，资源成功 {resource.success}。",
                )
            self._checkpoint_code_command(
                source, extract_channel, next_id, None, force=force, unread=False
            )
        await self._reply(
            event,
            "提取码处理完成："
            f"逻辑帖子 {processed} 个；"
            f"原消息成功 {original.success}，失败 {original.failed}，跳过 {original.skipped}；"
            f"资源成功 {resource.success}，失败 {resource.failed}，跳过 {resource.skipped}。"
        )

    def _checkpoint_code_command(
        self,
        source: str,
        extract_channel: str,
        message_id: int,
        count: int | None,
        *,
        force: bool,
        unread: bool,
    ) -> None:
        link = self._message_link(source, message_id)
        if link is None:
            return
        suffix = " force" if force else ""
        self._checkpoint_pending_command(
            f"/code {source} {extract_channel} from {link}{suffix}"
        )

    async def _extract_code_resources(
        self, source: str, code_message: Message, extract_entity: Any
    ) -> list[Message]:
        before = await self.client.get_messages(extract_entity, limit=1)
        before_id = int(before[0].id) if before else 0
        await self._forward_code_to_extract(source, code_message, extract_entity)
        await asyncio.sleep(1.5)
        return await self._collect_code_resources(extract_entity, before_id)

    async def _forward_code_to_extract(
        self, source: str, code_message: Message, extract_entity: Any
    ) -> None:
        while True:
            try:
                await self.client.forward_messages(extract_entity, code_message)
                return
            except FloodWaitError as exc:
                await self._sleep_for_flood_wait(
                    f"转发提取码到提取频道：{self._message_reference(source, int(code_message.id))}",
                    exc,
                )

    async def _collect_code_resources(self, entity: Any, after_id: int) -> list[Message]:
        resources: list[Message] = []
        seen_ids: set[int] = set()
        last_seen_id = after_id
        page_messages: list[Message] = []
        for _ in range(self.config.max_resource_bot_pages):
            new_messages = await self._wait_code_messages(entity, last_seen_id)
            if not new_messages:
                break
            page_messages = new_messages
            last_seen_id = max(last_seen_id, *(int(message.id) for message in new_messages))
            retry_seconds = self._code_retry_seconds(new_messages)
            if retry_seconds is not None:
                await asyncio.sleep(retry_seconds)
                await self._click_code_next_page(page_messages)
                continue
            for message in new_messages:
                if (
                    message.out
                    or int(message.id) in seen_ids
                    or self._is_code_control_message(message)
                ):
                    continue
                seen_ids.add(int(message.id))
                resources.append(message)
                if len(resources) >= self.config.max_resource_bot_messages:
                    return resources
            if self._code_last_page(new_messages):
                break
            if not await self._click_code_next_page(new_messages):
                break
            await asyncio.sleep(random.uniform(1.0, 2.0))
        return resources

    async def _wait_code_messages(self, entity: Any, after_id: int) -> list[Message]:
        deadline = asyncio.get_running_loop().time() + self.config.max_resource_bot_wait_seconds
        messages: list[Message] = []
        seen_ids: set[int] = set()
        last_new_at: float | None = None
        while asyncio.get_running_loop().time() < deadline:
            current = [
                message
                async for message in self.client.iter_messages(
                    entity, min_id=after_id, reverse=True
                )
            ]
            ids = {int(message.id) for message in current}
            if ids - seen_ids:
                seen_ids = ids
                messages = current
                last_new_at = asyncio.get_running_loop().time()
            if messages and last_new_at is not None:
                if asyncio.get_running_loop().time() - last_new_at >= RESOURCE_BOT_IDLE_SECONDS:
                    return messages
            await asyncio.sleep(1)
        return messages

    @staticmethod
    def _code_retry_seconds(messages: list[Message]) -> int | None:
        for message in messages:
            text = message.raw_text or ""
            if "请求频繁" not in text:
                continue
            match = re.search(r"等待\s*(\d+)\s*秒", text)
            return int(match.group(1)) + 1 if match else 2
        return None

    @staticmethod
    def _is_code_control_message(message: Message) -> bool:
        text = (message.raw_text or "").strip()
        return bool(CODE_RETRY_RE.search(text) or CODE_PAGE_RE.fullmatch(text))

    @staticmethod
    def _code_last_page(messages: list[Message]) -> bool:
        for message in messages:
            match = CODE_PAGE_RE.search(message.raw_text or "")
            if match and match.group(1) == match.group(2):
                return True
        return False

    async def _click_code_next_page(self, messages: list[Message]) -> bool:
        current = 0
        for message in messages:
            match = CODE_PAGE_RE.search(message.raw_text or "")
            if match:
                current = max(current, int(match.group(1)))
        for message in reversed(messages):
            for row_index, row in enumerate(message.buttons or []):
                for button_index, button in enumerate(row):
                    text = (getattr(button, "text", "") or "").strip()
                    if RESOURCE_NEXT_BUTTON_RE.search(text) or text in {"▶", "➡️"}:
                        await message.click(row_index, button_index)
                        return True
                    if current and text.isdigit() and int(text) == current + 1:
                        await message.click(row_index, button_index)
                        return True
        return False

    async def _collect_resource_bot_media(self, bot: Any, after_id: int) -> list[Message]:
        media_messages: list[Message] = []
        seen_ids: set[int] = set()
        clicked_pages: set[int] = set()
        last_seen_id = after_id
        pages = 0
        while pages < self.config.max_resource_bot_pages:
            pages += 1
            LOGGER.info(
                "resource bot wait page=%s after_id=%s max_wait=%s",
                pages,
                last_seen_id,
                self.config.max_resource_bot_wait_seconds,
            )
            new_messages = await self._wait_resource_bot_messages(bot, last_seen_id)
            if not new_messages:
                LOGGER.info("resource bot page=%s no new messages", pages)
                break
            retry_seconds = self._code_retry_seconds(new_messages)
            if retry_seconds is not None:
                LOGGER.info("resource bot retry requested: seconds=%s", retry_seconds)
                await asyncio.sleep(retry_seconds)
                clicked_id = await self._click_resource_next_page(new_messages)
                if clicked_id is not None:
                    last_seen_id = min(last_seen_id, clicked_id - 1)
                continue
            for message in new_messages:
                last_seen_id = max(last_seen_id, int(message.id))
                match = CODE_PAGE_RE.search(message.raw_text or "")
                if match:
                    clicked_pages.add(int(match.group(1)))
                if message.file is not None and message.id not in seen_ids:
                    seen_ids.add(int(message.id))
                    media_messages.append(message)
                    if len(media_messages) >= self.config.max_resource_bot_messages:
                        return media_messages
            if self._resource_bot_finished(new_messages):
                LOGGER.info("resource bot finished marker found on page=%s", pages)
                break
            clicked_id = await self._click_resource_previous_page(
                new_messages, clicked_pages
            )
            if clicked_id is not None:
                last_seen_id = min(last_seen_id, clicked_id - 1)
                LOGGER.info("resource bot clicked previous page range page=%s", pages)
                await asyncio.sleep(random.uniform(1.0, 2.0))
                continue
            clicked_id = await self._click_resource_number_button(
                new_messages, clicked_pages
            )
            if clicked_id is not None:
                last_seen_id = min(last_seen_id, clicked_id - 1)
                LOGGER.info("resource bot clicked numbered item page=%s", pages)
                await asyncio.sleep(random.uniform(1.0, 2.0))
                continue
            clicked_id = await self._click_resource_next_page(new_messages)
            if clicked_id is None:
                current, total = self._resource_page_status(new_messages)
                if current is not None and total is not None and current < total:
                    LOGGER.info(
                        "resource bot page=%s waiting for navigation current=%s total=%s",
                        pages,
                        current,
                        total,
                    )
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    continue
                LOGGER.info("resource bot page=%s no next button", pages)
                break
            last_seen_id = min(last_seen_id, clicked_id - 1)
            LOGGER.info("resource bot clicked next page=%s", pages)
            await asyncio.sleep(random.uniform(2.0, 3.5))
        return media_messages

    async def _wait_resource_bot_messages(self, bot: Any, after_id: int) -> list[Message]:
        deadline = asyncio.get_running_loop().time() + self.config.max_resource_bot_wait_seconds
        idle_seconds = min(
            RESOURCE_BOT_IDLE_SECONDS,
            max(1.0, self.config.max_resource_bot_wait_seconds / 4),
        )
        messages: list[Message] = []
        seen_ids: set[int] = set()
        seen_signature: tuple[Any, ...] | None = None
        last_new_at: float | None = None
        while asyncio.get_running_loop().time() < deadline:
            current = [
                message
                async for message in self.client.iter_messages(
                    bot, min_id=after_id, reverse=True
                )
                if not message.out
            ]
            current_ids = {int(message.id) for message in current}
            current_signature = self._messages_signature(current)
            if current_ids - seen_ids or current_signature != seen_signature:
                seen_ids = current_ids
                seen_signature = current_signature
                messages = current
                last_new_at = asyncio.get_running_loop().time()
                LOGGER.info(
                    "resource bot updated %s messages after_id=%s latest=%s",
                    len(messages),
                    after_id,
                    max(current_ids) if current_ids else None,
                )
                if self._resource_bot_finished(messages):
                    return messages
            if messages and last_new_at is not None:
                if asyncio.get_running_loop().time() - last_new_at >= idle_seconds:
                    return messages
            await asyncio.sleep(1)
        return messages

    @staticmethod
    def _messages_signature(messages: list[Message]) -> tuple[Any, ...]:
        return tuple(
            (
                int(message.id),
                message.raw_text or "",
                tuple(
                    tuple((getattr(button, "text", "") or "").strip() for button in row)
                    for row in (message.buttons or [])
                ),
            )
            for message in messages
        )

    @staticmethod
    def _resource_bot_finished(messages: list[Message]) -> bool:
        for message in messages:
            text = message.raw_text or ""
            if "全部" in text and ("发送完毕" in text or "已发送完毕" in text):
                return True
            if re.search(r"(所有|全部).*(内容|资源|文件|视频|照片|图片).*(提取|发送).*完毕", text):
                return True
        return False

    @staticmethod
    def _resource_page_status(messages: list[Message]) -> tuple[int | None, int | None]:
        current: int | None = None
        total: int | None = None
        for message in messages:
            match = CODE_PAGE_RE.search(message.raw_text or "")
            if match:
                current = int(match.group(1))
                total = int(match.group(2))
        return current, total

    async def _click_resource_all_button(self, messages: list[Message]) -> bool:
        for message in reversed(messages):
            for row_index, row in enumerate(message.buttons or []):
                for button_index, button in enumerate(row):
                    text = (getattr(button, "text", "") or "").strip()
                    if RESOURCE_ALL_BUTTON_RE.search(text):
                        await message.click(row_index, button_index)
                        return True
        return False

    async def _click_resource_next_page(self, messages: list[Message]) -> int | None:
        current = 0
        for message in messages:
            match = CODE_PAGE_RE.search(message.raw_text or "")
            if match:
                current = max(current, int(match.group(1)))
        for message in reversed(messages):
            rows = message.buttons or []
            for row_index, row in enumerate(rows):
                for button_index, button in enumerate(row):
                    text = (getattr(button, "text", "") or "").strip()
                    if RESOURCE_NEXT_BUTTON_RE.search(text):
                        await message.click(row_index, button_index)
                        return int(message.id)
                    if current and text.isdigit() and int(text) == current + 1:
                        await message.click(row_index, button_index)
                        return int(message.id)
        return None

    async def _click_resource_number_button(
        self, messages: list[Message], clicked_pages: set[int]
    ) -> int | None:
        for message in reversed(messages):
            if "分页导航" not in (message.raw_text or ""):
                continue
            candidates: list[tuple[int, int, int]] = []
            for row_index, row in enumerate(message.buttons or []):
                for button_index, button in enumerate(row):
                    text = (getattr(button, "text", "") or "").strip()
                    if text.isdigit() and int(text) not in clicked_pages:
                        candidates.append((int(text), row_index, button_index))
            if candidates:
                page, row_index, button_index = min(candidates)
                clicked_pages.add(page)
                await message.click(row_index, button_index)
                return int(message.id)
        return None

    async def _click_resource_previous_page(
        self, messages: list[Message], clicked_pages: set[int]
    ) -> int | None:
        for message in reversed(messages):
            if "分页导航" not in (message.raw_text or ""):
                continue
            numbers = [
                int((getattr(button, "text", "") or "").strip())
                for row in (message.buttons or [])
                for button in row
                if (getattr(button, "text", "") or "").strip().isdigit()
            ]
            if not numbers or not any(page not in clicked_pages for page in range(1, min(numbers))):
                continue
            for row_index, row in enumerate(message.buttons or []):
                for button_index, button in enumerate(row):
                    text = (getattr(button, "text", "") or "").strip()
                    if RESOURCE_PREV_BUTTON_RE.search(text):
                        await message.click(row_index, button_index)
                        return int(message.id)
        return None

    def _extract_resource_bot_links(
        self,
        message: Message,
        source: str,
        ignored_links: list[ResourceBotLink] | None = None,
    ) -> list[ResourceBotLink]:
        links: list[ResourceBotLink] = []
        whitelist = self._resource_bot_whitelist()
        for url in self._message_urls(message):
            parsed = self._parse_resource_bot_url(url, source, int(message.id))
            if parsed is None:
                continue
            if parsed.bot_username in whitelist:
                links.append(parsed)
            else:
                LOGGER.info(
                    "resource link ignored: bot_not_whitelisted bot=%s payload=%s source=%s message=%s url=%s",
                    parsed.bot_username,
                    parsed.payload,
                    source,
                    message.id,
                    parsed.url,
                )
                if ignored_links is not None:
                    ignored_links.append(parsed)
        return links

    @staticmethod
    def _message_urls(message: Message) -> list[str]:
        urls: list[str] = []
        text = message.raw_text or ""
        for entity in message.entities or []:
            if isinstance(entity, MessageEntityTextUrl):
                urls.append(entity.url)
            elif isinstance(entity, MessageEntityUrl):
                urls.append(text[entity.offset : entity.offset + entity.length])
        urls.extend(match.group(0) for match in re.finditer(r"https?://\S+", text))
        for row in message.buttons or []:
            for button in row:
                url = getattr(button, "url", None) or getattr(
                    getattr(button, "button", None), "url", None
                )
                if url:
                    urls.append(str(url))
        return urls

    def _parse_resource_bot_url(
        self, url: str, source: str, source_message_id: int
    ) -> ResourceBotLink | None:
        cleaned_url = self._clean_url(url)
        parsed = urlparse(cleaned_url)
        if parsed.scheme not in {"http", "https"}:
            return None
        if parsed.netloc.lower() not in {"t.me", "telegram.me", "www.t.me"}:
            return None
        path = parsed.path.strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", path):
            return None
        payload = parse_qs(parsed.query).get("start", [""])[0]
        if not RESOURCE_BOT_PAYLOAD_RE.fullmatch(payload):
            return None
        return ResourceBotLink(
            bot_username=path.lower(),
            payload=payload,
            url=cleaned_url,
            source=source,
            source_message_id=source_message_id,
        )

    @staticmethod
    def _clean_url(url: str) -> str:
        return url.strip().rstrip(".,;:!?，。；：！？）)]}>\"'")

    def _resource_bot_whitelist(self) -> set[str]:
        return set(self.config.resource_bots) | set(self.db.list_resource_bots())

    @staticmethod
    def _normalize_bot_username(value: str) -> str:
        candidate = value.strip()
        if candidate.startswith("http://") or candidate.startswith("https://"):
            candidate = urlparse(candidate).path.strip("/")
        candidate = candidate.lstrip("@").lower()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", candidate):
            raise CommandError("机器人 username 格式不合法。")
        return candidate

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

    async def _watch_resource(self, event: events.NewMessage.Event, source: str) -> None:
        entity = await self._resolve_source(source)
        peer_id = int(utils.get_peer_id(entity))
        title = utils.get_display_name(entity) or source
        self.db.add_watch(source, peer_id, title, mode="resource")
        await self._reply(event, f"已监听资源频道：{title}（{source}）")

    async def _unwatch_resource(self, event: events.NewMessage.Event, source: str) -> None:
        removed = self.db.remove_watch(source=source, mode="resource")
        if not removed:
            try:
                entity = await self._resolve_source(source)
                removed = self.db.remove_watch(
                    peer_id=int(utils.get_peer_id(entity)), mode="resource"
                )
            except (CommandError, ValueError, RPCError):
                pass
        await self._reply(
            event,
            "已取消资源监听。" if removed else "未找到该资源监听。",
        )

    async def _watch_code(
        self, event: events.NewMessage.Event, source: str, extract_channel: str
    ) -> None:
        entity = await self._resolve_source(source)
        extract_entity = await self._resolve_source(extract_channel)
        peer_id = int(utils.get_peer_id(entity))
        extract_peer_id = int(utils.get_peer_id(extract_entity))
        title = utils.get_display_name(entity) or source
        extract_title = utils.get_display_name(extract_entity) or extract_channel
        self.db.add_watch(
            source,
            peer_id,
            title,
            mode="code",
            linked_peer_id=extract_peer_id,
            linked_title=extract_title,
        )
        await self._reply(
            event,
            f"已监听提取码频道：{title}（{source}）\n提取频道：{extract_title}（{extract_channel}）",
        )

    async def _unwatch_code(self, event: events.NewMessage.Event, source: str) -> None:
        removed = self.db.remove_watch(source=source, mode="code")
        if not removed:
            try:
                entity = await self._resolve_source(source)
                removed = self.db.remove_watch(
                    peer_id=int(utils.get_peer_id(entity)), mode="code"
                )
            except (CommandError, ValueError, RPCError):
                pass
        await self._reply(
            event,
            "已取消提取码监听。" if removed else "未找到该提取码监听。",
        )

    async def _forward_last_comments(
        self,
        event: events.NewMessage.Event,
        source: str,
        count: int | None,
        force: bool = False,
        start_message_id: int | None = None,
    ) -> None:
        entity = await self._resolve_source(source)
        await self._get_linked_discussion(entity)
        channel_peer_id = int(utils.get_peer_id(entity))
        if count is None:
            await self._forward_last_comments_stream(
                event, entity, source, channel_peer_id, start_message_id or 1, force=force
            )
            return
        post_groups = (
            await self._recent_message_groups(entity, count)
            if start_message_id is None
            else await self._message_groups_from(entity, start_message_id, count)
        )

        result = ForwardResult()
        comment_count = 0
        for group in post_groups:
            self._checkpoint_from_command(
                "/lastcomments", source, int(group[0].id), count, force
            )
            messages, group_comment_count = await self._post_groups_with_comments(
                entity, channel_peer_id, [group]
            )
            comment_count += group_comment_count
            item = await self._forward_many(f"{source}#with-comments", messages, force=force)
            self._merge_forward_result(result, item)
            await self._pause_after_forward(item)
        details = f"\n主帖 {len(post_groups)} 个，评论 {comment_count} 条。"
        await self._reply(event, result.summary() + details)

    async def _forward_last_comments_stream(
        self,
        event: events.NewMessage.Event,
        entity: Any,
        source: str,
        channel_peer_id: int,
        start_message_id: int,
        *,
        force: bool,
    ) -> None:
        await self._reply(
            event,
            f"开始边扫描边转发 {source} 的主帖及评论，起点 {self._message_reference(source, start_message_id)}。",
        )
        result = ForwardResult()
        processed = 0
        comment_count = 0
        async for group in self._iter_message_groups_from(entity, start_message_id):
            processed += 1
            current_id = int(group[0].id)
            next_id = max(int(message.id) for message in group) + 1
            self._checkpoint_from_command("/lastcomments", source, current_id, None, force)
            messages, group_comment_count = await self._post_groups_with_comments(
                entity, channel_peer_id, [group]
            )
            comment_count += group_comment_count
            item = await self._forward_many(f"{source}#with-comments", messages, force=force)
            self._merge_forward_result(result, item)
            await self._pause_after_forward(item)
            if processed % 50 == 0:
                await self._reply(
                    event,
                    f"评论转发进度：主帖 {processed} 个，评论 {comment_count} 条；"
                    f"成功 {result.success}，失败 {result.failed}，跳过 {result.skipped}。",
                )
            self._checkpoint_from_command("/lastcomments", source, next_id, None, force)
        await self._reply(
            event,
            result.summary() + f"\n主帖 {processed} 个，评论 {comment_count} 条。",
        )

    async def _forward_unread_comments(
        self,
        event: events.NewMessage.Event,
        source: str,
        limit: int | None,
        force: bool = False,
        start_message_id: int | None = None,
    ) -> None:
        entity = await self._resolve_source(source)
        linked_entity = await self._get_linked_discussion(entity)
        channel_peer_id = int(utils.get_peer_id(entity))
        linked_peer_id = int(utils.get_peer_id(linked_entity))
        if start_message_id is not None:
            post_groups = await self._message_groups_from(entity, start_message_id, limit)
            result = ForwardResult()
            comment_count = 0
            for group in post_groups:
                self._checkpoint_from_command(
                    "/unreadcomments", source, int(group[0].id), limit, force
                )
                messages, group_comment_count = await self._post_groups_with_comments(
                    entity, channel_peer_id, [group]
                )
                comment_count += group_comment_count
                item = await self._forward_many(
                    f"{source}#with-comments", messages, force=force
                )
                self._merge_forward_result(result, item)
                await self._pause_after_forward(item)
            await self._reply(
                event, result.summary() + f"\n从断点恢复主帖 {len(post_groups)} 个，评论 {comment_count} 条。"
            )
            return

        post_unread = await self._dialog_unread_count(entity)
        comment_unread = await self._dialog_unread_count(linked_entity)
        if post_unread <= 0 and comment_unread <= 0:
            await self._reply(event, f"当前没有未读主帖或未读评论：{source}")
            return

        remaining = limit
        selected_posts = post_unread if remaining is None else min(remaining, post_unread)
        if remaining is not None:
            remaining -= selected_posts
        selected_comments = (
            comment_unread if remaining is None else min(remaining, comment_unread)
        )

        post_groups = await self._recent_message_groups(entity, selected_posts)
        post_result = ForwardResult()
        existing_comment_count = 0
        for group in post_groups:
            self._checkpoint_from_command(
                "/unreadcomments", source, int(group[0].id), limit, force
            )
            messages, group_comment_count = await self._post_groups_with_comments(
                entity, channel_peer_id, [group]
            )
            existing_comment_count += group_comment_count
            item = await self._forward_many(
                f"{source}#with-comments", messages, force=force
            )
            self._merge_forward_result(post_result, item)
            await self._pause_after_forward(item)

        unread_comment_messages = await self._recent_messages(
            linked_entity, selected_comments
        )
        valid_unread_comments = [
            message
            for message in unread_comment_messages
            if await self._is_channel_comment(message, channel_peer_id)
        ]
        comment_result = await self._forward_many(
            f"{source}#comments@{linked_peer_id}", valid_unread_comments, force=force
        )

        processed_all = (
            selected_posts >= post_unread and selected_comments >= comment_unread
        )
        if processed_all:
            if post_unread:
                await self.client.send_read_acknowledge(entity)
            if comment_unread:
                await self.client.send_read_acknowledge(linked_entity)
            read_note = "已标记为已读。"
        else:
            read_note = "仅处理部分未读，未自动标记已读。"

        total = ForwardResult(
            success=post_result.success + comment_result.success,
            failed=post_result.failed + comment_result.failed,
            skipped=post_result.skipped + comment_result.skipped,
            errors=post_result.errors + comment_result.errors,
        )
        details = (
            f"\n频道未读主帖 {post_unread} 条，本次主帖 {selected_posts} 条；"
            f"随主帖转发已有评论 {existing_comment_count} 条。"
            f"\n评论区未读 {comment_unread} 条，本次读取 {selected_comments} 条，"
            f"有效评论 {len(valid_unread_comments)} 条。{read_note}"
        )
        await self._reply(event, total.summary() + details)

    async def _post_groups_with_comments(
        self, entity: Any, channel_peer_id: int, post_groups: list[list[Message]]
    ) -> tuple[list[Message], int]:
        messages: list[Message] = []
        comment_count = 0
        for post_group in post_groups:
            messages.extend(post_group)
            comments: list[Message] | None = None
            for post in post_group:
                try:
                    while True:
                        try:
                            comments = [
                                message
                                async for message in self.client.iter_messages(
                                    entity, reply_to=post.id, limit=None
                                )
                            ]
                            break
                        except FloodWaitError as exc:
                            await self._sleep_for_flood_wait(
                                f"读取评论区：主帖 {self._message_reference(str(channel_peer_id), int(post.id))}",
                                exc,
                            )
                    break
                except MsgIdInvalidError:
                    continue
            if comments is None:
                continue
            comments.reverse()
            messages.extend(comments)
            comment_count += len(comments)
        return messages, comment_count

    async def _recent_message_groups(
        self,
        entity: Any,
        count: int | None,
        progress_callback: Any | None = None,
    ) -> list[list[Message]]:
        if count is not None and count <= 0:
            return []
        order: list[tuple[str, int]] = []
        grouped: dict[tuple[str, int], list[Message]] = {}
        messages_seen = 0
        next_progress_at = 500
        async for message in self.client.iter_messages(entity, limit=None):
            messages_seen += 1
            key = (
                ("album", int(message.grouped_id))
                if message.grouped_id is not None
                else ("message", int(message.id))
            )
            if key not in grouped:
                if count is not None and len(order) >= count:
                    break
                order.append(key)
                grouped[key] = []
            grouped[key].append(message)
            if (
                progress_callback is not None
                and len(order) >= next_progress_at
            ):
                await progress_callback(len(order), messages_seen)
                next_progress_at += 500
        return [list(reversed(grouped[key])) for key in reversed(order)]

    async def _message_groups_from(
        self,
        entity: Any,
        start_message_id: int,
        count: int | None,
        progress_callback: Any | None = None,
    ) -> list[list[Message]]:
        if count is not None and count <= 0:
            return []
        order: list[tuple[str, int]] = []
        grouped: dict[tuple[str, int], list[Message]] = {}
        messages_seen = 0
        next_progress_at = 500
        async for message in self.client.iter_messages(
            entity, min_id=max(0, start_message_id - 1), reverse=True
        ):
            messages_seen += 1
            key = (
                ("album", int(message.grouped_id))
                if message.grouped_id is not None
                else ("message", int(message.id))
            )
            if key not in grouped:
                if count is not None and len(order) >= count:
                    break
                order.append(key)
                grouped[key] = []
            grouped[key].append(message)
            if progress_callback is not None and len(order) >= next_progress_at:
                await progress_callback(len(order), messages_seen)
                next_progress_at += 500
        return [grouped[key] for key in order]

    async def _iter_message_groups_from(
        self, entity: Any, start_message_id: int
    ) -> Any:
        current_key: tuple[str, int] | None = None
        current_group: list[Message] = []
        async for message in self.client.iter_messages(
            entity, min_id=max(0, start_message_id - 1), reverse=True
        ):
            key = (
                ("album", int(message.grouped_id))
                if message.grouped_id is not None
                else ("message", int(message.id))
            )
            if current_key is not None and key != current_key:
                yield current_group
                current_group = []
            current_key = key
            current_group.append(message)
        if current_group:
            yield current_group

    async def _resource_one_groups(
        self, entity: Any, message_id: int
    ) -> list[list[Message]]:
        message = await self.client.get_messages(entity, ids=message_id)
        if message is None:
            raise CommandError(f"消息不存在、已删除或无权访问：{message_id}")
        original_group = (
            await self._nearby_grouped_messages(message)
            if message.grouped_id is not None
            else [message]
        )
        original_ids = {int(item.id) for item in original_group}
        parent_reply_ids = set(original_ids)
        groups: list[list[Message]] = [original_group]
        async for candidate in self.client.iter_messages(
            entity,
            min_id=max(original_ids),
            reverse=True,
            limit=RESOURCE_ONE_LOOKAHEAD,
        ):
            reply_id = int(candidate.reply_to_msg_id or 0)
            if reply_id in parent_reply_ids:
                groups.append([candidate])
                parent_reply_ids.add(int(candidate.id))
        return groups

    async def _recent_messages(self, entity: Any, count: int) -> list[Message]:
        if count <= 0:
            return []
        messages: list[Message] = []
        boundary_group_id: int | None = None
        async for message in self.client.iter_messages(entity, limit=None):
            if len(messages) >= count:
                if (
                    boundary_group_id is not None
                    and message.grouped_id == boundary_group_id
                ):
                    messages.append(message)
                    continue
                break
            messages.append(message)
            if len(messages) == count:
                boundary_group_id = message.grouped_id
                if boundary_group_id is None:
                    break
        return list(reversed(messages))

    async def _dialog_unread_count(self, entity: Any) -> int:
        target_peer_id = int(utils.get_peer_id(entity))
        async for dialog in self.client.iter_dialogs():
            if int(dialog.id) == target_peer_id:
                return int(getattr(dialog, "unread_count", 0) or 0)
        return 0

    async def _entity_from_dialogs(self, peer_id: int) -> Any | None:
        async for dialog in self.client.iter_dialogs():
            if int(dialog.id) == peer_id:
                return dialog.entity
        return None

    @staticmethod
    def _numeric_source_candidates(value: int) -> list[int]:
        candidates = [value]
        # Telegram clients and t.me/c links often expose the internal channel or
        # megagroup ID without the Telethon ``-100`` peer prefix.  Accept that
        # shorthand too, e.g. ``-3630726172`` -> ``-1003630726172``.
        if value < 0 and not str(abs(value)).startswith("100"):
            candidates.append(int(f"-100{abs(value)}"))
        return candidates

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
        saved_watches = self.db.saved_watch_rows()
        if not watches and not saved_watches:
            await self._reply(event, "当前没有监听源。")
            return
        lines = []
        for index, item in enumerate(watches, 1):
            if item.mode == "comments":
                suffix = f" + 评论区（{item.linked_title}）"
                command = "/watchcomments"
            elif item.mode == "resource":
                suffix = " + 资源链接"
                command = "/watchresource"
            elif item.mode == "code":
                suffix = f" + 提取频道（{item.linked_title}）"
                command = "/watchcode"
            else:
                suffix = ""
                command = "/watch"
            lines.append(f"{index}. {command}：{item.title}（{item.source}）{suffix}")
        for item in saved_watches:
            command = "/watchsaved" if item["mode"] == "backup" else "/watchstreamsaved"
            lines.append(
                f"{len(lines) + 1}. {command}：我的收藏；"
                f"最近处理消息 {item['last_message_id'] or '尚未开始'}"
            )
        await self._reply(event, "当前监听源：\n" + "\n".join(lines))

    async def _status(self, event: events.NewMessage.Event) -> None:
        latest_problem = self.db.latest_forward_problem()
        if latest_problem is not None:
            reference = self._message_reference(
                str(latest_problem["source"]), int(latest_problem["message_id"])
            )
            last_error = f"{reference}\n错误：{latest_problem['error']}"
        else:
            last_error = self.db.get_state("last_error", "无")
        last_forward = self.db.get_state("last_forward_at", "无")
        watches = self.db.list_watches()
        saved_watches = self.db.saved_watch_rows()
        if watches:
            watch_lines = []
            for index, item in enumerate(watches, 1):
                if item.mode == "comments":
                    command = "/watchcomments"
                elif item.mode == "resource":
                    command = "/watchresource"
                elif item.mode == "code":
                    command = "/watchcode"
                else:
                    command = "/watch"
                linked = (
                    f"；关联评论区：{item.linked_title}（{item.linked_peer_id}）"
                    if item.mode == "comments"
                    else ""
                )
                if item.mode == "code":
                    linked = f"；提取频道：{item.linked_title}（{item.linked_peer_id}）"
                watch_lines.append(
                    f"  {index}. {command}：{item.title}（{item.source}）{linked}"
                )
            watch_detail = "\n".join(watch_lines)
        else:
            watch_detail = "  无"
        if saved_watches:
            saved_lines = []
            for item in saved_watches:
                command = "/watchsaved" if item["mode"] == "backup" else "/watchstreamsaved"
                saved_lines.append(
                    f"  {len(watches) + len(saved_lines) + 1}. {command}：我的收藏；"
                    f"最近处理消息 {item['last_message_id'] or '尚未开始'}"
                )
            watch_detail = (watch_detail + "\n" if watch_detail != "  无" else "") + "\n".join(saved_lines)
        active_tasks = [
            (task, description)
            for task, description in self.active_command_tasks.items()
            if not task.done()
        ]
        active_descriptions = {description for _, description in active_tasks}
        active_descriptions.update(
            self.active_pending_commands.get(task, "")
            for task, _ in active_tasks
        )
        pending_tasks = [
            item for item in self.db.pending_manual_commands() if item not in active_descriptions
        ]
        task_lines = []
        for task, description in active_tasks:
            checkpoint = self.active_pending_commands.get(task)
            suffix = (
                f"；恢复断点：{checkpoint}"
                if checkpoint and checkpoint != description
                else ""
            )
            task_lines.append(f"  - 开始执行：{description}{suffix}")
        task_lines += [f"  - 待恢复：{description}" for description in pending_tasks]
        active_detail = (
            "\n".join(task_lines)
            if task_lines
            else "  无"
        )
        watch_summary = (
            "普通 "
            f"{self.db.get_state('watch_summary_standard_success', '0')}/"
            f"{self.db.get_state('watch_summary_standard_failed', '0')}/"
            f"{self.db.get_state('watch_summary_standard_skipped', '0')}；"
            "资源 "
            f"{self.db.get_state('watch_summary_resource_success', '0')}/"
            f"{self.db.get_state('watch_summary_resource_failed', '0')}/"
            f"{self.db.get_state('watch_summary_resource_skipped', '0')}；"
            "提取码 "
            f"{self.db.get_state('watch_summary_code_success', '0')}/"
            f"{self.db.get_state('watch_summary_code_failed', '0')}/"
            f"{self.db.get_state('watch_summary_code_skipped', '0')}"
        )
        text = (
            "运行状态\n"
            f"- 已登录：是（{self.owner_id}）\n"
            f"- 监听数量：{len(watches) + len(saved_watches)}\n"
            f"- 监听明细：\n{watch_detail}\n"
            f"- 正在执行的手动命令：\n{active_detail}\n"
            f"- 监听累计 成功/失败/跳过：{watch_summary}\n"
            f"- 最近转发时间：{last_forward}\n"
            f"- 最近错误：{last_error}\n"
            f"- 已转发总数：{self.db.successful_count()}"
            f"\n- 收藏媒体已同步：{self.db.saved_sync_count()}"
            f"\n- 收藏媒体汇总已转发：{self.db.saved_summary_count()}"
            f"\n- 收藏完整备份：{self.db.saved_backup_count()}"
            f"\n- 收藏视频已转换：{self.db.saved_stream_count()}"
            f"\n- 资源链接已处理：{self.db.resource_link_count('done')}"
        )
        await self._reply(event, text)

    async def _tasks(self, event: events.NewMessage.Event) -> None:
        active = [
            (task, description)
            for task, description in self.active_command_tasks.items()
            if not task.done()
        ]
        pending = [
            item
            for item in self.db.pending_manual_commands()
            if item not in {description for _, description in active}
        ]
        if not active and not pending:
            await self._reply(event, "当前没有正在执行或待恢复的任务。")
            return
        lines = ["当前任务："]
        for index, (task, description) in enumerate(active, 1):
            status = self.task_status.get(task, {})
            lines.append(
                f"{index}. {description}\n"
                f"   状态：{status.get('state', '执行中')}\n"
                f"   当前：{status.get('current', '未知')}\n"
                f"   进度：{status.get('processed', 0)}/{status.get('total', '未知')}\n"
                f"   成功/失败/跳过/重复："
                f"{status.get('success', 0)}/{status.get('failed', 0)}/"
                f"{status.get('skipped', 0)}/{status.get('duplicate', 0)}\n"
                + (f"   预计恢复：{status['resume_at']}\n" if status.get("resume_at") else "")
                + f"   更新时间：{status.get('updated_at', '未知')}"
            )
        offset = len(active)
        for index, description in enumerate(pending[:20], offset + 1):
            lines.append(f"{index}. 待恢复：{description}")
        if len(pending) > 20:
            lines.append(f"……另有 {len(pending) - 20} 条待恢复未显示。")
        await self._reply(event, "\n".join(lines))

    async def _stats(self, event: events.NewMessage.Event, period: str) -> None:
        label, start_local, end_local = self._stats_window(period)
        start_utc = start_local.astimezone(timezone.utc).isoformat(timespec="seconds")
        end_utc = end_local.astimezone(timezone.utc).isoformat(timespec="seconds")

        forward_rows = self.db.forward_stats_between(start_utc, end_utc)
        forward_counts = {str(row["status"]): int(row["count"]) for row in forward_rows}
        forward_success = forward_counts.get("success", 0)
        forward_failed = forward_counts.get("failed", 0)
        forward_skipped = forward_counts.get("skipped", 0)
        forward_total = sum(forward_counts.values())

        saved_synced = self.db.saved_sync_count_between(start_utc, end_utc)
        saved_summary = self.db.saved_summary_count_between(start_utc, end_utc)
        saved_sources = self.db.saved_sync_source_count_between(start_utc, end_utc)

        top_rows = self.db.top_forward_sources_between(start_utc, end_utc, limit=8)
        top_lines = []
        for row in top_rows:
            top_lines.append(
                f"  - {row['source']}：{row['status']} {int(row['count'])}"
            )
        top_text = "\n".join(top_lines) if top_lines else "  无"

        text = (
            f"统计：{label}\n"
            f"- 时间范围：{start_local.isoformat(timespec='seconds')} ～ "
            f"{end_local.isoformat(timespec='seconds')}\n"
            f"- 普通转发总记录：{forward_total}\n"
            f"  - 成功：{forward_success}\n"
            f"  - 失败：{forward_failed}\n"
            f"  - 跳过：{forward_skipped}\n"
            f"- 收藏媒体同步：{saved_synced}\n"
            f"- 收藏汇总转发：{saved_summary}\n"
            f"- 涉及收藏来源数：{saved_sources}\n"
            f"- 来源 Top：\n{top_text}"
        )
        await self._reply(event, text)

    @staticmethod
    def _stats_window(period: str) -> tuple[str, datetime, datetime]:
        now = datetime.now().astimezone()
        value = period.lower()
        if value in {"day", "today"}:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            label = "当天"
        elif value == "month":
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if start.month == 12:
                end = start.replace(year=start.year + 1, month=1)
            else:
                end = start.replace(month=start.month + 1)
            label = "当月"
        elif value == "year":
            start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            end = start.replace(year=start.year + 1)
            label = "当年"
        else:
            raise CommandError("用法：/stats [day|month|year]")
        return label, start, end

    async def _saved_selector(
        self, event: events.NewMessage.Event, args: tuple[str, ...]
    ) -> tuple[int | None, int | None, bool]:
        force = self._has_force_arg(args)
        core = self._strip_tail_flags(args, {"force"})
        first = core[0].lower()
        if first == "all":
            return None, None, force
        if first != "from":
            return int(core[0]), None, force
        if len(core) == 2:
            value = core[1].strip()
            if value.isdigit():
                return None, int(value), force
            match = LINK_RE.fullmatch(value)
            if match is None:
                raise CommandError("from 后请使用收藏消息 ID 或消息链接。")
            return None, int(match.group("message_id")), force
        reply = await event.get_reply_message()
        if reply is None or int(getattr(reply, "chat_id", 0) or 0) != self.owner_id:
            raise CommandError("from 未指定消息时，请在“我的收藏”中回复起始消息。")
        return None, int(reply.id), force

    async def _iter_saved_history_groups(
        self, count: int | None, start_message_id: int | None
    ) -> Any:
        if count is not None:
            recent = [message async for message in self.client.iter_messages("me", limit=count)]
            recent.reverse()
            iterator = recent
        else:
            iterator = self.client.iter_messages(
                "me", min_id=max(0, (start_message_id or 1) - 1), reverse=True
            )
        group: list[Message] = []
        async def emit_source() -> Any:
            if isinstance(iterator, list):
                for item in iterator:
                    yield item
            else:
                async for item in iterator:
                    yield item
        async for message in emit_source():
            if start_message_id is not None and int(message.id) < start_message_id:
                continue
            if group and (
                message.grouped_id is None
                or group[0].grouped_id is None
                or message.grouped_id != group[0].grouped_id
            ):
                yield group
                group = []
            group.append(message)
        if group:
            yield group

    async def _run_saved_history(
        self,
        event: events.NewMessage.Event,
        mode: str,
        count: int | None,
        start_message_id: int | None,
        force: bool,
        *,
        watch: bool,
    ) -> None:
        label = "收藏完整备份" if mode == "backup" else "收藏视频在线播放化"
        scope = (
            f"最近 {count} 条" if count is not None
            else f"从消息 {start_message_id} 开始" if start_message_id is not None
            else "全部历史消息"
        )
        await self._reply(event, f"{label}：开始边扫描边处理 {scope}。每扫描 500 条汇报一次。")
        scanned = processed = success = skipped = failed = 0
        history = self._iter_saved_history_groups(count, start_message_id)
        if mode == "backup":
            history = self._batch_saved_history(
                history, size=self.config.forward_batch_size
            )
        lock = self.saved_backup_lock if mode == "backup" else self.saved_stream_lock
        async with lock:
            async for group in history:
                scanned += len(group)
                command_name = f"/watch{mode if mode == 'stream' else ''}saved" if watch else (
                    "/streamsaved" if mode == "stream" else "/syncsaved"
                )
                current_checkpoint = (
                    f"{command_name} from {int(group[0].id)}{' force' if force else ''}"
                )
                self._checkpoint_pending_command(current_checkpoint)
                if mode == "backup":
                    result = await self._backup_saved_group(group, force)
                else:
                    result = ForwardResult()
                    for message in group:
                        item = await self._stream_saved_video(message, force)
                        result.success += item.success
                        result.failed += item.failed
                        result.skipped += item.skipped
                        result.errors.extend(item.errors)
                processed += len(group)
                success += result.success
                skipped += result.skipped
                failed += result.failed
                next_id = int(group[-1].id) + 1
                checkpoint = f"{command_name} from {next_id}{' force' if force else ''}"
                self._checkpoint_pending_command(checkpoint)
                if watch:
                    self.db.update_saved_watch_position(mode, int(group[-1].id))
                self._set_task_status(
                    state="补扫收藏消息", current=f"收藏消息 {int(group[-1].id)}",
                    scanned=scanned, processed=processed, success=success,
                    failed=failed, skipped=skipped,
                )
                if scanned % 500 < len(group):
                    await self._reply(
                        event,
                        f"{label}进度：扫描 {scanned}，成功 {success}，"
                        f"跳过 {skipped}，失败 {failed}；当前位置：收藏消息 {int(group[-1].id)}。",
                    )
        suffix = "；已进入持续监听。" if watch else "。"
        await self._reply(
            event,
            f"{label}补处理完成：扫描 {scanned}，成功 {success}，"
            f"跳过 {skipped}，失败 {failed}{suffix}",
        )

    async def _batch_saved_history(self, groups: Any, size: int = 100) -> Any:
        batch: list[Message] = []
        async for group in groups:
            if batch and len(batch) + len(group) > size:
                yield batch
                batch = []
            batch.extend(group)
        if batch:
            yield batch

    async def _saved_backup_destination(self) -> Any:
        stored = self.db.get_state("saved_backup_peer_id", "")
        if stored:
            try:
                return await self._resolve_source(stored)
            except CommandError:
                LOGGER.warning("Saved backup group unavailable; recreating")
        async for dialog in self.client.iter_dialogs():
            candidate = dialog.entity
            if (
                isinstance(candidate, Channel) and candidate.megagroup and candidate.creator
                and utils.get_display_name(candidate) == SAVED_BACKUP_TITLE
            ):
                self.db.set_state("saved_backup_peer_id", str(int(utils.get_peer_id(candidate))))
                return candidate
        try:
            created = await self.client(CreateChannelRequest(
                title=SAVED_BACKUP_TITLE,
                about="我的收藏完整独立备份；媒体以复制方式保存"[:255],
                broadcast=False,
                megagroup=True,
            ))
        except FloodWaitError as exc:
            await self._sleep_for_flood_wait(f"创建收藏备份群：{SAVED_BACKUP_TITLE}", exc)
            return await self._saved_backup_destination()
        destination = next((chat for chat in created.chats if isinstance(chat, Channel)), None)
        if destination is None:
            raise RuntimeError("创建收藏备份群失败")
        self.db.set_state("saved_backup_peer_id", str(int(utils.get_peer_id(destination))))
        return destination

    async def _backup_saved_group(self, messages: list[Message], force: bool) -> ForwardResult:
        result = ForwardResult()
        destination = await self._saved_backup_destination()
        destination_peer_id = int(utils.get_peer_id(destination))
        pending: list[Message] = []
        recovery_candidates: list[Message] | None = None
        recovered_output_ids: set[int] = set()
        for message in messages:
            if message.file is None and (
                isinstance(message, MessageService) or not (message.message or "").strip()
            ):
                self.db.save_backup_result(
                    int(message.id), int(message.grouped_id) if message.grouped_id else None,
                    destination_peer_id, None, "skipped", "不支持复制的服务或空消息",
                )
                result.skipped += 1
                continue
            row = self.db.saved_backup_row(int(message.id))
            if not force and row is not None and row["status"] == "success":
                result.skipped += 1
                continue
            if not force and row is not None and row["status"] in {"sending", "failed"}:
                if recovery_candidates is None:
                    recovery_candidates = [
                        item async for item in self.client.iter_messages(destination, limit=5000)
                    ]
                recovered = self._match_saved_backup_output(
                    recovery_candidates, message, recovered_output_ids
                )
                if recovered is not None:
                    recovered_output_ids.add(int(recovered.id))
                    self.db.save_backup_result(
                        int(message.id), int(message.grouped_id) if message.grouped_id else None,
                        destination_peer_id, int(recovered.id), "success",
                    )
                    result.success += 1
                    continue
            pending.append(message)
        if not pending:
            return result
        for message in pending:
            self.db.save_backup_result(
                int(message.id), int(message.grouped_id) if message.grouped_id else None,
                destination_peer_id, None, "sending",
            )
        try:
            await self._wait_for_forward_slot(batch=True)
            sent = await self.client.forward_messages(
                destination, pending, drop_author=True, silent=True
            )
            outputs = self._ensure_message_list(sent)
            sent_candidates: list[Message] | None = None
            mapped_output_ids = {
                int(item.id) for item in outputs if item is not None
            }
            for index, message in enumerate(pending):
                output = outputs[index] if index < len(outputs) else None
                output_id = int(output.id) if output is not None else None
                if output_id is None:
                    if sent_candidates is None:
                        sent_candidates = [
                            item async for item in self.client.iter_messages(destination, limit=200)
                        ]
                    output = self._match_saved_backup_output(
                        sent_candidates, message, mapped_output_ids
                    )
                    output_id = int(output.id) if output is not None else None
                    if output_id is not None:
                        mapped_output_ids.add(output_id)
                if output_id is None:
                    output = await self.client.send_message(destination, message, silent=True)
                    output_id = int(output.id) if output is not None else None
                if output_id is None:
                    raise RuntimeError(f"收藏消息 {int(message.id)} 复制后没有目标消息 ID")
                self.db.save_backup_result(
                    int(message.id), int(message.grouped_id) if message.grouped_id else None,
                    destination_peer_id, output_id, "success",
                )
                result.success += 1
        except FloodWaitError as exc:
            await self._sleep_for_flood_wait(f"收藏备份：消息 {int(pending[0].id)}", exc)
            return await self._backup_saved_group(messages, force)
        except Exception as exc:
            error = self._error_text(exc)
            for message in pending:
                self.db.save_backup_result(
                    int(message.id), int(message.grouped_id) if message.grouped_id else None,
                    destination_peer_id, None, "failed", error,
                )
                self._record_failure(result, "saved-backup", int(message.id), exc)
        return result

    @staticmethod
    def _media_identity(message: Message) -> tuple[str, int] | None:
        media = getattr(message, "media", None)
        document = getattr(media, "document", None)
        photo = getattr(media, "photo", None)
        if document is not None:
            return "document", int(document.id)
        if photo is not None:
            return "photo", int(photo.id)
        return None

    @classmethod
    def _match_saved_backup_output(
        cls, candidates: list[Message], source: Message, used_ids: set[int] | None = None
    ) -> Message | None:
        source_media = cls._media_identity(source)
        for candidate in candidates:
            if used_ids is not None and int(candidate.id) in used_ids:
                continue
            if source_media is not None and cls._media_identity(candidate) == source_media:
                return candidate
            if (
                source_media is None
                and bool(source.message)
                and (source.message or "") == (candidate.message or "")
            ):
                return candidate
        return None

    @staticmethod
    def _is_saved_video(message: Message) -> bool:
        mime = str(getattr(getattr(message, "file", None), "mime_type", "") or "")
        return mime.startswith("video/")

    def _is_generated_stream_message(self, message: Message) -> bool:
        if int(message.id) in self.saved_generated_message_ids:
            return True
        reply_to = int(getattr(message, "reply_to_msg_id", 0) or 0)
        if not reply_to:
            return False
        row = self.db.saved_stream_row(reply_to)
        return row is not None and row["stage"] in {"uploading", "complete"}

    @staticmethod
    def _video_already_streamable(message: Message) -> bool:
        document = getattr(getattr(message, "media", None), "document", None)
        return any(
            isinstance(attribute, DocumentAttributeVideo)
            and bool(getattr(attribute, "supports_streaming", False))
            for attribute in (getattr(document, "attributes", None) or [])
        )

    def _stream_disk_safe(self, expected_bytes: int) -> bool:
        usage = shutil.disk_usage(self.config.saved_media_path)
        reserve = max(expected_bytes * 2, 512 * 1024**2)
        return usage.used + reserve <= int(usage.total * 0.90)

    async def _run_process(self, *args: str) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        return int(process.returncode or 0), (stdout + stderr).decode(errors="replace")[-4000:]

    async def _stream_copy_compatible(self, path: Path) -> bool | None:
        process = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
            "-of", "json", str(path), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if process.returncode:
            return None
        try:
            streams = json.loads(stdout.decode()).get("streams", [])
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        video = [item.get("codec_name") for item in streams if item.get("codec_type") == "video"]
        audio = [item.get("codec_name") for item in streams if item.get("codec_type") == "audio"]
        if not video or video == [None] or video == ["unknown"]:
            return None
        return video == ["h264"] and all(codec == "aac" for codec in audio)

    async def _stream_saved_video(self, message: Message, force: bool) -> ForwardResult:
        result = ForwardResult()
        row = self.db.saved_stream_row(int(message.id))
        if not self._is_saved_video(message) or self._is_generated_stream_message(message):
            if row is not None and row["status"] == "running":
                self.db.save_stream_state(
                    int(message.id), "skipped", "not_convertible",
                    error="不是待转换视频或属于程序生成的视频",
                )
                for value in (row["local_input"], row["local_output"]):
                    if value:
                        Path(value).unlink(missing_ok=True)
            result.skipped += 1
            return result
        if not force and row is not None and row["status"] in {"success", "skipped"}:
            result.skipped += 1
            return result
        if not force and self._video_already_streamable(message):
            self.db.save_stream_state(
                int(message.id), "skipped", "already_streaming",
                error="视频已经支持在线播放",
            )
            if row is not None:
                for value in (row["local_input"], row["local_output"]):
                    if value:
                        Path(value).unlink(missing_ok=True)
            result.skipped += 1
            return result
        if not force and row is not None and row["stage"] == "uploading":
            recovered = await self._find_stream_saved_output(int(message.id))
            if recovered is not None:
                self.saved_generated_message_ids.add(int(recovered.id))
                self.db.save_stream_state(
                    int(message.id), "success", "complete", output_message_id=int(recovered.id)
                )
                for value in (row["local_input"], row["local_output"]):
                    if value:
                        Path(value).unlink(missing_ok=True)
                result.success += 1
                return result
        size = int(getattr(message.file, "size", 0) or 0)
        if size >= MAX_STREAM_VIDEO_BYTES:
            reason = f"文件大小 {size / 1024**3:.2f} GB，达到 5 GB 跳过阈值"
            self.db.save_stream_state(int(message.id), "skipped", "size_limit", error=reason)
            self._record_saved_skip(result, int(message.id), reason)
            return result
        if not self._stream_disk_safe(size):
            reason = "预计下载和转换后磁盘占用会达到 90%，已跳过"
            self.db.save_stream_state(int(message.id), "skipped", "disk_limit", error=reason)
            self._record_saved_skip(result, int(message.id), reason)
            return result
        folder = self.config.saved_media_path / "streamsaved"
        folder.mkdir(parents=True, exist_ok=True)
        extension = getattr(message.file, "ext", None) or ".video"
        input_path = Path(row["local_input"]) if row and row["local_input"] else folder / f"{message.id}_input{extension}"
        output_path = Path(row["local_output"]) if row and row["local_output"] else folder / f"{message.id}_stream.mp4"
        try:
            if input_path.exists() and size and input_path.stat().st_size != size:
                input_path.unlink()
                output_path.unlink(missing_ok=True)
            if row is not None and row["stage"] == "downloading" and input_path.exists():
                # Telegram downloads are not byte-resumable here; a partial file must not be probed.
                input_path.unlink()
            if not input_path.exists():
                self.db.save_stream_state(
                    int(message.id), "running", "downloading", local_input=str(input_path),
                    local_output=str(output_path),
                )
                downloaded = await self.client.download_media(message, file=str(input_path))
                if downloaded is None:
                    raise RuntimeError("视频下载失败")
                input_path = Path(downloaded)
            if not output_path.exists():
                compatible = await self._stream_copy_compatible(input_path)
                if compatible is None:
                    reason = "源视频文件损坏或无法读取，已跳过"
                    self.db.save_stream_state(
                        int(message.id), "skipped", "invalid_media", error=reason
                    )
                    input_path.unlink(missing_ok=True)
                    output_path.unlink(missing_ok=True)
                    self._record_saved_skip(result, int(message.id), reason)
                    return result
                if compatible:
                    self.db.save_stream_state(int(message.id), "running", "remuxing")
                    code, detail = await self._run_process(
                        "ffmpeg", "-y", "-i", str(input_path), "-map", "0:v:0", "-map", "0:a:0?",
                        "-c", "copy", "-movflags", "+faststart", str(output_path),
                    )
                else:
                    code, detail = 1, "codec requires transcoding"
                if code != 0:
                    self.db.save_stream_state(int(message.id), "running", "transcoding")
                    code, detail = await self._run_process(
                        "ffmpeg", "-y", "-i", str(input_path), "-map", "0:v:0", "-map", "0:a:0?",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                        "-c:a", "aac", "-movflags", "+faststart", str(output_path),
                    )
                    if code != 0:
                        raise RuntimeError(f"ffmpeg 转换失败：{detail[-800:]}")
            self.db.save_stream_state(int(message.id), "running", "uploading")
            sent = await self.client.send_file(
                "me", str(output_path), caption=message.message or "",
                formatting_entities=message.entities or [], supports_streaming=True,
                force_document=False, reply_to=int(message.id),
            )
            output_id = int(sent.id)
            self.saved_generated_message_ids.add(output_id)
            self.db.save_stream_state(
                int(message.id), "success", "complete", output_message_id=output_id,
                local_input=str(input_path), local_output=str(output_path),
            )
            input_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)
            result.success += 1
        except FloodWaitError as exc:
            await self._sleep_for_flood_wait(f"收藏视频转换上传：消息 {int(message.id)}", exc)
            return await self._stream_saved_video(message, force)
        except Exception as exc:
            error = self._error_text(exc)
            self.db.save_stream_state(
                int(message.id), "failed", "failed", local_input=str(input_path),
                local_output=str(output_path), error=error,
            )
            self._record_failure(result, "saved-stream", int(message.id), exc)
        return result

    async def _find_stream_saved_output(self, source_message_id: int) -> Message | None:
        async for candidate in self.client.iter_messages(
            "me", min_id=source_message_id, limit=2000
        ):
            if (
                int(getattr(candidate, "reply_to_msg_id", 0) or 0) == source_message_id
                and self._is_saved_video(candidate)
            ):
                return candidate
        return None

    async def _handle_saved_message(self, event: events.NewMessage.Event) -> None:
        if event.chat_id != self.owner_id or event.sender_id != self.owner_id:
            return
        message = event.message
        if (message.raw_text or "").lstrip().startswith("/"):
            return
        if message.grouped_id is not None:
            grouped_id = int(message.grouped_id)
            if grouped_id in self.saved_event_groups:
                return
            self.saved_event_groups.add(grouped_id)
            await asyncio.sleep(3)
            messages = await self._nearby_grouped_messages(message)
        else:
            messages = [message]
        for mode in ("backup", "stream"):
            watch = self.db.saved_watch(mode)
            if watch is None or not bool(watch["enabled"]):
                continue
            lock = self.saved_backup_lock if mode == "backup" else self.saved_stream_lock
            async with lock:
                if mode == "backup":
                    result = await self._backup_saved_group(messages, False)
                else:
                    result = ForwardResult()
                    for item in messages:
                        partial = await self._stream_saved_video(item, False)
                        result.success += partial.success
                        result.failed += partial.failed
                        result.skipped += partial.skipped
                        result.errors.extend(partial.errors)
                self.db.update_saved_watch_position(mode, int(messages[-1].id))
                if result.failed or result.errors:
                    await self._notify_control_bot(
                        f"收藏{'备份' if mode == 'backup' else '视频转换'}监听异常："
                        f"消息 {int(messages[0].id)}\n{result.summary()}"
                    )

    async def _saved_watch_sweep_loop(self) -> None:
        await asyncio.sleep(10)
        while True:
            try:
                for mode in ("stream", "backup"):
                    lock = self.saved_stream_lock if mode == "stream" else self.saved_backup_lock
                    if lock.locked():
                        continue
                    watch = self.db.saved_watch(mode)
                    if (
                        watch is None
                        or not bool(watch["enabled"])
                        or watch["last_message_id"] is None
                    ):
                        continue
                    await self._sweep_saved_watch(mode, int(watch["last_message_id"]) + 1)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Saved Messages watch sweep failed")
            await asyncio.sleep(SAVED_WATCH_SWEEP_INTERVAL_SECONDS)

    async def _sweep_saved_watch(self, mode: str, start_message_id: int) -> None:
        lock = self.saved_stream_lock if mode == "stream" else self.saved_backup_lock
        async for messages in self._iter_saved_history_groups(None, start_message_id):
            async with lock:
                if mode == "backup":
                    result = await self._backup_saved_group(messages, False)
                else:
                    result = ForwardResult()
                    for message in messages:
                        partial = await self._stream_saved_video(message, False)
                        result.success += partial.success
                        result.failed += partial.failed
                        result.skipped += partial.skipped
                        result.errors.extend(partial.errors)
                self.db.update_saved_watch_position(mode, int(messages[-1].id))
            if result.failed or result.errors:
                await self._notify_control_bot(
                    f"收藏{'备份' if mode == 'backup' else '视频转换'}轮询异常："
                    f"消息 {int(messages[0].id)}\n{result.summary()}"
                )

    async def _sync_saved_media(
        self,
        event: events.NewMessage.Event,
        count: int | None,
        source_filter: tuple[int, str] | None,
        *,
        download_upload: bool,
    ) -> None:
        """Copy Saved Messages media by source channel, optionally via local files."""
        result = ForwardResult()
        summary_result = ForwardResult()
        channel_count = 0
        summary_channel_created = False
        stopped_reason = ""
        scope = "全部收藏媒体" if count is None else f"最近 {count} 条收藏媒体"
        if source_filter is not None:
            scope += f"；来源：{source_filter[1]}"
        await self._reply(event, f"同步准备：{scope}。扫描和创建频道可能需要一些时间。")
        async with self.saved_sync_lock:
            messages = await self._saved_messages_for_sync(
                count,
                progress_callback=lambda scanned, matched: self._reply(
                    event,
                    f"扫描进度：已读取 {scanned} 条收藏消息，匹配 {matched} 条媒体消息。",
                ),
            )
            scanned_count = len(messages)
            messages.reverse()
            groups = self._group_messages(messages)
            destinations: dict[int, Any] = {}
            summary_channel: Any | None = None

            for group_index, group in enumerate(groups, 1):
                if group_index == 1 or group_index % 100 == 0:
                    await self._reply(
                        event,
                        f"处理进度：{group_index}/{len(groups)} 个媒体组；"
                        f"同步成功 {result.success}，汇总成功 {summary_result.success}，"
                        f"跳过 {result.skipped}。",
                    )
                media_group = [message for message in group if message.file is not None]
                if not media_group:
                    for message in group:
                        self._record_saved_skip(result, message.id, "不是媒体消息")
                    continue

                source = await self._saved_forward_source(media_group[0])
                if source is None:
                    source = (UNKNOWN_SAVED_SOURCE_PEER_ID, UNKNOWN_SAVED_SOURCE_TITLE)
                source_peer_id, source_title = source
                if source_filter is not None and source_peer_id != source_filter[0]:
                    continue

                sync_rows = {
                    message.id: self.db.get_saved_sync(message.id)
                    for message in media_group
                }
                pending_summary = [
                    message
                    for message in media_group
                    if sync_rows[message.id] is not None
                    and sync_rows[message.id]["destination_message_id"] is not None
                    and sync_rows[message.id]["summary_message_id"] is None
                ]
                already_complete = [
                    message
                    for message in media_group
                    if sync_rows[message.id] is not None
                    and sync_rows[message.id]["summary_message_id"] is not None
                ]
                result.skipped += len(already_complete)

                unsynced = [message for message in media_group if sync_rows[message.id] is None]
                if not unsynced and not pending_summary:
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
                    if not unsynced and not pending_summary:
                        continue

                try:
                    destination = destinations.get(source_peer_id)
                    if destination is None:
                        destination, created = await self._saved_destination(
                            source_peer_id, source_title
                        )
                        destinations[source_peer_id] = destination
                        channel_count += int(created)
                    if summary_channel is None:
                        summary_channel, summary_created_now = (
                            await self._saved_summary_channel()
                        )
                        summary_channel_created = (
                            summary_channel_created or summary_created_now
                        )
                    destination_peer_id = int(utils.get_peer_id(destination))
                    destination_messages: list[Message] = []
                    destination_saved_ids: list[int] = []

                    if pending_summary:
                        pending_ids = [
                            int(sync_rows[message.id]["destination_message_id"])
                            for message in pending_summary
                        ]
                        fetched = await self.client.get_messages(destination, ids=pending_ids)
                        fetched_items = self._ensure_message_list(fetched)
                        for saved_message, destination_message in zip(
                            pending_summary, fetched_items
                        ):
                            if destination_message is None:
                                self._record_saved_skip(
                                    summary_result,
                                    saved_message.id,
                                    "私有频道中的已同步消息不存在，无法补转汇总",
                                )
                                continue
                            destination_messages.append(destination_message)
                            destination_saved_ids.append(saved_message.id)

                    paths: list[Path] | None = None
                    if unsynced:
                        if download_upload:
                            paths = await self._download_saved_group(
                                unsynced, source_peer_id, source_title
                            )
                            sent = await self._upload_saved_group(
                                destination, unsynced, paths
                            )
                        else:
                            sent = await self._copy_saved_group(destination, unsynced)
                        sent_messages = self._ensure_message_list(sent)
                        for index, (message, sent_message) in enumerate(
                            zip(unsynced, sent_messages)
                        ):
                            local_path = str(paths[index]) if paths is not None else None
                            destination_message_id = (
                                int(sent_message.id) if sent_message is not None else None
                            )
                            self.db.mark_saved_message_synced(
                                message.id,
                                source_peer_id,
                                destination_peer_id,
                                destination_message_id,
                                local_path,
                            )
                            if sent_message is not None:
                                destination_messages.append(sent_message)
                                destination_saved_ids.append(message.id)
                        result.success += len(unsynced)

                    if destination_messages:
                        await self._forward_saved_summary_group(
                            summary_channel,
                            destination_messages,
                            destination_saved_ids,
                            summary_result,
                        )
                    self.db.set_state(
                        "last_saved_sync_at",
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    )
                except FloodWaitError as exc:
                    request_name = type(getattr(exc, "request", None)).__name__
                    stopped_reason = (
                        f"Telegram 要求等待 {int(exc.seconds)} 秒后才能继续"
                        f"（{request_name}）。本次同步已暂停，稍后可重试。"
                    )
                    self._remember_error(exc)
                    await self._notify_flood_wait(
                        f"收藏媒体同步：{source_title}（{request_name}）",
                        exc,
                    )
                    for message in unsynced + pending_summary:
                        self._record_failure(
                            result, f"saved:{source_peer_id}", message.id, exc
                        )
                    break
                except Exception as exc:
                    LOGGER.exception("Saved media sync failed for %s", source_title)
                    for message in unsynced:
                        self._record_failure(
                            result, f"saved:{source_peer_id}", message.id, exc
                        )

                await asyncio.sleep(random.uniform(0.5, 1.2))

        detail = (
            f"\n扫描范围：{scope}；实际匹配 {scanned_count} 条媒体消息；"
            f"新建目标频道 {channel_count} 个；"
            f"汇总频道{'新建' if summary_channel_created else '复用'}。"
        )
        if download_upload:
            detail += f"\n模式：下载后上传；下载目录：{self.config.saved_media_path}"
        else:
            detail += "\n模式：Telegram 服务器端复制；未下载媒体文件。"
        if stopped_reason:
            detail += f"\n停止原因：{stopped_reason}"
        await self._reply(
            event,
            result.summary() + "\n汇总转发：" + summary_result.summary() + detail,
        )

    @staticmethod
    def _saved_sync_limit(value: str) -> int | None:
        return None if value.lower() == "all" else int(value)

    async def _saved_source_filter(self, args: tuple[str, ...]) -> tuple[int, str] | None:
        if len(args) < 2:
            return None
        source = args[1].strip()
        if source.lower() in UNKNOWN_SAVED_SOURCE_TOKENS:
            return UNKNOWN_SAVED_SOURCE_PEER_ID, UNKNOWN_SAVED_SOURCE_TITLE
        entity = await self._resolve_source(source)
        return int(utils.get_peer_id(entity)), utils.get_display_name(entity) or source

    async def _saved_messages_for_sync(
        self,
        count: int | None,
        progress_callback: Any | None = None,
    ) -> list[Message]:
        """Read the requested number of media messages, keeping albums intact.

        Saved Messages also contains the control commands and their replies.  A
        Telegram-side ``limit=count`` would let those text messages consume the
        whole range before older media is reached, so apply the limit only after
        filtering for copyable media.
        """
        messages: list[Message] = []
        boundary_group_id: int | None = None
        scanned_count = 0
        last_progress_scanned = 0
        last_progress_matched = 0
        async for message in self.client.iter_messages("me", limit=None):
            scanned_count += 1
            if message.file is None:
                if (
                    progress_callback is not None
                    and scanned_count - last_progress_scanned >= 1000
                ):
                    await progress_callback(scanned_count, len(messages))
                    last_progress_scanned = scanned_count
                    last_progress_matched = len(messages)
                continue

            if count is not None and len(messages) >= count:
                if (
                    boundary_group_id is not None
                    and message.grouped_id == boundary_group_id
                ):
                    messages.append(message)
                    continue
                break

            messages.append(message)
            if (
                progress_callback is not None
                and (
                    scanned_count - last_progress_scanned >= 1000
                    or len(messages) - last_progress_matched >= 250
                )
            ):
                await progress_callback(scanned_count, len(messages))
                last_progress_scanned = scanned_count
                last_progress_matched = len(messages)
            if count is not None and len(messages) == count:
                boundary_group_id = message.grouped_id
                if boundary_group_id is None:
                    break
        if progress_callback is not None:
            await progress_callback(scanned_count, len(messages))
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

        try:
            created = await self.client(
                CreateChannelRequest(
                    title=source_title[:128],
                    about=f"由收藏夹同步；原频道：{source_title}"[:255],
                    broadcast=True,
                    megagroup=False,
                )
            )
        except FloodWaitError as exc:
            await self._notify_flood_wait(f"创建来源私有频道：{source_title}", exc)
            raise
        destination = next(
            (chat for chat in created.chats if isinstance(chat, Channel)), None
        )
        if destination is None:
            raise RuntimeError(f"创建目标频道失败：{source_title}")
        self.db.save_channel_mapping(
            source_peer_id, source_title, int(utils.get_peer_id(destination))
        )
        return destination, True

    async def _saved_summary_channel(self) -> tuple[Any, bool]:
        stored_peer_id = self.db.get_state("saved_summary_channel_peer_id", "")
        if stored_peer_id:
            try:
                return await self._resolve_source(stored_peer_id), False
            except CommandError:
                LOGGER.warning("Stored saved summary channel is unavailable; recreating it")

        async for dialog in self.client.iter_dialogs():
            candidate = dialog.entity
            if (
                isinstance(candidate, Channel)
                and candidate.broadcast
                and candidate.creator
                and utils.get_display_name(candidate) == SAVED_SUMMARY_CHANNEL_TITLE
            ):
                peer_id = int(utils.get_peer_id(candidate))
                self.db.set_state("saved_summary_channel_peer_id", str(peer_id))
                return candidate, False

        try:
            created = await self.client(
                CreateChannelRequest(
                    title=SAVED_SUMMARY_CHANNEL_TITLE,
                    about="由收藏夹同步汇总；消息从各来源私有频道转发而来"[:255],
                    broadcast=True,
                    megagroup=False,
                )
            )
        except FloodWaitError as exc:
            await self._notify_flood_wait(
                f"创建收藏媒体汇总频道：{SAVED_SUMMARY_CHANNEL_TITLE}", exc
            )
            raise
        summary = next(
            (chat for chat in created.chats if isinstance(chat, Channel)), None
        )
        if summary is None:
            raise RuntimeError(f"创建汇总频道失败：{SAVED_SUMMARY_CHANNEL_TITLE}")
        self.db.set_state("saved_summary_channel_peer_id", str(int(utils.get_peer_id(summary))))
        return summary, True

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
    ) -> Message | list[Message]:
        captions = [message.message or "" for message in messages]
        entities = [message.entities or [] for message in messages]
        return await self.client.send_file(
            destination,
            [str(path) for path in paths],
            caption=captions,
            formatting_entities=entities,
            supports_streaming=True,
        )

    async def _copy_saved_group(
        self, destination: Any, messages: list[Message]
    ) -> Message | list[Message]:
        captions = [message.message or "" for message in messages]
        entities = [message.entities or [] for message in messages]
        return await self.client.send_file(
            destination,
            [message.media for message in messages],
            caption=captions,
            formatting_entities=entities,
            supports_streaming=True,
        )

    async def _forward_saved_summary_group(
        self,
        summary_channel: Any,
        destination_messages: list[Message],
        saved_message_ids: list[int],
        result: ForwardResult,
    ) -> None:
        if not destination_messages:
            return
        try:
            forwarded = await self.client.forward_messages(
                summary_channel, destination_messages
            )
            forwarded_messages = self._ensure_message_list(forwarded)
            summary_peer_id = int(utils.get_peer_id(summary_channel))
            for saved_message_id, summary_message in zip(
                saved_message_ids, forwarded_messages
            ):
                summary_message_id = (
                    int(summary_message.id) if summary_message is not None else None
                )
                self.db.mark_saved_message_summarized(
                    saved_message_id, summary_peer_id, summary_message_id
                )
                if summary_message_id is not None:
                    result.success += 1
                else:
                    self._record_saved_skip(
                        result,
                        saved_message_id,
                        "汇总转发没有返回消息 ID",
                    )
        except (ChatForwardsRestrictedError, MessageIdInvalidError, ChannelPrivateError, ChatAdminRequiredError) as exc:
            for saved_message_id in saved_message_ids:
                self._record_saved_skip(result, saved_message_id, self._error_text(exc))
        except RPCError as exc:
            for saved_message_id in saved_message_ids:
                self._record_failure(
                    result, f"saved-summary:{SAVED_SUMMARY_CHANNEL_TITLE}", saved_message_id, exc
                )

    @staticmethod
    def _ensure_message_list(
        value: Message | list[Message] | None,
    ) -> list[Message | None]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

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
            asyncio.create_task(self._handle_grouped_message_fallback(event))
            return  # Album events forward the complete media group once when available.
        if int(event.chat_id) not in self.db.watched_peer_ids():
            return
        match = self.db.find_watch_for_peer(int(event.chat_id))
        if match is not None:
            watch, is_linked_discussion = match
            if watch.mode == "resource" and not is_linked_discussion:
                await self._process_resource_watch_group(watch.source, [event.message])
                return
            if watch.mode == "code" and not is_linked_discussion:
                await self._process_code_watch_group(watch, [event.message])
                return
            if watch.mode == "comments" and not is_linked_discussion:
                await self._process_comments_watch_group(watch, [event.message])
                return
        LOGGER.info(
            "watch event: chat=%s message=%s grouped=%s",
            event.chat_id,
            event.id,
            event.message.grouped_id,
        )
        source = await self._watched_source_for_message(int(event.chat_id), event.message)
        if source is None:
            return
        LOGGER.info("watch forward start: source=%s message=%s", source, event.id)
        if "#comments@" not in source:
            self._schedule_watch_forward(source, int(event.id), int(event.id))
            return
        command_text = self._watch_recovery_command(
            source, event.message, comments="#comments@" in source
        )
        result = await self._run_limited_watch_forward(
            source, [event.message], command_text
        )
        LOGGER.info(
            "watch forward done: source=%s message=%s success=%s failed=%s skipped=%s",
            source,
            event.id,
            result.success,
            result.failed,
            result.skipped,
        )
        if result.failed or result.skipped:
            text = f"监听源 {source} 的消息 {event.id} 未能转发。\n{result.summary()}"
            if not await self._notify_control_bot(text):
                await self.client.send_message("me", text)
        else:
            self._record_watch_summary("standard", success=result.success)

    async def _handle_grouped_message_fallback(
        self, event: events.NewMessage.Event
    ) -> None:
        if event.chat_id is None or event.message.grouped_id is None:
            return
        key = (int(event.chat_id), int(event.message.grouped_id))
        await asyncio.sleep(3.0)
        if key in self.handled_album_keys:
            return
        if int(event.chat_id) not in self.db.watched_peer_ids():
            return
        match = self.db.find_watch_for_peer(int(event.chat_id))
        if match is not None:
            watch, is_linked_discussion = match
            if watch.mode == "resource" and not is_linked_discussion:
                self.handled_album_keys.add(key)
                group = await self._nearby_grouped_messages(event.message)
                await self._process_resource_watch_group(watch.source, group or [event.message])
                return
            if watch.mode == "code" and not is_linked_discussion:
                self.handled_album_keys.add(key)
                group = await self._nearby_grouped_messages(event.message)
                await self._process_code_watch_group(watch, group or [event.message])
                return
            if watch.mode == "comments" and not is_linked_discussion:
                self.handled_album_keys.add(key)
                group = await self._nearby_grouped_messages(event.message)
                await self._process_comments_watch_group(watch, group or [event.message])
                return
        source = await self._watched_source_for_message(int(event.chat_id), event.message)
        if source is None:
            return
        self.handled_album_keys.add(key)
        LOGGER.info(
            "watch grouped fallback forward start: source=%s message=%s grouped=%s",
            source,
            event.id,
            event.message.grouped_id,
        )
        group = await self._nearby_grouped_messages(event.message)
        if "#comments@" not in source:
            ids = [int(message.id) for message in group or [event.message]]
            self._schedule_watch_forward(source, min(ids), max(ids))
            return
        command_text = self._watch_recovery_command(
            source, group[0] if group else event.message
        )
        result = await self._run_limited_watch_forward(
            source, group or [event.message], command_text
        )
        LOGGER.info(
            "watch grouped fallback forward done: source=%s message=%s group_count=%s success=%s failed=%s skipped=%s",
            source,
            event.id,
            len(group) if group else 1,
            result.success,
            result.failed,
            result.skipped,
        )
        if result.failed or result.skipped:
            await self.client.send_message(
                "me",
                f"监听源 {source} 的 grouped 消息 {event.id} 兜底转发未完整成功。\n{result.summary()}",
            )
        else:
            self._record_watch_summary("standard", success=result.success)

    async def _nearby_grouped_messages(self, message: Message) -> list[Message]:
        if message.chat_id is None or message.grouped_id is None:
            return [message]
        lower_bound = max(0, int(message.id) - 30)
        group: list[Message] = []
        async for candidate in self.client.iter_messages(
            message.chat_id, min_id=lower_bound, reverse=True
        ):
            if candidate.grouped_id == message.grouped_id:
                group.append(candidate)
            elif candidate.id > message.id + 30:
                break
        return group or [message]

    async def _handle_watched_album(self, event: events.Album.Event) -> None:
        if event.chat_id is None or event.chat_id == self.owner_id or not event.messages:
            return
        if int(event.chat_id) not in self.db.watched_peer_ids():
            return
        if event.messages[0].grouped_id is not None:
            self.handled_album_keys.add(
                (int(event.chat_id), int(event.messages[0].grouped_id))
            )
        match = self.db.find_watch_for_peer(int(event.chat_id))
        if match is not None:
            watch, is_linked_discussion = match
            if watch.mode == "resource" and not is_linked_discussion:
                await self._process_resource_watch_group(watch.source, list(event.messages))
                return
            if watch.mode == "code" and not is_linked_discussion:
                await self._process_code_watch_group(watch, list(event.messages))
                return
            if watch.mode == "comments" and not is_linked_discussion:
                await self._process_comments_watch_group(watch, list(event.messages))
                return
        LOGGER.info(
            "watch album event: chat=%s first_message=%s count=%s",
            event.chat_id,
            event.messages[0].id,
            len(event.messages),
        )
        source = await self._watched_source_for_message(
            int(event.chat_id), event.messages[0]
        )
        if source is None:
            return
        LOGGER.info(
            "watch album forward start: source=%s first_message=%s count=%s",
            source,
            event.messages[0].id,
            len(event.messages),
        )
        if "#comments@" not in source:
            ids = [int(message.id) for message in event.messages]
            self._schedule_watch_forward(source, min(ids), max(ids))
            return
        command_text = self._watch_recovery_command(source, event.messages[0])
        result = await self._run_limited_watch_forward(
            source, list(event.messages), command_text
        )
        LOGGER.info(
            "watch album forward done: source=%s first_message=%s success=%s failed=%s skipped=%s",
            source,
            event.messages[0].id,
            result.success,
            result.failed,
            result.skipped,
        )
        if result.failed or result.skipped:
            await self.client.send_message(
                "me",
                f"监听源 {source} 的媒体组 {event.messages[0].id} 未能完整转发。\n{result.summary()}",
            )
        else:
            self._record_watch_summary("standard", success=result.success)

    def _watch_recovery_command(
        self, source: str, message: Message, *, comments: bool = False
    ) -> str | None:
        link = self._message_link(source, int(message.id))
        if link is None:
            return None
        return f"/link {link}"

    async def _run_limited_watch_forward(
        self,
        source: str,
        messages: list[Message],
        command_text: str | None,
    ) -> ForwardResult:
        async with self.watch_forward_semaphore:
            if command_text:
                return await self._run_recoverable_text(
                    command_text, self._forward_many(source, messages)
                )
            return await self._forward_many(source, messages)

    @staticmethod
    def _canonical_watch_source(source: str) -> str | None:
        link = TelegramSaveHelper._message_link(source, 1)
        match = LINK_RE.fullmatch(link or "")
        if match is None:
            return None
        if match.group("username"):
            return "@" + match.group("username")
        return "-100" + match.group("internal")

    def _schedule_watch_forward(
        self, source: str, first_message_id: int, last_message_id: int
    ) -> None:
        queue_source = self._canonical_watch_source(source)
        if queue_source is None:
            return
        first_message_id = int(first_message_id)
        last_message_id = int(last_message_id)
        current = self.pending_watch_forwards.get(queue_source)
        if current is None:
            self.pending_watch_forwards[queue_source] = (
                first_message_id,
                last_message_id,
            )
        else:
            self.pending_watch_forwards[queue_source] = (
                min(current[0], first_message_id),
                max(current[1], last_message_id),
            )
        self._save_watch_forward_ranges()
        task = self.watch_forward_tasks.get(queue_source)
        if task is None or task.done():
            self._start_watch_forward_task(queue_source)

    def _start_watch_forward_task(self, source: str) -> None:
        task = asyncio.create_task(self._watch_forward_worker(source))
        self.watch_forward_tasks[source] = task
        task.add_done_callback(
            lambda finished, scheduled_source=source: self._finish_watch_forward_task(
                scheduled_source, finished
            )
        )

    def _finish_watch_forward_task(
        self, source: str, task: asyncio.Task[Any]
    ) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            LOGGER.exception("watch forward queue failed: source=%s", source)
        finally:
            if self.watch_forward_tasks.get(source) is task:
                self.watch_forward_tasks.pop(source, None)
        if source in self.pending_watch_forwards:
            self._start_watch_forward_task(source)

    def _save_watch_forward_ranges(self) -> None:
        self.db.set_state(
            WATCH_FORWARD_STATE_KEY,
            json.dumps(self.pending_watch_forwards, ensure_ascii=False),
        )

    def _resume_watch_forward_ranges(self) -> None:
        raw = self.db.get_state(WATCH_FORWARD_STATE_KEY, "")
        if not raw:
            return
        try:
            saved = json.loads(raw)
            self.pending_watch_forwards = {
                str(source): (int(bounds[0]), int(bounds[1]))
                for source, bounds in saved.items()
                if isinstance(bounds, list) and len(bounds) == 2
            }
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            LOGGER.warning("Ignoring invalid persisted watch forward ranges")
            self.pending_watch_forwards = {}
            self._save_watch_forward_ranges()
            return
        for source in self.pending_watch_forwards:
            self._start_watch_forward_task(source)

    def _migrate_pending_watch_links(self) -> None:
        if self.db.get_state(WATCH_FORWARD_MIGRATION_KEY, "") == "done":
            return
        standard_sources = {
            canonical
            for watch in self.db.list_watches()
            if watch.mode == "standard"
            if (canonical := self._canonical_watch_source(watch.source)) is not None
        }
        migrated = 0
        for command_text in list(self.db.pending_manual_commands()):
            match = re.fullmatch(r"/link\s+(\S+)", command_text.strip())
            if match is None:
                continue
            link_match = LINK_RE.fullmatch(match.group(1))
            if link_match is None:
                continue
            source = (
                "@" + link_match.group("username")
                if link_match.group("username")
                else "-100" + link_match.group("internal")
            )
            if source not in standard_sources:
                continue
            message_id = int(link_match.group("message_id"))
            current = self.pending_watch_forwards.get(source)
            self.pending_watch_forwards[source] = (
                min(current[0], message_id) if current else message_id,
                max(current[1], message_id) if current else message_id,
            )
            self.db.remove_pending_manual_command(command_text)
            migrated += 1
        self._save_watch_forward_ranges()
        self.db.set_state(WATCH_FORWARD_MIGRATION_KEY, "done")
        LOGGER.info(
            "migrated pending watch links: commands=%s sources=%s",
            migrated,
            len(self.pending_watch_forwards),
        )

    async def _watch_forward_worker(self, source: str) -> None:
        entity = await self._resolve_source(source)
        while source in self.pending_watch_forwards:
            start_id, end_id = self.pending_watch_forwards[source]
            total = ForwardResult()
            async for group in self._iter_message_groups_from(entity, start_id):
                first_id = int(group[0].id)
                if first_id > end_id:
                    break
                item = await self._forward_many(source, group)
                self._merge_forward_result(total, item)
                self._record_watch_summary(
                    "standard",
                    success=item.success,
                    failed=item.failed,
                    skipped=item.skipped,
                )
                if item.failed or item.skipped:
                    await self._notify_control_bot(
                        f"watch 转发异常：{self._message_reference(source, first_id)}\n"
                        f"{item.summary()}"
                    )
                next_id = max(int(message.id) for message in group) + 1
                current = self.pending_watch_forwards.get(source)
                if current is None:
                    return
                self.pending_watch_forwards[source] = (
                    min(current[0], next_id) if current[0] < start_id else next_id,
                    max(current[1], end_id),
                )
                self._save_watch_forward_ranges()
            current = self.pending_watch_forwards.get(source)
            if current is None:
                return
            if current[1] <= end_id:
                self.pending_watch_forwards.pop(source, None)
            else:
                self.pending_watch_forwards[source] = (
                    max(current[0], end_id + 1),
                    current[1],
                )
            self._save_watch_forward_ranges()
            LOGGER.info(
                "watch forward range complete: source=%s range=%s-%s success=%s failed=%s skipped=%s",
                source,
                start_id,
                end_id,
                total.success,
                total.failed,
                total.skipped,
            )

    async def _process_comments_watch_group(self, watch: Any, group: list[Message]) -> None:
        link = self._message_link(watch.source, int(group[0].id)) if group else None
        command_text = f"/watchcomments {watch.source} from {link}" if link else f"/watchcomments {watch.source}"
        await self._run_recoverable_text(
            command_text, self._process_comments_watch_group_inner(watch, group)
        )

    async def _process_comments_watch_group_inner(self, watch: Any, group: list[Message]) -> None:
        if not group:
            return
        messages, comment_count = await self._post_groups_with_comments(
            await self._resolve_source(watch.source),
            int(watch.peer_id),
            [group],
        )
        result = await self._forward_many(
            f"{watch.source}#with-comments",
            messages,
        )
        self._record_watch_summary(
            "comments",
            success=result.success,
            failed=result.failed,
            skipped=result.skipped,
        )
        first_id = int(group[0].id)
        if result.failed or result.skipped:
            text = (
                f"watchcomments 异常：{self._message_reference(watch.source, first_id)}\n"
                f"- 已有评论：{comment_count} 条\n"
                f"- 转发：成功 {result.success}，失败 {result.failed}，跳过 {result.skipped}"
            )
            if result.errors:
                text += "\n最近错误：\n" + "\n".join(f"- {item}" for item in result.errors[-5:])
            await self._notify_control_bot(text)
        else:
            LOGGER.info(
                "watchcomments success: source=%s message=%s comments=%s forwarded=%s",
                watch.source,
                first_id,
                comment_count,
                result.success,
            )
        if comment_count == 0:
            self._schedule_comments_recheck(watch, first_id)

    def _schedule_comments_recheck(self, watch: Any, message_id: int) -> None:
        key = (str(watch.source), int(message_id))
        if key in self.pending_comment_rechecks:
            return
        self.pending_comment_rechecks.add(key)
        task = asyncio.create_task(
            self._delayed_comments_recheck(
                str(watch.source), int(watch.peer_id), int(message_id), key
            )
        )
        task.add_done_callback(self._finish_comments_recheck)

    def _finish_comments_recheck(self, task: asyncio.Task[Any]) -> None:
        try:
            key = task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            LOGGER.exception("watchcomments delayed recheck failed")
            return
        self.pending_comment_rechecks.discard(key)

    async def _delayed_comments_recheck(
        self, source: str, channel_peer_id: int, message_id: int, key: tuple[str, int]
    ) -> tuple[str, int]:
        try:
            for delay in WATCHCOMMENTS_RECHECK_DELAYS:
                await asyncio.sleep(delay)
                entity = await self._resolve_source(source)
                groups = await self._message_groups_from(entity, message_id, 1)
                if not groups:
                    return key
                messages, comment_count = await self._post_groups_with_comments(
                    entity, channel_peer_id, [groups[0]]
                )
                if comment_count <= 0:
                    continue
                result = await self._forward_many(f"{source}#with-comments", messages)
                self._record_watch_summary(
                    "comments",
                    success=result.success,
                    failed=result.failed,
                    skipped=result.skipped,
                )
                if result.failed:
                    await self._notify_control_bot(
                        f"watchcomments 延迟补查异常：{self._message_reference(source, message_id)}\n"
                        f"- 已有评论：{comment_count} 条\n"
                        f"- 转发：成功 {result.success}，失败 {result.failed}，跳过 {result.skipped}"
                    )
                else:
                    LOGGER.info(
                        "watchcomments delayed recheck success: source=%s message=%s comments=%s forwarded=%s skipped=%s",
                        source,
                        message_id,
                        comment_count,
                        result.success,
                        result.skipped,
                    )
                return key
            LOGGER.info(
                "watchcomments delayed recheck found no comments: source=%s message=%s",
                source,
                message_id,
            )
            return key
        finally:
            self.pending_comment_rechecks.discard(key)

    async def _process_resource_watch_group(self, source: str, group: list[Message]) -> None:
        link = self._message_link(source, int(group[0].id)) if group else None
        command_text = f"/resource {source} one from {link}" if link else f"/watchresource {source}"
        if self._resource_watch_source_busy(source):
            LOGGER.info(
                "watchresource source busy; delayed recheck scheduled: source=%s message=%s",
                source,
                group[0].id if group else None,
            )
            if group:
                self._schedule_resource_recheck(source, int(group[0].id))
            return
        self.active_resource_watch_sources.add(str(source))
        try:
            await self._run_recoverable_text(
                command_text,
                self._process_resource_watch_group_inner(source, group),
                dedupe_prefix=f"/resource {source} one from ",
            )
        finally:
            self.active_resource_watch_sources.discard(str(source))

    def _resource_watch_source_busy(self, source: str) -> bool:
        source = str(source)
        if source in self.active_resource_watch_sources:
            return True
        prefix = f"/resource {source} one from "
        if any(
            description.startswith(prefix)
            for task, description in self.active_command_tasks.items()
            if not task.done()
        ):
            return True
        return any(item.startswith(prefix) for item in self.db.pending_manual_commands())

    async def _watchresource_sweep_loop(self) -> None:
        await asyncio.sleep(30)
        while True:
            try:
                await self._sweep_recent_watchresources()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("watchresource sweep failed")
            await asyncio.sleep(WATCHRESOURCE_SWEEP_INTERVAL_SECONDS)

    async def _sweep_recent_watchresources(self) -> None:
        watches = [watch for watch in self.db.list_watches() if watch.mode == "resource"]
        for watch in watches:
            entity = await self._resolve_source(watch.source)
            groups = await self._recent_message_groups(entity, WATCHRESOURCE_SWEEP_GROUPS)
            for group in reversed(groups):
                await self._process_resource_watch_group(watch.source, group)

    async def _process_resource_watch_group_inner(
        self, source: str, group: list[Message]
    ) -> None:
        if not group:
            return
        first_id = int(group[0].id)
        if self._id_in_sparse_intervals(
            self.completed_resource_rechecks.get(str(source), []), first_id
        ):
            return
        entity = await self._resolve_source(source)
        grouped_links, ignored, *_ = await self._resource_link_groups(
            entity, source, [group]
        )
        if not grouped_links:
            if ignored:
                LOGGER.info(
                    "watchresource ignored non-whitelisted links: source=%s message=%s count=%s",
                    source,
                    group[0].id if group else None,
                    len(ignored),
                )
            if self._resource_forwardable_originals(group):
                self._schedule_resource_recheck(source, int(group[0].id))
            return
        first_group, _ = grouped_links[0]
        first_id = int(first_group[0].id)
        await self._forward_resource_watch_links(source, first_id, grouped_links)
        self._schedule_resource_recheck(source, first_id)

    async def _forward_resource_watch_links(
        self,
        source: str,
        first_id: int,
        grouped_links: list[tuple[list[Message], list[ResourceBotLink]]],
    ) -> None:
        original_total = ForwardResult()
        success = duplicate = failed = skipped = collected = forwarded = 0
        errors: list[str] = []
        for original_group, links in grouped_links:
            original_group = self._resource_forwardable_originals(original_group)
            item = await self._forward_many(source, original_group)
            self._merge_forward_result(original_total, item)
            await self._pause_after_forward(item)
            for link in links:
                outcome = await self._process_resource_bot_link(link)
                if outcome.status == "duplicate":
                    duplicate += 1
                elif outcome.status == "skipped":
                    skipped += 1
                elif outcome.status == "failed":
                    failed += 1
                    errors.append(outcome.text)
                else:
                    success += 1
                collected += outcome.collected
                forwarded += outcome.forwarded
        if (
            duplicate
            and not success
            and not failed
            and not skipped
            and not collected
            and not forwarded
            and not original_total.success
            and not original_total.failed
            and not original_total.skipped
        ):
            LOGGER.info(
                "watchresource duplicate only: source=%s message=%s duplicate=%s",
                source,
                first_id,
                duplicate,
            )
            return
        self._record_watch_summary(
            "resource",
            success=forwarded + original_total.success,
            failed=failed + original_total.failed,
            skipped=skipped + original_total.skipped + duplicate,
        )
        if errors or failed or skipped or original_total.failed:
            text = (
                f"watchresource 异常：{self._message_reference(source, first_id)}\n"
                f"- 原帖：成功 {original_total.success}，失败 {original_total.failed}，"
                f"跳过 {original_total.skipped}\n"
                f"- 链接：成功 {success}，重复 {duplicate}，跳过 {skipped}，失败 {failed}\n"
                f"- 资源媒体：收集 {collected}，转发 {forwarded}"
            )
            if errors:
                text += "\n最近错误：\n" + "\n".join(f"- {item}" for item in errors[-5:])
            await self._notify_control_bot(text)
        else:
            LOGGER.info(
                "watchresource success: source=%s message=%s original=%s links=%s media=%s duplicate=%s",
                source,
                first_id,
                original_total.success,
                success,
                forwarded,
                duplicate,
            )

    def _schedule_resource_recheck(self, source: str, message_id: int) -> None:
        source = str(source)
        message_id = int(message_id)
        if self._id_in_sparse_intervals(
            self.completed_resource_rechecks.get(source, []), message_id
        ):
            return
        intervals = self.pending_resource_rechecks.get(source, [])
        self.pending_resource_rechecks[source] = self._merge_sparse_intervals(
            [*intervals, (message_id, message_id)]
        )
        self._save_resource_recheck_ranges()
        task = self.resource_recheck_tasks.get(source)
        if task is not None and not task.done():
            return
        self._start_resource_recheck_task(source)

    def _start_resource_recheck_task(self, source: str) -> None:
        task = asyncio.create_task(self._delayed_resource_recheck(source))
        self.resource_recheck_tasks[source] = task
        task.add_done_callback(
            lambda finished, scheduled_source=source: self._finish_resource_recheck(
                scheduled_source, finished
            )
        )

    def _save_resource_recheck_ranges(self) -> None:
        self.db.set_state(
            RESOURCE_RECHECK_STATE_KEY,
            json.dumps(self.pending_resource_rechecks, ensure_ascii=False),
        )
        self.db.set_state(
            RESOURCE_RECHECK_DONE_STATE_KEY,
            json.dumps(self.completed_resource_rechecks, ensure_ascii=False),
        )

    def _resume_resource_rechecks(self) -> None:
        raw = self.db.get_state(RESOURCE_RECHECK_STATE_KEY, "")
        try:
            saved = json.loads(raw) if raw else {}
            restored: dict[str, list[tuple[int, int]]] = {}
            for source, value in saved.items():
                if not isinstance(value, list):
                    continue
                raw_intervals = (
                    [value]
                    if len(value) == 2 and all(isinstance(item, int) for item in value)
                    else value
                )
                intervals = [
                    (int(bounds[0]), int(bounds[1]))
                    for bounds in raw_intervals
                    if isinstance(bounds, list) and len(bounds) == 2
                ]
                if intervals:
                    restored[str(source)] = self._merge_sparse_intervals(intervals)
            self.pending_resource_rechecks = restored
            completed_raw = self.db.get_state(RESOURCE_RECHECK_DONE_STATE_KEY, "")
            completed_saved = json.loads(completed_raw) if completed_raw else {}
            self.completed_resource_rechecks = {
                str(source): self._merge_sparse_intervals(
                    [(int(bounds[0]), int(bounds[1])) for bounds in intervals]
                )
                for source, intervals in completed_saved.items()
                if isinstance(intervals, list)
            }
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            LOGGER.warning("Ignoring invalid persisted watchresource recheck ranges")
            self.pending_resource_rechecks = {}
            self.completed_resource_rechecks = {}
            self._save_resource_recheck_ranges()
            return
        for source in self.pending_resource_rechecks:
            self._start_resource_recheck_task(source)

    @staticmethod
    def _merge_sparse_intervals(
        intervals: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        merged: list[tuple[int, int]] = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    @staticmethod
    def _remove_sparse_interval(
        intervals: list[tuple[int, int]], start_id: int, end_id: int
    ) -> list[tuple[int, int]]:
        remaining: list[tuple[int, int]] = []
        for start, end in intervals:
            if end < start_id or start > end_id:
                remaining.append((start, end))
                continue
            if start < start_id:
                remaining.append((start, start_id - 1))
            if end > end_id:
                remaining.append((end_id + 1, end))
        return remaining

    @staticmethod
    def _id_in_sparse_intervals(
        intervals: list[tuple[int, int]], message_id: int
    ) -> bool:
        return any(start <= message_id <= end for start, end in intervals)

    def _finish_resource_recheck(
        self, source: str, task: asyncio.Task[Any]
    ) -> None:
        failed = False
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            failed = True
            LOGGER.exception("watchresource delayed recheck failed")
        finally:
            if self.resource_recheck_tasks.get(source) is task:
                self.resource_recheck_tasks.pop(source, None)
        if failed and source in self.pending_resource_rechecks:
            self._start_resource_recheck_task(source)

    async def _delayed_resource_recheck(
        self, source: str
    ) -> str:
        while source in self.pending_resource_rechecks:
            start_id, end_id = self.pending_resource_rechecks[source][0]
            for delay in WATCHRESOURCE_RECHECK_DELAYS:
                await asyncio.sleep(delay)
                busy_checks = 0
                while self._resource_watch_source_busy(source):
                    busy_checks += 1
                    if busy_checks == 1:
                        LOGGER.info(
                            "watchresource recheck waiting: source=%s range=%s-%s",
                            source,
                            start_id,
                            end_id,
                        )
                    await asyncio.sleep(WATCHRESOURCE_BUSY_RECHECK_DELAY_SECONDS)
                if busy_checks:
                    LOGGER.info(
                        "watchresource recheck resumed: source=%s range=%s-%s waited_checks=%s",
                        source,
                        start_id,
                        end_id,
                        busy_checks,
                    )
                entity = await self._resolve_source(source)
                groups: list[list[Message]] = []
                async for group in self._iter_message_groups_from(entity, start_id):
                    if int(group[0].id) > end_id:
                        break
                    groups.append(group)
                grouped_links, ignored, *_ = await self._resource_link_groups(
                    entity, source, groups
                )
                if not grouped_links:
                    if ignored:
                        LOGGER.info(
                            "watchresource recheck ignored non-whitelisted links: source=%s range=%s-%s count=%s",
                            source,
                            start_id,
                            end_id,
                            len(ignored),
                        )
                    continue
                self.active_resource_watch_sources.add(str(source))
                try:
                    await self._forward_resource_watch_links(
                        source, int(grouped_links[0][0][0].id), grouped_links
                    )
                finally:
                    self.active_resource_watch_sources.discard(str(source))
                LOGGER.info(
                    "watchresource recheck processed: source=%s range=%s-%s links=%s",
                    source,
                    start_id,
                    end_id,
                    sum(len(links) for _, links in grouped_links),
                )
            current = self.pending_resource_rechecks.get(source, [])
            remaining = self._remove_sparse_interval(current, start_id, end_id)
            if remaining:
                self.pending_resource_rechecks[source] = remaining
            else:
                self.pending_resource_rechecks.pop(source, None)
            completed = self.completed_resource_rechecks.get(source, [])
            self.completed_resource_rechecks[source] = self._merge_sparse_intervals(
                [*completed, (start_id, end_id)]
            )
            self._save_resource_recheck_ranges()
        return source

    async def _process_code_watch_group(self, watch: Any, group: list[Message]) -> None:
        link = self._message_link(watch.source, int(group[0].id)) if group else None
        extract = str(watch.linked_peer_id or watch.linked_title or "")
        command_text = (
            f"/code {watch.source} {extract} from {link}"
            if link and extract
            else f"/watchcode {watch.source} {extract}".strip()
        )
        await self._run_recoverable_text(
            command_text, self._process_code_watch_group_inner(watch, group)
        )

    async def _process_code_watch_group_inner(self, watch: Any, group: list[Message]) -> None:
        if watch.linked_peer_id is None:
            await self._notify_control_bot(f"watchcode 配置缺少提取频道：{watch.source}")
            return
        extract_entity = await self._entity_from_dialogs(int(watch.linked_peer_id))
        if extract_entity is None:
            extract_entity = await self._resolve_source(str(watch.linked_peer_id))
        code_message = group[0]
        first_id = int(code_message.id)
        original = await self._forward_many(watch.source, [code_message])
        await self._pause_after_forward(original)
        resources = await self._extract_code_resources(watch.source, code_message, extract_entity)
        result = await self._forward_many(f"code:{watch.source}:{first_id}", resources)
        self._record_watch_summary(
            "code",
            success=original.success + result.success,
            failed=original.failed + result.failed,
            skipped=original.skipped + result.skipped,
        )
        if original.failed or result.failed or original.skipped or result.skipped:
            await self._notify_control_bot(
                f"watchcode 异常：{self._message_reference(watch.source, first_id)}\n"
                f"- 原消息：成功 {original.success}，失败 {original.failed}，跳过 {original.skipped}\n"
                f"- 资源：成功 {result.success}，失败 {result.failed}，跳过 {result.skipped}"
            )
        else:
            LOGGER.info(
                "watchcode success: source=%s message=%s original=%s resources=%s",
                watch.source,
                first_id,
                original.success,
                result.success,
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
        if watch.mode != "comments":
            LOGGER.info(
                "watch linked skip: chat=%s message=%s source=%s reason=watch mode is %s",
                chat_id,
                message.id,
                watch.source,
                watch.mode,
            )
            return None
        is_comment, reason = await self._channel_comment_status(message, watch.peer_id)
        if not is_comment:
            LOGGER.info(
                "watchcomments skip: source=%s linked_chat=%s message=%s reason=%s",
                watch.source,
                chat_id,
                message.id,
                reason,
            )
            return None
        # Keep the discussion peer ID so failures can point at the exact
        # comment message rather than at the linked channel post.
        return f"{watch.source}#comments@{chat_id}"

    async def _is_channel_comment(self, message: Message, channel_peer_id: int) -> bool:
        is_comment, _ = await self._channel_comment_status(message, channel_peer_id)
        return is_comment

    async def _channel_comment_status(
        self, message: Message, channel_peer_id: int
    ) -> tuple[bool, str]:
        reply = message.reply_to
        if reply is None:
            return False, "no reply_to"
        root_id = reply.reply_to_top_id or reply.reply_to_msg_id
        if root_id is None or message.chat_id is None:
            return False, "missing root_id or chat_id"
        cache_key = (int(message.chat_id), int(root_id), channel_peer_id)
        if cache_key in self.valid_comment_roots:
            return True, "cached"
        current_id = int(root_id)
        visited: set[int] = set()
        last_reason = ""
        for depth in range(6):
            if current_id in visited:
                return False, f"reply chain loop at {current_id}"
            visited.add(current_id)
            while True:
                try:
                    root = await self.client.get_messages(message.chat_id, ids=current_id)
                    break
                except FloodWaitError as exc:
                    await self._sleep_for_flood_wait(
                        "读取评论区回复链："
                        f"评论 {self._message_reference(str(message.chat_id), int(message.id))}，"
                        f"祖先消息 {current_id}",
                        exc,
                    )
            if root is None:
                return False, f"reply ancestor {current_id} not found"
            if root.fwd_from is not None and root.fwd_from.from_id is not None:
                forward_peer_id = int(utils.get_peer_id(root.fwd_from.from_id))
                if forward_peer_id == channel_peer_id:
                    self.valid_comment_roots.add(cache_key)
                    self.valid_comment_roots.add(
                        (int(message.chat_id), int(current_id), channel_peer_id)
                    )
                    return True, f"matched at depth {depth}"
                return (
                    False,
                    f"ancestor {current_id} forward source {forward_peer_id} != {channel_peer_id}",
                )
            parent = root.reply_to
            parent_id = None
            if parent is not None:
                parent_id = parent.reply_to_top_id or parent.reply_to_msg_id
            if parent_id is None:
                last_reason = f"ancestor {current_id} has no channel forward header or parent"
                break
            last_reason = f"ancestor {current_id} has no channel forward header; follow {parent_id}"
            current_id = int(parent_id)
        return False, last_reason or f"root {root_id} has no channel forward header"

    async def _forward_many(
        self,
        source: str,
        messages: Iterable[Message | None],
        expected_ids: Iterable[int] | None = None,
        force: bool = False,
    ) -> ForwardResult:
        items = list(messages)
        ids = list(expected_ids) if expected_ids is not None else []
        result = ForwardResult()
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
                service_messages = [
                    item for item in group if isinstance(item, MessageService)
                ]
                for item in service_messages:
                    self._record_skip(result, source, int(item.id), "服务消息不可转发")
                group = [
                    item for item in group if not isinstance(item, MessageService)
                ]
                if not group:
                    group_size = len(service_messages)
                elif not force and all(
                    self.db.forward_was_successful(source, int(item.id))
                    for item in group
                ):
                    result.skipped += len(group)
                    LOGGER.info(
                        "forward skipped duplicate: source=%s message_ids=%s",
                        source,
                        ",".join(str(item.id) for item in group),
                    )
                else:
                    while True:
                        async with self.forward_lock:
                            completed = await self._forward_group(source, group, result)
                        if completed is not False:
                            break
                        await asyncio.sleep(2)
                if group:
                    group_size = len(group) + len(service_messages)
            processed_in_batch += group_size
            if index < len(items):
                if processed_in_batch >= self.config.forward_batch_size:
                    await asyncio.sleep(
                        random.uniform(
                            self.config.forward_batch_pause_min_seconds,
                            self.config.forward_batch_pause_max_seconds,
                        )
                    )
                    processed_in_batch = 0
                else:
                    await asyncio.sleep(
                        random.uniform(
                            self.config.forward_interval_min_seconds,
                            self.config.forward_interval_max_seconds,
                        )
                    )
        return result

    async def _forward_group(
        self, source: str, messages: list[Message], result: ForwardResult
    ) -> bool:
        while True:
            try:
                payload: Message | list[Message] = messages[0] if len(messages) == 1 else messages
                await self._wait_for_forward_slot()
                await asyncio.wait_for(
                    self.client.forward_messages("me", payload),
                    FORWARD_REQUEST_TIMEOUT_SECONDS,
                )
                result.success += len(messages)
                for message in messages:
                    self.db.log_forward(source, message.id, "success")
                self.db.set_state("last_forward_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
                return True
            except TimeoutError:
                LOGGER.warning(
                    "forward request timed out; releasing lock before retry: source=%s message_ids=%s",
                    source,
                    ",".join(str(message.id) for message in messages),
                )
                return False
            except FloodWaitError as exc:
                wait_seconds = int(exc.seconds) + 1
                first_message_id = int(messages[0].id)
                reference = self._message_reference(source, first_message_id)
                LOGGER.warning("FloodWait for %d seconds", wait_seconds)
                self._remember_error(exc)
                await self._notify_flood_wait(
                    f"转发消息：{reference}"
                    + (f" 等 {len(messages)} 条" if len(messages) > 1 else ""),
                    exc,
                    will_retry=True,
                )
                await asyncio.sleep(wait_seconds)
            except (ChatForwardsRestrictedError, MessageIdInvalidError, ChannelPrivateError, ChatAdminRequiredError) as exc:
                for message in messages:
                    self._record_skip(result, source, message.id, self._error_text(exc))
                return True
            except RPCError as exc:
                for message in messages:
                    self._record_failure(result, source, message.id, exc)
                return True
            except Exception as exc:
                LOGGER.exception(
                    "Unexpected forwarding failure for %s/%s",
                    source,
                    ",".join(str(message.id) for message in messages),
                )
                for message in messages:
                    self._record_failure(result, source, message.id, exc)
                return True

    def _record_skip(self, result: ForwardResult, source: str, message_id: int, reason: str) -> None:
        reference = self._message_reference(source, message_id)
        result.skipped += 1
        result.errors.append(f"{reference}\n原因：{reason}")
        self.db.log_forward(source, message_id, "skipped", reason)
        self.db.set_state("last_error", f"{reference}\n原因：{reason}")

    def _record_failure(self, result: ForwardResult, source: str, message_id: int, exc: Exception) -> None:
        reason = self._error_text(exc)
        reference = self._message_reference(source, message_id)
        result.failed += 1
        result.errors.append(f"{reference}\n原因：{reason}")
        self.db.log_forward(source, message_id, "failed", reason)
        self.db.set_state("last_error", f"{reference}\n原因：{reason}")

    @staticmethod
    def _message_reference(source: str, message_id: int) -> str:
        """Describe a failed message and include its Telegram link when possible."""
        link_source = source
        comments_marker = "#comments@"
        if comments_marker in source:
            link_source = source.split(comments_marker, 1)[1]
        else:
            link_source = source.split("#", 1)[0]

        link = TelegramSaveHelper._message_link(link_source, message_id)
        reference = f"来源 {source}，消息 {message_id}"
        return f"{reference}\n链接：{link}" if link else reference

    @staticmethod
    def _message_link(source: str, message_id: int) -> str | None:
        if "#comments@" in source:
            source = source.split("#comments@", 1)[1]
        else:
            source = source.split("#", 1)[0]
        link_source = source.strip().rstrip("/")
        if re.fullmatch(r"@[A-Za-z0-9_]+", link_source):
            return f"https://t.me/{link_source[1:]}/{message_id}"
        public_match = re.fullmatch(
            r"https?://(?:www\.)?t\.me/(?:s/)?([A-Za-z0-9_]+)",
            link_source,
        )
        if public_match:
            return f"https://t.me/{public_match.group(1)}/{message_id}"
        if re.fullmatch(r"-100\d+", link_source):
            return f"https://t.me/c/{link_source[4:]}/{message_id}"
        if re.fullmatch(r"-\d+", link_source):
            return f"https://t.me/c/{link_source[1:]}/{message_id}"
        return None

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
            for peer_id in self._numeric_source_candidates(value):
                entity = await self._entity_from_dialogs(peer_id)
                if entity is not None:
                    return entity
        try:
            return await self.client.get_entity(value)
        except (ValueError, TypeError, RPCError) as exc:
            if isinstance(value, int):
                for peer_id in self._numeric_source_candidates(value):
                    entity = await self._entity_from_dialogs(peer_id)
                    if entity is not None:
                        return entity
                    if peer_id != value:
                        try:
                            return await self.client.get_entity(peer_id)
                        except (ValueError, TypeError, RPCError):
                            pass
            raise CommandError(f"无法访问 source {source}：{self._error_text(exc)}") from exc

    async def _reply(self, event: events.NewMessage.Event, text: str) -> None:
        if getattr(event, "client", None) is self.client:
            if await self._notify_control_bot(text):
                return
            await self.client.send_message("me", text, reply_to=event.id)
            return
        await event.respond(text, reply_to=event.id)

    async def _notify_control_bot(self, text: str) -> bool:
        if self.bot_client is None or self.bot_owner_id is None:
            return False
        try:
            await self.bot_client.send_message(self.bot_owner_id, text)
            return True
        except Exception:
            LOGGER.exception("Failed to send control bot notification")
            return False

    async def _sleep_for_flood_wait(self, context: str, exc: FloodWaitError) -> None:
        wait_seconds = int(exc.seconds) + 1
        self._remember_error(exc)
        await self._notify_flood_wait(context, exc, will_retry=True)
        await asyncio.sleep(wait_seconds)

    async def _notify_flood_wait(
        self, context: str, exc: FloodWaitError, *, will_retry: bool = False
    ) -> None:
        seconds = int(exc.seconds)
        resume_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        self.db.set_state("telegram_floodwait_until", resume_at.isoformat(timespec="seconds"))
        self.next_forward_at = max(
            self.next_forward_at, time.monotonic() + seconds
        )
        self._set_task_status(
            state="等待 FloodWait",
            current=context,
            resume_at=resume_at.astimezone().isoformat(timespec="seconds"),
        )
        wait_text = f"{seconds // 60} 分 {seconds % 60} 秒"
        retry_text = (
            f"\n- 处理方式：已暂停当前任务，将在约 {wait_text} 后继续尝试；"
            f"预计时间：{resume_at.astimezone().isoformat(timespec='seconds')}。"
            if will_retry
            else ""
        )
        command_context = CURRENT_COMMAND_CONTEXT.get()
        command_text = f"\n- 触发命令：{command_context}" if command_context else ""
        text = (
            f"Telegram 限流：{context}\n"
            f"{command_text}\n"
            f"- 需要等待：{seconds} 秒（约 {wait_text}）\n"
            f"- 预计可重试：{resume_at.astimezone().isoformat(timespec='seconds')}\n"
            f"- 错误：{self._error_text(exc)}"
            f"{retry_text}"
        )
        await self._notify_control_bot(text)
