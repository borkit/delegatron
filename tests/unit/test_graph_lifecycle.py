"""Pipeline.execute lifecycle: finalization on every exit path (#105), fail-closed
workspace setup (#87, #88), resume/refine restore, checkpoints and cost baseline (#89).

The three LLM roles are scripted through ``Pipeline._invoke_agent`` and the sandbox
is a stub, so no provider, network, Docker or Ollama is involved.
"""

from __future__ import annotations

import asyncio
import copy
import json
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from delegatron.config import Config, PipelineConfig, RoleConfig
from delegatron.integrations.git_pr import MockPRCreator
from delegatron.orchestrator import graph
from delegatron.orchestrator.graph import (
    RESUME_STATE_KEYS,
    Pipeline,
    _new_state,
    _reset_for_next_task,
    _restore_state,
)
from delegatron.orchestrator.state import AgentState
from delegatron.workspace import Workspace, WorkspaceInitError

APPROVE: dict[str, Any] = {"summary": "ok", "acceptance_met": True, "issues": []}
REJECT: dict[str, Any] = {
    "summary": "no",
    "acceptance_met": False,
    "issues": [
        {
            "severity": "major",
            "title": "Broken",
            "details": "d",
            "recommended_fix": "f",
            "evidence": "",
        }
    ],
}
PASSED = {"passed": True, "total": 1, "failures": 0, "stdout": "", "stderr": ""}
FAILED = {"passed": False, "total": 1, "failures": 1, "stdout": "", "stderr": "boom"}


def _plan(*task_ids: str) -> dict[str, Any]:
    return {
        "summary": "plan",
        "tasks": [
            {
                "task_id": task_id,
                "title": task_id.upper(),
                "description": f"do {task_id}",
                "outputs": [f"{task_id}.py"],
                "risk_level": "low",
                "depends_on": [],
            }
            for task_id in task_ids
        ],
        "acceptance": {"criteria": ["works"]},
    }


def _config(implementer: RoleConfig | None = None, **pipeline: Any) -> Config:
    settings: dict[str, Any] = {
        "max_iterations": 3,
        "max_cost_usd": 10.0,
        "max_wall_time_minutes": 5,
        "sandbox": "local",
    }
    settings.update(pipeline)
    return Config(
        architect=RoleConfig(provider="mock", model="mock-architect"),
        implementer=implementer or RoleConfig(provider="mock", model="mock-implementer"),
        reviewer=RoleConfig(provider="mock", model="mock-reviewer"),
        pipeline=PipelineConfig(**settings),
    )


class _Agents:
    """Scripted outputs per role; records every call's input."""

    def __init__(
        self,
        plan: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]],
        *,
        implement: Callable[[dict[str, Any], int], dict[str, Any]] | None = None,
        review: Callable[[dict[str, Any], int], dict[str, Any]] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.plan = plan
        self.implement = implement or (lambda data, n: {"files": {f"mod_{n}.py": f"x = {n}\n"}})
        self.review = review or (lambda data, n: APPROVE)
        self.meta = meta or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def invoke(self, agent: object, input_data: dict[str, Any], role: str) -> dict[str, Any]:
        self.calls.append((role, dict(input_data)))
        count = sum(1 for called, _ in self.calls if called == role)
        if role == "architect":
            out = self.plan(input_data) if callable(self.plan) else copy.deepcopy(self.plan)
        elif role == "implementer":
            out = {"status": "ok", "rationale": "r", "tests_to_run": [], "questions": []}
            out.update(self.implement(input_data, count))
        else:
            out = copy.deepcopy(self.review(input_data, count))
        out["_meta"] = {"model": "mock-model", **self.meta.get(role, {})}
        return out

    def task_ids(self) -> list[str]:
        return [
            (data.get("task") or {}).get("task_id", "")
            for role, data in self.calls
            if role == "implementer"
        ]

    def count(self, role: str) -> int:
        return sum(1 for called, _ in self.calls if called == role)


