"""Pipeline task flow, gates and control: one completion gate, per-task outcomes, task
selection, test evidence, EMCS override, known failures, security findings, constraints,
egress; the per-task stall policy, director arbitration, re-planning, observer metrics,
parallel tasks, and run 6f29441bbe8c replayed end to end.

The three LLM roles are scripted through ``Pipeline._invoke_agent`` and the sandbox
is a stub, so no provider, network, Docker or Ollama is involved.
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import json
import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from delegatron.artifacts import ArtifactStore
from delegatron.config import Config, PipelineConfig, RoleConfig
from delegatron.integrations.git_pr import MockPRCreator
from delegatron.orchestrator import graph
from delegatron.orchestrator.graph import (
    RESUME_STATE_KEYS,
    Pipeline,
    _merge_two_pass,
    _new_state,
    _next_pending_task,
)
from delegatron.orchestrator.router import MAX_REPLANS
from delegatron.worktree import WorktreeError

APPROVE: dict[str, Any] = {"summary": "ok", "acceptance_met": True, "issues": []}
PASSED: dict[str, Any] = {"passed": True, "total": 1, "failures": 0, "stdout": "", "stderr": ""}
LOCAL = RoleConfig(provider="openai-compatible", model="qwen", base_url="http://localhost:11434/v1")
CLOUD = RoleConfig(provider="openai", model="gpt-4.1")


def _issue(severity: str, title: str = "Broken") -> dict[str, Any]:
    return {"severity": severity, "title": title, "details": "d", "recommended_fix": "f"}


def _reject(*issues: dict[str, Any], summary: str = "no") -> dict[str, Any]:
    return {"summary": summary, "acceptance_met": False, "issues": list(issues)}


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
        direct: Script | None = None,
    ) -> None:
        self.plan = plan
        self.implement = implement or (
            lambda data, n: {"files": {f"{_task_id(data)}_{n}.py": f"x = {n}\n"}}
        )
        self.review = review or (lambda data, n: APPROVE)
        self.architect = architect or (lambda data, n: self.plan)
        self.direct = direct or (lambda data, n: {"resolutions": []})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def invoke(self, agent: object, input_data: dict[str, Any], role: str) -> dict[str, Any]:
        self.calls.append((role, copy.deepcopy(input_data)))
        count = self.count(role)
        out: dict[str, Any]
        if role == "architect":
            out = copy.deepcopy(self.architect(input_data, count))
        elif role == "implementer":
            out = {"status": "ok", "rationale": "r", "tests_to_run": [], "questions": []}
            out.update(self.implement(input_data, count))
        elif role == "director":
            out = copy.deepcopy(self.direct(input_data, count))
        else:
            out = copy.deepcopy(self.review(input_data, count))
        out["_meta"] = {"model": "mock-model", "input_tokens": 10, "output_tokens": 5}
        return out

    def inputs(self, role: str) -> list[dict[str, Any]]:
        return [data for called, data in self.calls if called == role]

    def task_ids(self) -> list[str]:
        return [_task_id(data) for data in self.inputs("implementer")]

    def count(self, role: str) -> int:
        return len(self.inputs(role))


class _Sandbox:
    def __init__(self) -> None:
        self.reports: Callable[[int, dict[str, Any]], dict[str, Any]] = lambda n, p: PASSED
        self.patches: list[dict[str, Any]] = []

    async def apply_and_test(self, patch: dict[str, Any]) -> dict[str, Any]:
        self.patches.append(copy.deepcopy(patch))
        return copy.deepcopy(self.reports(len(self.patches), patch))


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    sandbox = _Sandbox()
    watts: list[str] = []

    def gpu_watts() -> float:
        watts.append("sampled")
        return 100.0

    monkeypatch.setattr(graph, "cleanup_after_run", lambda config: None)
    monkeypatch.setattr(graph, "get_gpu_power_watts", gpu_watts)
    monkeypatch.setattr(graph, "create_sandbox", lambda **kwargs: sandbox)
    for name in (
        "_create_architect",
        "_create_implementer",
        "_create_reviewer",
        "_create_director",
    ):
        monkeypatch.setattr(Pipeline, name, lambda self: object())

    def use(agents: _Agents) -> _Agents:
        monkeypatch.setattr(Pipeline, "_invoke_agent", staticmethod(agents.invoke))
        return agents

    return SimpleNamespace(sandbox=sandbox, use=use, watts=watts)


def _pipeline(root: Path, config: Config | None = None, **kwargs: Any) -> Pipeline:
    return Pipeline(config=config or _config(), workspace_root=root, run_id="run1", **kwargs)


def _run_dir(root: Path) -> Path:
    return root / "runs" / "run1"


def _events(root: Path, name: str | None = None) -> list[dict[str, Any]]:
    lines = (_run_dir(root) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    return [e for e in events if name is None or e["event"] == name]


def _artifact(root: Path, name: str) -> dict[str, Any]:
    return json.loads((_run_dir(root) / name).read_text(encoding="utf-8"))


def _messages(state: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [m for m in state["messages"] if m.get("event") == name]


def _review_by_task(reviews: dict[str, Any]) -> Callable[[dict[str, Any], int], dict[str, Any]]:
    """Reviewer outcome per task id (from the implementer's latest task)."""

    def review(data: dict[str, Any], n: int) -> dict[str, Any]:
        verdict = reviews[review.task]  # type: ignore[attr-defined]
        return verdict(n) if callable(verdict) else verdict

    review.task = ""  # type: ignore[attr-defined]
    return review


def _tracking(agents: _Agents, review: Callable[..., dict[str, Any]]) -> None:
    """Point *review* at the task of the latest implementer call."""
    original = agents.invoke

    async def invoke(agent: object, input_data: dict[str, Any], role: str) -> dict[str, Any]:
        if role == "implementer":
            review.task = _task_id(input_data)  # type: ignore[attr-defined]
        return await original(agent, input_data, role)

    agents.invoke = invoke  # type: ignore[method-assign]


def _varying_major(n: int) -> dict[str, Any]:
    """A rejection whose issue title changes every time (never a stall)."""
    return _reject(_issue("major", f"Broken {n}"))


class TestCompletionGate:
    async def test_tests_passing_with_a_major_issue_never_completes_a_task(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(_plan("t1", "t2"), review=lambda d, n: _varying_major(n)))

        state = await _pipeline(tmp_path, _config(max_iterations=2)).execute("build it")

        assert agents.task_ids() == ["t1", "t1", "t2", "t2"]
        assert state["_completed_tasks"] == []
        assert state["_skipped_tasks"] == ["t1", "t2"]
        assert state["status"] == "exhausted"
        assert state["_task_outcomes"]["t1"] == {"status": "exhausted", "iterations": 2}

    async def test_single_task_with_a_major_issue_is_never_approved(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(_Agents(_plan("t1"), review=lambda d, n: _varying_major(n)))

        state = await _pipeline(tmp_path, _config(max_iterations=2)).execute("build it")

        assert state["status"] == "exhausted"
        assert state["_completed_tasks"] == []
        assert state["_task_outcomes"]["t1"]["status"] == "exhausted"

    async def test_unapplied_patch_never_completes_a_task(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(_plan("t1", "t2"), implement=lambda d, n: {"files": {}}))

        state = await _pipeline(tmp_path, _config(max_iterations=2)).execute("build it")

        assert agents.task_ids() == ["t1", "t1", "t2", "t2"]
        assert state["_completed_tasks"] == []

    async def test_approval_protects_the_tasks_passing_tests(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        test_file = "def test_one():\n    pass\n\n\ndef test_two():\n    pass\n"
        harness.use(
            _Agents(
                _plan("t1", "t2"),
                implement=lambda d, n: {"files": {f"tests/test_{_task_id(d)}.py": test_file}},
            )
        )
        harness.sandbox.reports = lambda n, p: {
            **PASSED,
            "total": 3,
            "results": [
                {"name": "tests/test_t1.py::test_one", "passed": True},
                {"name": "tests/test_t1.py::test_two", "passed": True},
                {"name": "tests/test_smoke.py::test_x", "passed": True},  # sandbox-only file
            ],
        }

        state = await _pipeline(tmp_path).execute("build it")

        assert state["status"] == "approved"
        assert state["_accepted_test_ids"] == [
            "tests/test_t1.py::test_one",
            "tests/test_t1.py::test_two",
        ]


class TestRegressionGuard:
    """Accepted tests of an earlier task cannot disappear (#70)."""

    SIX = (
        "class TestCard:\n"
        "    def test_card_creation(self):\n        pass\n\n"
        "    def test_card_representation(self):\n        pass\n\n\n"
        "class TestDeck:\n"
        "    def test_deck_initialization(self):\n        pass\n\n"
        "    def test_shuffle(self):\n        pass\n\n"
        "    def test_deal(self):\n        pass\n\n"
        "    def test_deal_all_cards(self):\n        pass\n"
    )
    TWO = (
        "class TestDeck:\n"
        "    def test_shuffle(self):\n        pass\n\n"
        "    def test_deal(self):\n        pass\n"
    )
    FILE = "tests/test_card_deck.py"

    def _resumed_at_t2(self, root: Path) -> dict[str, Any]:
        (root / "tests").mkdir()
        (root / self.FILE).write_text(self.SIX, encoding="utf-8")
        plan = _plan("t1", "t2")
        accepted = [
            f"{self.FILE}::{cls}::{name}"
            for cls, name in [
                ("TestCard", "test_card_creation"),
                ("TestCard", "test_card_representation"),
                ("TestDeck", "test_deck_initialization"),
                ("TestDeck", "test_shuffle"),
                ("TestDeck", "test_deal"),
                ("TestDeck", "test_deal_all_cards"),
            ]
        ]
        return {
            "request": "build it",
            "plan": plan,
            "all_tasks": plan["tasks"],
            "current_task": plan["tasks"][1],
            "iteration": 1,
            "messages": [],
            "_completed_tasks": ["t1"],
            "_task_outcomes": {"t1": {"status": "approved", "iterations": 1}},
            "_task_iteration": 0,
            "_accepted_test_ids": accepted,
            "_accepted_test_owners": dict.fromkeys(accepted, "t1"),
            "_file_history": {self.FILE: {"task_id": "t1", "iteration": 1}},
            "status": "running",
        }

    async def test_rewrite_blocks_until_the_tests_are_restored(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        contents = {1: self.TWO, 2: self.SIX}
        agents = harness.use(
            _Agents(_plan("t1", "t2"), implement=lambda d, n: {"files": {self.FILE: contents[n]}})
        )

        state = await _pipeline(tmp_path).execute(
            "build it", initial_state=self._resumed_at_t2(tmp_path)
        )

        assert agents.task_ids() == ["t2", "t2"]
        assert state["status"] == "approved"
        [removed] = _events(tmp_path, "tests_removed")
        assert removed["removed"] == [
            {
                "file": self.FILE,
                "removed": [
                    "TestCard::test_card_creation",
                    "TestCard::test_card_representation",
                    "TestDeck::test_deal_all_cards",
                    "TestDeck::test_deck_initialization",
                ],
                # the task whose approval protected them, and gone from the file itself
                "owner_task": "t1",
                "source": "static",
            }
        ]
        review = _artifact(tmp_path, "review_report_002.json")
        assert [i["title"] for i in review["issues"]] == [f"Existing tests removed: {self.FILE}"]
        assert agents.inputs("reviewer")[0]["removed_tests"] == removed["removed"]
        retry = agents.inputs("implementer")[1]
        assert retry["removed_tests"] == removed["removed"]
        assert any(
            i["title"].startswith("Existing tests removed") for i in retry["review_feedback"]
        )
        assert state["_removed_tests"] == []

    async def test_deleting_the_file_reports_every_test(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(
            _Agents(
                _plan("t1", "t2"), implement=lambda d, n: {"files": {}, "delete_files": [self.FILE]}
            )
        )

        state = await _pipeline(tmp_path, _config(max_iterations=1)).execute(
            "build it", initial_state=self._resumed_at_t2(tmp_path)
        )

        [entry] = _events(tmp_path, "tests_removed")[0]["removed"]
        assert len(entry["removed"]) == 6
        assert state["_task_outcomes"]["t2"]["status"] == "exhausted"

    async def test_accepted_test_missing_from_the_run_blocks(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(_Agents(_plan("t1", "t2")))
        harness.sandbox.reports = lambda n, p: {
            **PASSED,
            "results": [{"name": f"{self.FILE}::TestDeck::test_deal", "passed": True}],
        }

        state = await _pipeline(tmp_path, _config(max_iterations=1)).execute(
            "build it", initial_state=self._resumed_at_t2(tmp_path)
        )

        assert state["_task_outcomes"]["t2"]["status"] == "exhausted"
        [entry] = state["_removed_tests"]
        assert len(entry["removed"]) == 5
        assert (entry["owner_task"], entry["source"]) == ("t1", "sandbox")

    @pytest.mark.parametrize(
        ("pass2", "expected_file", "sibling"),
        [
            pytest.param("TWO", "SIX", None, id="subset-kept"),
            pytest.param("SIX_PLUS", "SIX_PLUS", None, id="superset-overwrites"),
            pytest.param("TWO_PLUS", "SIX", "only-new", id="new-test-to-sibling"),
        ],
    )
    async def test_two_pass_never_overwrites_existing_tests(
        self,
        tmp_path: Path,
        harness: SimpleNamespace,
        pass2: str,
        expected_file: str,
        sibling: str | None,
    ) -> None:
        extra = "\n\ndef test_deal_two_hands():\n    pass\n"
        sources = {
            "TWO": self.TWO,
            "SIX": self.SIX,
            "SIX_PLUS": self.SIX + extra,
            "TWO_PLUS": self.TWO + extra,
        }
        (tmp_path / "tests").mkdir()
        (tmp_path / self.FILE).write_text(self.SIX, encoding="utf-8")

        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            if data["task"].get("mode") == "code_only":
                return {"files": {"card_deck.py": "class Deck:\n    pass\n"}}
            return {"files": {self.FILE: sources[pass2]}}

        harness.use(_Agents(_plan(_task("t2", ["card_deck.py"])), implement=implement))

        state = await _pipeline(tmp_path, _config(implementer=LOCAL)).execute("build it")

        on_disk = (tmp_path / self.FILE).read_text(encoding="utf-8")
        assert on_disk == sources[expected_file]
        sibling_path = tmp_path / "tests" / "test_card_deck_t2.py"
        preserved = _messages(state, "tests_preserved")
        if sibling is None:
            assert not sibling_path.exists()
        else:
            new_tests = sibling_path.read_text(encoding="utf-8")
            assert "test_deal_two_hands" in new_tests
            assert "test_shuffle" not in new_tests
            assert preserved[0]["redirected_to"] == "tests/test_card_deck_t2.py"
        assert bool(preserved) == (pass2 != "SIX_PLUS")


class TestTaskOutcomes:
    """Per-task outcomes and the run status (#101, X10)."""

    async def test_exhausted_task_makes_the_run_partial_and_opens_no_pr(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = _Agents(_plan("t1", "t2", "t3"))
        review = _review_by_task({"t1": _varying_major, "t2": APPROVE, "t3": APPROVE})
        agents.review = review
        _tracking(agents, review)
        harness.use(agents)
        creator = MockPRCreator()
        pipeline = _pipeline(
            tmp_path, _config(max_iterations=2, create_pr=True), pr_creator=creator
        )

        state = await pipeline.execute("build it")

        assert state["status"] == "partial"
        assert state["is_approved"] is False
        summary = _artifact(tmp_path, "summary.json")
        assert summary["status"] == "partial"
        assert summary["is_approved"] is False
        assert summary["task_outcomes"] == [
            {"task_id": "t1", "title": "Title t1", "status": "exhausted", "iterations": 2},
            {"task_id": "t2", "title": "Title t2", "status": "approved", "iterations": 1},
            {"task_id": "t3", "title": "Title t3", "status": "approved", "iterations": 1},
        ]
        assert summary["skipped_tasks"] == ["t1"]
        assert "partial" in summary["pull_request"]["skipped_reason"]
        assert creator.calls == []

    async def test_stall_skips_a_middle_task(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = _Agents(_plan("t1", "t2", "t3"))
        review = _review_by_task(
            {"t1": APPROVE, "t2": _reject(_issue("major", "Same")), "t3": APPROVE}
        )
        agents.review = review
        _tracking(agents, review)
        harness.use(agents)

        state = await _pipeline(tmp_path, _config(max_iterations=5)).execute("build it")

        # The stall grace counts t2's own attempts (3 for a cloud implementer, #72)
        assert agents.task_ids() == ["t1", "t2", "t2", "t2", "t3"]
        assert state["_task_outcomes"]["t2"] == {
            "status": "stalled",
            "iterations": 3,
            "reason": "recurring_issues",
        }
        assert state["_task_outcomes"]["t3"]["status"] == "approved"
        assert state["status"] == "partial"
        [skipped] = _events(tmp_path, "task_skipped")
        assert skipped["reason"] == "stalled"
        assert skipped["next_task"] == "t3"

    async def test_stall_of_the_only_task_ends_the_run_stalled(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(_Agents(_plan("t1"), review=lambda d, n: _reject(_issue("major", "Same"))))

        state = await _pipeline(tmp_path, _config(max_iterations=5)).execute("build it")

        assert state["status"] == "stalled"
        assert state["_task_outcomes"]["t1"]["status"] == "stalled"

    async def test_every_task_gets_exactly_one_outcome(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            if _task_id(data) == "t2":
                raise RuntimeError("provider down")
            return {"files": {"a.py": "a = 1\n"}}

        harness.use(_Agents(_plan("t1", "t2", "t3"), implement=implement))

        state = await _pipeline(tmp_path).execute("build it")

        assert state["status"] == "error"
        summary = _artifact(tmp_path, "summary.json")
        assert [(r["task_id"], r["status"]) for r in summary["task_outcomes"]] == [
            ("t1", "approved"),
            ("t2", "skipped"),
            ("t3", "skipped"),
        ]

    async def test_outcomes_survive_resume(self, tmp_path: Path, harness: SimpleNamespace) -> None:
        plan = _plan("t1", "t2")
        saved = {
            "request": "build it",
            "plan": plan,
            "current_task": plan["tasks"][1],
            "iteration": 3,
            "messages": [],
            "_completed_tasks": ["t1"],
            "_task_outcomes": {"t1": {"status": "approved", "iterations": 3}},
            "status": "running",
        }
        harness.use(_Agents(plan))

        state = await _pipeline(tmp_path).execute("build it", initial_state=saved)

        assert "_task_outcomes" in RESUME_STATE_KEYS
        assert state["_task_outcomes"] == {
            "t1": {"status": "approved", "iterations": 3},
            "t2": {"status": "approved", "iterations": 1},
        }
        assert state["status"] == "approved"


class TestTaskEvents:
    async def test_without_a_callback_each_transition_is_logged_once(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(_Agents(_plan("t1", "t2", "t3")))

        await _pipeline(tmp_path).execute("build it")

        advanced = _events(tmp_path, "task_advanced")
        assert [(e["completed_task"], e["next_task"], e["progress"]) for e in advanced] == [
            ("t1", "t2", "1/3"),
            ("t2", "t3", "2/3"),
        ]
        assert all(e["next_title"] for e in advanced)

    async def test_a_persisting_callback_is_not_doubled(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(_Agents(_plan("t1", "t2")))
        server_store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        steps: list[str] = []

        def persist(step: str, data: dict[str, Any]) -> None:
            steps.append(step)
            if step != "token":
                server_store.append_event({"event": step, **data})

        pipeline = _pipeline(tmp_path, step_callback=persist, step_callback_persists=True)

        await pipeline.execute("build it")

        [advanced] = _events(tmp_path, "task_advanced")
        assert advanced["next_title"] == "Title t2"
        assert steps.count("task_advanced") == 1


class TestTaskSelection:
    """Tasks are chosen by id and dependencies, never by position (#99)."""

    @pytest.mark.parametrize(
        ("ids", "expected"),
        [(["t1", "t1", "t2"], ["t1", "t3", "t2"]), (["t1", "", "t3"], ["t1", "t2", "t3"])],
    )
    async def test_duplicate_and_empty_ids_run_every_task_once(
        self, tmp_path: Path, harness: SimpleNamespace, ids: list[str], expected: list[str]
    ) -> None:
        tasks = [{**_task(task_id), "title": f"Task {i}"} for i, task_id in enumerate(ids)]
        agents = harness.use(_Agents(_plan(*tasks)))

        state = await _pipeline(tmp_path).execute("build it")

        assert agents.task_ids() == expected
        titles = [data["task"]["title"] for data in agents.inputs("implementer")]
        assert titles == ["Task 0", "Task 1", "Task 2"]
        assert state["status"] == "approved"
        [normalized] = _events(tmp_path, "plan_normalized")
        assert any("taken" in warning for warning in normalized["warnings"])

    def test_dependencies_decide_the_order(self) -> None:
        tasks = [
            {"task_id": "tests", "depends_on": ["impl"]},
            {"task_id": "impl", "depends_on": []},
            {"task_id": "docs", "depends_on": ["missing"]},
        ]
        assert _next_pending_task(tasks, set())["task_id"] == "impl"
        assert _next_pending_task(tasks, {"impl"})["task_id"] == "tests"
        assert _next_pending_task(tasks, {"impl", "tests"})["task_id"] == "docs"
        assert _next_pending_task(tasks, {"impl", "tests", "docs"}) is None

    def test_cycle_falls_back_to_the_first_pending_task(self) -> None:
        tasks = [{"task_id": "a", "depends_on": ["b"]}, {"task_id": "b", "depends_on": ["a"]}]
        assert _next_pending_task(tasks, set())["task_id"] == "a"
        assert _next_pending_task(tasks, {"a"})["task_id"] == "b"

    def test_out_of_order_completion_selects_the_first_pending_task(self) -> None:
        tasks = [{"task_id": "t1"}, {"task_id": "t2"}, {"task_id": "t3"}]
        assert _next_pending_task(tasks, {"t3"})["task_id"] == "t1"

    def test_subset_replan_continues_with_the_remaining_tasks(self, tmp_path: Path) -> None:
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        state = _new_state("r", tmp_path)
        state["_completed_tasks"] = ["t1"]
        graph._init_task_plan(state, [_task("t2"), _task("t3")])
        assert state["current_task"]["task_id"] == "t2"
        state["is_approved"] = True

        assert _pipeline(tmp_path)._finish_task(state, store, "approved") is True

        assert state["current_task"]["task_id"] == "t3"
        assert state["_completed_tasks"] == ["t1", "t2"]


class TestTestEvidence:
    """Skipped sandboxes and smoke tests are no test evidence (#102, #92)."""

    async def test_skipped_sandbox_reports_not_passed(self, tmp_path: Path) -> None:
        state = _new_state("r", tmp_path)
        state["project_info"] = {"languages": ["c"]}
        state["current_patch"] = {"files": {"src/main.c": "int main(){}\n"}}
        sandbox = _Sandbox()

        state = await _pipeline(tmp_path)._run_sandbox(state, sandbox)  # type: ignore[arg-type]

        report = state["test_report"]
        assert report is not None
        assert report["passed"] is False
        assert report["skipped_reason"] == "non_testable_language"
        assert "c" in report["stderr"]
        assert sandbox.patches == []

    async def test_custom_image_always_runs_the_sandbox(self, tmp_path: Path) -> None:
        state = _new_state("r", tmp_path)
        state["project_info"] = {"languages": ["c"]}
        state["current_patch"] = {"files": {"src/main.c": "int main(){}\n"}}
        sandbox = _Sandbox()
        pipeline = _pipeline(tmp_path, _config(docker_image="gcc:14"))

        await pipeline._run_sandbox(state, sandbox)  # type: ignore[arg-type]

        assert len(sandbox.patches) == 1

    @pytest.mark.parametrize(
        ("review", "advanced"),
        [
            pytest.param(_reject(_issue("blocker")), False, id="blocker"),
            pytest.param(APPROVE, True, id="explicit-acceptance"),
            pytest.param(_reject(_issue("minor")), False, id="minor-rejection-no-emcs"),
        ],
    )
    async def test_untested_task_needs_explicit_reviewer_acceptance(
        self, tmp_path: Path, harness: SimpleNamespace, review: dict[str, Any], advanced: bool
    ) -> None:
        agents = harness.use(
            _Agents(
                _plan("t1", "t2"),
                implement=lambda d, n: {"files": {f"{_task_id(d)}.swift": "let x = 1\n"}},
                review=lambda d, n: review,
            )
        )

        state = await _pipeline(tmp_path, _config(max_iterations=1)).execute("build it")

        assert harness.sandbox.patches == []
        assert ("t1" in state["_completed_tasks"]) is advanced
        assert agents.task_ids() == ["t1", "t2"]
        assert not _events(tmp_path, "emcs_override")

    async def test_smoke_only_run_is_untested_and_kept_out_of_the_patch(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(_Agents(_plan("t1"), review=lambda d, n: _reject(_issue("minor"))))
        harness.sandbox.reports = lambda n, p: {**PASSED, "total": 2}

        state = await _pipeline(tmp_path, _config(max_iterations=1)).execute("build it")

        assert graph.SMOKE_TEST_PATH in harness.sandbox.patches[0]["files"]
        assert graph.SMOKE_TEST_PATH not in _artifact(tmp_path, "patch_set_001.json")["files"]
        assert _artifact(tmp_path, "test_report_001.json")["synthetic_tests"] is True
        assert state["is_approved"] is False
        assert state["status"] == "exhausted"

    async def test_a_real_test_beside_the_smoke_tests_counts(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(_Agents(_plan("t1")))
        harness.sandbox.reports = lambda n, p: {**PASSED, "total": 3}

        await _pipeline(tmp_path).execute("build it")

        assert "synthetic_tests" not in _artifact(tmp_path, "test_report_001.json")

    @pytest.mark.parametrize(
        "files",
        [
            {"deck.py": "", "deck_test.py": ""},
            {"index.js": "", "index.test.js": ""},
            {"src/a.js": "", "__tests__/a.test.js": ""},
            {"lib/a.rb": "", "spec/a_spec.rb": ""},
            {"pkg/calc.go": "", "pkg/calc_test.go": ""},
        ],
    )
    def test_patches_with_tests_get_no_smoke_file(self, files: dict[str, str]) -> None:
        patch = {"files": files}
        assert Pipeline._ensure_tests(patch) is patch

    def test_no_python_smoke_file_for_other_runners(self) -> None:
        patch = {"files": {"src/sum.js": "module.exports = 1\n"}}
        assert Pipeline._ensure_tests(patch, ["javascript"]) is patch

    @pytest.mark.parametrize("test_file", ["shop/tests.py", "tests/conftest.py"])
    def test_tests_pytest_does_not_collect_still_get_smoke_tests(self, test_file: str) -> None:
        # Django's tests.py (or a lone conftest) gives pytest nothing to collect:
        # without the smoke file a pytest run exits 5 ("no tests ran").
        patch = {"files": {"shop/models.py": "x = 1\n", test_file: "x = 2\n"}}

        smoked = Pipeline._ensure_tests(patch, ["python"])

        assert graph.SMOKE_TEST_PATH in smoked["files"]

    def test_python_source_gets_smoke_tests_in_a_new_patch(self) -> None:
        patch = {"files": {"src/calc.py": "x = 1\n"}}

        smoked = Pipeline._ensure_tests(patch, ["Python"])

        assert graph.SMOKE_TEST_PATH in smoked["files"]
        assert patch == {"files": {"src/calc.py": "x = 1\n"}}

    async def test_config_only_first_patch_is_sent_straight_back(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        patches = {1: {"files": {"requirements.txt": "requests\n"}}, 2: {"files": {"t1.py": "x\n"}}}
        agents = harness.use(_Agents(_plan("t1"), implement=lambda d, n: patches[n]))

        state = await _pipeline(tmp_path).execute("build it")

        assert len(harness.sandbox.patches) == 1
        assert agents.count("reviewer") == 1
        assert not (tmp_path / "requirements.txt").exists()
        retry = agents.inputs("implementer")[1]
        assert [i["title"] for i in retry["review_feedback"]] == ["No source code produced"]
        assert _artifact(tmp_path, "review_report_001.json")["issues"][0]["severity"] == "blocker"
        assert _events(tmp_path, "config_only_patch")
        assert state["status"] == "approved"

    async def test_config_only_task_is_not_rejected(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(
            _Agents(
                _plan(_task("t1", ["ci.yaml"])),
                implement=lambda d, n: {"files": {"ci.yaml": "on: push\n"}},
            )
        )

        state = await _pipeline(tmp_path).execute("build it")

        assert len(harness.sandbox.patches) == 1
        assert state["status"] == "approved"

    def test_merge_two_pass_keeps_tests_and_support_files(self, tmp_path: Path) -> None:
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "existing.py").write_text("x = 1\n", encoding="utf-8")
        pass1 = {"src/Counter.tsx": "code", "pkg/calc.go": "code", "src/old.test.tsx": "p1"}
        pass2 = {
            "src/Counter.test.tsx": "t",
            "pkg/calc/calc_test.go": "t",
            "tests/conftest.py": "t",
            "tests/helpers.py": "t",
            "setupTests.ts": "t",
            "src/Counter.tsx": "rewrite",
            "pkg/existing.py": "rewrite",
        }

        merged, dropped = _merge_two_pass(pass1, pass2, tmp_path)

        assert merged == {
            "src/Counter.tsx": "code",
            "pkg/calc.go": "code",
            "src/Counter.test.tsx": "t",
            "pkg/calc/calc_test.go": "t",
            "tests/conftest.py": "t",
            "tests/helpers.py": "t",
            "setupTests.ts": "t",
        }
        assert dropped == ["pkg/existing.py", "src/Counter.tsx"]

    def test_merge_two_pass_falls_back_to_pass_one_tests(self, tmp_path: Path) -> None:
        merged, dropped = _merge_two_pass({"a.py": "a", "tests/test_a.py": "t"}, {}, tmp_path)

        assert merged == {"a.py": "a", "tests/test_a.py": "t"}
        assert dropped == []

    async def test_two_pass_patch_keeps_non_python_tests(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            if data["task"]["mode"] == "code_only":
                return {"files": {"src/Counter.tsx": "export {}\n"}}
            return {"files": {"src/Counter.test.tsx": "test()\n", "src/Counter.tsx": "rewrite\n"}}

        harness.use(_Agents(_plan(_task("t1", ["src/Counter.tsx"])), implement=implement))

        state = await _pipeline(tmp_path, _config(implementer=LOCAL)).execute("build it")

        patch = _artifact(tmp_path, "patch_set_001.json")
        assert patch["files"] == {
            "src/Counter.tsx": "export {}\n",
            "src/Counter.test.tsx": "test()\n",
        }
        [dropped] = _messages(state, "implementer_pass2_dropped")
        assert dropped["paths"] == ["src/Counter.tsx"]


class TestEmcsOverride:
    """EMCS never approves an unexplained rejection (#103)."""

    async def test_rejection_without_issues_is_not_overridden(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        reviews = {1: _reject(summary="Does not match the request"), 2: APPROVE}
        agents = harness.use(_Agents(_plan("t1"), review=lambda d, n: reviews[n]))
        harness.sandbox.reports = lambda n, p: {**PASSED, "total": 5}

        await _pipeline(tmp_path).execute("build it")

        assert not _events(tmp_path, "emcs_override")
        assert agents.count("implementer") == 2
        [feedback] = agents.inputs("implementer")[1]["review_feedback"]
        assert feedback["title"] == "Reviewer rejected without itemized issues"
        assert feedback["details"] == "Does not match the request"

    async def test_minor_only_rejection_is_overridden_with_the_configured_threshold(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        review = _reject(_issue("minor", "a"), _issue("nit", "b"))
        harness.use(_Agents(_plan("t1"), review=lambda d, n: review))
        harness.sandbox.reports = lambda n, p: {**PASSED, "total": 5}

        state = await _pipeline(tmp_path, _config(emcs_threshold=0.6)).execute("build it")

        assert state["status"] == "approved"
        [override] = _events(tmp_path, "emcs_override")
        assert override["threshold"] == 0.6
        [score] = _events(tmp_path, "emcs_score")
        assert score["threshold"] == 0.6
        [saved] = _events(tmp_path, "review_saved")
        assert saved["accepted"] is True
        assert saved["emcs_override"] is True
        assert saved["reviewer_accepted"] is False
        assert _artifact(tmp_path, "review_report_001.json")["acceptance_met"] is False
        checkpoint = _artifact(tmp_path, "state_001.json")
        assert checkpoint["is_approved"] is True
        assert checkpoint["review_report"]["acceptance_met"] is False


class TestKnownFailures:
    """Tests a skipped task left failing do not block later tasks (#104)."""

    OLD = "tests/test_a.py::test_x"
    NEW = "tests/test_b.py::test_y"

    def _failing(self, *ids: str) -> dict[str, Any]:
        return {"passed": False, "total": 3, "failures": len(ids), "failed_tests": list(ids)}

    async def test_later_task_passes_despite_the_skipped_tasks_failure(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(_plan("t1", "t2")))
        harness.sandbox.reports = lambda n, p: self._failing(self.OLD)

        state = await _pipeline(tmp_path, _config(max_iterations=2)).execute("build it")

        assert agents.task_ids() == ["t1", "t1", "t2"]
        [skipped] = _events(tmp_path, "task_skipped")
        assert skipped["left_failing"] == [self.OLD]
        report = _artifact(tmp_path, "test_report_003.json")
        assert report["passed"] is False
        assert report["task_passed"] is True
        assert report["baseline_failures"] == [self.OLD]
        summary = _artifact(tmp_path, "summary.json")
        assert summary["baseline_failures"] == {self.OLD: "t1"}
        assert state["status"] == "partial"

    async def test_a_new_failure_still_blocks(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(_Agents(_plan("t1", "t2")))
        harness.sandbox.reports = lambda n, p: (
            self._failing(self.OLD) if n <= 2 else self._failing(self.OLD, self.NEW)
        )

        state = await _pipeline(tmp_path, _config(max_iterations=2)).execute("build it")

        assert state["_completed_tasks"] == []
        report = _artifact(tmp_path, "test_report_003.json")
        assert report["new_failures"] == [self.NEW]
        assert report["baseline_failures"] == [self.OLD]
        assert report["task_passed"] is False

    async def test_unattributable_failures_are_not_excused(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(_Agents(_plan("t1", "t2")))
        harness.sandbox.reports = lambda n, p: (
            self._failing(self.OLD) if n <= 2 else {"passed": False, "stderr": "install failed"}
        )

        state = await _pipeline(tmp_path, _config(max_iterations=2)).execute("build it")

        assert state["_completed_tasks"] == []
        assert "task_passed" not in _artifact(tmp_path, "test_report_003.json")


class TestSecurityFindings:
    """Findings reach the agents and block the patch and approval in "block" mode
    (#113, #114, #115, X5); a finding is tracked until its line is gone."""

    KEY = 'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n'

    @staticmethod
    def _pr_ready(root: Path, monkeypatch: pytest.MonkeyPatch) -> MockPRCreator:
        _git_repo(root)
        monkeypatch.setattr(graph, "check_gh_available", lambda: (True, ""))
        monkeypatch.setattr(graph, "check_git_remote", lambda path: (True, ""))
        monkeypatch.setattr(graph, "git_remote_url", lambda path: "https://github.com/o/r.git")
        return MockPRCreator()

    async def test_block_mode_never_writes_the_blocked_patch(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        _git_repo(tmp_path)
        patches = {1: {"files": {"app.py": self.KEY}}, 2: {"files": {"app.py": "import os\n"}}}
        agents = harness.use(_Agents(_plan("t1"), implement=lambda d, n: patches[n]))

        state = await _pipeline(tmp_path, _config(security_mode="block")).execute("build it")

        assert state["status"] == "approved"
        assert agents.count("implementer") == 2
        # iteration 1: not written, committed, tested or reviewed
        assert "AKIA" not in _git(tmp_path, "log", "-p", "--all")
        assert len(harness.sandbox.patches) == 1
        assert agents.count("reviewer") == 1
        assert "security_findings" not in agents.inputs("reviewer")[0]
        skipped = _artifact(tmp_path, "test_report_001.json")
        assert skipped["skipped_reason"] == "security_blocked"
        assert _artifact(tmp_path, "review_report_001.json")["synthesized"] is True
        assert _artifact(tmp_path, "state_001.json")["patch_applied"] is False
        [blocked] = _events(tmp_path, "security_blocked")
        assert blocked["findings"][0]["file"] == "app.py"
        assert blocked["findings"][0]["severity"] == "blocker"
        assert "aws_access_key" in blocked["findings"][0]["title"]
        # iteration 2: the implementer is told, as a review issue and as a finding
        retry = agents.inputs("implementer")[1]
        assert any(i.get("source") == "secrets" for i in retry["review_feedback"])
        assert retry["security_findings"][0]["file"] == "app.py"
        assert (tmp_path / "app.py").read_text(encoding="utf-8") == "import os\n"
        security = _artifact(tmp_path, "summary.json")["security"]
        assert security["mode"] == "block"
        assert security["unresolved_findings"] == []
        assert security["unresolved_blocking"] == 0
        assert security["blocked_patches"] == 1
        assert security["detected"] == {
            "by_kind": {"secrets": 1},
            "by_severity": {"blocker": 1},
        }

    async def test_block_mode_a_resubmitted_secret_stays_blocked_and_opens_no_pr(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        creator = self._pr_ready(tmp_path, monkeypatch)
        agents = harness.use(
            _Agents(_plan("t1"), implement=lambda d, n: {"files": {"app.py": self.KEY}})
        )
        config = _config(security_mode="block", max_iterations=2, create_pr=True)

        state = await _pipeline(tmp_path, config, pr_creator=creator).execute("build it")

        assert state["status"] == "exhausted"
        assert [e["iteration"] for e in _events(tmp_path, "security_blocked")] == [1, 2]
        assert agents.count("reviewer") == 0
        assert harness.sandbox.patches == []
        assert not (tmp_path / "app.py").exists()
        assert "AKIA" not in _git(tmp_path, "log", "-p", "--all")
        assert creator.calls == []
        assert _artifact(tmp_path, "summary.json")["security"]["blocked_patches"] == 2

    async def test_block_mode_a_clean_retry_ships_without_the_secret(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The verifier's probe: iteration 1 writes the key, iteration 2 another file."""
        creator = self._pr_ready(tmp_path, monkeypatch)
        patches = {1: {"files": {"app.py": self.KEY}}, 2: {"files": {"other.py": "x = 1\n"}}}
        harness.use(_Agents(_plan("t1"), implement=lambda d, n: patches[n]))
        config = _config(security_mode="block", create_pr=True)

        state = await _pipeline(tmp_path, config, pr_creator=creator).execute("build it")

        assert state["status"] == "approved"
        assert len(creator.calls) == 1
        assert not (tmp_path / "app.py").exists()
        assert "AKIA" not in _git(tmp_path, "log", "-p", "--all")
        assert _artifact(tmp_path, "summary.json")["security"]["unresolved_findings"] == []

    async def test_block_mode_a_task_with_an_open_finding_never_advances(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        def script(task_id: str, call: int) -> dict[str, Any]:
            return {"files": {"t1.py": self.KEY}} if task_id == "t1" else _impl_file(task_id)

        harness.use(_Agents(_plan("t1", "t2"), implement=_per_task(script)))

        state = await _pipeline(tmp_path, _config(security_mode="block", max_iterations=2)).execute(
            "build it"
        )

        assert state["_completed_tasks"] == ["t2"]
        assert state["_task_outcomes"]["t1"]["status"] == "exhausted"
        assert not [e for e in _events(tmp_path, "task_advanced") if e["completed_task"] == "t1"]
        assert not (tmp_path / "t1.py").exists()
        assert state["status"] == "partial"

    async def test_warn_mode_findings_are_advisory_and_tracked_until_fixed(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        patches = {
            1: {"files": {"app.py": self.KEY}},
            2: {"files": {"other.py": "y = 2\n"}},  # leaves the key alone
            3: {"files": {"app.py": "import os\n"}},
        }
        reviews = {
            1: _reject(_issue("major", "Remove the key")),
            2: _reject(_issue("major", "Still there")),
            3: APPROVE,
        }
        agents = harness.use(
            _Agents(
                _plan("t1"),
                implement=lambda d, n: patches[n],
                review=lambda d, n: reviews[n],
            )
        )

        state = await _pipeline(tmp_path, _config(max_iterations=4)).execute("build it")

        assert state["status"] == "approved"
        assert not _events(tmp_path, "security_blocked")
        # applied in warn mode; the reviewer sees it
        assert agents.inputs("reviewer")[0]["security_findings"][0]["source"] == "secrets"
        # the key is still reported while it is on disk, though pass 2 left it alone
        for later in agents.inputs("implementer")[1:3]:
            assert [f["file"] for f in later["security_findings"]] == ["app.py"]
        assert agents.inputs("reviewer")[1]["security_findings"][0]["file"] == "app.py"
        # the clean rewrite clears it
        assert state["security_findings"] == []
        security = _artifact(tmp_path, "summary.json")["security"]
        assert security["unresolved_findings"] == []
        assert security["detected"]["by_kind"] == {"secrets": 1}

    async def test_summary_reports_a_secret_an_earlier_task_left(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        def script(task_id: str, call: int) -> dict[str, Any]:
            return {"files": {"t1.py": self.KEY}} if task_id == "t1" else _impl_file(task_id)

        agents = harness.use(_Agents(_plan("t1", "t2"), implement=_per_task(script)))

        state = await _pipeline(tmp_path).execute("build it")

        assert state["status"] == "approved"
        # t2 is not told about t1's key, neither its implementer nor its reviewer ...
        t2_input = next(d for d in agents.inputs("implementer") if _task_id(d) == "t2")
        assert "security_findings" not in t2_input
        t1_review, t2_review = agents.inputs("reviewer")
        assert [f["file"] for f in t1_review["security_findings"]] == ["t1.py"]
        assert "security_findings" not in t2_review
        # ... but the run's summary is
        security = _artifact(tmp_path, "summary.json")["security"]
        [finding] = security["unresolved_findings"]
        assert (finding["file"], finding["severity"]) == ("t1.py", "blocker")
        assert security["unresolved"] == {
            "by_kind": {"secrets": 1},
            "by_severity": {"blocker": 1},
        }
        assert security["unresolved_blocking"] == 0  # warn mode blocks nothing

    async def test_allow_mode_keeps_findings_from_the_agents(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(
            _Agents(_plan("t1"), implement=lambda d, n: {"files": {"app.py": self.KEY}})
        )

        await _pipeline(tmp_path, _config(security_mode="allow")).execute("build it")

        assert "security_findings" not in agents.inputs("reviewer")[0]
        assert _events(tmp_path, "secrets_detected")
        assert _artifact(tmp_path, "summary.json")["security"]["unresolved_findings"] == []

    async def test_a_resumed_run_still_sees_a_secret_its_earlier_session_committed(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        """The run's security baseline is rebuilt from the run branch's fork point."""
        _git_repo(tmp_path)
        harness.use(_Agents(_plan("t1"), implement=lambda d, n: {"files": {"app.py": self.KEY}}))
        await _pipeline(tmp_path).execute("build it")
        saved = _artifact(tmp_path, "state_001.json")
        (_run_dir(tmp_path) / "summary.json").unlink()

        await _pipeline(tmp_path).execute("build it", initial_state=saved)

        [finding] = _artifact(tmp_path, "summary.json")["security"]["unresolved_findings"]
        assert finding["file"] == "app.py"


class TestConstraints:
    """Project constraints reach every role (#97, X6)."""

    async def test_every_role_sees_the_constraints(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            return {"files": {"t1.py": "x = 1\n"} if data["task"]["mode"] == "code_only" else {}}

        agents = harness.use(_Agents(_plan("t1"), implement=implement))
        config = _config(
            implementer=LOCAL,
            constraints={"no_modify": [".env"], "forbidden_patterns": ["shell=True"]},
        )

        await _pipeline(tmp_path, config).execute("build it")

        expected = {"no_modify": [".env"], "forbidden_patterns": ["shell=True"]}
        modes = [data["task"]["mode"] for data in agents.inputs("implementer")]
        assert modes == ["code_only", "tests_only"]
        for _, data in agents.calls:
            assert data["constraints"] == expected

    async def test_no_constraints_no_key(self, tmp_path: Path, harness: SimpleNamespace) -> None:
        agents = harness.use(_Agents(_plan("t1")))

        await _pipeline(tmp_path).execute("build it")

        assert all("constraints" not in data for _, data in agents.calls)


class TestEgressAndCost:
    """Zero-cloud events are recorded as they happen (#116); cost rows per call (#112)."""

    async def test_every_agent_call_is_recorded(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(_plan("t1", "t2")))

        await _pipeline(tmp_path).execute("build it")

        report = _artifact(tmp_path, "zero_cloud_report.json")
        llm_events = [e for e in report["events"] if e["category"] == "llm_call"]
        assert len(llm_events) == len(agents.calls)
        assert report["cloud_free"] is True

    async def test_e2b_sandbox_is_not_cloud_free(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(_Agents(_plan("t1")))

        await _pipeline(tmp_path, _config(sandbox="e2b")).execute("build it")

        report = _artifact(tmp_path, "zero_cloud_report.json")
        [sandbox] = [e for e in report["events"] if e["category"] == "sandbox"]
        assert sandbox["is_local"] is False
        assert _artifact(tmp_path, "summary.json")["zero_cloud"]["verified"] is False

    async def test_failing_sandbox_creation_records_no_llm_call(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_sandbox(**kwargs: Any) -> None:
            raise RuntimeError("docker unavailable")

        monkeypatch.setattr(graph, "create_sandbox", no_sandbox)
        harness.use(_Agents(_plan("t1")))

        await _pipeline(tmp_path).execute("build it")

        report = _artifact(tmp_path, "zero_cloud_report.json")
        assert not [e for e in report["events"] if e["category"] == "llm_call"]

    async def test_pr_push_is_recorded_before_finalizing(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def git(*args: str) -> None:
            subprocess.run(
                ["git", "-c", "user.name=T", "-c", "user.email=t@t", *args],
                cwd=tmp_path,
                env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull},  # no global hooks/signing
                capture_output=True,
                check=True,
            )

        git("init", "-q", "-b", "main")
        (tmp_path / "app.py").write_bytes(b"x = 1\n")
        git("add", "app.py")
        git("-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "init")
        monkeypatch.setattr(graph, "check_gh_available", lambda: (True, ""))
        monkeypatch.setattr(graph, "check_git_remote", lambda path: (True, ""))
        monkeypatch.setattr(graph, "git_remote_url", lambda path: "https://github.com/o/r.git")
        harness.use(_Agents(_plan("t1")))
        creator = MockPRCreator()

        await _pipeline(tmp_path, _config(create_pr=True), pr_creator=creator).execute("build it")

        assert len(creator.calls) == 1
        report = _artifact(tmp_path, "zero_cloud_report.json")
        [push] = [e for e in report["events"] if e["category"] == "git_push"]
        assert push["host"] == "github.com"
        assert push["is_local"] is False
        assert _artifact(tmp_path, "summary.json")["zero_cloud"]["verified"] is False

    async def test_two_pass_books_one_cost_row_per_pass_and_samples_after_generation(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = _Agents(_plan("t1"))
        original = agents.invoke

        async def invoke(agent: object, input_data: dict[str, Any], role: str) -> dict[str, Any]:
            harness.watts.append(role)
            # Measurable generation time even with Windows' ~16 ms monotonic clock.
            await asyncio.sleep(0.05)
            return await original(agent, input_data, role)

        agents.invoke = invoke  # type: ignore[method-assign]
        harness.use(agents)

        await _pipeline(tmp_path, _config(implementer=LOCAL)).execute("build it")

        log = (_run_dir(tmp_path) / "provider_calls.jsonl").read_text(encoding="utf-8")
        rows = [json.loads(line) for line in log.splitlines()]
        implementer_rows = [r for r in rows if r["role"] == "implementer"]
        assert len(implementer_rows) == 2
        assert all(r["local_cost"]["gpu_watts"] == 100.0 for r in implementer_rows)
        assert harness.watts[:6] == [
            "architect",
            "sampled",
            "implementer",
            "sampled",
            "implementer",
            "sampled",
        ]

    async def test_cloud_roles_are_never_sampled(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(_plan("t1")))

        await _pipeline(tmp_path, _config(implementer=CLOUD)).execute("build it")

        # architect and reviewer (mock, local) are sampled; the cloud implementer is not
        assert agents.count("implementer") == 1
        assert len(harness.watts) == 2

    @pytest.mark.parametrize(
        ("status_script", "recorded"),
        [("architect_fails", True), ("approved", False)],
    )
    async def test_the_failure_webhook_is_recorded_as_egress(
        self,
        tmp_path: Path,
        harness: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        status_script: str,
        recorded: bool,
    ) -> None:
        """#116 AC7: the server's alert webhook for a failed run is egress of that run."""
        monkeypatch.setenv(graph.ALERT_WEBHOOK_ENV, "https://hooks.example.com/x")

        def architect(data: dict[str, Any], n: int) -> dict[str, Any]:
            if status_script == "architect_fails":
                raise ValueError("invalid JSON after retries")
            return _plan("t1")

        harness.use(_Agents(_plan("t1"), architect=architect))

        state = await _pipeline(tmp_path).execute("build it")

        assert (state["status"] == "error") is recorded
        report = _artifact(tmp_path, "zero_cloud_report.json")
        notices = [e for e in report["events"] if e["category"] == "notification"]
        if recorded:
            [notice] = notices
            assert (notice["host"], notice["is_local"]) == ("hooks.example.com", False)
            assert _artifact(tmp_path, "summary.json")["zero_cloud"]["verified"] is False
        else:
            assert notices == []

    async def test_the_pr_api_call_is_recorded_with_its_host(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#116 fix item 3: ``gh pr create`` talks to the GitHub API (``GH_HOST`` for GHE)."""
        _git_repo(tmp_path)
        monkeypatch.setenv("GH_HOST", "github.example.corp")
        monkeypatch.setattr(graph, "check_gh_available", lambda: (True, ""))
        monkeypatch.setattr(graph, "check_git_remote", lambda path: (True, ""))
        monkeypatch.setattr(graph, "git_remote_url", lambda path: "git@github.com:o/r.git")
        harness.use(_Agents(_plan("t1")))
        creator = MockPRCreator()

        await _pipeline(tmp_path, _config(create_pr=True), pr_creator=creator).execute("x")

        report = _artifact(tmp_path, "zero_cloud_report.json")
        [api] = [e for e in report["events"] if e["category"] == "vcs_api"]
        assert (api["host"], api["is_local"]) == ("github.example.corp", False)

    async def test_a_malformed_origin_never_breaks_finalization(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#116 regression: an origin such as ``https://[::1/x`` is no reason to lose the run."""
        _git_repo(tmp_path)
        _git(tmp_path, "remote", "add", "origin", "https://[::1/x")
        monkeypatch.setattr(graph, "check_gh_available", lambda: (True, ""))
        harness.use(_Agents(_plan("t1")))
        creator = MockPRCreator()

        state = await _pipeline(tmp_path, _config(create_pr=True), pr_creator=creator).execute(
            "build it"
        )

        assert state["status"] == "approved"
        assert _artifact(tmp_path, "summary.json")["status"] == "approved"
        report = _artifact(tmp_path, "zero_cloud_report.json")
        [push] = [e for e in report["events"] if e["category"] == "git_push"]
        assert (push["host"], push["is_local"]) == ("unknown", False)
        assert _events(tmp_path, "pipeline_complete")

    async def test_cost_locality_follows_each_role_endpoint_not_the_model_name(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#112: a local model name on a cloud host is cloud; a cloud name on Ollama is local."""
        booked: list[tuple[str, bool | None]] = []
        original = graph.CostTracker.record

        def record(self: Any, *args: Any, **kwargs: Any) -> float:
            booked.append((kwargs["role"], kwargs["is_local"]))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(graph.CostTracker, "record", record)
        harness.use(_Agents(_plan("t1")))
        config = Config(
            architect=RoleConfig(
                provider="openai",
                model="llama-3.3-70b-versatile",
                base_url="https://api.groq.com/openai/v1",
            ),
            implementer=RoleConfig(
                provider="openai-compatible", model="gpt-4.1", base_url="http://localhost:11434/v1"
            ),
            reviewer=RoleConfig(
                provider="openai-compatible",
                model="claude-sonnet-4",
                base_url="http://10.0.0.5:11434/v1",
            ),
            pipeline=PipelineConfig(max_iterations=3, max_cost_usd=10.0, sandbox="local"),
        )

        await _pipeline(tmp_path, config).execute("build it")

        # the local implementer writes in two passes, each booked on its own
        assert booked == [
            ("architect", False),
            ("implementer", True),
            ("implementer", True),
            ("reviewer", True),
        ]

    async def test_a_single_pass_cloud_implementer_on_a_local_model_name_is_cloud(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        booked: list[tuple[str, bool | None]] = []
        original = graph.CostTracker.record

        def record(self: Any, *args: Any, **kwargs: Any) -> float:
            booked.append((kwargs["role"], kwargs["is_local"]))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(graph.CostTracker, "record", record)
        harness.use(_Agents(_plan("t1")))
        hosted = RoleConfig(
            provider="openai",
            model="qwen2.5-coder:14b",
            base_url="https://api.together.xyz/v1",
        )

        await _pipeline(tmp_path, _config(implementer=hosted)).execute("build it")

        assert ("implementer", False) in booked
        assert [role for role, _ in booked].count("implementer") == 1

    async def test_the_gpu_is_sampled_off_the_event_loop_thread(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#112: nvidia-smi (a blocking subprocess) never runs on the loop's thread."""
        threads: list[bool] = []

        def sample() -> float:
            threads.append(threading.current_thread() is threading.main_thread())
            return 100.0

        monkeypatch.setattr(graph, "get_gpu_power_watts", sample)
        harness.use(_Agents(_plan("t1")))

        await _pipeline(tmp_path, _config(implementer=LOCAL)).execute("build it")

        assert threads and not any(threads)

    async def test_tests_to_run_that_rerun_the_whole_suite_are_not_targeted(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        """Cross review: a cloud ``tests_to_run`` of ``pytest tests/`` is no second full run."""
        harness.use(
            _Agents(
                _plan("t1"),
                implement=lambda d, n: {
                    "files": {"t1.py": "x = 1\n"},
                    "tests_to_run": ["pytest tests/", "pytest tests/test_t1.py"],
                },
            )
        )

        await _pipeline(tmp_path).execute("build it")

        [options] = harness.sandbox.patches
        assert options["targeted_tests"] == ["python -m pytest tests/test_t1.py"]
        [rejected] = _events(tmp_path, "targeted_tests_rejected")
        assert rejected["commands"] == ["pytest tests/"]


# --- Control: stall policy, director, re-plan, observer, parallel tasks (CORE-4) ------


def _failed_run(nodeid: str, error: str, passing: tuple[str, ...] = ()) -> dict[str, Any]:
    """A failed pytest run: *nodeid* fails with *error*; the *passing* tests pass."""
    title = nodeid.partition("::")[2].replace("::", ".")
    stdout = (
        "=================================== FAILURES ===================================\n"
        f"______________________________ {title} ______________________________\n"
        f"E       {error}\n"
        "=========================== short test summary info ============================\n"
        f"FAILED {nodeid} - {error}\n"
    )
    return {
        "passed": False,
        "total": len(passing) + 1,
        "failures": 1,
        "stdout": stdout,
        "stderr": "",
        "failed_tests": [nodeid],
        "results": [
            *({"name": name, "passed": True} for name in passing),
            {"name": nodeid, "passed": False},
        ],
    }


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t", *args],
        cwd=cwd,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull},  # no global hooks/signing
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _git_repo(root: Path) -> None:
    """Make *root* a repository with one commit (the run branch forks from it)."""
    _git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_bytes(b"demo\n")
    _git(root, "add", "README.md")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "init")


def _provider_rows(root: Path) -> list[dict[str, Any]]:
    log = (_run_dir(root) / "provider_calls.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in log.splitlines()]


def _arch_issue(title: str = "Split the task") -> dict[str, Any]:
    return {**_issue("major", title), "fix_target": "architecture"}


T2_TEST = "tests/test_t2.py::test_x"


class TestStallPolicy:
    """Stalls are judged per task, on the failure itself (#72)."""

    async def test_every_task_gets_its_own_attempts(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = _Agents(_plan("t1", "t2"))
        review = _review_by_task({"t1": APPROVE, "t2": _varying_major})
        agents.review = review
        _tracking(agents, review)
        harness.use(agents)
        harness.sandbox.reports = lambda n, p: (
            # A different failure each attempt (numbers alone are masked in signatures)
            PASSED if n == 1 else _failed_run(T2_TEST, f"AssertionError: check_{n} failed")
        )

        state = await _pipeline(tmp_path).execute("build it")

        assert agents.task_ids() == ["t1", "t2", "t2", "t2"]
        assert state["iteration"] == 4
        assert state["status"] == "partial"
        assert state["_task_outcomes"]["t2"] == {"status": "exhausted", "iterations": 3}
        attempts = [
            _artifact(tmp_path, f"state_{i:03d}.json")["_task_iteration"] for i in (1, 2, 3)
        ]
        assert attempts == [0, 1, 2]

    @pytest.mark.parametrize(
        ("base_url", "grace"),
        [("http://127.0.0.1:11434/v1", 5), ("http://localhost:11434/v1", 5), ("", 3)],
    )
    def test_grace_counts_attempts_on_the_task(
        self, tmp_path: Path, base_url: str, grace: int
    ) -> None:
        implementer = RoleConfig(provider="openai-compatible", model="m", base_url=base_url)
        pipeline = _pipeline(tmp_path, _config(implementer=implementer))
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        state = _new_state("r", tmp_path)
        state.update(
            iteration=7,
            current_task=_task("t3"),
            _failure_signatures=["sig", "sig"],
            review_report=_reject(_issue("major", "Same")),
            previous_review=_reject(_issue("major", "Same")),
        )

        state["_task_iteration"] = grace - 1
        assert pipeline._detect_stall(state, store) is None
        state["_task_iteration"] = grace
        assert pipeline._detect_stall(state, store) == "recurring_failure"

        [event] = _messages(state, "stall_detected")
        assert event == {
            "event": "stall_detected",
            "iteration": 7,
            "task_id": "t3",
            "task_attempt": grace,
            "reason": "recurring_failure",
        }


class TestArbitration:
    """A recurring conflict gets one binding ruling and one directed attempt (#74)."""

    RULING: ClassVar[dict[str, Any]] = {
        "title": "Broken 3",
        "decision": "REVISE",
        "reasoning": "the test asks for the wrong count",
        "alternative": "Expect 2 in tests/test_t1.py",
        "fix_target": "test",
        "fix_file": "tests/test_t1.py",
    }

    @staticmethod
    def _recurring(n: int, p: dict[str, Any]) -> dict[str, Any]:
        """Attempt 1 fails one way, attempts 2+ fail the same other way."""
        return _failed_run("tests/test_t1.py::test_x", "AssertionError: A" if n == 1 else "B")

    async def test_director_rules_once_and_grants_one_directed_attempt(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(
            _Agents(
                _plan("t1"),
                review=lambda d, n: _varying_major(n),
                direct=lambda d, n: {"resolutions": [self.RULING]},
            )
        )
        harness.sandbox.reports = self._recurring

        state = await _pipeline(tmp_path, _config(max_iterations=6)).execute("build it")

        # Attempt 3 repeats attempt 2's failure: ruling; attempt 4 is the directed
        # one (past the 3-attempt grace); its identical failure then stalls the task.
        assert agents.count("director") == 1
        [ruling_input] = agents.inputs("director")
        assert [c["title"] for c in ruling_input["conflicts"]] == ["Broken 3"]
        assert ruling_input["task"]["task_id"] == "t1"
        assert "B" in ruling_input["failing_tests"]
        assert ruling_input["test_passed"] is False
        assert "t1_3.py" in ruling_input["changed_files"]
        implementer_inputs = agents.inputs("implementer")
        assert len(implementer_inputs) == 4
        assert "director_guidance" not in implementer_inputs[2]
        assert (
            implementer_inputs[3]["director_guidance"][0]["alternative"]
            == (self.RULING["alternative"])
        )
        revised = _artifact(tmp_path, "state_003.json")["review_report"]["issues"][0]
        assert revised["recommended_fix"] == self.RULING["alternative"]
        assert revised["fix_target"] == "test"
        [resolved] = _events(tmp_path, "arbitration_resolved")
        assert resolved["approved"] is False
        assert state["status"] == "stalled"
        assert state["_task_outcomes"]["t1"]["reason"] == "recurring_failure"
        assert state["_arbitration_pending"] is False
        assert [r["role"] for r in _provider_rows(tmp_path)].count("director") == 1

    async def test_director_failure_changes_nothing(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        def broken(data: dict[str, Any], n: int) -> dict[str, Any]:
            raise RuntimeError("director timed out")

        agents = harness.use(
            _Agents(_plan("t1"), review=lambda d, n: _varying_major(n), direct=broken)
        )
        harness.sandbox.reports = self._recurring

        state = await _pipeline(tmp_path, _config(max_iterations=6)).execute("build it")

        assert agents.count("director") == 1
        assert agents.count("implementer") == 3
        [failed] = _events(tmp_path, "arbitration_failed")
        assert "timed out" in failed["error"]
        assert state["status"] == "stalled"
        assert _artifact(tmp_path, "summary.json")["observer_metrics"]["errors"] == 1

    async def test_disabled_arbitration_creates_no_director(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # _maybe_arbitrate swallows a director failure, so a raising stub would
        # pass unnoticed: record the calls instead.
        created: list[Pipeline] = []

        def no_director(self: Pipeline) -> object:
            created.append(self)
            raise RuntimeError("director created")

        monkeypatch.setattr(Pipeline, "_create_director", no_director)
        agents = harness.use(_Agents(_plan("t1"), review=lambda d, n: _varying_major(n)))
        harness.sandbox.reports = self._recurring
        config = _config(max_iterations=6, enable_arbitration=False)

        state = await _pipeline(tmp_path, config).execute("build it")

        assert created == []
        assert _events(tmp_path, "arbitration_triggered") == []
        assert _events(tmp_path, "arbitration_failed") == []
        assert agents.count("director") == 0
        assert agents.count("implementer") == 3
        assert state["status"] == "stalled"

    async def test_accept_ruling_with_passing_tests_approves(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(
            _Agents(
                _plan("t1"),
                review=lambda d, n: _reject(_issue("major", "Use a dataclass")),
                direct=lambda d, n: {
                    "resolutions": [{"title": "use a dataclass", "decision": "ACCEPT"}]
                },
            )
        )
        harness.sandbox.reports = lambda n, p: {**PASSED, "total": 3}

        state = await _pipeline(tmp_path).execute("build it")

        assert agents.count("implementer") == 2
        assert state["status"] == "approved"
        assert _events(tmp_path, "arbitration_resolved")[0]["approved"] is True

    async def test_orchestrator_gate_issues_are_never_arbitrated(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(_plan("t1")))
        pipeline = _pipeline(tmp_path)
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        state = _new_state("r", tmp_path)
        state["_security_blockers"] = [
            {
                "severity": "blocker",
                "title": "AWS key",
                "details": "d",
                "file": "app.py",
                "source": "secrets",
            }
        ]
        [gate] = pipeline._gate_issues(state)
        state.update(
            current_task=_task("t1"),
            _task_iteration=2,
            review_report=_reject(gate),
            previous_review=_reject(gate),
        )

        await pipeline._maybe_arbitrate(state, graph.CostTracker(), store)

        assert agents.count("director") == 0
        assert state["_arbitrated_signatures"] == []


class TestReplan:
    """A re-plan keeps finished work and restarts only the current task (#79)."""

    async def test_replan_keeps_completed_tasks(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        plan = _plan("t1", "t2", "t3")
        reworked = _plan("t1", "t2", _task("t3", description="rework t3"))
        agents = _Agents(plan, architect=lambda d, n: plan if n == 1 else reworked)
        reviews = iter([APPROVE, APPROVE, _reject(_arch_issue(), _issue("minor")), APPROVE])
        agents.review = lambda d, n: next(reviews)
        harness.use(agents)

        state = await _pipeline(tmp_path).execute("build it")

        replan_input = agents.inputs("architect")[1]
        completed = [(t["task_id"], t["files"]) for t in replan_input["completed_tasks"]]
        assert completed == [("t1", ["t1_1.py"]), ("t2", ["t2_2.py"])]
        assert "t1_1.py" in replan_input["file_tree"]
        assert [i["title"] for i in replan_input["review_feedback"]] == ["Split the task"]
        assert agents.task_ids() == ["t1", "t2", "t3", "t3"]
        after = agents.inputs("implementer")[3]
        assert after["task"]["description"] == "rework t3"
        assert "review_feedback" not in after
        [replanned] = _events(tmp_path, "replanned")
        assert replanned["kept_completed"] == ["t1", "t2"]
        assert replanned["dropped_completed"] == []
        assert replanned["current_task"] == "t3"
        assert replanned["plan"]["tasks"][2]["description"] == "rework t3"
        assert _artifact(tmp_path, "work_plan.json")["tasks"][2]["description"] == "do t3"
        revision = _artifact(tmp_path, "work_plan_r1.json")
        assert revision["tasks"][2]["description"] == "rework t3"
        assert state["status"] == "approved"
        assert state["_completed_tasks"] == ["t1", "t2", "t3"]
        assert state["_task_outcomes"]["t3"]["iterations"] == 1

    async def test_replans_are_capped(self, tmp_path: Path, harness: SimpleNamespace) -> None:
        agents = harness.use(_Agents(_plan("t1"), review=lambda d, n: _reject(_arch_issue())))

        state = await _pipeline(tmp_path, _config(max_iterations=8)).execute("build it")

        assert agents.count("architect") == 1 + MAX_REPLANS
        assert _events(tmp_path, "replan_suppressed")
        assert state["_replan_count"] == MAX_REPLANS
        assert state["status"] == "stalled"

    async def test_the_replan_cap_comes_from_the_config(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(_plan("t1"), review=lambda d, n: _reject(_arch_issue())))

        state = await _pipeline(tmp_path, _config(max_iterations=8, max_replans=1)).execute("x")

        assert agents.count("architect") == 2
        assert state["_replan_count"] == 1

    async def test_a_director_ruling_that_the_plan_must_change_replans(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        # The director's REVISE with fix_target "architecture" is the
        # orchestrator's re-plan request (replan_request), with its reason.
        ruling = {
            "title": "Broken 3",
            "decision": "REVISE",
            "reasoning": "t1 cannot pass without a shared deck module",
            "alternative": "Split the deck into its own task",
            "fix_target": "architecture",
        }
        plan = _plan("t1")
        reworked = _plan("t0", _task("t1", depends_on=["t0"], description="rework t1"))
        agents = _Agents(
            plan,
            architect=lambda d, n: plan if n == 1 else reworked,
            review=lambda d, n: _varying_major(n) if n < 4 else APPROVE,
            direct=lambda d, n: {"resolutions": [ruling]},
        )
        harness.use(agents)
        harness.sandbox.reports = lambda n, p: TestArbitration._recurring(n, p) if n < 4 else PASSED

        state = await _pipeline(tmp_path, _config(max_iterations=6)).execute("build it")

        assert agents.count("director") == 1
        assert agents.count("architect") == 2
        feedback = agents.inputs("architect")[1]["review_feedback"]
        assert feedback[-1]["title"] == "Re-plan requested by the director"
        assert feedback[-1]["details"] == ruling["reasoning"]
        assert state["replan_request"] is None  # consumed by the re-plan
        assert _events(tmp_path, "replanned")

    async def test_replan_without_pending_work_is_rejected(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        plan = _plan("t1", "t2")
        agents = _Agents(plan, architect=lambda d, n: plan if n == 1 else _plan("t1"))
        reviews = iter([APPROVE, _reject(_arch_issue()), APPROVE])
        agents.review = lambda d, n: next(reviews)
        harness.use(agents)

        state = await _pipeline(tmp_path).execute("build it")

        assert _events(tmp_path, "replan_rejected")[0]["task_ids"] == ["t1"]
        assert agents.task_ids() == ["t1", "t2", "t2"]
        assert [t["task_id"] for t in state["all_tasks"]] == ["t1", "t2"]
        assert state["status"] == "approved"

    async def test_pending_work_never_merges_into_a_completed_task(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        plan = _plan(_task("t1", ["deck.py"]), "t2")
        redundant = _plan(_task("t1", ["deck.py"]), _task("t2", ["deck.py"], depends_on=["t1"]))
        agents = _Agents(plan, architect=lambda d, n: plan if n == 1 else redundant)
        reviews = iter([APPROVE, _reject(_arch_issue()), APPROVE])
        agents.review = lambda d, n: next(reviews)
        harness.use(agents)

        state = await _pipeline(tmp_path).execute("build it")

        assert _events(tmp_path, "replanned")
        assert agents.task_ids() == ["t1", "t2", "t2"]
        assert state["_completed_tasks"] == ["t1", "t2"]


class TestObserverMetrics:
    """Observer call and token counts mirror the cost tracker (#82)."""

    async def test_observer_counts_match_the_cost_breakdown(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(_Agents(_plan("t1")))

        await _pipeline(tmp_path, _config(implementer=LOCAL)).execute("build it")

        summary = _artifact(tmp_path, "summary.json")
        metrics, costs = summary["observer_metrics"], summary["cost_breakdown"]
        assert metrics["agent_calls"] == costs["total_calls"] == 4  # architect, 2 passes, reviewer
        assert metrics["total_input_tokens"] == costs["total_input_tokens"] == 40
        assert metrics["total_output_tokens"] == costs["total_output_tokens"] == 20
        assert metrics["stalls_detected"] == 0

    async def test_a_failed_agent_call_counts_as_an_error(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            if n == 1:
                raise RuntimeError("timeout")
            return {"files": {"t1.py": "x = 1\n"}}

        harness.use(_Agents(_plan("t1"), implement=implement))

        await _pipeline(tmp_path).execute("build it")

        summary = _artifact(tmp_path, "summary.json")
        assert summary["observer_metrics"]["errors"] == 1
        assert (
            summary["observer_metrics"]["agent_calls"] == summary["cost_breakdown"]["total_calls"]
        )


class TestParallelTasks:
    """Parallel mode generates first patches concurrently, then gates each task (#100)."""

    async def test_without_git_tasks_run_sequentially(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(_plan("t1", "t2", "t3")))

        state = await _pipeline(tmp_path, _config(parallel_tasks=True)).execute("build it")

        assert agents.task_ids() == ["t1", "t2", "t3"]
        assert len(_events(tmp_path, "parallel_disabled")) == 1
        assert state["status"] == "approved"

    async def test_each_task_is_implemented_once_and_gated(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        _git_repo(tmp_path)
        agents = harness.use(
            _Agents(
                _plan("t1", "t2", "t3"),
                implement=lambda d, n: {"files": {f"{_task_id(d)}_impl.py": "x = 1\n"}},
            )
        )

        state = await _pipeline(tmp_path, _config(parallel_tasks=True)).execute("build it")

        assert sorted(agents.task_ids()) == ["t1", "t2", "t3"]
        assert agents.count("reviewer") == 3
        assert state["_completed_tasks"] == ["t1", "t2", "t3"]
        assert state["status"] == "approved"
        assert all((tmp_path / f"{t}_impl.py").is_file() for t in ("t1", "t2", "t3"))
        used = [m["pregenerated"] for m in _messages(state, "implementer_complete")]
        assert used == [False, True, True]
        assert state["_parallel_patches"] == {}
        assert not list((tmp_path / ".delegatron-worktrees").glob("*/*"))

    async def test_a_later_wave_is_generated_from_the_earlier_waves_code(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        _git_repo(tmp_path)
        plan = _plan("t1", _task("t2", depends_on=["t1"]), _task("t3", depends_on=["t1"]))
        agents = harness.use(_Agents(plan))

        state = await _pipeline(tmp_path, _config(parallel_tasks=True)).execute("build it")

        [t3_input] = [data for data in agents.inputs("implementer") if _task_id(data) == "t3"]
        assert t3_input["existing_files"]["t1_1.py"] == "x = 1\n"
        assert agents.count("implementer") == 3
        assert state["status"] == "approved"

    async def test_a_failed_worktree_falls_back_to_a_live_call(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _git_repo(tmp_path)
        create = graph.WorktreeManager.create

        def flaky_create(self: graph.WorktreeManager, task_id: str) -> Path:
            if task_id.startswith("t3"):
                raise WorktreeError("worktree add failed")
            return create(self, task_id)

        monkeypatch.setattr(graph.WorktreeManager, "create", flaky_create)
        agents = harness.use(_Agents(_plan("t1", "t2", "t3")))

        state = await _pipeline(tmp_path, _config(parallel_tasks=True)).execute("build it")

        [fallback] = _events(tmp_path, "parallel_task_fallback")
        assert fallback["task_id"] == "t3"
        assert "worktree add failed" in fallback["error"]
        assert sorted(agents.task_ids()) == ["t1", "t2", "t3"]
        assert state["status"] == "approved"

    async def test_a_stale_parallel_patch_is_regenerated_with_the_conflict_as_feedback(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        _git_repo(tmp_path)

        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            return {"files": {"shared.py": f"{_task_id(data)} = {n}\n"}}

        agents = harness.use(_Agents(_plan("t1", "t2"), implement=implement))

        state = await _pipeline(tmp_path, _config(parallel_tasks=True)).execute("build it")

        [conflict] = _events(tmp_path, "parallel_patch_conflict")
        assert conflict["task_id"] == "t2"
        assert conflict["paths"] == {"shared.py": "t1"}
        assert agents.task_ids() == ["t1", "t2", "t2"]
        live = agents.inputs("implementer")[2]
        assert [i["title"] for i in live["review_feedback"]] == ["File not written: shared.py"]
        assert (tmp_path / "shared.py").read_text(encoding="utf-8") == "t2 = 3\n"
        assert state["status"] == "approved"


# --- Run 6f29441bbe8c, replayed end to end --------------------------------------

CARD_DECK = (
    "import random\n\n\n"
    "class Card:\n    def __init__(self, name):\n        self.name = name\n\n\n"
    "class Deck:\n    def __init__(self, cards):\n        self.cards = list(cards)\n\n"
    "    def shuffle(self):\n        random.shuffle(self.cards)\n\n"
    "    def deal(self, count):\n        return [self.cards.pop() for _ in range(count)]\n"
)
CARD_TESTS = (
    "from card_deck import Card, Deck\n\n\n"
    "class TestCard:\n"
    "    def test_card_creation(self):\n        assert Card('Pikachu').name == 'Pikachu'\n\n"
    "    def test_card_representation(self):\n        assert Card('Eevee')\n\n\n"
    "class TestDeck:\n"
    "    def test_deck_initialization(self):\n        assert Deck([1]).cards == [1]\n\n"
    "    def test_shuffle(self):\n        assert sorted(Deck([1, 2]).cards) == [1, 2]\n\n"
    "    def test_deal(self):\n        assert len(Deck([1, 2]).deal(1)) == 1\n\n"
    "    def test_deal_all_cards(self):\n        assert len(Deck([1, 2]).deal(2)) == 2\n"
)
CARD_IDS = tuple(
    f"tests/test_card_deck.py::{name}"
    for name in (
        "TestCard::test_card_creation",
        "TestCard::test_card_representation",
        "TestDeck::test_deck_initialization",
        "TestDeck::test_shuffle",
        "TestDeck::test_deal",
        "TestDeck::test_deal_all_cards",
    )
)
SIMULATE_DEAL = (
    "from card_deck import Card, Deck\n\n\n"
    "def simulate_deal(lines=12):\n"
    "    deck = Deck(Card(str(i)) for i in range(10))\n"
    "    for line in range(lines):\n        print(line)\n\n\n"
    'if __name__ == "__main__":\n    simulate_deal()\n'
)
DEAL_TEST = "tests/test_simulate_deal.py"
DEAL_ID = f"{DEAL_TEST}::TestSimulateDeal::test_simulate_deal"


def _deal_test(expected: int, mock: str = "unittest.mock") -> str:
    return (
        "import unittest\nimport unittest.mock\n\nfrom simulate_deal import simulate_deal\n\n\n"
        "class TestSimulateDeal(unittest.TestCase):\n"
        "    def test_simulate_deal(self):\n"
        f"        with {mock}.patch('builtins.print') as printed:\n"
        "            simulate_deal()\n"
        f"        self.assertEqual(len(printed.call_args_list), {expected})\n"
    )


RUN_PLAN: dict[str, Any] = {
    "summary": "Simulate a Pokemon card deal",
    "tasks": [
        _task("t1", ["card_deck.py"], title="Define Card and Deck Structures"),
        _task(
            "t2",
            ["card_deck.py"],
            title="Implement Deck Shuffling and Dealing",
            depends_on=["t1"],
        ),
        _task(
            "t3",
            ["simulate_deal.py", "test_simulate_deal.py"],
            title="Simulate the first deal",
            description="Write a script that deals cards to two players and prints the hands.",
            depends_on=["t2"],
        ),
    ],
    "acceptance": {"criteria": ["the script prints both hands"]},
}

# The implementer's patches for task t3, attempt by attempt (pass 1 and 2 first)
T3_PATCHES: list[dict[str, str]] = [
    {"simulate_deal.py": SIMULATE_DEAL},
    {DEAL_TEST: _deal_test(10, mock="unittest.mocks")},  # AttributeError
    {DEAL_TEST: _deal_test(10)},  # 12 != 10
    {DEAL_TEST: _deal_test(10)},  # the same patch again: a no-op
    {  # both sides of the assertion moved: 14 != 12
        "simulate_deal.py": SIMULATE_DEAL.replace("lines=12", "lines=14"),
        DEAL_TEST: _deal_test(12),
    },
    {DEAL_TEST: _deal_test(14)},  # the director's ruling: fix only the test
]
T3_REPORTS = [
    _failed_run(DEAL_ID, "AttributeError: module 'unittest' has no attribute 'mocks'", CARD_IDS),
    _failed_run(DEAL_ID, "AssertionError: 12 != 10", CARD_IDS),
    _failed_run(DEAL_ID, "AssertionError: 12 != 10", CARD_IDS),
    _failed_run(DEAL_ID, "AssertionError: 14 != 12", CARD_IDS),
]
PRINT_CALLS = "Incorrect number of print calls during simulation"
RULING: dict[str, Any] = {
    "title": PRINT_CALLS + ".",
    "decision": "REVISE",
    "reasoning": "The task asks to print the hands; the code does, the test miscounts.",
    "alternative": f"In {DEAL_TEST} expect the 14 lines simulate_deal prints.",
    "fix_target": "test",
    "fix_file": DEAL_TEST,
}


class TestRunStory:
    """Run 6f29441bbe8c (local preset) replayed: plan, t1, then t3's struggle.

    The architect's redundant t2 is merged and t3's root-level test moves to
    ``tests/``; t1 is written in two passes and its six tests become protected;
    t3 sees t1's code, fails on a bad mock, then on a print count; an identical
    patch skips the reviewer; when both sides of the assertion move and the
    reviewer repeats itself, the director rules once and the directed attempt
    passes.
    """

    @staticmethod
    def _implement(data: dict[str, Any], n: int) -> dict[str, Any]:
        if _task_id(data) == "t1":
            code_only = data["task"].get("mode") == "code_only"
            files = (
                {"card_deck.py": CARD_DECK}
                if code_only
                else {"tests/test_card_deck.py": CARD_TESTS}
            )
            return {"files": files}
        return {"files": T3_PATCHES[n - 3]}

    @staticmethod
    def _review(data: dict[str, Any], n: int) -> dict[str, Any]:
        mock_issue = {**_issue("major", "Incorrect Mock Import"), "evidence": f"{DEAL_TEST}:2"}
        return [
            APPROVE,
            _reject(mock_issue),
            _reject(_issue("major", PRINT_CALLS)),
            _reject(_issue("major", PRINT_CALLS + ".")),
            APPROVE,
        ][n - 1]

    @staticmethod
    def _sandbox(n: int, patch: dict[str, Any]) -> dict[str, Any]:
        if n == 1:
            return {
                **PASSED,
                "total": 6,
                "results": [{"name": i, "passed": True} for i in CARD_IDS],
            }
        if n <= 5:
            return T3_REPORTS[n - 2]
        names = (*CARD_IDS, DEAL_ID)
        return {**PASSED, "total": 7, "results": [{"name": i, "passed": True} for i in names]}

    async def test_the_run_converges(self, tmp_path: Path, harness: SimpleNamespace) -> None:
        agents = harness.use(
            _Agents(
                RUN_PLAN,
                implement=self._implement,
                review=self._review,
                direct=lambda d, n: {"resolutions": [RULING]},
            )
        )
        harness.sandbox.reports = self._sandbox
        config = _config(implementer=LOCAL, max_iterations=10)

        state = await _pipeline(tmp_path, config).execute(
            "Simulate Pokemon card game card selection on a first deal via python script"
        )

        # The plan: redundant t2 merged into t1, t3's test under tests/
        assert [t["task_id"] for t in state["all_tasks"]] == ["t1", "t3"]
        t3 = state["all_tasks"][1]
        assert t3["depends_on"] == ["t1"]
        assert t3["outputs"] == ["simulate_deal.py", DEAL_TEST]
        assert _events(tmp_path, "plan_normalized")

        # Every attempt, in order: two passes for each task's first attempt
        calls = [(_task_id(d), d["task"].get("mode")) for d in agents.inputs("implementer")]
        assert calls == [
            ("t1", "code_only"),
            ("t1", "tests_only"),
            ("t3", "code_only"),
            ("t3", "tests_only"),
            *[("t3", None)] * 4,
        ]
        inputs = agents.inputs("implementer")

        # t3 builds on t1's code: shown as a current file and in the repo map
        assert inputs[2]["existing_files"]["card_deck.py"] == CARD_DECK
        assert "card_deck.py" in inputs[2]["repo_map"]
        # Imports are validated after apply: card_deck resolves
        assert not _events(tmp_path, "import_issue")

        # The reviewer sees the diff, the changed files and the task
        first_review = agents.inputs("reviewer")[1]
        assert "+def simulate_deal" in first_review["diff"]
        assert set(first_review["changed_files"]) == {"simulate_deal.py", DEAL_TEST}
        assert first_review["task"]["task_id"] == "t3"

        # Feedback is the real failure and only this task's review
        assert "AttributeError" in inputs[4]["failing_tests"]
        assert [i["title"] for i in inputs[4]["review_feedback"]] == ["Incorrect Mock Import"]

        # The identical patch: no reviewer call, the previous review stands
        assert agents.count("reviewer") == 5
        assert [e["streak"] for e in _events(tmp_path, "noop_patch")] == [1]
        assert _artifact(tmp_path, "review_report_004.json")["synthesized"] is True
        assert inputs[6]["previous_attempt"]["no_changes"] is True
        assert inputs[6]["_temperature"] == config.pipeline.retry_temperature

        # The repeated review (reworded only in punctuation): one ruling, one directed try
        assert agents.count("director") == 1
        [ruling_input] = agents.inputs("director")
        assert [c["title"] for c in ruling_input["conflicts"]] == [PRINT_CALLS + "."]
        assert "14 != 12" in ruling_input["failing_tests"]
        assert ruling_input["changed_files"]["simulate_deal.py"].count("lines=14") == 1
        directed = inputs[7]
        assert directed["director_guidance"] == [RULING]
        assert directed["review_feedback"][0]["fix_target"] == "test"

        # Converged: both tasks approved, t1's tests intact, no stall
        assert state["status"] == "approved"
        assert state["iteration"] == 6
        assert state["_completed_tasks"] == ["t1", "t3"]
        assert set(CARD_IDS) <= set(state["_accepted_test_ids"])
        assert not _events(tmp_path, "tests_removed")
        assert not _events(tmp_path, "stall_detected")
        summary = _artifact(tmp_path, "summary.json")
        assert [
            (row["task_id"], row["status"], row["iterations"]) for row in summary["task_outcomes"]
        ] == [
            ("t1", "approved", 1),
            ("t3", "approved", 5),
        ]
        metrics, costs = summary["observer_metrics"], summary["cost_breakdown"]
        assert metrics["agent_calls"] == costs["total_calls"] == 15
        # The carried-forward review of the no-op attempt (4) is no reviewer
        # verdict; only the reviewer really repeating itself (5) is flagged.
        assert [m["iteration"] for m in _messages(state, "stuck_detected")] == [5]
        assert metrics["stalls_detected"] == 1


# --- Fix round: gates, arbitration and re-planning -------------------------------

COLLECTION_ERROR_OUTPUT = (
    "============================= test session starts =============================\n"
    "collected 6 items / 1 error\n\n"
    "=================================== ERRORS ====================================\n"
    "______________________ ERROR collecting tests/test_t2.py ______________________\n"
    "ImportError while importing test module 'tests/test_t2.py'.\n"
    "E   ModuleNotFoundError: No module named 'deal'\n"
    "=========================== short test summary info ===========================\n"
    "ERROR tests/test_t2.py\n"
    "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!!\n"
    "=============================== 1 error in 0.12s ==============================\n"
)


class TestRegressionGateHolds:
    """#70: the test regression gate cannot be bypassed or misfire."""

    _resumed_at_t2 = TestRegressionGuard._resumed_at_t2
    FILE = TestRegressionGuard.FILE
    SIX = TestRegressionGuard.SIX
    TWO = TestRegressionGuard.TWO

    async def test_a_collection_error_elsewhere_removes_no_test(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(
            _Agents(
                _plan("t1", "t2"),
                implement=lambda d, n: {"files": {"tests/test_t2.py": "from deal import x\n"}},
            )
        )
        harness.sandbox.reports = lambda n, p: {
            "passed": False,
            "total": 0,
            "failures": 1,
            "stdout": COLLECTION_ERROR_OUTPUT,
            "stderr": "",
            "failed_tests": ["tests/test_t2.py"],
            "results": [{"name": "tests/test_t2.py", "passed": False}],
        }

        state = await _pipeline(tmp_path, _config(max_iterations=1)).execute(
            "build it", initial_state=self._resumed_at_t2(tmp_path)
        )

        assert _events(tmp_path, "tests_removed") == []
        assert state["_removed_tests"] == []
        review = _artifact(tmp_path, "review_report_002.json")
        assert not any(i["title"].startswith("Existing tests removed") for i in review["issues"])

    async def test_a_director_ruling_never_approves_over_removed_tests(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        gate_title = f"Existing tests removed: {self.FILE}"
        agents = harness.use(
            _Agents(
                _plan("t1", "t2"),
                implement=lambda d, n: {"files": {self.FILE: self.TWO}},
                review=lambda d, n: _reject(_issue("major", gate_title)),
                direct=lambda d, n: {"resolutions": [{"title": gate_title, "decision": "ACCEPT"}]},
            )
        )
        harness.sandbox.reports = lambda n, p: {**PASSED, "total": 2}

        state = await _pipeline(tmp_path, _config(max_iterations=3)).execute(
            "build it", initial_state=self._resumed_at_t2(tmp_path)
        )

        assert agents.count("director") == 1
        [resolved] = _events(tmp_path, "arbitration_resolved")
        assert resolved["iteration"] == 3
        assert all(not e["accepted"] for e in _events(tmp_path, "review_saved"))
        # The ruling neither completes t2 nor ends the run: t2 goes on until it stalls.
        assert agents.task_ids() == ["t2", "t2", "t2"]
        assert state["_task_outcomes"]["t2"]["status"] == "stalled"
        assert state["_completed_tasks"] == ["t1"]
        assert state["status"] == "partial"

    async def test_a_restored_approval_never_finishes_over_removed_tests(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        pipeline = _pipeline(tmp_path)
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        state = _new_state("r", tmp_path)
        state.update(
            all_tasks=_plan("t1")["tasks"],
            current_task=_plan("t1")["tasks"][0],
            is_approved=True,
            _removed_tests=[{"file": self.FILE, "removed": ["TestDeck::test_deal"]}],
        )

        pipeline._resume_after_restore(state, store)

        assert state["is_approved"] is False
        assert state["status"] == "running"
        assert state["_completed_tasks"] == []


class TestArbitrationFixes:
    """#74: what the director sees, what is saved, and how often it is asked."""

    @staticmethod
    def _recurring(n: int, p: dict[str, Any]) -> dict[str, Any]:
        return TestArbitration._recurring(n, p)

    async def test_the_director_sees_the_previous_failure_and_the_ruling_is_saved(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(
            _Agents(
                _plan("t1"),
                review=lambda d, n: _varying_major(n),
                direct=lambda d, n: {"resolutions": [TestArbitration.RULING]},
            )
        )
        harness.sandbox.reports = lambda n, p: _failed_run(
            "tests/test_t1.py::test_x", f"AssertionError: {n + 10} != {n + 8}"
        )

        await _pipeline(tmp_path, _config(max_iterations=6)).execute("build it")

        [ruling_input] = agents.inputs("director")
        current, _, previous = ruling_input["failing_tests"].partition(
            graph._PREVIOUS_FAILURE_LABEL
        )
        assert "AssertionError: 12 != 10" in current
        assert "AssertionError: 11 != 9" in previous
        assert len(ruling_input["failing_tests"]) <= graph.MAX_FAILURE_EXCERPT
        assert ruling_input["test_evidence"] == "failed"
        saved = _artifact(tmp_path, "arbitration_002.json")
        assert saved["resolutions"][0]["decision"] == "REVISE"
        assert saved["task_id"] == "t1"

    async def test_gate_only_issues_still_let_a_recurring_failure_be_arbitrated(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(_plan("t1")))
        pipeline = _pipeline(tmp_path)
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        state = _new_state("r", tmp_path)
        state["_security_blockers"] = [
            {
                "severity": "blocker",
                "title": "AWS key",
                "details": "d",
                "file": "a.py",
                "source": "s",
            }
        ]
        [gate] = pipeline._gate_issues(state)
        signature = "tests/test_t1.py::test_x - AssertionError: B"
        state.update(
            current_task=_task("t1"),
            _task_iteration=2,
            review_report=_reject(gate),
            previous_review=_reject(gate),
            _failure_signatures=[signature, signature],
        )

        await pipeline._maybe_arbitrate(state, graph.CostTracker(), store)

        [ruling_input] = agents.inputs("director")
        assert [c["title"] for c in ruling_input["conflicts"]] == ["Recurring test failure"]

    async def test_title_churn_asks_the_director_once_and_an_unmatched_revise_grants_nothing(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        titles = ["issue a", "issue b", "issue c", "issue d", "issue e"]

        def review(data: dict[str, Any], n: int) -> dict[str, Any]:
            # Four of five majors, a different one missing each time (75% overlap)
            return _reject(*(_issue("major", t) for i, t in enumerate(titles) if i != n % 5))

        agents = harness.use(
            _Agents(
                _plan("t1"),
                review=review,
                direct=lambda d, n: {
                    "resolutions": [{"title": "something else", "decision": "REVISE"}]
                },
            )
        )
        harness.sandbox.reports = lambda n, p: {**PASSED, "total": 3}

        state = await _pipeline(tmp_path, _config(CLOUD, max_iterations=7)).execute("build it")

        assert agents.count("director") == 1
        assert _events(tmp_path, "arbitration_resolved")[0]["directed_attempt"] is False
        assert state["status"] == "stalled"
        assert state["_task_outcomes"]["t1"] == {
            "status": "stalled",
            "iterations": 3,
            "reason": "recurring_issues",
        }

    async def test_an_accepting_ruling_is_what_the_saved_review_shows(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(
            _Agents(
                _plan("t1"),
                review=lambda d, n: _reject(_issue("major", "Use a dataclass")),
                direct=lambda d, n: {
                    "resolutions": [{"title": "use a dataclass", "decision": "ACCEPT"}]
                },
            )
        )
        harness.sandbox.reports = lambda n, p: {**PASSED, "total": 3}

        state = await _pipeline(tmp_path).execute("build it")

        assert state["status"] == "approved"
        saved = _events(tmp_path, "review_saved")[-1]
        assert (saved["iteration"], saved["accepted"]) == (2, True)
        review = _artifact(tmp_path, "review_report_002.json")
        assert review["issues"] == []
        assert review["acceptance_met"] is True


class TestArchitectStep:
    """#78/#55: the architect step on its own."""

    RUN_PLAN: ClassVar[dict[str, Any]] = {
        "summary": "Card game",
        "tasks": [
            _task("t1", ["card_deck.py"], description="Create Card and Deck."),
            _task("t2", ["card_deck.py"], depends_on=["t1"], description="Shuffle and deal."),
            _task("t3", ["simulate_deal.py", "test_simulate_deal.py"], depends_on=["t2"]),
        ],
        "acceptance": {"criteria": ["works"]},
    }

    async def test_a_normalized_plan_is_announced_everywhere(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(_Agents(self.RUN_PLAN))
        steps: list[tuple[str, dict[str, Any]]] = []
        pipeline = _pipeline(tmp_path, step_callback=lambda s, d: steps.append((s, d)))
        state = _new_state("cards", tmp_path)
        state["file_tree"] = [".gitignore"]

        state = await pipeline._run_architect(state, object(), graph.CostTracker())  # type: ignore[arg-type]

        waves = [task["task_id"] for wave in state["execution_waves"] for task in wave]
        assert waves == ["t1", "t3"]
        assert state["current_task"]["task_id"] == "t1"
        [message] = _messages(state, "plan_normalized")
        assert [data for step, data in steps if step == "plan_normalized"] == [
            {"warnings": message["warnings"]}
        ]

    @pytest.mark.parametrize("plan_name", ["mock", "clean"])
    async def test_a_clean_plan_is_not_reported_as_normalized(
        self, tmp_path: Path, harness: SimpleNamespace, plan_name: str
    ) -> None:
        from delegatron.agents.providers.mock import MOCK_WORK_PLAN

        clean = _plan(_task("t1", ["app.py", "tests/test_app.py"]))
        harness.use(_Agents(copy.deepcopy(MOCK_WORK_PLAN) if plan_name == "mock" else clean))
        steps: list[str] = []
        pipeline = _pipeline(tmp_path, step_callback=lambda s, d: steps.append(s))
        state = _new_state("app", tmp_path)

        state = await pipeline._run_architect(state, object(), graph.CostTracker())  # type: ignore[arg-type]

        assert _messages(state, "plan_normalized") == []
        assert "plan_normalized" not in steps

    async def test_a_plan_without_tasks_ends_the_run(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents({"summary": "nothing to do", "tasks": []}))

        state = await _pipeline(tmp_path).execute("build it")

        assert state["status"] == "error"
        assert state["error"] == "Architect produced a plan with no tasks"
        assert agents.count("implementer") == 0
        assert _events(tmp_path, "architect_error")[0]["error"] == state["error"]
        assert _artifact(tmp_path, "summary.json")["status"] == "error"


class TestReplanFixes:
    """#79 and the re-plan control-flow findings."""

    def _replan_state(
        self, tmp_path: Path, plan: dict[str, Any], completed: list[str], current: int
    ) -> Any:
        state = _new_state("r", tmp_path)
        state.update(
            plan=plan,
            all_tasks=plan["tasks"],
            current_task=plan["tasks"][current],
            _completed_tasks=completed,
            iteration=4,
        )
        return state

    def test_a_plan_with_new_ids_drops_the_completed_tasks(self, tmp_path: Path) -> None:
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        old = _plan("t1", "t2", "t3")
        state = self._replan_state(tmp_path, old, ["t1", "t2"], 2)

        _pipeline(tmp_path)._install_replan(state, _plan("a", "b"), store)

        [replanned] = _events(tmp_path, "replanned")
        assert replanned["dropped_completed"] == ["t1", "t2"]
        assert state["_completed_tasks"] == []
        assert state["current_task"]["task_id"] == "a"

    def test_pending_waves_follow_the_dependencies(self, tmp_path: Path) -> None:
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        new = _plan(
            "t1",
            _task("t2", depends_on=["t1"]),
            _task("t3", depends_on=["t2"]),
            _task("t4", depends_on=["t3"]),
        )
        state = self._replan_state(tmp_path, _plan("t1", "t2", "t3"), ["t1", "t2"], 2)

        _pipeline(tmp_path)._install_replan(state, new, store)

        assert [[t["task_id"] for t in wave] for wave in state["execution_waves"]] == [
            ["t3"],
            ["t4"],
        ]
        assert state["current_task"]["task_id"] == "t3"
        assert state["_completed_tasks"] == ["t1", "t2"]

    def test_a_reset_task_never_carries_a_replan_request(self, tmp_path: Path) -> None:
        state = self._replan_state(tmp_path, _plan("t1", "t2"), [], 0)
        state["_replan"] = True

        graph._reset_for_next_task(state, state["all_tasks"][1])

        assert state["_replan"] is False

    async def test_a_shorter_plan_gets_a_full_budget_for_its_work(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        plan = _plan("t1", "t2", "t3")
        agents = _Agents(plan, architect=lambda d, n: plan if n == 1 else _plan("t9"))
        reviews = iter(
            [APPROVE, APPROVE, _reject(_arch_issue()), _varying_major(1), _varying_major(2)]
        )
        agents.review = lambda d, n: next(reviews, APPROVE)
        harness.use(agents)

        state = await _pipeline(tmp_path, _config(max_iterations=3)).execute("build it")

        assert agents.task_ids() == ["t1", "t2", "t3", "t9", "t9", "t9"]
        assert state["status"] == "approved"
        assert state["_task_outcomes"]["t9"]["iterations"] == 3
        assert _artifact(tmp_path, "work_plan_r1.json")["tasks"][0]["task_id"] == "t9"

    async def test_a_replan_asked_on_the_last_attempt_replans_that_task(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        plan = _plan("t1", "t2")
        agents = _Agents(plan)
        reviews = iter([_varying_major(1), _reject(_arch_issue()), APPROVE, APPROVE])
        agents.review = lambda d, n: next(reviews)
        harness.use(agents)

        state = await _pipeline(tmp_path, _config(max_iterations=2)).execute("build it")

        assert agents.count("architect") == 2
        replan_input = agents.inputs("architect")[1]
        assert replan_input["current_task"] == {"task_id": "t1", "title": "Title t1"}
        assert [i["title"] for i in replan_input["review_feedback"]] == ["Split the task"]
        assert _events(tmp_path, "replanned")[0]["current_task"] == "t1"
        assert agents.task_ids() == ["t1", "t1", "t1", "t2"]
        assert state["status"] == "approved"
        assert _events(tmp_path, "task_skipped") == []

    async def test_a_rejected_replan_keeps_the_current_plan(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        plan = _plan("t1", "t2")
        agents = _Agents(plan, architect=lambda d, n: plan if n == 1 else _plan("t9"))
        reviews = iter([APPROVE, _reject(_arch_issue()), APPROVE])
        agents.review = lambda d, n: next(reviews)
        harness.use(agents)
        asked: list[list[str]] = []

        def approve_only_the_first(candidate: dict[str, Any]) -> bool:
            asked.append([t["task_id"] for t in candidate["tasks"]])
            return len(asked) == 1

        pipeline = _pipeline(
            tmp_path, _config(interactive=True), approval_callback=approve_only_the_first
        )

        state = await pipeline.execute("build it")

        assert asked == [["t1", "t2"], ["t9"]]
        [rejected] = _events(tmp_path, "replan_rejected")
        assert (rejected["reason"], rejected["task_ids"]) == ("user", ["t9"])
        assert _events(tmp_path, "replanned") == []
        assert [t["task_id"] for t in state["plan"]["tasks"]] == ["t1", "t2"]
        assert agents.task_ids() == ["t1", "t2", "t2"]
        assert state["status"] == "approved"
        assert state["_completed_tasks"] == ["t1", "t2"]


class TestRegressedTests:
    """A skipped task that broke an approved task's test leaves the run unapproved."""

    TEST_ONE = "tests/test_t1.py::test_one"

    def _report(self, passed: bool) -> dict[str, Any]:
        if passed:
            return {**PASSED, "results": [{"name": self.TEST_ONE, "passed": True}]}
        return {
            "passed": False,
            "total": 1,
            "failures": 1,
            "failed_tests": [self.TEST_ONE],
            "results": [{"name": self.TEST_ONE, "passed": False}],
        }

    async def test_the_regression_is_reported_and_blocks_run_approval(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        files = {
            "t1": {"tests/test_t1.py": "def test_one():\n    pass\n", "t1.py": "x = 1\n"},
            "t2": {"t1.py": "x = broken\n"},
            "t3": {"t3.py": "y = 1\n"},
        }
        review = _review_by_task({"t1": APPROVE, "t2": _reject(_issue("major")), "t3": APPROVE})
        agents = _Agents(
            _plan("t1", "t2", "t3"), implement=lambda d, n: {"files": files[_task_id(d)]}
        )
        agents.review = review
        _tracking(agents, review)
        harness.use(agents)
        harness.sandbox.reports = lambda n, p: self._report(passed=n == 1)

        state = await _pipeline(tmp_path, _config(max_iterations=1)).execute("build it")

        assert agents.task_ids() == ["t1", "t2", "t3"]
        assert state["_completed_tasks"] == ["t1", "t3"]
        [skipped] = _events(tmp_path, "task_skipped")
        assert skipped["regressed_tests"] == [self.TEST_ONE]
        summary = _artifact(tmp_path, "summary.json")
        assert summary["regressed_tests"] == {self.TEST_ONE: "t2"}
        t1 = next(row for row in summary["task_outcomes"] if row["task_id"] == "t1")
        assert t1["regressed_tests"] == [self.TEST_ONE]
        assert summary["status"] == "partial"

    def test_an_otherwise_approved_run_is_partial_while_a_test_regressed(
        self, tmp_path: Path
    ) -> None:
        state = _new_state("r", tmp_path)
        state.update(
            status="approved",
            is_approved=True,
            all_tasks=_plan("t1", "t9")["tasks"],
            _completed_tasks=["t1", "t9"],
            _accepted_test_ids=[self.TEST_ONE],
            _known_failures={self.TEST_ONE: "t2"},
        )

        graph._settle_run_status(state)

        assert (state["status"], state["is_approved"]) == ("partial", False)


class TestSiblingEvents:
    async def test_a_parallel_siblings_events_are_tagged_and_its_failure_is_not_the_current_tasks(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        """Parallel mode: a sibling's events name its task; its failed call is a fallback only."""
        _git_repo(tmp_path)

        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            if _task_id(data) == "t2" and n <= 2:
                raise ValueError("bad JSON")
            return {"files": {f"{_task_id(data)}_impl.py": "x = 1\n"}}

        harness.use(_Agents(_plan("t1", "t2"), implement=implement))

        state = await _pipeline(tmp_path, _config(parallel_tasks=True)).execute("build it")

        errors = _events(tmp_path, "implementer_error")
        assert all(e.get("task_id") != "t2" or not e.get("parallel") for e in errors)
        assert not [e for e in errors if e["iteration"] == 1]
        [fallback] = _events(tmp_path, "parallel_task_fallback")
        assert fallback["task_id"] == "t2"
        assert state["status"] == "approved"
        assert state["_completed_tasks"] == ["t1", "t2"]

    def test_sibling_events_are_tagged(self) -> None:
        received: list[dict[str, Any]] = []
        sink = graph._SiblingEvents(SimpleNamespace(append_event=received.append), "t2")

        sink.append_event({"event": "patch_entry_blocked", "iteration": 1})
        sink.append_event({"event": "implementer_error", "iteration": 1})

        assert received == [
            {"event": "patch_entry_blocked", "iteration": 1, "task_id": "t2", "parallel": True}
        ]


# --- Fix round (GRAPH-B): selection, parallel mode, gates, known failures ------------


def _per_task(script: Callable[[str, int], dict[str, Any]]) -> Script:
    """An implementer script called with the task id and that task's call number."""
    calls: dict[str, int] = {}

    def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
        task_id = _task_id(data)
        calls[task_id] = calls.get(task_id, 0) + 1
        return script(task_id, calls[task_id])

    return implement


def _impl_file(task_id: str) -> dict[str, Any]:
    return {"files": {f"{task_id}_impl.py": "x = 1\n"}}


class TestTaskSelectionFixes:
    """#99: a restored parallel-shaped state, selector safety, old duplicate-id plans."""

    async def test_a_parallel_shaped_checkpoint_runs_every_task_once(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        plan = _plan("t1", "t2", _task("t3", depends_on=["t1", "t2"]))
        tasks = plan["tasks"]
        saved = {
            "request": "build it",
            "plan": plan,
            "all_tasks": tasks,
            "execution_waves": [[tasks[0], tasks[1]], [tasks[2]]],
            "current_task": tasks[2],
            "_completed_tasks": [],
            "iteration": 1,
            "messages": [],
        }
        agents = harness.use(_Agents(plan))

        state = await _pipeline(tmp_path).execute("build it", initial_state=saved)

        assert sorted(agents.task_ids()) == ["t1", "t2", "t3"]
        assert state["iteration"] - 1 <= len(tasks)
        # each task completes exactly once; the restored order is not part of the contract
        assert sorted(state["_completed_tasks"]) == ["t1", "t2", "t3"]
        assert state["status"] == "approved"

    def test_the_selector_never_returns_a_finished_task(self) -> None:
        tasks = [
            {"task_id": "a", "depends_on": ["b"]},
            {"task_id": "b", "depends_on": ["a"]},  # a cycle
            {"task_id": "c", "depends_on": ["a", "missing"]},
            {"task_id": "d", "depends_on": []},
        ]
        ids = [task["task_id"] for task in tasks]
        for size in range(len(ids) + 1):
            for finished in itertools.combinations(ids, size):
                chosen = _next_pending_task(tasks, set(finished))
                if len(finished) == len(ids):
                    assert chosen is None
                else:
                    assert chosen is not None
                    assert chosen["task_id"] not in finished

    async def test_a_selector_returning_the_finished_task_never_loops(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(graph, "_next_pending_task", lambda tasks, finished: tasks[0])
        agents = harness.use(_Agents(_plan("t1", "t2")))

        state = await _pipeline(tmp_path).execute("build it")

        assert agents.task_ids() == ["t1"]
        assert state["_completed_tasks"] == ["t1"]
        assert state["status"] == "partial"

    async def test_an_old_checkpoint_with_duplicate_ids_runs_the_twin_too(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        tasks = [
            {**_task("t1"), "title": "first"},
            {**_task("t1"), "title": "twin"},
            _task("t2"),
        ]
        plan = _plan(*tasks)
        saved = {
            "request": "build it",
            "plan": plan,
            "all_tasks": copy.deepcopy(tasks),
            "current_task": copy.deepcopy(tasks[1]),
            "_completed_tasks": ["t1"],
            "iteration": 2,
            "messages": [],
        }
        agents = harness.use(_Agents(plan))

        state = await _pipeline(tmp_path).execute("build it", initial_state=saved)

        titles = [data["task"]["title"] for data in agents.inputs("implementer")]
        assert titles == ["twin", "Title t2"]
        ids = [task["task_id"] for task in state["all_tasks"]]
        assert len(set(ids)) == 3
        assert state["status"] == "approved"


class TestParallelWaves:
    """#100: parallel mode keeps the sequential bookkeeping and falls back per task."""

    async def test_a_second_wave_runs_after_the_first_and_every_task_once(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        _git_repo(tmp_path)
        plan = _plan("t1", "t2", _task("t3", depends_on=["t1", "t2"]))
        seen: dict[str, set[str]] = {}

        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            seen[_task_id(data)] = set(data.get("existing_files") or {})
            return _impl_file(_task_id(data))

        agents = harness.use(_Agents(plan, implement=implement))

        state = await _pipeline(tmp_path, _config(parallel_tasks=True)).execute("build it")

        assert sorted(agents.task_ids()) == ["t1", "t2", "t3"]
        assert state["_completed_tasks"] == ["t1", "t2", "t3"]
        assert state["status"] == "approved"
        assert {"t1_impl.py", "t2_impl.py"} <= seen["t3"]
        tracked = set(_git(tmp_path, "ls-files").split())
        assert {"t1_impl.py", "t2_impl.py", "t3_impl.py"} <= tracked

    async def test_a_repository_without_commits_still_implements_every_task(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        _git(tmp_path, "init", "-q", "-b", "main")
        agents = harness.use(
            _Agents(_plan("t1", "t2"), implement=lambda d, n: _impl_file(_task_id(d)))
        )

        state = await _pipeline(tmp_path, _config(parallel_tasks=True)).execute("build it")

        assert sorted(agents.task_ids()) == ["t1", "t2"]
        assert state["_completed_tasks"] == ["t1", "t2"]
        assert state["status"] == "approved"
        # The run branch starts from a fresh root commit, so worktrees work.
        used = [m["pregenerated"] for m in _messages(state, "implementer_complete")]
        assert used == [False, True]

    @pytest.mark.parametrize("failure", ["raises", "blocked"])
    async def test_a_sibling_that_fails_or_is_blocked_runs_live_later(
        self, tmp_path: Path, harness: SimpleNamespace, failure: str
    ) -> None:
        _git_repo(tmp_path)

        def script(task_id: str, call: int) -> dict[str, Any]:
            if task_id == "t2" and call == 1:
                if failure == "raises":
                    raise ValueError("bad JSON")
                return {"status": "blocked", "questions": ["Which API?"], "files": {}}
            return _impl_file(task_id)

        agents = harness.use(_Agents(_plan("t1", "t2"), implement=_per_task(script)))

        state = await _pipeline(tmp_path, _config(parallel_tasks=True)).execute("build it")

        [fallback] = _events(tmp_path, "parallel_task_fallback")
        assert fallback["task_id"] == "t2"
        assert ("bad JSON" if failure == "raises" else "Which API?") in fallback["error"]
        assert agents.task_ids().count("t2") == 2
        assert state["_completed_tasks"] == ["t1", "t2"]
        assert state["status"] == "approved"
        used = [m["pregenerated"] for m in _messages(state, "implementer_complete")]
        assert used == [False, False]
        errors = _artifact(tmp_path, "summary.json")["observer_metrics"]["errors"]
        assert errors == (1 if failure == "raises" else 0)

    def test_a_sibling_starts_without_the_current_tasks_patch(self, tmp_path: Path) -> None:
        state = _new_state("r", tmp_path)
        state["current_patch"] = {"files": {"t1.py": "x\n"}}

        sibling = Pipeline._sibling_state(state, _task("t2"))

        assert sibling["current_patch"] is None
        assert sibling["current_task"]["task_id"] == "t2"
        assert state["current_patch"] == {"files": {"t1.py": "x\n"}}

    def test_a_diff_only_parallel_patch_that_went_stale_is_regenerated(
        self, tmp_path: Path
    ) -> None:
        diff = "--- a/shared.py\n+++ b/shared.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
        state = _new_state("r", tmp_path)
        state.update(
            iteration=3,
            current_task=_task("t2"),
            _file_history={"shared.py": {"task_id": "t1", "iteration": 2}},
            _parallel_patches={
                "t2": {
                    "patch": {"files": {}, "diff_unified": diff},
                    "blocked_paths": [],
                    "iteration": 1,
                }
            },
        )

        assert _pipeline(tmp_path)._take_parallel_patch(state, None) is None

        [conflict] = _messages(state, "parallel_patch_conflict")
        assert conflict["paths"] == {"shared.py": "t1"}
        assert [b["file"] for b in state["_blocked_paths"]] == ["shared.py"]


class TestConfigOnlyGateFixes:
    """#102: the gate holds until the task has written source, sequential or parallel."""

    async def test_config_only_patches_are_turned_back_until_source_is_written(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        patches = {
            1: {"files": {"requirements.txt": "requests\n"}},
            2: {"files": {"requirements.txt": "requests==2\n"}},
            3: {"files": {"t1.py": "import requests\n"}},
            4: {"files": {"requirements.txt": "requests==3\n"}},
        }
        reviews = {1: _reject(_issue("minor", "pin it")), 2: APPROVE}
        agents = harness.use(
            _Agents(
                _plan("t1"),
                implement=lambda d, n: patches[n],
                review=lambda d, n: reviews[n],
            )
        )

        state = await _pipeline(tmp_path, _config(max_iterations=5)).execute("build it")

        assert agents.count("implementer") == 4
        assert len(harness.sandbox.patches) == 2  # attempts 3 and 4
        assert [e["iteration"] for e in _events(tmp_path, "config_only_patch")] == [1, 2]
        skipped = _artifact(tmp_path, "test_report_002.json")
        assert skipped["skipped_reason"] == "config_only_patch"
        assert (tmp_path / "requirements.txt").read_text(encoding="utf-8") == "requests==3\n"
        assert state["status"] == "approved"

    async def test_a_task_without_planned_outputs_must_write_source(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        patches = {1: {"files": {"setup.cfg": "[x]\n"}}, 2: {"files": {"app.py": "x = 1\n"}}}
        agents = harness.use(_Agents(_plan(_task("t1", [])), implement=lambda d, n: patches[n]))

        state = await _pipeline(tmp_path).execute("build it")

        assert agents.count("implementer") == 2
        assert len(harness.sandbox.patches) == 1
        assert _events(tmp_path, "config_only_patch")
        assert state["status"] == "approved"

    async def test_a_parallel_config_only_patch_is_gated_like_a_live_one(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        _git_repo(tmp_path)

        def script(task_id: str, call: int) -> dict[str, Any]:
            if task_id == "t2" and call == 1:
                return {"files": {"requirements.txt": "requests\n"}}
            return _impl_file(task_id)

        agents = harness.use(_Agents(_plan("t1", "t2"), implement=_per_task(script)))

        state = await _pipeline(tmp_path, _config(parallel_tasks=True)).execute("build it")

        [gated] = _events(tmp_path, "config_only_patch")
        assert gated["iteration"] == 2
        assert len(harness.sandbox.patches) == 2  # t1, then t2's live retry
        assert agents.count("reviewer") == 2
        retry = [d for d in agents.inputs("implementer") if _task_id(d) == "t2"][-1]
        assert [i["title"] for i in retry["review_feedback"]] == ["No source code produced"]
        assert _artifact(tmp_path, "state_002.json")["previous_review"] is None
        assert not (tmp_path / "requirements.txt").exists()
        assert state["_completed_tasks"] == ["t1", "t2"]


class TestReviewVerdictEvents:
    """#103/#101: verdict events and feedback stay consistent with the gates."""

    async def test_a_blocker_with_passing_tests_never_completes_a_task(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        review = _reject(_issue("blocker", "Crashes on empty input"))
        agents = harness.use(_Agents(_plan("t1", "t2"), review=lambda d, n: review))
        harness.sandbox.reports = lambda n, p: {**PASSED, "stderr": f"run {n}"}

        state = await _pipeline(tmp_path, _config(max_iterations=2)).execute("build it")

        assert state["_completed_tasks"] == []
        assert agents.task_ids()[:2] == ["t1", "t1"]
        assert not _events(tmp_path, "emcs_override")

    async def test_a_skipped_task_is_announced_once_through_a_persisting_callback(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        review = _review_by_task({"t1": _varying_major, "t2": APPROVE})
        agents = _Agents(_plan("t1", "t2"), review=review)
        _tracking(agents, review)
        harness.use(agents)
        server_store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        steps: list[str] = []

        def persist(step: str, data: dict[str, Any]) -> None:
            steps.append(step)
            server_store.append_event({"event": step, **data})

        pipeline = _pipeline(
            tmp_path, _config(max_iterations=1), step_callback=persist, step_callback_persists=True
        )

        await pipeline.execute("build it")

        [skipped] = _events(tmp_path, "task_skipped")
        assert skipped["skipped_task"] == "t1"
        assert steps.count("task_skipped") == 1

    async def test_untested_code_the_reviewer_accepts_scores_acceptable(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(_Agents(_plan("t1")))
        harness.sandbox.reports = lambda n, p: {"passed": True, "total": 0, "stdout": ""}

        state = await _pipeline(tmp_path).execute("build it")

        [score] = _events(tmp_path, "emcs_score")
        assert score["test_evidence"] == "untested"
        assert score["acceptable"] is True
        assert state["status"] == "approved"

    async def test_the_reasoning_event_names_an_emcs_override(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        review = _reject(_issue("minor", "a"))
        harness.use(_Agents(_plan("t1"), review=lambda d, n: review))
        harness.sandbox.reports = lambda n, p: {**PASSED, "total": 5}

        state = await _pipeline(tmp_path, _config(emcs_threshold=0.6)).execute("build it")

        [reasoning] = _messages(state, "reviewer_reasoning")
        assert reasoning["emcs_override"] is True
        assert reasoning["accepted"] is True

    def test_an_issue_free_rejection_reaches_the_implementer_beside_gate_issues(
        self, tmp_path: Path
    ) -> None:
        state = _new_state("r", tmp_path)
        state["_removed_tests"] = [
            {"file": "tests/test_a.py", "removed": ["test_x"], "owner_task": "t1"}
        ]
        gate = Pipeline._gate_issues(state)
        state["review_report"] = {
            "summary": "Does not match the request",
            "acceptance_met": False,
            "issues": list(gate),
        }

        feedback = Pipeline._implementer_feedback(state)

        assert [i["title"] for i in feedback] == [
            gate[0]["title"],
            "Reviewer rejected without itemized issues",
        ]
        assert feedback[1]["details"] == "Does not match the request"

    def test_a_skipped_sandbox_leaves_no_failure_behind(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        state = _new_state("r", tmp_path)
        state["test_report"] = {
            "passed": False,
            "total": 0,
            "skipped_reason": "non_testable_language",
        }
        graph.logger.addHandler(caplog.handler)
        try:
            with caplog.at_level("WARNING", logger=graph.logger.name):
                assert graph._record_known_failures(state, "t1") == []
        finally:
            graph.logger.removeHandler(caplog.handler)

        assert "names no test" not in caplog.text
        assert state["_known_failures"] == {}


class TestRunOutcomeSummary:
    async def test_tasks_a_limit_stopped_are_listed_as_skipped(
        self, tmp_path: Path, harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#101: summary ``skipped_tasks`` lists every task the run did not deliver."""
        clock = {"now": 0.0}
        monkeypatch.setattr(graph, "time", SimpleNamespace(monotonic=lambda: clock["now"]))

        def review(data: dict[str, Any], n: int) -> dict[str, Any]:
            clock["now"] = 10 * 60.0
            return _reject(_issue("major"))

        harness.use(_Agents(_plan("t1", "t2"), review=review))

        state = await _pipeline(tmp_path).execute("build it")

        summary = _artifact(tmp_path, "summary.json")
        assert state["status"] == "exhausted"
        assert [(r["task_id"], r["status"]) for r in summary["task_outcomes"]] == [
            ("t1", "exhausted"),
            ("t2", "skipped"),
        ]
        assert summary["skipped_tasks"] == ["t1", "t2"]


class TestKnownFailureFixes:
    """#104: what the implementer sees of known failures; fixed ones are on record."""

    OLD = "tests/test_a.py::test_x"
    NEW = "tests/test_b.py::test_y"

    def _failing(self, *ids: str) -> dict[str, Any]:
        return {"passed": False, "total": 3, "failures": len(ids), "failed_tests": list(ids)}

    async def test_the_retry_after_a_new_failure_lists_only_it_as_failing(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        agents = harness.use(_Agents(_plan("t1", "t2")))
        harness.sandbox.reports = lambda n, p: (
            self._failing(self.OLD) if n <= 2 else self._failing(self.OLD, self.NEW)
        )

        await _pipeline(tmp_path, _config(max_iterations=2)).execute("build it")

        assert agents.task_ids() == ["t1", "t1", "t2", "t2"]
        retry = agents.inputs("implementer")[3]
        assert json.loads(retry["failing_tests"])["failed_tests"] == [self.NEW]
        assert retry["baseline_failures"] == {self.OLD: "t1"}

    async def test_a_known_failure_that_passes_later_is_listed_as_fixed(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(_Agents(_plan("t1", "t2")))
        fixed = {**PASSED, "total": 3, "results": [{"name": self.OLD, "passed": True}]}
        harness.sandbox.reports = lambda n, p: self._failing(self.OLD) if n <= 2 else fixed

        await _pipeline(tmp_path, _config(max_iterations=2)).execute("build it")

        summary = _artifact(tmp_path, "summary.json")
        assert summary["baseline_failures"] == {}
        assert summary["fixed_baseline_failures"] == {self.OLD: "t1"}


class TestPreRunBaseline:
    """#104: tests failing before the run never block it; the baseline is taken once."""

    LEGACY = "tests/test_legacy.py::test_x"

    def _legacy_failure(self) -> dict[str, Any]:
        return {"passed": False, "total": 2, "failures": 1, "failed_tests": [self.LEGACY]}

    def _workspace(self, root: Path) -> None:
        (root / "tests").mkdir()
        (root / "tests" / "test_legacy.py").write_text(
            "def test_x():\n    assert False\n", encoding="utf-8"
        )

    async def test_a_legacy_failure_does_not_block_approval(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        self._workspace(tmp_path)
        harness.use(_Agents(_plan("t1")))
        harness.sandbox.reports = lambda n, p: self._legacy_failure()

        state = await _pipeline(tmp_path).execute("build it")

        baseline_call, first = harness.sandbox.patches
        assert baseline_call["files"] == {}
        assert first["files"]
        assert state["status"] == "approved"
        report = _artifact(tmp_path, "test_report_001.json")
        assert (report["passed"], report["task_passed"]) == (False, True)
        [event] = _events(tmp_path, "baseline_tests")
        assert event["pre_existing"] == [self.LEGACY]
        assert _artifact(tmp_path, graph.BASELINE_REPORT_ARTIFACT)["failed_tests"] == [self.LEGACY]
        summary = _artifact(tmp_path, "summary.json")
        assert summary["baseline_failures"] == {self.LEGACY: graph.PRE_EXISTING}

    async def test_a_resume_reuses_the_baseline(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        self._workspace(tmp_path)

        def stop(data: dict[str, Any], n: int) -> dict[str, Any]:
            raise KeyboardInterrupt

        harness.use(_Agents(_plan("t1"), implement=stop))
        harness.sandbox.reports = lambda n, p: self._legacy_failure()
        with pytest.raises(KeyboardInterrupt):
            await _pipeline(tmp_path).execute("build it")
        assert len(harness.sandbox.patches) == 1
        checkpoint = _artifact(tmp_path, "state_001.json")
        assert checkpoint["_baseline_done"] is True

        harness.use(_Agents(_plan("t1")))
        state = await _pipeline(tmp_path).execute("build it", initial_state=checkpoint)

        assert len(harness.sandbox.patches) == 2  # the resumed attempt only
        assert state["status"] == "approved"

    async def test_a_checkpoint_from_before_the_flag_reuses_the_saved_report(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        self._workspace(tmp_path)
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        graph._save_json_artifact(store, graph.BASELINE_REPORT_ARTIFACT, self._legacy_failure())
        plan = _plan("t1")
        saved = {"request": "build it", "plan": plan, "iteration": 0, "messages": []}
        harness.use(_Agents(plan))
        harness.sandbox.reports = lambda n, p: self._legacy_failure()

        state = await _pipeline(tmp_path).execute("build it", initial_state=saved)

        assert len(harness.sandbox.patches) == 1
        assert state["_known_failures"] == {self.LEGACY: graph.PRE_EXISTING}
        assert state["status"] == "approved"

    async def test_a_baseline_on_record_is_never_taken_again(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        self._workspace(tmp_path)
        plan = _plan("t1")
        saved = {
            "request": "build it",
            "plan": plan,
            "iteration": 0,
            "messages": [],
            "_baseline_done": True,
            "_known_failures": {self.LEGACY: graph.PRE_EXISTING},
        }
        harness.use(_Agents(plan))
        harness.sandbox.reports = lambda n, p: self._legacy_failure()

        state = await _pipeline(tmp_path).execute("build it", initial_state=saved)

        assert len(harness.sandbox.patches) == 1
        assert not _events(tmp_path, "baseline_tests")
        assert state["status"] == "approved"

    async def test_disabled_baseline_runs_no_extra_sandbox(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        self._workspace(tmp_path)
        harness.use(_Agents(_plan("t1")))
        harness.sandbox.reports = lambda n, p: self._legacy_failure()

        state = await _pipeline(tmp_path, _config(baseline_tests=False)).execute("build it")

        assert all(patch["files"] for patch in harness.sandbox.patches)
        assert not _events(tmp_path, "baseline_tests")
        assert state["status"] != "approved"


class TestConstraintsSinglePass:
    async def test_a_cloud_implementer_sees_the_constraints(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        """#97: the single-pass implementer call carries them too."""
        agents = harness.use(_Agents(_plan("t1")))
        config = _config(implementer=CLOUD, constraints={"no_modify": [".env"]})

        await _pipeline(tmp_path, config).execute("build it")

        [data] = agents.inputs("implementer")
        assert "mode" not in data["task"]
        assert data["constraints"] == {"no_modify": [".env"]}


# --- Final fix round: control flow (resume, refine, re-plan, removed tests, events) ----

TEST_A = "tests/test_a.py::test_one"


class TestRemovalsBySkippedTasks:
    """Accepted tests a skipped task deleted are excused for later tasks, like its failures."""

    def _implement(self, restore_in_t3: bool = False) -> Script:
        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            task = _task_id(data)
            if task == "t1":
                return {
                    "files": {"a.py": "A = 1\n", "tests/test_a.py": "def test_one():\n    pass\n"}
                }
            if task == "t2":  # a refactor that deletes t1's test module
                return {"files": {"b.py": f"B = {n}\n"}, "delete_files": ["tests/test_a.py"]}
            files = {"c.py": f"C = {n}\n"}
            if restore_in_t3:
                files["tests/test_a.py"] = "def test_one():\n    pass\n"
            return {"files": files}

        return implement

    @staticmethod
    def _report(n: int, patch: dict[str, Any]) -> dict[str, Any]:
        if n == 1:
            return {**PASSED, "results": [{"name": TEST_A, "passed": True}]}
        return {**PASSED, "total": 2}

    async def test_tests_a_skipped_task_deleted_never_block_a_later_task(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        review = _review_by_task({"t1": APPROVE, "t2": _varying_major, "t3": APPROVE})
        agents = _Agents(_plan("t1", "t2", "t3"), implement=self._implement())
        agents.review = review
        _tracking(agents, review)
        harness.use(agents)
        harness.sandbox.reports = self._report

        state = await _pipeline(tmp_path, _config(max_iterations=2)).execute("build it")

        assert agents.task_ids() == ["t1", "t2", "t2", "t3"]
        assert state["_task_outcomes"]["t3"] == {"status": "approved", "iterations": 1}
        assert state["_known_removals"] == {TEST_A: "t2"}
        [skipped] = _events(tmp_path, "task_skipped")
        assert skipped["regressed_tests"] == [TEST_A]
        summary = _artifact(tmp_path, "summary.json")
        assert summary["regressed_tests"] == {TEST_A: "t2"}
        t1 = next(row for row in summary["task_outcomes"] if row["task_id"] == "t1")
        assert t1["regressed_tests"] == [TEST_A]
        assert summary["status"] == "partial"

    async def test_a_later_task_that_brings_them_back_restores_their_protection(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        review = _review_by_task({"t1": APPROVE, "t2": _varying_major, "t3": APPROVE})
        agents = _Agents(_plan("t1", "t2", "t3"), implement=self._implement(restore_in_t3=True))
        agents.review = review
        _tracking(agents, review)
        harness.use(agents)
        harness.sandbox.reports = self._report

        state = await _pipeline(tmp_path, _config(max_iterations=2)).execute("build it")

        assert state["_known_removals"] == {}
        assert _artifact(tmp_path, "summary.json")["regressed_tests"] == {}
        assert TEST_A in state["_accepted_test_ids"]
        # protected again: deleting the file now is a removal of the current task
        (tmp_path / "tests" / "test_a.py").unlink()
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        _pipeline(tmp_path)._check_test_suite(state, store)
        assert [item["removed"] for item in state["_removed_tests"]] == [["test_one"]]

    def test_reopening_the_skipped_task_makes_it_restore_them(self, tmp_path: Path) -> None:
        plan = _plan("t1", "t2")
        state = _new_state("r", tmp_path)
        state.update(
            all_tasks=plan["tasks"],
            current_task=plan["tasks"][1],
            _completed_tasks=["t1"],
            _skipped_tasks=["t2"],
            _accepted_test_ids=[TEST_A],
            _accepted_test_owners={TEST_A: "t1"},
            _known_removals={TEST_A: "t2"},
        )

        graph.restart_current_task(state)

        assert state["_known_removals"] == {}
        assert state["_accepted_test_ids"] == [TEST_A]  # t1's, still protected


class TestResumeOfAPartialRun:
    """A "partial" run's last checkpoint has its approved last task current."""

    async def test_the_approved_task_is_never_rerun_and_the_given_up_one_is(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        review = _review_by_task({"t1": _varying_major, "t2": APPROVE})
        agents = _Agents(_plan("t1", "t2"))
        agents.review = review
        _tracking(agents, review)
        harness.use(agents)
        first = await _pipeline(tmp_path, _config(max_iterations=2)).execute("build it")
        assert first["status"] == "partial"
        saved = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1").load_state()
        assert saved is not None and saved["current_task"]["task_id"] == "t2"

        agents = harness.use(_Agents(_plan("t1", "t2")))
        state = await _pipeline(tmp_path, _config(max_iterations=5)).execute(
            "build it", initial_state=saved
        )

        assert agents.task_ids() == ["t1"]
        [reopened] = _events(tmp_path, "tasks_reopened")
        assert reopened["tasks"] == ["t1"]
        assert sorted(state["_completed_tasks"]) == ["t1", "t2"]
        assert state["_skipped_tasks"] == []
        assert state["status"] == "approved"

    async def test_a_resume_with_every_task_approved_runs_nothing(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        plan = _plan("t1", "t2")
        saved = {
            "request": "build it",
            "plan": plan,
            "all_tasks": plan["tasks"],
            "current_task": plan["tasks"][1],
            "iteration": 2,
            "is_approved": False,
            "_completed_tasks": ["t1", "t2"],
            "_task_outcomes": {
                "t1": {"status": "approved", "iterations": 1},
                "t2": {"status": "approved", "iterations": 1},
            },
        }
        agents = harness.use(_Agents(plan))

        state = await _pipeline(tmp_path).execute("build it", initial_state=saved)

        assert agents.calls == []
        assert (state["status"], state["is_approved"]) == ("approved", True)

    def test_a_task_given_up_is_never_also_completed(self, tmp_path: Path) -> None:
        state = _new_state("r", tmp_path)
        plan = _plan("t1", "t2")
        state.update(
            all_tasks=plan["tasks"], current_task=plan["tasks"][0], _completed_tasks=["t1"]
        )
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")

        _pipeline(tmp_path)._finish_task(state, store, "stalled", reason="no_progress")

        assert state["_completed_tasks"] == []
        assert state["_skipped_tasks"] == ["t1"]


class TestSessionKeys:
    """What only describes one session is not restored by a resume or a refine."""

    async def test_an_approved_resume_reports_no_old_limit(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.use(_Agents(_plan("t1"), review=lambda d, n: _varying_major(n)))
        first = await _pipeline(tmp_path, _config(max_iterations=1)).execute("build it")
        assert first["_termination_reason"] == "Iteration limit (1) reached"
        saved = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1").load_state()
        assert saved is not None

        harness.use(_Agents(_plan("t1")))
        state = await _pipeline(tmp_path, _config(max_iterations=3)).execute(
            "build it", initial_state=saved
        )

        assert state["status"] == "approved"
        assert _artifact(tmp_path, "summary.json")["termination_reason"] == ""
        final = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1").load_state()
        assert final is not None and final["_termination_reason"] == ""

    async def test_a_resumed_error_run_tolerates_a_transient_failure(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        def down(data: dict[str, Any], n: int) -> dict[str, Any]:
            raise TimeoutError("provider down")

        harness.use(_Agents(_plan("t1"), implement=down))
        first = await _pipeline(tmp_path, _config(max_iterations=5)).execute("build it")
        assert first["status"] == "error"
        saved = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1").load_state()
        assert saved is not None and saved["_agent_failures"] == {"implementer": 2}

        def flaky_once(data: dict[str, Any], n: int) -> dict[str, Any]:
            if n == 1:
                raise TimeoutError("one transient failure")
            return {"files": {f"t1_{n}.py": "x = 1\n"}}

        agents = harness.use(_Agents(_plan("t1"), implement=flaky_once))
        state = await _pipeline(tmp_path, _config(max_iterations=5)).execute(
            "build it", initial_state=saved
        )

        assert agents.count("implementer") == 2
        assert state["status"] == "approved"
        errors = _events(tmp_path, "implementer_error")
        transient = [e for e in errors if "transient" in e["error"]]
        assert [e["consecutive_failures"] for e in transient] == [1]


class _PricedAgents(_Agents):
    """Every call costs $2 (one million gpt-4.1 input tokens)."""

    async def invoke(self, agent: object, input_data: dict[str, Any], role: str) -> dict[str, Any]:
        out = await super().invoke(agent, input_data, role)
        out["_meta"] = {"model": "gpt-4.1", "input_tokens": 1_000_000, "output_tokens": 0}
        return out


class TestBudgetStopStatus:
    async def test_a_budget_stop_after_an_approved_task_stays_exhausted(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        """Work was pending, so the run stays resumable: it is not "partial"."""
        harness.use(_PricedAgents(_plan("t1", "t2")))

        state = await _pipeline(tmp_path, _config(max_cost_usd=5.0)).execute("build it")

        [terminated] = _events(tmp_path, "terminated")
        assert terminated["limit"] == "cost"
        summary = _artifact(tmp_path, "summary.json")
        assert summary["status"] == state["status"] == "exhausted"
        assert summary["skipped_tasks"] == ["t2"]
        assert state["_task_outcomes"]["t1"]["status"] == "approved"

    def test_a_run_whose_last_task_gave_up_is_still_partial(self, tmp_path: Path) -> None:
        state = _new_state("r", tmp_path)
        state.update(
            status="exhausted",
            all_tasks=_plan("t1", "t2")["tasks"],
            _completed_tasks=["t1"],
            _skipped_tasks=["t2"],
        )

        graph._settle_run_status(state)

        assert state["status"] == "partial"


class TestReplanRequestScope:
    def test_a_director_re_plan_request_ends_with_its_task(self, tmp_path: Path) -> None:
        state = _new_state("r", tmp_path)
        state.update(_replan=True, replan_request={"source": "director", "reason": "T1-ONLY"})

        graph._reset_for_next_task(state, _task("t2"))

        assert state["_replan"] is False
        assert state["replan_request"] is None

    async def test_a_ruling_on_an_approved_task_never_re_plans_the_next_one(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        harness.sandbox.reports = lambda n, p: {**PASSED, "total": 3}
        reviews = iter(
            [
                _reject(_issue("major", "Same")),
                _reject(_issue("major", "Same")),  # recurs: the director rules
                _reject(_issue("minor", "t2 nit"), _issue("major", "t2 needs work")),
            ]
            + [APPROVE] * 10
        )

        def direct(data: dict[str, Any], n: int) -> dict[str, Any]:
            return {
                "resolutions": [
                    {"title": "Same", "decision": "ACCEPT", "reasoning": "fine as is"},
                    {
                        "title": "Later: split the IO layer",
                        "decision": "REVISE",
                        "alternative": "split",
                        "reasoning": "T1-RULING",
                        "fix_target": "architecture",
                    },
                ]
            }

        def implement(data: dict[str, Any], n: int) -> dict[str, Any]:
            task = _task_id(data)
            return {
                "files": {
                    f"{task}_{n}.py": "x = 1\n",
                    f"tests/test_{task}.py": "def test_x():\n    pass\n",
                }
            }

        agents = _Agents(_plan("t1", "t2"), direct=direct, implement=implement)
        agents.review = lambda d, n: next(reviews)
        harness.use(agents)

        state = await _pipeline(tmp_path, _config(max_iterations=5)).execute("build it")

        assert agents.count("director") == 1
        assert state["_task_outcomes"]["t1"]["status"] == "approved"
        assert agents.count("architect") == 1
        assert _events(tmp_path, "replanned") == []
        assert state["status"] == "approved"


class TestReplanOfSkippedTasks:
    async def test_a_skipped_task_reworked_under_its_id_is_done_again(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        plan = _plan("t1", "t2")
        reworked = _plan(
            _task("t1", ["store_v2.py"], title="Rework t1: new storage layer"),
            _task("t3", ["t3.py"], depends_on=["t1"]),
        )
        seen = {"t1": 0}

        def review(data: dict[str, Any], n: int) -> dict[str, Any]:
            task = data["task"]["task_id"]
            if task == "t1":
                seen["t1"] += 1
                return _varying_major(seen["t1"]) if seen["t1"] <= 2 else APPROVE
            if task == "t2":
                return _reject(_arch_issue())
            return APPROVE

        agents = _Agents(plan, review=review, architect=lambda d, n: plan if n == 1 else reworked)
        harness.use(agents)

        state = await _pipeline(tmp_path, _config(max_iterations=2)).execute("build it")

        # the architect is told which task was given up
        replan_input = agents.inputs("architect")[1]
        [skipped] = replan_input["skipped_tasks"]
        assert (skipped["task_id"], skipped["title"], skipped["status"]) == (
            "t1",
            "Title t1",
            "exhausted",
        )
        assert "t1.py" in skipped["outputs"]
        assert agents.task_ids()[3:] == ["t1", "t3"]
        [replanned] = _events(tmp_path, "replanned")
        assert replanned["reworked_skipped"] == ["t1"]
        summary = _artifact(tmp_path, "summary.json")
        [row] = [r for r in summary["task_outcomes"] if r["task_id"] == "t1"]
        assert (row["title"], row["status"]) == ("Rework t1: new storage layer", "approved")
        assert state["status"] == "approved"

    def test_only_an_unchanged_skipped_task_stays_given_up(self, tmp_path: Path) -> None:
        state = _new_state("r", tmp_path)
        state.update(all_tasks=_plan("t1", "t2")["tasks"], _skipped_tasks=["t1"])
        same = _plan("t1", "t3")["tasks"]
        renamed = [_task("t1", title="Other"), _task("t3")]
        new_outputs = [_task("t1", ["t1.py", "t1b.py"]), _task("t3")]

        assert graph._replan_finished_ids(state, same) == ([], ["t1"])
        assert graph._replan_finished_ids(state, renamed) == ([], [])
        assert graph._replan_finished_ids(state, new_outputs) == ([], [])

    def test_the_architect_input_lists_skipped_tasks_with_why(self, tmp_path: Path) -> None:
        state = _new_state("r", tmp_path)
        state.update(
            all_tasks=_plan("t1", "t2", "t3")["tasks"],
            _completed_tasks=["t2"],
            _skipped_tasks=["t3", "t1"],
            _task_outcomes={
                "t1": {"status": "stalled", "iterations": 3, "reason": "recurring_issues"},
                "t2": {"status": "approved", "iterations": 1},
                "t3": {"status": "exhausted", "iterations": 2},
            },
        )

        assert graph._skipped_tasks_input(state) == [
            {
                "task_id": "t1",
                "title": "Title t1",
                "outputs": ["t1.py"],
                "status": "stalled",
                "reason": "recurring_issues",
            },
            {"task_id": "t3", "title": "Title t3", "outputs": ["t3.py"], "status": "exhausted"},
        ]
        assert graph._skipped_tasks_input(_new_state("r", tmp_path)) == []


CALC_V1 = "def test_add():\n    pass\n\n\ndef test_sub():\n    pass\n"
CALC_V2 = "def test_add():\n    pass\n\n\ndef test_subtract():\n    pass\n"


def _calc_report(n: int, patch: dict[str, Any]) -> dict[str, Any]:
    """The per-test results of whichever calc test module the patch wrote."""
    tests = patch["files"].get("tests/test_calc.py", "")
    names = ["test_add", "test_sub"] if "test_sub(" in tests else ["test_add", "test_subtract"]
    results = [{"name": f"tests/test_calc.py::{name}", "passed": True} for name in names]
    return {**PASSED, "total": 2, "results": results}


class TestRefineOfTheTasksOwnTests:
    """#70: a refine re-opens the task, so its own accepted tests are its to change."""

    async def test_a_refine_may_rename_one_of_the_tasks_own_tests(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        v1 = {"calc.py": "V = 1\n", "tests/test_calc.py": CALC_V1}
        harness.use(_Agents(_plan("t1"), implement=lambda d, n: {"files": v1}))
        harness.sandbox.reports = _calc_report
        first = await _pipeline(tmp_path).execute("build it")
        assert first["_accepted_test_ids"] == [
            "tests/test_calc.py::test_add",
            "tests/test_calc.py::test_sub",
        ]

        # what ``delegatron refine`` does before a round
        state: dict[str, Any] = dict(first)
        state.update(status="running", error="", is_approved=False)
        state["_iteration_base"] = state["iteration"]
        graph.restart_current_task(state)  # type: ignore[arg-type]
        state["review_report"] = _reject(_issue("major", "rename test_sub to test_subtract"))
        assert state["_accepted_test_ids"] == []

        v2 = {"calc.py": "V = 2\n", "tests/test_calc.py": CALC_V2}
        agents = harness.use(_Agents(_plan("t1"), implement=lambda d, n: {"files": v2}))
        refined = await _pipeline(tmp_path).execute("build it", initial_state=state)

        assert agents.count("implementer") == 1
        assert refined["status"] == "approved"
        assert refined["_removed_tests"] == []
        assert refined["_accepted_test_ids"] == [
            "tests/test_calc.py::test_add",
            "tests/test_calc.py::test_subtract",
        ]

    def test_restart_keeps_the_tests_other_tasks_accepted(self, tmp_path: Path) -> None:
        plan = _plan("t1", "t2")
        state = _new_state("r", tmp_path)
        state.update(
            all_tasks=plan["tasks"],
            current_task=plan["tasks"][1],
            _completed_tasks=["t1", "t2"],
            _accepted_test_ids=["tests/test_t1.py::test_a", "tests/test_t2.py::test_b"],
            _accepted_test_owners={
                "tests/test_t1.py::test_a": "t1",
                "tests/test_t2.py::test_b": "t2",
            },
        )

        graph.restart_current_task(state)

        assert state["_accepted_test_ids"] == ["tests/test_t1.py::test_a"]
        assert state["_accepted_test_owners"] == {"tests/test_t1.py::test_a": "t1"}


class _TrimmingAgents(_Agents):
    """Every implementer call reports a prompt the context guard trimmed."""

    async def invoke(self, agent: object, input_data: dict[str, Any], role: str) -> dict[str, Any]:
        out = await super().invoke(agent, input_data, role)
        if role == "implementer":
            out["_meta"]["context_trimmed"] = {"keys": ["repo_map"], "window": 4096}
        return out


class TestStepEventsOnce:
    async def test_a_persisting_step_callback_writes_each_event_once(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        """The server's step callback writes every step to events.jsonl itself."""
        store = ArtifactStore(base_dir=tmp_path / "runs", run_id="run1")
        steps: list[str] = []

        def step_cb(step: str, data: dict[str, Any]) -> None:
            steps.append(step)
            store.append_event({"event": step, **data})

        harness.use(_TrimmingAgents(_plan("t1", "t2")))

        await _pipeline(tmp_path, step_callback=step_cb, step_callback_persists=True).execute(
            "build it"
        )

        assert len(_events(tmp_path, "plan_normalized")) == 1
        assert len(_events(tmp_path, "context_trimmed")) == 2
        assert len(_events(tmp_path, "task_advanced")) == 1
        assert steps.count("plan_normalized") == 1
        assert steps.count("context_trimmed") == 2

    async def test_without_one_the_store_writes_them(
        self, tmp_path: Path, harness: SimpleNamespace
    ) -> None:
        steps: list[str] = []
        harness.use(_TrimmingAgents(_plan("t1", "t2")))

        await _pipeline(tmp_path, step_callback=lambda s, d: steps.append(s)).execute("build it")

        assert len(_events(tmp_path, "plan_normalized")) == 1
        assert len(_events(tmp_path, "context_trimmed")) == 2
        assert steps.count("plan_normalized") == 1

    def test_a_siblings_step_is_tagged_like_its_event(self, tmp_path: Path) -> None:
        received: list[tuple[str, dict[str, Any]]] = []
        pipeline = _pipeline(tmp_path, step_callback=lambda s, d: received.append((s, d)))
        written: list[dict[str, Any]] = []
        sink = graph._SiblingEvents(SimpleNamespace(append_event=written.append), "t2")
        state = _new_state("r", tmp_path)

        pipeline._emit_step_event(state, sink, {"event": "context_trimmed", "keys": ["x"]})

        tagged = {"keys": ["x"], "task_id": "t2", "parallel": True}
        assert received == [("context_trimmed", tagged)]
        assert written == [{"event": "context_trimmed", **tagged}]
