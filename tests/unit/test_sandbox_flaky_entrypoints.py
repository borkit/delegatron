"""Tests for per-test report fields, flaky-test reruns and entrypoint smoke runs.

Covers the shared helpers in ``sandbox/base.py`` and their use by the Docker
sandbox (``subprocess.run`` stubbed, no Docker needed) and the local sandbox
(mostly mocked, plus real runs of throwaway scripts and one real pytest run).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock, patch

import pytest

from delegatron.sandbox import local_runner
from delegatron.sandbox.base import (
    MAX_RERUN_TESTS,
    MAX_RESULTS,
    build_results,
    entrypoint_paths,
    entrypoint_run,
    flaky_from_rerun,
    flaky_tolerated,
    per_test_fields,
    plan_rerun,
    pytest_prefix,
    pytest_rerun_args,
    rerun_candidates,
)
from delegatron.sandbox.docker_runner import DockerSandbox
from delegatron.sandbox.local_runner import LocalSandbox
from delegatron.sandbox.parse_check import ParseCheckResult, ParseError

SHUFFLE = "tests/test_deck.py::test_shuffle"
SIZE = "tests/test_deck.py::TestHand::test_size[5]"

# Real-shaped `pytest -v` output: one pass, two failures (one parametrized).
FAILING_RUN = f"""\
============================= test session starts ==============================
platform linux -- Python 3.12.13, pytest-9.1.1, pluggy-1.6.0 -- /usr/local/bin/python
rootdir: /workspace
collecting ... collected 3 items

tests/test_deck.py::test_deal PASSED                                     [ 33%]
{SHUFFLE} FAILED                                  [ 66%]
{SIZE} FAILED                        [100%]

=================================== FAILURES ===================================
_________________________________ test_shuffle _________________________________

    def test_shuffle():
>       assert deck.cards != shuffled
E       AssertionError: assert [1, 2, 3] != [1, 2, 3]

tests/test_deck.py:12: AssertionError
____________________________ TestHand.test_size[5] _____________________________

>       assert len(hand) == 5
E       assert 4 == 5

