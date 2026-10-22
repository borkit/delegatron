"""Evidence the orchestrator derives for the agents: attempt diffs, flaky tests, entrypoints."""

from __future__ import annotations

from pathlib import Path

import pytest

from delegatron.orchestrator.evidence import (
    MAX_ENTRYPOINTS,
    dependency_files,
    flaky_flips,
    flaky_from_history,
    select_entrypoints,
    synthesize_diff,
)

SHUFFLE = "tests/test_card_deck.py::TestDeck::test_shuffle"
DEAL = "tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal"

# simulate_deal.py as run 6f29441bbe8c wrote it: a function, no __main__ guard
SIMULATE_DEAL = (
    "import random\n\n\ndef simulate_deal():\n"
    "    for player in ('Player 1', 'Player 2'):\n        print(player)\n"
)
T3 = {
    "task_id": "t3",
    "title": "Simulate Initial Card Selection",
    "description": "Write a script to deal cards to two players. Print each player's hand.",
    "outputs": ["simulate_deal.py", "test_simulate_deal.py"],
}
T1 = {
    "task_id": "t1",
    "title": "Define Card and Deck Structures",
    "description": "Create Card and Deck classes.",
    "outputs": ["card_deck.py"],
}


def _write(root: Path, files: dict[str, str]) -> None:
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


class TestSynthesizeDiff:
    def test_new_file_diffs_from_dev_null(self) -> None:
        diff = synthesize_diff({"card_deck.py": None}, {"card_deck.py": "class Card:\n    pass\n"})

        assert "--- /dev/null" in diff
        assert "+++ b/card_deck.py" in diff
        assert "+class Card:" in diff

    def test_modified_file_shows_removed_and_added_lines(self) -> None:
        diff = synthesize_diff(
            {"calc.py": "def add(a, b):\n    return a - b\n"},
            {"calc.py": "def add(a, b):\n    return a + b\n"},
        )

        assert "--- a/calc.py" in diff and "+++ b/calc.py" in diff
        assert "-    return a - b" in diff
        assert "+    return a + b" in diff

    def test_deleted_file_diffs_to_dev_null(self) -> None:
        diff = synthesize_diff({"old.py": "x = 1\n"}, {"old.py": None})

        assert "--- a/old.py" in diff and "+++ /dev/null" in diff
        assert "-x = 1" in diff

    def test_unchanged_and_line_ending_only_changes_give_no_hunk(self) -> None:
        diff = synthesize_diff(
            {"same.py": "x = 1\n", "crlf.py": "a = 1\r\nb = 2\r\n", "new.py": None},
            {"same.py": "x = 1\n", "crlf.py": "a = 1\nb = 2\n", "new.py": "y = 2\n"},
        )

        assert "same.py" not in diff
        assert "crlf.py" not in diff
        assert "+++ b/new.py" in diff


class TestFlakyFromHistory:
    def test_flip_on_an_unchanged_tree_is_flaky(self) -> None:
        # Run 6f29441bbe8c it6: test_shuffle passed at it4, failed at it5 and
        # passed at it6 although no file changed after it5.
        history = {SHUFFLE: [[4, "passed"], [5, "failed"]], DEAL: [[5, "failed"]]}
        files = {"simulate_deal.py": {"task_id": "t3", "iteration": 5}}

        flaky = flaky_from_history({SHUFFLE: True, DEAL: False}, history, files)

        assert flaky == [SHUFFLE]

    def test_flip_after_a_file_changed_is_not_flaky(self) -> None:
        history = {SHUFFLE: [[5, "failed"]]}
        files = {"card_deck.py": {"task_id": "t2", "iteration": 6}}

        assert flaky_from_history({SHUFFLE: True}, history, files) == []

    def test_without_a_workspace_any_change_explains_the_flip(self) -> None:
        """No workspace to read imports from: every changed file counts (conservative)."""
        history = {SHUFFLE: [[3, "passed"], [4, "passed"]]}
        files = {"simulate_deal.py": {"task_id": "t3", "iteration": 5}}

        assert flaky_from_history({SHUFFLE: False}, history, files) == []

    def test_failing_after_passing_on_an_unchanged_tree_is_flaky(self) -> None:
        history = {SHUFFLE: [[3, "passed"]]}
        files = {"card_deck.py": {"task_id": "t2", "iteration": 3}}

        assert flaky_from_history({SHUFFLE: False}, history, files) == [SHUFFLE]

    def test_new_or_steady_tests_are_not_flaky(self) -> None:
        history = {DEAL: [[1, "failed"]]}

        assert flaky_from_history({DEAL: False, SHUFFLE: True}, history, {}) == []


