"""Dashboard smoke tests — serve + create run flow.

Validates the full server lifecycle without a running Ollama instance, Docker
or network: runs use the mock preset and a sandbox whose tests always pass.
- Health check and provider key status
- Create a run (mock preset) and poll until it finishes with its known status
- SSE event streaming
- Plan-only and cost estimation endpoints
- Run lifecycle (create → list → get → delete)
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from delegatron.orchestrator import graph
from delegatron.server.app import app, configure

TERMINAL_STATUSES = ("approved", "partial", "exhausted", "stalled", "error", "cancelled")

# The mock plan has two tasks and the mock implementer always creates
# src/app/health.py: task t1 is approved, t2's identical patch cannot apply
# over the file t1 created, so every mock run ends "partial".
MOCK_RUN_STATUS = "partial"


class _PassingSandbox:
    """Every test run passes; no venv, no installs, no network, no Docker."""

    async def apply_and_test(self, patch: dict[str, Any]) -> dict[str, Any]:
        return {
            "passed": True,
            "total": 1,
            "failures": 0,
            "stdout": "tests/test_health.py::test_ok PASSED\n1 passed in 0.01s",
            "stderr": "",
            "failed_tests": [],
            "results": [{"name": "tests/test_health.py::test_ok", "passed": True}],
        }

    def cleanup(self) -> None:
        pass


def _wait_for_run(client: TestClient, run_id: str, timeout: float = 60.0) -> dict[str, Any]:
    """Poll a run until it reaches a terminal status; fail the test on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/api/runs/{run_id}")
        if resp.status_code == 200 and resp.json().get("status") in TERMINAL_STATUSES:
            return dict(resp.json())
        time.sleep(0.2)
    pytest.fail(f"Run {run_id} did not finish within {timeout:.0f}s")


