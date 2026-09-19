"""SQLite manifest with atomic page/checkpoint commits."""

import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .security import RecoveryError, private_directory, private_file


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class VideoRecord:
    channel_id: int
    message_id: int
    date: str | None
    caption: str
    tags: list[str]
    title: str
    original_filename: str | None
    mime_type: str | None
    expected_size: int | None
    duration: float | None
    document_id: int
    suggested_filename: str


SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    date TEXT,
    caption TEXT NOT NULL,
    tags TEXT NOT NULL,
    title TEXT NOT NULL,
    original_filename TEXT,
    mime_type TEXT,
    expected_size INTEGER,
    duration REAL,
    document_id INTEGER NOT NULL,
    suggested_filename TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'inventoried',
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_id, message_id)
);
CREATE TABLE IF NOT EXISTS inventory_scans (
    channel_id INTEGER NOT NULL,
    scope TEXT NOT NULL,
    last_message_id INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_id, scope)
);
CREATE TABLE IF NOT EXISTS downloads (
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    expected_size INTEGER NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    owned_part INTEGER NOT NULL DEFAULT 0,
    bytes_downloaded INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT,
    partial_sha256 TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    downloaded_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_id, message_id)
);
PRAGMA user_version = 2;
"""


class Database:
    def __init__(self, path: Path, *, readonly: bool = False):
        self.path = path
        if readonly:
            if path.is_symlink():
                raise RecoveryError("O manifesto não pode ser um link simbólico.")
            self.connection = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)
        else:
            private_directory(path.parent)
            private_file(path)
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        try:
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise RecoveryError(
                    "Versão do manifesto não suportada; arquivo preservado.",
                    code="manifest_version",
                )
            if not readonly:
                self.connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
            self.has_downloads = not readonly or version == 2
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.connection.close()

    def cursor(self, channel_id: int, scope: str) -> int:
        row = self.connection.execute(
            "SELECT last_message_id FROM inventory_scans WHERE channel_id=? AND scope=?",
            (channel_id, scope),
        ).fetchone()
        return row[0] if row else 0

    def _save_cursor(self, channel_id: int, scope: str, cursor: int, status: str) -> None:
        self.connection.execute(
            """INSERT INTO inventory_scans VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, scope) DO UPDATE SET
            last_message_id=excluded.last_message_id, status=excluded.status,
            updated_at=excluded.updated_at""",
            (channel_id, scope, cursor, status, utc_now()),
        )

    def set_scan(self, channel_id: int, scope: str, cursor: int, status: str) -> None:
        with self.connection:
            self._save_cursor(channel_id, scope, cursor, status)

    def _save_records(self, records: list[VideoRecord]):
        # Caller owns the transaction, so history records and cursor remain atomic.
        inserted = 0
        for record in records:
            values = asdict(record)
            values["tags"] = json.dumps(record.tags)
            values["now"] = utc_now()
            previous = self.connection.execute(
                "SELECT 1 FROM videos WHERE channel_id=? AND message_id=?",
                (record.channel_id, record.message_id),
            ).fetchone()
            inserted += previous is None
            self.connection.execute(
                """INSERT INTO videos (
                    channel_id, message_id, date, caption, tags, title, original_filename,
                    mime_type, expected_size, duration, document_id, suggested_filename,
                    first_seen_at, updated_at
                ) VALUES (
                    :channel_id, :message_id, :date, :caption, :tags, :title,
                    :original_filename, :mime_type, :expected_size, :duration,
                    :document_id, :suggested_filename, :now, :now
                ) ON CONFLICT(channel_id, message_id) DO UPDATE SET
                    date=excluded.date, caption=excluded.caption, tags=excluded.tags,
                    title=excluded.title, original_filename=excluded.original_filename,
                    mime_type=excluded.mime_type, expected_size=excluded.expected_size,
                    duration=excluded.duration, document_id=excluded.document_id,
                    suggested_filename=excluded.suggested_filename,
                    updated_at=excluded.updated_at,
                    status=CASE WHEN videos.document_id != excluded.document_id
                        OR videos.expected_size IS NOT excluded.expected_size
                        THEN 'media_changed' ELSE videos.status END""",
                values,
            )
        return inserted, len(records) - inserted

    def save_records(self, records: list[VideoRecord]):
        """Refresh individual messages without creating or changing a history checkpoint."""
        with self.connection:
            return self._save_records(records)

    def save_page(self, records: list[VideoRecord], channel_id: int, scope: str, cursor: int):
        with self.connection:
            counts = self._save_records(records)
            self._save_cursor(channel_id, scope, cursor, "running")
        return counts

    def videos(self, channel_id: int | None = None):
        where = " WHERE channel_id=?" if channel_id is not None else ""
        params = (channel_id,) if channel_id is not None else ()
        for row in self.connection.execute(
            "SELECT * FROM videos" + where + " ORDER BY channel_id, message_id", params
        ):
            result = dict(row)
            result["tags"] = json.loads(result["tags"])
            yield result

    def summary(self) -> dict:
        row = self.connection.execute(
            """SELECT COUNT(*) AS videos, COUNT(DISTINCT channel_id) AS channels,
            COALESCE(SUM(expected_size), 0) AS expected_bytes,
            COALESCE(SUM(expected_size IS NULL), 0) AS unknown_sizes,
            COALESCE(SUM(tags = '[]'), 0) AS without_tags FROM videos"""
        ).fetchone()
        statuses = self.connection.execute(
            "SELECT status, COUNT(*) FROM videos GROUP BY status ORDER BY status"
        )
        scans = self.connection.execute("SELECT * FROM inventory_scans ORDER BY channel_id, scope")
        downloads = (
            dict(self.connection.execute("SELECT status, COUNT(*) FROM downloads GROUP BY status"))
            if self.has_downloads
            else {}
        )
        return {
            **dict(row),
            "statuses": dict(statuses),
            "scans": [dict(row) for row in scans],
            "downloads": downloads,
        }

    def download(self, channel_id: int, message_id: int) -> dict | None:
        if not self.has_downloads:
            return None
        row = self.connection.execute(
            "SELECT * FROM downloads WHERE channel_id=? AND message_id=?",
            (channel_id, message_id),
        ).fetchone()
        return dict(row) if row else None

    def add_download(self, video: dict, relative_path: str):
        with self.connection:
            self.connection.execute(
                """INSERT INTO downloads
                (channel_id, message_id, document_id, expected_size, relative_path, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    video["channel_id"],
                    video["message_id"],
                    video["document_id"],
                    video["expected_size"],
                    relative_path,
                    utc_now(),
                ),
            )

    def update_download(self, channel_id: int, message_id: int, **fields):
        allowed = {
            "status",
            "owned_part",
            "bytes_downloaded",
            "sha256",
            "partial_sha256",
            "attempts",
            "error_code",
            "downloaded_at",
        }
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid download update fields")
        fields["updated_at"] = utc_now()
        with self.connection:
            self.connection.execute(
                "UPDATE downloads SET "
                + ", ".join(f"{key}=?" for key in fields)
                + " WHERE channel_id=? AND message_id=?",
                (*fields.values(), channel_id, message_id),
            )
