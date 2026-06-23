from __future__ import annotations

import asyncio
import logging

from .config import load_config
from .db import Database
from .telegram_client import TelegramSaveHelper


async def async_main() -> None:
    config = load_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    database = Database(config.database_path)
    helper = TelegramSaveHelper(config, database)
    try:
        await helper.run()
    finally:
        database.close()


def main() -> None:
    try:
        asyncio.run(async_main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc


if __name__ == "__main__":
    main()
