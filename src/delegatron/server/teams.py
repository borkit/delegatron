"""Team workspace management — create and manage team workspaces."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from delegatron.server.db import get_db

logger = logging.getLogger(__name__)


@dataclass
class TeamMember:
    """A member of a team."""

    user_id: str
    email: str
    role: str  # "admin", "member", "viewer"
    joined_at: str = ""

    def __post_init__(self) -> None:
        if not self.joined_at:
            self.joined_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TeamMember:
        filtered: dict[str, Any] = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**filtered)


@dataclass
class Team:
    """A team workspace."""

    team_id: str
    name: str
    owner_id: str
    members: list[TeamMember] = field(default_factory=list)
    created_at: str = ""
    settings: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Team:
        members_data = data.get("members", [])
        members = [TeamMember.from_dict(m) for m in members_data]
        return cls(
            team_id=data["team_id"],
            name=data["name"],
            owner_id=data["owner_id"],
            members=members,
            created_at=data.get("created_at", ""),
            settings=data.get("settings", {}),
        )


def _load_team_with_members(conn: Any, team_id: str) -> Team | None:
    """Load a team and its members from the database."""
    row = conn.execute("SELECT * FROM teams WHERE team_id = ?", (team_id,)).fetchone()
    if row is None:
        return None
    members_rows = conn.execute(
        "SELECT * FROM team_members WHERE team_id = ?", (team_id,)
    ).fetchall()
    members = [
        TeamMember(
            user_id=m["user_id"],
            email=m["email"],
            role=m["role"],
            joined_at=m["joined_at"],
        )
        for m in members_rows
    ]
    return Team(
        team_id=row["team_id"],
        name=row["name"],
        owner_id=row["owner_id"],
        members=members,
        created_at=row["created_at"],
        settings=json.loads(row["settings"]),
    )


class TeamStore:
    """SQLite-backed team storage.

    Provides CRUD operations for teams and their members.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    def create_team(self, name: str, owner_id: str, owner_email: str = "") -> Team:
        """Create a new team with the owner as the first admin member."""
        team_id = uuid.uuid4().hex[:12]
        created_at = datetime.now(UTC).isoformat()
        owner_member = TeamMember(
            user_id=owner_id,
            email=owner_email,
            role="admin",
        )

        conn = get_db(self._db_path)
        try:
            conn.execute(
                "INSERT INTO teams (team_id, name, owner_id, created_at, settings)"
                " VALUES (?, ?, ?, ?, ?)",
                (team_id, name, owner_id, created_at, "{}"),
            )
            conn.execute(
                "INSERT INTO team_members (team_id, user_id, email, role, joined_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (team_id, owner_id, owner_email, "admin", owner_member.joined_at),
            )
            conn.commit()
        finally:
            conn.close()

        team = Team(
            team_id=team_id,
            name=name,
            owner_id=owner_id,
            members=[owner_member],
            created_at=created_at,
        )
        logger.info("Created team %s (%s) for owner %s", team_id, name, owner_id)
        return team

    def get_team(self, team_id: str) -> Team | None:
        """Get a team by ID. Returns None if not found."""
        conn = get_db(self._db_path)
        try:
            return _load_team_with_members(conn, team_id)
        finally:
            conn.close()

    def add_member(
        self, team_id: str, user_id: str, email: str, role: str = "member"
    ) -> TeamMember | None:
        """Add a member to a team. Returns None if team not found."""
        conn = get_db(self._db_path)
        try:
            # Check team exists
            team_row = conn.execute(
                "SELECT team_id FROM teams WHERE team_id = ?",
                (team_id,),
            ).fetchone()
            if team_row is None:
                return None

            # Check for duplicate
            existing = conn.execute(
                "SELECT * FROM team_members WHERE team_id = ? AND user_id = ?",
                (team_id, user_id),
            ).fetchone()
            if existing is not None:
                return TeamMember(
                    user_id=existing["user_id"],
                    email=existing["email"],
                    role=existing["role"],
                    joined_at=existing["joined_at"],
                )

            member = TeamMember(user_id=user_id, email=email, role=role)
            conn.execute(
                "INSERT INTO team_members (team_id, user_id, email, role, joined_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (team_id, user_id, email, role, member.joined_at),
            )
            conn.commit()
            logger.info("Added member %s to team %s", user_id, team_id)
            return member
        finally:
            conn.close()

    def remove_member(self, team_id: str, user_id: str) -> bool:
        """Remove a member from a team. Returns True if removed.

        Raises:
            ValueError: If the member being removed is the team owner.
        """
        conn = get_db(self._db_path)
        try:
            team_row = conn.execute(
                "SELECT owner_id FROM teams WHERE team_id = ?", (team_id,)
            ).fetchone()
            if team_row is None:
                return False

            if team_row["owner_id"] == user_id:
                msg = f"Cannot remove owner '{user_id}' from team '{team_id}'"
                raise ValueError(msg)

            cursor = conn.execute(
                "DELETE FROM team_members WHERE team_id = ? AND user_id = ?",
                (team_id, user_id),
            )
            conn.commit()
            if cursor.rowcount > 0:
                logger.info("Removed member %s from team %s", user_id, team_id)
                return True
            return False
        finally:
            conn.close()

    def list_teams(self, user_id: str) -> list[Team]:
        """List all teams a user belongs to."""
        conn = get_db(self._db_path)
        try:
            team_ids = conn.execute(
                "SELECT team_id FROM team_members WHERE user_id = ?", (user_id,)
            ).fetchall()
            teams: list[Team] = []
            for row in team_ids:
                team = _load_team_with_members(conn, row["team_id"])
                if team is not None:
                    teams.append(team)
            return teams
        finally:
            conn.close()

    def update_settings(self, team_id: str, settings: dict[str, Any]) -> bool:
        """Update team settings. Returns True if team was found and updated."""
        conn = get_db(self._db_path)
        try:
            row = conn.execute(
                "SELECT settings FROM teams WHERE team_id = ?",
                (team_id,),
            ).fetchone()
            if row is None:
                return False
            _ALLOWED_SETTINGS = {
                "preset",
                "max_iterations",
                "max_cost_usd",
                "max_cost",
                "max_wall_time_minutes",
                "sandbox",
                "docker_image",
                "architect_model",
                "implementer_model",
                "reviewer_model",
            }
            current = json.loads(row["settings"])
            filtered = {k: v for k, v in settings.items() if k in _ALLOWED_SETTINGS}
            current.update(filtered)
            conn.execute(
                "UPDATE teams SET settings = ? WHERE team_id = ?",
                (json.dumps(current), team_id),
            )
            conn.commit()
            logger.info("Updated settings for team %s", team_id)
            return True
        finally:
            conn.close()
