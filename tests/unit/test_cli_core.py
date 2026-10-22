"""CLI behaviour for run results, show, refine, review/plan on non-git dirs and
template constraints (#87, #89, #97, #120, #125).  No LLM, Docker or network:
pipelines are faked and review/plan use the mock preset.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any, ClassVar

import pytest
from click.testing import CliRunner
from rich.console import Console

from delegatron import cli
from delegatron.orchestrator import graph
from delegatron.prompts import set_prompts_dir

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _quiet_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    # The CLI's logging setup reconfigures the global "delegatron" logger.
    monkeypatch.setattr(cli, "setup_logging", lambda **kwargs: None)
    set_prompts_dir(REPO_ROOT / "prompts")


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, width=200, color_system=None))
    return buffer


class _FakePipeline:
    """Captures what the CLI hands to Pipeline and returns a canned state."""

    instances: ClassVar[list[_FakePipeline]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.initial_state: dict[str, Any] | None = None
        _FakePipeline.instances.append(self)

    async def execute(
        self, request: str, initial_state: dict[str, Any] | None = None, stream: bool = False
    ) -> dict[str, Any]:
        self.initial_state = None if initial_state is None else dict(initial_state)
        state = dict(initial_state or {"iteration": 1, "messages": []})
        state.update({"status": "exhausted", "request": request})
        return state


@pytest.fixture
def fake_pipeline(monkeypatch: pytest.MonkeyPatch) -> type[_FakePipeline]:
    _FakePipeline.instances = []
    monkeypatch.setattr(graph, "Pipeline", _FakePipeline)
    return _FakePipeline


class TestRunResults:
    def test_lists_every_file_changed_in_the_run(self, tmp_path: Path, captured: io.StringIO):
        (tmp_path / "card_deck.py").write_text("a\nb\n", encoding="utf-8")
        (tmp_path / "simulate_deal.py").write_text("c\n", encoding="utf-8")
        state = {
            "status": "approved",
            "workspace_root": str(tmp_path),
            "current_patch": {"files": {"simulate_deal.py": "c\n"}},
            "_file_history": {
                "card_deck.py": {"task_id": "t1", "iteration": 1},
                "simulate_deal.py": {"task_id": "t3", "iteration": 4},
                "old.py": {"task_id": "t2", "iteration": 2, "deleted": True},
            },
            "messages": [],
        }

        cli._display_results(state)

        output = captured.getvalue()
        assert "card_deck.py (2 lines)" in output
        assert "simulate_deal.py (1 lines)" in output
        assert "- old.py" in output

    def test_shows_the_run_branch_and_the_way_back(self, captured: io.StringIO):
        state = {
            "status": "approved",
            "messages": [
                {
                    "event": "workspace_initialized",
                    "branch": "delegatron/run-abc",
                    "original_branch": "feature-x",
                }
            ],
        }

        cli._display_results(state)

        output = captured.getvalue()
        assert "delegatron/run-abc" in output
        assert "git checkout feature-x" in output


class TestShow:
    def test_show_prints_branch_and_files_changed(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "runs" / "abc"
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            json.dumps(
                {
                    "run_id": "abc",
                    "status": "error",
                    "error": "Architect failed: bad JSON",
                    "branch": "delegatron/run-abc",
                    "files_changed": ["card_deck.py", "tests/test_card_deck.py"],
                }
            ),
            encoding="utf-8",
        )

        result = CliRunner().invoke(cli.main, ["show", "abc", "--workspace", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "delegatron/run-abc" in result.output
        assert "tests/test_card_deck.py" in result.output
        assert "error" in result.output

    def test_show_prints_the_stop_reason_and_the_test_lists(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "runs" / "abc"
        run_dir.mkdir(parents=True)
        summary = {
            "run_id": "abc",
            "status": "partial",
            "termination_reason": "Iteration limit (2) reached",
            "regressed_tests": {"tests/test_deck.py::test_size[52]": "t2"},
            "fixed_baseline_failures": {"tests/test_old.py::test_x": "pre_existing"},
            "files_deleted": ["old_deck.py"],
        }
        (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")

        result = CliRunner().invoke(cli.main, ["show", "abc", "--workspace", str(tmp_path)])

        assert result.exit_code == 0, result.output
        output = " ".join(result.output.split())
        assert "Iteration limit (2) reached" in output
        assert "tests/test_deck.py::test_size[52] (t2)" in output  # [52] is not markup
        assert "tests/test_old.py::test_x (pre_existing)" in output
        assert "old_deck.py" in output


class TestRefine:
    def test_round_gets_its_own_window_and_warns_when_nothing_ran(
        self, tmp_path: Path, fake_pipeline: type[_FakePipeline]
    ) -> None:
        run_dir = tmp_path / "runs" / "abc"
        run_dir.mkdir(parents=True)
        saved = {
            "request": "build",
            "iteration": 5,
            "_task_iteration": 2,
            "status": "exhausted",
            "total_cost_usd": 1.5,
            "messages": [],
        }
        (run_dir / "state_005.json").write_text(json.dumps(saved), encoding="utf-8")
        (run_dir / "summary.json").write_text(
            json.dumps({"run_id": "abc", "branch": "delegatron/run-abc"}), encoding="utf-8"
        )

        result = CliRunner().invoke(
            cli.main,
            ["refine", "abc", "--workspace", str(tmp_path), "--mock"],
            input="make it faster\ndone\n",
        )

        assert result.exit_code == 0, result.output
        [pipeline] = fake_pipeline.instances
        assert pipeline.initial_state is not None
        assert pipeline.initial_state["_iteration_base"] == 5
        assert pipeline.initial_state["_task_iteration"] == 0
        assert "ran no iteration" in result.output
        assert "Status:" in result.output
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        assert summary["refinement_rounds"] == 1
        assert summary["branch"] == "delegatron/run-abc"
        assert summary["total_cost_usd"] == 1.5

    def test_the_summary_says_why_the_last_round_stopped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An approved round leaves no stop reason of an earlier session behind."""

        class _Approving(_FakePipeline):
            async def execute(
                self,
                request: str,
                initial_state: dict[str, Any] | None = None,
                stream: bool = False,
            ) -> dict[str, Any]:
                state = await super().execute(request, initial_state, stream)
                state.update(status="approved", is_approved=True, _termination_reason="")
                state["iteration"] = state.get("iteration", 0) + 1
                return state

        monkeypatch.setattr(graph, "Pipeline", _Approving)
        run_dir = tmp_path / "runs" / "abc"
        run_dir.mkdir(parents=True)
        stale = "Iteration limit (1) reached"
        saved = {
            "request": "build",
            "iteration": 1,
            "status": "exhausted",
            "_termination_reason": stale,
            "messages": [],
        }
        (run_dir / "state_001.json").write_text(json.dumps(saved), encoding="utf-8")
        (run_dir / "summary.json").write_text(
            json.dumps({"run_id": "abc", "termination_reason": stale}), encoding="utf-8"
        )

        result = CliRunner().invoke(
            cli.main,
            ["refine", "abc", "--workspace", str(tmp_path), "--mock"],
            input="rename it\ndone\n",
        )

        assert result.exit_code == 0, result.output
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        assert (summary["status"], summary["termination_reason"]) == ("approved", "")


