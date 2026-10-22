"""Tests for the review API endpoint and related functionality."""

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from delegatron.server.app import app, configure


def _init_git_repo(path: Path) -> None:
    """Initialize a bare git repo in path so workspace operations work."""
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path),
        capture_output=True,
        check=True,
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DELEGATRON_AUTH_ENABLED", "false")
    _init_git_repo(tmp_path)
    configure(workspace=tmp_path, artifacts=tmp_path / "runs")
    # Reset prompts dir to the real prompts/ in case test_prompts.py ran first
    from delegatron.prompts import load_prompt, set_prompts_dir

    repo_root = Path(__file__).resolve().parent.parent.parent
    prompts_dir = repo_root / "prompts"
    if prompts_dir.exists():
        set_prompts_dir(prompts_dir)
    else:
        load_prompt.cache_clear()
    return TestClient(app)


# --- Review endpoint ---


def test_review_endpoint_exists(client: TestClient):
    """POST /api/review should be a valid route (not 404 or 405)."""
    resp = client.post("/api/review", json={"preset": "mock"})
    # May fail with 500 (workspace not a git repo), but the route exists
    assert resp.status_code not in (404, 405)


def test_review_invalid_focus(client: TestClient):
    """Invalid focus value should be rejected."""
    resp = client.post(
        "/api/review",
        json={"focus": "not-a-valid-focus"},
    )
    assert resp.status_code == 422


def test_review_invalid_preset(client: TestClient):
    """Invalid preset characters should be rejected."""
    resp = client.post(
        "/api/review",
        json={"preset": "../../evil"},
    )
    assert resp.status_code == 422


def test_review_valid_focus_values(client: TestClient):
    """All valid focus values should pass request validation (not 422)."""
    for focus in ["security", "bugs", "performance", "all"]:
        resp = client.post(
            "/api/review",
            json={"focus": focus, "preset": "mock"},
        )
        # Should not be a validation error (may 500 since tmp_path isn't a git repo)
        assert resp.status_code != 422, f"focus={focus} rejected"


# --- Reviews list endpoint ---


def test_list_reviews_empty(client: TestClient):
    """GET /api/reviews should return empty list when no reviews exist."""
    resp = client.get("/api/reviews")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_reviews_with_data(client: TestClient, tmp_path: Path):
    """GET /api/reviews should list reviews from workspace/reviews/ directory."""
    # Reviews are stored in workspace_root/reviews/review-*/summary.json
    reviews_dir = tmp_path / "reviews"
    reviews_dir.mkdir(parents=True)
    review_dir = reviews_dir / "review-001"
    review_dir.mkdir()
    (review_dir / "summary.json").write_text(
        json.dumps(
            {
                "review_id": "review-001",
                "result": {
                    "summary": "Found 2 issues",
                    "acceptance_met": False,
                    "issues": [
                        {"severity": "major", "title": "SQL injection"},
                        {"severity": "minor", "title": "Missing docstring"},
                    ],
                },
                "project": {
                    "files_reviewed": 10,
                    "path": "/tmp/repo",
                    "languages": ["python"],
                },
            }
        )
    )

    resp = client.get("/api/reviews")
    assert resp.status_code == 200
    reviews = resp.json()
    assert len(reviews) >= 1
    review = next((r for r in reviews if r.get("review_id") == "review-001"), None)
    assert review is not None
    assert review["summary"] == "Found 2 issues"
    assert review["issue_count"] == 2
    assert review["accepted"] is False


# --- Defaults endpoint ---


def test_defaults_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """GET /api/defaults should return server configuration."""
    monkeypatch.delenv("DELEGATRON_MAX_WALL_TIME", raising=False)
    resp = client.get("/api/defaults")
    assert resp.status_code == 200
    data = resp.json()
    assert "sandbox" in data
    assert "max_iterations" in data
    assert "max_cost_usd" in data
    assert "provider_keys" in data
    assert isinstance(data["provider_keys"], dict)
    # The dashboard shows this as "(server default: N)" (#84): the real default.
    assert data["max_wall_time_minutes"] == 60


# --- Run files endpoint ---


