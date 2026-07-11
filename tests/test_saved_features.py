from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.db import Database
from src.telegram_client import TelegramSaveHelper
from telethon.tl.types import DocumentAttributeVideo


class _HistoryClient:
    def __init__(self, messages: list[SimpleNamespace]) -> None:
        self.messages = messages
        self.consumed = 0

    def iter_messages(self, *_args, **_kwargs):
        async def iterator():
            for message in self.messages:
                self.consumed += 1
                yield message

        return iterator()


class SavedFeatureTest(unittest.IsolatedAsyncioTestCase):
    def test_database_records_backup_stream_and_watch_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.save_backup_result(10, None, -1001, 20, "success")
            db.save_stream_state(10, "running", "uploading", local_output="/tmp/a.mp4")
            db.set_saved_watch("backup", True, "/watchsaved all", 10)
            db.set_saved_watch("backup", True, "/watchsaved from 11")
            self.assertEqual(db.saved_backup_count(), 1)
            self.assertEqual(db.saved_backup_row(10)["destination_message_id"], 20)
            self.assertEqual(db.saved_stream_row(10)["stage"], "uploading")
            self.assertEqual(db.saved_watch("backup")["last_message_id"], 10)

    async def test_all_history_yields_before_scanning_every_message(self) -> None:
        messages = [
            SimpleNamespace(id=1, grouped_id=None),
            SimpleNamespace(id=2, grouped_id=None),
            SimpleNamespace(id=3, grouped_id=None),
        ]
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.client = _HistoryClient(messages)
        groups = helper._iter_saved_history_groups(None, None)
        first = await anext(groups)
        self.assertEqual([message.id for message in first], [1])
        self.assertEqual(helper.client.consumed, 2)

    async def test_history_keeps_album_together(self) -> None:
        messages = [
            SimpleNamespace(id=1, grouped_id=99),
            SimpleNamespace(id=2, grouped_id=99),
            SimpleNamespace(id=3, grouped_id=None),
        ]
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.client = _HistoryClient(messages)
        groups = helper._iter_saved_history_groups(None, None)
        first = await anext(groups)
        self.assertEqual([message.id for message in first], [1, 2])

    async def test_backup_batches_without_splitting_album(self) -> None:
        async def groups():
            yield [SimpleNamespace(id=1)] * 99
            yield [SimpleNamespace(id=2), SimpleNamespace(id=3)]

        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        batches = helper._batch_saved_history(groups())
        self.assertEqual(len(await anext(batches)), 99)
        self.assertEqual(len(await anext(batches)), 2)

    def test_streaming_reply_is_not_converted_again(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.saved_generated_message_ids = set()
        helper.db = SimpleNamespace(
            saved_stream_row=lambda message_id: {"stage": "uploading"} if message_id == 10 else None
        )
        message = SimpleNamespace(id=11, reply_to_msg_id=10)
        self.assertTrue(helper._is_generated_stream_message(message))

    def test_already_streamable_video_is_detected(self) -> None:
        message = SimpleNamespace(
            media=SimpleNamespace(
                document=SimpleNamespace(
                    attributes=[DocumentAttributeVideo(1, 10, 10, supports_streaming=True)]
                )
            )
        )
        self.assertTrue(TelegramSaveHelper._video_already_streamable(message))

    async def test_invalid_video_probe_is_distinct_from_transcode_needed(self) -> None:
        process = SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(b"", b"bad")))
        with patch("src.telegram_client.asyncio.create_subprocess_exec", return_value=process):
            helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
            self.assertIsNone(await helper._stream_copy_compatible(Path("broken.mp4")))
