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
    database_path: Path
    log_level: str


def load_config() -> Config:
    load_dotenv()
    api_id_text = os.getenv("TG_API_ID", "").strip()
    api_hash = os.getenv("TG_API_HASH", "").strip()
    session_name = os.getenv("TG_SESSION_NAME", "data/tg_save_helper").strip()
    owner_text = os.getenv("OWNER_ID", "").strip()

    if not api_id_text or not api_hash:
        raise ValueError("TG_API_ID and TG_API_HASH must be set in .env")
    try:
        api_id = int(api_id_text)
        owner_id = int(owner_text) if owner_text else None
    except ValueError as exc:
        raise ValueError("TG_API_ID and OWNER_ID must be integers") from exc
    if api_id <= 0 or not session_name:
        raise ValueError("TG_API_ID must be positive and TG_SESSION_NAME cannot be empty")

    database_path = Path(os.getenv("TG_DATABASE_PATH", "data/tg_save_helper.sqlite3"))
    Path(session_name).expanduser().parent.mkdir(parents=True, exist_ok=True)
    database_path.expanduser().parent.mkdir(parents=True, exist_ok=True)
    return Config(
        api_id=api_id,
        api_hash=api_hash,
        session_name=str(Path(session_name).expanduser()),
        owner_id=owner_id,
        database_path=database_path.expanduser(),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )

