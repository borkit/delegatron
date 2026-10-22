"""Tests for work-plan normalization."""

from __future__ import annotations

import copy
import logging
from typing import Any

import pytest

from delegatron.agents.providers.mock import MOCK_WORK_PLAN
from delegatron.orchestrator.fanout import build_execution_waves
from delegatron.orchestrator.plan_normalize import normalize_plan, normalize_plan_changes
from delegatron.schemas.work_plan import WorkPlan

# Tasks of run 6f29441bbe8c's work_plan.json (descriptions verbatim).
RUN_PLAN: dict[str, Any] = {
    "summary": "Simulate the initial card selection process in a Pokémon card game using Python.",
    "tasks": [
        {
            "task_id": "t1",
            "title": "Define Card and Deck Structures",
            "description": (
                "Create classes for `Card` and `Deck`. The `Card` class should include "
                "attributes like name, type, and any other relevant properties. The `Deck` "
                "class should manage a collection of cards, including shuffling and dealing."
            ),
            "outputs": ["card_deck.py"],
            "risk_level": "low",
            "depends_on": [],
        },
        {
            "task_id": "t2",
            "title": "Implement Deck Shuffling and Dealing",
            "description": (
                "Add methods to the `Deck` class for shuffling the cards and dealing a "
                "specified number of cards to each player. Ensure that the deck is shuffled "
                "before dealing."
            ),
            "outputs": ["card_deck.py"],
            "risk_level": "low",
            "depends_on": ["t1"],
        },
        {
            "task_id": "t3",
            "title": "Simulate Initial Card Selection",
            "description": (
                "Write a script to simulate the initial card selection process. Create a "
                "deck, shuffle it, and deal cards to two players. Print each player's hand."
            ),
            "outputs": ["simulate_deal.py", "test_simulate_deal.py"],
            "risk_level": "low",
            "depends_on": ["t2"],
        },
    ],
    "acceptance": {"criteria": ["The deck can be shuffled and dealt."], "tests": ["unittest"]},
    "termination": {"done_when": ["All tasks pass their tests"]},
}

# The run's initial file tree.
RUN_TREE = [".gitignore", "reviews/review-1/summary.json"]


def _plan(*tasks: dict[str, Any]) -> dict[str, Any]:
    return {"summary": "s", "tasks": list(tasks)}


def _task(task_id: str, outputs: list[str], depends_on: list[str] | None = None) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "title": f"Title {task_id}",
        "description": f"Do {task_id}.",
        "outputs": outputs,
        "risk_level": "low",
        "depends_on": depends_on or [],
    }


def _by_id(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["task_id"]: t for t in plan["tasks"]}


