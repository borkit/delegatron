"""Tests for the authentication / API key management module."""

from __future__ import annotations

from pathlib import Path

import pytest

from delegatron.server.auth import APIKey, APIKeyManager, _hash_key
from delegatron.server.db import init_db


@pytest.fixture()
def key_manager(tmp_path: Path) -> APIKeyManager:
    db_path = tmp_path / "test.db"
    init_db(db_path)
    return APIKeyManager(db_path=db_path)


class TestAPIKeyDataclass:
    def test_round_trip(self) -> None:
        key = APIKey(
            key_id="abc123",
            key_hash="deadbeef",
            user_id="u1",
            team_id="t1",
            scopes=["run"],
            created_at="2026-01-01T00:00:00+00:00",
        )
        restored = APIKey.from_dict(key.to_dict())
        assert restored.key_id == key.key_id
        assert restored.scopes == ["run"]
        assert restored.is_active is True


class TestAPIKeyManager:
    def test_create_and_validate(self, key_manager: APIKeyManager) -> None:
        raw_key, record = key_manager.create_key("user1", "team1")
        assert raw_key.startswith("dlg_")
        assert record.is_active is True
        assert record.user_id == "user1"
        assert record.team_id == "team1"

        validated = key_manager.validate_key(raw_key)
        assert validated is not None
        assert validated.key_id == record.key_id

    def test_validate_bad_key(self, key_manager: APIKeyManager) -> None:
        assert key_manager.validate_key("dlg_nonexistent") is None

    def test_validate_wrong_prefix(self, key_manager: APIKeyManager) -> None:
        assert key_manager.validate_key("bad_prefix_key") is None

    def test_revoke_key(self, key_manager: APIKeyManager) -> None:
        raw_key, record = key_manager.create_key("user1", "team1")
        key_manager.revoke_key(record.key_id)
        assert key_manager.validate_key(raw_key) is None

    def test_list_keys(self, key_manager: APIKeyManager) -> None:
        key_manager.create_key("u1", "teamA")
        key_manager.create_key("u2", "teamA")
        key_manager.create_key("u3", "teamB")

        keys_a = key_manager.list_keys("teamA")
        assert len(keys_a) == 2
        assert all(k.team_id == "teamA" for k in keys_a)

        keys_b = key_manager.list_keys("teamB")
        assert len(keys_b) == 1

    def test_custom_scopes(self, key_manager: APIKeyManager) -> None:
        raw_key, record = key_manager.create_key("u1", "t1", scopes=["run", "admin"])
        assert record.scopes == ["run", "admin"]
        validated = key_manager.validate_key(raw_key)
        assert validated is not None
        assert validated.scopes == ["run", "admin"]

    def test_expired_key(self, key_manager: APIKeyManager) -> None:
        raw_key, _ = key_manager.create_key("u1", "t1", expires_at="2020-01-01T00:00:00+00:00")
        assert key_manager.validate_key(raw_key) is None

    def test_hash_deterministic(self) -> None:
        assert _hash_key("test") == _hash_key("test")
        assert _hash_key("a") != _hash_key("b")
