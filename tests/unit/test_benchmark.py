"""Tests for benchmark module."""

import subprocess
from pathlib import Path

import pytest

from delegatron.benchmark import BenchmarkReport, BenchmarkResult, run_benchmark


def test_benchmark_result_to_dict():
    r = BenchmarkResult(
        preset="mock",
        status="approved",
        iterations=2,
        cost_usd=0.035,
        elapsed_seconds=6.8,
        emcs_score=0.95,
        approved=True,
    )
    d = r.to_dict()
    assert d["preset"] == "mock"
    assert d["emcs_score"] == 0.95
    assert d["approved"] is True


def test_benchmark_report_best():
    report = BenchmarkReport(
        request="test",
        results=[
            BenchmarkResult("a", "approved", 3, 0.1, 10.0, 0.8, True),
            BenchmarkResult("b", "approved", 2, 0.05, 5.0, 0.9, True),
            BenchmarkResult("c", "exhausted", 5, 0.2, 20.0, 0.5, False),
        ],
    )
    d = report.to_dict()
    assert d["best_quality"] == "b"  # highest emcs_score among approved
    assert d["cheapest"] == "b"  # lowest cost among approved
    assert d["fastest"] == "b"  # lowest time among approved


def test_benchmark_report_no_approved():
    report = BenchmarkReport(
        request="test",
        results=[
            BenchmarkResult("a", "exhausted", 5, 0.5, 30.0, 0.3, False),
        ],
    )
    d = report.to_dict()
    assert d["best_quality"] is None
    assert d["cheapest"] is None


@pytest.fixture
def bench_workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace for benchmarking."""
    env = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True, env=env)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        env=env,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        env=env,
    )
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\nversion = "0.1.0"\n')
    subprocess.run(
        ["git", "add", "."],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        env=env,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        env=env,
    )
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.slow
async def test_run_benchmark_with_mock(bench_workspace: Path, monkeypatch: pytest.MonkeyPatch):
    """Run benchmark with mock preset using local sandbox (no Docker needed).

    Marked slow: runs a full mock pipeline (~60s). Use ``pytest -m slow`` to include.
    """
    monkeypatch.setenv("DELEGATRON_SANDBOX", "local")
    monkeypatch.setenv("DELEGATRON_MAX_ITERATIONS", "2")
    report = await run_benchmark(
        request="Add health endpoint",
        workspace_root=bench_workspace,
        presets=["mock"],
    )
    assert len(report.results) == 1
    result = report.results[0]
    assert result.preset == "mock"
    assert result.status in ("approved", "exhausted")
    assert result.cost_usd >= 0  # Mock provider is free ($0)
    assert result.emcs_score >= 0  # May be 0 if mock patches don't produce passing tests


@pytest.mark.asyncio
async def test_each_preset_gets_a_fresh_run_from_the_same_base(
    bench_workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    """Re-running a benchmark never resumes an earlier run's branch (#88 regression)."""
    from delegatron import benchmark

    created: list[dict] = []

    class _FakePipeline:
        def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            created.append(kwargs)

        async def execute(self, request: str) -> dict:
            # a run leaves HEAD on its own branch
            subprocess.run(
                ["git", "checkout", "-q", "-b", f"delegatron/run-{created[-1]['run_id']}"],
                cwd=bench_workspace,
                check=True,
            )
            return {"status": "approved", "iteration": 1, "is_approved": True}

    monkeypatch.setattr(benchmark, "Pipeline", _FakePipeline)
    monkeypatch.setattr(benchmark, "load_config", lambda preset: object())
    start_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=bench_workspace,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    await run_benchmark("r", bench_workspace, ["default", "budget"], allow_dirty=True)
    await run_benchmark("r", bench_workspace, ["default"])

    run_ids = [kwargs["run_id"] for kwargs in created]
    assert len(set(run_ids)) == 3
    assert run_ids[0].startswith("bench-default-") and run_ids[2].startswith("bench-default-")
    assert [kwargs["base_branch"] for kwargs in created[:2]] == [start_branch, start_branch]
    assert [kwargs["allow_dirty"] for kwargs in created] == [True, True, False]
