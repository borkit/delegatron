"""Observer metrics and provider cost rows of the pipeline's agent steps (#82).

Two-pass implementer calls book one row each, with their own request id; a
failing pass 2 keeps pass 1's row and fails only the attempt; every failed agent
call, including one generated in parallel, counts as an observer error.  No
provider, network, Docker or Ollama is involved.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from delegatron.config import Config, PipelineConfig, RoleConfig
from delegatron.orchestrator import graph
from delegatron.orchestrator.graph import Pipeline, _new_state, _StepMark
from delegatron.utils.cost import CostTracker

LOCAL = RoleConfig(provider="mock", model="m", base_url="http://localhost:11434/v1")
PLAN = {
    "summary": "plan",
    "tasks": [
        {
            "task_id": task_id,
            "title": task_id,
            "description": f"do {task_id}",
            "outputs": [f"{task_id}.py"],
            "depends_on": [],
        }
        for task_id in ("t1", "t2")
    ],
}


def _config(implementer: RoleConfig = LOCAL, **pipeline: Any) -> Config:
    settings: dict[str, Any] = {
        "max_iterations": 3,
        "max_wall_time_minutes": 5,
        "sandbox": "local",
        **pipeline,
    }
    return Config(
        architect=RoleConfig(provider="mock", model="mock-architect"),
        implementer=implementer,
        reviewer=RoleConfig(provider="mock", model="mock-reviewer"),
        pipeline=PipelineConfig(**settings),
    )


class _Implementer:
    """An implementer agent that answers each call from a script (a reply may be an error)."""

    def __init__(self, *replies: dict[str, Any] | Exception) -> None:
        self.replies = list(replies)
        self.inputs: list[dict[str, Any]] = []

    async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
        self.inputs.append(input_data)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"status": "ok", "rationale": "r", **reply}


def _reply(files: dict[str, str], request_id: str, tokens: int) -> dict[str, Any]:
    meta = {"model": "m", "input_tokens": tokens, "output_tokens": 5, "request_id": request_id}
    return {"files": files, "_meta": meta}


@pytest.fixture(autouse=True)
def _no_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graph, "unload_except", lambda config, model: None)
    monkeypatch.setattr(graph, "get_gpu_power_watts", lambda: 50.0)


def _first_attempt(root: Path) -> Any:
    state = _new_state("build it", root)
    state.update(
        iteration=1,
        plan=PLAN,
        all_tasks=PLAN["tasks"],
        current_task=PLAN["tasks"][0],
        file_tree=[],
    )
    return state


class TestTwoPassRows:
    async def test_each_pass_books_its_own_row_and_request_id(self, tmp_path: Path) -> None:
        agent = _Implementer(
            _reply({"t1.py": "x = 1\n"}, "r1", 100),
            _reply({"tests/test_t1.py": "def test_x():\n    pass\n"}, "r2", 40),
        )
        tracker = CostTracker()
        pipeline = Pipeline(config=_config(), workspace_root=tmp_path, run_id="run1")

        state = await pipeline._run_implementer(_first_attempt(tmp_path), agent, tracker)  # type: ignore[arg-type]

        rows = [call for call in tracker.calls if call.role == "implementer"]
        assert [row.request_id for row in rows] == ["r1", "r2"]
        assert {row.iteration for row in rows} == {1}
        assert tracker.total_input_tokens == 140
        assert tracker.total_output_tokens == 10
        assert set(state["current_patch"]["files"]) == {"t1.py", "tests/test_t1.py"}

    async def test_a_failing_second_pass_keeps_the_first_row_and_fails_the_attempt(
        self, tmp_path: Path
    ) -> None:
        agent = _Implementer(
            _reply({"t1.py": "x = 1\n"}, "r1", 100),
            TimeoutError("pass 2 timed out"),
        )
        tracker = CostTracker()
        pipeline = Pipeline(config=_config(), workspace_root=tmp_path, run_id="run1")

        state = await pipeline._run_implementer(_first_attempt(tmp_path), agent, tracker)  # type: ignore[arg-type]

        assert [call.request_id for call in tracker.calls] == ["r1"]
        assert state["current_patch"] is None
        assert state["status"] == "running"
        assert state["_agent_failures"] == {"implementer": 1}
        review = state["review_report"]
        assert review["synthesized"] is True
        assert "pass 2 timed out" in review["summary"]


class TestStepMark:
    def test_usage_is_what_was_booked_since_the_mark(self, tmp_path: Path) -> None:
        tracker = CostTracker()
        tracker.record("m", 7, 3, role="architect")
        state = _new_state("r", tmp_path)

        mark = _StepMark.take(tracker, state)
        tracker.record("m", 10, 5, role="implementer")
        tracker.record("m", 20, 1, role="implementer")

        assert mark.usage(tracker) == {"calls": 2, "input_tokens": 30, "output_tokens": 6}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        check=True,
    )


class TestParallelSiblingFailure:
    async def test_a_failed_parallel_call_counts_as_an_observer_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _git(tmp_path, "init", "-q", "-b", "main")
        (tmp_path / "README.md").write_bytes(b"demo\n")
        _git(tmp_path, "add", "README.md")
        _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "init")
        calls: dict[str, int] = {}

        async def invoke(agent: object, data: dict[str, Any], role: str) -> dict[str, Any]:
            meta = {"model": "m", "input_tokens": 10, "output_tokens": 5}
            if role == "architect":
                return {**PLAN, "_meta": meta}
            if role == "reviewer":
                return {"summary": "ok", "acceptance_met": True, "issues": [], "_meta": meta}
            task_id = data["task"]["task_id"]
            calls[task_id] = calls.get(task_id, 0) + 1
            if task_id == "t2" and calls[task_id] == 1:
                raise ValueError("bad JSON")
            return {"status": "ok", "files": {f"{task_id}.py": "x = 1\n"}, "_meta": meta}

        class _Sandbox:
            async def apply_and_test(self, patch: dict[str, Any]) -> dict[str, Any]:
                return {"passed": True, "total": 1, "failures": 0, "stdout": "1 passed"}

            def cleanup(self) -> None:
                pass

        monkeypatch.setattr(graph, "cleanup_after_run", lambda config: None)
        monkeypatch.setattr(graph, "create_sandbox", lambda **kwargs: _Sandbox())
        monkeypatch.setattr(Pipeline, "_invoke_agent", staticmethod(invoke))
        for name in ("_create_architect", "_create_implementer", "_create_reviewer"):
            monkeypatch.setattr(Pipeline, name, lambda self: object())
        config = _config(
            implementer=RoleConfig(provider="openai", model="gpt-4.1"), parallel_tasks=True
        )

        state = await Pipeline(config=config, workspace_root=tmp_path, run_id="run1").execute(
            "build it"
        )

        assert state["status"] == "approved"
        assert calls == {"t1": 1, "t2": 2}
        summary = json.loads((tmp_path / "runs" / "run1" / "summary.json").read_text("utf-8"))
        assert summary["observer_metrics"]["errors"] == 1
        assert (
            summary["observer_metrics"]["agent_calls"] == summary["cost_breakdown"]["total_calls"]
        )
