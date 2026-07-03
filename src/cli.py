from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from telethon import TelegramClient

from .commands import CommandError, parse_command
from .config import Config, load_config
from .db import Database
from .telegram_client import TelegramSaveHelper


class ConsoleEvent:
    """Small event shim used by TelegramSaveHelper._reply in CLI mode."""

    id = 0
    client = None

    async def respond(self, text: str, reply_to: int | None = None) -> None:
        print(text, flush=True)


def _telethon_session_file(session_name: str) -> Path:
    path = Path(session_name).expanduser()
    return path if path.suffix == ".session" else path.with_suffix(".session")


def _copy_session(config: Config) -> tuple[Config, Path | None]:
    source = _telethon_session_file(config.session_name)
    if not source.exists():
        return config, None
    target = Path(tempfile.gettempdir()) / f"tg_save_helper_cli_{os.getpid()}.session"
    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        target_conn = sqlite3.connect(target)
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
    finally:
        source_conn.close()
    return replace(config, session_name=str(target.with_suffix(""))), target


async def _run_one(helper: TelegramSaveHelper, text: str) -> int:
    try:
        command = parse_command(text)
        if command is None:
            print("不是命令：请输入以 / 开头的指令", file=sys.stderr)
            return 2
        await helper._execute_command(command, ConsoleEvent())
        return 0
    except CommandError as exc:
        print(str(exc), file=sys.stderr)
        return 2


async def _async_main(args: argparse.Namespace) -> int:
    config = load_config()
    logging.basicConfig(
        level=getattr(logging, args.log_level or config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    text = " ".join(args.command).strip()
    if args.parse_only:
        if not text:
            print("--parse-only 需要提供一条命令", file=sys.stderr)
            return 2
        command = parse_command(text)
        print(command)
        return 0

    session_copy: Path | None = None
    if not args.live_session:
        config, session_copy = _copy_session(config)
        if session_copy is not None:
            print(f"[cli] 使用 session 副本：{session_copy}", flush=True)
        else:
            print("[cli] 未找到现有 session，将使用配置 session", flush=True)

    database = Database(config.database_path)
    helper = TelegramSaveHelper(config, database)
    try:
        await helper.client.start()
        me = await helper.client.get_me()
        if me is not None:
            helper.owner_id = int(me.id)
        if helper.bot_owner_id is None:
            helper.bot_owner_id = helper.owner_id
        if config.bot_token:
            helper.bot_client = TelegramClient(
                f"{config.session_name}_cli_bot",
                config.api_id,
                config.api_hash,
            )
            await helper.bot_client.start(bot_token=config.bot_token)
        if text:
            return await _run_one(helper, text)

        print("进入交互模式。输入 Telegram 指令，或输入 exit/quit 退出。", flush=True)
        while True:
            try:
                line = await asyncio.to_thread(input, "tg> ")
            except EOFError:
                break
            line = line.strip()
            if not line:
                continue
            if line.lower() in {"exit", "quit"}:
                break
            await _run_one(helper, line)
        return 0
    finally:
        if helper.bot_client is not None:
            await helper.bot_client.disconnect()
        await helper.client.disconnect()
        database.close()
        if session_copy is not None:
            try:
                session_copy.unlink()
            except OSError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Telegram Saved Messages helper commands from the shell."
    )
    parser.add_argument(
        "command",
        nargs="*",
        help='command to run, e.g. "/status" or /last @channel 3',
    )
    parser.add_argument(
        "--parse-only",
        action="store_true",
        help="only parse and validate the command without connecting to Telegram",
    )
    parser.add_argument(
        "--live-session",
        action="store_true",
        help="use the configured session directly instead of a temporary copy",
    )
    parser.add_argument("--log-level", help="override LOG_LEVEL for this CLI run")
    raise SystemExit(asyncio.run(_async_main(parser.parse_args())))


if __name__ == "__main__":
    main()
