from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class WatchedSource:
    source: str
    peer_id: int
    title: str
    created_at: str


class Database:
    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS watched_sources (
                source TEXT PRIMARY KEY,
                peer_id INTEGER NOT NULL UNIQUE,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS forwarding_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_forwarding_logs_created_at
                ON forwarding_logs(created_at);
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def add_watch(self, source: str, peer_id: int, title: str) -> None:
        self.connection.execute(
            """INSERT INTO watched_sources(source, peer_id, title, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(peer_id) DO UPDATE SET source=excluded.source, title=excluded.title""",
            (source, peer_id, title, utc_now()),
        )
        self.connection.commit()

    def remove_watch(self, source: str | None = None, peer_id: int | None = None) -> bool:
        if source is None and peer_id is None:
            raise ValueError("source or peer_id is required")
        if peer_id is not None:
            cursor = self.connection.execute("DELETE FROM watched_sources WHERE peer_id = ?", (peer_id,))
        else:
            cursor = self.connection.execute("DELETE FROM watched_sources WHERE source = ?", (source,))
        self.connection.commit()
        return cursor.rowcount > 0

    def list_watches(self) -> list[WatchedSource]:
        rows = self.connection.execute(
            "SELECT source, peer_id, title, created_at FROM watched_sources ORDER BY created_at"
        ).fetchall()
        return [WatchedSource(**dict(row)) for row in rows]

    def watched_peer_ids(self) -> set[int]:
        return {row[0] for row in self.connection.execute("SELECT peer_id FROM watched_sources")}

    def log_forward(self, source: str, message_id: int, status: str, error: str | None = None) -> None:
        self.connection.execute(
            "INSERT INTO forwarding_logs(source, message_id, status, error, created_at) VALUES (?, ?, ?, ?, ?)",
            (source, message_id, status, error, utc_now()),
        )
        self.connection.commit()

    def set_state(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO app_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.connection.commit()

    def get_state(self, key: str, default: str = "") -> str:
        row = self.connection.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else default

    def successful_count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) FROM forwarding_logs WHERE status = 'success'").fetchone()
        return int(row[0])

    def close(self) -> None:
        self.connection.close()

