"""Tests for orchestrator/suite_guard.py — test paths, removed tests, known failures."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from delegatron.config import Config, PipelineConfig, RoleConfig
from delegatron.orchestrator.graph import Pipeline
from delegatron.orchestrator.suite_guard import (
    attributable_failures,
    excuse_known_failures,
    failing_test_ids,
    is_pytest_file,
    is_test_path,
    node_file,
    passing_test_ids,
    removed_tests,
    sibling_test_path,
    strip_known_tests,
)

# Run 6f29441bbe8c: t1 accepted six tests; t2's pass 2 rewrote the file with two.
OLD_TESTS = """\
import unittest


class TestCard(unittest.TestCase):
    def test_card_creation(self):
        pass

    def test_card_representation(self):
        pass


class TestDeck(unittest.TestCase):
    def setUp(self):
        self.deck = list(range(10))

    def test_deck_initialization(self):
        pass

    def test_shuffle(self):
        pass

    def test_deal(self):
        pass

    def test_deal_all_cards(self):
        pass
"""

NEW_TESTS = """\
import unittest


class TestDeck(unittest.TestCase):
    def setUp(self):
        self.deck = list(range(3))

    def test_shuffle(self):
        pass

    def test_deal(self):
        pass
"""

FILE = "tests/test_card_deck.py"
ACCEPTED = [
    f"{FILE}::TestCard::test_card_creation",
    f"{FILE}::TestCard::test_card_representation",
    f"{FILE}::TestDeck::test_deal",
    f"{FILE}::TestDeck::test_deal_all_cards",
    f"{FILE}::TestDeck::test_deck_initialization",
    f"{FILE}::TestDeck::test_shuffle",
]
REMOVED_NAMES = [
    "TestCard::test_card_creation",
    "TestCard::test_card_representation",
    "TestDeck::test_deal_all_cards",
    "TestDeck::test_deck_initialization",
]


def _results(*names: str, passed: bool = True) -> list[dict[str, Any]]:
    return [{"name": name, "passed": passed} for name in names]


class TestIsTestPath:
    @pytest.mark.parametrize(
        "path",
        [
            "tests/test_calc.py",
            "test_calc.py",
            "calc_test.py",
            "conftest.py",
            "tests/conftest.py",
            "tests/__init__.py",
            "tests/helpers.py",
            "tests/fixtures/data.json",
            "src/Counter.test.tsx",
            "src/a.spec.ts",
            "src/__tests__/X.tsx",
            "pkg/calc/calc_test.go",
            "tests/integration.rs",
            "src/test/java/FooTest.java",
            "spec/foo_spec.rb",
            "tests/FooTests.cs",
            "tests\\test_calc.py",
            "./tests/test_calc.py",
            "test/unit/app.js",
            "scripts/Deploy.Tests.ps1",
            "shop/tests.py",
            "tests.py",
            "App.Tests/UnitTest1.cs",
            "src/App.UnitTests/Helpers.cs",
            "Tests/MyLibTests/MyLibTests.swift",
            "MyLibTests/ParserTest.swift",
            "Sources/Tests/x.swift",
        ],
    )
    def test_test_paths(self, path: str) -> None:
        assert is_test_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "src/calc.py",
            "src/Counter.tsx",
            "calc.go",
            "src/main/java/Foo.java",
            "src/attest.py",
            "src/myapp/spec/openapi.py",
            "lib/test/helper.rb",
            "Contest.java",
            "contests/views.py",
            "Sources/MyLib/Parser.swift",
            "App/Program.cs",
            "shop/testing.py",
        ],
    )
    def test_source_paths(self, path: str) -> None:
        assert is_test_path(path) is False

    def test_pytest_collects_only_its_own_file_names(self) -> None:
        assert is_pytest_file("tests/test_calc.py")
        assert is_pytest_file("calc_test.py")
        assert not is_pytest_file("shop/tests.py")
        assert not is_pytest_file("tests/conftest.py")
        assert not is_pytest_file("tests/helpers.py")


class TestRemovedTests:
    def test_rewrite_to_two_tests_reports_the_four_removed(self) -> None:
        assert removed_tests(ACCEPTED, {FILE: NEW_TESTS}, None) == [
            {"file": FILE, "removed": REMOVED_NAMES}
        ]

    def test_restored_file_reports_nothing(self) -> None:
        assert removed_tests(ACCEPTED, {FILE: OLD_TESTS}, None) == []

    def test_deleted_file_reports_every_test(self) -> None:
        [entry] = removed_tests(ACCEPTED, {FILE: None}, None)
        assert len(entry["removed"]) == 6

    def test_unparsable_rewrite_reports_every_test(self) -> None:
        [entry] = removed_tests(ACCEPTED, {FILE: "def test_(:\n"}, None)
        assert len(entry["removed"]) == 6

    def test_accepted_id_missing_from_the_run_is_reported(self) -> None:
        report = {"passed": True, "results": _results(*ACCEPTED[1:])}

        assert removed_tests(ACCEPTED, {}, report) == [
            {"file": FILE, "removed": ["TestCard::test_card_creation"]}
        ]

    def test_a_failing_accepted_test_is_not_removed(self) -> None:
        report = {"passed": False, "results": _results(*ACCEPTED, passed=False)}

        assert removed_tests(ACCEPTED, {FILE: OLD_TESTS}, report) == []

    @pytest.mark.parametrize(
        "report",
        [
            {"passed": True, "results": []},
            {"passed": False, "skipped_reason": "non_testable_language", "results": []},
            None,
        ],
    )
    def test_runs_without_results_skip_the_runtime_check(self, report: Any) -> None:
        assert removed_tests(ACCEPTED, {}, report) == []

    def test_a_file_that_errored_at_collection_is_not_reported_by_the_run(self) -> None:
        report = {"passed": False, "results": _results(FILE, passed=False)}

        assert removed_tests(ACCEPTED, {}, report) == []

    def test_another_files_collection_error_reports_nothing_removed(self) -> None:
        """pytest stops the whole session on a collection error: no test of t1 ran (#70)."""
        report = {
            "passed": False,
            "stdout": COLLECTION_ERROR_OUTPUT,
            "results": _results("tests/test_b.py", passed=False),
        }

        assert removed_tests(ACCEPTED, {FILE: OLD_TESTS}, report) == []
        # The static check still catches tests really deleted from disk.
        assert removed_tests(ACCEPTED, {FILE: NEW_TESTS}, report) == [
            {"file": FILE, "removed": REMOVED_NAMES}
        ]

    def test_interrupted_or_truncated_run_skips_the_runtime_check(self) -> None:
        interrupted = {
            "passed": False,
            "stdout": "!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!\n",
            "results": _results(*ACCEPTED[1:]),
        }
        truncated = {
            "passed": False,
            "stdout": f"============ test session starts ============\n{ACCEPTED[1]} PASSED\n",
            "results": _results(*ACCEPTED[1:]),
        }
        for report in (interrupted, truncated):
            assert removed_tests(ACCEPTED, {FILE: OLD_TESTS}, report) == []

    def test_complete_run_missing_an_accepted_id_is_reported(self) -> None:
        report = {
            "passed": True,
            "stdout": "============ test session starts ============\n"
            + "".join(f"{nodeid} PASSED\n" for nodeid in ACCEPTED[1:])
            + "============ 5 passed in 0.10s ============\n",
            "results": _results(*ACCEPTED[1:]),
        }

        assert removed_tests(ACCEPTED, {FILE: OLD_TESTS}, report) == [
            {"file": FILE, "removed": ["TestCard::test_card_creation"]}
        ]

    def test_non_python_accepted_tests_are_ignored(self) -> None:
        """Only Python test files are guarded (graph.py reads only .py sources)."""
        accepted = ["web/app.test.js::adds numbers"]

        assert removed_tests(accepted, {}, None) == []
        assert removed_tests(accepted, {}, {"passed": True, "results": []}) == []

    def test_parametrized_ids_match_their_function(self) -> None:
        accepted = ["tests/test_p.py::test_x[1]", "tests/test_p.py::test_x[2]"]
        source = (
            "import pytest\n\n\n@pytest.mark.parametrize('n', [1, 2])\ndef test_x(n):\n    pass\n"
        )

        assert removed_tests(accepted, {"tests/test_p.py": source}, None) == []

    def test_idempotent(self) -> None:
        report = {"passed": True, "results": _results(*ACCEPTED[2:])}
        first = removed_tests(ACCEPTED, {FILE: NEW_TESTS}, report)

        assert removed_tests(ACCEPTED, {FILE: NEW_TESTS}, report) == first


class TestStripKnownTests:
    def test_subset_rewrite_keeps_nothing(self) -> None:
        known = {name.partition("::")[2] for name in ACCEPTED}
        assert strip_known_tests(NEW_TESTS, known) is None

    def test_only_the_new_test_remains_and_parses(self) -> None:
        source = NEW_TESTS + "\n    def test_deal_two_hands(self):\n        pass\n"
        known = {"TestDeck::test_shuffle", "TestDeck::test_deal"}

        stripped = strip_known_tests(source, known)

        assert stripped is not None
        tree = ast.parse(stripped)
        [cls] = [node for node in tree.body if isinstance(node, ast.ClassDef)]
        assert [m.name for m in cls.body] == ["setUp", "test_deal_two_hands"]

    def test_top_level_tests_and_emptied_classes(self) -> None:
        source = (
            "def test_old():\n    pass\n\n\ndef test_new():\n    pass\n\n\n"
            "class TestGone:\n    def test_a(self):\n        pass\n"
        )

        stripped = strip_known_tests(source, {"test_old", "TestGone::test_a"})

        assert stripped is not None
        assert "test_new" in stripped
        assert "test_old" not in stripped
        assert "TestGone" not in stripped

    def test_unparsable_source(self) -> None:
        assert strip_known_tests("def test_(:\n", set()) is None

    def test_nested_class_tests_are_stripped(self) -> None:
        """Nested classes chain with ``::`` as extract_test_functions names them (#70)."""
        source = (
            "class TestOuter:\n"
            "    class TestInner:\n"
            "        def test_old(self):\n            pass\n\n"
            "        def test_new(self):\n            pass\n\n"
            "    class TestGone:\n"
            "        def test_a(self):\n            pass\n"
        )
        known = {"TestOuter::TestInner::test_old", "TestOuter::TestGone::test_a"}

        stripped = strip_known_tests(source, known)

        assert stripped is not None
        assert "test_new" in stripped
        assert "test_old" not in stripped
        assert "TestGone" not in stripped
        assert strip_known_tests(source, known | {"TestOuter::TestInner::test_new"}) is None


class TestSiblingTestPath:
    @pytest.mark.parametrize(
        ("path", "task_id", "sibling"),
        [
            ("tests/test_card_deck.py", "t2", "tests/test_card_deck_t2.py"),
            ("deck_test.py", "t2", "deck_t2_test.py"),
            ("tests/test_x.py", "task 3", "tests/test_x_task_3.py"),
        ],
    )
    def test_sibling(self, path: str, task_id: str, sibling: str) -> None:
        assert sibling_test_path(path, task_id) == sibling


class TestKnownFailures:
    def test_failing_ids_from_report_fields_and_output(self) -> None:
        assert failing_test_ids({"passed": True}) == frozenset()
        assert failing_test_ids({"passed": False, "failed_tests": ["a.py::t"]}) == {"a.py::t"}
        summary = (
            "FAILED tests/test_a.py::test_x - AssertionError: no\n"
            "========================= 1 failed, 2 passed in 0.05s =========================\n"
        )
        assert failing_test_ids({"passed": False, "stdout": summary}) == {"tests/test_a.py::test_x"}

    def test_ids_from_the_last_5000_chars_of_the_output(self) -> None:
        """Sandboxes keep only the output tail; the short summary survives (#104)."""
        tail = REALISTIC_FAILURE_OUTPUT[-5000:]
        assert "test session starts" not in tail

        assert failing_test_ids({"passed": False, "stdout": tail, "exit_code": 1}) == {
            "tests/test_a.py::test_x",
            "tests/test_a.py::test_y[2]",
        }

    def test_collection_error_is_unattributable(self) -> None:
        """Real pytest output for an import error in one test file (#104)."""
        report = {"passed": False, "stdout": COLLECTION_ERROR_OUTPUT, "exit_code": 2}
        assert failing_test_ids(report) is None
        # Even with the module id parsed into failed_tests and no exit code.
        report = {
            "passed": False,
            "stdout": COLLECTION_ERROR_OUTPUT,
            "failed_tests": ["tests/test_b.py"],
        }
        assert failing_test_ids(report) is None
        assert failing_test_ids({"passed": False, "failed_tests": ["tests/test_b.py"]}) is None

    def test_incomplete_or_inflated_runs_are_unattributable(self) -> None:
        progress = "tests/test_a.py::test_x FAILED\n"
        # The ids are still reported (agent feedback) ...
        assert failing_test_ids({"passed": False, "stdout": progress}) == {
            "tests/test_a.py::test_x"
        }
        # ... but a session that timed out before its result line explains nothing:
        # later tests never ran.
        assert attributable_failures({"passed": False, "stdout": progress}) is None
        # pytest counted more failures than the ids found.
        inflated = (
            progress + "==================== 3 failed, 1 passed in 0.10s ====================\n"
        )
        assert attributable_failures({"passed": False, "stdout": inflated}) is None
        complete = (
            progress + "==================== 1 failed, 1 passed in 0.10s ====================\n"
        )
        assert attributable_failures({"passed": False, "stdout": complete}) == {
            "tests/test_a.py::test_x"
        }

    def test_another_test_command_failing_is_unattributable(self) -> None:
        """A second command (mypy, npm test) may be what failed (#104)."""
        tail = REALISTIC_FAILURE_OUTPUT[-3000:]
        after = tail + "src/app.py:3: error: Incompatible return value type\nFound 1 error\n"
        assert attributable_failures({"passed": False, "stdout": after}) is None
        ids = ["tests/test_a.py::test_x", "tests/test_a.py::test_y[2]"]
        report = {"passed": False, "stdout": tail, "failed_tests": ids}
        assert (
            attributable_failures(report, test_commands=["python -m pytest -v", "mypy ."]) is None
        )
        assert attributable_failures(report, test_commands=["python -m pytest && npm test"]) is None
        assert attributable_failures({**report, "test_commands": ["npm test"]}) is None
        assert attributable_failures({**report, "exit_code": 127}) is None
        assert attributable_failures(report, test_commands=["cd app && pytest -q"]) == set(ids)
        assert attributable_failures({**report, "exit_code": 1}) == set(ids)
        assert attributable_failures({"passed": True}) == frozenset()
        assert attributable_failures(None) is None

    @pytest.mark.parametrize(
        "report",
        [
            None,
            {"passed": False, "stderr": "pip install failed"},
            {"passed": False, "total": 0, "skipped_reason": "non_testable_language"},
        ],
    )
    def test_unattributable_failures(self, report: Any) -> None:
        assert failing_test_ids(report) is None

    def test_only_known_failures_pass_the_task(self) -> None:
        report = {"passed": False, "failed_tests": ["tests/test_a.py::test_x"]}

        split = excuse_known_failures(report, {"tests/test_a.py::test_x": "t1"}, set())

        assert split == {
            "new_failures": [],
            "baseline_failures": ["tests/test_a.py::test_x"],
            "task_passed": True,
        }

    def test_a_new_failure_still_fails_the_task(self) -> None:
        report = {"passed": False, "failed_tests": ["tests/test_a.py::test_x", "tests/b.py::t"]}

        split = excuse_known_failures(report, {"tests/test_a.py::test_x": "t1"}, set())

        assert split is not None
        assert split["new_failures"] == ["tests/b.py::t"]
        assert split["task_passed"] is False

    def test_failures_in_the_current_tasks_files_are_not_excused(self) -> None:
        report = {"passed": False, "failed_tests": ["tests/test_a.py::test_x"]}
        known = {"tests/test_a.py::test_x": "t1"}

        assert excuse_known_failures(report, known, {"tests/test_a.py"}) is None

    def test_unattributable_failures_are_not_excused(self) -> None:
        report = {"passed": False, "stderr": "collection error"}

        assert excuse_known_failures(report, {"tests/test_a.py::test_x": "t1"}, set()) is None

    def test_a_skipped_tasks_collection_error_is_never_excused(self) -> None:
        """t1 left tests/test_a.py failing to import: no later run can be excused (#104)."""
        report = {"passed": False, "stdout": COLLECTION_ERROR_OUTPUT}
        known = {"tests/test_b.py": "t1", "tests/test_b.py::test_y": "t1"}

        assert excuse_known_failures(report, known, set()) is None

    def test_a_failing_second_command_is_not_masked(self) -> None:
        report = {"passed": False, "failed_tests": ["tests/test_a.py::test_x"]}
        known = {"tests/test_a.py::test_x": "t1"}

        split = excuse_known_failures(
            report, known, set(), test_commands=["python -m pytest -v", "npm test"]
        )

        assert split is None
        assert excuse_known_failures(report, known, set(), test_commands=["pytest"]) == {
            "new_failures": [],
            "baseline_failures": ["tests/test_a.py::test_x"],
            "task_passed": True,
        }

    def test_targeted_failure_on_a_new_id_fails_the_task(self) -> None:
        """Scratch D: t2's targeted run failed on its own test; t2 must not be approved."""
        known = {"tests/test_a.py::test_x": "t1"}
        report = {
            "passed": False,
            "failed_tests": ["tests/test_a.py::test_x"],
            "targeted": {"passed": False, "failing_ids": ["tests/test_b.py::test_y"]},
        }

        split = excuse_known_failures(report, known, set())

        assert split == {
            "new_failures": ["tests/test_b.py::test_y"],
            "baseline_failures": ["tests/test_a.py::test_x"],
            "task_passed": False,
        }

    def test_targeted_failures_must_be_known_too(self) -> None:
        known = {"tests/test_a.py::test_x": "t1"}
        base = {"passed": False, "failed_tests": ["tests/test_a.py::test_x"]}
        only_known = {"passed": False, "failing_ids": ["tests/test_a.py::test_x"]}
        unattributed = {"passed": False, "failing_ids": []}

        split = excuse_known_failures({**base, "targeted": only_known}, known, set())
        assert split is not None and split["task_passed"] is True
        split = excuse_known_failures({**base, "targeted": {"passed": True}}, known, set())
        assert split is not None and split["task_passed"] is True
        split = excuse_known_failures({**base, "targeted": unattributed}, known, set())
        assert split is not None
        assert split["task_passed"] is False
        assert split["new_failures"] == []


def test_node_file_and_passing_ids() -> None:
    assert node_file(f"{FILE}::TestDeck::test_deal") == FILE
    report = {
        "passed": False,
        "results": [*_results("a.py::t1"), *_results("a.py::t2", passed=False)],
    }
    assert passing_test_ids(report) == ["a.py::t1"]
    assert passing_test_ids({**report, "skipped_reason": "x"}) == []


# Real pytest output (-v) for an import error in tests/test_b.py: the whole
# session is interrupted, so no other test ran.
COLLECTION_ERROR_OUTPUT = """\
============================= test session starts =============================
platform linux -- Python 3.12.3, pytest-8.3.2, pluggy-1.5.0 -- /usr/bin/python
rootdir: /workspace
collecting ... collected 6 items / 1 error

=================================== ERRORS ====================================
______________________ ERROR collecting tests/test_b.py _______________________
ImportError while importing test module '/workspace/tests/test_b.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
tests/test_b.py:1: in <module>
    from deal import deal_hands
E   ModuleNotFoundError: No module named 'deal'
=========================== short test summary info ===========================
ERROR tests/test_b.py
!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
============================== 1 error in 0.12s ===============================
"""

# A long -v run with two failures (one parametrized); its head is cut off by
# the sandbox's 5000-character tail.
REALISTIC_FAILURE_OUTPUT = (
    "============================= test session starts =============================\n"
    + "".join(
        f"tests/test_many.py::test_case_{i:03d} PASSED [ {i % 100:2d}%]\n" for i in range(200)
    )
    + "tests/test_a.py::test_x FAILED\n"
    + "tests/test_a.py::test_y[2] FAILED\n"
    + "=================================== FAILURES ===================================\n"
    + "___________________________________ test_x ___________________________________\n"
    + "    def test_x():\n>       assert add(1, 2) == 4\nE       assert 3 == 4\n\n"
    + "tests/test_a.py:5: AssertionError\n"
    + "_________________________________ test_y[2] __________________________________\n"
    + ">       assert n > 2\nE       assert 2 > 2\n\ntests/test_a.py:9: AssertionError\n"
    + "=========================== short test summary info ============================\n"
    + "FAILED tests/test_a.py::test_x - assert 3 == 4\n"
    + "FAILED tests/test_a.py::test_y[2] - assert 2 > 2\n"
    + "======================== 2 failed, 200 passed in 1.52s =========================\n"
)


def _pipeline(root: Path) -> Pipeline:
    config = Config(
        architect=RoleConfig(provider="mock", model="mock-architect"),
        implementer=RoleConfig(provider="mock", model="mock-implementer"),
        reviewer=RoleConfig(provider="mock", model="mock-reviewer"),
        pipeline=PipelineConfig(max_iterations=1, max_cost_usd=1.0, max_wall_time_minutes=1),
    )
    return Pipeline(config=config, workspace_root=root, run_id="suite-guard")


def _preserve_state(root: Path) -> dict[str, Any]:
    return {
        "workspace_root": str(root),
        "current_task": {"task_id": "t2"},
        "_file_history": {},
        "messages": [],
        "iteration": 2,
    }


class TestPreserveExistingTests:
    """The two-pass guard against rewrites that drop accepted tests (#70 AC3)."""

    def test_unparsable_old_file_may_be_overwritten(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        (tmp_path / FILE).write_text("def test_(:\n    broken\n", encoding="utf-8")
        state = _preserve_state(tmp_path)

        kept = _pipeline(tmp_path)._preserve_existing_tests(state, {FILE: NEW_TESTS})  # type: ignore[arg-type]

        assert kept == {FILE: NEW_TESTS}
        assert not [m for m in state["messages"] if m.get("event") == "tests_preserved"]

    def test_non_python_test_files_are_not_guarded(self, tmp_path: Path) -> None:
        old = "test('adds', () => {});\ntest('subtracts', () => {});\n"
        new = "test('adds', () => {});\n"
        (tmp_path / "web").mkdir()
        (tmp_path / "web/app.test.js").write_text(old, encoding="utf-8")
        state = _preserve_state(tmp_path)

        kept = _pipeline(tmp_path)._preserve_existing_tests(state, {"web/app.test.js": new})  # type: ignore[arg-type]

        assert kept == {"web/app.test.js": new}
        assert not [m for m in state["messages"] if m.get("event") == "tests_preserved"]

    def test_a_rewrite_dropping_tests_is_still_redirected(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        (tmp_path / FILE).write_text(OLD_TESTS, encoding="utf-8")
        state = _preserve_state(tmp_path)

        kept = _pipeline(tmp_path)._preserve_existing_tests(state, {FILE: NEW_TESTS})  # type: ignore[arg-type]

        assert kept == {}
        [event] = [m for m in state["messages"] if m.get("event") == "tests_preserved"]
        assert event["dropped_tests"] == REMOVED_NAMES