# Run 6f29441bbe8c's workspace at it5 (#68): test_shuffle's own files last
# changed at it3; at it5 only simulate_deal.py and its test changed.
IT5_FILES = {
    "card_deck.py": "import random\n\n\nclass Deck:\n    def shuffle(self):\n"
    "        random.shuffle(self.cards)\n",
    "tests/test_card_deck.py": "import unittest\n\nfrom card_deck import Deck\n\n\n"
    "class TestDeck(unittest.TestCase):\n    def test_shuffle(self):\n        pass\n",
    "simulate_deal.py": "from card_deck import Deck\n\n\ndef simulate_deal():\n    pass\n",
    "tests/test_simulate_deal.py": "from simulate_deal import simulate_deal\n\n\n"
    "def test_simulate_deal():\n    pass\n",
}
IT5_FILE_HISTORY = {
    "card_deck.py": {"task_id": "t1", "iteration": 3},
    "tests/test_card_deck.py": {"task_id": "t1", "iteration": 3},
    "simulate_deal.py": {"task_id": "t3", "iteration": 5},
    "tests/test_simulate_deal.py": {"task_id": "t3", "iteration": 5},
}


class TestFlakyOnUnchangedDependencies:
    """Flips judged on the files a test depends on, not on the whole tree (#68)."""

    def test_it5_shuffle_failure_on_unchanged_files_is_reported(self, tmp_path: Path) -> None:
        _write(tmp_path, IT5_FILES)
        history = {SHUFFLE: [[3, "passed"], [4, "passed"]], DEAL: [[4, "failed"]]}

        flips = flaky_flips(
            {SHUFFLE: False, DEAL: False}, history, IT5_FILE_HISTORY, workspace_root=tmp_path
        )

        assert flips == [
            {
                "test": SHUFFLE,
                "file": "tests/test_card_deck.py",
                "outcome": "failed",
                "other_outcome_iteration": 4,
                "files_unchanged_since": 3,
            }
        ]
        assert flaky_from_history(
            {SHUFFLE: False}, history, IT5_FILE_HISTORY, workspace_root=tmp_path
        ) == [SHUFFLE]

    def test_a_change_to_an_imported_module_explains_the_flip(self, tmp_path: Path) -> None:
        _write(tmp_path, IT5_FILES)
        history = {SHUFFLE: [[3, "passed"], [4, "passed"]]}
        files = {**IT5_FILE_HISTORY, "card_deck.py": {"task_id": "t3", "iteration": 5}}

        assert flaky_from_history({SHUFFLE: False}, history, files, workspace_root=tmp_path) == []

    def test_a_conftest_change_explains_the_flip(self, tmp_path: Path) -> None:
        _write(tmp_path, {**IT5_FILES, "tests/conftest.py": "import pytest\n"})
        history = {SHUFFLE: [[4, "passed"]]}
        files = {**IT5_FILE_HISTORY, "tests/conftest.py": {"task_id": "t3", "iteration": 5}}

        assert flaky_from_history({SHUFFLE: False}, history, files, workspace_root=tmp_path) == []

    def test_still_failing_after_an_unexplained_flip_stays_reported(self, tmp_path: Path) -> None:
        """Passed at it3, failed at it4 and it5 on the same files: still nondeterministic."""
        _write(tmp_path, IT5_FILES)
        history = {SHUFFLE: [[3, "passed"], [4, "failed"]]}

        assert flaky_from_history(
            {SHUFFLE: False}, history, IT5_FILE_HISTORY, workspace_root=tmp_path
        ) == [SHUFFLE]

    def test_a_pass_after_any_file_changed_is_no_flip(self, tmp_path: Path) -> None:
        """#77: what made a failing test pass can be any file (a script it runs, a data file)."""
        _write(tmp_path, IT5_FILES)
        history = {SHUFFLE: [[4, "failed"]]}  # only simulate_deal.py changed since, at it5

        assert flaky_from_history({SHUFFLE: True}, history, IT5_FILE_HISTORY) == []
        assert (
            flaky_from_history({SHUFFLE: True}, history, IT5_FILE_HISTORY, workspace_root=tmp_path)
            == []
        )

    def test_a_failure_its_reruns_reproduced_is_no_flip(self, tmp_path: Path) -> None:
        """The sandbox reran it and it never passed: it fails for a reason, not by chance."""
        _write(tmp_path, IT5_FILES)
        history = {SHUFFLE: [[3, "passed"], [4, "passed"]]}

        flips = flaky_flips(
            {SHUFFLE: False},
            history,
            IT5_FILE_HISTORY,
            workspace_root=tmp_path,
            rerun_failed=[SHUFFLE],
        )

        assert flips == []

    def test_dependency_files_follow_imports_and_conftests(self, tmp_path: Path) -> None:
        _write(tmp_path, {**IT5_FILES, "conftest.py": "", "tests/conftest.py": ""})

        deps = dependency_files(tmp_path, "tests/test_simulate_deal.py")

        assert deps[0] == "tests/test_simulate_deal.py"
        assert set(deps) == {
            "tests/test_simulate_deal.py",
            "tests/conftest.py",
            "conftest.py",
            "simulate_deal.py",
            "card_deck.py",
        }
        assert dependency_files(tmp_path, "../outside.py") == []


