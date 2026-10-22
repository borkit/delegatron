"""Pipeline agent I/O: what the implementer and reviewer are shown, and how a failed or
repeated attempt is handled.

Covers implementer input (earlier tasks' files, repo map, failing tests as text, import
warnings, previous no-op attempt, retry temperature, two-pass gate), reviewer input
(synthesized diff, task scope, related files), no-op attempts, flaky tests from history,
sandbox run options and toolchain changes, diff-only patches, agent failures, re-plan
failures, the architect's repo map and the recorded workspace HEAD.

LLM roles are scripted through ``Pipeline._invoke_agent`` and the sandbox is a stub,
so no provider, network, Docker or Ollama is involved.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from delegatron.agents.base import TruncatedOutputError
from delegatron.agents.reviewer import ReviewerAgent
from delegatron.artifacts import ArtifactStore
from delegatron.config import Config, PipelineConfig, RoleConfig
from delegatron.context import ContextSelection
from delegatron.orchestrator import graph
from delegatron.orchestrator.graph import (
    Pipeline,
    _new_state,
    _prior_files,
    _reset_for_next_task,
)
from delegatron.workspace import Workspace

APPROVE: dict[str, Any] = {"summary": "ok", "acceptance_met": True, "issues": []}
PASSED: dict[str, Any] = {"passed": True, "total": 1, "failures": 0, "stdout": "", "stderr": ""}
LOCAL = RoleConfig(provider="openai-compatible", model="qwen", base_url="http://localhost:11434/v1")

CARD_DECK = (
    "import random\n\n\nclass Card:\n    pass\n\n\nclass Deck:\n"
    "    def shuffle(self):\n        random.shuffle(self.cards)\n"
)
CARD_TEST = (
    "from card_deck import Deck, Card\n\n\n"
    "class TestDeck:\n    def test_shuffle(self):\n        pass\n"
)
SIMULATE_DEAL = "def simulate_deal():\n    print('Player 1')\n"
SHUFFLE = "tests/test_card_deck.py::TestDeck::test_shuffle"
FAILING_OUTPUT = (
    "=================================== FAILURES ===================================\n"
    "____________________ TestSimulateDeal.test_simulate_deal ____________________\n"
    "E       AssertionError: 12 != 10\n"
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal"
    " - AssertionError: 12 != 10\n"
)


def _failed(*ids: str, **extra: Any) -> dict[str, Any]:
    return {
        "passed": False,
        "total": len(ids) or 1,
        "failures": len(ids) or 1,
        "stdout": FAILING_OUTPUT,
        "stderr": "",
        "failed_tests": list(ids),
        "results": [{"name": i, "passed": False} for i in ids],
        **extra,
    }


def _issue(title: str = "Broken", **extra: Any) -> dict[str, Any]:
    return {"severity": "major", "title": title, "details": "d", "recommended_fix": "f", **extra}


def _reject(*issues: dict[str, Any]) -> dict[str, Any]:
    return {"summary": "no", "acceptance_met": False, "issues": list(issues)}


def _task(task_id: str, outputs: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "title": f"Title {task_id}",
        "description": f"do {task_id}",
        "outputs": outputs if outputs is not None else [f"{task_id}.py"],
        "risk_level": "low",
        "depends_on": [],
        **extra,
    }


def _plan(*tasks: dict[str, Any] | str) -> dict[str, Any]:
    return {
        "summary": "plan",
        "tasks": [_task(t) if isinstance(t, str) else t for t in tasks],
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


def _task_id(data: dict[str, Any]) -> str:
    return str((data.get("task") or {}).get("task_id", ""))


Script = Callable[[dict[str, Any], int], dict[str, Any]]


class _Agents:
    """Scripted outputs per role (a script may raise); records every call's input."""

    def __init__(
        self,
        plan: dict[str, Any],
        *,
        implement: Script | None = None,
        review: Script | None = None,
        architect: Script | None = None,
    ) -> None:
        self.plan = plan
        self.implement = implement or (
            lambda data, n: {"files": {f"{_task_id(data)}_{n}.py": f"x = {n}\n"}}
        )
        self.review = review or (lambda data, n: APPROVE)
        self.architect = architect or (lambda data, n: self.plan)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def invoke(self, agent: object, input_data: dict[str, Any], role: str) -> dict[str, Any]:
        self.calls.append((role, copy.deepcopy(input_data)))
        count = len(self.inputs(role))
        out: dict[str, Any]
        if role == "architect":
            out = copy.deepcopy(self.architect(input_data, count))
        elif role == "implementer":
            out = {"status": "ok", "rationale": "r", "tests_to_run": [], "questions": []}
            out.update(self.implement(input_data, count))
        else:
            out = copy.deepcopy(self.review(input_data, count))
        out["_meta"] = {"model": "mock-model", "input_tokens": 10, "output_tokens": 5}
        return out

    def inputs(self, role: str) -> list[dict[str, Any]]:
        return [data for called, data in self.calls if called == role]


class _Sandbox:
    def __init__(self) -> None:
        self.reports: Callable[[int, dict[str, Any]], dict[str, Any]] = lambda n, p: PASSED
        self.patches: list[dict[str, Any]] = []

    async def apply_and_test(self, patch: dict[str, Any]) -> dict[str, Any]:
        self.patches.append(copy.deepcopy(patch))
        return copy.deepcopy(self.reports(len(self.patches), patch))

    def cleanup(self) -> None:
        pass


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    sandbox = _Sandbox()
    created: list[dict[str, Any]] = []

    def create_sandbox(**kwargs: Any) -> _Sandbox:
        created.append(kwargs)
        return sandbox

    monkeypatch.setattr(graph, "cleanup_after_run", lambda config: None)
    monkeypatch.setattr(graph, "get_gpu_power_watts", lambda: 0.0)
    monkeypatch.setattr(graph, "create_sandbox", create_sandbox)
    for name in ("_create_architect", "_create_implementer", "_create_reviewer"):
        monkeypatch.setattr(Pipeline, name, lambda self: object())

    def use(agents: _Agents) -> _Agents:
        monkeypatch.setattr(Pipeline, "_invoke_agent", staticmethod(agents.invoke))
        return agents

    return SimpleNamespace(sandbox=sandbox, use=use, created=created)


def _pipeline(root: Path, config: Config | None = None) -> Pipeline:
    return Pipeline(config=config or _config(), workspace_root=root, run_id="run1")


def _events(root: Path, name: str) -> list[dict[str, Any]]:
    lines = (root / "runs" / "run1" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["event"] == name]


def _artifact(root: Path, name: str) -> dict[str, Any]:
    return json.loads((root / "runs" / "run1" / name).read_text(encoding="utf-8"))


def _write(root: Path, files: dict[str, str]) -> None:
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t", *args],
        cwd=cwd,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull},  # no global hooks/signing
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _task_state(root: Path, plan: dict[str, Any], current: int, **overrides: Any) -> Any:
    """A state on the plan's task at index *current*, as the loop has it before the implementer."""
    state = _new_state("deal cards", root)
    state.update(
        {
            "iteration": 2,
            "plan": plan,
            "all_tasks": plan["tasks"],
            "current_task": plan["tasks"][current],
            "file_tree": [".gitignore"],
            **overrides,
        }
    )
    return state


async def _implement(pipeline: Pipeline, state: Any) -> Any:
    return await pipeline._run_implementer(state, object(), graph.CostTracker())  # type: ignore[arg-type]


CARD_PLAN = _plan(
    _task("t1", ["card_deck.py"]),
    _task("t2", ["card_deck.py"], depends_on=["t1"]),
    _task(
        "t3",
        ["simulate_deal.py", "test_simulate_deal.py"],
        depends_on=["t2"],
        description="Write a script to deal cards to two players. Print each player's hand.",
    ),
    _task("t4", ["report.py"], depends_on=["t3"]),
)


