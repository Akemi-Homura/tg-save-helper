from __future__ import annotations

import tempfile
import unittest
import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.db import Database
from src.telegram_client import TelegramSaveHelper
from src.telegram_client import ForwardResult
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
    async def test_forward_lock_is_released_between_logical_posts(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.forward_lock = asyncio.Lock()
        helper.config = SimpleNamespace(
            forward_batch_size=50,
            forward_interval_min_seconds=0,
            forward_interval_max_seconds=0,
            forward_batch_pause_min_seconds=0,
            forward_batch_pause_max_seconds=0,
        )
        helper.db = SimpleNamespace(forward_was_successful=lambda *_args: False)
        order: list[str] = []

        async def forward(source, _group, result):
            order.append(source)
            result.success += 1
            await asyncio.sleep(0)

        helper._forward_group = forward
        first = [SimpleNamespace(id=1, grouped_id=None), SimpleNamespace(id=2, grouped_id=None)]
        second = [SimpleNamespace(id=3, grouped_id=None)]

        await asyncio.gather(
            helper._forward_many("first", first),
            helper._forward_many("second", second),
        )

        self.assertEqual(order, ["first", "second", "first"])

    async def test_forward_timeout_releases_lock_before_retry(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.forward_lock = asyncio.Lock()
        helper.config = SimpleNamespace(
            forward_batch_size=50,
            forward_interval_min_seconds=0,
            forward_interval_max_seconds=0,
            forward_batch_pause_min_seconds=0,
            forward_batch_pause_max_seconds=0,
        )
        helper.db = SimpleNamespace(forward_was_successful=lambda *_args: False)
        order: list[str] = []
        first_attempts = 0

        async def forward(source, _group, result):
            nonlocal first_attempts
            order.append(source)
            if source == "first" and first_attempts == 0:
                first_attempts += 1
                return False
            result.success += 1
            return True

        helper._forward_group = forward
        real_sleep = asyncio.sleep
        async def yield_sleep(_delay: float) -> None:
            await real_sleep(0)

        with patch("src.telegram_client.asyncio.sleep", new=yield_sleep):
            await asyncio.gather(
                helper._forward_many("first", [SimpleNamespace(id=1, grouped_id=None)]),
                helper._forward_many("second", [SimpleNamespace(id=2, grouped_id=None)]),
            )

        self.assertEqual(order, ["first", "second", "first"])

    async def test_watch_all_uses_watch_checkpoint_and_updates_progress(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.task_status = {}
        helper._reply = AsyncMock()
        helper._pause_after_forward = AsyncMock()
        helper._forward_many = AsyncMock(return_value=ForwardResult(success=1))
        helper._checkpoint_from_command = Mock()

        async def groups(_entity, _start):
            yield [SimpleNamespace(id=8)]

        helper._iter_message_groups_from = groups
        await helper._forward_last_stream(
            SimpleNamespace(),
            object(),
            "-2312388706",
            8,
            force=False,
            checkpoint_command="/watch",
        )

        self.assertEqual(
            helper._checkpoint_from_command.call_args_list,
            [
                unittest.mock.call("/watch", "-2312388706", 8, None, False),
                unittest.mock.call("/watch", "-2312388706", 9, None, False),
            ],
        )
        status = helper.task_status[asyncio.current_task()]
        self.assertEqual(status["processed"], 1)
        self.assertEqual(status["success"], 1)
        self.assertIn("消息 8", status["current"])

    async def test_forward_gate_shares_batch_quiet_period(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.forward_rate_lock = asyncio.Lock()
        helper.next_forward_at = 0.0
        helper.config = SimpleNamespace(
            forward_interval_min_seconds=20,
            forward_interval_max_seconds=35,
            forward_batch_pause_min_seconds=90,
            forward_batch_pause_max_seconds=150,
        )
        with (
            patch("src.telegram_client.random.uniform", side_effect=[90.0, 20.0]),
            patch("src.telegram_client.asyncio.sleep", new_callable=AsyncMock) as sleep,
        ):
            await helper._wait_for_forward_slot(batch=True)
            await helper._wait_for_forward_slot()

        sleep.assert_awaited_once()
        self.assertGreaterEqual(sleep.await_args.args[0], 89.0)
        self.assertLessEqual(sleep.await_args.args[0], 90.0)
        self.assertGreater(helper.next_forward_at, 0.0)

    def test_forward_gate_restores_persisted_floodwait(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.next_forward_at = 0.0
        future = datetime.now(timezone.utc) + timedelta(seconds=60)
        helper.db = SimpleNamespace(
            get_state=lambda key, default: future.isoformat(timespec="seconds")
        )

        helper._restore_forward_gate()

        remaining = helper.next_forward_at - time.monotonic()
        self.assertGreaterEqual(remaining, 58)
        self.assertLessEqual(remaining, 60)

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

    async def test_saved_watch_sweep_advances_persistent_position(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.saved_stream_lock = asyncio.Lock()
        helper.saved_backup_lock = asyncio.Lock()
        positions: list[tuple[str, int]] = []
        helper.db = SimpleNamespace(
            update_saved_watch_position=lambda mode, message_id: positions.append((mode, message_id))
        )

        async def groups(_count, _start):
            yield [SimpleNamespace(id=11)]

        async def process(_message, _force):
            return ForwardResult(success=1)

        helper._iter_saved_history_groups = groups
        helper._stream_saved_video = process
        helper._notify_control_bot = AsyncMock()
        await helper._sweep_saved_watch("stream", 11)
        self.assertEqual(positions, [("stream", 11)])
