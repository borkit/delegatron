"""Tests for the team workspace management module."""

from __future__ import annotations

from pathlib import Path

import pytest

from delegatron.server.db import init_db
from delegatron.server.teams import Team, TeamMember, TeamStore


@pytest.fixture()
def team_store(tmp_path: Path) -> TeamStore:
    db_path = tmp_path / "test.db"
    init_db(db_path)
    return TeamStore(db_path=db_path)


class TestTeamMember:
    def test_round_trip(self) -> None:
        m = TeamMember(user_id="u1", email="a@b.com", role="member")
        restored = TeamMember.from_dict(m.to_dict())
        assert restored.user_id == "u1"
        assert restored.role == "member"
        assert restored.joined_at != ""


class TestTeam:
    def test_round_trip(self) -> None:
        team = Team(
            team_id="t1",
            name="Acme",
            owner_id="u1",
            members=[TeamMember(user_id="u1", email="a@b.com", role="admin")],
            settings={"max_cost": 10.0},
        )
        restored = Team.from_dict(team.to_dict())
        assert restored.team_id == "t1"
        assert len(restored.members) == 1
        assert restored.settings["max_cost"] == 10.0


class TestTeamStore:
    def test_create_team(self, team_store: TeamStore) -> None:
        team = team_store.create_team("Acme", "owner1", owner_email="owner@acme.com")
        assert team.name == "Acme"
        assert team.owner_id == "owner1"
        assert len(team.members) == 1
        assert team.members[0].role == "admin"
        assert team.team_id != ""

    def test_get_team(self, team_store: TeamStore) -> None:
        created = team_store.create_team("Test", "u1")
        fetched = team_store.get_team(created.team_id)
        assert fetched is not None
        assert fetched.name == "Test"

    def test_get_team_not_found(self, team_store: TeamStore) -> None:
        assert team_store.get_team("nonexistent") is None

    def test_add_member(self, team_store: TeamStore) -> None:
        team = team_store.create_team("Test", "u1")
        member = team_store.add_member(team.team_id, "u2", "u2@test.com", role="member")
        assert member is not None
        assert member.user_id == "u2"

        refreshed = team_store.get_team(team.team_id)
        assert refreshed is not None
        assert len(refreshed.members) == 2

    def test_add_member_duplicate(self, team_store: TeamStore) -> None:
        team = team_store.create_team("Test", "u1")
        team_store.add_member(team.team_id, "u2", "u2@test.com")
        team_store.add_member(team.team_id, "u2", "u2@test.com")
        refreshed = team_store.get_team(team.team_id)
        assert refreshed is not None
        assert len(refreshed.members) == 2  # owner + u2, no duplicate

    def test_add_member_no_team(self, team_store: TeamStore) -> None:
        assert team_store.add_member("fake", "u1", "a@b.com") is None

    def test_remove_member(self, team_store: TeamStore) -> None:
        team = team_store.create_team("Test", "u1")
        team_store.add_member(team.team_id, "u2", "u2@test.com")
        assert team_store.remove_member(team.team_id, "u2") is True
        refreshed = team_store.get_team(team.team_id)
        assert refreshed is not None
        assert len(refreshed.members) == 1

    def test_remove_owner_raises(self, team_store: TeamStore) -> None:
        team = team_store.create_team("Test", "u1")
        with pytest.raises(ValueError, match="Cannot remove owner"):
            team_store.remove_member(team.team_id, "u1")

    def test_remove_member_not_found(self, team_store: TeamStore) -> None:
        team = team_store.create_team("Test", "u1")
        assert team_store.remove_member(team.team_id, "ghost") is False

    def test_remove_member_no_team(self, team_store: TeamStore) -> None:
        assert team_store.remove_member("fake", "u1") is False

    def test_list_teams(self, team_store: TeamStore) -> None:
        team_store.create_team("A", "u1")
        team_store.create_team("B", "u2")
        t3 = team_store.create_team("C", "u2")
        team_store.add_member(t3.team_id, "u1", "u1@test.com")

        u1_teams = team_store.list_teams("u1")
        assert len(u1_teams) == 2  # team A (owner) + team C (member)

    def test_update_settings(self, team_store: TeamStore) -> None:
        team = team_store.create_team("Test", "u1")
        assert team_store.update_settings(team.team_id, {"max_cost": 50.0}) is True
        refreshed = team_store.get_team(team.team_id)
        assert refreshed is not None
        assert refreshed.settings["max_cost"] == 50.0

    def test_update_settings_no_team(self, team_store: TeamStore) -> None:
        assert team_store.update_settings("fake", {"x": 1}) is False
