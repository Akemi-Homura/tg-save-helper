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
    mode: str = "standard"
    linked_peer_id: int | None = None
    linked_title: str | None = None


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
            CREATE TABLE IF NOT EXISTS saved_channel_mappings (
                source_peer_id INTEGER PRIMARY KEY,
                source_title TEXT NOT NULL,
                destination_peer_id INTEGER NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS saved_media_sync (
                saved_message_id INTEGER PRIMARY KEY,
                source_peer_id INTEGER NOT NULL,
                destination_peer_id INTEGER NOT NULL,
                local_path TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(watched_sources)")
        }
        if "mode" not in columns:
            self.connection.execute(
                "ALTER TABLE watched_sources ADD COLUMN mode TEXT NOT NULL DEFAULT 'standard'"
            )
        if "linked_peer_id" not in columns:
            self.connection.execute(
                "ALTER TABLE watched_sources ADD COLUMN linked_peer_id INTEGER"
            )
        if "linked_title" not in columns:
            self.connection.execute(
                "ALTER TABLE watched_sources ADD COLUMN linked_title TEXT"
            )
        self.connection.commit()

    def add_watch(
        self,
        source: str,
        peer_id: int,
        title: str,
        mode: str = "standard",
        linked_peer_id: int | None = None,
        linked_title: str | None = None,
    ) -> None:
        self.connection.execute(
            "DELETE FROM watched_sources WHERE source = ? OR peer_id = ?",
            (source, peer_id),
        )
        self.connection.execute(
            """INSERT INTO watched_sources(
                   source, peer_id, title, created_at, mode, linked_peer_id, linked_title
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (source, peer_id, title, utc_now(), mode, linked_peer_id, linked_title),
        )
        self.connection.commit()

    def remove_watch(
        self,
        source: str | None = None,
        peer_id: int | None = None,
        mode: str | None = None,
    ) -> bool:
        if source is None and peer_id is None:
            raise ValueError("source or peer_id is required")
        field = "peer_id" if peer_id is not None else "source"
        value = peer_id if peer_id is not None else source
        sql = f"DELETE FROM watched_sources WHERE {field} = ?"
        params: tuple[object, ...] = (value,)
        if mode is not None:
            sql += " AND mode = ?"
            params += (mode,)
        cursor = self.connection.execute(sql, params)
        self.connection.commit()
        return cursor.rowcount > 0

    def list_watches(self) -> list[WatchedSource]:
        rows = self.connection.execute(
            """SELECT source, peer_id, title, created_at, mode, linked_peer_id, linked_title
               FROM watched_sources ORDER BY created_at"""
        ).fetchall()
        return [WatchedSource(**dict(row)) for row in rows]

    def watched_peer_ids(self) -> set[int]:
        peer_ids: set[int] = set()
        for row in self.connection.execute(
            "SELECT peer_id, linked_peer_id FROM watched_sources"
        ):
            peer_ids.add(int(row[0]))
            if row[1] is not None:
                peer_ids.add(int(row[1]))
        return peer_ids

    def find_watch_for_peer(self, peer_id: int) -> tuple[WatchedSource, bool] | None:
        row = self.connection.execute(
            """SELECT source, peer_id, title, created_at, mode, linked_peer_id, linked_title
               FROM watched_sources
               WHERE peer_id = ? OR linked_peer_id = ?
               LIMIT 1""",
            (peer_id, peer_id),
        ).fetchone()
        if row is None:
            return None
        watch = WatchedSource(**dict(row))
        return watch, watch.linked_peer_id == peer_id

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

    def get_saved_channel_mapping(self, source_peer_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT source_peer_id, source_title, destination_peer_id
               FROM saved_channel_mappings WHERE source_peer_id = ?""",
            (source_peer_id,),
        ).fetchone()

    def save_channel_mapping(
        self, source_peer_id: int, source_title: str, destination_peer_id: int
    ) -> None:
        self.connection.execute(
            """INSERT INTO saved_channel_mappings(
                   source_peer_id, source_title, destination_peer_id, created_at
               ) VALUES (?, ?, ?, ?)
               ON CONFLICT(source_peer_id) DO UPDATE SET
                   source_title=excluded.source_title,
                   destination_peer_id=excluded.destination_peer_id""",
            (source_peer_id, source_title, destination_peer_id, utc_now()),
        )
        self.connection.commit()

    def saved_message_was_synced(self, message_id: int) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM saved_media_sync WHERE saved_message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def mark_saved_message_synced(
        self,
        message_id: int,
        source_peer_id: int,
        destination_peer_id: int,
        local_path: str | None,
    ) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO saved_media_sync(
                   saved_message_id, source_peer_id, destination_peer_id, local_path, created_at
               ) VALUES (?, ?, ?, ?, ?)""",
            (message_id, source_peer_id, destination_peer_id, local_path, utc_now()),
        )
        self.connection.commit()

    def saved_sync_count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) FROM saved_media_sync").fetchone()
        return int(row[0])

    def close(self) -> None:
        self.connection.close()