class TestRunPlan:
    """normalize_plan on run 6f29441bbe8c's plan."""

    def test_redundant_t2_merged_into_t1(self) -> None:
        plan, warnings = normalize_plan(RUN_PLAN, RUN_TREE)
        tasks = _by_id(plan)
        assert list(tasks) == ["t1", "t3"]
        t1_description = RUN_PLAN["tasks"][0]["description"]
        t2_description = RUN_PLAN["tasks"][1]["description"]
        assert tasks["t1"]["description"] == f"{t1_description}\n\n{t2_description}"
        assert tasks["t3"]["depends_on"] == ["t1"]
        assert any("t2" in w and "merged into t1" in w for w in warnings)

    def test_t1_gains_its_test_file(self) -> None:
        plan, _ = normalize_plan(RUN_PLAN, RUN_TREE)
        assert _by_id(plan)["t1"]["outputs"] == ["card_deck.py", "tests/test_card_deck.py"]

    def test_root_test_output_moves_under_tests(self) -> None:
        plan, warnings = normalize_plan(RUN_PLAN, RUN_TREE)
        assert _by_id(plan)["t3"]["outputs"] == ["simulate_deal.py", "tests/test_simulate_deal.py"]
        assert any("test_simulate_deal.py to tests/test_simulate_deal.py" in w for w in warnings)

    def test_other_plan_fields_kept(self) -> None:
        plan, _ = normalize_plan(RUN_PLAN, RUN_TREE)
        assert plan["acceptance"] == RUN_PLAN["acceptance"]
        assert plan["summary"] == RUN_PLAN["summary"]

    def test_input_not_mutated(self) -> None:
        original = copy.deepcopy(RUN_PLAN)
        normalize_plan(RUN_PLAN, RUN_TREE)
        assert original == RUN_PLAN

    def test_idempotent(self) -> None:
        once, _ = normalize_plan(RUN_PLAN, RUN_TREE)
        twice, warnings = normalize_plan(once, RUN_TREE)
        assert twice == once
        assert warnings == []

    def test_change_kinds(self) -> None:
        """The issue's run: exactly these four kinds of change (#78)."""
        plan, changes = normalize_plan_changes(RUN_PLAN, [".gitignore"])

        assert {change["kind"] for change in changes} == {
            "test_path_moved",
            "redundant_task_merged",
            "dependency_rewired",
            "test_output_added",
        }
        [rewired] = [c for c in changes if c["kind"] == "dependency_rewired"]
        assert (rewired["task_id"], rewired["before"], rewired["after"]) == ("t3", ["t2"], ["t1"])
        [added] = [c for c in changes if c["kind"] == "test_output_added"]
        assert (added["task_id"], added["after"]) == ("t1", "tests/test_card_deck.py")
        assert [t["task_id"] for t in plan["tasks"]] == ["t1", "t3"]

    def test_result_validates_as_a_work_plan(self) -> None:
        plan, _ = normalize_plan(RUN_PLAN, RUN_TREE)
        assert WorkPlan.model_validate(plan).tasks[0].outputs == [
            "card_deck.py",
            "tests/test_card_deck.py",
        ]

    def test_waves_resolve_without_fallback(self, caplog: pytest.LogCaptureFixture) -> None:
        plan, _ = normalize_plan(RUN_PLAN, RUN_TREE)
        caplog.set_level(logging.WARNING, logger="delegatron.orchestrator.fanout")

        waves = build_execution_waves(plan["tasks"])

        assert [[t["task_id"] for t in wave] for wave in waves] == [["t1"], ["t3"]]
        assert "Cannot resolve dependencies" not in caplog.text

    def test_no_merge_keeps_every_task_but_fixes_paths(self) -> None:
        """A re-plan with completed tasks never merges pending work into them (#78)."""
        plan, warnings = normalize_plan(RUN_PLAN, RUN_TREE, merge_redundant=False)
        tasks = _by_id(plan)
        assert list(tasks) == ["t1", "t2", "t3"]
        assert tasks["t3"]["outputs"] == ["simulate_deal.py", "tests/test_simulate_deal.py"]
        assert not any("merged" in warning for warning in warnings)


