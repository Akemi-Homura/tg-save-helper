from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.telegram_client import ResourceBotLink, TelegramSaveHelper


class WatchCommentsRecheckTest(unittest.IsolatedAsyncioTestCase):
    async def test_resource_link_in_discussion_comment_belongs_to_original_post(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        original = SimpleNamespace(id=355, reply_to_msg_id=None)
        comment = SimpleNamespace(id=6, reply_to_msg_id=3)
        link = ResourceBotLink(
            bot_username="zyck6948bot",
            payload="7549756109_BMXeEtWB",
            url="https://t.me/zyck6948bot?start=7549756109_BMXeEtWB",
            source="https://t.me/mijianqjlj",
            source_message_id=6,
        )
        helper._resource_comment_messages = AsyncMock(return_value=[comment])
        helper._extract_resource_bot_links = Mock(
            side_effect=lambda message, _source, ignored_links=None: [link]
            if message.id == 6
            else []
        )

        grouped, ignored, duplicates, direct, replies, missing = (
            await helper._resource_link_groups(
                object(), "https://t.me/mijianqjlj", [[original]]
            )
        )

        self.assertEqual(ignored, [])
        self.assertEqual((duplicates, direct, replies, missing), (0, 0, 1, 0))
        self.assertEqual(grouped[0][0], [original])
        self.assertEqual(len(grouped[0][1]), 1)
        self.assertEqual(grouped[0][1][0].source_message_id, 355)
        self.assertEqual(grouped[0][1][0].entry_message_id, 6)

    async def test_all_distinct_resource_links_in_comments_are_kept(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        original = SimpleNamespace(id=355, reply_to_msg_id=None)
        comments = [
            SimpleNamespace(id=6, reply_to_msg_id=3),
            SimpleNamespace(id=7, reply_to_msg_id=3),
        ]
        links = {
            6: ResourceBotLink("bot1", "first", "https://t.me/bot1?start=first", "s", 6),
            7: ResourceBotLink("bot2", "second", "https://t.me/bot2?start=second", "s", 7),
        }
        helper._resource_comment_messages = AsyncMock(return_value=comments)
        helper._extract_resource_bot_links = Mock(
            side_effect=lambda message, _source, ignored_links=None: [links[message.id]]
            if message.id in links
            else []
        )

        grouped, *_ = await helper._resource_link_groups(object(), "source", [[original]])

        self.assertEqual(
            [(item.bot_username, item.payload) for item in grouped[0][1]],
            [("bot1", "first"), ("bot2", "second")],
        )

    def test_message_reference_keeps_link_clickable(self) -> None:
        reference = TelegramSaveHelper._message_reference("@beigh6", 1)
        self.assertEqual(reference, "来源 @beigh6，消息 1\n链接：https://t.me/beigh6/1")
        self.assertNotIn("https://t.me/beigh6/1）", reference)

    def test_stream_disk_guard_reserves_space_below_ninety_percent(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.config = type("Config", (), {"saved_media_path": Path("/tmp")})()
        with patch("src.telegram_client.shutil.disk_usage") as usage:
            usage.return_value = type(
                "Usage", (), {"total": 1000, "used": 800, "free": 200}
            )()
            self.assertFalse(helper._stream_disk_safe(100))

    async def test_recheck_is_deduped_per_source_and_message(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.pending_comment_rechecks = set()
        calls: list[tuple[str, int, int, tuple[str, int]]] = []
        release = asyncio.Event()

        async def fake_recheck(
            source: str, peer_id: int, message_id: int, key: tuple[str, int]
        ) -> tuple[str, int]:
            calls.append((source, peer_id, message_id, key))
            await release.wait()
            helper.pending_comment_rechecks.discard(key)
            return key

        helper._delayed_comments_recheck = fake_recheck  # type: ignore[method-assign]
        watch = SimpleNamespace(source="@OFbozhu", peer_id=-1001813567803)

        helper._schedule_comments_recheck(watch, 461)
        helper._schedule_comments_recheck(watch, 461)
        await asyncio.sleep(0)

        self.assertEqual(calls, [("@OFbozhu", -1001813567803, 461, ("@OFbozhu", 461))])
        self.assertEqual(helper.pending_comment_rechecks, {("@OFbozhu", 461)})

        release.set()
        await asyncio.sleep(0)
        self.assertEqual(helper.pending_comment_rechecks, set())

    async def test_resource_recheck_is_deduped_per_source_and_message(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.pending_resource_rechecks = set()
        calls: list[tuple[str, int, tuple[str, int]]] = []
        release = asyncio.Event()

        async def fake_recheck(
            source: str, message_id: int, key: tuple[str, int]
        ) -> tuple[str, int]:
            calls.append((source, message_id, key))
            await release.wait()
            helper.pending_resource_rechecks.discard(key)
            return key

        helper._delayed_resource_recheck = fake_recheck  # type: ignore[method-assign]

        helper._schedule_resource_recheck("https://t.me/papashipin8", 756812)
        helper._schedule_resource_recheck("https://t.me/papashipin8", 756812)
        await asyncio.sleep(0)

        self.assertEqual(
            calls,
            [
                (
                    "https://t.me/papashipin8",
                    756812,
                    ("https://t.me/papashipin8", 756812),
                )
            ],
        )
        self.assertEqual(
            helper.pending_resource_rechecks,
            {("https://t.me/papashipin8", 756812)},
        )

        release.set()
        await asyncio.sleep(0)
        self.assertEqual(helper.pending_resource_rechecks, set())

    def test_resource_watch_source_busy_checks_active_and_pending_work(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.active_resource_watch_sources = {"https://t.me/papashipin8"}
        helper.active_command_tasks = {}
        helper.db = SimpleNamespace(pending_manual_commands=lambda: [])

        self.assertTrue(helper._resource_watch_source_busy("https://t.me/papashipin8"))
        self.assertFalse(helper._resource_watch_source_busy("https://t.me/jibahenyanga"))

        class Task:
            def done(self) -> bool:
                return False

        helper.active_resource_watch_sources = set()
        helper.active_command_tasks = {
            Task(): "/resource https://t.me/papashipin8 one from https://t.me/papashipin8/770382"
        }
        self.assertTrue(helper._resource_watch_source_busy("https://t.me/papashipin8"))

        helper.active_command_tasks = {}
        helper.db = SimpleNamespace(
            pending_manual_commands=lambda: [
                "/resource https://t.me/papashipin8 one from https://t.me/papashipin8/770382"
            ]
        )
        self.assertTrue(helper._resource_watch_source_busy("https://t.me/papashipin8"))

    async def test_resource_recheck_waits_for_busy_source_without_losing_link(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.pending_resource_rechecks = {("https://t.me/jibahenyanga", 5682)}
        helper.active_resource_watch_sources = set()
        group = [SimpleNamespace(id=5682)]
        links = [SimpleNamespace(bot_username="seliu", payload="j_69103756")]
        helper._resolve_source = AsyncMock(return_value=object())
        helper._resource_one_groups = AsyncMock(return_value=[group])
        helper._resource_link_groups = AsyncMock(
            return_value=([(group, links)], []),
        )
        helper._resource_watch_source_busy = Mock(
            side_effect=[True, True, False]
        )
        helper._forward_resource_watch_links = AsyncMock()

        with patch("src.telegram_client.asyncio.sleep", new_callable=AsyncMock) as sleep:
            key = await helper._delayed_resource_recheck(
                "https://t.me/jibahenyanga",
                5682,
                ("https://t.me/jibahenyanga", 5682),
            )

        self.assertEqual(key, ("https://t.me/jibahenyanga", 5682))
        self.assertEqual(sleep.await_count, 3)  # initial delay + two busy waits
        helper._forward_resource_watch_links.assert_awaited_once_with(
            "https://t.me/jibahenyanga", 5682, [(group, links)]
        )
        self.assertEqual(helper.pending_resource_rechecks, set())

    def test_resource_page_status_accepts_spaced_page_text(self) -> None:
        messages = [
            SimpleNamespace(raw_text="✅ 全部文件 第 1/3 页"),
            SimpleNamespace(raw_text="📄 全部文件\n分页导航 (第 2/3 页)"),
        ]

        self.assertEqual(TelegramSaveHelper._resource_page_status(messages), (2, 3))

    async def test_resource_collect_waits_for_delayed_navigation_message(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.config = SimpleNamespace(
            max_resource_bot_pages=3,
            max_resource_bot_wait_seconds=1,
            max_resource_bot_messages=100,
        )
        clicked: list[tuple[int, int, int]] = []

        class Button:
            def __init__(self, text: str) -> None:
                self.text = text

        class Message:
            def __init__(
                self,
                message_id: int,
                text: str = "",
                buttons: list[list[Button]] | None = None,
                file: object | None = None,
            ) -> None:
                self.id = message_id
                self.raw_text = text
                self.buttons = buttons
                self.file = file

            async def click(self, row: int, column: int) -> None:
                clicked.append((self.id, row, column))

        batches = [
            [Message(10, "✅ 全部文件 第 1/3 页")],
            [
                Message(10, "✅ 全部文件 第 1/3 页"),
                Message(
                    11,
                    "📄 全部文件\n分页导航 (第 1/3 页)",
                    [[Button("✅ 1"), Button("2"), Button("3")]],
                ),
            ],
            [Message(12, file=object()), Message(13, "✅ 全部文件已发送完毕")],
        ]

        async def fake_wait(bot: object, after_id: int) -> list[Message]:
            return batches.pop(0) if batches else []

        helper._wait_resource_bot_messages = fake_wait  # type: ignore[method-assign]

        media = await helper._collect_resource_bot_media(object(), 9)

        self.assertEqual(len(media), 1)
        self.assertEqual(clicked, [(11, 0, 1)])


if __name__ == "__main__":
    unittest.main()