@pytest.fixture
def dashboard_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Configure and yield a test client for dashboard smoke tests.

    Uses a context manager so that background tasks (e.g. pipeline runs)
    are properly awaited/cancelled when the test finishes, preventing
    "cancelled run" artifacts that cause spurious failures.
    """
    monkeypatch.setenv("DELEGATRON_AUTH_ENABLED", "false")
    monkeypatch.setenv("DELEGATRON_SANDBOX", "local")
    monkeypatch.setattr(graph, "create_sandbox", lambda **_kwargs: _PassingSandbox())
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Create a minimal project in the workspace
    (workspace / "pyproject.toml").write_text('[project]\nname = "test-app"\nversion = "0.1.0"\n')
    (workspace / "requirements.txt").write_text("fastapi\npytest\n")
    src_dir = workspace / "src"
    src_dir.mkdir()
    (src_dir / "main.py").write_text("print('hello')\n")

    configure(workspace=workspace, artifacts=tmp_path / "runs")
    with TestClient(app) as client:
        yield client


# ---------------------------------------------------------------------------
# Health & metadata
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_dashboard_health_and_provider_keys(dashboard_client: TestClient) -> None:
    """Health endpoint returns ok; provider-keys reports configured status."""
    resp = dashboard_client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "version" in data

    resp = dashboard_client.get("/api/provider-keys")
    assert resp.status_code == 200
    keys = resp.json()
    # Should return a dict of provider -> bool
    assert isinstance(keys, dict)


# ---------------------------------------------------------------------------
# Create run and poll for summary
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_create_run_and_poll_summary(dashboard_client: TestClient) -> None:
    """Create a run with mock preset and poll until summary is available."""
    resp = dashboard_client.post(
        "/api/runs",
        json={
            "request": "Add a GET /health endpoint",
            "preset": "mock",
            "max_iterations": 2,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "started"
    run_id = data["run_id"]

    summary = _wait_for_run(dashboard_client, run_id)
    assert summary["status"] == MOCK_RUN_STATUS, summary


# ---------------------------------------------------------------------------
# SSE event streaming
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_create_run_events_saved_to_disk(dashboard_client: TestClient, tmp_path: Path) -> None:
    """Create a run, wait for completion, verify events.jsonl is written."""
    resp = dashboard_client.post(
        "/api/runs",
        json={"request": "Add a health endpoint", "preset": "mock", "max_iterations": 1},
    )
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    assert _wait_for_run(dashboard_client, run_id)["status"] == MOCK_RUN_STATUS

    # Verify events.jsonl was written with pipeline events
    events_file = tmp_path / "runs" / run_id / "events.jsonl"
    assert events_file.exists(), f"events.jsonl not found for run {run_id}"

    event_types: set[str] = set()
    with open(events_file) as f:
        for line in f:
            line = line.strip()
            if line:
                event = json.loads(line)
                if "event" in event:
                    event_types.add(event["event"])

    assert "pipeline_start" in event_types, f"Missing pipeline_start in: {event_types}"
    assert "pipeline_complete" in event_types, f"Missing pipeline_complete in: {event_types}"
    assert "pipeline_error" not in event_types


@pytest.mark.integration
def test_stream_events_from_saved_file(dashboard_client: TestClient, tmp_path: Path) -> None:
    """Verify SSE replay from a pre-existing events.jsonl file."""
    # Write a fake events file
    run_dir = tmp_path / "runs" / "smoke-events-001"
    run_dir.mkdir(parents=True)
    events = [
        {"event": "pipeline_start", "run_id": "smoke-events-001"},
        {"event": "architect_complete", "summary": "test plan"},
        {"event": "pipeline_complete", "status": "approved"},
    ]
    with open(run_dir / "events.jsonl", "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    resp = dashboard_client.get("/api/runs/smoke-events-001/events")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")

    text = resp.text
    assert "pipeline_start" in text
    assert "architect_complete" in text
    assert "pipeline_complete" in text


# ---------------------------------------------------------------------------
# Plan-only endpoint
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_plan_only_endpoint(dashboard_client: TestClient) -> None:
    """POST /api/runs/plan returns a plan without executing the full pipeline."""
    resp = dashboard_client.post(
        "/api/runs/plan",
        json={"request": "Add user CRUD endpoints", "preset": "mock"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "planned"
    assert "plan_summary" in data or "plan" in data
    assert "tasks" in data
    assert len(data["tasks"]) > 0


# ---------------------------------------------------------------------------
# Estimate endpoint
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_estimate_endpoint(dashboard_client: TestClient) -> None:
    """POST /api/estimate returns cost breakdown."""
    resp = dashboard_client.post(
        "/api/estimate",
        json={"request": "Add a health endpoint"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "min_cost_usd" in data
    assert "max_cost_usd" in data
    assert "breakdown" in data
    assert data["min_cost_usd"] >= 0
    assert data["max_cost_usd"] >= data["min_cost_usd"]


# ---------------------------------------------------------------------------
# Run lifecycle: create → list → get → delete
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_create_and_delete_run(dashboard_client: TestClient, tmp_path: Path) -> None:
    """Full lifecycle: create a run, verify it exists, delete, verify gone."""
    # Create
    resp = dashboard_client.post(
        "/api/runs",
        json={"request": "Add tests", "preset": "mock", "max_iterations": 1},
    )
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    # Delete only a finished run: its summary is on disk and nothing writes to it any more
    assert _wait_for_run(dashboard_client, run_id)["status"] == MOCK_RUN_STATUS

    # Delete
    resp = dashboard_client.delete(f"/api/runs/{run_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"

    # Verify the run directory is actually removed from disk
    run_dir = tmp_path / "runs" / run_id
    assert not run_dir.exists(), f"Run directory {run_dir} should be deleted"


@pytest.mark.integration
def test_list_runs_after_create(dashboard_client: TestClient) -> None:
    """Create a run and verify it appears in the runs list."""
    resp = dashboard_client.post(
        "/api/runs",
        json={"request": "Add logging", "preset": "mock", "max_iterations": 1},
    )
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    # Wait for completion so summary.json is written
    assert _wait_for_run(dashboard_client, run_id)["status"] == MOCK_RUN_STATUS

    resp = dashboard_client.get("/api/runs")
    assert resp.status_code == 200
    runs = {r["run_id"]: r for r in resp.json()}
    assert runs[run_id]["status"] == MOCK_RUN_STATUS