class TestTestPaths:
    """Test-output mapping and insertion."""

    def test_root_test_kept_when_already_in_tree(self) -> None:
        plan, warnings = normalize_plan(
            _plan(_task("t1", ["app.py", "test_app.py"])), ["app.py", "test_app.py"]
        )
        assert plan["tasks"][0]["outputs"] == ["app.py", "test_app.py"]
        assert warnings == []

    def test_root_test_maps_to_existing_nested_file(self) -> None:
        plan, _ = normalize_plan(
            _plan(_task("t1", ["app.py", "test_app.py"])), ["tests\\unit\\test_app.py"]
        )
        assert plan["tasks"][0]["outputs"] == ["app.py", "tests/unit/test_app.py"]

    def test_duplicate_after_mapping_is_dropped(self) -> None:
        plan, _ = normalize_plan(
            _plan(_task("t1", ["app.py", "test_app.py", "tests/test_app.py"])), []
        )
        assert plan["tasks"][0]["outputs"] == ["app.py", "tests/test_app.py"]

    def test_added_test_reuses_existing_root_test(self) -> None:
        plan, _ = normalize_plan(_plan(_task("t1", ["card_deck.py"])), ["test_card_deck.py"])
        assert plan["tasks"][0]["outputs"] == ["card_deck.py", "test_card_deck.py"]

    def test_added_test_named_after_first_module(self) -> None:
        plan, _ = normalize_plan(
            _plan(_task("t1", ["src/pkg/__init__.py", "src/pkg/conftest.py", "src/pkg/core.py"])),
            [],
        )
        assert plan["tasks"][0]["outputs"][-1] == "tests/test_core.py"

    def test_task_with_any_test_output_unchanged(self) -> None:
        tasks = [
            _task("t1", ["app.py", "tests/helpers.py"]),
            _task("t2", ["web/app.js", "web/app.test.js"]),
            _task("t3", ["pkg/lib.py", "pkg/lib_test.py"]),
        ]
        plan, warnings = normalize_plan(_plan(*tasks), [])
        assert plan["tasks"] == tasks
        assert warnings == []

    def test_root_suffix_test_output_moves_under_tests(self) -> None:
        plan, warnings = normalize_plan(_plan(_task("t1", ["lib.py", "lib_test.py"])), [])
        assert plan["tasks"][0]["outputs"] == ["lib.py", "tests/lib_test.py"]
        assert warnings == ["t1: moved test output lib_test.py to tests/lib_test.py"]

    def test_root_layout_is_kept(self) -> None:
        """A project with root-level tests and no tests/ keeps writing tests at the root."""
        tree = ["mod.py", "test_mod.py"]
        tasks = [_task("t1", ["mod2.py", "test_new.py"]), _task("t2", ["util.py"])]

        plan, warnings = normalize_plan(_plan(*tasks), tree)

        assert plan["tasks"][0]["outputs"] == ["mod2.py", "test_new.py"]
        assert plan["tasks"][1]["outputs"] == ["util.py", "test_util.py"]
        assert warnings == ["t2: added test output test_util.py"]

    def test_tests_dir_wins_over_root_tests(self) -> None:
        tree = ["mod.py", "test_mod.py", "tests/test_other.py"]

        plan, _ = normalize_plan(_plan(_task("t1", ["mod2.py", "test_new.py"])), tree)

        assert plan["tasks"][0]["outputs"] == ["mod2.py", "tests/test_new.py"]

    def test_non_python_outputs_untouched(self) -> None:
        tasks = [_task("t1", ["src/Counter.tsx", "src/Counter.test.tsx", "README.md"])]
        plan, warnings = normalize_plan(_plan(*tasks), [])
        assert plan["tasks"] == tasks
        assert warnings == []

    def test_test_file_listed_by_another_task_is_not_added(self) -> None:
        """Two tasks must not own one test file (blind test regeneration, #78)."""
        tasks = [
            _task("t1", ["src/app/health.py"]),
            _task("t2", ["tests/test_health.py"], ["t1"]),
        ]

        plan, warnings = normalize_plan(_plan(*tasks), [], merge_redundant=False)

        assert plan["tasks"][0]["outputs"] == ["src/app/health.py"]
        assert warnings == []

    def test_conftest_alone_is_not_a_test(self) -> None:
        plan, _ = normalize_plan(_plan(_task("t1", ["app.py", "tests/conftest.py"])), [])
        assert plan["tasks"][0]["outputs"] == ["app.py", "tests/conftest.py", "tests/test_app.py"]

    def test_non_python_task_gets_no_python_test(self) -> None:
        plan, warnings = normalize_plan(_plan(_task("t1", ["README.md", "web/app.js"])), [])
        assert plan["tasks"][0]["outputs"] == ["README.md", "web/app.js"]
        assert warnings == []


