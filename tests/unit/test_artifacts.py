"""Tests for the ArtifactStore."""

import json
from pathlib import Path

import pytest

from delegatron.artifacts import ArtifactStore


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(base_dir=tmp_path, run_id="test-run-001")


def test_run_dir_created(store: ArtifactStore):
    assert store.run_dir.exists()
    assert store.run_dir.name == "test-run-001"


def test_save_plan(store: ArtifactStore):
    plan = {"summary": "Add health endpoint", "tasks": [{"task_id": "T1"}]}
    path = store.save_plan(plan)
    assert path.exists()
    assert path.name == "work_plan.json"
    loaded = json.loads(path.read_text())
    assert loaded["summary"] == "Add health endpoint"


def test_save_patch(store: ArtifactStore):
    patch = {"status": "ok", "diff_unified": "--- a/foo\n+++ b/foo"}
    path = store.save_patch(1, patch)
    assert path.name == "patch_set_001.json"
    loaded = json.loads(path.read_text())
    assert loaded["status"] == "ok"


def test_save_test_report(store: ArtifactStore):
    report = {"passed": True, "total": 5, "failures": 0}
    path = store.save_test_report(2, report)
    assert path.name == "test_report_002.json"


def test_save_review(store: ArtifactStore):
    review = {"acceptance_met": False, "issues": [{"severity": "major"}]}
    path = store.save_review(1, review)
    assert path.name == "review_report_001.json"


def test_save_and_load_state(store: ArtifactStore):
    state = {"iteration": 3, "status": "running", "total_cost_usd": 0.05}
    store.save_state(state)
    loaded = store.load_state(3)
    assert loaded is not None
    assert loaded["iteration"] == 3
    assert loaded["status"] == "running"


def test_load_latest_state(store: ArtifactStore):
    store.save_state({"iteration": 1, "status": "running"})
    store.save_state({"iteration": 2, "status": "running"})
    store.save_state({"iteration": 3, "status": "approved"})
    latest = store.load_state()
    assert latest is not None
    assert latest["iteration"] == 3


def test_load_state_missing(store: ArtifactStore):
    assert store.load_state(99) is None
    assert store.load_state() is None


def test_save_summary(store: ArtifactStore):
    summary = {"run_id": "test-run-001", "status": "approved", "iterations": 2}
    path = store.save_summary(summary)
    assert path.name == "summary.json"
    loaded = json.loads(path.read_text())
    assert loaded["status"] == "approved"


def test_append_event(store: ArtifactStore):
    store.append_event({"event": "pipeline_start", "request": "test"})
    store.append_event({"event": "architect_complete"})
    events_file = store.run_dir / "events.jsonl"
    assert events_file.exists()
    lines = events_file.read_text().strip().split("\n")
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event"] == "pipeline_start"
    assert "timestamp" in first


def test_append_event_preserves_timestamp(store: ArtifactStore):
    store.append_event({"event": "test", "timestamp": "2025-01-01T00:00:00Z"})
    events_file = store.run_dir / "events.jsonl"
    event = json.loads(events_file.read_text().strip())
    assert event["timestamp"] == "2025-01-01T00:00:00Z"


def test_load_plan(store: ArtifactStore):
    assert store.load_plan() is None
    store.save_plan({"summary": "test plan"})
    loaded = store.load_plan()
    assert loaded is not None
    assert loaded["summary"] == "test plan"


def test_list_runs(tmp_path: Path):
    # Create multiple run directories
    (tmp_path / "run-001").mkdir()
    (tmp_path / "run-002").mkdir()
    (tmp_path / "run-003").mkdir()
    store = ArtifactStore(base_dir=tmp_path, run_id="run-002")
    runs = store.list_runs()
    assert runs == ["run-001", "run-002", "run-003"]
