from __future__ import annotations

import asyncio
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.commands import CommandError
from src.telegram_client import ResourceBotLink, TelegramSaveHelper


class WatchCommentsRecheckTest(unittest.IsolatedAsyncioTestCase):
    async def test_recoverable_task_state_is_only_removed_for_command_errors(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        pending: list[str] = []
        helper.db = SimpleNamespace(
            add_pending_manual_command=lambda command: pending.append(command),
            remove_pending_manual_command=lambda command: pending.remove(command),
            pending_manual_commands=lambda: list(pending),
        )
        helper.active_command_tasks = {}
        helper.active_pending_commands = {}
        helper.task_status = {}

        async def fail(exc: Exception) -> None:
            raise exc

        with self.assertRaises(RuntimeError):
            await helper._run_recoverable_text(
                "/last @source all", fail(RuntimeError("temporary"))
            )
        self.assertEqual(pending, ["/last @source all"])

        pending.clear()
        with self.assertRaises(CommandError):
            await helper._run_recoverable_text(
                "/last @source all", fail(CommandError("invalid"))
            )
        self.assertEqual(pending, [])

    async def test_bot_menu_contains_every_supported_command(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)

        class BotClient:
            request = None

            async def __call__(self, request: object) -> None:
                self.request = request

        helper.bot_client = BotClient()
        await helper._set_bot_commands()

        actual = {"/" + item.command for item in helper.bot_client.request.commands}
        expected = {
            "/help", "/stop", "/last", "/unread", "/between", "/link",
            "/watch", "/unwatch", "/watchcomments", "/unwatchcomments",
            "/lastcomments", "/unreadcomments", "/resourcebot",
            "/resourcelink", "/resource", "/watchresource",
            "/unwatchresource", "/code", "/watchcode", "/unwatchcode",
            "/mixed", "/listwatch", "/status", "/tasks", "/stats",
            "/syncsaved", "/syncsaved_download", "/streamsaved",
            "/watchstreamsaved", "/unwatchstreamsaved", "/watchsaved",
            "/unwatchsaved", "/messageid",
        }
        self.assertEqual(actual, expected)
        self.assertTrue(all(re.fullmatch(r"/[a-z0-9_]+", item) for item in actual))

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

    async def test_resource_comment_timeout_retries_instead_of_stalling(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.exhausted_resource_comment_reads = set()
        comments = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        helper._resource_comments_for_post = AsyncMock(
            side_effect=[TimeoutError(), comments]
        )

        with patch(
            "src.telegram_client.asyncio.sleep", new_callable=AsyncMock
        ) as sleep:
            result = await helper._resource_comment_messages(
                object(), "source", [SimpleNamespace(id=522)]
            )

        self.assertEqual([item.id for item in result], [2, 1])
        self.assertEqual(helper._resource_comments_for_post.await_count, 2)
        sleep.assert_awaited_once_with(2)

    async def test_resource_comment_timeout_is_exhausted_after_three_attempts(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.exhausted_resource_comment_reads = set()
        helper._resource_comments_for_post = AsyncMock(
            side_effect=[TimeoutError(), TimeoutError(), TimeoutError()]
        )

        with patch(
            "src.telegram_client.asyncio.sleep", new_callable=AsyncMock
        ) as sleep:
            result = await helper._resource_comment_messages(
                object(), "https://t.me/papashipin8", [SimpleNamespace(id=90036)]
            )

        self.assertEqual(result, [])
        self.assertEqual(helper._resource_comments_for_post.await_count, 3)
        self.assertEqual(sleep.await_count, 2)
        self.assertEqual(
            helper.exhausted_resource_comment_reads,
            {("https://t.me/papashipin8", 90036)},
        )

    async def test_resource_wait_ignores_outgoing_start_message(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.config = SimpleNamespace(max_resource_bot_wait_seconds=1)

        class Client:
            def iter_messages(self, *_args, **_kwargs):
                async def messages():
                    yield SimpleNamespace(
                        id=10, out=True, raw_text="/start", buttons=[]
                    )

                return messages()

        helper.client = Client()
        times = iter([0.0, 0.0])
        loop = SimpleNamespace(time=Mock(side_effect=lambda: next(times, 2.0)))

        with (
            patch("src.telegram_client.asyncio.get_running_loop", return_value=loop),
            patch("src.telegram_client.asyncio.sleep", new_callable=AsyncMock),
        ):
            messages = await helper._wait_resource_bot_messages(object(), 0)

        self.assertEqual(messages, [])
    async def test_resource_wait_does_not_finish_on_sending_notice(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.config = SimpleNamespace(max_resource_bot_wait_seconds=40)
        pending = SimpleNamespace(
            id=1,
            out=False,
            raw_text="已收到，正在发送资源。",
            file=None,
            buttons=[],
        )
        finished = SimpleNamespace(
            id=2,
            out=False,
            raw_text="全部文件已发送完毕",
            file=object(),
            buttons=[],
        )
        batches = [[pending], [pending, finished]]

        class Client:
            def iter_messages(self, *_args, **_kwargs):
                async def messages():
                    for message in batches.pop(0):
                        yield message

                return messages()

        helper.client = Client()
        loop = SimpleNamespace(
            time=Mock(side_effect=[0.0, 0.0, 0.0, 10.0, 11.0, 11.0])
        )
        with (
            patch("src.telegram_client.asyncio.get_running_loop", return_value=loop),
            patch("src.telegram_client.asyncio.sleep", new_callable=AsyncMock),
        ):
            messages = await helper._wait_resource_bot_messages(object(), 0)

        self.assertEqual([message.id for message in messages], [1, 2])

    async def test_resource_link_with_no_media_is_empty(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.config = SimpleNamespace(max_resource_bot_wait_seconds=120)
        helper._resource_bot_whitelist = Mock(return_value={"arbbanyunbot"})
        helper.db = SimpleNamespace(
            get_resource_link=Mock(return_value={"status": "empty"}),
            upsert_resource_link=Mock(),
        )

        class Client:
            get_entity = AsyncMock(return_value=object())
            get_messages = AsyncMock(return_value=[])

            def iter_messages(self, *_args, **_kwargs):
                async def messages():
                    if False:
                        yield None

                return messages()

        helper.client = Client()
        pending = SimpleNamespace(
            id=2,
            out=False,
            raw_text="已收到，正在发送资源。",
            file=None,
            buttons=[],
        )
        helper._start_resource_bot = AsyncMock()
        helper._wait_resource_bot_messages = AsyncMock(side_effect=[[], [pending]])
        helper._click_resource_all_button = AsyncMock(return_value=False)
        helper._collect_resource_bot_media = AsyncMock(return_value=[])
        helper._forward_many = AsyncMock()
        link = ResourceBotLink(
            "arbbanyunbot",
            "C5I9hoewsFD0Q1Vh",
            "https://t.me/arbbanyunbot?start=C5I9hoewsFD0Q1Vh",
            "https://t.me/utwtda",
            841,
        )

        outcomes = []
        with patch("src.telegram_client.asyncio.sleep", new_callable=AsyncMock):
            for _ in range(2):
                outcomes.append(
                    await helper._process_resource_bot_link(link, force=True)
                )

        self.assertEqual([outcome.status for outcome in outcomes], ["empty", "empty"])
        self.assertEqual(helper.db.upsert_resource_link.call_args.args[4], "empty")
        helper._forward_many.assert_not_awaited()

    async def test_empty_resource_link_is_not_retried_without_force(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper._resource_bot_whitelist = Mock(return_value={"arbbanyunbot"})
        helper.db = SimpleNamespace(
            get_resource_link=Mock(return_value={"status": "empty"})
        )
        helper.client = SimpleNamespace(get_entity=AsyncMock())
        link = ResourceBotLink(
            "arbbanyunbot",
            "C5I9hoewsFD0Q1Vh",
            "https://t.me/arbbanyunbot?start=C5I9hoewsFD0Q1Vh",
            "https://t.me/utwtda",
            841,
        )

        outcome = await helper._process_resource_bot_link(link)

        self.assertEqual(outcome.status, "empty")
        helper.client.get_entity.assert_not_awaited()

    def test_resource_expired_text_is_classified_explicitly(self) -> None:
        self.assertTrue(
            TelegramSaveHelper._resource_bot_expired(
                [SimpleNamespace(raw_text="抱歉，该资源已失效")]
            )
        )
        self.assertFalse(
            TelegramSaveHelper._resource_bot_expired(
                [SimpleNamespace(raw_text="该资源不会失效")]
            )
        )

    def test_message_reference_keeps_link_clickable(self) -> None:
        reference = TelegramSaveHelper._message_reference("@beigh6", 1)
        self.assertEqual(reference, "来源 @beigh6，消息 1\n链接：https://t.me/beigh6/1")
        self.assertNotIn("https://t.me/beigh6/1）", reference)

    def test_short_channel_id_builds_private_message_link(self) -> None:
        self.assertEqual(
            TelegramSaveHelper._message_link("-2312388706", 10),
            "https://t.me/c/2312388706/10",
        )

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

    async def test_resource_recheck_merges_messages_into_one_source_task(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.pending_resource_rechecks = {}
        helper.completed_resource_rechecks = {}
        helper.resource_recheck_tasks = {}
        helper.db = SimpleNamespace(set_state=Mock())
        calls: list[str] = []
        release = asyncio.Event()

        async def fake_recheck(source: str) -> str:
            calls.append(source)
            await release.wait()
            helper.pending_resource_rechecks.pop(source, None)
            return source

        helper._delayed_resource_recheck = fake_recheck  # type: ignore[method-assign]

        helper._schedule_resource_recheck("https://t.me/papashipin8", 756812)
        helper._schedule_resource_recheck("https://t.me/papashipin8", 756900)
        await asyncio.sleep(0)

        self.assertEqual(calls, ["https://t.me/papashipin8"])
        self.assertEqual(
            helper.pending_resource_rechecks,
            {"https://t.me/papashipin8": [(756812, 756812), (756900, 756900)]},
        )
        self.assertEqual(len(helper.resource_recheck_tasks), 1)

        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(helper.pending_resource_rechecks, {})
        self.assertEqual(helper.resource_recheck_tasks, {})

    async def test_busy_resource_watch_only_rechecks_forwardable_root_posts(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper._resource_watch_source_busy = Mock(return_value=True)
        helper._extract_resource_bot_links = Mock(return_value=[])
        helper._schedule_resource_recheck = Mock()
        helper._schedule_resource_ready = Mock()
        source = "https://t.me/papashipin8"

        await helper._process_resource_watch_group(
            source,
            [
                SimpleNamespace(
                    id=1,
                    reply_to_msg_id=None,
                    file=None,
                    raw_text="text",
                    entities=[],
                    buttons=[],
                )
            ],
        )
        await helper._process_resource_watch_group(
            source,
            [SimpleNamespace(id=2, reply_to_msg_id=1, file=object(), raw_text="")],
        )
        await helper._process_resource_watch_group(
            source,
            [SimpleNamespace(id=3, reply_to_msg_id=None, file=object(), raw_text="")],
        )

        helper._schedule_resource_recheck.assert_called_once_with(source, 3)
        helper._schedule_resource_ready.assert_not_called()

    async def test_busy_resource_reply_link_is_queued_for_its_root_post(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        source = "https://t.me/jibahenyanga"
        root = [SimpleNamespace(id=5618)]
        reply = SimpleNamespace(id=5620, reply_to_msg_id=5618)
        link = ResourceBotLink(
            "seliu",
            "j_2bfc3620",
            "https://t.me/seliu?start=j_2bfc3620",
            source,
            5620,
        )
        helper._extract_resource_bot_links = Mock(return_value=[link])
        helper._resolve_source = AsyncMock(return_value=object())
        helper._resource_reply_group = AsyncMock(return_value=root)
        helper._schedule_resource_ready = Mock()
        helper._schedule_resource_recheck = Mock()

        await helper._queue_busy_resource_watch_group(source, [reply])

        helper._schedule_resource_ready.assert_called_once_with(source, 5618)
        helper._schedule_resource_recheck.assert_not_called()

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

    def test_resource_recheck_range_is_restored_as_one_task_per_source(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.pending_resource_rechecks = {}
        helper.completed_resource_rechecks = {}
        helper.ready_resource_rechecks = {}
        helper.resource_recheck_tasks = {}
        helper.resource_ready_tasks = {}
        states = {
            "watchresource_recheck_ranges":
                '{"https://t.me/papashipin8": [[875854, 875854], [876900, 876900]]}',
            "watchresource_ready_ranges":
                '{"https://t.me/papashipin8": [[875900, 875900]]}',
        }
        helper.db = SimpleNamespace(
            get_state=lambda key, _default: states.get(key, "{}")
        )
        helper._start_resource_recheck_task = Mock()
        helper._start_resource_ready_task = Mock()

        helper._resume_resource_rechecks()

        self.assertEqual(
            helper.pending_resource_rechecks,
            {"https://t.me/papashipin8": [(875854, 875854), (876900, 876900)]},
        )
        self.assertEqual(
            helper.ready_resource_rechecks,
            {"https://t.me/papashipin8": [(875900, 875900)]},
        )
        helper._start_resource_recheck_task.assert_called_once_with(
            "https://t.me/papashipin8"
        )
        helper._start_resource_ready_task.assert_called_once_with(
            "https://t.me/papashipin8"
        )

    def test_completed_resource_recheck_is_not_scheduled_again(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.pending_resource_rechecks = {}
        helper.completed_resource_rechecks = {
            "https://t.me/jibahenyanga": [(5682, 5682)]
        }
        helper.resource_recheck_tasks = {}
        helper.db = SimpleNamespace(set_state=Mock())

        helper._schedule_resource_recheck("https://t.me/jibahenyanga", 5682)

        self.assertEqual(helper.pending_resource_rechecks, {})
        self.assertEqual(helper.resource_recheck_tasks, {})

    async def test_completed_resource_watch_group_is_not_scanned_again(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.completed_resource_rechecks = {
            "https://t.me/jibahenyanga": [(5682, 5682)]
        }
        helper._resolve_source = AsyncMock()

        await helper._process_resource_watch_group_inner(
            "https://t.me/jibahenyanga", [SimpleNamespace(id=5682)]
        )

        helper._resolve_source.assert_not_awaited()

    async def test_resource_watch_does_not_recheck_after_link_is_found(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.completed_resource_rechecks = {}
        group = [SimpleNamespace(id=5682)]
        links = [SimpleNamespace(bot_username="seliu", payload="j_69103756")]
        helper._resolve_source = AsyncMock(return_value=object())
        helper._resource_link_groups = AsyncMock(return_value=([(group, links)], []))
        helper._forward_resource_watch_links = AsyncMock()
        helper._schedule_resource_recheck = Mock()

        await helper._process_resource_watch_group_inner(
            "https://t.me/jibahenyanga", group
        )

        helper._forward_resource_watch_links.assert_awaited_once_with(
            "https://t.me/jibahenyanga", 5682, [(group, links)]
        )
        helper._schedule_resource_recheck.assert_not_called()


    async def test_resource_detection_does_not_wait_for_busy_extraction(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.pending_resource_rechecks = {
            "https://t.me/jibahenyanga": [(5682, 5682)]
        }
        helper.completed_resource_rechecks = {}
        helper.ready_resource_rechecks = {}
        helper.exhausted_resource_comment_reads = set()
        helper.db = SimpleNamespace(set_state=Mock())
        group = [SimpleNamespace(id=5682)]
        links = [SimpleNamespace(bot_username="seliu", payload="j_69103756")]
        helper._resolve_source = AsyncMock(return_value=object())
        helper._resource_one_groups = AsyncMock(return_value=[group])
        helper._resource_link_groups = AsyncMock(
            return_value=([(group, links)], []),
        )
        helper._resource_watch_source_busy = Mock(return_value=True)
        helper._forward_resource_watch_links = AsyncMock()
        helper._schedule_resource_ready = Mock()

        with (
            patch("src.telegram_client.WATCHRESOURCE_RECHECK_DELAYS", (0, 0, 0)),
            patch("src.telegram_client.asyncio.sleep", new_callable=AsyncMock) as sleep,
        ):
            key = await helper._delayed_resource_recheck(
                "https://t.me/jibahenyanga"
            )

        self.assertEqual(key, "https://t.me/jibahenyanga")
        self.assertEqual(sleep.await_count, 1)
        helper._resource_watch_source_busy.assert_not_called()
        helper._forward_resource_watch_links.assert_not_awaited()
        helper._schedule_resource_ready.assert_called_once_with(
            "https://t.me/jibahenyanga", 5682
        )
        self.assertEqual(helper.pending_resource_rechecks, {})
        self.assertEqual(
            helper.completed_resource_rechecks,
            {"https://t.me/jibahenyanga": [(5682, 5682)]},
        )

    async def test_resource_ready_worker_waits_then_forwards_serially(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        source = "https://t.me/jibahenyanga"
        helper.ready_resource_rechecks = {source: [(5682, 5682)]}
        helper.pending_resource_rechecks = {}
        helper.completed_resource_rechecks = {}
        helper.active_resource_watch_sources = set()
        helper.db = SimpleNamespace(set_state=Mock())
        group = [SimpleNamespace(id=5682)]
        links = [SimpleNamespace(bot_username="seliu", payload="j_69103756")]
        helper._resource_watch_source_busy = Mock(side_effect=[True, False])
        helper._resolve_source = AsyncMock(return_value=object())
        helper._resource_one_groups = AsyncMock(return_value=[group])
        helper._resource_link_groups = AsyncMock(
            return_value=([(group, links)], []),
        )
        helper._forward_resource_watch_links = AsyncMock()

        with patch(
            "src.telegram_client.asyncio.sleep", new_callable=AsyncMock
        ) as sleep:
            key = await helper._resource_ready_worker(source)

        self.assertEqual(key, source)
        sleep.assert_awaited_once()
        helper._forward_resource_watch_links.assert_awaited_once_with(
            source, 5682, [(group, links)]
        )
        self.assertEqual(helper.ready_resource_rechecks, {})

    async def test_automatic_watch_forwards_are_serialized(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.watch_forward_semaphore = asyncio.Semaphore(1)
        active = 0
        maximum = 0

        async def forward(_source: str, _messages: list[object]):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1
            return SimpleNamespace(success=1, failed=0, skipped=0)

        helper._forward_many = forward  # type: ignore[method-assign]
        await asyncio.gather(
            helper._run_limited_watch_forward("source", [object()], None),
            helper._run_limited_watch_forward("source", [object()], None),
        )

        self.assertEqual(maximum, 1)

    async def test_standard_watch_messages_merge_into_one_source_range(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.pending_watch_forwards = {}
        helper.watch_forward_tasks = {}
        helper.db = SimpleNamespace(set_state=Mock())
        release = asyncio.Event()

        async def worker(_source: str) -> None:
            await release.wait()
            helper.pending_watch_forwards.clear()

        helper._watch_forward_worker = worker  # type: ignore[method-assign]
        helper._schedule_watch_forward("-2369004562", 100, 100)
        helper._schedule_watch_forward("-2369004562", 101, 103)
        await asyncio.sleep(0)

        self.assertEqual(
            helper.pending_watch_forwards,
            {"-1002369004562": (100, 103)},
        )
        self.assertEqual(len(helper.watch_forward_tasks), 1)

        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(helper.watch_forward_tasks, {})

    def test_pending_watch_links_are_migrated_to_source_ranges(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.pending_watch_forwards = {}
        helper.watch_forward_tasks = {}
        pending = [
            "/link https://t.me/c/2369004562/100",
            "/link https://t.me/c/2369004562/103",
            "/link https://t.me/not_watched/9",
        ]
        state: dict[str, str] = {}
        helper.db = SimpleNamespace(
            get_state=lambda key, default="": state.get(key, default),
            set_state=lambda key, value: state.__setitem__(key, value),
            list_watches=lambda: [
                SimpleNamespace(source="-2369004562", mode="standard")
            ],
            pending_manual_commands=lambda: list(pending),
            remove_pending_manual_command=lambda command: pending.remove(command),
        )

        helper._migrate_pending_watch_links()

        self.assertEqual(
            helper.pending_watch_forwards,
            {"-1002369004562": (100, 103)},
        )
        self.assertEqual(pending, ["/link https://t.me/not_watched/9"])
        self.assertEqual(state["watch_forward_link_migration_v1"], "done")

    async def test_watch_forward_worker_advances_and_clears_range(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.pending_watch_forwards = {"@source": (10, 11)}
        helper.db = SimpleNamespace(set_state=Mock())
        helper._resolve_source = AsyncMock(return_value=object())
        helper._record_watch_summary = Mock()
        helper._notify_control_bot = AsyncMock()
        helper._forward_many = AsyncMock(
            side_effect=[
                SimpleNamespace(success=1, failed=0, skipped=0, errors=[]),
                SimpleNamespace(success=1, failed=0, skipped=0, errors=[]),
            ]
        )

        async def groups(_entity: object, _start: int):
            yield [SimpleNamespace(id=10)]
            yield [SimpleNamespace(id=11)]

        helper._iter_message_groups_from = groups  # type: ignore[method-assign]
        await helper._watch_forward_worker("@source")

        self.assertEqual(helper.pending_watch_forwards, {})
        self.assertEqual(helper._forward_many.await_count, 2)

    async def test_restarted_link_commands_are_serialized(self) -> None:
        helper = TelegramSaveHelper.__new__(TelegramSaveHelper)
        helper.watch_forward_semaphore = asyncio.Semaphore(1)
        active = 0
        maximum = 0

        async def execute(_command: object, _event: object) -> None:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1

        helper._execute_command = execute  # type: ignore[method-assign]
        command = SimpleNamespace(name="/link")
        await asyncio.gather(
            helper._resume_command(command),
            helper._resume_command(command),
        )

        self.assertEqual(maximum, 1)

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
