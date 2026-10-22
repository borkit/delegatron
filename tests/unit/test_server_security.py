"""Server access control: signed stream URLs, admin-only endpoints, run ids, workspace root."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import delegatron.server.app as app_module
import delegatron.utils.logging as log_mod
from delegatron.server.app import app, configure
from delegatron.server.db import init_db


@pytest.fixture(autouse=True)
def _isolated_server_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Own data dir and feature flags; settings endpoints' env writes are undone."""
    monkeypatch.setattr(app_module, "_data_dir", tmp_path / "data")
    monkeypatch.setattr(
        app_module, "_feature_flags", {"parallel_tasks": False, "project_mode": False}
    )
    monkeypatch.delenv("DELEGATRON_PARALLEL_TASKS", raising=False)
    monkeypatch.delenv("DELEGATRON_PROJECT_MODE", raising=False)
    monkeypatch.delenv("DELEGATRON_WORKSPACE_ROOT", raising=False)
    monkeypatch.setattr(log_mod, "LOG_TIMEZONE", log_mod.LOG_TIMEZONE)
    delegatron_logger = logging.getLogger("delegatron")
    level = delegatron_logger.level
    yield
    delegatron_logger.setLevel(level)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DELEGATRON_AUTH_ENABLED", "false")
    configure(workspace=tmp_path, artifacts=tmp_path / "runs")
    return TestClient(app)


@pytest.fixture
def auth_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DELEGATRON_AUTH_ENABLED", "true")
    configure(workspace=tmp_path, artifacts=tmp_path / "runs")
    db_path = tmp_path / "test.db"
    init_db(db_path)
    monkeypatch.setattr(app_module.api_key_manager, "_db_path", str(db_path))
    monkeypatch.setattr(app_module.team_store, "_db_path", str(db_path))
    return TestClient(app)


class _Team:
    """A team with an admin (its creator) and a plain member, each with an API key."""

    def __init__(self) -> None:
        team = app_module.team_store.create_team("Team", "admin-user", "admin@example.com")
        app_module.team_store.add_member(team.team_id, "member-user", "m@example.com", "member")
        self.team_id = team.team_id
        self.admin_key, _ = app_module.api_key_manager.create_key(
            user_id="admin-user", team_id=team.team_id
        )
        self.member_key, _ = app_module.api_key_manager.create_key(
            user_id="member-user", team_id=team.team_id
        )

    @property
    def admin(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.admin_key}"}

    @property
    def member(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.member_key}"}


@pytest.fixture
def team(auth_client: TestClient, tmp_path: Path) -> _Team:
    created = _Team()
    run_dir = tmp_path / "runs" / "my-run"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({"team_id": created.team_id}))
    (run_dir / "events.jsonl").write_text(json.dumps({"event": "pipeline_start"}) + "\n")
    return created


# --- Signed stream URLs -------------------------------------------------------

EVENTS = "/api/runs/my-run/events"


def _signed_url(client: TestClient, headers: dict[str, str], path: str = EVENTS) -> str:
    resp = client.get("/api/stream-token", params={"path": path}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["expires_in_seconds"] == app_module._STREAM_TOKEN_TTL
    return str(resp.json()["url"])


def _with_params(url: str, **changes: str) -> tuple[str, dict[str, str]]:
    parts = urlsplit(url)
    params = {k: v[0] for k, v in parse_qs(parts.query).items()}
    return parts.path, {**params, **changes}


def test_stream_token_signs_the_callers_identity(auth_client: TestClient, team: _Team) -> None:
    url = _signed_url(auth_client, team.member)

    path, params = _with_params(url)
    assert path == EVENTS
    assert (params["uid"], params["tid"], params["role"]) == ("member-user", team.team_id, "member")

    resp = auth_client.get(path, params=params)  # no Authorization header
    assert resp.status_code == 200
    assert "pipeline_start" in resp.text


def test_stream_token_requires_auth_and_a_run_path(auth_client: TestClient, team: _Team) -> None:
    assert auth_client.get("/api/stream-token", params={"path": EVENTS}).status_code == 401
    resp = auth_client.get("/api/stream-token", params={"path": "/api/teams"}, headers=team.admin)
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "change",
    [
        {"sig": "0" * 64},  # tampered signature
        {"role": "admin"},  # escalated role
        {"uid": "admin-user"},  # someone else
        {"tid": "other-team"},  # another team
    ],
)
def test_altered_signed_url_is_rejected(
    auth_client: TestClient, team: _Team, change: dict[str, str]
) -> None:
    path, params = _with_params(_signed_url(auth_client, team.member), **change)

    resp = auth_client.get(path, params=params)

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid signature"