class _Sandbox:
    def __init__(self) -> None:
        self.reports: Callable[[int], dict[str, Any]] = lambda n: PASSED
        self.calls = 0
        self.cleanups = 0

    async def apply_and_test(self, patch: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return dict(self.reports(self.calls))

    def cleanup(self) -> None:
        self.cleanups += 1


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    cleanup_calls: list[Config] = []
    sandbox = _Sandbox()
    monkeypatch.setattr(graph, "cleanup_after_run", cleanup_calls.append)
    monkeypatch.setattr(graph, "get_gpu_power_watts", lambda: None)
    monkeypatch.setattr(graph, "create_sandbox", lambda **kwargs: sandbox)
    for name in ("_create_architect", "_create_implementer", "_create_reviewer"):
        monkeypatch.setattr(Pipeline, name, lambda self: object())

    def use(agents: _Agents) -> _Agents:
        monkeypatch.setattr(Pipeline, "_invoke_agent", agents.invoke)
        return agents

    return SimpleNamespace(cleanup_calls=cleanup_calls, sandbox=sandbox, use=use)


def _pipeline(root: Path, config: Config | None = None, **kwargs: Any) -> Pipeline:
    return Pipeline(config=config or _config(), workspace_root=root, run_id="run1", **kwargs)


def _run_dir(root: Path) -> Path:
    return root / "runs" / "run1"


def _events(root: Path) -> list[dict[str, Any]]:
    lines = (_run_dir(root) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _checkpoint(root: Path, iteration: int) -> dict[str, Any]:
    path = _run_dir(root) / f"state_{iteration:03d}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _saved(plan: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """A checkpoint as an older run would have written it (no all_tasks)."""
    state: dict[str, Any] = {
        "request": "build it",
        "plan": plan,
        "current_task": plan["tasks"][0],
        "iteration": 0,
        "messages": [],
        "_completed_tasks": [],
        "_task_iteration": 0,
        "is_approved": False,
        "patch_applied": True,
        "total_cost_usd": 0.0,
        "status": "running",
    }
    state.update(overrides)
    return state


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "app.py").write_bytes(b"x = 1\n")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "init")
    return tmp_path


class TestFinalizationOnEarlyExit:
    async def test_architect_failure_writes_summary_and_cleans_up(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        def fail(_data: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("invalid JSON after retries")

        harness.use(_Agents(fail))
        steps: list[str] = []
        approvals: list[dict[str, Any]] = []
        pipeline = _pipeline(
            tmp_path,
            _config(interactive=True),
            step_callback=lambda step, data: steps.append(step),
            approval_callback=approvals.append,
        )

        state = await pipeline.execute("build it")

        assert state["status"] == "error"
        summary = json.loads((_run_dir(tmp_path) / "summary.json").read_text(encoding="utf-8"))
        assert summary["status"] == "error"
        assert summary["error"].startswith("Architect failed:")
        assert (_run_dir(tmp_path) / "zero_cloud_report.json").exists()
        events = _events(tmp_path)
        assert any(e["event"] == "architect_error" for e in events)
        assert events[-1]["event"] == "pipeline_complete"
        assert len(harness.cleanup_calls) == 1
        assert harness.sandbox.cleanups == 1
        assert "architect_complete" not in steps
        assert approvals == []

    @pytest.mark.parametrize("asynchronous", [False, True])
    async def test_plan_rejection_finalizes_without_implementing(
        self, tmp_path: Path, harness: SimpleNamespace, asynchronous: bool
    ) -> None:
        agents = harness.use(_Agents(_plan("t1")))

        async def reject_async(plan: dict[str, Any]) -> bool:
            return False

        callback = reject_async if asynchronous else (lambda plan: False)
        pipeline = _pipeline(tmp_path, _config(interactive=True), approval_callback=callback)

        state = await pipeline.execute("build it")

        assert state["error"] == "Plan rejected by user"
        assert agents.count("implementer") == 0
        summary = json.loads((_run_dir(tmp_path) / "summary.json").read_text(encoding="utf-8"))
        assert summary["status"] == "error"
        assert (_run_dir(tmp_path) / "zero_cloud_report.json").exists()
        events = _events(tmp_path)
        assert any(e["event"] == "plan_rejected" for e in events)
        assert events[-1]["event"] == "pipeline_complete"
        assert len(harness.cleanup_calls) == 1
        assert harness.sandbox.cleanups == 1

    @pytest.mark.parametrize("interrupt", [asyncio.CancelledError, KeyboardInterrupt])
    async def test_interrupt_still_unloads_models_and_checkpoints(
        self, tmp_path: Path, harness: SimpleNamespace, interrupt: type[BaseException]
    ) -> None:
        def stop(_data: dict[str, Any], _n: int) -> dict[str, Any]:
            raise interrupt()

        harness.use(_Agents(_plan("t1"), implement=stop))

        with pytest.raises(interrupt):
            await _pipeline(tmp_path).execute("build it")

        assert len(harness.cleanup_calls) == 1
        assert harness.sandbox.cleanups == 1
        assert _checkpoint(tmp_path, 1)["iteration"] == 1


class TestFailClosedWorkspace:
    async def test_init_failure_ends_the_run_without_commits(
        self, repo: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commits: list[str] = []

        def fail_init(self: Workspace, *, allow_dirty: bool = False, fresh: bool = False) -> None:
            raise WorkspaceInitError("base branch 'main' does not exist")

        monkeypatch.setattr(Workspace, "initialize", fail_init)
        monkeypatch.setattr(Workspace, "commit", lambda self, message: commits.append(message))
        agents = harness.use(_Agents(_plan("t1")))

        state = await _pipeline(repo).execute("build it")

        assert state["status"] == "error"
        assert "does not exist" in state["error"]
        assert commits == []
        assert agents.calls == []
        assert any(e["event"] == "workspace_init_failed" for e in _events(repo))

    async def test_dirty_repo_stops_before_any_agent_call(
        self, repo: Path, harness: SimpleNamespace
    ) -> None:
        (repo / "app.py").write_bytes(b"x = 2  # WIP\n")
        agents = harness.use(_Agents(_plan("t1")))

        state = await _pipeline(repo).execute("build it")

        assert state["status"] == "error"
        assert "app.py" in state["error"]
        assert agents.calls == []
        assert _git(repo, "branch", "--show-current") == "main"

    async def test_subdirectory_workspace_gets_run_branch_and_scoped_commits(
        self, repo: Path, harness: SimpleNamespace
    ) -> None:
        billing = repo / "services" / "billing"
        other = repo / "services" / "other"
        billing.mkdir(parents=True)
        other.mkdir(parents=True)
        (billing / "core.py").write_bytes(b"c = 1\n")
        (other / "x.py").write_bytes(b"o = 1\n")
        _git(repo, "add", "-A")
        _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "svc")
        (other / "x.py").write_bytes(b"o = 2  # sibling WIP\n")
        harness.use(_Agents(_plan("t1"), implement=lambda d, n: {"files": {"billing.py": "b\n"}}))

        state = await _pipeline(billing).execute("build it")

        assert state["status"] == "approved"
        assert _git(repo, "branch", "--show-current") == "delegatron/run-run1"
        committed = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
        assert committed == ["services/billing/billing.py"]
        assert _git(repo, "diff", "--name-only") == "services/other/x.py"
        assert _git(repo, "diff", "--cached", "--name-only") == ""
        summary = json.loads((_run_dir(billing) / "summary.json").read_text(encoding="utf-8"))
        assert summary["branch"] == "delegatron/run-run1"
        assert summary["files_changed"] == ["billing.py"]


class TestResume:
    async def test_multi_task_resume_past_single_task_cap(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        plan = _plan("t1", "t2", "t3")
        saved = _saved(
            plan,
            iteration=7,
            current_task=plan["tasks"][2],
            _completed_tasks=["t1", "t2"],
        )
        agents = harness.use(_Agents(plan))

        state = await _pipeline(tmp_path, _config(max_iterations=5)).execute(
            "build it", initial_state=saved
        )

        assert agents.task_ids()[0] == "t3"
        assert state["status"] == "approved"
        assert [t["task_id"] for t in state["all_tasks"]] == ["t1", "t2", "t3"]

    async def test_restored_approval_advances_and_opens_no_pr(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        plan = _plan("t1", "t2", "t3")
        saved = _saved(
            plan,
            iteration=2,
            current_task=plan["tasks"][1],
            _completed_tasks=["t1"],
            is_approved=True,
            test_report=PASSED,
        )
        agents = harness.use(_Agents(plan, review=lambda d, n: REJECT))
        harness.sandbox.reports = lambda n: FAILED
        creator = MockPRCreator()
        pipeline = _pipeline(
            tmp_path, _config(max_iterations=1, create_pr=True), pr_creator=creator
        )

        state = await pipeline.execute("build it", initial_state=saved)

        assert agents.task_ids() == ["t3"]
        # t1 and t2 approved, t3 exhausted: a partial run (X10), still no PR
        assert state["status"] == "partial"
        assert creator.calls == []

    async def test_mid_task_resume_approves_only_after_every_task(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        plan = _plan("t1", "t2", "t3")
        saved = _saved(plan, iteration=2, current_task=plan["tasks"][1], _completed_tasks=["t1"])
        agents = harness.use(_Agents(plan))

        state = await _pipeline(tmp_path).execute("build it", initial_state=saved)

        assert agents.task_ids() == ["t2", "t3"]
        assert state["status"] == "approved"
        assert state["_completed_tasks"] == ["t1", "t2", "t3"]

    async def test_resume_keeps_cumulative_cost_toward_the_budget(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        plan = _plan("t1")
        saved = _saved(plan, iteration=1, total_cost_usd=4.80)
        # 37,500 gpt-4.1 output tokens cost $0.30 on a cloud endpoint.
        agents = harness.use(
            _Agents(
                plan,
                review=lambda d, n: REJECT,
                meta={"implementer": {"model": "gpt-4.1", "output_tokens": 37_500}},
            )
        )
        harness.sandbox.reports = lambda n: FAILED
        config = _config(
            implementer=RoleConfig(provider="openai", model="gpt-4.1"), max_cost_usd=5.0
        )

        state = await _pipeline(tmp_path, config).execute("build it", initial_state=saved)

        assert state["total_cost_usd"] == pytest.approx(5.10)
        assert agents.count("implementer") == 1
        assert state["status"] == "exhausted"
        summary = json.loads((_run_dir(tmp_path) / "summary.json").read_text(encoding="utf-8"))
        assert summary["prior_cost_usd"] == pytest.approx(4.80)
        assert summary["session_cost_usd"] == pytest.approx(0.30)

    async def test_prior_cost_prefers_the_larger_call_log(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        _run_dir(tmp_path).mkdir(parents=True)
        (_run_dir(tmp_path) / "provider_calls.jsonl").write_text(
            json.dumps({"cost_usd": 2.5}) + "\n", encoding="utf-8"
        )
        harness.use(_Agents(_plan("t1")))

        state = await _pipeline(tmp_path).execute(
            "build it", initial_state=_saved(_plan("t1"), total_cost_usd=1.0)
        )

        assert state["total_cost_usd"] == pytest.approx(2.5)


class TestCheckpoints:
    async def test_advancing_pass_checkpoints_the_next_task(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(_Agents(_plan("t1", "t2")))

        state = await _pipeline(tmp_path).execute("build it")

        first = _checkpoint(tmp_path, 1)
        assert first["current_task"]["task_id"] == "t2"
        assert first["is_approved"] is False
        assert first["_task_iteration"] == 0
        assert _checkpoint(tmp_path, 2)["status"] == state["status"] == "approved"

    async def test_last_checkpoint_of_a_stalled_run_says_stalled(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(_Agents(_plan("t1"), review=lambda d, n: REJECT))
        harness.sandbox.reports = lambda n: FAILED

        state = await _pipeline(tmp_path, _config(max_iterations=5)).execute("build it")

        assert state["status"] == "stalled"
        assert _checkpoint(tmp_path, state["iteration"])["status"] == "stalled"


class TestRefineWindow:
    async def test_refine_round_runs_past_the_previous_budget(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        saved = _saved(_plan("t1"), iteration=5, _iteration_base=5)
        agents = harness.use(_Agents(_plan("t1")))

        state = await _pipeline(tmp_path, _config(max_iterations=5)).execute(
            "build it\n\nfeedback", initial_state=saved
        )

        assert agents.count("implementer") == 1
        assert state["status"] == "approved"
        assert _checkpoint(tmp_path, 6)["_iteration_base"] == 5

    async def test_without_a_window_the_cap_is_already_reached(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(_plan("t1")))

        state = await _pipeline(tmp_path, _config(max_iterations=5)).execute(
            "build it", initial_state=_saved(_plan("t1"), iteration=5)
        )

        assert agents.calls == []
        assert state["status"] == "exhausted"


class TestStateHelpers:
    def test_resume_keys_are_declared_state_keys(self) -> None:
        declared = set(AgentState.__annotations__)
        assert set(RESUME_STATE_KEYS) <= declared
        for key in (
            "all_tasks",
            "execution_waves",
            "_file_history",
            "_fixed_failures",
            "_baseline_done",
            "_last_changed_files",
            "patch_noop",
            "import_issues",
            "_iteration_base",
            "_arbitrated_signatures",
            "_arbitration_pending",
            "_replan_count",
            "_parallel_patches",
        ):
            assert key in RESUME_STATE_KEYS
        assert "status" not in RESUME_STATE_KEYS
        assert "request" not in RESUME_STATE_KEYS

    def test_new_state_has_bookkeeping_defaults(self, tmp_path: Path) -> None:
        state = _new_state("r", tmp_path)

        assert state["patch_noop"] is False
        assert state["_file_history"] == {}
        assert state["import_issues"] == []
        assert state["_iteration_base"] == 0
        assert set(state) == set(AgentState.__annotations__)

    def test_restore_rebuilds_task_list_and_keeps_current_task(self, tmp_path: Path) -> None:
        plan = _plan("t1", "t2", "t3")
        state = _new_state("r", tmp_path)

        _restore_state(state, _saved(plan, current_task=plan["tasks"][2], status="stalled"))

        assert [t["task_id"] for t in state["all_tasks"]] == ["t1", "t2", "t3"]
        assert state["execution_waves"]
        assert state["current_task"]["task_id"] == "t3"
        assert state["status"] == "running"

    def test_restore_copies_saved_task_list(self, tmp_path: Path) -> None:
        plan = _plan("t1", "t2")
        state = _new_state("r", tmp_path)
        saved = _saved(plan, all_tasks=plan["tasks"][:1], execution_waves=[["w"]], current_wave=1)

        _restore_state(state, saved)

        assert state["all_tasks"] == plan["tasks"][:1]
        assert state["execution_waves"] == [["w"]]
        assert "current_wave" not in state  # written by older runs, never read

    def test_reset_for_next_task_drops_task_specific_state(self, tmp_path: Path) -> None:
        state = _new_state("r", tmp_path)
        state.update(
            {
                "import_issues": [{"title": "stale"}],
                "review_report": REJECT,
                "previous_review": REJECT,
                "test_report": FAILED,
                "_task_iteration": 4,
                "is_approved": True,
                "_rejected_paths": ["../x.py"],
                "_arbitrated_signatures": ["sig"],
                "_arbitration_pending": True,
            }
        )

        _reset_for_next_task(state, {"task_id": "t2"})

        assert state["current_task"] == {"task_id": "t2"}
        assert state["import_issues"] == []
        assert state["review_report"] is None
        assert state["previous_review"] is None
        assert state["test_report"] is None
        assert state["_task_iteration"] == 0
        assert state["is_approved"] is False
        assert state["_rejected_paths"] == []
        assert state["_arbitrated_signatures"] == []
        assert state["_arbitration_pending"] is False


class TestImplementerResponse:
    async def test_zero_file_response_is_not_replaced_by_workspace_files(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        (tmp_path / "t1.py").write_text("existing = 1\n", encoding="utf-8")
        harness.use(_Agents(_plan("t1"), implement=lambda d, n: {"files": {}}))
        pipeline = _pipeline(tmp_path)
        state = _new_state("r", tmp_path)
        state.update(
            {
                "iteration": 2,
                "plan": _plan("t1"),
                "current_task": _plan("t1")["tasks"][0],
                "file_tree": ["t1.py"],
                "review_report": REJECT,
            }
        )

        state = await pipeline._run_implementer(state, object(), graph.CostTracker())  # type: ignore[arg-type]

        assert state["current_patch"] is not None
        assert state["current_patch"]["files"] == {}

    async def test_rejected_paths_reach_the_next_implementer_as_blockers(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(_plan("t1")))
        state = _new_state("r", tmp_path)
        state.update(
            {
                "iteration": 2,
                "plan": _plan("t1"),
                "current_task": _plan("t1")["tasks"][0],
                "_rejected_paths": ["../evil.py"],
            }
        )

        await _pipeline(tmp_path)._run_implementer(state, object(), graph.CostTracker())  # type: ignore[arg-type]

        [(_, data)] = agents.calls
        [issue] = data["review_feedback"]
        assert issue["severity"] == "blocker"
        assert issue["file"] == "../evil.py"
        assert "../evil.py" in issue["title"]


# --- Fix round: finalization and restart --------------------------------------------


class TestPullRequestNeverBreaksFinalization:
    @pytest.mark.parametrize("failure", ["gh_timeout", "pr_body"])
    async def test_an_approved_run_keeps_its_summary_when_the_pr_step_raises(
        self, repo: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: str
    ) -> None:
        if failure == "gh_timeout":

            def gh_status() -> tuple[bool, str]:
                raise subprocess.TimeoutExpired(["gh", "auth", "status"], 10)

            monkeypatch.setattr(graph, "check_gh_available", gh_status)
        else:

            def broken_body(state: dict[str, Any], summary: dict[str, Any]) -> str:
                raise ValueError("Invalid IPv6 URL")

            monkeypatch.setattr(graph, "check_gh_available", lambda: (True, ""))
            monkeypatch.setattr(graph, "check_git_remote", lambda path: (True, ""))
            monkeypatch.setattr(graph, "build_pr_body", broken_body)
        harness.use(_Agents(_plan("t1")))
        pipeline = _pipeline(repo, _config(create_pr=True), pr_creator=MockPRCreator())

        state = await pipeline.execute("build it")

        assert state["status"] == "approved"
        summary = json.loads((_run_dir(repo) / "summary.json").read_text(encoding="utf-8"))
        assert summary["status"] == "approved"
        assert summary["pull_request"]["created"] is False
        expected = "TimeoutExpired" if failure == "gh_timeout" else "ValueError"
        assert summary["pull_request"]["error"].startswith(expected)
        assert _events(repo)[-1]["event"] == "pipeline_complete"


class TestPlanApprovalSurvivesACancel:
    async def test_a_plan_cancelled_while_awaiting_approval_is_asked_again_on_resume(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        """A cancel during plan approval must not let a resume implement the plan unasked."""
        agents = harness.use(_Agents(_plan("t1")))
        asked: list[str] = []

        async def cancelled(plan: dict[str, Any]) -> bool:
            asked.append("first")
            raise asyncio.CancelledError

        first = _pipeline(tmp_path, _config(interactive=True), approval_callback=cancelled)
        with pytest.raises(asyncio.CancelledError):
            await first.execute("build it")
        checkpoint = _checkpoint(tmp_path, 0)
        assert checkpoint["_plan_pending_approval"] is True

        def reject(plan: dict[str, Any]) -> bool:
            asked.append("resume")
            return False

        resumed = _pipeline(tmp_path, _config(interactive=True), approval_callback=reject)
        state = await resumed.execute("build it", initial_state=checkpoint)

        assert asked == ["first", "resume"]
        assert agents.count("implementer") == 0
        assert state["error"] == "Plan rejected by user"

    async def test_an_approved_plan_is_not_asked_again(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(_plan("t1")))
        asked: list[dict[str, Any]] = []

        def approve(plan: dict[str, Any]) -> bool:
            asked.append(plan)
            return True

        pipeline = _pipeline(tmp_path, _config(interactive=True), approval_callback=approve)

        state = await pipeline.execute("build it")

        assert len(asked) == 1
        assert state["_plan_pending_approval"] is False
        assert _checkpoint(tmp_path, 0)["_plan_pending_approval"] is False
        assert agents.count("implementer") == 1


class TestRestartOfAGivenUpTask:
    async def test_resuming_a_stalled_task_restarts_it_cleanly(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        plan = _plan("t1")
        saved = _saved(
            plan,
            iteration=2,
            status="stalled",
            _task_iteration=2,
            _skipped_tasks=["t1"],
            _task_outcomes={"t1": {"status": "stalled", "iterations": 2, "reason": "no_progress"}},
            _noop_streak=2,
            _agent_failures={"implementer": 1},
            _failure_signatures=["sig", "sig"],
            _known_failures={"tests/test_t1.py::test_a": "t1"},
        )
        agents = harness.use(_Agents(plan))

        state = await _pipeline(tmp_path, _config(max_iterations=5)).execute(
            "build it", initial_state=saved
        )

        assert agents.count("implementer") == 1
        assert state["status"] == "approved"
        assert state["_skipped_tasks"] == []
        assert state["_known_failures"] == {}
        summary = json.loads((_run_dir(tmp_path) / "summary.json").read_text(encoding="utf-8"))
        assert summary["skipped_tasks"] == []
        assert summary["task_outcomes"][0]["status"] == "approved"

    def test_restart_resets_the_attempt_history_and_keeps_the_feedback(
        self, tmp_path: Path
    ) -> None:
        """What ``delegatron refine`` needs before a new round on a finished task."""
        plan = _plan("t1", "t2")
        feedback = {"summary": "User feedback", "acceptance_met": False, "issues": [{"t": 1}]}
        state = _new_state("r", tmp_path)
        state.update(
            all_tasks=plan["tasks"],
            current_task=plan["tasks"][1],
            _completed_tasks=["t1", "t2"],
            _task_outcomes={
                "t1": {"status": "approved", "iterations": 1},
                "t2": {"status": "approved", "iterations": 3},
            },
            _noop_streak=2,
            _agent_failures={"reviewer": 1},
            _failure_signatures=["a", "a"],
            _arbitrated_signatures=["a"],
            _director_guidance=[{"title": "x"}],
            _known_failures={"tests/test_t2.py::test_b": "t2", "tests/test_x.py::test_x": "t0"},
            review_report=feedback,
        )

        graph.restart_current_task(state)

        assert state["_completed_tasks"] == ["t1"]
        assert set(state["_task_outcomes"]) == {"t1"}
        assert state["_known_failures"] == {"tests/test_x.py::test_x": "t0"}
        assert state["_noop_streak"] == 0
        assert state["_agent_failures"] == {}
        assert state["_failure_signatures"] == []
        assert state["_arbitrated_signatures"] == []
        assert state["_director_guidance"] == []
        assert state["_task_iteration"] == 0
        assert state["review_report"] == feedback
        assert state["current_task"]["task_id"] == "t2"


class TestStopReasons:
    async def test_a_wall_time_stop_counts_no_iteration_that_did_not_run(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"now": 0.0}
        monkeypatch.setattr(graph, "time", SimpleNamespace(monotonic=lambda: clock["now"]))

        def review(data: dict[str, Any], n: int) -> dict[str, Any]:
            clock["now"] = 10 * 60.0  # the 5-minute limit passes during the first review
            return REJECT

        agents = harness.use(_Agents(_plan("t1"), review=review))

        state = await _pipeline(tmp_path).execute("build it")

        assert agents.count("implementer") == 1
        assert state["iteration"] == 1
        assert state["status"] == "exhausted"
        [terminated] = [e for e in _events(tmp_path) if e["event"] == "terminated"]
        assert terminated["reason"] == "Wall time limit (5min) reached"
        assert terminated["iteration"] == 1
        assert (terminated["limit"], terminated["phase"]) == ("wall_time", "iteration_start")
        assert terminated["elapsed_seconds"] == 600.0
        summary = json.loads((_run_dir(tmp_path) / "summary.json").read_text(encoding="utf-8"))
        assert summary["iterations"] == 1
        assert summary["termination_reason"] == "Wall time limit (5min) reached"

    async def test_budget_and_per_task_stops_are_on_record(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(_Agents(_plan("t1"), review=lambda d, n: REJECT))
        harness.sandbox.reports = lambda n: {**FAILED, "stderr": f"boom {n}"}

        state = await _pipeline(tmp_path, _config(max_iterations=2)).execute("build it")

        assert state["status"] == "exhausted"
        [terminated] = [e for e in _events(tmp_path) if e["event"] == "terminated"]
        assert terminated["reason"] == "Iteration limit (2) reached"
        assert (terminated["limit"], terminated["phase"]) == ("iterations", "iteration_start")
        summary = json.loads((_run_dir(tmp_path) / "summary.json").read_text(encoding="utf-8"))
        assert summary["termination_reason"] == terminated["reason"]


# --- Fix round (GRAPH-B): final checkpoint, commits before a PR, resume ------------


class TestFinalCheckpoint:
    """#89: the last checkpoint says how the run ended, whatever stopped it."""

    async def test_the_last_task_exhausting_its_attempts(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(_Agents(_plan("t1", "t2"), review=lambda d, n: REJECT))
        harness.sandbox.reports = lambda n: {**FAILED, "stderr": f"boom {n}"}

        state = await _pipeline(tmp_path, _config(max_iterations=2)).execute("build it")

        assert state["status"] == "exhausted"
        assert _checkpoint(tmp_path, state["iteration"])["status"] == "exhausted"

    async def test_a_budget_stop(self, tmp_path: Path, harness: SimpleNamespace) -> None:
        harness.use(_Agents(_plan("t1"), review=lambda d, n: REJECT))
        harness.sandbox.reports = lambda n: {**FAILED, "stderr": f"boom {n}"}

        state = await _pipeline(tmp_path, _config(max_iterations=2)).execute("build it")

        checkpoint = _checkpoint(tmp_path, state["iteration"])
        assert (checkpoint["status"], state["status"]) == ("exhausted", "exhausted")
        assert checkpoint["_termination_reason"] == "Iteration limit (2) reached"

    async def test_a_wall_time_stop(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"now": 0.0}
        monkeypatch.setattr(graph, "time", SimpleNamespace(monotonic=lambda: clock["now"]))

        def review(data: dict[str, Any], n: int) -> dict[str, Any]:
            clock["now"] = 10 * 60.0
            return REJECT

        harness.use(_Agents(_plan("t1"), review=review))

        await _pipeline(tmp_path).execute("build it")

        checkpoint = _checkpoint(tmp_path, 1)
        assert checkpoint["status"] == "exhausted"
        assert checkpoint["_task_outcomes"]["t1"]["status"] == "exhausted"

    async def test_a_checkpoint_below_the_cap_resumes_to_approval(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        saved = _saved(_plan("t1"), iteration=2, _task_iteration=2, test_report=FAILED)
        agents = harness.use(_Agents(_plan("t1")))

        state = await _pipeline(tmp_path, _config(max_iterations=5)).execute(
            "build it", initial_state=saved
        )

        assert agents.count("implementer") == 1
        assert state["status"] == "approved"
        assert _checkpoint(tmp_path, 3)["status"] == "approved"

    def test_a_resume_forgets_outcomes_of_tasks_still_to_do(self, tmp_path: Path) -> None:
        plan = _plan("t1", "t2", "t3")
        state = _new_state("r", tmp_path)
        state.update(
            plan=plan,
            all_tasks=plan["tasks"],
            current_task=plan["tasks"][1],
            _completed_tasks=["t1"],
            _task_outcomes={
                "t1": {"status": "approved", "iterations": 1},
                "t2": {"status": "exhausted", "iterations": 2},  # a limit stopped it
                "t3": {"status": "skipped", "iterations": 0},  # never reached
            },
        )
        store = graph.ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")

        _pipeline(tmp_path)._resume_after_restore(state, store)

        assert state["_task_outcomes"] == {"t1": {"status": "approved", "iterations": 1}}


class TestCommitsBeforeAPullRequest:
    """#88: the pushed branch holds everything written, or no PR is opened."""

    @pytest.fixture
    def gh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(graph, "check_gh_available", lambda: (True, ""))
        monkeypatch.setattr(graph, "check_git_remote", lambda path: (True, ""))
        monkeypatch.setattr(graph, "git_remote_url", lambda path: "https://github.com/o/r.git")

    async def test_a_failed_commit_opens_no_pr(
        self,
        repo: Path,
        harness: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        gh: None,
    ) -> None:
        def refuse(self: Workspace, message: str) -> str | None:
            raise RuntimeError("git commit failed (rc=1): pre-commit hook says no")

        monkeypatch.setattr(Workspace, "commit", refuse)
        harness.use(_Agents(_plan("t1")))
        creator = MockPRCreator()
        pipeline = _pipeline(repo, _config(create_pr=True), pr_creator=creator)

        state = await pipeline.execute("build it")

        assert state["status"] == "approved"
        assert creator.calls == []
        summary = json.loads((_run_dir(repo) / "summary.json").read_text(encoding="utf-8"))
        assert "commit failed" in summary["pull_request"]["skipped_reason"]
        assert "hook says no" in summary["pull_request"]["skipped_reason"]

    async def test_files_written_before_a_resume_are_committed_before_the_pr(
        self, repo: Path, harness: SimpleNamespace, gh: None
    ) -> None:
        (repo / "t1.py").write_bytes(b"x = 1\n")  # written, never committed
        _git(repo, "checkout", "-q", "-b", "delegatron/run-run1")
        saved = _saved(
            _plan("t1"),
            iteration=1,
            is_approved=True,
            test_report=PASSED,
            _file_history={"t1.py": {"task_id": "t1", "iteration": 1}},
        )
        agents = harness.use(_Agents(_plan("t1")))
        creator = MockPRCreator()
        pipeline = _pipeline(repo, _config(create_pr=True), pr_creator=creator, allow_dirty=True)

        state = await pipeline.execute("build it", initial_state=saved)

        assert agents.calls == []
        assert state["status"] == "approved"
        assert "t1.py" in _git(repo, "ls-files").split()
        assert _git(repo, "status", "--porcelain", "--", "t1.py") == ""
        assert len(creator.calls) == 1

    async def test_configured_git_hooks_run_on_run_commits(
        self, repo: Path, harness: SimpleNamespace
    ) -> None:
        @dataclass(frozen=True)
        class _HookedPipeline(PipelineConfig):
            run_git_hooks: bool = True

        hook = repo / ".git" / "hooks" / "commit-msg"
        hook.write_text("#!/bin/sh\necho commitlint says no >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        config = _config()
        config = Config(
            architect=config.architect,
            implementer=config.implementer,
            reviewer=config.reviewer,
            pipeline=_HookedPipeline(max_iterations=3, max_wall_time_minutes=5, sandbox="local"),
        )
        harness.use(_Agents(_plan("t1")))

        state = await _pipeline(repo, config).execute("build it")

        assert "commitlint says no" in state["workspace_commit_error"]
        assert any(e["event"] == "commit_failed" for e in _events(repo))


class TestCommitErrorText:
    """#88: git's line-ending notices never hide why a commit failed."""

    def test_line_ending_notices_are_dropped(self) -> None:
        notices = "".join(
            f"warning: in the working copy of 'f{i}.py', LF will be replaced by CRLF the next"
            " time Git touches it\n"
            for i in range(8)
        )
        error = RuntimeError(f"git commit failed (rc=1): {notices}commitlint: subject empty")

        text = graph._commit_error_text(error)

        assert "commitlint: subject empty" in text
        assert "LF will be replaced" not in text

    def test_a_long_error_keeps_its_first_line_and_its_tail(self) -> None:
        body = "\n".join(f"hook output line {i}" for i in range(100))
        error = RuntimeError(f"git commit failed (rc=1):\n{body}\nfinal reason")

        text = graph._commit_error_text(error)

        assert len(text) <= graph.MAX_COMMIT_ERROR
        assert text.startswith("git commit failed (rc=1):")
        assert text.endswith("final reason")


class TestFreshRunBranch:
    async def test_a_fresh_run_never_continues_an_old_run_branch(
        self, repo: Path, harness: SimpleNamespace
    ) -> None:
        """D8: only a resume or refine reuses ``delegatron/run-<id>``."""
        _git(repo, "checkout", "-q", "-b", "delegatron/run-run1")
        (repo / "stale.py").write_bytes(b"left by an earlier run\n")
        _git(repo, "add", "stale.py")
        _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "old")
        _git(repo, "checkout", "-q", "main")
        harness.use(_Agents(_plan("t1")))

        state = await _pipeline(repo).execute("build it")

        branch = _git(repo, "branch", "--show-current")
        assert branch.startswith("delegatron/run-run1-")
        assert not (repo / "stale.py").exists()
        assert state["status"] == "approved"


# --- Fix round (GRAPH-C): wall-time deadline, rejected paths, nested repos ----------


class _HookedAgents(_Agents):
    """``_Agents`` that await *hook(role)* before each scripted answer."""

    def __init__(
        self,
        plan: dict[str, Any],
        hook: Callable[[str], Awaitable[None]],
        **kwargs: Any,
    ) -> None:
        super().__init__(plan, **kwargs)
        self.hook = hook

    async def invoke(self, agent: object, input_data: dict[str, Any], role: str) -> dict[str, Any]:
        await self.hook(role)
        return await super().invoke(agent, input_data, role)


def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """The graph's clock, set by the test (the event loop keeps its own)."""
    clock = {"now": 0.0}
    monkeypatch.setattr(graph, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    return clock


def _summary(root: Path) -> dict[str, Any]:
    return json.loads((_run_dir(root) / "summary.json").read_text(encoding="utf-8"))


def _terminated(root: Path) -> dict[str, Any]:
    [event] = [e for e in _events(root) if e["event"] == "terminated"]
    return event


BUDGET = 5 * 60.0  # _config's max_wall_time_minutes


class TestWallTimeDeadline:
    """#106 / D13: one deadline for the run, on every agent call and sandbox run."""

    async def test_a_call_running_past_the_deadline_is_cancelled(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _fake_clock(monkeypatch)
        cancelled: list[str] = []

        async def hook(role: str) -> None:
            if role == "architect":
                clock["now"] = BUDGET - 0.05  # 50 ms of the budget left
            elif role == "implementer":
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    cancelled.append(role)
                    raise

        harness.use(_HookedAgents(_plan("t1"), hook))
        loop = asyncio.get_running_loop()
        started = loop.time()

        state = await _pipeline(tmp_path).execute("build it")

        assert cancelled == ["implementer"]
        assert loop.time() - started < 10
        assert (state["status"], state["error"]) == ("exhausted", "")
        summary = _summary(tmp_path)
        assert summary["status"] == "exhausted"
        assert summary["iterations"] == state["iteration"] == 1
        assert summary["termination_reason"] == "Wall time limit (5min) reached"
        terminated = _terminated(tmp_path)
        assert (terminated["limit"], terminated["phase"], terminated["iteration"]) == (
            "wall_time",
            "implementer",
            1,
        )
        assert terminated["elapsed_seconds"] == pytest.approx(BUDGET - 0.05, abs=0.1)
        assert not [e for e in _events(tmp_path) if e["event"] == "implementer_error"]
        assert _checkpoint(tmp_path, 1)["status"] == "exhausted"
        assert harness.sandbox.cleanups == 1

    async def test_a_sandbox_run_past_the_deadline_ends_the_run(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _fake_clock(monkeypatch)

        async def hook(role: str) -> None:
            if role == "implementer":
                clock["now"] = BUDGET - 0.05

        async def slow_tests(patch: dict[str, Any]) -> dict[str, Any]:
            await asyncio.sleep(30)
            return PASSED

        harness.sandbox.apply_and_test = slow_tests
        agents = harness.use(_HookedAgents(_plan("t1"), hook))

        state = await _pipeline(tmp_path).execute("build it")

        assert state["status"] == "exhausted"
        assert agents.count("reviewer") == 0
        assert (_terminated(tmp_path)["phase"], _summary(tmp_path)["iterations"]) == ("sandbox", 1)

    async def test_a_baseline_cut_short_is_taken_again_on_resume(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _fake_clock(monkeypatch)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    pass\n", "utf-8")

        async def hook(role: str) -> None:
            if role == "architect":
                clock["now"] = BUDGET - 0.05

        async def slow_tests(patch: dict[str, Any]) -> dict[str, Any]:
            await asyncio.sleep(30)
            return PASSED

        harness.sandbox.apply_and_test = slow_tests
        harness.use(_HookedAgents(_plan("t1"), hook))

        state = await _pipeline(tmp_path).execute("build it")

        assert state["status"] == "exhausted"
        assert (_terminated(tmp_path)["phase"], state["iteration"]) == ("baseline", 0)
        assert _checkpoint(tmp_path, 0)["_baseline_done"] is False

    async def test_a_call_starting_after_the_deadline_never_runs(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _fake_clock(monkeypatch)

        def review(data: dict[str, Any], n: int) -> dict[str, Any]:
            return REJECT

        async def hook(role: str) -> None:
            if role == "implementer":
                clock["now"] = BUDGET + 1  # the deadline passes during the implementer

        agents = harness.use(_HookedAgents(_plan("t1"), hook, review=review))

        state = await _pipeline(tmp_path).execute("build it")

        assert state["status"] == "exhausted"
        assert agents.count("reviewer") == 0
        assert harness.sandbox.calls == 0
        assert _terminated(tmp_path)["phase"] == "sandbox"

    async def test_a_pending_replan_is_not_started_past_the_deadline(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _fake_clock(monkeypatch)
        architecture = {**REJECT["issues"][0], "fix_target": "architecture"}

        def review(data: dict[str, Any], n: int) -> dict[str, Any]:
            clock["now"] = BUDGET * 2  # the limit passes during the review that asks
            return {"summary": "re-plan", "acceptance_met": False, "issues": [architecture]}

        agents = harness.use(_Agents(_plan("t1"), review=review))

        state = await _pipeline(tmp_path).execute("build it")

        assert agents.count("architect") == 1
        assert state["status"] == "exhausted"
        assert state["_replan"] is True  # still pending: a resume re-plans first
        terminated = _terminated(tmp_path)
        assert (terminated["phase"], terminated["iteration"]) == ("replan", 1)

    async def test_elapsed_time_is_checkpointed_and_a_resume_continues_the_budget(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _fake_clock(monkeypatch)
        budgets: list[float] = []
        pipeline = _pipeline(tmp_path, _config(max_iterations=1))

        async def hook(role: str) -> None:
            if role == "implementer":
                budgets.append(pipeline._clock.remaining())
                clock["now"] += 40.0

        harness.use(_HookedAgents(_plan("t1"), hook, review=lambda d, n: REJECT))
        await pipeline.execute("build it")
        saved = _checkpoint(tmp_path, 1)
        assert saved["_wall_clock"] == {
            "elapsed_seconds": 40.0,
            "round_base": 0,
            "round_start_seconds": 0.0,
        }

        # The resume: 40 s of the round's budget are gone (the cap is raised to go on)
        resumed = _pipeline(tmp_path, _config(max_iterations=2))
        pipeline = resumed
        await resumed.execute("build it", initial_state=saved)

        assert budgets == [BUDGET, BUDGET - 40.0]
        summary = _summary(tmp_path)
        assert (summary["elapsed_seconds"], summary["prior_elapsed_seconds"]) == (80.0, 40.0)
        assert summary["session_elapsed_seconds"] == 40.0
        assert _checkpoint(tmp_path, 2)["_wall_clock"]["elapsed_seconds"] == 80.0

    async def test_a_refine_round_gets_a_fresh_budget(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_clock(monkeypatch)
        budgets: list[float] = []
        pipeline = _pipeline(tmp_path)

        async def hook(role: str) -> None:
            if role == "implementer":
                budgets.append(pipeline._clock.remaining())

        harness.use(_HookedAgents(_plan("t1"), hook))
        spent = {"elapsed_seconds": BUDGET + 5, "round_base": 0, "round_start_seconds": 0.0}
        refined = _saved(_plan("t1"), iteration=3, _iteration_base=3, _wall_clock=spent)

        state = await pipeline.execute("build it", initial_state=refined)

        assert state["status"] == "approved"
        assert budgets == [BUDGET]
        assert _summary(tmp_path)["elapsed_seconds"] == BUDGET + 5

    async def test_a_resume_with_no_time_left_stops_before_any_call(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_clock(monkeypatch)
        agents = harness.use(_Agents(_plan("t1")))
        spent = {"elapsed_seconds": BUDGET, "round_base": 0, "round_start_seconds": 0.0}

        state = await _pipeline(tmp_path).execute(
            "build it", initial_state=_saved(_plan("t1"), _wall_clock=spent)
        )

        assert state["status"] == "exhausted"
        assert agents.calls == []
        assert (_terminated(tmp_path)["phase"], state["iteration"]) == ("iteration_start", 0)

    async def test_a_timeout_of_the_call_itself_is_not_the_deadline(self, tmp_path: Path) -> None:
        pipeline = _pipeline(tmp_path)
        started: list[str] = []

        async def provider_timeout() -> None:
            started.append("call")
            raise TimeoutError("no response from the provider within 900s")

        pipeline._clock = graph._WallClock(budget_seconds=BUDGET)
        with pytest.raises(TimeoutError, match="provider"):
            await pipeline._within_deadline("reviewer", provider_timeout)

        pipeline._clock = graph._WallClock(budget_seconds=0.0)
        with pytest.raises(graph.WallTimeExceeded) as raised:
            await pipeline._within_deadline("reviewer", provider_timeout)
        assert raised.value.phase == "reviewer"
        assert started == ["call"]

    def test_a_clock_resumes_its_round_and_a_refine_starts_one(self) -> None:
        saved = {
            "_iteration_base": 2,
            "_wall_clock": {"elapsed_seconds": 90.0, "round_base": 2, "round_start_seconds": 30.0},
        }

        same_round = graph._WallClock.start(saved, 100.0)
        new_round = graph._WallClock.start({**saved, "_iteration_base": 5}, 100.0)
        fresh = graph._WallClock.start({}, 100.0)

        assert (same_round.prior_seconds, same_round.round_start) == (90.0, 30.0)
        assert (new_round.round_start, new_round.round_base) == (90.0, 5)
        assert (fresh.prior_seconds, fresh.round_start) == (0.0, 0.0)


class TestStopReasonNamesTheLimit:
    """#112: the termination reason says which limit fired."""

    def test_each_limit(self, tmp_path: Path) -> None:
        pipeline = _pipeline(tmp_path, _config(max_cost_usd=1.0))
        state = _new_state("r", tmp_path)
        state["all_tasks"] = _plan("t1", "t2")["tasks"]

        assert pipeline._stop_reason(state) is None
        state["total_cost_usd"] = 1.25
        assert pipeline._stop_reason(state) == ("cost", "Cost limit ($1.00) reached: $1.25 spent")
        state["iteration"] = 6
        assert pipeline._stop_reason(state) == ("iterations", "Iteration limit (6) reached")
        state.update({"iteration": 0, "total_cost_usd": 0.0})
        pipeline._clock = graph._WallClock(budget_seconds=0.0)
        assert pipeline._stop_reason(state) == ("wall_time", "Wall time limit (5min) reached")

    def test_no_cost_limit_when_it_is_zero(self, tmp_path: Path) -> None:
        pipeline = _pipeline(tmp_path, _config(max_cost_usd=0.0))
        state = _new_state("r", tmp_path)
        state["total_cost_usd"] = 99.0

        assert pipeline._stop_reason(state) is None


class TestRejectedPaths:
    """#110: a patch none of whose paths is safe is turned back untested; one with a
    rejected path among valid ones cannot be approved."""

    TEST = "def test_x():\n    pass\n"

    async def test_a_patch_of_only_rejected_paths_is_neither_tested_nor_reviewed(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        patches = {
            1: {"files": {"/tests/test_t1.py": self.TEST}},
            2: {"files": {"t1.py": "x = 1\n", "tests/test_t1.py": self.TEST}},
        }
        agents = harness.use(_Agents(_plan("t1"), implement=lambda d, n: patches[n]))

        state = await _pipeline(tmp_path).execute("build it")

        assert state["status"] == "approved"
        assert harness.sandbox.calls == 1  # iteration 2 only
        assert agents.count("reviewer") == 1
        skipped = json.loads((_run_dir(tmp_path) / "test_report_001.json").read_text("utf-8"))
        assert skipped["skipped_reason"] == "paths_rejected"
        assert "/tests/test_t1.py" in skipped["stderr"]
        assert _checkpoint(tmp_path, 1)["patch_applied"] is False
        retry = [data for role, data in agents.calls if role == "implementer"][1]
        [blocker] = [i for i in retry["review_feedback"] if i["severity"] == "blocker"]
        assert blocker["file"] == "/tests/test_t1.py"

    async def test_a_partly_rejected_patch_is_not_approved(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        """The verifier's case: a valid file plus ``/tests/...``, tests pass, reviewer approves."""
        patches = {
            1: {"files": {"t1.py": "x = 1\n", "/tests/test_t1.py": self.TEST}},
            2: {"files": {"tests/test_t1.py": self.TEST}},
        }
        harness.use(_Agents(_plan("t1"), implement=lambda d, n: patches[n]))

        state = await _pipeline(tmp_path).execute("build it")

        first = json.loads((_run_dir(tmp_path) / "review_report_001.json").read_text("utf-8"))
        assert "Rejected file path: /tests/test_t1.py" in [i["title"] for i in first["issues"]]
        assert _checkpoint(tmp_path, 1)["is_approved"] is False
        assert state["iteration"] == 2
        assert state["status"] == "approved"
        assert (tmp_path / "tests" / "test_t1.py").exists()


class TestNestedWorkspace:
    """A workspace inside a larger repository (cross review: paths regression)."""

    async def test_a_folder_the_repo_ignores_runs_without_git(
        self, repo: Path, harness: SimpleNamespace
    ) -> None:
        (repo / ".gitignore").write_text("newapp/\n", encoding="utf-8")
        _git(repo, "add", ".gitignore")
        _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "ignore")
        _git(repo, "checkout", "-q", "-b", "feature")
        (repo / "newapp").mkdir()
        harness.use(_Agents(_plan("t1")))

        state = await _pipeline(repo / "newapp").execute("build it")

        assert state["status"] == "approved"
        assert _git(repo, "branch", "--show-current") == "feature"
        assert "delegatron/" not in _git(repo, "branch", "--list")
        assert (repo / "newapp" / "mod_1.py").exists()
        events = [e["event"] for e in _events(repo / "newapp")]
        assert "workspace_not_git" in events
        assert "workspace_initialized" not in events
        assert _summary(repo / "newapp")["branch"] == ""

    async def test_a_dirty_subfolder_names_the_enclosing_repository(
        self, repo: Path, harness: SimpleNamespace
    ) -> None:
        (repo / "sub").mkdir()
        (repo / "sub" / "draft.py").write_text("x = 1\n", encoding="utf-8")
        harness.use(_Agents(_plan("t1")))

        state = await _pipeline(repo / "sub").execute("build it")

        assert state["status"] == "error"
        assert "uncommitted changes" in state["error"]
        assert "inside the git repository at" in state["error"]
        assert _git(repo, "branch", "--show-current") == "main"


class TestRunSummaryFiles:
    """#120/#109: the summary lists the run's files, deletions apart, and its root."""

    async def test_deleted_files_are_not_reported_as_changed(
        self, repo: Path, harness: SimpleNamespace
    ) -> None:
        patches = {
            1: {"files": {"t1.py": "x = 1\n", "old.py": "y = 1\n"}},
            2: {"files": {"t1.py": "x = 2\n"}, "delete_files": ["old.py"]},
        }
        reviews = {1: REJECT, 2: APPROVE}
        harness.use(
            _Agents(
                _plan("t1"),
                implement=lambda d, n: patches[n],
                review=lambda d, n: reviews[n],
            )
        )

        await _pipeline(repo).execute("build it")

        summary = _summary(repo)
        assert summary["files_changed"] == ["t1.py"]
        assert summary["files_deleted"] == ["old.py"]
        assert summary["workspace_root"] == str(repo)
        commits = [e for e in _events(repo) if e["event"] == "workspace_commit"]
        assert commits[-1]["files_deleted"] == ["old.py"]