tests/test_deck.py:20: AssertionError
=========================== short test summary info ============================
FAILED {SHUFFLE} - AssertionError: assert [1, 2, 3] != [1, 2, 3]
FAILED {SIZE} - assert 4 == 5
========================= 2 failed, 1 passed in 0.05s ==========================
"""

# `pytest -q` rerun of both ids: the shuffle test passes this time.
RERUN_ONE_STILL_FAILING = f"""\
.F                                                                       [100%]
=================================== FAILURES ===================================
____________________________ TestHand.test_size[5] _____________________________
E       assert 4 == 5
=========================== short test summary info ============================
FAILED {SIZE} - assert 4 == 5
1 failed, 1 passed in 0.03s
"""

# `pytest -q` rerun of the size test alone: it fails again.
RERUN_SIZE_FAILS = f"""\
F                                                                        [100%]
=========================== short test summary info ============================
FAILED {SIZE} - assert 4 == 5
1 failed in 0.02s
"""

ALL_PASS = "..\n2 passed in 0.02s\n"


# ---------------------------------------------------------------------------
# Shared helpers (sandbox/base.py)
# ---------------------------------------------------------------------------


class TestPerTestFields:
    def test_results_and_failures_from_output(self) -> None:
        fields = per_test_fields(FAILING_RUN, passed=False)
        assert [(r.name, r.passed) for r in fields["results"]] == [
            ("tests/test_deck.py::test_deal", True),
            (SHUFFLE, False),
            (SIZE, False),
        ]
        assert fields["failed_tests"] == [SHUFFLE, SIZE]
        assert fields["failure_excerpt"].startswith("=" * 35 + " FAILURES")
        assert "E       assert 4 == 5" in fields["failure_excerpt"]

    def test_results_carry_status_and_first_error_line(self) -> None:
        results = {r.name: r for r in build_results(FAILING_RUN)}
        assert results["tests/test_deck.py::test_deal"].error == ""
        assert results[SHUFFLE].error == "AssertionError: assert [1, 2, 3] != [1, 2, 3]"
        assert results[SIZE].error == "assert 4 == 5"
        dumped = {r.name: r.model_dump() for r in results.values()}
        assert dumped[SHUFFLE]["status"] == "failed"
        assert dumped["tests/test_deck.py::test_deal"]["status"] == "passed"

    def test_collection_error_carries_its_error(self) -> None:
        output = (
            "==================================== ERRORS ====================================\n"
            "____________________ ERROR collecting tests/test_cards.py _____________________\n"
            "E   ModuleNotFoundError: No module named 'cards'\n"
            "=========================== short test summary info ============================\n"
            "ERROR tests/test_cards.py\n"
            "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!\n"
            "=============================== 1 error in 0.12s ===============================\n"
        )
        [result] = build_results(output)
        assert (result.name, result.passed) == ("tests/test_cards.py", False)
        assert result.error == "ModuleNotFoundError: No module named 'cards'"

    def test_results_are_capped_keeping_failures(self) -> None:
        passing = "".join(f"tests/t.py::test_{i} PASSED\n" for i in range(MAX_RESULTS + 50))
        output = f"{passing}tests/t.py::test_bad FAILED\n"
        results = build_results(output)
        assert len(results) == MAX_RESULTS
        assert results[-1].name == "tests/t.py::test_bad"
        assert results[0].name == "tests/t.py::test_0"
        assert len(build_results(output, cap=3)) == 3

    def test_skip_and_xfail_count_as_passing(self) -> None:
        output = "t.py::test_a SKIPPED (no db)\nt.py::test_b XFAIL\nt.py::test_c XPASS\n"
        fields = per_test_fields(output, passed=True)
        assert all(r.passed for r in fields["results"])
        assert fields["failed_tests"] == []

    def test_passing_run_has_no_excerpt(self) -> None:
        output = "tests/test_deck.py::test_deal PASSED [100%]\n1 passed in 0.01s\n"
        assert per_test_fields(output, passed=True)["failure_excerpt"] == ""

    def test_non_pytest_output_has_no_results(self) -> None:
        fields = per_test_fields("--- FAIL: TestFib (0.00s)\nFAIL\tfib\t0.1s\n", passed=False)
        assert fields["results"] == []
        assert "--- FAIL: TestFib" in fields["failure_excerpt"]


class TestRerunCandidates:
    def _candidates(self, **overrides: Any) -> list[str]:
        args: dict[str, Any] = {
            "patch": {},
            "test_commands": ["python -m pytest -v"],
            "passed": False,
            "failed_tests": [SHUFFLE, SIZE],
        }
        args.update(overrides)
        return rerun_candidates(**args)

    def test_failed_pytest_run_is_rerun(self) -> None:
        assert self._candidates() == [SHUFFLE, SIZE]

    def test_bare_pytest_command_counts(self) -> None:
        assert self._candidates(test_commands=["pytest -x tests/"]) == [SHUFFLE, SIZE]

    def test_passed_run_not_rerun(self) -> None:
        assert self._candidates(passed=True) == []

    def test_disabled_by_patch(self) -> None:
        assert self._candidates(patch={"flaky_rerun": False}) == []
        assert self._candidates(patch={"flaky_reruns": 0}) == []

    def test_non_pytest_command_not_rerun(self) -> None:
        assert self._candidates(test_commands=["npm test"]) == []

    def test_multiple_test_commands_not_rerun(self) -> None:
        commands = ["python -m pytest -v", "python -m mypy ."]
        assert self._candidates(test_commands=commands) == []

    def test_no_failed_ids_not_rerun(self) -> None:
        assert self._candidates(failed_tests=[]) == []

    def test_too_many_failures_not_rerun(self) -> None:
        ids = [f"tests/t.py::test_{i}" for i in range(MAX_RERUN_TESTS + 1)]
        assert self._candidates(failed_tests=ids) == []
        assert self._candidates(failed_tests=ids[:MAX_RERUN_TESTS]) == ids[:MAX_RERUN_TESTS]

    @pytest.mark.parametrize(
        "bad_id",
        [
            "tests/test_x.py",  # collection error: module id, no test
            "tests/t.py::test_x[a b]",  # space in parametrize id
            "tests/t.py::test_x;rm -rf /",
            "tests/t.py::test_$(id)",
            "tests/t.py::test_x&&id",
            "../t.py::test_x",  # leaves the workspace
            "tests/../../etc/t.py::test_x",
            "-x.py::test_a",  # would be read as an option
            "-p.py::x",
        ],
    )
    def test_any_unsafe_id_blocks_rerun(self, bad_id: str) -> None:
        assert self._candidates(failed_tests=[SHUFFLE, bad_id]) == []
        plan = plan_rerun({}, ["python -m pytest -v"], False, [SHUFFLE, bad_id])
        assert plan.skipped == "unsafe_id"

    def test_parametrized_id_with_dash_is_accepted(self) -> None:
        assert self._candidates(failed_tests=["tests/t.py::test_x[1-2]"]) == [
            "tests/t.py::test_x[1-2]"
        ]

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"patch": {"flaky_rerun": False}}, "disabled"),
            ({"patch": {"flaky_reruns": 0}}, "disabled"),
            ({"test_commands": ["npm test"]}, "not_pytest"),
            ({"test_commands": ["PYTHONPATH=. pytest"]}, "not_pytest"),
            ({"failed_tests": []}, "no_ids"),
            ({"failed_tests": [f"t.py::test_{i}" for i in range(11)]}, "too_many"),
            ({"passed": True}, ""),
        ],
    )
    def test_skip_reasons(self, overrides: dict[str, Any], reason: str) -> None:
        args: dict[str, Any] = {
            "patch": {},
            "test_commands": ["python -m pytest -v"],
            "passed": False,
            "failed_tests": [SHUFFLE],
            **overrides,
        }
        plan = plan_rerun(**args)
        assert plan.ids == ()
        assert plan.skipped == reason

    @pytest.mark.parametrize(
        ("patch_data", "attempts"),
        [({}, 2), ({"flaky_reruns": 1}, 1), ({"flaky_reruns": 99}, 5), ({"flaky_reruns": "x"}, 2)],
    )
    def test_rerun_count(self, patch_data: dict[str, Any], attempts: int) -> None:
        plan = plan_rerun(patch_data, ["python -m pytest -v"], False, [SHUFFLE])
        assert plan.attempts == attempts


class TestRerunHelpers:
    def test_rerun_args(self) -> None:
        assert pytest_rerun_args([SHUFFLE, SIZE]) == [
            "python",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            SHUFFLE,
            SIZE,
        ]

    @pytest.mark.parametrize(
        ("command", "prefix"),
        [
            ("python -m pytest -v", ["python", "-m", "pytest"]),
            ("pytest -x tests/", ["pytest"]),
            ("python3 -m pytest", ["python3", "-m", "pytest"]),
            (".venv/bin/pytest -q", [".venv/bin/pytest"]),
            ("uv run pytest", ["uv", "run", "pytest"]),
            ("npm test", None),
            ("PYTHONPATH=. pytest", None),
            ("pytest 'unterminated", None),
        ],
    )
    def test_prefix_mirrors_the_test_command(self, command: str, prefix: list[str] | None) -> None:
        assert pytest_prefix(command) == prefix

    def test_bare_pytest_reruns_with_bare_pytest(self) -> None:
        """A rerun via ``python -m`` would add cwd to sys.path and pass import failures."""
        plan = plan_rerun({}, ["pytest -x"], False, [SHUFFLE])
        assert pytest_rerun_args(plan.ids, plan.prefix)[:2] == ["pytest", "-q"]

    def test_all_pass_on_rerun(self) -> None:
        assert flaky_from_rerun([SHUFFLE, SIZE], 0, ALL_PASS) == [SHUFFLE, SIZE]

    def test_some_still_failing(self) -> None:
        assert flaky_from_rerun([SHUFFLE, SIZE], 1, RERUN_ONE_STILL_FAILING) == [SHUFFLE]

    def test_unparseable_failure_proves_nothing(self) -> None:
        assert flaky_from_rerun([SHUFFLE], 1, "Segmentation fault") == []

    @pytest.mark.parametrize("returncode", [2, 4, 5, None])
    def test_errors_and_timeouts_prove_nothing(self, returncode: int | None) -> None:
        assert flaky_from_rerun([SHUFFLE], returncode, "ERROR: not found") == []


class TestFlakyTolerated:
    """``tolerate_preexisting_flaky``: only flakes in files this run did not write."""

    _ON: ClassVar[dict[str, Any]] = {"tolerate_preexisting_flaky": True, "run_files": ["deck.py"]}

    def test_preexisting_flake_tolerated(self) -> None:
        assert flaky_tolerated(self._ON, [SHUFFLE], [SHUFFLE], failures=1)
        assert flaky_tolerated({"tolerate_flaky": True, "run_files": []}, [SHUFFLE], [SHUFFLE])

    def test_not_without_opt_in(self) -> None:
        assert not flaky_tolerated({"run_files": []}, [SHUFFLE], [SHUFFLE])

    def test_not_when_a_failure_was_not_flaky(self) -> None:
        assert not flaky_tolerated(self._ON, [SHUFFLE, SIZE], [SHUFFLE])
        assert not flaky_tolerated(self._ON, [], [])

    @pytest.mark.parametrize(
        "patch_data",
        [
            {"run_files": ["tests/test_deck.py"]},  # written by an earlier task of this run
            {"run_files": [], "files": {"tests/test_deck.py": ""}},  # written by this patch
            {"run_files": ["./tests\\test_deck.py"]},  # same path, other spelling
        ],
    )
    def test_never_for_tests_the_run_wrote(self, patch_data: dict[str, Any]) -> None:
        patch_data = {"tolerate_preexisting_flaky": True, **patch_data}
        assert not flaky_tolerated(patch_data, [SHUFFLE], [SHUFFLE])

    def test_unknown_ownership_is_not_tolerated(self) -> None:
        assert not flaky_tolerated({"tolerate_preexisting_flaky": True}, [SHUFFLE], [SHUFFLE])

    def test_never_for_errors(self) -> None:
        outcomes = {SHUFFLE: "error"}
        assert not flaky_tolerated(self._ON, [SHUFFLE], [SHUFFLE], outcomes=outcomes)
        # A failure count above the failed ids means errors the ids do not show.
        assert not flaky_tolerated(self._ON, [SHUFFLE], [SHUFFLE], failures=2)


class TestEntrypointHelpers:
    def test_paths_filtered_and_deduplicated(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text("print('hi')\n")
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "cli.py").write_text("")
        patch_data = {
            "entrypoints": [
                "main.py",
                "pkg/cli.py",
                "main.py",
                "missing.py",
                "../outside.py",
                "/etc/passwd.py",
                "-c.py",
                "pkg/../main.py",
                "notes.txt",
                42,
            ]
        }
        assert entrypoint_paths(patch_data, tmp_path) == ["main.py", "pkg/cli.py"]

    def test_written_paths_count_as_existing(self, tmp_path: Path) -> None:
        patch_data = {"entrypoints": ["deal.py", "missing.py"]}
        assert entrypoint_paths(patch_data, tmp_path, written=["./deal.py"]) == ["deal.py"]

    def test_no_entrypoints(self, tmp_path: Path) -> None:
        assert entrypoint_paths({}, tmp_path) == []
        assert entrypoint_paths({"entrypoints": None}, tmp_path) == []

    def test_run_keeps_stdout_head_and_stderr_tail(self) -> None:
        run = entrypoint_run("main.py", 1, "a" * 1500 + "b" * 1500, "c" * 1500 + "d" * 1500)
        assert run.returncode == 1
        assert run.stdout_head == "a" * 1500 + "b" * 500
        assert run.stderr_tail == "c" * 500 + "d" * 1500

    def test_timed_out_run_has_no_returncode(self) -> None:
        run = entrypoint_run("main.py", 124, "tick\n", "", timed_out=True)
        assert run.timed_out
        assert run.returncode is None
        assert run.stdout_head == "tick\n"


# ---------------------------------------------------------------------------
# Docker sandbox (subprocess.run stubbed)
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _is_gate(cmd: list[str]) -> bool:
    return "--name" in cmd and "-gate-" in cmd[cmd.index("--name") + 1]


def _docker_stub(
    calls: list[list[str]],
    *,
    test: _Result,
    rerun: _Result | BaseException | list[_Result] | None = None,
    entrypoint: _Result | BaseException | None = None,
    install: _Result | None = None,
    docker_os: str = "linux",
) -> Any:
    """Fake ``subprocess.run`` routing ``docker run`` by the script it runs.

    *rerun* may be a list: one answer per rerun, in order.
    """
    reruns = list(rerun) if isinstance(rerun, list) else None

    def _run(cmd: list[str], **_kwargs: Any) -> _Result:
        calls.append(list(cmd))
        if cmd[:2] == ["docker", "version"]:
            return _Result(stdout=f"{docker_os}\n")
        if cmd[:2] == ["docker", "image"]:
            return _Result(stdout="100000000\n")
        if cmd[:2] != ["docker", "run"]:
            return _Result()
        script = cmd[-1]
        if _is_gate(cmd) or "--label" not in cmd or script.startswith("command -v "):
            return _Result()  # parse-gate and toolchain-probe containers
        response: Any
        if "no:cacheprovider" in script:
            response = reruns.pop(0) if reruns is not None else rerun
        elif "timeout 10 python" in script:
            response = entrypoint
        elif install is not None and "-install-" in cmd[cmd.index("--name") + 1]:
            response = install
        else:
            response = test
        if isinstance(response, BaseException):
            raise response
        assert response is not None, f"unexpected docker run: {script}"
        return response

    return _run


def _docker_runs(calls: list[list[str]], marker: str) -> list[list[str]]:
    return [c for c in calls if c[:2] == ["docker", "run"] and not _is_gate(c) and marker in c[-1]]


def _container_setup(cmd: list[str]) -> list[str]:
    """A ``docker run`` command up to its script, without the per-container name."""
    name = cmd.index("--name")
    return cmd[:name] + cmd[name + 2 : -1]


_PATCH_FILES = {
    "main.py": "print('dealt 5 cards')\n",
    "tests/test_deck.py": "def test_deal():\n    pass\n",
}


def _run_docker(
    tmp_path: Path,
    patch_data: dict[str, Any],
    image: str = "python:3.12",
    test_commands: list[str] | None = None,
    install_commands: list[str] | None = None,
    **stub: Any,
) -> tuple[dict[str, Any], list[list[str]]]:
    sandbox = DockerSandbox(
        workspace_root=tmp_path,
        image=image,
        test_commands=test_commands,
        install_commands=install_commands,
    )
    calls: list[list[str]] = []
    with patch.object(subprocess, "run", side_effect=_docker_stub(calls, **stub)):
        report = sandbox._apply_and_test_sync({"files": dict(_PATCH_FILES), **patch_data})
        sandbox.cleanup()  # while subprocess.run is still stubbed
    return report, calls


class TestDockerFlakyRerun:
    def test_reruns_only_the_still_failing_tests(self, tmp_path: Path) -> None:
        """The #77 main case: shuffle passes on the first rerun, size fails twice."""
        report, calls = _run_docker(
            tmp_path,
            {},
            test=_Result(stdout=FAILING_RUN, returncode=1),
            rerun=[
                _Result(stdout=RERUN_ONE_STILL_FAILING, returncode=1),
                _Result(stdout=RERUN_SIZE_FAILS, returncode=1),
            ],
        )
        assert report["passed"] is False
        assert report["failed_tests"] == [SHUFFLE, SIZE]
        assert report["flaky_tests"] == [SHUFFLE]
        assert report["rerun_attempts"] == 2
        assert report["rerun_skipped"] == ""
        assert report["_sandbox"]["rerun_attempts"] == 2

        [test_run] = _docker_runs(calls, "pytest -v")
        first, second = _docker_runs(calls, "no:cacheprovider")
        # Same container setup as the test phase: everything up to the script.
        assert _container_setup(first) == _container_setup(test_run)
        assert first[first.index("--network") + 1] == "none"
        assert ". /workspace/.venv/bin/activate" in first[-1]
        assert first[-1].endswith(f"python -m pytest -q -p no:cacheprovider {SHUFFLE} '{SIZE}'")
        assert second[-1].endswith(f"python -m pytest -q -p no:cacheprovider '{SIZE}'")

    def test_all_passing_on_the_first_rerun_reruns_once(self, tmp_path: Path) -> None:
        report, calls = _run_docker(
            tmp_path,
            {},
            test=_Result(stdout=FAILING_RUN, returncode=1),
            rerun=_Result(stdout=ALL_PASS, returncode=0),
        )
        assert len(_docker_runs(calls, "no:cacheprovider")) == 1
        assert report["flaky_tests"] == [SHUFFLE, SIZE]
        assert report["rerun_attempts"] == 1
        assert report["passed"] is False  # raw by default

    def test_bare_pytest_command_reruns_with_bare_pytest(self, tmp_path: Path) -> None:
        report, calls = _run_docker(
            tmp_path,
            {"flaky_reruns": 1},
            test_commands=["pytest -x"],
            test=_Result(stdout=FAILING_RUN, returncode=1),
            rerun=_Result(stdout=ALL_PASS, returncode=0),
        )
        [rerun] = _docker_runs(calls, "no:cacheprovider")
        assert rerun[-1].endswith(f"&& pytest -q -p no:cacheprovider {SHUFFLE} '{SIZE}'")
        assert "-m pytest -q" not in rerun[-1]
        assert report["rerun_attempts"] == 1

    def test_too_many_failures_are_not_rerun(self, tmp_path: Path) -> None:
        many = "".join(f"FAILED tests/t.py::test_{i} - assert 0\n" for i in range(11))
        report, calls = _run_docker(
            tmp_path, {}, test=_Result(stdout=many + "11 failed in 0.1s\n", returncode=1)
        )
        assert not _docker_runs(calls, "no:cacheprovider")
        assert report["rerun_skipped"] == "too_many"
        assert report["rerun_attempts"] == 0

    def test_tolerates_a_preexisting_flake(self, tmp_path: Path) -> None:
        report, _ = _run_docker(
            tmp_path,
            {
                "files": {"main.py": "print('x')\n"},  # the run did not write tests/test_deck.py
                "run_files": ["main.py"],
                "tolerate_preexisting_flaky": True,
            },
            test=_Result(stdout=FAILING_RUN, returncode=1),
            rerun=_Result(stdout=ALL_PASS, returncode=0),
        )
        assert report["flaky_tests"] == [SHUFFLE, SIZE]
        assert report["passed"] is True
        assert report["raw_passed"] is False
        assert report["passed_with_flaky"] is True

    def test_never_tolerates_a_flake_the_run_wrote(self, tmp_path: Path) -> None:
        report, _ = _run_docker(
            tmp_path,
            {"run_files": ["tests/test_deck.py"], "tolerate_preexisting_flaky": True},
            test=_Result(stdout=FAILING_RUN, returncode=1),
            rerun=_Result(stdout=ALL_PASS, returncode=0),
        )
        assert report["flaky_tests"] == [SHUFFLE, SIZE]
        assert report["passed"] is False
        assert not report.get("passed_with_flaky")

    def test_tolerate_keeps_real_failures(self, tmp_path: Path) -> None:
        report, _ = _run_docker(
            tmp_path,
            {"run_files": [], "files": {"main.py": ""}, "tolerate_preexisting_flaky": True},
            test=_Result(stdout=FAILING_RUN, returncode=1),
            rerun=[
                _Result(stdout=RERUN_ONE_STILL_FAILING, returncode=1),
                _Result(stdout=RERUN_SIZE_FAILS, returncode=1),
            ],
        )
        assert report["passed"] is False

    def test_flaky_rerun_disabled(self, tmp_path: Path) -> None:
        report, calls = _run_docker(
            tmp_path,
            {"flaky_rerun": False},
            test=_Result(stdout=FAILING_RUN, returncode=1),
        )
        assert not _docker_runs(calls, "no:cacheprovider")
        assert report["flaky_tests"] == []
        assert report["failed_tests"] == [SHUFFLE, SIZE]
        assert report["rerun_skipped"] == "disabled"

    def test_rerun_timeout_reports_no_flakes(self, tmp_path: Path) -> None:
        report, calls = _run_docker(
            tmp_path,
            {},
            test=_Result(stdout=FAILING_RUN, returncode=1),
            rerun=subprocess.TimeoutExpired("docker", 600),
        )
        assert report["flaky_tests"] == []
        assert report["failed_tests"] == [SHUFFLE, SIZE]
        assert len(_docker_runs(calls, "no:cacheprovider")) == 1  # no second try

    def test_per_test_fields_use_untruncated_output(self, tmp_path: Path) -> None:
        passing = "".join(
            f"tests/test_many.py::test_{i} PASSED{' ' * 40}[ 50%]\n" for i in range(300)
        )
        stdout = FAILING_RUN.replace("collected 3 items\n", f"collected 303 items\n{passing}")
        report, _ = _run_docker(
            tmp_path,
            {"flaky_rerun": False},
            test=_Result(stdout=stdout, returncode=1),
        )
        assert len(report["stdout"]) == 5000
        assert report["output_truncated"] is True
        assert len(report["results"]) == 303
        first = report["results"][0]
        assert (first["name"], first["passed"], first["error"]) == (
            "tests/test_many.py::test_0",
            True,
            "",
        )
        failing = next(r for r in report["results"] if r["name"] == SIZE)
        assert failing["error"] == "assert 4 == 5"
        assert "E       AssertionError" in report["failure_excerpt"]

    def test_passing_run_is_not_rerun(self, tmp_path: Path) -> None:
        report, calls = _run_docker(
            tmp_path,
            {},
            test=_Result(stdout="tests/test_deck.py::test_deal PASSED [100%]\n1 passed\n"),
        )
        assert report["passed"] is True
        assert report["failure_excerpt"] == ""
        assert report["output_truncated"] is False
        assert report["rerun_skipped"] == ""
        assert not _docker_runs(calls, "no:cacheprovider")


class TestDockerEntrypoints:
    def test_entrypoints_run_in_test_container(self, tmp_path: Path) -> None:
        report, calls = _run_docker(
            tmp_path,
            {"entrypoints": ["main.py", "missing.py"]},
            test=_Result(stdout="1 passed\n"),
            entrypoint=_Result(stdout="dealt 5 cards\n"),
        )
        [test_run] = _docker_runs(calls, "pytest -v")
        [run] = _docker_runs(calls, "timeout 10 python")
        assert _container_setup(run) == _container_setup(test_run)
        assert run[-1].endswith("timeout 10 python main.py </dev/null")
        assert report["entrypoint_runs"] == [
            {
                "file": "main.py",
                "returncode": 0,
                "timed_out": False,
                "stdout_head": "dealt 5 cards\n",
                "stderr_tail": "",
            }
        ]
        assert report["passed"] is True

    def test_entrypoint_failure_does_not_change_passed(self, tmp_path: Path) -> None:
        report, _ = _run_docker(
            tmp_path,
            {"entrypoints": ["main.py"]},
            test=_Result(stdout="1 passed\n"),
            entrypoint=_Result(stderr="Traceback ...\nNameError: x\n", returncode=1),
        )
        assert report["passed"] is True
        [run] = report["entrypoint_runs"]
        assert run["returncode"] == 1
        assert run["stderr_tail"].endswith("NameError: x\n")

    def test_entrypoint_timeout_exit_code(self, tmp_path: Path) -> None:
        report, _ = _run_docker(
            tmp_path,
            {"entrypoints": ["main.py"]},
            test=_Result(stdout="1 passed\n"),
            entrypoint=_Result(stdout="Choose a card: ", returncode=124),
        )
        [run] = report["entrypoint_runs"]
        assert run["timed_out"] is True
        assert run["returncode"] is None
        assert run["stdout_head"] == "Choose a card: "

    def test_container_hang_recorded_as_timeout(self, tmp_path: Path) -> None:
        report, _ = _run_docker(
            tmp_path,
            {"entrypoints": ["main.py"]},
            test=_Result(stdout="1 passed\n"),
            entrypoint=subprocess.TimeoutExpired("docker", 70),
        )
        assert report["entrypoint_runs"][0]["timed_out"] is True

    def test_no_entrypoints_no_runs(self, tmp_path: Path) -> None:
        report, calls = _run_docker(tmp_path, {}, test=_Result(stdout="1 passed\n"))
        assert not _docker_runs(calls, "timeout 10 python")
        assert report["entrypoint_runs"] == []

    def test_no_entrypoint_run_when_the_parse_gate_fails(self, tmp_path: Path) -> None:
        blocked = ParseCheckResult(errors=[ParseError("main.py", "Python", "invalid syntax")])
        with patch("delegatron.sandbox.docker_runner.run_parse_checks", return_value=blocked):
            report, calls = _run_docker(
                tmp_path,
                {"entrypoints": ["main.py"]},
                test=_Result(stdout="1 passed\n"),
                entrypoint=_Result(stdout="ran\n"),
            )
        assert report["passed"] is False
        assert report["stderr"].startswith("Parse-check gate failed:")
        assert not _docker_runs(calls, "timeout 10 python")
        assert report["entrypoint_runs"] == []

    def test_no_entrypoint_run_when_the_install_fails(self, tmp_path: Path) -> None:
        report, calls = _run_docker(
            tmp_path,
            {"entrypoints": ["main.py"]},
            install_commands=["pip install nopkg -q"],
            install=_Result(stderr="No matching distribution found for nopkg", returncode=1),
            test=_Result(stdout="1 passed\n"),
            entrypoint=_Result(stdout="ran\n"),
        )
        assert report["passed"] is False
        assert report["stderr"].startswith("Dependency install failed:")
        assert not _docker_runs(calls, "timeout 10 python")
        assert not _docker_runs(calls, "pytest -v")