class TestRedundantTasks:
    """Merging tasks whose outputs a dependency already produces."""

    def test_dependents_repointed_and_deduplicated(self) -> None:
        plan, _ = normalize_plan(
            _plan(
                _task("t1", ["a.py"]),
                _task("t2", ["a.py"], ["t1"]),
                _task("t3", ["b.py"], ["t1", "t2"]),
            ),
            [],
        )
        assert _by_id(plan)["t3"]["depends_on"] == ["t1"]

    def test_chain_of_redundant_tasks_collapses(self) -> None:
        plan, _ = normalize_plan(
            _plan(
                _task("t1", ["a.py"]),
                _task("t2", ["a.py"], ["t1"]),
                _task("t3", ["a.py"], ["t2"]),
            ),
            [],
        )
        assert list(_by_id(plan)) == ["t1"]
        assert plan["tasks"][0]["description"] == "Do t1.\n\nDo t2.\n\nDo t3."

    def test_mock_work_plan_is_unchanged(self) -> None:
        """MOCK_WORK_PLAN's test-only t2 is never merged, nor its test file stolen (#78)."""
        plan, warnings = normalize_plan(MOCK_WORK_PLAN, [])

        assert plan == MOCK_WORK_PLAN
        assert warnings == []

    def test_test_only_task_is_never_merged(self) -> None:
        tasks = [_task("t1", ["a.py", "tests/test_a.py"]), _task("t2", ["tests/test_a.py"], ["t1"])]

        plan, _ = normalize_plan(_plan(*tasks), [])

        assert list(_by_id(plan)) == ["t1", "t2"]

    def test_merge_compares_source_outputs_only(self) -> None:
        """B's tests do not block the merge; A takes the union and the higher risk."""
        tasks = [
            {**_task("t1", ["a.py"]), "risk_level": "medium"},
            {**_task("t2", ["a.py", "tests/test_a_more.py"], ["t1"]), "risk_level": "high"},
        ]

        plan, changes = normalize_plan_changes(_plan(*tasks), [])

        [t1] = plan["tasks"]
        assert t1["outputs"] == ["a.py", "tests/test_a_more.py"]
        assert t1["risk_level"] == "high"
        assert [c["kind"] for c in changes] == ["redundant_task_merged"]

    def test_not_merged_when_its_sources_are_not_a_subset(self) -> None:
        tasks = [_task("t1", ["a.py", "tests/test_a.py"]), _task("t2", ["a.py", "b.py"], ["t1"])]
        plan, _ = normalize_plan(_plan(*tasks), [])
        assert list(_by_id(plan)) == ["t1", "t2"]

    def test_task_adding_outputs_is_kept(self) -> None:
        plan, _ = normalize_plan(
            _plan(_task("t1", ["a.py"]), _task("t2", ["a.py", "b.py"], ["t1"])), []
        )
        assert list(_by_id(plan)) == ["t1", "t2"]

    def test_unrelated_tasks_with_same_outputs_kept(self) -> None:
        plan, _ = normalize_plan(_plan(_task("t1", ["a.py"]), _task("t2", ["a.py"])), [])
        assert list(_by_id(plan)) == ["t1", "t2"]

    def test_task_without_outputs_kept(self) -> None:
        plan, _ = normalize_plan(_plan(_task("t1", ["a.py"]), _task("t2", [], ["t1"])), [])
        assert list(_by_id(plan)) == ["t1", "t2"]

    def test_not_merged_when_it_would_reorder_dependencies(self) -> None:
        """t3 also needs t2, which t1 does not wait for: merging would reorder."""
        plan, _ = normalize_plan(
            _plan(
                _task("t1", ["a.py"]),
                _task("t2", ["b.py"]),
                _task("t3", ["a.py"], ["t1", "t2"]),
            ),
            [],
        )
        assert list(_by_id(plan)) == ["t1", "t2", "t3"]

    def test_merged_when_other_dependencies_already_precede(self) -> None:
        plan, _ = normalize_plan(
            _plan(
                _task("t0", ["base.py"]),
                _task("t1", ["a.py"], ["t0"]),
                _task("t2", ["a.py"], ["t1", "t0"]),
            ),
            [],
        )
        assert list(_by_id(plan)) == ["t0", "t1"]

    def test_mutual_dependency_keeps_one_task(self) -> None:
        plan, _ = normalize_plan(
            _plan(_task("t1", ["a.py"], ["t2"]), _task("t2", ["a.py"], ["t1"])), []
        )
        assert len(plan["tasks"]) == 1
        assert plan["tasks"][0]["depends_on"] == []


class TestPathHygiene:
    """Output paths become safe, clean relative posix paths (#78 step 1)."""

    def test_paths_are_cleaned_and_unsafe_ones_dropped(self) -> None:
        outputs = [
            "src\\a.py",
            "./b.py",
            "b.py",
            "/etc/passwd",
            "../x.py",
            "C:\\abs.py",
            "tests/test_a.py",
        ]

        plan, changes = normalize_plan_changes(_plan(_task("t1", outputs)), [])

        assert plan["tasks"][0]["outputs"] == ["src/a.py", "b.py", "tests/test_a.py"]
        kinds = [c["kind"] for c in changes]
        assert kinds.count("output_dropped") == 3
        assert "output_normalized" in kinds

    def test_clean_paths_report_nothing(self) -> None:
        tasks = [_task("t1", ["src/a.py", "tests/test_a.py"])]
        plan, warnings = normalize_plan(_plan(*tasks), [])
        assert plan["tasks"] == tasks
        assert warnings == []

    def test_cleaned_plan_is_idempotent(self) -> None:
        once, _ = normalize_plan(_plan(_task("t1", ["src\\a.py", "../x.py"])), ["src\\b.py"])
        twice, warnings = normalize_plan(once, ["src\\b.py"])
        assert twice == once
        assert warnings == []