class TestNonGitWorkspace:
    def test_review_does_not_crash_outside_git(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

        result = CliRunner().invoke(
            cli.main, ["review", "--mock", "--workspace", str(tmp_path), "--json-output"]
        )

        assert result.exit_code == 0, result.output
        # Other tests may leave a stdout log handler behind, so match instead of parse.
        assert re.search(r'"files_reviewed": [1-9]', result.output)

    def test_plan_does_not_crash_outside_git(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

        result = CliRunner().invoke(
            cli.main,
            [
                "plan",
                "--preset",
                "mock",
                "--workspace",
                str(tmp_path),
                "--json-output",
                "add a CLI",
            ],
        )

        assert result.exit_code == 0, result.output
        assert '"tasks": [' in result.output

    def test_plan_shows_the_normalized_plan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`delegatron plan` previews the plan `run` executes (#78)."""
        raw = {
            "summary": "deal cards",
            "tasks": [
                {"task_id": "t1", "title": "Deck", "outputs": ["deck.py"]},
                {"task_id": "t2", "title": "Deal", "outputs": ["deal.py", "test_deal.py"]},
            ],
            "acceptance": {"criteria": ["deals"]},
        }

        class _Architect:
            async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
                return {**raw, "_meta": {}}

        monkeypatch.setattr(
            "delegatron.factories.agent_factory.create_architect", lambda config: _Architect()
        )

        result = CliRunner().invoke(
            cli.main,
            ["plan", "--preset", "mock", "--workspace", str(tmp_path), "--json-output", "deal"],
        )

        assert result.exit_code == 0, result.output
        assert '"tests/test_deal.py"' in result.output
        assert '"tests/test_deck.py"' in result.output
        assert "Plan normalized: t2" in result.output


class TestTemplateRun:
    def test_template_constraints_reach_the_pipeline_config(
        self, tmp_path: Path, fake_pipeline: type[_FakePipeline]
    ) -> None:
        result = CliRunner().invoke(
            cli.main,
            [
                "template",
                "run",
                "security-hardening",
                "--mock",
                "--dir",
                str(tmp_path / "templates"),
                "--workspace",
                str(tmp_path),
                "--json-output",
                "--var",
                "target=src",
            ],
        )

        assert result.exit_code == 0, result.output
        [pipeline] = fake_pipeline.instances
        constraints = pipeline.kwargs["config"].pipeline.constraints
        assert "shell=True" in constraints["forbidden_patterns"].values()


class TestPartialRuns:
    def test_partial_status_and_each_task_outcome_are_shown(self, captured: io.StringIO) -> None:
        state = {
            "status": "partial",
            "plan": {
                "summary": "deal",
                "tasks": [
                    {"task_id": "t1", "title": "Deck", "risk_level": "low"},
                    {"task_id": "t2", "title": "Deal", "risk_level": "low"},
                ],
            },
            "_task_outcomes": {
                "t1": {"status": "approved", "iterations": 1},
                "t2": {"status": "skipped", "iterations": 3},
            },
            "messages": [],
        }

        cli._display_results(state)

        output = captured.getvalue()
        assert "PARTIAL" in output
        assert "Deck (approved)" in output
        assert "Deal (skipped)" in output

    def test_list_runs_and_show_name_partial_runs(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "runs" / "abc"
        run_dir.mkdir(parents=True)
        summary = {
            "run_id": "abc",
            "status": "partial",
            "task_outcomes": [
                {"task_id": "t1", "title": "Deck", "status": "approved", "iterations": 1},
                {"task_id": "t2", "title": "Deal", "status": "stalled", "iterations": 5},
            ],
        }
        (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")

        listed = CliRunner().invoke(cli.main, ["list-runs", "--workspace", str(tmp_path)])
        shown = CliRunner().invoke(cli.main, ["show", "abc", "--workspace", str(tmp_path)])

        assert listed.exit_code == 0, listed.output
        assert "partial" in listed.output
        assert shown.exit_code == 0, shown.output
        assert "t2: Deal — stalled (5 iterations)" in shown.output


class TestWorkspaceArgument:
    def test_run_reads_the_workspace_delegatron_toml(
        self, tmp_path: Path, fake_pipeline: type[_FakePipeline]
    ) -> None:
        """#111: `run --workspace X` picks up X/delegatron.toml."""
        (tmp_path / "delegatron.toml").write_text(
            "[pipeline]\nmax_iterations = 7\n", encoding="utf-8"
        )

        result = CliRunner().invoke(
            cli.main, ["run", "--mock", "--workspace", str(tmp_path), "--json-output", "x"]
        )

        assert result.exit_code == 0, result.output
        [pipeline] = fake_pipeline.instances
        assert pipeline.kwargs["config"].pipeline.max_iterations == 7

    @pytest.mark.parametrize("command", ["run", "review"])
    def test_branch_reaches_the_clone_of_a_git_url(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_pipeline: type[_FakePipeline],
        command: str,
    ) -> None:
        """#86: `--branch` selects the branch cloned for a git URL workspace."""
        calls: list[tuple[str, str | None]] = []
        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

        def resolve(workspace: str, branch: str | None = None) -> tuple[Path, Any]:
            calls.append((workspace, branch))
            return tmp_path, lambda: None

        monkeypatch.setattr("delegatron.repo_resolve.resolve_workspace", resolve)
        url = "https://github.com/org/repo.git"
        args = [command, "--mock", "--workspace", url, "--branch", "develop", "--json-output"]

        result = CliRunner().invoke(cli.main, [*args, "x"] if command == "run" else args)

        assert result.exit_code == 0, result.output
        assert calls == [(url, "develop")]

    @pytest.mark.parametrize("workspace", ["--upload-pack=x", "missing-dir"])
    def test_invalid_workspace_is_a_usage_error(self, workspace: str) -> None:
        result = CliRunner().invoke(cli.main, ["run", "--mock", f"--workspace={workspace}", "x"])

        assert result.exit_code == 2
        assert "--workspace" in result.output


class _ReviewerSpy:
    """Wraps the real reviewer agent; records its input and the ``_meta`` of its result."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.inputs: list[dict[str, Any]] = []
        self.metas: list[dict[str, Any]] = []

    async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
        self.inputs.append(dict(input_data))
        result = await self.agent.invoke(input_data)
        self.metas.append(dict(result.get("_meta") or {}))
        return result


@pytest.fixture
def reviewer_spy(monkeypatch: pytest.MonkeyPatch) -> list[_ReviewerSpy]:
    from delegatron.factories import agent_factory

    spies: list[_ReviewerSpy] = []
    real_create = agent_factory.create_reviewer

    def create(config: Any) -> _ReviewerSpy:
        spies.append(_ReviewerSpy(real_create(config)))
        return spies[-1]

    monkeypatch.setattr(agent_factory, "create_reviewer", create)
    return spies


def _source_files(root: Path, count: int, size: int) -> None:
    for i in range(count):
        body = "".join(f"def f{i}_{n}(x):\n    return x + {n}\n" for n in range(size // 30))
        (root / f"mod_{i:02d}.py").write_text(body, encoding="utf-8")


class TestReviewInput:
    def test_reviewer_window_sizes_and_trims_the_reviewed_files(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reviewer_spy: list[_ReviewerSpy],
    ) -> None:
        """#75: the file budget comes from the reviewer's window and the files can be trimmed."""
        from dataclasses import replace

        from delegatron import context

        _source_files(tmp_path, count=20, size=1_000)
        real_load = cli.load_config

        def load(**kwargs: Any) -> Any:
            config = real_load(**kwargs)
            return replace(config, reviewer=replace(config.reviewer, context_window=8192))

        budgets: list[int] = []
        real_gather = context.gather_review_context

        def gather(*args: Any, **kwargs: Any) -> Any:
            budgets.append(kwargs["max_total_size"])
            return real_gather(*args, **kwargs)

        monkeypatch.setattr(cli, "load_config", load)
        monkeypatch.setattr(context, "gather_review_context", gather)

        result = CliRunner().invoke(
            cli.main, ["review", "--mock", "--workspace", str(tmp_path), "--json-output"]
        )

        assert result.exit_code == 0, result.output
        assert budgets == [14_336]  # half of an 8192-token window, in bytes
        [spy] = reviewer_spy
        assert spy.inputs[0]["related_files"]
        assert "--- File:" not in spy.inputs[0]["diff"]
        trimmed = spy.metas[0]["context_trimmed"]
        assert "related_files" in (trimmed["keys"] if isinstance(trimmed, dict) else trimmed)

    def test_review_input_starts_with_the_repo_map(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reviewer_spy: list[_ReviewerSpy],
    ) -> None:
        """#125: the reviewer sees the structural repo map before the files."""
        from delegatron import context

        (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        monkeypatch.setattr(context, "gather_repo_map", lambda *a, **k: "app.py: def add(a, b)")

        result = CliRunner().invoke(
            cli.main, ["review", "--mock", "--workspace", str(tmp_path), "--json-output"]
        )

        assert result.exit_code == 0, result.output
        diff = reviewer_spy[0].inputs[0]["diff"]
        assert "--- Repository Structure ---" in diff
        assert "app.py: def add(a, b)" in diff
        assert "app.py" in reviewer_spy[0].inputs[0]["related_files"]

    def test_log_review_with_suggestions_outside_git(self, tmp_path: Path) -> None:
        """#125: logs + suggestions on a non-git workspace finish cleanly."""
        log = tmp_path / "x.log"
        log.write_text("ERROR boom\nWARN slow\n", encoding="utf-8")

        result = CliRunner().invoke(
            cli.main,
            [
                "review",
                "--mock",
                "--logs",
                str(log),
                "--with-suggestions",
                "--workspace",
                str(tmp_path),
                "--json-output",
            ],
        )

        assert result.exit_code == 0, result.output
        assert '"mode": "logs"' in result.output


class TestRemoteWorkspaceConfig:
    @pytest.mark.parametrize("command", ["run", "review"])
    def test_cloned_repo_toml_is_not_trusted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_pipeline: type[_FakePipeline],
        command: str,
    ) -> None:
        """#111: a remote repo's delegatron.toml cannot point roles at its own endpoint."""
        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "delegatron.toml").write_text(
            "[pipeline]\nmax_iterations = 9\n"
            '[roles.reviewer]\nprovider = "openai-compatible"\n'
            'base_url = "https://attacker.example/v1"\napi_key_env = "OPENAI_API_KEY"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "delegatron.repo_resolve.resolve_workspace",
            lambda workspace, branch=None: (tmp_path, lambda: None),
        )
        loaded: list[dict[str, Any]] = []
        real_load = cli.load_config

        def load(**kwargs: Any) -> Any:
            loaded.append(kwargs)
            return real_load(**kwargs)

        monkeypatch.setattr(cli, "load_config", load)
        url = "https://github.com/untrusted/repo.git"
        args = [command, "--mock", "--workspace", url, "--json-output"]

        result = CliRunner().invoke(cli.main, [*args, "x"] if command == "run" else args)

        assert result.exit_code == 0, result.output
        assert loaded[0]["workspace"] is None
        if command == "run":
            config = fake_pipeline.instances[0].kwargs["config"]
            assert config.pipeline.max_iterations != 9
            assert config.reviewer.base_url != "https://attacker.example/v1"


class TestPlanInput:
    def test_plan_gives_the_architect_the_repo_map_and_constraints(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#97/#124: `plan` builds the architect input `run` builds."""
        (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
        (tmp_path / "delegatron.toml").write_text(
            '[constraints]\nno_modify = ["app.py"]\n', encoding="utf-8"
        )
        seen: list[dict[str, Any]] = []

        class _Architect:
            async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
                seen.append(dict(input_data))
                return {
                    "summary": "s",
                    "tasks": [{"task_id": "t1", "title": "T", "outputs": ["b.py"]}],
                    "acceptance": {"criteria": ["c"]},
                    "_meta": {},
                }

        monkeypatch.setattr(
            "delegatron.factories.agent_factory.create_architect", lambda config: _Architect()
        )

        result = CliRunner().invoke(
            cli.main,
            ["plan", "--preset", "mock", "--workspace", str(tmp_path), "--json-output", "add b"],
        )

        assert result.exit_code == 0, result.output
        assert "main" in seen[0]["repo_map"]
        assert seen[0]["constraints"]["no_modify"] == ["app.py"]


class TestEstimate:
    def test_local_endpoint_roles_are_free_whatever_the_model_name(
        self, monkeypatch: pytest.MonkeyPatch, captured: io.StringIO
    ) -> None:
        """#112: locality comes from the role's endpoint, not a model-name guess."""
        from delegatron.config import Config, RoleConfig

        config = Config(
            architect=RoleConfig(
                provider="openai-compatible",
                model="custom-coder",
                base_url="http://localhost:11434/v1",
            ),
            implementer=RoleConfig(provider="openai", model="llama-3.3-70b"),
            reviewer=RoleConfig(provider="openai", model="gpt-4.1"),
        )
        monkeypatch.setattr(cli, "load_config", lambda **kwargs: config)

        result = CliRunner().invoke(cli.main, ["estimate", "x", "--max-iterations", "1"])

        assert result.exit_code == 0, result.output
        costs = {
            cells[0]: cells[2]
            for line in captured.getvalue().splitlines()
            if len(cells := [c.strip() for c in re.split(r"[│|]", line)[1:-1]]) == 3
        }
        assert costs["Architect"] == "$0.0000"
        assert costs["Implementer"] != "$0.0000"
        assert "Local GPU cost" in captured.getvalue()


class TestTemplateValidation:
    def test_template_security_mode_is_normalized(
        self, tmp_path: Path, fake_pipeline: type[_FakePipeline]
    ) -> None:
        from delegatron.templates import RunTemplate, TemplateStore

        store_dir = tmp_path / "templates"
        TemplateStore(store_dir).save(
            RunTemplate(
                name="t", request_pattern="do it", overrides={"pipeline": {"security_mode": "WARN"}}
            )
        )
        args = ["template", "run", "t", "--mock", "--dir", str(store_dir)]

        result = CliRunner().invoke(
            cli.main, [*args, "--workspace", str(tmp_path), "--json-output"]
        )

        assert result.exit_code == 0, result.output
        assert fake_pipeline.instances[0].kwargs["config"].pipeline.security_mode == "warn"

    @pytest.mark.parametrize(
        ("overrides", "constraints"),
        [({"pipeline": {"security_mode": "strict"}}, {}), ({}, {"forbidden_patterns": ["re:("]})],
    )
    def test_invalid_template_settings_are_a_usage_error(
        self,
        tmp_path: Path,
        fake_pipeline: type[_FakePipeline],
        overrides: dict[str, Any],
        constraints: dict[str, Any],
    ) -> None:
        from delegatron.templates import RunTemplate, TemplateStore

        store_dir = tmp_path / "templates"
        TemplateStore(store_dir).save(
            RunTemplate(
                name="bad", request_pattern="do it", overrides=overrides, constraints=constraints
            )
        )
        args = ["template", "run", "bad", "--mock", "--dir", str(store_dir)]

        result = CliRunner().invoke(cli.main, [*args, "--workspace", str(tmp_path)])

        assert result.exit_code == 2, result.output
        assert fake_pipeline.instances == []


class _Scripted:
    """Agents and sandbox for a real ``graph.Pipeline`` run: no LLM, Docker or network."""

    PLAN: ClassVar[dict[str, Any]] = {
        "summary": "plan",
        "tasks": [
            {
                "task_id": "t1",
                "title": "T1",
                "description": "write t1.py",
                "outputs": ["t1.py"],
                "risk_level": "low",
                "depends_on": [],
            }
        ],
        "acceptance": {"criteria": ["works"]},
    }
    META: ClassVar[dict[str, Any]] = {
        "model": "gpt-4.1",
        "input_tokens": 1000,
        "output_tokens": 500,
    }

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, architect_error: bool = False) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from delegatron.detection import ProjectInfo

        self.implementer_calls = 0

        async def plan(_data: dict[str, Any]) -> dict[str, Any]:
            if architect_error:
                raise RuntimeError("architect is down")
            return {**json.loads(json.dumps(self.PLAN)), "_meta": dict(self.META)}

        async def implement(_data: dict[str, Any]) -> dict[str, Any]:
            self.implementer_calls += 1
            n = self.implementer_calls
            return {
                "status": "ok",
                "rationale": f"attempt {n}",
                "files": {"t1.py": f"VALUE = {n}\n"},
                "diff_unified": "",
                "tests_to_run": [],
                "questions": [],
                "_meta": dict(self.META),
            }

        async def review(_data: dict[str, Any]) -> dict[str, Any]:
            issue = {
                "severity": "major",
                "title": "not yet",
                "details": "d",
                "recommended_fix": "f",
                "evidence": "t1.py",
            }
            return {
                "summary": "no",
                "acceptance_met": False,
                "issues": [issue],
                "_meta": dict(self.META),
            }

        agents = {
            "create_architect": plan,
            "create_implementer": implement,
            "create_reviewer": review,
        }
        for factory, invoke in agents.items():
            agent = MagicMock()
            agent.invoke = AsyncMock(side_effect=invoke)
            monkeypatch.setattr(graph, factory, lambda config, agent=agent: agent)
        sandbox = MagicMock()
        sandbox.apply_and_test = AsyncMock(
            return_value={"passed": True, "total": 1, "failures": 0, "stdout": "1 passed"}
        )
        monkeypatch.setattr(graph, "create_sandbox", lambda **kwargs: sandbox)
        monkeypatch.setattr(graph, "detect_project", lambda root: ProjectInfo(languages=["python"]))
        monkeypatch.setattr(graph, "get_gpu_power_watts", lambda: None)
        monkeypatch.setattr(graph, "unload_except", lambda *a, **k: None)
        monkeypatch.setattr(graph, "cleanup_after_run", lambda *a, **k: None)

    @staticmethod
    def config() -> Any:
        from delegatron.config import Config, PipelineConfig, RoleConfig

        return Config(
            architect=RoleConfig(provider="openai", model="gpt-4.1"),
            implementer=RoleConfig(provider="openai", model="gpt-4.1"),
            reviewer=RoleConfig(provider="openai", model="gpt-4.1"),
            pipeline=PipelineConfig(
                max_iterations=1, max_cost_usd=50.0, max_wall_time_minutes=5, sandbox="local"
            ),
        )


class TestRefineCost:
    def test_two_rounds_add_both_rounds_to_the_run_cost(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#89: the summary's cost is the original run plus every refinement round."""
        import asyncio

        from delegatron.utils.cost import logged_cost_usd

        _Scripted(monkeypatch)
        config = _Scripted.config()
        monkeypatch.setattr(cli, "load_config", lambda **kwargs: config)
        state = asyncio.run(
            graph.Pipeline(config=config, workspace_root=tmp_path, run_id="abc").execute("build")
        )
        original = state["total_cost_usd"]
        call_log = tmp_path / "runs" / "abc" / "provider_calls.jsonl"
        assert original == pytest.approx(logged_cost_usd(call_log)) and original > 0

        result = CliRunner().invoke(
            cli.main,
            ["refine", "abc", "--workspace", str(tmp_path)],
            input="faster\nsmaller\ndone\n",
        )

        assert result.exit_code == 0, result.output
        rows = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()]
        per_iteration: dict[int, float] = {}
        for row in rows:
            per_iteration[row["iteration"]] = (
                per_iteration.get(row["iteration"], 0.0) + row["cost_usd"]
            )
        round1, round2 = per_iteration[2], per_iteration[3]
        assert round1 > 0 and round2 > 0
        summary = json.loads((tmp_path / "runs" / "abc" / "summary.json").read_text("utf-8"))
        assert summary["refinement_rounds"] == 2
        assert summary["total_cost_usd"] == pytest.approx(original + round1 + round2)


class TestFailedRunListing:
    def test_list_runs_and_show_report_an_architect_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#105: a run that failed before coding still lists as error, with its reason."""
        import asyncio

        _Scripted(monkeypatch, architect_error=True)
        state = asyncio.run(
            graph.Pipeline(
                config=_Scripted.config(), workspace_root=tmp_path, run_id="boom"
            ).execute("build")
        )
        assert state["status"] == "error"

        listed = CliRunner().invoke(cli.main, ["list-runs", "--workspace", str(tmp_path)])
        shown = CliRunner().invoke(cli.main, ["show", "boom", "--workspace", str(tmp_path)])

        assert listed.exit_code == 0, listed.output
        assert re.search(r"boom\s.*error", listed.output)
        assert shown.exit_code == 0, shown.output
        assert "architect is down" in shown.output


class TestRunWiring:
    def test_branch_is_the_base_of_the_run_branch(
        self, tmp_path: Path, fake_pipeline: type[_FakePipeline]
    ) -> None:
        """D8: `run --branch dev` forks the run branch from dev; no --branch: the checkout."""
        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

        with_branch = CliRunner().invoke(
            cli.main,
            [
                "run",
                "--mock",
                "--workspace",
                str(tmp_path),
                "--branch",
                "dev",
                "--json-output",
                "x",
            ],
        )
        without = CliRunner().invoke(
            cli.main, ["run", "--mock", "--workspace", str(tmp_path), "--json-output", "x"]
        )

        assert with_branch.exit_code == 0, with_branch.output
        assert without.exit_code == 0, without.output
        bases = [pipeline.kwargs["base_branch"] for pipeline in fake_pipeline.instances]
        assert bases == ["dev", ""]

    def test_benchmark_and_project_accept_allow_dirty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#88: the benchmark and project commands have the --allow-dirty opt-out."""
        import delegatron.benchmark as benchmark_module
        import delegatron.project as project_module

        seen: dict[str, Any] = {}

        async def run_benchmark(request: str, workspace: Path, presets: list[str], **kw: Any):
            seen["benchmark"] = kw
            return benchmark_module.BenchmarkReport(request=request)

        class _Runner:
            def __init__(self, **kwargs: Any) -> None:
                seen["project"] = kwargs

            async def execute(self, request: str) -> Any:
                return project_module.ProjectReport(project_id="p", request=request, preset="x")

        monkeypatch.setattr(benchmark_module, "run_benchmark", run_benchmark)
        monkeypatch.setattr(project_module, "ProjectRunner", _Runner)
        common = ["--workspace", str(tmp_path), "--json-output", "--allow-dirty", "x"]

        bench = CliRunner().invoke(cli.main, ["benchmark", "--presets", "mock", *common])
        proj = CliRunner().invoke(cli.main, ["project", "--preset", "mock", *common])

        assert bench.exit_code == 0, bench.output
        assert proj.exit_code == 0, proj.output
        assert seen["benchmark"]["allow_dirty"] is True
        assert seen["project"]["allow_dirty"] is True


class TestResultDisplay:
    @pytest.mark.parametrize(
        ("report", "expected"),
        [
            ({"passed": True, "total": 3, "failures": 0, "stdout": "3 passed"}, "PASSED (3 total"),
            (
                {"passed": False, "total": 0, "skipped_reason": "non_testable_language"},
                "UNTESTED (non_testable_language)",
            ),
            (
                {"passed": True, "total": 1, "synthetic_tests": True, "stdout": "1 passed"},
                "UNTESTED (only synthetic smoke tests)",
            ),
            ({"passed": False, "total": 2, "failures": 1, "stdout": "1 failed"}, "FAILED (2 total"),
        ],
    )
    def test_untested_runs_are_not_shown_as_passed_or_failed(
        self, captured: io.StringIO, report: dict[str, Any], expected: str
    ) -> None:
        cli._display_results({"status": "approved", "test_report": report, "messages": []})

        assert expected in captured.getvalue()

    def test_refine_summary_separates_deleted_files(self) -> None:
        state = {
            "_file_history": {
                "a.py": {"task_id": "t1", "iteration": 1},
                "gone.py": {"task_id": "t1", "iteration": 1, "deleted": True},
            }
        }

        assert cli._files_summary(state) == {
            "files_changed": ["a.py"],
            "files_deleted": ["gone.py"],
        }


class TestRefineReopensTheTask:
    def test_the_refined_task_leaves_completed_and_skipped(
        self, tmp_path: Path, fake_pipeline: type[_FakePipeline]
    ) -> None:
        run_dir = tmp_path / "runs" / "abc"
        run_dir.mkdir(parents=True)
        saved = {
            "request": "build",
            "iteration": 4,
            "_task_iteration": 3,
            "status": "stalled",
            "current_task": {"task_id": "t2", "title": "Deal"},
            "_completed_tasks": ["t1", "t2"],
            "_skipped_tasks": ["t2"],
            "_failure_signatures": ["sig"],
            "messages": [],
        }
        (run_dir / "state_004.json").write_text(json.dumps(saved), encoding="utf-8")

        result = CliRunner().invoke(
            cli.main,
            ["refine", "abc", "--workspace", str(tmp_path), "--mock"],
            input="deal five cards\ndone\n",
        )

        assert result.exit_code == 0, result.output
        [pipeline] = fake_pipeline.instances
        state = pipeline.initial_state
        assert state is not None
        assert state["_completed_tasks"] == ["t1"]
        assert state["_skipped_tasks"] == []
        assert state["_failure_signatures"] == []
        assert state["review_report"]["issues"][0]["details"] == "deal five cards"
