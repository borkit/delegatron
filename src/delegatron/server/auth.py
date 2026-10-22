"""Authentication — API key management and user identity."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from delegatron.server.db import get_db

logger = logging.getLogger(__name__)

KEY_PREFIX = "dlg_"


@dataclass
class APIKey:
    """A stored API key record (hash only, never the raw key)."""

    key_id: str
    key_hash: str
    user_id: str
    team_id: str
    scopes: list[str]
    created_at: str
    expires_at: str | None = None
    is_active: bool = True

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> APIKey:
        filtered: dict[str, Any] = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**filtered)


@dataclass
class AuthUser:
    """An authenticated user identity."""

    user_id: str
    email: str
    team_id: str
    role: str  # "admin", "member", "viewer"


def _hash_key(raw_key: str) -> str:
    """SHA-256 hash of a raw API key."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _generate_raw_key() -> str:
    """Generate a random API key with the dlg_ prefix."""
    return KEY_PREFIX + secrets.token_hex(24)


def _row_to_api_key(row: Any) -> APIKey:
    """Convert a sqlite3.Row to an APIKey dataclass."""
    return APIKey(
        key_id=row["key_id"],
        key_hash=row["key_hash"],
        user_id=row["user_id"],
        team_id=row["team_id"],
        scopes=json.loads(row["scopes"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        is_active=bool(row["is_active"]),
    )


class APIKeyManager:
    """Manages API keys with SQLite-backed persistence.

    Keys are stored as SHA-256 hashes; the raw key is returned only
    at creation time and never persisted.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    def create_key(
        self,
        user_id: str,
        team_id: str,
        scopes: list[str] | None = None,
        expires_at: str | None = None,
    ) -> tuple[str, APIKey]:
        """Create a new API key. Returns (raw_key, key_record)."""
        raw_key = _generate_raw_key()
        key_id = secrets.token_hex(8)
        key_hash = _hash_key(raw_key)
        scopes_list = scopes or ["run", "read"]
        created_at = datetime.now(UTC).isoformat()

        api_key = APIKey(
            key_id=key_id,
            key_hash=key_hash,
            user_id=user_id,
            team_id=team_id,
            scopes=scopes_list,
            created_at=created_at,
            expires_at=expires_at,
            is_active=True,
        )

        conn = get_db(self._db_path)
        try:
            conn.execute(
                "INSERT INTO api_keys"
                " (key_id, key_hash, user_id, team_id,"
                " scopes, created_at, expires_at, is_active)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key_id,
                    key_hash,
                    user_id,
                    team_id,
                    json.dumps(scopes_list),
                    created_at,
                    expires_at,
                    1,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        logger.info("Created API key %s for user %s", key_id, user_id)
        return raw_key, api_key

    def validate_key(self, raw_key: str) -> APIKey | None:
        """Validate a raw API key. Returns the key record or None."""
        if not raw_key.startswith(KEY_PREFIX):
            return None

        key_hash = _hash_key(raw_key)

        conn = get_db(self._db_path)
        try:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE key_hash = ? AND is_active = 1",
                (key_hash,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            return None

        api_key = _row_to_api_key(row)
        if api_key.expires_at:
            expires = datetime.fromisoformat(api_key.expires_at)
            # Ensure timezone-aware comparison — treat naive timestamps as UTC
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires < datetime.now(UTC):
                return None
        return api_key

    def revoke_key(self, key_id: str) -> None:
        """Revoke an API key by its ID."""
        conn = get_db(self._db_path)
        try:
            conn.execute("UPDATE api_keys SET is_active = 0 WHERE key_id = ?", (key_id,))
            conn.commit()
        finally:
            conn.close()
        logger.info("Revoked API key %s", key_id)

    def list_keys(self, team_id: str) -> list[APIKey]:
        """List all keys for a team."""
        conn = get_db(self._db_path)
        try:
            rows = conn.execute("SELECT * FROM api_keys WHERE team_id = ?", (team_id,)).fetchall()
        finally:
            conn.close()
        return [_row_to_api_key(r) for r in rows]
