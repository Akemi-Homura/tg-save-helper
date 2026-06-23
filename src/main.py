from __future__ import annotations

import asyncio
import argparse
import logging

from .config import load_config
from .db import Database
from .telegram_client import TelegramSaveHelper


async def async_main(login_only: bool = False) -> None:
    config = load_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    database = Database(config.database_path)
    helper = TelegramSaveHelper(config, database)
    try:
        if login_only:
            await helper.login_only()
        else:
            await helper.run()
    finally:
        database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram Saved Messages helper")
    parser.add_argument(
        "--login-only",
        action="store_true",
        help="complete interactive login, save the session, and exit",
    )
    args = parser.parse_args()
    try:
        asyncio.run(async_main(login_only=args.login_only))
    except (KeyboardInterrupt, SystemExit):
        pass
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc


if __name__ == "__main__":
    main()