def test_signed_url_is_bound_to_its_path(
    auth_client: TestClient, team: _Team, tmp_path: Path
) -> None:
    other = tmp_path / "runs" / "other-run"
    other.mkdir(parents=True)
    (other / "meta.json").write_text(json.dumps({"team_id": team.team_id}))
    _path, params = _with_params(_signed_url(auth_client, team.member))

    resp = auth_client.get("/api/runs/other-run/events", params=params)

    assert resp.status_code == 401


def test_expired_signed_url_is_rejected(auth_client: TestClient, team: _Team) -> None:
    """Correctly signed, but past its expiry."""
    expires = str(int(time.time()) - 1)
    payload = f"{EVENTS}:{expires}:member-user:{team.team_id}:member"
    assert app_module._STREAM_TOKEN_SECRET is not None
    sig = hmac.new(
        app_module._STREAM_TOKEN_SECRET.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    params = {"sig": sig, "expires": expires, "uid": "member-user", "tid": team.team_id}

    resp = auth_client.get(EVENTS, params={**params, "role": "member"})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Signed URL expired"


def test_signed_url_missing_a_parameter_is_rejected(auth_client: TestClient, team: _Team) -> None:
    path, params = _with_params(_signed_url(auth_client, team.member))
    del params["role"]

    assert auth_client.get(path, params=params).status_code == 401


# --- Admin-only endpoints -----------------------------------------------------

SETTINGS = [
    ("/api/settings/log-level", {"level": "DEBUG"}, {"status": "ok", "level": "DEBUG"}),
    ("/api/settings/log-timezone", {"timezone": "local"}, {"status": "ok", "timezone": "local"}),
    ("/api/settings/parallel-tasks", {"enabled": True}, {"parallel_tasks": True}),
    ("/api/settings/project-mode", {"enabled": True}, {"project_mode": True}),
]


@pytest.mark.parametrize(("endpoint", "body", "_expected"), SETTINGS)
def test_settings_need_an_admin(
    auth_client: TestClient, team: _Team, endpoint: str, body: dict[str, Any], _expected: Any
) -> None:
    flags = dict(app_module._feature_flags)
    timezone = log_mod.LOG_TIMEZONE

    resp = auth_client.put(endpoint, json=body, headers=team.member)

    assert resp.status_code == 403
    assert app_module._feature_flags == flags
    assert timezone == log_mod.LOG_TIMEZONE


@pytest.mark.parametrize(("endpoint", "body", "expected"), SETTINGS)
def test_admin_can_change_settings(
    auth_client: TestClient,
    team: _Team,
    endpoint: str,
    body: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    resp = auth_client.put(endpoint, json=body, headers=team.admin)

    assert resp.status_code == 200
    assert resp.json() == expected


class _Restart:
    """Stands in for everything /api/ollama/restart touches: HTTP, processes, sleeps."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.unloaded: list[str] = []


@pytest.fixture
def restart(monkeypatch: pytest.MonkeyPatch) -> _Restart:
    import httpx

    stub = _Restart()

    class _Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"models": [{"name": "m:7b"}]}

    class _Client:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        async def get(self, _url: str) -> _Response:
            return _Response()

        async def post(self, _url: str, json: dict[str, Any]) -> _Response:
            stub.unloaded.append(json["model"])
            return _Response()

    def _command(cmd: list[str], *_args: Any, **_kwargs: Any) -> None:
        stub.commands.append(cmd)

    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(app_module.subprocess, "run", _command)
    monkeypatch.setattr(app_module.subprocess, "Popen", _command)
    monkeypatch.setattr(app_module.asyncio, "sleep", _no_sleep)
    return stub


def test_ollama_restart_needs_an_admin(
    auth_client: TestClient, team: _Team, restart: _Restart
) -> None:
    resp = auth_client.post("/api/ollama/restart", headers=team.member)

    assert resp.status_code == 403
    assert restart.commands == []
    assert restart.unloaded == []


def test_admin_can_restart_ollama(auth_client: TestClient, team: _Team, restart: _Restart) -> None:
    resp = auth_client.post("/api/ollama/restart", headers=team.admin)

    assert resp.status_code == 200
    assert resp.json()["status"] == "restarted"
    assert resp.json()["models_available"] == 1
    assert restart.unloaded == ["m:7b"]
    assert len(restart.commands) == 2  # stop, then start (stubbed: nothing ran)


# --- Run ids ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/runs/bad.id"),
        ("DELETE", "/api/runs/bad.id"),
        ("POST", "/api/runs/bad$id/cancel"),
        ("GET", "/api/runs/bad.id/events"),
    ],
)
def test_malformed_run_id_is_a_400(client: TestClient, method: str, path: str) -> None:
    resp = client.request(method, path)

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid run_id"


# --- DELEGATRON_WORKSPACE_ROOT ------------------------------------------------


@pytest.fixture
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(inside, outside) directories for an allowed root of tmp_path/allowed."""
    allowed = tmp_path / "allowed"
    inside = allowed / "project"
    outside = tmp_path / "outside"
    inside.mkdir(parents=True)
    outside.mkdir()
    monkeypatch.setenv("DELEGATRON_WORKSPACE_ROOT", str(allowed))
    return inside, outside


@pytest.fixture
def started_runs(monkeypatch: pytest.MonkeyPatch) -> list[app_module.RunRequest]:
    calls: list[app_module.RunRequest] = []

    def _fake_execute_run(run_id: str, req: app_module.RunRequest, **_kwargs: Any) -> Any:
        calls.append(req)
        return asyncio.sleep(0)

    monkeypatch.setattr(app_module, "_execute_run", _fake_execute_run)
    return calls


def test_run_workspace_must_lie_under_the_root(
    client: TestClient,
    workspace_root: tuple[Path, Path],
    started_runs: list[app_module.RunRequest],
) -> None:
    inside, outside = workspace_root

    rejected = client.post("/api/runs", json={"request": "x", "workspace": str(outside)})
    accepted = client.post("/api/runs", json={"request": "x", "workspace": str(inside)})

    assert rejected.status_code == 422
    assert "Workspace must be under" in rejected.text
    assert accepted.status_code == 200
    assert [req.workspace for req in started_runs] == [str(inside)]


class _PlanPipeline:
    """graph.Pipeline for /api/runs/plan: records the workspace, returns a one-task plan."""

    workspaces: ClassVar[list[Path]] = []

    def __init__(self, **kwargs: Any) -> None:
        _PlanPipeline.workspaces.append(kwargs["workspace_root"])

    def _create_architect(self) -> object:
        return object()

    async def _run_architect(self, state: dict[str, Any], *_args: Any) -> dict[str, Any]:
        plan = {"summary": "one task", "tasks": [{"task_id": "t1"}], "assumptions": []}
        return {**state, "plan": plan}


def test_plan_workspace_must_lie_under_the_root(
    client: TestClient, workspace_root: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    inside, outside = workspace_root
    monkeypatch.setattr(_PlanPipeline, "workspaces", [])
    monkeypatch.setattr("delegatron.orchestrator.graph.Pipeline", _PlanPipeline)
    body = {"request": "x", "preset": "mock"}

    rejected = client.post("/api/runs/plan", json={**body, "workspace": str(outside)})
    accepted = client.post("/api/runs/plan", json={**body, "workspace": str(inside)})

    assert rejected.status_code == 422
    assert "Workspace must be under" in rejected.text
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["plan_summary"] == "one task"
    assert _PlanPipeline.workspaces == [inside]
