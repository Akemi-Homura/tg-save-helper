from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any

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


if __name__ == "__main__":
    unittest.main()