class TestEdgeCases:
    def test_plan_without_tasks(self) -> None:
        assert normalize_plan({"summary": "s"}, []) == ({"summary": "s"}, [])
        assert normalize_plan({"summary": "s", "tasks": []}, []) == (
            {"summary": "s", "tasks": []},
            [],
        )


class TestTaskIds:
    """Unique, non-empty task ids and resolvable dependencies (#99)."""

    def test_duplicate_and_empty_ids_become_unique(self) -> None:
        tasks = [_task("t1", ["a.md"]), _task("t1", ["b.md"]), _task("", ["c.md"])]

        plan, warnings = normalize_plan(_plan(*tasks), [])

        assert [t["task_id"] for t in plan["tasks"]] == ["t1", "t2", "t3"]
        assert len(warnings) == 2

    def test_new_ids_skip_ids_already_taken(self) -> None:
        tasks = [_task("t2", ["a.md"]), _task("t2", ["b.md"]), _task("t1", ["c.md"])]

        plan, _ = normalize_plan(_plan(*tasks), [])

        assert [t["task_id"] for t in plan["tasks"]] == ["t2", "t3", "t1"]

    def test_ids_are_stripped(self) -> None:
        plan, _ = normalize_plan(_plan(_task(" t1 ", ["a.md"]), _task("t2", ["b.md"], ["t1"])), [])

        assert plan["tasks"][0]["task_id"] == "t1"
        assert plan["tasks"][1]["depends_on"] == ["t1"]

    def test_dependencies_are_stripped(self) -> None:
        """A dependency written as ' t1 ' resolves to t1 and keeps the order (#99)."""
        tasks = [_task("t2", ["b.md"], [" t1 "]), _task("t1", ["a.md"])]

        plan, warnings = normalize_plan(_plan(*tasks), [])

        assert plan["tasks"][0]["depends_on"] == ["t1"]
        assert warnings == []
        waves = build_execution_waves(plan["tasks"])
        assert [[t["task_id"] for t in wave] for wave in waves] == [["t1"], ["t2"]]

    def test_unknown_dependencies_leave_waves_resolvable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        tasks = [_task("t1", ["a.md"], ["t1", "ghost"]), _task("t2", ["b.md"], ["t1", "t9"])]
        caplog.set_level(logging.WARNING, logger="delegatron.orchestrator.fanout")

        plan, _ = normalize_plan(_plan(*tasks), [])
        waves = build_execution_waves(plan["tasks"])

        assert [[t["task_id"] for t in wave] for wave in waves] == [["t1"], ["t2"]]
        assert "Cannot resolve dependencies" not in caplog.text

    def test_self_and_unknown_dependencies_dropped(self) -> None:
        tasks = [_task("t1", ["a.md"]), _task("t2", ["b.md"], ["t2", "t9", "t1", "t1"])]

        plan, warnings = normalize_plan(_plan(*tasks), [])

        assert plan["tasks"][1]["depends_on"] == ["t1"]
        assert any("t9" in w for w in warnings)

    def test_reference_to_a_duplicate_id_resolves_to_its_first_holder(self) -> None:
        tasks = [_task("t1", ["a.md"]), _task("t1", ["b.md"]), _task("t3", ["c.md"], ["t1"])]

        plan, _ = normalize_plan(_plan(*tasks), [])

        assert plan["tasks"][2]["depends_on"] == ["t1"]

    def test_normalized_ids_are_stable(self) -> None:
        tasks = [_task("t1", ["a.md"]), _task("t1", ["b.md"], ["t1", "zz"]), _task("", ["c.md"])]
        once, _ = normalize_plan(_plan(*tasks), [])

        twice, warnings = normalize_plan(once, [])

        assert twice == once
        assert warnings == []
