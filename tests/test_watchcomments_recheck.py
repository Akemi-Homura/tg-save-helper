from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from src.telegram_client import TelegramSaveHelper


class WatchCommentsRecheckTest(unittest.IsolatedAsyncioTestCase):
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