class TestImplementerInput:
    async def test_later_task_sees_earlier_tasks_files_and_repo_map(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        # Run 6f29441bbe8c it4: t3 started with no context and redefined Card/Deck.
        _write(tmp_path, {"card_deck.py": CARD_DECK})
        agents = harness.use(_Agents(CARD_PLAN))
        state = _task_state(
            tmp_path,
            CARD_PLAN,
            2,
            _completed_tasks=["t1", "t2"],
            _file_history={"card_deck.py": {"task_id": "t1", "iteration": 1}},
        )

        await _implement(_pipeline(tmp_path), state)

        [data] = agents.inputs("implementer")
        assert "card_deck.py" in data["existing_files"]
        assert "class Deck" in data["repo_map"]
        assert "context_files" not in data

    def test_prior_files_order_own_then_dependencies_then_completed(self, tmp_path: Path) -> None:
        plan = _plan(
            _task("t1"), _task("t2", depends_on=["t1"]), _task("t3", depends_on=["t2"]), _task("t4")
        )
        history = {
            "a_other.py": {"task_id": "t4", "iteration": 1},
            "b_base.py": {"task_id": "t1", "iteration": 2},
            "c_mid.py": {"task_id": "t2", "iteration": 3},
            "d_own.py": {"task_id": "t3", "iteration": 4},
            "e_gone.py": {"task_id": "t2", "iteration": 3, "deleted": True},
        }
        state = _task_state(
            tmp_path, plan, 2, _completed_tasks=["t4", "t1", "t2"], _file_history=history
        )

        assert _prior_files(state) == ["d_own.py", "c_mid.py", "b_base.py", "a_other.py"]

    def test_prior_files_fall_back_to_plan_outputs_without_history(self, tmp_path: Path) -> None:
        state = _task_state(tmp_path, CARD_PLAN, 2, _completed_tasks=["t1", "t2"])

        assert _prior_files(state) == ["card_deck.py"]

    async def test_failing_tests_are_text_with_import_warnings_beside_them(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        _write(tmp_path, {"card_deck.py": CARD_DECK, "tests/test_card_deck.py": CARD_TEST})
        agents = harness.use(_Agents(CARD_PLAN))
        warning = {
            "severity": "warning",
            "title": "Unresolvable import: numpy",
            "file": "tests/test_card_deck.py",
            "evidence": "tests/test_card_deck.py",
            "source": "import_validator",
        }
        state = _task_state(
            tmp_path,
            CARD_PLAN,
            1,
            test_report=_failed(SHUFFLE),
            review_report=_reject(_issue("Shuffle broken")),
            import_issues=[warning],
            _task_iteration=1,
        )

        await _implement(_pipeline(tmp_path, _config(LOCAL)), state)

        [data] = agents.inputs("implementer")
        assert "AssertionError: 12 != 10" in data["failing_tests"]
        assert "\\n" not in data["failing_tests"]
        assert "test_report" not in data
        assert data["import_warnings"] == [warning]
        assert [i["title"] for i in data["review_feedback"]] == ["Shuffle broken"]
        assert "tests/test_card_deck.py" in data["existing_files"]
        assert "card_deck.py" in data["existing_files"]

    async def test_passing_run_hides_failures_and_import_warnings(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(CARD_PLAN))
        state = _task_state(
            tmp_path,
            CARD_PLAN,
            1,
            test_report=PASSED,
            review_report=_reject(_issue()),
            import_issues=[{"title": "Unresolvable import: card_deck", "file": "x.py"}],
            _task_iteration=1,
        )

        await _implement(_pipeline(tmp_path), state)

        [data] = agents.inputs("implementer")
        assert "failing_tests" not in data
        assert "import_warnings" not in data

    async def test_new_task_runs_two_passes_without_any_feedback(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(CARD_PLAN))
        state = _task_state(tmp_path, CARD_PLAN, 1, import_issues=[{"title": "stale"}])
        _reset_for_next_task(state, CARD_PLAN["tasks"][1])

        await _implement(_pipeline(tmp_path, _config(LOCAL)), state)

        calls = agents.inputs("implementer")
        assert [c["task"]["mode"] for c in calls] == ["code_only", "tests_only"]
        assert not any("review_feedback" in c or "import_warnings" in c for c in calls)

    async def test_any_feedback_rules_out_two_passes(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(CARD_PLAN))
        state = _task_state(
            tmp_path,
            CARD_PLAN,
            1,
            test_report=_failed(SHUFFLE),
            import_issues=[{"title": "Unresolvable import: x", "file": "x.py"}],
        )

        await _implement(_pipeline(tmp_path, _config(LOCAL)), state)

        [data] = agents.inputs("implementer")
        assert "mode" not in data["task"]

    async def test_pass_two_sees_existing_tests_of_the_modules_pass_one_wrote(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        _write(tmp_path, {"tests/test_helpers.py": "def test_existing():\n    pass\n"})

        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            return {"files": {"helpers.py": "def h():\n    pass\n"}} if n == 1 else {"files": {}}

        agents = harness.use(_Agents(CARD_PLAN, implement=implement))
        state = _task_state(tmp_path, CARD_PLAN, 0)

        await _implement(_pipeline(tmp_path, _config(LOCAL)), state)

        pass2 = agents.inputs("implementer")[1]["existing_files"]
        assert pass2["helpers.py"] == "def h():\n    pass\n"
        assert "tests/test_helpers.py" in pass2

    async def test_noop_attempt_is_reported_and_sampled_warmer(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(CARD_PLAN))
        state = _task_state(
            tmp_path,
            CARD_PLAN,
            2,
            test_report=_failed(
                "tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal"
            ),
            current_patch={"files": {"simulate_deal.py": SIMULATE_DEAL}, "delete_files": []},
            _noop_streak=1,
            _task_iteration=1,
        )

        await _implement(_pipeline(tmp_path), state)

        [data] = agents.inputs("implementer")
        assert data["previous_attempt"] == {
            "no_changes": True,
            "files": ["simulate_deal.py"],
            "still_failing": ["tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal"],
        }
        assert data["_temperature"] == pytest.approx(0.4)

    async def test_repeated_failure_raises_the_temperature_but_never_lowers_it(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(CARD_PLAN))
        warm = RoleConfig(provider="mock", model="mock-implementer", temperature=0.7)
        state = _task_state(tmp_path, CARD_PLAN, 2, _failure_signatures=["", "sig", "sig"])

        await _implement(_pipeline(tmp_path, _config(warm)), state)
        state["_failure_signatures"] = ["sig", "other"]
        await _implement(_pipeline(tmp_path), state)

        repeated, varied = agents.inputs("implementer")
        assert repeated["_temperature"] == pytest.approx(0.7)
        assert "_temperature" not in varied
        assert "previous_attempt" not in varied

    async def test_director_guidance_reaches_one_attempt(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(CARD_PLAN))
        ruling = {"title": "print count", "decision": "REVISE", "fix_target": "test"}
        state = _task_state(tmp_path, CARD_PLAN, 2, _director_guidance=[ruling])
        pipeline = _pipeline(tmp_path)

        await _implement(pipeline, state)
        await _implement(pipeline, state)

        first, second = agents.inputs("implementer")
        assert first["director_guidance"] == [ruling]
        assert "director_guidance" not in second
        assert state["_director_guidance"] == []

    async def test_known_failures_of_skipped_tasks_are_passed_as_advisory(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(CARD_PLAN))
        report = _failed(SHUFFLE, "tests/test_x.py::test_x")
        report.update(baseline_failures=[SHUFFLE], new_failures=["tests/test_x.py::test_x"])
        state = _task_state(tmp_path, CARD_PLAN, 2, test_report=report)

        await _implement(_pipeline(tmp_path), state)

        [data] = agents.inputs("implementer")
        assert data["baseline_failures"] == {SHUFFLE: graph.PRE_EXISTING}
        assert "failing_tests" in data


class TestUnseenOverwrites:
    async def test_excerpt_copied_back_is_not_written(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        big = "".join(f"def f{i}():\n    return {i}\n\n\n" for i in range(3000))
        _write(tmp_path, {"engine.py": big})
        task = _task("t1", ["engine.py"])

        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            return {"files": {"engine.py": data["existing_files"]["engine.py"], "ok.py": "y = 1\n"}}

        agents = harness.use(_Agents(_plan(task), implement=implement))
        state = _task_state(tmp_path, _plan(task), 0)
        pipeline = _pipeline(tmp_path, _config(LOCAL))
        state["_task_iteration"] = 1  # single pass

        await _implement(pipeline, state)

        assert state["current_patch"]["files"] == {"ok.py": "y = 1\n"}
        [blocked] = state["_blocked_paths"]
        assert blocked["file"] == "engine.py"
        assert [m["event"] for m in state["messages"]].count("patch_entry_blocked") == 1

        await _implement(pipeline, state)
        feedback = agents.inputs("implementer")[1]["review_feedback"]
        assert any(i["title"] == "File not written: engine.py" for i in feedback)

    def test_binary_and_excerpted_files_are_never_rewritten_whole(self, tmp_path: Path) -> None:
        """#98: a complete rewrite of a file shown only as an excerpt loses its unseen part."""
        state = _task_state(tmp_path, CARD_PLAN, 0)
        selection = ContextSelection(
            files={"engine.py": "head\n... [truncated 79000 bytes] ...\ntail\n"},
            binary={"logo.png": 18008},
            partial={"engine.py": 80000},
        )
        result = {"files": {"logo.png": "garbled", "engine.py": "rewrite\n", "a.py": "x\n"}}

        blocked = _pipeline(tmp_path)._resolve_patch(state, result, selection)

        assert result["files"] == {"a.py": "x\n"}
        assert blocked == [
            {"file": "logo.png", "reason": "binary file; it was not shown"},
            {"file": "engine.py", "reason": "existing file that was shown only as an excerpt"},
        ]


class TestReviewerInput:
    async def test_reviewer_sees_the_code_task_scope_and_related_files(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        # Run 6f29441bbe8c it5: files-only patch, failing test in an unchanged file.
        _write(tmp_path, {"card_deck.py": CARD_DECK, "tests/test_card_deck.py": CARD_TEST})
        agents = harness.use(_Agents(CARD_PLAN))
        pipeline = _pipeline(tmp_path)
        state = _task_state(
            tmp_path,
            CARD_PLAN,
            2,
            _completed_tasks=["t1", "t2"],
            current_patch={
                "files": {
                    "simulate_deal.py": SIMULATE_DEAL,
                    "tests/test_simulate_deal.py": "def test_deal():\n    pass\n",
                },
                "diff_unified": "",
            },
        )
        workspace = Workspace(repo_path=tmp_path, run_id="run1")
        pipeline._apply_to_workspace(state, workspace, is_git_repo=False)
        state["test_report"] = _failed(SHUFFLE)

        await pipeline._run_reviewer(state, object(), graph.CostTracker())  # type: ignore[arg-type]

        [data] = agents.inputs("reviewer")
        assert "+++ b/simulate_deal.py" in data["diff"]
        assert set(data["changed_files"]) == {"simulate_deal.py", "tests/test_simulate_deal.py"}
        assert {"tests/test_card_deck.py", "card_deck.py"} <= set(data["related_files"])
        assert data["task"]["task_id"] == "t3"
        assert data["later_tasks"] == ["Title t4"]
        assert data["task_attempt"] == 1
        assert data["patch_noop"] is False
        assert "previous_issues" not in data

        message = ReviewerAgent(provider=object(), system_prompt="")._build_user_message(data)  # type: ignore[arg-type]
        assert "def simulate_deal" in message
        assert "random.shuffle(self.cards)" in message
        assert "Print each player's hand" in message

    async def test_retry_shows_the_previous_issues_and_import_warnings(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(CARD_PLAN))
        warning = {"title": "Unresolvable import: x", "source": "import_validator"}
        state = _task_state(
            tmp_path,
            CARD_PLAN,
            2,
            review_report=_reject(_issue("Wrong count")),
            import_issues=[warning],
            _task_iteration=1,
            test_report=PASSED,
        )

        await _pipeline(tmp_path)._run_reviewer(state, object(), graph.CostTracker())  # type: ignore[arg-type]

        [data] = agents.inputs("reviewer")
        assert [i["title"] for i in data["previous_issues"]] == ["Wrong count"]
        assert data["import_warnings"] == [warning]
        assert data["task_attempt"] == 2


class TestNoopAttempts:
    @staticmethod
    def _same_patch(data: dict[str, Any], n: int) -> dict[str, Any]:
        return {"files": {"t1.py": "x = 1\n"}}

    async def test_unchanged_failing_attempt_skips_the_reviewer(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(
            _Agents(
                _plan("t1"),
                implement=self._same_patch,
                review=lambda d, n: _reject(_issue("Wrong", evidence="t1.py:1")),
            )
        )
        harness.sandbox.reports = lambda n, p: _failed("tests/test_t1.py::test_a")

        state = await _pipeline(tmp_path).execute("build it")

        assert len(agents.inputs("reviewer")) == 1
        assert len(harness.sandbox.patches) == 3
        review = _artifact(tmp_path, "review_report_002.json")
        assert review["synthesized"] is True
        assert [i["title"] for i in review["issues"]] == ["Wrong"]
        assert [e["streak"] for e in _events(tmp_path, "noop_patch")] == [1, 2]
        assert _events(tmp_path, "review_saved")[1]["synthesized"] is True
        retry = agents.inputs("implementer")[2]
        assert retry["previous_attempt"]["no_changes"] is True
        # The second no-op in a row ends the task (#73, CORE-4 stall policy)
        assert state["status"] == "stalled"
        assert state["_task_outcomes"]["t1"]["reason"] == "no_progress"
        assert state["iteration"] == 3
        # Only the reviewed attempt is scored; carried reviews are never arbitrated.
        assert [e["iteration"] for e in _events(tmp_path, "emcs_score")] == [1]
        assert _events(tmp_path, "arbitration_triggered") == []

    async def test_unchanged_attempt_that_now_passes_is_reviewed(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(
            _Agents(
                _plan("t1"),
                implement=self._same_patch,
                review=lambda d, n: _reject(_issue("Wrong")) if n == 1 else APPROVE,
            )
        )
        harness.sandbox.reports = lambda n, p: (
            _failed("tests/test_t1.py::test_a") if n == 1 else PASSED
        )

        state = await _pipeline(tmp_path).execute("build it")

        assert len(agents.inputs("reviewer")) == 2
        second = agents.inputs("reviewer")[1]
        assert second["patch_noop"] is True
        assert second["related_files"] == {"t1.py": "x = 1\n"}
        assert state["status"] == "approved"

    def test_carried_review_drops_issues_about_tests_that_pass_now(self, tmp_path: Path) -> None:
        state = _task_state(
            tmp_path,
            CARD_PLAN,
            2,
            review_report=_reject(
                _issue("Shuffle broken", evidence=f"{SHUFFLE.partition('::')[0]}:15"),
                _issue("Wrong count", evidence="tests/test_simulate_deal.py:33"),
                _issue("Style", evidence="simulate_deal.py:3"),
            ),
            test_report=_failed(
                "tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal"
            ),
        )

        Pipeline._carry_review_forward(state)

        review = state["review_report"]
        assert [i["title"] for i in review["issues"]] == ["Wrong count", "Style"]
        assert review["acceptance_met"] is False and review["synthesized"] is True
        assert state["previous_review"]["issues"][0]["title"] == "Shuffle broken"

    def test_gate_issues_are_not_duplicated_on_a_carried_review(self, tmp_path: Path) -> None:
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        removed = [{"file": "tests/test_a.py", "removed": ["tests/test_a.py::test_a"]}]
        state = _task_state(tmp_path, CARD_PLAN, 0, _removed_tests=removed, test_report=PASSED)
        state["review_report"] = _reject()
        pipeline = _pipeline(tmp_path)

        pipeline._judge_review(state, store)
        Pipeline._carry_review_forward(state)
        pipeline._judge_review(state, store)

        titles = [i["title"] for i in state["review_report"]["issues"]]
        assert titles == ["Existing tests removed: tests/test_a.py"]


class TestTestHistory:
    async def test_flip_on_an_unchanged_tree_is_flagged_flaky(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(
            _Agents(
                _plan("t1"),
                implement=lambda d, n: {"files": {"t1.py": "x = 1\n"}},
                review=lambda d, n: (
                    _reject(_issue("Shuffle", evidence="tests/test_t1.py:3")) if n == 1 else APPROVE
                ),
            )
        )
        test_a = "tests/test_t1.py::test_a"
        harness.sandbox.reports = lambda n, p: (
            _failed(test_a) if n == 1 else {**PASSED, "results": [{"name": test_a, "passed": True}]}
        )

        state = await _pipeline(tmp_path).execute("build it")

        assert agents.inputs("reviewer")[1]["test_report"]["flaky_tests"] == [test_a]
        assert _events(tmp_path, "flaky_tests_detected")[0]["tests"] == [test_a]
        assert state["_test_history"][test_a] == [[1, "failed"], [2, "passed"]]
        assert _events(tmp_path, "test_report_saved")[1]["flaky_tests"] == [test_a]

    async def test_failure_signatures_per_attempt_and_reset_on_advance(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(
            _Agents(
                _plan("t1", "t2"),
                review=lambda d, n: _reject(_issue(f"Broken {n}")) if n == 1 else APPROVE,
            )
        )
        harness.sandbox.reports = lambda n, p: (
            _failed("tests/test_t.py::test_a") if n == 1 else PASSED
        )
        signatures: list[list[str]] = []
        original = Pipeline._finish_task

        def finish(self: Pipeline, state: Any, store: Any, status: str, **kw: Any) -> bool:
            signatures.append(list(state["_failure_signatures"]))
            return original(self, state, store, status, **kw)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Pipeline, "_finish_task", finish)
            state = await _pipeline(tmp_path).execute("build it")

        assert signatures[0][0].startswith("tests/test_t.py::test_a")
        assert signatures[0][1] == ""
        assert state["_failure_signatures"] == [""]


class TestSandboxRun:
    async def test_run_options_go_to_the_sandbox_not_the_patch(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        script_task = _task(
            "t1", ["deal.py"], description="Write a script that prints each player's hand."
        )
        harness.use(
            _Agents(
                _plan(script_task),
                implement=lambda d, n: {"files": {"deal.py": "print('hand')\n"}},
            )
        )

        # The local sandbox smoke-runs scripts only when asked to (D6).
        config = _config(tolerate_flaky_tests=True, run_entrypoints=True)
        state = await _pipeline(tmp_path, config).execute("x")

        [sent] = harness.sandbox.patches
        assert sent["flaky_rerun"] is True
        assert sent["tolerate_flaky"] is True
        assert sent["entrypoints"] == ["deal.py"]
        assert not {"flaky_rerun", "entrypoints"} & set(state["current_patch"])

    async def test_no_entrypoints_when_disabled_or_outside_python(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        state = _task_state(
            tmp_path,
            _plan(_task("t1", ["deal.py"], description="a script")),
            0,
            _file_history={"deal.py": {"task_id": "t1", "iteration": 1}},
        )
        _write(tmp_path, {"deal.py": "print(1)\n"})

        assert "entrypoints" not in _pipeline(
            tmp_path, _config(run_entrypoints=False)
        )._sandbox_options(state)
        # Unset: on in the isolated sandboxes, off in the local one (D6)
        assert "entrypoints" not in _pipeline(tmp_path)._sandbox_options(state)
        docker = _pipeline(tmp_path, _config(sandbox="docker"))
        assert docker._sandbox_options(state)["entrypoints"] == ["deal.py"]
        state["project_info"] = {"languages": ["javascript"]}
        assert "entrypoints" not in docker._sandbox_options(state)

    async def test_new_toolchain_recreates_the_sandbox(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(
            _Agents(
                _plan(_task("t1", ["index.js"])),
                implement=lambda d, n: {
                    "files": {"package.json": '{"name": "x"}\n', "index.js": "module.exports = 1\n"}
                },
            )
        )

        state = await _pipeline(tmp_path).execute("node app")

        assert [c["primary_language"] for c in harness.created] == ["", "javascript"]
        assert harness.created[1]["test_commands"] == ["npm test"]
        assert state["project_info"]["languages"][0] == "javascript"
        [event] = _events(tmp_path, "sandbox_reconfigured")
        assert event["language"] == "javascript"

    async def test_first_python_file_keeps_the_default_sandbox(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        # A manifest-less project becomes "python" once a .py file lands; the
        # default sandbox is Python's already, so it is not rebuilt.
        harness.use(
            _Agents(
                _plan(_task("t1", ["calc.py"])),
                implement=lambda d, n: {"files": {"calc.py": "x = 1\n"}},
            )
        )

        state = await _pipeline(tmp_path).execute("calc")

        assert [c["primary_language"] for c in harness.created] == [""]
        assert state["project_info"]["languages"] == ["python"]
        assert _events(tmp_path, "sandbox_reconfigured") == []

    async def test_applied_diff_is_not_applied_again_in_the_sandbox(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        _git(tmp_path, "init", "-q", "-b", "main")
        _write(tmp_path, {"calc.py": "def add(a, b):\n    return a - b\n"})
        _git(tmp_path, "add", "calc.py")
        _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "init")
        workspace = Workspace(repo_path=tmp_path, run_id="run1")
        workspace.initialize()
        diff = (
            "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n"
            "-    return a - b\n+    return a + b\n"
        )
        state = _task_state(
            tmp_path, CARD_PLAN, 0, current_patch={"files": {}, "diff_unified": diff}
        )
        pipeline = _pipeline(tmp_path)

        pipeline._apply_to_workspace(state, workspace, is_git_repo=True)
        await pipeline._run_sandbox(state, harness.sandbox)  # type: ignore[arg-type]

        [sent] = harness.sandbox.patches
        assert sent["diff_unified"] == ""
        assert "a + b" in (tmp_path / "calc.py").read_text(encoding="utf-8")
        assert state["current_patch"]["diff_unified"] == diff

    async def test_diff_that_did_not_apply_is_not_tested(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        diff = "--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-a\n+b\n"
        state = _task_state(
            tmp_path, CARD_PLAN, 0, current_patch={"files": {}, "diff_unified": diff}
        )
        pipeline = _pipeline(tmp_path)

        pipeline._apply_to_workspace(
            state, Workspace(repo_path=tmp_path, run_id="r"), is_git_repo=False
        )
        await pipeline._run_sandbox(state, harness.sandbox)  # type: ignore[arg-type]

        assert harness.sandbox.patches == []
        assert state["test_report"]["passed"] is False
        assert "did not apply" in state["test_report"]["stderr"]
        [event] = [m for m in state["messages"] if m["event"] == "sandbox_skipped"]
        assert event["reason"] == "patch_not_applied"


class TestAgentFailures:
    async def test_one_reviewer_failure_fails_only_the_attempt(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        def review(data: dict[str, Any], n: int) -> dict[str, Any]:
            if n == 1:
                raise ValueError("Agent failed to produce valid output after 3 attempts")
            return APPROVE

        agents = harness.use(_Agents(_plan("t1"), review=review))

        state = await _pipeline(tmp_path).execute("build it")

        assert state["status"] == "approved"
        [event] = _events(tmp_path, "reviewer_error")
        assert event["consecutive_failures"] == 1
        [unavailable] = _events(tmp_path, "reviewer_unavailable")
        assert (unavailable["iteration"], unavailable["consecutive_failures"]) == (1, 1)
        assert _artifact(tmp_path, "review_report_001.json")["synthesized"] is True
        # the failed iteration was checkpointed as a running run
        checkpoint = _artifact(tmp_path, "state_001.json")
        assert checkpoint["status"] == "running"
        assert checkpoint["_agent_failures"] == {"reviewer": 1}
        retry = agents.inputs("implementer")[1]
        assert "review_feedback" not in retry

    async def test_a_reviewer_failure_on_the_first_task_still_advances_the_plan(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        """#108: in a 2-task plan the failed iteration keeps t1; the run then moves to t2."""

        def review(data: dict[str, Any], n: int) -> dict[str, Any]:
            if n == 1:
                raise ValueError("Agent failed to produce valid output after 3 attempts")
            return APPROVE

        agents = harness.use(_Agents(_plan("t1", "t2"), review=review))

        state = await _pipeline(tmp_path).execute("build it")

        failed = _artifact(tmp_path, "state_001.json")
        assert failed["status"] == "running"
        assert failed["current_task"]["task_id"] == "t1"
        assert failed["review_report"]["synthesized"] is True
        assert failed["_agent_failures"] == {"reviewer": 1}
        assert [_task_id(d) for d in agents.inputs("implementer")] == ["t1", "t1", "t2"]
        [advanced] = _events(tmp_path, "task_advanced")
        assert (advanced["completed_task"], advanced["next_task"]) == ("t1", "t2")
        assert {k: v["status"] for k, v in state["_task_outcomes"].items()} == {
            "t1": "approved",
            "t2": "approved",
        }
        assert state["status"] == "approved"

    async def test_a_call_that_never_validated_is_still_booked(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        """#107/#82: the attempts of a failed call count against max_cost_usd."""
        attempts = [
            {"model": "gpt-4.1", "input_tokens": 9_000, "output_tokens": 16_384},
            {"model": "gpt-4.1", "input_tokens": 9_000, "output_tokens": 32_768},
        ]
        meta = {
            "model": "gpt-4.1",
            "input_tokens": 18_000,
            "output_tokens": 49_152,
            "request_id": "req-1",
            "attempts": attempts,
        }

        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            if n == 1:
                raise TruncatedOutputError("gpt-4.1", 32_768, 32_768, meta)
            return {"files": {"t1.py": "x = 1\n"}}

        def review(data: dict[str, Any], n: int) -> dict[str, Any]:
            if n == 1:
                raise ValueError("no usage known")  # nothing to book
            return APPROVE

        harness.use(_Agents(_plan("t1"), implement=implement, review=review))
        cloud = RoleConfig(provider="openai", model="gpt-4.1")

        state = await _pipeline(tmp_path, _config(implementer=cloud, max_iterations=4)).execute(
            "build it"
        )

        log = (tmp_path / "runs" / "run1" / "provider_calls.jsonl").read_text(encoding="utf-8")
        rows = [json.loads(line) for line in log.splitlines()]
        failed = [r for r in rows if r["role"] == "implementer" and r["iteration"] == 1]
        assert [(r["input_tokens"], r["output_tokens"]) for r in failed] == [(18_000, 49_152)]
        assert failed[0]["cost_usd"] > 0
        assert len([r for r in rows if r["role"] == "reviewer"]) == 1  # failed call unbooked
        assert state["total_cost_usd"] == pytest.approx(sum(r["cost_usd"] for r in rows))
        assert _events(tmp_path, "implementer_output_truncated")

    @pytest.mark.parametrize(("prompt_version", "two_pass"), [(1, False), (4, True), (0, True)])
    async def test_two_pass_needs_a_prompt_that_explains_it(
        self, tmp_path: Path, harness: SimpleNamespace, prompt_version: int, two_pass: bool
    ) -> None:
        """#111: a localhost implementer pinned to the cloud prompt writes in one pass."""
        agents = harness.use(_Agents(_plan("t1")))
        local = RoleConfig(
            provider="openai-compatible",
            model="qwen",
            base_url="http://localhost:11434/v1",
            prompt_version=prompt_version,
        )

        await _pipeline(tmp_path, _config(implementer=local)).execute("build it")

        modes = [d["task"].get("mode") for d in agents.inputs("implementer")]
        assert modes == (["code_only", "tests_only"] if two_pass else [None])

    async def test_second_failure_in_a_row_ends_the_run(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        def review(data: dict[str, Any], n: int) -> dict[str, Any]:
            raise TimeoutError("no response from the provider within 450s")

        harness.use(_Agents(_plan("t1"), review=review))

        state = await _pipeline(tmp_path).execute("build it")

        assert state["status"] == "error"
        assert state["error"].startswith("Reviewer failed: no response")
        assert state["iteration"] == 2
        assert _artifact(tmp_path, "summary.json")["status"] == "error"

    async def test_implementer_failure_keeps_the_previous_review(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            if n == 2:
                raise ValueError("invalid JSON")
            return {"files": {f"t1_{n}.py": f"x = {n}\n"}}

        agents = harness.use(
            _Agents(
                _plan("t1"),
                implement=implement,
                review=lambda d, n: _reject(_issue("Fix it")) if n == 1 else APPROVE,
            )
        )

        state = await _pipeline(tmp_path).execute("build it")

        assert state["status"] == "approved"
        assert not (tmp_path / "runs" / "run1" / "patch_set_002.json").exists()
        carried = _artifact(tmp_path, "review_report_002.json")
        assert carried["synthesized"] is True
        assert [i["title"] for i in carried["issues"]] == ["Fix it"]
        third = agents.inputs("implementer")[2]
        assert [i["title"] for i in third["review_feedback"]] == ["Fix it"]
        assert state["_agent_failures"] == {}

    async def test_failed_replan_keeps_the_current_plan(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        def architect(data: dict[str, Any], n: int) -> dict[str, Any]:
            raise ValueError("bad JSON")

        harness.use(_Agents(CARD_PLAN, architect=architect))
        state = _task_state(tmp_path, CARD_PLAN, 2)

        state = await _pipeline(tmp_path)._run_architect(state, object(), graph.CostTracker())  # type: ignore[arg-type]

        assert state["status"] == "running"
        assert state["plan"] == CARD_PLAN
        assert state["current_task"]["task_id"] == "t3"
        assert [m["event"] for m in state["messages"]][-1] == "replan_failed"


class TestArchitectAndWorkspace:
    async def test_architect_sees_a_repo_map(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        _write(tmp_path, {"card_deck.py": CARD_DECK})
        agents = harness.use(_Agents(_plan("t1")))

        await _pipeline(tmp_path).execute("add a shuffle to the deck")

        assert "class Deck" in agents.inputs("architect")[0]["repo_map"]

    async def test_workspace_head_is_recorded_per_commit(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        _git(tmp_path, "init", "-q", "-b", "main")
        _write(tmp_path, {"app.py": "x = 1\n"})
        _git(tmp_path, "add", "app.py")
        _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "init")
        harness.use(_Agents(_plan("t1")))

        state = await _pipeline(tmp_path).execute("build it")

        assert state["workspace_sha"] == _git(tmp_path, "rev-parse", "HEAD")
        [commit] = [m for m in state["messages"] if m["event"] == "workspace_commit"]
        assert commit["sha"] == state["workspace_sha"]

    async def test_context_trimming_is_reported(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = _Agents(CARD_PLAN)
        original = agents.invoke

        async def invoke(agent: object, data: dict[str, Any], role: str) -> dict[str, Any]:
            out = await original(agent, data, role)
            out["_meta"]["context_trimmed"] = ["repo_map"]
            return out

        agents.invoke = invoke  # type: ignore[method-assign]
        harness.use(agents)
        state = _task_state(tmp_path, CARD_PLAN, 0)

        await _implement(_pipeline(tmp_path), state)

        [event] = [m for m in state["messages"] if m["event"] == "context_trimmed"]
        assert event == {
            "event": "context_trimmed",
            "role": "implementer",
            "iteration": 2,
            "keys": ["repo_map"],
        }


def test_task_change_resets_the_attempt_history(tmp_path: Path) -> None:
    state = _task_state(
        tmp_path,
        CARD_PLAN,
        0,
        _noop_streak=2,
        _failure_signatures=["a", "a"],
        _agent_failures={"reviewer": 1},
        _director_guidance=[{"title": "x"}],
        _blocked_paths=[{"file": "a.png", "reason": "binary"}],
        _test_history={SHUFFLE: [[1, "passed"]]},
    )

    _reset_for_next_task(state, CARD_PLAN["tasks"][1])

    assert state["_noop_streak"] == 0
    assert state["_failure_signatures"] == []
    assert state["_agent_failures"] == {}
    assert state["_director_guidance"] == []
    assert state["_blocked_paths"] == []
    assert state["_test_history"] == {SHUFFLE: [[1, "passed"]]}


class TestCrossLaneWiring:
    """Inputs and outputs other components provide that the pipeline must use."""

    async def test_truncated_implementer_output_is_named_and_fails_only_the_attempt(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            if n == 1:
                raise TruncatedOutputError("qwen3-coder", 16384, 32768)
            return {"files": {"t1.py": "x = 1\n"}}

        harness.use(_Agents(_plan("t1"), implement=implement))

        state = await _pipeline(tmp_path).execute("build it")

        assert state["status"] == "approved"
        [event] = _events(tmp_path, "implementer_output_truncated")
        assert event["model"] == "qwen3-coder"
        assert (event["output_tokens"], event["max_tokens"]) == (16384, 32768)

    async def test_truncated_first_plan_ends_the_run_naming_the_cause(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        def architect(data: dict[str, Any], n: int) -> dict[str, Any]:
            raise TruncatedOutputError("qwen3", 8192, 16384)

        harness.use(_Agents(_plan("t1"), architect=architect))

        state = await _pipeline(tmp_path).execute("build it")

        assert state["status"] == "error"
        assert "cut off at the token limit" in state["error"]
        assert [e["model"] for e in _events(tmp_path, "architect_output_truncated")] == ["qwen3"]

    async def test_sandboxes_are_cleaned_up_when_replaced_and_when_the_run_ends(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cleaned: list[str] = []

        class Tracked(_Sandbox):
            def __init__(self, language: str) -> None:
                super().__init__()
                self.language = language

            def cleanup(self) -> None:
                cleaned.append(self.language)

        monkeypatch.setattr(
            graph, "create_sandbox", lambda **kw: Tracked(kw["primary_language"] or "none")
        )
        harness.use(
            _Agents(
                _plan(_task("t1", ["index.js"])),
                implement=lambda d, n: {
                    "files": {"package.json": '{"name": "x"}\n', "index.js": "module.exports = 1\n"}
                },
            )
        )

        await _pipeline(tmp_path).execute("node app")

        assert cleaned == ["none", "javascript"]

    async def test_tests_to_run_become_targeted_tests_of_the_project_runner(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(
            _Agents(
                _plan("t1"),
                implement=lambda d, n: {
                    "files": {"t1.py": "x = 1\n"},
                    "tests_to_run": ["python -m pytest tests/test_t1.py", "curl evil.example"],
                },
            )
        )

        state = await _pipeline(tmp_path).execute("build it")

        [sent] = harness.sandbox.patches
        assert sent["targeted_tests"] == ["python -m pytest tests/test_t1.py"]
        assert "targeted_tests" not in state["current_patch"]
        [event] = _events(tmp_path, "targeted_tests_rejected")
        assert event["commands"] == ["curl evil.example"]

    async def test_summary_lists_each_roles_endpoint_and_prompt_without_keys(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        """#111: the effective role settings are on record, the keys are not."""
        harness.use(_Agents(_plan("t1")))
        implementer = RoleConfig(
            provider="openai-compatible",
            model="qwen",
            base_url="http://localhost:11434/v1",
            api_key="sk-secret",
        )

        await _pipeline(tmp_path, _config(implementer)).execute("build it")

        summary = _artifact(tmp_path, "summary.json")
        assert summary["roles"]["implementer"] == {
            "provider": "openai-compatible",
            "model": "qwen",
            "base_url": "http://localhost:11434/v1",
            "prompt_version": 5,
        }
        assert set(summary["roles"]) == {"architect", "implementer", "reviewer"}
        assert "sk-secret" not in json.dumps(summary)

    async def test_files_not_shown_reach_the_implementer_and_are_never_overwritten(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        selection = ContextSelection(
            files={"t1.py": "x = 1\n"},
            omitted={"engine.py": 90000},
            binary={"logo.png": 18008},
        )
        monkeypatch.setattr(graph, "select_context", lambda *a, **kw: selection)
        agents = harness.use(
            _Agents(
                _plan("t1"),
                implement=lambda d, n: {"files": {"engine.py": "rewritten\n", "t1.py": "x = 2\n"}},
            )
        )
        state = _task_state(tmp_path, _plan("t1"), 0, _task_iteration=1)

        await _implement(_pipeline(tmp_path), state)

        [data] = agents.inputs("implementer")
        assert data["omitted_files"] == {"engine.py": 90000}
        assert data["binary_files"] == {"logo.png": 18008}
        assert state["current_patch"]["files"] == {"t1.py": "x = 2\n"}
        assert [b["file"] for b in state["_blocked_paths"]] == ["engine.py"]


# --- Fix round: agent I/O gaps ---------------------------------------------------


class TestImportIssuesPerIteration:
    async def test_an_attempt_without_a_patch_resets_import_issues(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        """#65: import_issues is assigned on every iteration, a failed implementer call too."""

        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            if n == 2:
                raise ValueError("invalid JSON")
            return {"files": {"t1.py": f"import not_installed_{n}\n"}}

        harness.use(
            _Agents(
                _plan("t1"),
                implement=implement,
                review=lambda d, n: _reject(_issue(f"Broken {n}")) if n == 1 else APPROVE,
            )
        )
        snapshots: list[list[dict[str, Any]]] = []
        original = Pipeline._observe_step

        def observe(self: Pipeline, role: str, state: Any, *args: Any) -> None:
            if role == "reviewer":
                snapshots.append(list(state.get("import_issues") or []))
            original(self, role, state, *args)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Pipeline, "_observe_step", observe)
            state = await _pipeline(tmp_path).execute("build it")

        assert state["status"] == "approved"
        assert snapshots[0]  # attempt 1 had an unresolvable import
        assert snapshots[1] == []  # attempt 2: no patch, nothing to validate


class TestTestHistoryMerge:
    """#69: the per-test history merge."""

    def test_flip_sequence_keeps_the_latest_runs_in_order(self) -> None:
        history: dict[str, list[list[Any]]] = {}
        for iteration, passed in [(3, True), (4, True), (5, False), (6, True)]:
            graph._merge_test_history(history, {SHUFFLE: passed}, iteration)

        assert history[SHUFFLE] == [[3, "passed"], [4, "passed"], [5, "failed"], [6, "passed"]]

    def test_merging_the_same_iteration_twice_changes_nothing(self) -> None:
        history: dict[str, list[list[Any]]] = {}
        graph._merge_test_history(history, {SHUFFLE: False}, 5)
        once = copy.deepcopy(history)

        graph._merge_test_history(history, {SHUFFLE: False}, 5)

        assert history == once

    def test_tests_absent_from_a_run_keep_their_last_iteration(self) -> None:
        first = {f"tests/test_card_deck.py::test_{i}": True for i in range(6)}
        second = dict(list(first.items())[:2])
        history: dict[str, list[list[Any]]] = {}

        graph._merge_test_history(history, first, 1)
        graph._merge_test_history(history, second, 2)

        assert len(history) == 6
        last = sorted(runs[-1][0] for runs in history.values())
        assert last == [1, 1, 1, 1, 2, 2]

    def test_history_is_bounded(self) -> None:
        history: dict[str, list[list[Any]]] = {}
        for iteration in range(1, 9):
            graph._merge_test_history(history, {SHUFFLE: True}, iteration)

        assert [run[0] for run in history[SHUFFLE]] == [4, 5, 6, 7, 8]

    async def test_failed_tests_reach_the_sandbox_and_report_events(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(
            _Agents(
                _plan("t1"),
                review=lambda d, n: _reject(_issue(f"x{n}")) if n == 1 else APPROVE,
            )
        )
        harness.sandbox.reports = lambda n, p: _failed(SHUFFLE) if n == 1 else PASSED

        state = await _pipeline(tmp_path).execute("build it")

        first = next(m for m in state["messages"] if m["event"] == "sandbox_complete")
        assert first["failed_tests"] == [SHUFFLE]
        saved = _events(tmp_path, "test_report_saved")
        assert [e["failed_tests"] for e in saved] == [[SHUFFLE], []]


class TestSilentFailure:
    async def test_a_failure_without_output_still_reaches_the_implementer(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        """#71: no stdout, stderr or excerpt: the counts still say tests failed."""
        agents = harness.use(_Agents(CARD_PLAN))
        silent = {"passed": False, "total": 3, "failures": 1, "stdout": "", "stderr": ""}
        state = _task_state(tmp_path, CARD_PLAN, 0, test_report=silent, _task_iteration=1)

        await _implement(_pipeline(tmp_path), state)

        [data] = agents.inputs("implementer")
        assert json.loads(data["failing_tests"]) == {"passed": False, "total": 3, "failures": 1}


class TestNoopStreak:
    """#73: the no-op streak helper."""

    @staticmethod
    def _state(tmp_path: Path, **overrides: Any) -> Any:
        return _task_state(
            tmp_path, _plan("t1"), 0, current_patch={"files": {"t1.py": "x = 1\n"}}, **overrides
        )

    def test_an_unchanged_first_attempt_is_not_counted(self, tmp_path: Path) -> None:
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        state = self._state(tmp_path, patch_noop=True)

        Pipeline._track_noop(state, store, had_attempt=False)

        assert state["_noop_streak"] == 0
        assert [m for m in state["messages"] if m["event"] == "noop_patch"] == []

    def test_a_changing_patch_resets_the_streak(self, tmp_path: Path) -> None:
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        state = self._state(tmp_path, patch_noop=False, _noop_streak=2)

        Pipeline._track_noop(state, store, had_attempt=True)

        assert state["_noop_streak"] == 0

    def test_an_unchanged_retry_counts(self, tmp_path: Path) -> None:
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        state = self._state(tmp_path, patch_noop=True, _noop_streak=1)

        Pipeline._track_noop(state, store, had_attempt=True)

        assert state["_noop_streak"] == 2
        [event] = _events(tmp_path, "noop_patch")
        assert (event["streak"], event["files"]) == (2, ["t1.py"])


class TestCarriedEvidence:
    """#73: issues about a still-failing test survive a carried review, however cited."""

    FAILING: ClassVar[set[str]] = {"tests/test_simulate_deal.py"}

    @pytest.mark.parametrize(
        "evidence",
        [
            "tests/test_simulate_deal.py:33",
            "./tests/test_simulate_deal.py:33",
            "test_simulate_deal.py:33",
            "tests/test_simulate_deal.py line 33",
            "tests\\test_simulate_deal.py:33",
            "`tests/test_simulate_deal.py`, line 33",
        ],
    )
    def test_still_failing_file_is_kept(self, evidence: str) -> None:
        assert not graph._cites_passing_test({"evidence": evidence}, self.FAILING)

    @pytest.mark.parametrize(
        "evidence", ["tests/test_card_deck.py:15", "./tests/test_card_deck.py", "test_card_deck.py"]
    )
    def test_passing_file_is_dropped(self, evidence: str) -> None:
        assert graph._cites_passing_test({"evidence": evidence}, self.FAILING)

    @pytest.mark.parametrize("evidence", ["", "simulate_deal.py:3", "the shuffle looks wrong"])
    def test_no_test_file_is_never_dropped(self, evidence: str) -> None:
        assert not graph._cites_passing_test({"evidence": evidence}, self.FAILING)


class _BudgetAgent:
    """What the file budget reads of a BaseAgent: its prompt, schema and window detection."""

    def __init__(self, window: int = 0) -> None:
        self._system_prompt = "s" * 3500
        self.window = window

    def _get_output_schema(self) -> dict[str, Any]:
        return {"type": "object"}

    async def _resolve_context_window(self) -> int:
        return self.window


class TestContextBudget:
    """#75: the file budget comes from the role's context window."""

    async def test_unknown_window_keeps_the_default(self, tmp_path: Path) -> None:
        pipeline = _pipeline(tmp_path)

        assert await pipeline._context_budget("implementer", object()) == 50_000
        assert await pipeline._context_budget("implementer", _BudgetAgent(0)) == 50_000

    async def test_configured_window_bounds_the_files(self, tmp_path: Path) -> None:
        local = RoleConfig(
            provider="openai-compatible",
            model="qwen",
            base_url="http://localhost:11434/v1",
            context_window=4096,
        )
        pipeline = _pipeline(tmp_path, _config(local))

        budget = await pipeline._context_budget("implementer", _BudgetAgent())

        # (4096 - 1024 kept for the answer - ~1005 prompt/schema tokens) * 3.5 * 0.6
        assert 4_000 < budget < 4_500

    async def test_detected_window_is_used_and_bounded(self, tmp_path: Path) -> None:
        pipeline = _pipeline(tmp_path)

        small = await pipeline._context_budget("reviewer", _BudgetAgent(2048))
        huge = await pipeline._context_budget("reviewer", _BudgetAgent(1_000_000))

        assert small == graph.MIN_FILE_BUDGET
        assert huge == graph.DEFAULT_FILE_BUDGET


class TestContextTrimFeedback:
    """#75: a trimmed prompt is on record, and files it dropped are never overwritten blind."""

    TRIM: ClassVar[dict[str, Any]] = {
        "keys": ["repo_map", "existing_files"],
        "window": 4096,
        "tokens_before": 9100,
        "tokens_after": 2900,
        "dropped": {"existing_files": ["engine.py"]},
        "still_over": False,
    }

    async def test_trim_reaches_the_events_the_step_callback_and_the_overwrite_guard(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        _write(tmp_path, {"engine.py": "def run():\n    return 1\n" * 20, "t1.py": "x = 0\n"})
        plan = _plan(_task("t1", ["t1.py", "engine.py"]))
        agents = _Agents(
            plan,
            implement=lambda d, n: {"files": {"engine.py": "rewritten\n", "t1.py": "x = 2\n"}},
        )
        original = agents.invoke

        async def invoke(agent: object, data: dict[str, Any], role: str) -> dict[str, Any]:
            out = await original(agent, data, role)
            if role == "implementer":
                out["_meta"]["context_trimmed"] = copy.deepcopy(self.TRIM)
            return out

        agents.invoke = invoke  # type: ignore[method-assign]
        harness.use(agents)
        steps: list[tuple[str, dict[str, Any]]] = []
        pipeline = _pipeline(tmp_path)
        pipeline.step_callback = lambda step, data: steps.append((step, data))
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        state = _task_state(tmp_path, plan, 0, _task_iteration=1)

        await pipeline._run_implementer(state, object(), graph.CostTracker(), store)  # type: ignore[arg-type]

        [event] = _events(tmp_path, "context_trimmed")
        event.pop("timestamp", None)
        assert event == {
            "event": "context_trimmed",
            "role": "implementer",
            "iteration": 2,
            **self.TRIM,
        }
        [step] = [data for name, data in steps if name == "context_trimmed"]
        assert (step["window"], step["still_over"]) == (4096, False)
        assert state["current_patch"]["files"] == {"t1.py": "x = 2\n"}
        assert [b["file"] for b in state["_blocked_paths"]] == ["engine.py"]

    async def test_files_the_attempt_must_edit_are_marked_for_the_guard(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        _write(tmp_path, {"card_deck.py": CARD_DECK, "tests/test_card_deck.py": CARD_TEST})
        agents = harness.use(_Agents(CARD_PLAN))
        state = _task_state(tmp_path, CARD_PLAN, 1, test_report=_failed(SHUFFLE), _task_iteration=1)

        await _implement(_pipeline(tmp_path), state)

        [data] = agents.inputs("implementer")
        assert sorted(data["_protected_files"]) == ["card_deck.py", "tests/test_card_deck.py"]


class _Recorder:
    """A provider that records every request and answers with scripted patches."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.calls: list[dict[str, Any]] = []
        self.replies = replies

    async def generate(
        self,
        system_prompt: str,
        user_message: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> Any:
        from delegatron.agents.providers.base import ProviderResponse

        self.calls.append({"system": system_prompt, "user": user_message, "schema": json_schema})
        reply = {"status": "ok", "rationale": "r", "tests_to_run": [], "questions": []}
        reply.update(self.replies.pop(0))
        return ProviderResponse(
            content=json.dumps(reply), model="qwen", input_tokens=100, output_tokens=20
        )


class TestTwoPassUnderARealWindow:
    """#75: both two-pass requests fit a small context window; the trim is on record."""

    async def test_both_passes_fit_and_the_trim_is_logged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from delegatron.agents.implementer import ImplementerAgent
        from delegatron.prompts import load_prompt
        from delegatron.utils.context_budget import estimate_tokens

        monkeypatch.setattr(graph, "unload_except", lambda config, model: None)
        monkeypatch.setattr(graph, "get_gpu_power_watts", lambda: 0.0)
        engine = "".join(f"def step_{i}(value):\n    return value + {i}\n\n\n" for i in range(1200))
        assert len(engine) > 40_000
        _write(tmp_path, {"engine.py": engine})
        # Small, yet with room beside the v5 system prompt and the PatchSet schema.
        window = 4608
        local = RoleConfig(
            provider="openai-compatible",
            model="qwen",
            base_url="http://localhost:11434/v1",
            context_window=window,
        )
        recorder = _Recorder(
            [
                {"files": {"helper.py": "def helper():\n    return 1\n"}},
                {"files": {"tests/test_helper.py": "def test_helper():\n    pass\n"}},
            ]
        )
        agent = ImplementerAgent(
            provider=recorder,  # type: ignore[arg-type]
            system_prompt=load_prompt("implementer", 5),
            max_tokens=1024,
            context_window=window,
        )
        task = _task(
            "t1",
            ["helper.py", "tests/test_helper.py"],
            description="Add a helper next to engine.py that calls step_1 of engine.py.",
        )
        state = _task_state(tmp_path, _plan(task), 0)
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")

        await _pipeline(tmp_path, _config(local))._run_implementer(
            state, agent, graph.CostTracker(), store
        )

        assert len(recorder.calls) == 2
        budget = window - max(1024, window // 4)
        for call in recorder.calls:
            used = (
                estimate_tokens(call["system"])
                + estimate_tokens(json.dumps(call["schema"], indent=2))
                + estimate_tokens(call["user"])
            )
            assert used <= budget
        assert "def helper" in recorder.calls[1]["user"]  # pass 2 sees pass 1's code
        assert {e["role"] for e in _events(tmp_path, "context_trimmed")} == {"implementer"}
        assert set(state["current_patch"]["files"]) == {"helper.py", "tests/test_helper.py"}

    def test_pass_two_files_keep_the_code_first_within_budget(self) -> None:
        code = {"helper.py": "x" * 3000}
        tests = {"tests/test_engine.py": "t" * 1500}
        existing = {"engine.py": "e" * 4000, "small.py": "s" * 100}

        fitted = graph._fit_files([code, tests, existing], 4000, keep=code)

        assert list(fitted) == ["helper.py", "small.py"]
        assert graph._fit_files([code, tests, existing], 100, keep=code) == code


class TestDirectedAttemptTemperature:
    async def test_a_directed_attempt_is_sampled_warmer(
        self, tmp_path: Path, harness: SimpleNamespace, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#76/#74: the attempt a director ruling directs must not repeat the old patch."""
        agents = harness.use(_Agents(CARD_PLAN))
        ruling = {"title": "print count", "decision": "REVISE", "fix_target": "test"}
        state = _task_state(tmp_path, CARD_PLAN, 2, _director_guidance=[ruling])
        caplog.set_level("INFO", logger=graph.logger.name)
        graph.logger.addHandler(caplog.handler)
        try:
            await _implement(_pipeline(tmp_path), state)
            await _implement(_pipeline(tmp_path), state)
        finally:
            graph.logger.removeHandler(caplog.handler)

        directed, plain = agents.inputs("implementer")
        assert directed["_temperature"] == pytest.approx(0.4)
        assert "_temperature" not in plain
        assert "reason=directed" in caplog.text


class TestFlakyTests:
    """#77: nondeterministic tests are attributed, remembered and fed back."""

    OWNED = "tests/test_card_deck.py::TestDeck::test_shuffle"
    FOREIGN = "tests/test_vendor.py::test_clock"
    HISTORY: ClassVar[dict[str, Any]] = {
        "tests/test_card_deck.py": {"task_id": "t2", "iteration": 3}
    }

    def _state(self, tmp_path: Path, **overrides: Any) -> Any:
        return _task_state(
            tmp_path, CARD_PLAN, 2, _file_history=copy.deepcopy(self.HISTORY), **overrides
        )

    def test_rerun_flakes_are_attributed_and_announced(self, tmp_path: Path) -> None:
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        report = _failed("tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal")
        report["flaky_tests"] = [self.OWNED, self.FOREIGN]
        state = self._state(tmp_path, iteration=5, test_report=report)

        Pipeline._record_test_outcomes(state, store)

        [event] = _events(tmp_path, "flaky_tests_detected")
        assert event["evidence"] == "passed_on_rerun"
        assert event["tests"] == [self.OWNED, self.FOREIGN]
        owned, foreign = event["records"]
        assert owned == {
            "test": self.OWNED,
            "file": "tests/test_card_deck.py",
            "evidence": "passed_on_rerun",
            "owner": "this_run",
            "owner_task": "t2",
            "detected_iteration": 5,
        }
        assert (foreign["owner"], foreign["owner_task"]) == ("preexisting", None)
        assert set(state["_known_flaky_tests"]) == {self.OWNED, self.FOREIGN}

    async def test_owned_flakes_reach_the_implementer_across_tasks(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(CARD_PLAN))
        known = {
            self.OWNED: graph._flaky_record(self.OWNED, "flip_on_unchanged_tree", self.HISTORY, 6),
            self.FOREIGN: graph._flaky_record(self.FOREIGN, "passed_on_rerun", {}, 6),
        }
        state = self._state(tmp_path, _known_flaky_tests=known)
        _reset_for_next_task(state, CARD_PLAN["tasks"][3])
        assert set(state["_known_flaky_tests"]) == {self.OWNED, self.FOREIGN}

        await _implement(_pipeline(tmp_path), state)

        [data] = agents.inputs("implementer")
        [item] = [i for i in data["review_feedback"] if i.get("category")]
        assert item == {
            "severity": "major",
            "category": "nondeterministic_test",
            "title": f"nondeterministic_test: {self.OWNED}",
            "file": "tests/test_card_deck.py",
            "test": self.OWNED,
            "owner_task": "t2",
            "evidence": "flip_on_unchanged_tree",
            "source": "orchestrator",
        }

    def test_a_flaky_test_is_forgotten_once_its_file_changes(self, tmp_path: Path) -> None:
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        known = {self.OWNED: graph._flaky_record(self.OWNED, "passed_on_rerun", self.HISTORY, 5)}
        state = self._state(tmp_path, iteration=6, test_report=PASSED, _known_flaky_tests=known)

        Pipeline._record_test_outcomes(state, store)
        assert self.OWNED in state["_known_flaky_tests"]  # its file last changed at 3

        state["_file_history"]["tests/test_card_deck.py"] = {"task_id": "t3", "iteration": 7}
        state["iteration"] = 7
        Pipeline._record_test_outcomes(state, store)
        assert state["_known_flaky_tests"] == {}

    def test_known_flaky_tests_never_make_a_failure_recur(self, tmp_path: Path) -> None:
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        known = {self.OWNED: graph._flaky_record(self.OWNED, "passed_on_rerun", {}, 5)}
        state = self._state(
            tmp_path, iteration=6, test_report=_failed(self.OWNED), _known_flaky_tests=known
        )

        Pipeline._record_test_outcomes(state, store)

        assert state["_failure_signatures"] == [""]

    async def test_the_summary_lists_the_known_flaky_tests(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        test_id = "tests/test_t1.py::test_a"
        harness.use(
            _Agents(
                _plan("t1"),
                implement=lambda d, n: {"files": {"tests/test_t1.py": "def test_a():\n    pass\n"}},
            )
        )
        harness.sandbox.reports = lambda n, p: {**PASSED, "flaky_tests": [test_id]}

        state = await _pipeline(tmp_path).execute("build it")

        [record] = _artifact(tmp_path, "summary.json")["flaky_tests"]
        assert (record["test"], record["owner"], record["owner_task"]) == (
            test_id,
            "this_run",
            "t1",
        )
        assert _events(tmp_path, "test_report_saved")[0]["flaky_tests"] == [test_id]
        assert "_known_flaky_tests" in graph.RESUME_STATE_KEYS
        assert state["status"] == "approved"

    # A test that runs deal.py in a subprocess: no import links the two (#77, #68).
    CLI = "tests/test_cli.py::test_prints"
    CLI_TEST = (
        "import subprocess\nimport sys\n\n\ndef test_prints():\n"
        "    run = subprocess.run([sys.executable, 'deal.py'], capture_output=True, text=True)\n"
        "    assert 'dealt 5' in run.stdout\n"
    )

    def _cli_state(self, root: Path, report: dict[str, Any], last_run: str) -> Any:
        _write(root, {"deal.py": "print('dealt 5')\n", "tests/test_cli.py": self.CLI_TEST})
        files = {
            "deal.py": {"task_id": "t1", "iteration": 2},
            "tests/test_cli.py": {"task_id": "t1", "iteration": 1},
        }
        return _task_state(
            root,
            CARD_PLAN,
            0,
            test_report=report,
            _file_history=files,
            _test_history={self.CLI: [[1, last_run]]},
        )

    def test_a_failure_its_reruns_reproduce_is_no_flip(self, tmp_path: Path) -> None:
        """deal.py broke at it2; the test failed in the primary run and in both reruns."""
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        report = _failed(self.CLI, flaky_tests=[], rerun_attempts=2)
        state = self._cli_state(tmp_path, report, "passed")

        Pipeline._record_test_outcomes(state, store)

        assert report["flaky_tests"] == []
        assert state["_known_flaky_tests"] == {}
        assert state["_failure_signatures"][-1].startswith(self.CLI)
        assert not [i for i in Pipeline._implementer_feedback(state) if i.get("category")]

    def test_a_pass_after_any_file_changed_is_no_flip(self, tmp_path: Path) -> None:
        """deal.py was fixed at it2: the test it never imports passes now, for that reason."""
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        report = {**PASSED, "results": [{"name": self.CLI, "passed": True}]}
        state = self._cli_state(tmp_path, report, "failed")

        Pipeline._record_test_outcomes(state, store)

        assert report["flaky_tests"] == []
        assert state["_known_flaky_tests"] == {}

    def test_the_run_keeps_the_first_detection_of_a_flaky_test(self, tmp_path: Path) -> None:
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        sim = "tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal"
        state = self._state(
            tmp_path, iteration=5, test_report=_failed(self.OWNED, sim, flaky_tests=[self.OWNED])
        )
        Pipeline._record_test_outcomes(state, store)

        # it6: the same tree, and the test passes: a flip on an unchanged tree too
        state["iteration"] = 6
        state["test_report"] = {**PASSED, "results": [{"name": self.OWNED, "passed": True}]}
        Pipeline._record_test_outcomes(state, store)
        assert state["_known_flaky_tests"][self.OWNED]["evidence"] == "flip_on_unchanged_tree"

        # it7: the test file is made deterministic
        state["_file_history"]["tests/test_card_deck.py"] = {"task_id": "t3", "iteration": 7}
        state["iteration"] = 7
        state["test_report"] = PASSED
        Pipeline._record_test_outcomes(state, store)

        assert state["_known_flaky_tests"] == {}
        first = state["_flaky_seen"][self.OWNED]
        assert (first["evidence"], first["detected_iteration"]) == ("passed_on_rerun", 5)
        assert first["resolved_iteration"] == 7
        assert "_flaky_seen" in graph.RESUME_STATE_KEYS

    async def test_the_summary_lists_a_flaky_test_made_deterministic(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        test_id = "tests/test_t1.py::test_a"
        harness.use(
            _Agents(
                _plan("t1"),
                implement=lambda d, n: {
                    "files": {"tests/test_t1.py": f"def test_a():\n    assert {n}\n"}
                },
                review=lambda d, n: _reject(_issue("Nondeterministic")) if n == 1 else APPROVE,
            )
        )
        harness.sandbox.reports = lambda n, p: (
            {**PASSED, "flaky_tests": [test_id]} if n == 1 else PASSED
        )

        state = await _pipeline(tmp_path).execute("build it")

        assert state["status"] == "approved"
        assert state["_known_flaky_tests"] == {}
        [record] = _artifact(tmp_path, "summary.json")["flaky_tests"]
        assert record["test"] == test_id
        assert (record["evidence"], record["detected_iteration"]) == ("passed_on_rerun", 1)
        assert record["resolved_iteration"] == 2


class TestPassTwoEvents:
    async def test_pass_two_drops_and_preserved_tests_reach_events_jsonl(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        """#70: tests_preserved and implementer_pass2_dropped are in events.jsonl."""
        _write(
            tmp_path,
            {
                "lib.py": "def f():\n    return 1\n",
                "tests/test_lib.py": "def test_one():\n    pass\n\n\ndef test_two():\n    pass\n",
            },
        )

        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            if data["task"].get("mode") == "code_only":
                return {"files": {"lib.py": "def f():\n    return 2\n"}}
            return {
                "files": {
                    "lib.py": "def f():\n    return 3\n",
                    "tests/test_lib.py": "def test_one():\n    pass\n",
                }
            }

        plan = _plan(_task("t1", ["lib.py"]))
        harness.use(_Agents(plan, implement=implement))
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        state = _task_state(tmp_path, plan, 0)

        await _pipeline(tmp_path, _config(LOCAL))._run_implementer(
            state,
            object(),  # type: ignore[arg-type]
            graph.CostTracker(),
            store,
        )

        [dropped] = _events(tmp_path, "implementer_pass2_dropped")
        assert dropped["paths"] == ["lib.py"]
        [preserved] = _events(tmp_path, "tests_preserved")
        assert (preserved["file"], preserved["dropped_tests"]) == (
            "tests/test_lib.py",
            ["test_two"],
        )


# --- Fix round (GRAPH-B): patch resolution, sandbox evidence, known failures ---------

ENGINE = "".join(f"def f{i}():\n    return {i}\n\n\n" for i in range(3000))
F3 = "def f3():\n    return 3\n"


def _png() -> bytes:
    return b"\x89PNG\r\n\x1a\n\0" + bytes(range(256)) * 8


def _write_lf(root: Path, files: dict[str, str]) -> None:
    """Write *files* byte for byte (LF line endings on every platform)."""
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))


class TestEditsAndExcerpts:
    """#98: excerpted, omitted and binary files change only through ``edits``."""

    async def test_a_whole_rewrite_of_an_excerpted_file_is_dropped_and_reported(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        _write_lf(tmp_path, {"engine.py": ENGINE})
        task = _task("t1", ["engine.py"], description="Add logging to f3 in engine.py")
        seen: list[dict[str, Any]] = []

        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            seen.append(data)
            return {"files": {"engine.py": "def f3():\n    print(3)\n    return 3\n"}}

        agents = harness.use(_Agents(_plan(task), implement=implement))
        state = _task_state(tmp_path, _plan(task), 0, _task_iteration=1)
        pipeline = _pipeline(tmp_path)

        await _implement(pipeline, state)
        pipeline._apply_to_workspace(
            state, Workspace(repo_path=tmp_path, run_id="r"), is_git_repo=False
        )

        assert seen[0]["partial_files"] == {"engine.py": len(ENGINE.encode("utf-8"))}
        assert state["current_patch"]["files"] == {}
        assert [b["file"] for b in state["_blocked_paths"]] == ["engine.py"]
        assert (tmp_path / "engine.py").read_bytes() == ENGINE.encode("utf-8")
        [event] = [m for m in state["messages"] if m["event"] == "patch_entry_blocked"]
        assert event["paths"][0]["reason"] == "existing file that was shown only as an excerpt"

        await _implement(pipeline, state)
        feedback = agents.inputs("implementer")[1]["review_feedback"]
        assert any(i["title"] == "File not written: engine.py" for i in feedback)

    async def test_an_edit_keeps_the_unseen_middle_and_tail(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        _write_lf(tmp_path, {"engine.py": ENGINE})
        task = _task("t1", ["engine.py"])
        edit = {
            "path": "engine.py",
            "search": F3,
            "replace": "def f3():\n    log()\n    return 3\n",
        }
        harness.use(_Agents(_plan(task), implement=lambda d, n: {"files": {}, "edits": [edit]}))
        state = _task_state(tmp_path, _plan(task), 0, _task_iteration=1)

        await _implement(_pipeline(tmp_path), state)

        patch = state["current_patch"]
        assert patch["files"] == {"engine.py": ENGINE.replace(F3, edit["replace"])}
        assert patch["files"]["engine.py"].endswith("def f2999():\n    return 2999\n\n\n")
        assert patch["edits"] == []
        assert state["_blocked_paths"] == []

    async def test_an_edit_that_does_not_match_is_not_written_and_reported(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        _write_lf(tmp_path, {"engine.py": ENGINE})
        task = _task("t1", ["engine.py", "ok.py"])
        edit = {"path": "engine.py", "search": "def nowhere():\n", "replace": "x\n"}
        harness.use(
            _Agents(
                _plan(task),
                implement=lambda d, n: {"files": {"ok.py": "y = 1\n"}, "edits": [edit]},
            )
        )
        state = _task_state(tmp_path, _plan(task), 0, _task_iteration=1)

        await _implement(_pipeline(tmp_path), state)

        assert state["current_patch"]["files"] == {"ok.py": "y = 1\n"}
        [blocked] = state["_blocked_paths"]
        assert blocked["file"] == "engine.py"
        assert blocked["reason"].startswith("edits not applied:")
        assert any(m["event"] == "patch_entry_blocked" for m in state["messages"])

    async def test_a_task_output_that_did_not_fit_whole_stays_writable(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The smaller output is shown whole, the larger one as an excerpt it can edit."""
        a_py = "".join(f"A{i} = {i}\n" for i in range(2400))
        assert 28_000 < len(a_py) < 30_000
        b_py = "".join(f"B{i} = {i}\n" for i in range(600))
        _write_lf(tmp_path, {"a.py": a_py, "b.py": b_py})
        task = _task("t1", ["a.py", "b.py"])
        edit = {"path": "a.py", "search": "A7 = 7\n", "replace": "A7 = 70\n"}
        seen: list[dict[str, Any]] = []

        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            seen.append(data)
            return {"files": {"b.py": b_py + "B_NEW = 1\n"}, "edits": [edit]}

        harness.use(_Agents(_plan(task), implement=implement))

        async def budget(role: str, agent: object) -> int:
            return 30_000

        pipeline = _pipeline(tmp_path)
        monkeypatch.setattr(pipeline, "_context_budget", budget)
        state = _task_state(tmp_path, _plan(task), 0, _task_iteration=1)

        await _implement(pipeline, state)

        assert seen[0]["existing_files"]["b.py"] == b_py
        assert set(seen[0]["partial_files"]) == {"a.py"}
        assert state["_blocked_paths"] == []
        assert state["current_patch"]["files"] == {
            "b.py": b_py + "B_NEW = 1\n",
            "a.py": a_py.replace("A7 = 7\n", "A7 = 70\n"),
        }

    async def test_an_empty_retry_never_writes_an_excerpt_or_a_binary(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        _write_lf(tmp_path, {"engine.py": ENGINE})
        (tmp_path / "assets").mkdir()
        (tmp_path / "assets" / "logo.png").write_bytes(_png())
        task = _task("t1", ["engine.py"], description="Show assets/logo.png from engine.py")
        seen: list[dict[str, Any]] = []

        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            seen.append(data)
            return {"files": {}}

        harness.use(_Agents(_plan(task), implement=implement))
        state = _task_state(
            tmp_path,
            _plan(task),
            0,
            _task_iteration=1,
            review_report=_reject(_issue()),
            test_report=PASSED,
            patch_applied=True,
        )
        pipeline = _pipeline(tmp_path)

        await _implement(pipeline, state)
        pipeline._apply_to_workspace(
            state, Workspace(repo_path=tmp_path, run_id="r"), is_git_repo=False
        )

        assert "engine.py" in seen[0]["partial_files"]
        assert "assets/logo.png" in seen[0]["binary_files"]
        assert state["current_patch"]["files"] == {}
        assert (tmp_path / "engine.py").read_bytes() == ENGINE.encode("utf-8")
        assert (tmp_path / "assets" / "logo.png").read_bytes() == _png()


class TestPassTwoContext:
    """#98/#92: pass 2 is judged against what pass 2 was shown."""

    LIB = "def f():\n    return 1\n"
    LIB_TESTS = "from lib import f\n\n\ndef test_f():\n    assert f() == 1\n"
    CONFTEST = "import pytest\n\n\n@pytest.fixture\ndef shared():\n    return 1\n"

    def _selections(self, pass2: ContextSelection) -> Callable[..., ContextSelection]:
        pass1 = ContextSelection(
            files={"lib.py": self.LIB}, omitted={"tests/test_lib.py": len(self.LIB_TESTS)}
        )

        def select(root: Path, task: Any, *args: Any, **kwargs: Any) -> ContextSelection:
            return pass1 if "task_id" in (task or {}) else pass2

        return select

    async def _run(
        self,
        tmp_path: Path,
        harness: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        pass2: ContextSelection,
        tests: dict[str, str],
    ) -> Any:
        _write(
            tmp_path,
            {
                "lib.py": self.LIB,
                "tests/test_lib.py": self.LIB_TESTS,
                "tests/conftest.py": self.CONFTEST,
            },
        )
        monkeypatch.setattr(graph, "select_context", self._selections(pass2))

        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            if data["task"]["mode"] == "code_only":
                return {"files": {"lib.py": "def f():\n    return 1\n\n\ndef g():\n    pass\n"}}
            return {"files": dict(tests)}

        plan = _plan(_task("t1", ["lib.py"]))
        harness.use(_Agents(plan, implement=implement))
        state = _task_state(tmp_path, plan, 0)
        await _implement(_pipeline(tmp_path, _config(LOCAL)), state)
        return state

    async def test_a_test_file_shown_whole_to_pass_two_may_be_extended(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Left out of pass 1's context, shown whole to pass 2: not blocked."""
        extended = self.LIB_TESTS + "\n\ndef test_g():\n    pass\n"
        pass2 = ContextSelection(files={"tests/test_lib.py": self.LIB_TESTS})

        state = await self._run(
            tmp_path, harness, monkeypatch, pass2, {"tests/test_lib.py": extended}
        )

        assert state["current_patch"]["files"]["tests/test_lib.py"] == extended
        assert state["_blocked_paths"] == []

    async def test_unseen_fixtures_and_excerpted_helpers_are_never_rewritten(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path, {"tests/helpers.py": "def helper():\n    return 1\n" * 50})
        pass2 = ContextSelection(
            files={"tests/test_lib.py": self.LIB_TESTS, "tests/helpers.py": "head\n...tail\n"},
            partial={"tests/helpers.py": 1400},
        )
        tests = {
            "tests/conftest.py": "import pytest\n",  # unseen: would drop the shared fixture
            "tests/helpers.py": "def helper():\n    return 2\n",
            "tests/test_new.py": "def test_new():\n    pass\n",
        }

        state = await self._run(tmp_path, harness, monkeypatch, pass2, tests)

        assert set(state["current_patch"]["files"]) == {"lib.py", "tests/test_new.py"}
        assert {b["file"]: b["reason"] for b in state["_blocked_paths"]} == {
            "tests/conftest.py": "existing file that was not shown",
            "tests/helpers.py": "existing file that was shown only as an excerpt",
        }

    async def test_an_unseen_test_module_keeps_its_tests_and_gets_only_new_ones_beside(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rewrite = "def test_f():\n    pass\n\n\ndef test_g():\n    pass\n"

        state = await self._run(
            tmp_path, harness, monkeypatch, ContextSelection(), {"tests/test_lib.py": rewrite}
        )

        files = state["current_patch"]["files"]
        assert "tests/test_lib.py" not in files
        [sibling] = [path for path in files if path.startswith("tests/test_lib_")]
        assert "def test_g" in files[sibling]
        assert "def test_f" not in files[sibling]
        assert state["_blocked_paths"] == []


class TestSandboxEvidence:
    async def test_sandbox_complete_summarizes_the_entrypoint_runs(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        """#80: the event carries each run's file, exit code, timeout and output presence."""
        run = {"file": "deal.py", "returncode": 0, "timed_out": False, "stdout_head": ""}
        harness.sandbox.reports = lambda n, p: {**PASSED, "entrypoint_runs": [run]}
        state = _task_state(
            tmp_path,
            CARD_PLAN,
            0,
            current_patch={"files": {"deal.py": "print(1)\n"}},
            patch_applied=True,
        )

        await _pipeline(tmp_path)._run_sandbox(state, harness.sandbox)  # type: ignore[arg-type]

        [event] = [m for m in state["messages"] if m["event"] == "sandbox_complete"]
        assert event["entrypoint_runs"] == [
            {"file": "deal.py", "returncode": 0, "timed_out": False, "printed": False}
        ]

    @pytest.mark.parametrize(
        ("languages", "selected"),
        [
            (["python"], True),
            (["shell"], True),  # a marker-less Python project that ships run.sh
            ([], True),
            (["javascript", "python"], False),
            (["powershell"], False),
        ],
    )
    def test_entrypoints_run_where_the_sandbox_runs_python(
        self, tmp_path: Path, languages: list[str], selected: bool
    ) -> None:
        _write(tmp_path, {"deal.py": "print(1)\n", "run.sh": "python deal.py\n"})
        state = _task_state(
            tmp_path,
            _plan(_task("t1", ["deal.py"], description="a script")),
            0,
            project_info={"languages": languages},
            _file_history={"deal.py": {"task_id": "t1", "iteration": 1}},
        )

        assert Pipeline._entrypoints(state) == (["deal.py"] if selected else [])

    def test_the_sandbox_is_built_with_the_entrypoint_setting(self, tmp_path: Path) -> None:
        from delegatron.detection import detect_project

        spec = _pipeline(tmp_path, _config(run_entrypoints=True))._sandbox_spec(
            detect_project(tmp_path)
        )

        assert spec["run_entrypoints"] is True
        assert spec["test_commands"] == ["python -m pytest -v"]

    def test_tests_to_run_with_a_line_break_never_reach_a_shell(self, tmp_path: Path) -> None:
        """#96: a second line would run as a shell command of its own."""
        injected = "python -m pytest tests/test_a.py\nrm -rf ~"
        carriage = "python -m pytest tests/test_b.py\r"
        state = _task_state(
            tmp_path,
            CARD_PLAN,
            0,
            current_patch={
                "files": {},
                "tests_to_run": [injected, carriage, "pytest tests/x.py"],
            },
            project_info={"test_commands": ["python -m pytest -v"]},
        )

        options = _pipeline(tmp_path)._sandbox_options(state)

        assert options["targeted_tests"] == ["python -m pytest tests/x.py"]
        [event] = [m for m in state["messages"] if m["event"] == "targeted_tests_rejected"]
        assert event["commands"] == [injected, carriage]


class TestDiffWithDeletions:
    """#95: a diff beside deletions is applied, and the sandbox never re-applies it."""

    DIFF = (
        "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n"
        "-    return a - b\n+    return a + b\n"
    )

    def _repo(self, root: Path) -> Workspace:
        _git(root, "init", "-q", "-b", "main")
        _write(root, {"calc.py": "def add(a, b):\n    return a - b\n", "old.py": "x = 1\n"})
        _git(root, "add", "calc.py", "old.py")
        _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "init")
        workspace = Workspace(repo_path=root, run_id="run1")
        workspace.initialize()
        return workspace

    async def test_the_diff_and_the_deletions_are_applied_and_tested_once(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        workspace = self._repo(tmp_path)
        patch = {"files": {}, "delete_files": ["old.py"], "diff_unified": self.DIFF}
        state = _task_state(tmp_path, CARD_PLAN, 0, current_patch=patch)
        pipeline = _pipeline(tmp_path)

        pipeline._apply_to_workspace(state, workspace, is_git_repo=True)
        await pipeline._run_sandbox(state, harness.sandbox)  # type: ignore[arg-type]

        assert state["patch_applied"] is True
        assert "a + b" in (tmp_path / "calc.py").read_text(encoding="utf-8")
        assert not (tmp_path / "old.py").exists()
        assert state["_last_changed_files"] == ["calc.py", "old.py"]
        committed = _git(tmp_path, "show", "--name-status", "--format=", "HEAD").split()
        assert committed == ["M", "calc.py", "D", "old.py"]
        [sent] = harness.sandbox.patches
        assert sent["diff_unified"] == ""
        assert sent["delete_files"] == ["old.py"]

    @pytest.mark.parametrize("deletes", [[], ["old.py"]])
    async def test_a_diff_git_rejects_is_neither_applied_nor_tested(
        self, tmp_path: Path, harness: SimpleNamespace, deletes: list[str]
    ) -> None:
        workspace = self._repo(tmp_path)
        stale = self.DIFF.replace("return a - b", "return b - a")
        patch = {"files": {}, "diff_unified": stale, "delete_files": deletes}
        state = _task_state(tmp_path, CARD_PLAN, 0, current_patch=patch)
        pipeline = _pipeline(tmp_path)

        pipeline._apply_to_workspace(state, workspace, is_git_repo=True)
        await pipeline._run_sandbox(state, harness.sandbox)  # type: ignore[arg-type]

        assert state["patch_applied"] is False
        assert harness.sandbox.patches == []
        assert "patch_apply_failed" in [m["event"] for m in state["messages"]]
        assert "a - b" in (tmp_path / "calc.py").read_text(encoding="utf-8")
        assert (tmp_path / "old.py").exists()  # nothing of the patch is written
        [skipped] = [m for m in state["messages"] if m["event"] == "sandbox_skipped"]
        assert skipped["reason"] == "patch_not_applied"


OLD_ID = "tests/test_a.py::test_x"
NEW_ID = "tests/test_b.py::test_y"
TWO_FAILURES = (
    "=================================== FAILURES ===================================\n"
    "____________________________________ test_x ____________________________________\n"
    "E       AssertionError: OLD-TRACE\n"
    "____________________________________ test_y ____________________________________\n"
    "E       AssertionError: NEW-TRACE\n"
    "=========================== short test summary info ============================\n"
    f"FAILED {OLD_ID} - AssertionError: OLD-TRACE\n"
    f"FAILED {NEW_ID} - AssertionError: NEW-TRACE\n"
    "========================= 2 failed, 1 passed in 0.10s ==========================\n"
)


class TestKnownFailuresInAgentInput:
    """#104: known failures reach the agents apart, with their origin."""

    def _state(self, root: Path) -> Any:
        report = {
            "passed": False,
            "total": 3,
            "failures": 2,
            "stdout": TWO_FAILURES,
            "stderr": "",
            "failed_tests": [OLD_ID, NEW_ID],
            "new_failures": [NEW_ID],
            "baseline_failures": [OLD_ID],
            "task_passed": False,
        }
        return _task_state(
            root,
            CARD_PLAN,
            2,
            test_report=report,
            _known_failures={OLD_ID: "t1"},
            current_patch={"files": {"x.py": "x\n"}},
            _last_changed_files=[],
        )

    async def test_the_implementer_sees_only_new_failures_as_failing(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(CARD_PLAN))

        await _implement(_pipeline(tmp_path), self._state(tmp_path))

        [data] = agents.inputs("implementer")
        assert "NEW-TRACE" in data["failing_tests"]
        assert "OLD-TRACE" not in data["failing_tests"]
        assert data["baseline_failures"] == {OLD_ID: "t1"}

    def test_the_reviewer_sees_only_new_failures_in_the_output(self, tmp_path: Path) -> None:
        data = _pipeline(tmp_path)._build_reviewer_input(self._state(tmp_path))

        excerpt = data["test_report"]["failure_excerpt"]
        assert "NEW-TRACE" in excerpt
        assert "OLD-TRACE" not in excerpt
        assert data["baseline_failures"] == {OLD_ID: "t1"}

    def test_output_with_only_known_failures_is_not_repeated(self) -> None:
        report = {
            "passed": False,
            "stdout": TWO_FAILURES,
            "baseline_failures": [OLD_ID, NEW_ID],
            "new_failures": [],
        }

        shown = graph._without_known_failures(report)

        assert "TRACE" not in graph.report_excerpt(shown)

    def test_a_new_failure_sharing_a_known_ones_title_keeps_its_section(self) -> None:
        """Section titles leave out the file: tests/test_c.py::test_x is titled like OLD_ID."""
        same_title = "tests/test_c.py::test_x"
        report = {
            "passed": False,
            "stdout": TWO_FAILURES.replace(f"FAILED {NEW_ID}", f"FAILED {same_title}"),
            "baseline_failures": [OLD_ID],
            "new_failures": [same_title],
        }

        excerpt = graph._new_failures_excerpt(report)

        assert "OLD-TRACE" in excerpt  # the shared section stays
        assert f"FAILED {OLD_ID}" not in excerpt
        assert f"FAILED {same_title}" in excerpt
