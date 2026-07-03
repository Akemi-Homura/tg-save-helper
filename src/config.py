from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session_name: str
    owner_id: int | None
    bot_token: str | None
    bot_owner_id: int | None
    resource_bots: tuple[str, ...]
    max_resource_bot_pages: int
    max_resource_bot_wait_seconds: int
    max_resource_bot_messages: int
    resource_bot_start_interval_seconds: int
    forward_interval_min_seconds: float
    forward_interval_max_seconds: float
    forward_batch_size: int
    forward_batch_pause_min_seconds: float
    forward_batch_pause_max_seconds: float
    database_path: Path
    saved_media_path: Path
    log_level: str


def load_config() -> Config:
    load_dotenv()
    api_id_text = os.getenv("TG_API_ID", "").strip()
    api_hash = os.getenv("TG_API_HASH", "").strip()
    session_name = os.getenv("TG_SESSION_NAME", "data/tg_save_helper").strip()
    owner_text = os.getenv("OWNER_ID", "").strip()
    bot_token = os.getenv("BOT_TOKEN", "").strip() or None
    bot_owner_text = os.getenv("BOT_OWNER_ID", "").strip()
    resource_bots = tuple(
        item.strip().lstrip("@").lower()
        for item in os.getenv("TG_RESOURCE_BOTS", "").split(",")
        if item.strip()
    )

    if not api_id_text or not api_hash:
        raise ValueError("TG_API_ID and TG_API_HASH must be set in .env")
    try:
        api_id = int(api_id_text)
        owner_id = int(owner_text) if owner_text else None
        bot_owner_id = int(bot_owner_text) if bot_owner_text else None
        max_resource_bot_pages = int(os.getenv("MAX_RESOURCE_BOT_PAGES", "100"))
        max_resource_bot_wait_seconds = int(
            os.getenv("MAX_RESOURCE_BOT_WAIT_SECONDS", "120")
        )
        max_resource_bot_messages = int(os.getenv("MAX_RESOURCE_BOT_MESSAGES", "2000"))
        resource_bot_start_interval_seconds = int(
            os.getenv("RESOURCE_BOT_START_INTERVAL_SECONDS", "75")
        )
        forward_interval_min_seconds = float(
            os.getenv("TG_FORWARD_INTERVAL_MIN_SECONDS", "3")
        )
        forward_interval_max_seconds = float(
            os.getenv("TG_FORWARD_INTERVAL_MAX_SECONDS", "6")
        )
        forward_batch_size = int(os.getenv("TG_FORWARD_BATCH_SIZE", "50"))
        forward_batch_pause_min_seconds = float(
            os.getenv("TG_FORWARD_BATCH_PAUSE_MIN_SECONDS", "30")
        )
        forward_batch_pause_max_seconds = float(
            os.getenv("TG_FORWARD_BATCH_PAUSE_MAX_SECONDS", "60")
        )
    except ValueError as exc:
        raise ValueError(
            "TG_API_ID, OWNER_ID, BOT_OWNER_ID and rate limits must be valid numbers"
        ) from exc
    if api_id <= 0 or not session_name:
        raise ValueError("TG_API_ID must be positive and TG_SESSION_NAME cannot be empty")
    if (
        forward_interval_min_seconds < 0
        or forward_interval_max_seconds < forward_interval_min_seconds
    ):
        raise ValueError("TG_FORWARD_INTERVAL_MIN_SECONDS/MAX_SECONDS 配置无效")
    if forward_batch_size <= 0:
        raise ValueError("TG_FORWARD_BATCH_SIZE must be positive")
    if (
        forward_batch_pause_min_seconds < 0
        or forward_batch_pause_max_seconds < forward_batch_pause_min_seconds
    ):
        raise ValueError("TG_FORWARD_BATCH_PAUSE_MIN_SECONDS/MAX_SECONDS 配置无效")

    database_path = Path(os.getenv("TG_DATABASE_PATH", "data/tg_save_helper.sqlite3"))
    saved_media_path = Path(os.getenv("TG_SAVED_MEDIA_PATH", "data/saved_media"))
    Path(session_name).expanduser().parent.mkdir(parents=True, exist_ok=True)
    database_path.expanduser().parent.mkdir(parents=True, exist_ok=True)
    saved_media_path.expanduser().mkdir(parents=True, exist_ok=True)
    return Config(
        api_id=api_id,
        api_hash=api_hash,
        session_name=str(Path(session_name).expanduser()),
        owner_id=owner_id,
        bot_token=bot_token,
        bot_owner_id=bot_owner_id,
        resource_bots=resource_bots,
        max_resource_bot_pages=max_resource_bot_pages,
        max_resource_bot_wait_seconds=max_resource_bot_wait_seconds,
        max_resource_bot_messages=max_resource_bot_messages,
        resource_bot_start_interval_seconds=resource_bot_start_interval_seconds,
        forward_interval_min_seconds=forward_interval_min_seconds,
        forward_interval_max_seconds=forward_interval_max_seconds,
        forward_batch_size=forward_batch_size,
        forward_batch_pause_min_seconds=forward_batch_pause_min_seconds,
        forward_batch_pause_max_seconds=forward_batch_pause_max_seconds,
        database_path=database_path.expanduser(),
        saved_media_path=saved_media_path.expanduser(),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