def test_run_files_not_found(client: TestClient):
    """GET /api/runs/{id}/files should 404 for nonexistent run."""
    resp = client.get("/api/runs/nonexistent/files")
    assert resp.status_code == 404


def test_run_files_with_patch(client: TestClient, tmp_path: Path):
    """GET /api/runs/{id}/files should return files from latest patch."""
    run_dir = tmp_path / "runs" / "test-files-001"
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": "test-files-001",
                "status": "approved",
            }
        )
    )
    (run_dir / "patch_set_001.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "files": {
                    "src/app.py": "print('hello')",
                    "tests/test_app.py": "def test_it(): pass",
                },
                "diff_unified": "",
                "rationale": "Initial implementation",
                "tests_to_run": ["tests/test_app.py"],
            }
        )
    )

    resp = client.get("/api/runs/test-files-001/files")
    assert resp.status_code == 200
    data = resp.json()
    assert "files" in data
    assert "src/app.py" in data["files"]
    assert data["files"]["src/app.py"] == "print('hello')"


# --- Compliance endpoint ---


def test_compliance_not_found(client: TestClient):
    """GET /api/runs/{id}/compliance should 404 for nonexistent run."""
    resp = client.get("/api/runs/nonexistent/compliance")
    assert resp.status_code == 404


def test_compliance_with_report(client: TestClient, tmp_path: Path):
    """GET /api/runs/{id}/compliance should return zero-cloud report."""
    run_dir = tmp_path / "runs" / "test-compliance-001"
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": "test-compliance-001",
                "status": "approved",
            }
        )
    )
    (run_dir / "zero_cloud_report.json").write_text(
        json.dumps(
            {
                "run_id": "test-compliance-001",
                "cloud_free": True,
                "timestamp": "2026-04-09T12:00:00Z",
                "events": [],
                "violations": [],
                "summary": {"total_events": 0, "local_events": 0, "cloud_events": 0},
                "fingerprint": "sha256:abc123",
            }
        )
    )

    resp = client.get("/api/runs/test-compliance-001/compliance")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cloud_free"] is True
    assert data["fingerprint"] == "sha256:abc123"


# --- Download endpoint ---


def test_download_not_found(client: TestClient):
    """GET /api/runs/{id}/download should 404 for nonexistent run."""
    resp = client.get("/api/runs/nonexistent/download")
    assert resp.status_code == 404


def test_download_with_files(client: TestClient, tmp_path: Path):
    """GET /api/runs/{id}/download should return a zip file."""
    run_dir = tmp_path / "runs" / "test-dl-001"
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": "test-dl-001",
                "status": "approved",
                "request": "Add hello endpoint",
            }
        )
    )
    (run_dir / "patch_set_001.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "files": {"app.py": "print('hello')"},
                "diff_unified": "",
                "rationale": "",
                "tests_to_run": [],
            }
        )
    )

    resp = client.get("/api/runs/test-dl-001/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "delegatron-" in resp.headers.get("content-disposition", "")


# --- Delete run endpoint ---


def test_delete_run_not_found(client: TestClient):
    """DELETE /api/runs/{id} should 404 for nonexistent run."""
    resp = client.delete("/api/runs/nonexistent")
    assert resp.status_code == 404


def test_delete_run(client: TestClient, tmp_path: Path):
    """DELETE /api/runs/{id} should remove run and artifacts."""
    run_dir = tmp_path / "runs" / "test-del-001"
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": "test-del-001",
                "status": "approved",
            }
        )
    )

    resp = client.delete("/api/runs/test-del-001")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert not run_dir.exists()


# --- Review of a directory that is not a git checkout ---


@pytest.mark.parametrize("with_suggestions", [False, True])
def test_review_works_on_a_non_git_workspace(
    client: TestClient, tmp_path_factory: pytest.TempPathFactory, with_suggestions: bool
):
    """#125: /api/review reviews a plain directory (no git repository around it)."""
    project = tmp_path_factory.mktemp("plain")
    (project / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    assert subprocess.run(["git", "-C", str(project), "rev-parse"], capture_output=True).returncode

    resp = client.post(
        "/api/review",
        json={"preset": "mock", "workspace": str(project), "with_suggestions": with_suggestions},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["files_reviewed"] >= 1