def test_docker_windows_image_skips_followups(tmp_path: Path) -> None:
    """Follow-up scripts are bash, so Windows containers get neither follow-up."""
    report, calls = _run_docker(
        tmp_path,
        {"entrypoints": ["main.py"]},
        image="mcr.microsoft.com/windows/servercore:ltsc2022",
        docker_os="windows",
        test=_Result(stdout=FAILING_RUN, returncode=1),
    )
    sandbox_runs = [
        c for c in calls if c[:2] == ["docker", "run"] and "--label" in c and not _is_gate(c)
    ]
    assert sandbox_runs == _docker_runs(calls, "pytest")
    assert len(_docker_runs(calls, "pytest")) == 1
    assert report["failed_tests"] == [SHUFFLE, SIZE]
    assert report["flaky_tests"] == []
    assert report["rerun_skipped"] == "windows_image"
    assert report["entrypoint_runs"] == []


# ---------------------------------------------------------------------------
# Local sandbox
# ---------------------------------------------------------------------------


def _local_sandbox(tmp_path: Path, **kwargs: Any) -> LocalSandbox:
    sandbox = LocalSandbox(
        workspace_root=tmp_path,
        test_commands=kwargs.pop("test_commands", ["python -m pytest -v"]),
        install_commands=[],
        **kwargs,
    )
    sandbox._ensure_venv = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return sandbox


