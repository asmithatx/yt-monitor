"""
database.py — SQLite persistence layer for yt-monitor.

Uses WAL (Write-Ahead Logging) mode for safe concurrent access between
the monitor script and the future Flask dashboard. All schema changes
should be made here via the migrate() function.
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional

import config

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────
#  Custom timestamp converter (handles tz-aware ISO strings)
# ────────────────────────────────────────────────────────────

def _convert_timestamp(val: bytes) -> datetime:
    """Handle both naive ('2026-02-18 16:00:00') and tz-aware ('...+00:00') timestamps."""
    s = val.decode()
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f+00:00",
        "%Y-%m-%d %H:%M:%S+00:00",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp from database: {s!r}")


sqlite3.register_converter("TIMESTAMP", _convert_timestamp)


# ────────────────────────────────────────────────────────────
#  Connection helpers
# ────────────────────────────────────────────────────────────

def _get_connection() -> sqlite3.Connection:
    """Return a new SQLite connection with sensible defaults."""
    db_path = Path(config.DATABASE_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        db_path,
        detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        timeout=30,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # Safe for concurrent readers
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL") # Good balance of safety/speed
    return conn


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Context manager that yields a connection and commits/rolls back."""
    conn = _get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────
#  Schema
# ────────────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS videos (
    video_id        TEXT PRIMARY KEY,
    channel_id      TEXT NOT NULL,
    channel_name    TEXT NOT NULL,
    title           TEXT NOT NULL,
    published_at    TIMESTAMP,

    -- Transcript extraction
    transcript_tier INTEGER,        -- 1=captions, 2=whisper, 3=metadata-only, NULL=pending
    transcript_text TEXT,

    -- Summarization
    summary_status  TEXT NOT NULL DEFAULT 'pending',
    -- Values: pending | processing | done | failed | skipped
    summary_text    TEXT,
    summary_error   TEXT,           -- Error message if summary_status='failed'
    tokens_input    INTEGER,
    tokens_output   INTEGER,

    -- Output delivery
    output_status   TEXT NOT NULL DEFAULT 'pending',
    -- Values: pending | done | failed
    output_ref      TEXT,           -- e.g. Trello card ID or dashboard row ID
    output_error    TEXT,

    -- Housekeeping
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Speed up common queries used by the dashboard
CREATE INDEX IF NOT EXISTS idx_videos_channel      ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_videos_published    ON videos(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_videos_summary_status ON videos(summary_status);

-- Trigger to keep updated_at current
CREATE TRIGGER IF NOT EXISTS trg_videos_updated_at
AFTER UPDATE ON videos
BEGIN
    UPDATE videos SET updated_at = CURRENT_TIMESTAMP WHERE video_id = NEW.video_id;
END;


CREATE TABLE IF NOT EXISTS channels (
    channel_id      TEXT PRIMARY KEY,
    channel_name    TEXT NOT NULL,
    last_checked_at TIMESTAMP,
    error_count     INTEGER NOT NULL DEFAULT 0,
    enabled         INTEGER NOT NULL DEFAULT 1  -- BOOLEAN: 1=enabled, 0=paused
);
"""


def migrate() -> None:
    """Initialise / migrate the database schema. Safe to call on every start."""
    with get_db() as conn:
        conn.executescript(_SCHEMA_SQL)
    logger.info("Database schema OK — %s", config.DATABASE_PATH)


# ────────────────────────────────────────────────────────────
#  Channel helpers
# ────────────────────────────────────────────────────────────

def upsert_channel(channel_id: str, channel_name: str) -> None:
    """Insert a channel record if it doesn't exist yet."""
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO channels (channel_id, channel_name)
            VALUES (?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET channel_name = excluded.channel_name
            """,
            (channel_id, channel_name),
        )


def mark_channel_checked(channel_id: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE channels SET last_checked_at = ?, error_count = 0 WHERE channel_id = ?",
            (datetime.now(timezone.utc).replace(tzinfo=None), channel_id),
        )


def increment_channel_error(channel_id: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE channels SET error_count = error_count + 1 WHERE channel_id = ?",
            (channel_id,),
        )


# ────────────────────────────────────────────────────────────
#  Video helpers
# ────────────────────────────────────────────────────────────

def is_video_known(video_id: str) -> bool:
    """Return True if this video_id is already in the database."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
    return row is not None


def insert_video(
    video_id: str,
    channel_id: str,
    channel_name: str,
    title: str,
    published_at: Optional[datetime] = None,
) -> None:
    """Insert a newly discovered video with status=pending."""
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO videos (video_id, channel_id, channel_name, title, published_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO NOTHING
            """,
            (video_id, channel_id, channel_name, title,
             published_at.replace(tzinfo=None) if published_at else None),
        )
    logger.debug("Inserted video %s — %s", video_id, title)


def update_transcript(
    video_id: str,
    tier: int,
    transcript_text: str,
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            UPDATE videos
            SET transcript_tier = ?, transcript_text = ?
            WHERE video_id = ?
            """,
            (tier, transcript_text, video_id),
        )


def update_summary(
    video_id: str,
    status: str,
    summary_text: Optional[str] = None,
    error: Optional[str] = None,
    tokens_input: Optional[int] = None,
    tokens_output: Optional[int] = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            UPDATE videos
            SET summary_status = ?, summary_text = ?, summary_error = ?,
                tokens_input = ?, tokens_output = ?
            WHERE video_id = ?
            """,
            (status, summary_text, error, tokens_input, tokens_output, video_id),
        )


def update_output(
    video_id: str,
    status: str,
    output_ref: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            UPDATE videos
            SET output_status = ?, output_ref = ?, output_error = ?
            WHERE video_id = ?
            """,
            (status, output_ref, error, video_id),
        )


def get_pending_videos() -> list[sqlite3.Row]:
    """Return videos that need summarization (transcript available, summary pending)."""
    with get_db() as conn:
        return conn.execute(
            """
            SELECT * FROM videos
            WHERE summary_status = 'pending'
              AND transcript_text IS NOT NULL
            ORDER BY published_at ASC
            """
        ).fetchall()


def get_pending_output_videos() -> list[sqlite3.Row]:
    """Return videos that have a summary but haven't been pushed to the output backend."""
    with get_db() as conn:
        return conn.execute(
            """
            SELECT * FROM videos
            WHERE summary_status = 'done'
              AND output_status = 'pending'
            ORDER BY published_at ASC
            """
        ).fetchall()


def get_recent_summaries(limit: int = 50) -> list[sqlite3.Row]:
    """Return recent completed summaries — used by the future Flask dashboard."""
    with get_db() as conn:
        return conn.execute(
            """
            SELECT * FROM videos
            WHERE summary_status = 'done'
            ORDER BY published_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


# ────────────────────────────────────────────────────────────
#  Seeder helpers
# ────────────────────────────────────────────────────────────

def get_all_video_ids() -> list[str]:
    """Return every video_id already stored in the videos table."""
    with get_db() as conn:
        rows = conn.execute("SELECT video_id FROM videos").fetchall()
    return [row[0] for row in rows]


def mark_video_seen(
    *,
    video_id: str,
    channel_id: str,
    channel_name: str,
    title: str,
    summary: str,
    transcript_tier: int = 3,
) -> None:
    """
    Insert a fully-processed video record so the polling loop never
    re-processes it.  Uses INSERT OR IGNORE so it is safe to call even
    if the row already exists (e.g. on a repeated container start).
    """
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO videos
                (video_id, channel_id, channel_name, title,
                 transcript_tier, summary_status, summary_text,
                 output_status, created_at, updated_at)
            VALUES
                (?, ?, ?, ?,
                 ?, 'done', ?,
                 'done', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (video_id, channel_id, channel_name, title,
             transcript_tier, summary),
        )
    logger.debug("Seeder: recorded %s in database", video_id)