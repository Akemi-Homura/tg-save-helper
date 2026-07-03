from __future__ import annotations

import json
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
                destination_message_id INTEGER,
                summary_channel_peer_id INTEGER,
                summary_message_id INTEGER,
                summary_at TEXT,
                local_path TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS resource_bots (
                username TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS resource_bot_links (
                bot_username TEXT NOT NULL,
                payload TEXT NOT NULL,
                source TEXT NOT NULL,
                source_message_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_error TEXT,
                start_message_id INTEGER,
                first_response_id INTEGER,
                last_response_id INTEGER,
                collected_count INTEGER,
                forwarded_count INTEGER,
                PRIMARY KEY (bot_username, payload)
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
        sync_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(saved_media_sync)")
        }
        if "destination_message_id" not in sync_columns:
            self.connection.execute(
                "ALTER TABLE saved_media_sync ADD COLUMN destination_message_id INTEGER"
            )
        if "summary_channel_peer_id" not in sync_columns:
            self.connection.execute(
                "ALTER TABLE saved_media_sync ADD COLUMN summary_channel_peer_id INTEGER"
            )
        if "summary_message_id" not in sync_columns:
            self.connection.execute(
                "ALTER TABLE saved_media_sync ADD COLUMN summary_message_id INTEGER"
            )
        if "summary_at" not in sync_columns:
            self.connection.execute(
                "ALTER TABLE saved_media_sync ADD COLUMN summary_at TEXT"
            )
        resource_link_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(resource_bot_links)")
        }
        for column, column_type in (
            ("start_message_id", "INTEGER"),
            ("first_response_id", "INTEGER"),
            ("last_response_id", "INTEGER"),
            ("collected_count", "INTEGER"),
            ("forwarded_count", "INTEGER"),
        ):
            if column not in resource_link_columns:
                self.connection.execute(
                    f"ALTER TABLE resource_bot_links ADD COLUMN {column} {column_type}"
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

    def forward_was_successful(self, source: str, message_id: int) -> bool:
        row = self.connection.execute(
            """SELECT 1 FROM forwarding_logs
               WHERE source = ? AND message_id = ? AND status = 'success'
               LIMIT 1""",
            (source, message_id),
        ).fetchone()
        return row is not None

    def set_state(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO app_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.connection.commit()

    def get_state(self, key: str, default: str = "") -> str:
        row = self.connection.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else default

    def pending_manual_commands(self) -> list[str]:
        value = self.get_state("pending_manual_commands", "[]")
        try:
            items = json.loads(value)
        except json.JSONDecodeError:
            return []
        return [str(item) for item in items if str(item).strip()]

    def add_pending_manual_command(self, command: str) -> None:
        items = self.pending_manual_commands()
        if command not in items:
            items.append(command)
            self.set_state("pending_manual_commands", json.dumps(items, ensure_ascii=False))

    def remove_pending_manual_command(self, command: str) -> None:
        items = [item for item in self.pending_manual_commands() if item != command]
        self.set_state("pending_manual_commands", json.dumps(items, ensure_ascii=False))

    def replace_pending_manual_command(self, old: str, new: str) -> None:
        items = [new if item == old else item for item in self.pending_manual_commands()]
        if new not in items:
            items.append(new)
        self.set_state("pending_manual_commands", json.dumps(items, ensure_ascii=False))

    def successful_count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) FROM forwarding_logs WHERE status = 'success'").fetchone()
        return int(row[0])

    def latest_forward_problem(self) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT source, message_id, status, error, created_at
               FROM forwarding_logs
               WHERE status IN ('failed', 'skipped')
               ORDER BY id DESC
               LIMIT 1"""
        ).fetchone()

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

    def get_saved_sync(self, message_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT saved_message_id, source_peer_id, destination_peer_id,
                      destination_message_id, summary_channel_peer_id,
                      summary_message_id, local_path, created_at
               FROM saved_media_sync WHERE saved_message_id = ?""",
            (message_id,),
        ).fetchone()

    def mark_saved_message_synced(
        self,
        message_id: int,
        source_peer_id: int,
        destination_peer_id: int,
        destination_message_id: int | None,
        local_path: str | None,
    ) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO saved_media_sync(
                   saved_message_id, source_peer_id, destination_peer_id,
                   destination_message_id, local_path, created_at
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                message_id,
                source_peer_id,
                destination_peer_id,
                destination_message_id,
                local_path,
                utc_now(),
            ),
        )
        self.connection.commit()

    def mark_saved_message_summarized(
        self,
        message_id: int,
        summary_channel_peer_id: int,
        summary_message_id: int | None,
    ) -> None:
        self.connection.execute(
            """UPDATE saved_media_sync
               SET summary_channel_peer_id = ?,
                   summary_message_id = ?,
                   summary_at = ?
               WHERE saved_message_id = ?""",
            (summary_channel_peer_id, summary_message_id, utc_now(), message_id),
        )
        self.connection.commit()

    def saved_sync_count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) FROM saved_media_sync").fetchone()
        return int(row[0])

    def saved_summary_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM saved_media_sync WHERE summary_message_id IS NOT NULL"
        ).fetchone()
        return int(row[0])

    def forward_stats_between(self, start_at: str, end_at: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            """SELECT status, COUNT(*) AS count
               FROM forwarding_logs
               WHERE created_at >= ? AND created_at < ?
               GROUP BY status
               ORDER BY status""",
            (start_at, end_at),
        ).fetchall()

    def top_forward_sources_between(
        self, start_at: str, end_at: str, limit: int = 10
    ) -> list[sqlite3.Row]:
        return self.connection.execute(
            """SELECT source, status, COUNT(*) AS count
               FROM forwarding_logs
               WHERE created_at >= ? AND created_at < ?
               GROUP BY source, status
               ORDER BY count DESC
               LIMIT ?""",
            (start_at, end_at, limit),
        ).fetchall()

    def saved_sync_count_between(self, start_at: str, end_at: str) -> int:
        row = self.connection.execute(
            """SELECT COUNT(*) FROM saved_media_sync
               WHERE created_at >= ? AND created_at < ?""",
            (start_at, end_at),
        ).fetchone()
        return int(row[0])

    def saved_summary_count_between(self, start_at: str, end_at: str) -> int:
        row = self.connection.execute(
            """SELECT COUNT(*) FROM saved_media_sync
               WHERE summary_at >= ? AND summary_at < ?
                 AND summary_message_id IS NOT NULL""",
            (start_at, end_at),
        ).fetchone()
        return int(row[0])

    def saved_sync_source_count_between(self, start_at: str, end_at: str) -> int:
        row = self.connection.execute(
            """SELECT COUNT(DISTINCT source_peer_id) FROM saved_media_sync
               WHERE created_at >= ? AND created_at < ?""",
            (start_at, end_at),
        ).fetchone()
        return int(row[0] or 0)

    def add_resource_bot(self, username: str) -> None:
        self.connection.execute(
            """INSERT INTO resource_bots(username, created_at)
               VALUES (?, ?)
               ON CONFLICT(username) DO NOTHING""",
            (username, utc_now()),
        )
        self.connection.commit()

    def remove_resource_bot(self, username: str) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM resource_bots WHERE username = ?", (username,)
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def list_resource_bots(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT username FROM resource_bots ORDER BY username"
        ).fetchall()
        return [str(row[0]) for row in rows]

    def get_resource_link(self, bot_username: str, payload: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT bot_username, payload, source, source_message_id, status,
                      created_at, updated_at, last_error, start_message_id,
                      first_response_id, last_response_id, collected_count, forwarded_count
               FROM resource_bot_links
               WHERE bot_username = ? AND payload = ?""",
            (bot_username, payload),
        ).fetchone()

    def upsert_resource_link(
        self,
        bot_username: str,
        payload: str,
        source: str,
        source_message_id: int,
        status: str,
        last_error: str | None = None,
        start_message_id: int | None = None,
        first_response_id: int | None = None,
        last_response_id: int | None = None,
        collected_count: int | None = None,
        forwarded_count: int | None = None,
    ) -> None:
        now = utc_now()
        self.connection.execute(
            """INSERT INTO resource_bot_links(
                   bot_username, payload, source, source_message_id,
                   status, created_at, updated_at, last_error,
                   start_message_id, first_response_id, last_response_id,
                   collected_count, forwarded_count
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(bot_username, payload) DO UPDATE SET
                   source=excluded.source,
                   source_message_id=excluded.source_message_id,
                   status=excluded.status,
                   updated_at=excluded.updated_at,
                   last_error=excluded.last_error,
                   start_message_id=COALESCE(excluded.start_message_id, resource_bot_links.start_message_id),
                   first_response_id=COALESCE(excluded.first_response_id, resource_bot_links.first_response_id),
                   last_response_id=COALESCE(excluded.last_response_id, resource_bot_links.last_response_id),
                   collected_count=COALESCE(excluded.collected_count, resource_bot_links.collected_count),
                   forwarded_count=COALESCE(excluded.forwarded_count, resource_bot_links.forwarded_count)""",
            (
                bot_username,
                payload,
                source,
                source_message_id,
                status,
                now,
                now,
                last_error,
                start_message_id,
                first_response_id,
                last_response_id,
                collected_count,
                forwarded_count,
            ),
        )
        self.connection.commit()

    def resource_link_count(self, status: str | None = None) -> int:
        if status is None:
            row = self.connection.execute("SELECT COUNT(*) FROM resource_bot_links").fetchone()
        else:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM resource_bot_links WHERE status = ?", (status,)
            ).fetchone()
        return int(row[0])

    def close(self) -> None:
        self.connection.close()