class TestLocalFlakyRerun:
    async def test_reruns_only_the_still_failing_tests(self, tmp_path: Path) -> None:
        sandbox = _local_sandbox(tmp_path)
        sandbox._run = AsyncMock(return_value=(1, FAILING_RUN, ""))  # type: ignore[method-assign]
        sandbox._exec = AsyncMock(  # type: ignore[method-assign]
            side_effect=[(1, RERUN_ONE_STILL_FAILING, ""), (1, RERUN_SIZE_FAILS, "")]
        )

        report = await sandbox.apply_and_test({})

        first, second = sandbox._exec.await_args_list
        assert first.args == (pytest_rerun_args([SHUFFLE, SIZE]), 300)
        assert second.args == (pytest_rerun_args([SIZE]), 300)
        assert report["flaky_tests"] == [SHUFFLE]
        assert report["rerun_attempts"] == 2
        assert report["passed"] is False
        assert report["exit_code"] == 1
        assert report["sandbox"] == "local"

    async def test_rerun_mirrors_a_bare_pytest_command(self, tmp_path: Path) -> None:
        sandbox = _local_sandbox(tmp_path, test_commands=["pytest -x"])
        sandbox._run = AsyncMock(return_value=(1, FAILING_RUN, ""))  # type: ignore[method-assign]
        sandbox._exec = AsyncMock(return_value=(0, ALL_PASS, ""))  # type: ignore[method-assign]

        await sandbox.apply_and_test({})

        argv, _ = sandbox._exec.await_args.args
        assert argv == ["pytest", "-q", "-p", "no:cacheprovider", SHUFFLE, SIZE]

    async def test_tolerates_only_preexisting_flakes(self, tmp_path: Path) -> None:
        sandbox = _local_sandbox(tmp_path)
        sandbox._run = AsyncMock(return_value=(1, FAILING_RUN, ""))  # type: ignore[method-assign]
        sandbox._exec = AsyncMock(return_value=(0, ALL_PASS, ""))  # type: ignore[method-assign]

        report = await sandbox.apply_and_test(
            {"tolerate_preexisting_flaky": True, "run_files": ["deck.py"]}
        )
        assert report["flaky_tests"] == [SHUFFLE, SIZE]
        assert (report["passed"], report["raw_passed"], report["passed_with_flaky"]) == (
            True,
            False,
            True,
        )

        owned = await sandbox.apply_and_test(
            {"tolerate_preexisting_flaky": True, "run_files": ["tests/test_deck.py"]}
        )
        assert owned["passed"] is False

    async def test_rerun_disabled(self, tmp_path: Path) -> None:
        sandbox = _local_sandbox(tmp_path)
        sandbox._run = AsyncMock(return_value=(1, FAILING_RUN, ""))  # type: ignore[method-assign]
        sandbox._exec = AsyncMock()  # type: ignore[method-assign]

        report = await sandbox.apply_and_test({"flaky_rerun": False})

        sandbox._exec.assert_not_awaited()
        assert report["passed"] is False
        assert report["failed_tests"] == [SHUFFLE, SIZE]
        assert report["flaky_tests"] == []
        assert report["rerun_skipped"] == "disabled"


