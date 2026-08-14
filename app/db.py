from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

DATA_DIR = Path('/app/data')
DB_PATH = DATA_DIR / 'iptv.db'


def ensure_dirs() -> None:
    (DATA_DIR / 'cache').mkdir(parents=True, exist_ok=True)
    (DATA_DIR / 'uploads').mkdir(parents=True, exist_ok=True)
    Path('/app/output').mkdir(parents=True, exist_ok=True)


@contextmanager
def connect():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA synchronous = NORMAL')
    conn.execute('PRAGMA temp_store = FILE')
    conn.execute('PRAGMA cache_size = -4096')  # ~4 MiB SQLite page cache per connection
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            '''
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
            CREATE INDEX IF NOT EXISTS idx_channels_sort ON channels(selected, sort_order);

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
                message TEXT,
                peak_rss_kb INTEGER
            );

            CREATE TABLE IF NOT EXISTS epg_channels (
                source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                tvg_id TEXT NOT NULL,
                display_name TEXT,
                PRIMARY KEY(source_id, tvg_id)
            );
            CREATE INDEX IF NOT EXISTS idx_epg_tvg ON epg_channels(tvg_id);
            CREATE INDEX IF NOT EXISTS idx_epg_name ON epg_channels(display_name);

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            '''
        )

        # Additive migrations from v0.1/v0.2 databases.
        cols = {r[1] for r in conn.execute('PRAGMA table_info(channels)').fetchall()}
        added_hls_mode = 'hls_mode' not in cols
        for name, ddl in {
            'custom_name': 'TEXT',
            'custom_group': 'TEXT',
            'custom_tvg_id': 'TEXT',
            'custom_logo': 'TEXT',
            'hls_proxy_enabled': 'INTEGER NOT NULL DEFAULT 0',
            'hls_max_height': 'INTEGER',
            'hls_mode': 'TEXT',
        }.items():
            if name not in cols:
                conn.execute(f'ALTER TABLE channels ADD COLUMN {name} {ddl}')

        source_cols = {r[1] for r in conn.execute('PRAGMA table_info(sources)').fetchall()}
        for name, ddl in {
            'hls_mode': "TEXT NOT NULL DEFAULT 'inherit'",
            'hls_max_height': 'INTEGER',
        }.items():
            if name not in source_cols:
                conn.execute(f'ALTER TABLE sources ADD COLUMN {name} {ddl}')

        # v0.3.1 had a boolean per-channel variant lock. Preserve that behavior exactly
        # on first v0.3.2 migration: ON -> fixed+compatibility; OFF -> direct.
        if added_hls_mode:
            conn.execute("UPDATE channels SET hls_mode=CASE WHEN hls_proxy_enabled=1 THEN 'fixed' ELSE 'direct' END")

        log_cols = {r[1] for r in conn.execute('PRAGMA table_info(refresh_log)').fetchall()}
        if 'peak_rss_kb' not in log_cols:
            conn.execute('ALTER TABLE refresh_log ADD COLUMN peak_rss_kb INTEGER')

        conn.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('resource_profile','low-memory')")
        conn.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('history_limit','10')")
        conn.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('hls_proxy_enabled','1')")
        conn.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('hls_proxy_default_height','720')")
        conn.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('hls_proxy_default_mode','direct')")
        conn.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('hls_proxy_cache_seconds','15')")


def get_setting(key: str, default: str) -> str:
    with connect() as conn:
        row = conn.execute('SELECT value FROM app_settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            'INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, value),
        )


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]
