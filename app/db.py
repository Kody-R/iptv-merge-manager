from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

DATA_DIR = Path("/app/data")
DB_PATH = DATA_DIR / "iptv.db"


def ensure_dirs() -> None:
    (DATA_DIR / "cache").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "uploads").mkdir(parents=True, exist_ok=True)
    Path("/app/output").mkdir(parents=True, exist_ok=True)


@contextmanager
def connect():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                m3u_kind TEXT NOT NULL CHECK(m3u_kind IN ('url','file')),
                m3u_value TEXT NOT NULL,
                xml_kind TEXT CHECK(xml_kind IN ('url','file')),
                xml_value TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_status TEXT NOT NULL DEFAULT 'Never refreshed',
                last_refresh TEXT,
                last_error TEXT,
                channel_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                stable_key TEXT NOT NULL,
                tvg_id TEXT,
                name TEXT NOT NULL,
                group_title TEXT,
                logo TEXT,
                stream_url TEXT NOT NULL,
                selected INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                channel_number INTEGER,
                is_active INTEGER NOT NULL DEFAULT 1,
                first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source_id, stable_key)
            );

            CREATE INDEX IF NOT EXISTS idx_channels_source ON channels(source_id);
            CREATE INDEX IF NOT EXISTS idx_channels_selected ON channels(selected, is_active);
            CREATE INDEX IF NOT EXISTS idx_channels_tvg ON channels(tvg_id);

            CREATE TABLE IF NOT EXISTS lineup_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                sort_order INTEGER NOT NULL DEFAULT 0,
                number_start INTEGER NOT NULL DEFAULT 1,
                number_increment INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS refresh_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                message TEXT
            );
            """
        )
        # v0.2 additive migrations; safe on existing v0.1.x databases.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(channels)").fetchall()}
        for name, ddl in {
            "custom_name": "TEXT",
            "custom_group": "TEXT",
            "custom_tvg_id": "TEXT",
            "custom_logo": "TEXT",
        }.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE channels ADD COLUMN {name} {ddl}")


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]