def _script(tmp_path: Path, name: str, source: str) -> str:
    (tmp_path / name).write_text(source, encoding="utf-8")
    return name


def _gone(pid: int) -> bool:
    """Whether process *pid* has exited (it may take a moment after a kill)."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            if str(pid) not in out:
                return True
        else:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
        time.sleep(0.2)
    return False


class TestLocalEntrypoints:
    async def test_not_run_without_opt_in(self, tmp_path: Path) -> None:
        _script(tmp_path, "deal.py", "print('dealt')\n")
        sandbox = _local_sandbox(tmp_path)
        sandbox._run = AsyncMock(return_value=(0, "1 passed\n", ""))  # type: ignore[method-assign]
        sandbox._exec = AsyncMock()  # type: ignore[method-assign]

        report = await sandbox.apply_and_test({"entrypoints": ["deal.py"]})

        sandbox._exec.assert_not_awaited()
        assert report["entrypoint_runs"] == []

    async def test_run_with_opt_in(self, tmp_path: Path) -> None:
        _script(tmp_path, "deal.py", "print('dealt')\n")
        sandbox = _local_sandbox(tmp_path, run_entrypoints=True)
        sandbox._run = AsyncMock(return_value=(0, "1 passed\n", ""))  # type: ignore[method-assign]
        sandbox._exec = AsyncMock(return_value=(0, "dealt\n", ""))  # type: ignore[method-assign]

        report = await sandbox.apply_and_test({"entrypoints": ["deal.py"]})

        sandbox._exec.assert_awaited_once_with(["python", "deal.py"], 10)
        assert report["entrypoint_runs"][0]["stdout_head"] == "dealt\n"

    async def test_timeout_keeps_partial_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(local_runner, "ENTRYPOINT_TIMEOUT", 1)
        _script(
            tmp_path,
            "hang.py",
            "import sys, time\nprint('tick', flush=True)\nsys.stdin.read()\ntime.sleep(60)\n",
        )
        sandbox = _local_sandbox(tmp_path, run_entrypoints=True)
        sandbox._env = lambda: {**os.environ, "PATH": os.path.dirname(sys.executable)}  # type: ignore[method-assign]

        started = time.monotonic()
        result = await sandbox._run_entrypoint("hang.py")

        assert time.monotonic() - started < 15
        assert result.timed_out is True
        assert result.returncode is None
        assert result.stdout_head.strip() == "tick"

    async def test_timeout_kills_the_whole_process_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A server the script starts holds the output pipes; it must not hang the run."""
        monkeypatch.setattr(local_runner, "ENTRYPOINT_TIMEOUT", 1)
        _script(
            tmp_path,
            "serve.py",
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(60)\n",
        )
        sandbox = _local_sandbox(tmp_path, run_entrypoints=True)
        sandbox._env = lambda: {**os.environ, "PATH": os.path.dirname(sys.executable)}  # type: ignore[method-assign]

        started = time.monotonic()
        result = await sandbox._run_entrypoint("serve.py")

        assert time.monotonic() - started < 20
        assert result.timed_out is True
        grandchild = int(result.stdout_head.split()[0])
        assert _gone(grandchild)

    async def test_real_pytest_rerun_and_entrypoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One cheap end-to-end run: a flaky test, a real failure and a script."""
        flaky_test = (
            "from pathlib import Path\n\n\n"
            "def test_flaky():\n"
            "    marker = Path(__file__).with_name('ran_once')\n"
            "    first_run = not marker.exists()\n"
            "    marker.write_text('x')\n"
            "    assert not first_run\n\n\n"
            "def test_broken():\n"
            "    assert 1 == 2\n\n\n"
            "def test_ok():\n"
            "    pass\n"
        )
        # ``python`` is this interpreter, not whichever comes first on PATH.  (The
        # command keeps the bare name: a rerun only mirrors plain program names.)
        path = os.environ.get("PATH", "")
        monkeypatch.setenv("PATH", os.path.dirname(sys.executable) + os.pathsep + path)
        sandbox = _local_sandbox(tmp_path, run_entrypoints=True)

        report = await sandbox.apply_and_test(
            {
                "files": {
                    "tests/test_game.py": flaky_test,
                    "deal.py": "print('dealt 5 cards')\n",
                },
                "entrypoints": ["deal.py"],
            }
        )

        flaky_id = "tests/test_game.py::test_flaky"
        broken_id = "tests/test_game.py::test_broken"
        assert report["passed"] is False
        assert report["failed_tests"] == [flaky_id, broken_id]
        assert report["flaky_tests"] == [flaky_id]
        assert report["rerun_attempts"] == 2  # the broken test was rerun twice
        assert [r["name"] for r in report["results"]] == [
            flaky_id,
            broken_id,
            "tests/test_game.py::test_ok",
        ]
        broken = next(r for r in report["results"] if r["name"] == broken_id)
        assert broken["error"] == "assert 1 == 2"
        assert "assert 1 == 2" in report["failure_excerpt"]
        [run] = report["entrypoint_runs"]
        assert run["file"] == "deal.py"
        assert run["returncode"] == 0
        assert run["stdout_head"].strip() == "dealt 5 cards"
        assert not list(tmp_path.rglob("__pycache__"))  # no bytecode left in the repo
