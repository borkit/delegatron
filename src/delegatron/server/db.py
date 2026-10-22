"""Shared SQLite database for Delegatron server stores."""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path(os.environ.get("DELEGATRON_DATA_DIR", "data")) / "delegatron.db"


def get_db(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Return a new SQLite connection with WAL mode and foreign keys enabled."""
    path = str(db_path or _DEFAULT_DB_PATH)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | Path | None = None) -> None:
    """Create all tables if they don't already exist."""
    path = Path(db_path or _DEFAULT_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = get_db(path)
    try:
        conn.executescript(
            """\
            CREATE TABLE IF NOT EXISTS api_keys (
                key_id      TEXT PRIMARY KEY,
                key_hash    TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                team_id     TEXT NOT NULL,
                scopes      TEXT NOT NULL DEFAULT '["run","read"]',
                created_at  TEXT NOT NULL,
                expires_at  TEXT,
                is_active   INTEGER NOT NULL DEFAULT 1
            );

            CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
            CREATE INDEX IF NOT EXISTS idx_api_keys_team ON api_keys(team_id);

            CREATE TABLE IF NOT EXISTS teams (
                team_id     TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                owner_id    TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                settings    TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS team_members (
                team_id     TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                email       TEXT NOT NULL DEFAULT '',
                role        TEXT NOT NULL DEFAULT 'member',
                joined_at   TEXT NOT NULL,
                PRIMARY KEY (team_id, user_id),
                FOREIGN KEY (team_id) REFERENCES teams(team_id) ON DELETE CASCADE
            );
            """
        )
        conn.commit()
        logger.info("Database initialized at %s", path)
    finally:
        conn.close()