class TestSelectEntrypoints:
    def test_task_asking_for_a_script_runs_its_output(self, tmp_path: Path) -> None:
        _write(tmp_path, {"simulate_deal.py": SIMULATE_DEAL})

        selected = select_entrypoints(
            T3, ["simulate_deal.py", "tests/test_simulate_deal.py"], tmp_path
        )

        assert selected == ["simulate_deal.py"]

    def test_library_task_runs_nothing(self, tmp_path: Path) -> None:
        _write(tmp_path, {"card_deck.py": "class Card:\n    pass\n"})

        assert select_entrypoints(T1, ["card_deck.py", "tests/test_card_deck.py"], tmp_path) == []

    def test_main_guard_selects_a_module_whatever_the_task_says(self, tmp_path: Path) -> None:
        guarded = "def main():\n    pass\n\n\nif __name__ == '__main__':\n    main()\n"
        in_docstring = '"""Usage:\n\nif __name__ == "__main__":\n    main()\n"""\n'
        _write(tmp_path, {"tool.py": guarded, "doc.py": in_docstring})

        assert select_entrypoints(T1, ["doc.py", "tool.py"], tmp_path) == ["tool.py"]

    @pytest.mark.parametrize(
        "path",
        [
            "tests/run.py",
            "conftest.py",
            "pkg/__init__.py",
            "setup.py",
            "run.sh",
            "test_run.py",
        ],
    )
    def test_tests_and_packaging_files_are_never_run(self, tmp_path: Path, path: str) -> None:
        _write(tmp_path, {path: "if __name__ == '__main__':\n    print(1)\n"})
        task = {**T3, "outputs": [path]}

        assert select_entrypoints(task, [path], tmp_path) == []

    def test_unsafe_or_missing_paths_are_skipped(self, tmp_path: Path) -> None:
        task = {**T3, "outputs": ["../outside.py", "/abs/run.py", "missing.py"]}

        assert select_entrypoints(task, [], tmp_path) == []

    def test_unsafe_own_files_are_skipped(self, tmp_path: Path) -> None:
        """Files the task wrote go through normalize_rel_path like its outputs (#80)."""
        inside = tmp_path / "ws"
        _write(inside, {"tool.py": "if __name__ == '__main__':\n    pass\n"})
        _write(tmp_path, {"outside.py": "if __name__ == '__main__':\n    pass\n"})
        outside = str((tmp_path / "outside.py").resolve())

        selected = select_entrypoints(T1, ["../outside.py", outside, ".\\tool.py"], inside)

        assert selected == ["tool.py"]

    def test_task_asking_for_a_main_program_runs_its_output(self, tmp_path: Path) -> None:
        _write(tmp_path, {"game.py": "def play():\n    pass\n"})
        task = {
            "task_id": "t4",
            "title": "Main game loop",
            "description": "Write the main program for the card game.",
            "outputs": ["game.py"],
        }

        assert select_entrypoints(task, [], tmp_path) == ["game.py"]

    def test_at_most_three_in_stable_order(self, tmp_path: Path) -> None:
        names = [f"s{i}.py" for i in range(5)]
        _write(tmp_path, dict.fromkeys(names, "print(1)\n"))
        task = {**T3, "outputs": names}

        assert select_entrypoints(task, [], tmp_path) == names[:MAX_ENTRYPOINTS]

    def test_planned_root_test_file_written_under_tests_is_no_candidate(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, {"tests/test_simulate_deal.py": "if __name__ == '__main__':\n    pass\n"})

        assert select_entrypoints(T3, ["tests/test_simulate_deal.py"], tmp_path) == []

    def test_output_matched_by_file_name_when_written_elsewhere(self, tmp_path: Path) -> None:
        _write(tmp_path, {"src/simulate_deal.py": SIMULATE_DEAL})

        assert select_entrypoints(T3, ["src/simulate_deal.py"], tmp_path) == [
            "src/simulate_deal.py"
        ]
