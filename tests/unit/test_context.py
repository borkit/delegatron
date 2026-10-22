"""Tests for context management."""

import logging
import posixpath
import random
import re
from pathlib import Path
from typing import Any

import pytest

from delegatron.context import (
    MAX_ROOT_SIBLINGS,
    MAX_TOTAL_SIZE,
    ReviewContext,
    _guess_test_path,
    gather_context,
    gather_repo_map,
    gather_review_context,
    select_context,
    select_review_context,
    select_review_files,
)
from delegatron.orchestrator.suite_guard import is_test_path


def test_gather_context_from_task_outputs(tmp_path: Path):
    """Context should include files listed in task outputs."""
    # Create files
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "app" / "__init__.py").write_text("")
    (tmp_path / "src" / "app" / "health.py").write_text("# health endpoint")

    task = {"outputs": ["src/app/health.py"], "description": "Create health endpoint"}
    result = gather_context(workspace_root=tmp_path, task=task)

    assert "src/app/health.py" in result
    assert result["src/app/health.py"] == "# health endpoint"
    # Should also include __init__.py from the parent
    assert "src/app/__init__.py" in result


def test_gather_context_from_review_feedback(tmp_path: Path):
    """Context should include files referenced in review feedback."""
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "app" / "health.py").write_text("# code here")

    feedback = [
        {
            "severity": "minor",
            "evidence": "src/app/health.py:8",
            "recommended_fix": "Add error handling",
        }
    ]
    result = gather_context(workspace_root=tmp_path, review_feedback=feedback)

    assert "src/app/health.py" in result


def test_gather_context_skips_missing_files(tmp_path: Path):
    """Should not fail on files that don't exist."""
    task = {"outputs": ["nonexistent.py"]}
    result = gather_context(workspace_root=tmp_path, task=task)
    assert len(result) == 0


def test_gather_context_truncates_large_files(tmp_path: Path):
    """Large files are shown as a marked excerpt and reported as partial."""
    (tmp_path / "big.py").write_text("x" * 100_000)
    task = {"outputs": ["big.py"]}
    selection = select_context(workspace_root=tmp_path, task=task)

    assert "big.py" in selection.files
    assert "\n... [truncated " in selection.files["big.py"]
    assert len(selection.files["big.py"]) <= MAX_TOTAL_SIZE
    assert selection.partial == {"big.py": 100_000}


def test_gather_context_includes_test_files(tmp_path: Path):
    """Should include corresponding test files for source files."""
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "app" / "health.py").write_text("# source")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_health.py").write_text("# tests")

    task = {"outputs": ["src/app/health.py"]}
    result = gather_context(workspace_root=tmp_path, task=task)

    assert "src/app/health.py" in result
    assert "tests/test_health.py" in result


def test_guess_test_path():
    assert _guess_test_path("src/app/health.py") == "tests/test_health.py"
    assert _guess_test_path("tests/test_health.py") is None


def test_gather_context_empty_inputs(tmp_path: Path):
    """Should return empty dict with no inputs."""
    result = gather_context(workspace_root=tmp_path)
    assert result == {}


# ---------------------------------------------------------------------------
# Regressions from run 6f29441bbe8c (card deal simulation, local preset)
# ---------------------------------------------------------------------------

_CARD_DECK = (
    "from random import shuffle\n\n\n"
    "class Card:\n"
    "    def __init__(self, name, card_type):\n"
    "        self.name = name\n"
    "        self.card_type = card_type\n\n\n"
    "class Deck:\n"
    "    def __init__(self, cards):\n"
    "        self.cards = cards\n\n"
    "    def shuffle(self):\n"
    "        shuffle(self.cards)\n"
)
_TEST_CARD_DECK = (
    "import unittest\n"
    "from card_deck import Card, Deck\n\n\n"
    "class TestDeck(unittest.TestCase):\n"
    "    def test_shuffle(self):\n"
    "        deck = Deck([Card('Pikachu', 'Electric'), Card('Eevee', 'Normal')])\n"
    "        deck.shuffle()\n"
)
_T3 = {
    "task_id": "t3",
    "title": "Simulate Initial Card Selection",
    "description": (
        "Write a script to simulate the initial card selection process. "
        "Create a deck, shuffle it, and deal cards to two players."
    ),
    "outputs": ["simulate_deal.py", "test_simulate_deal.py"],
    "depends_on": ["t2"],
}
# The run's state kept the file tree from before t1 wrote anything.
_STALE_TREE = [".gitignore"]
_FAILED_SHUFFLE = {
    "passed": False,
    "failed_tests": ["tests/test_card_deck.py::TestDeck::test_shuffle"],
}


def _write(root: Path, files: dict[str, str]) -> None:
    for rel_path, content in files.items():
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _card_workspace(root: Path) -> None:
    """The run workspace after t1/t2: card_deck.py and its tests."""
    _write(
        root,
        {
            ".gitignore": "__pycache__/\n",
            "card_deck.py": _CARD_DECK,
            "tests/test_card_deck.py": _TEST_CARD_DECK,
        },
    )


@pytest.mark.parametrize(
    "file_tree",
    [_STALE_TREE, None, [".gitignore", "card_deck.py", "tests/test_card_deck.py"]],
    ids=["stale", "missing", "fresh"],
)
def test_dependent_task_sees_prior_task_files(tmp_path: Path, file_tree: list[str] | None):
    """t3's outputs do not exist yet; it must still see the Deck it builds on."""
    _card_workspace(tmp_path)

    result = gather_context(
        tmp_path,
        _T3,
        file_tree=file_tree,
        prior_files=["card_deck.py", "tests/test_card_deck.py"],
    )

    assert result["card_deck.py"] == _CARD_DECK
    assert "tests/test_card_deck.py" in result


@pytest.mark.parametrize(
    "test_report",
    [
        _FAILED_SHUFFLE,
        {
            "passed": False,
            "stdout": (
                "tests/test_card_deck.py::TestDeck::test_shuffle FAILED [100%]\n"
                "=========================== short test summary info ===========================\n"
                "FAILED tests/test_card_deck.py::TestDeck::test_shuffle - AssertionError: "
                "[Card(Pikachu)] == [Card(Pikachu)]\n"
            ),
        },
    ],
    ids=["failed_tests", "parsed_stdout"],
)
def test_failing_test_pulls_test_file_and_module_under_test(
    tmp_path: Path, test_report: dict[str, Any]
):
    _card_workspace(tmp_path)

    result = gather_context(tmp_path, _T3, file_tree=_STALE_TREE, test_report=test_report)

    assert list(result)[:2] == ["tests/test_card_deck.py", "card_deck.py"]


def test_review_issue_file_key_pulls_file(tmp_path: Path):
    _write(tmp_path, {"tests/x.py": "X = 1\n"})
    feedback = [{"severity": "major", "title": "Unresolvable import: y", "file": "tests/x.py"}]

    result = gather_context(tmp_path, review_feedback=feedback)

    assert "tests/x.py" in result


@pytest.mark.parametrize(
    "evidence",
    [
        "tests/test_card_deck.py:15",
        "tests/test_card_deck.py:15:9",
        "tests/test_card_deck.py::TestDeck::test_shuffle",
        "`tests\\test_card_deck.py`:15",
        "assertNotEqual fails (see tests/test_card_deck.py:15).",
    ],
)
def test_review_evidence_pulls_test_and_module_under_test(tmp_path: Path, evidence: str):
    """it6: evidence on the Deck test must also bring card_deck.py into view."""
    _card_workspace(tmp_path)
    _write(tmp_path, {"simulate_deal.py": "print('deal')\n"})
    feedback = [
        {
            "severity": "major",
            "title": "Deck shuffle method does not alter card order",
            "evidence": evidence,
            "recommended_fix": "Correct the shuffle algorithm within the Deck class.",
        }
    ]

    result = gather_context(tmp_path, _T3, review_feedback=feedback, file_tree=_STALE_TREE)

    assert "tests/test_card_deck.py" in result
    assert "card_deck.py" in result


def test_oversized_file_is_skipped_and_later_files_still_fit(tmp_path: Path):
    _write(tmp_path, {"a_big.py": "x" * 5_000, "b_small.py": "y = 1\n"})

    result = gather_context(tmp_path, {"outputs": ["a_big.py", "b_small.py"]}, max_total_size=1_000)

    assert list(result) == ["b_small.py"]


def test_stale_file_tree_falls_back_to_disk(tmp_path: Path):
    """A tree missing a referenced on-disk file is stale: root siblings come from disk."""
    _card_workspace(tmp_path)
    _write(tmp_path, {"simulate_deal.py": "print('deal')\n"})

    result = gather_context(tmp_path, {"outputs": ["simulate_deal.py"]}, file_tree=_STALE_TREE)

    assert list(result) == ["simulate_deal.py", "card_deck.py"]


def test_fresh_file_tree_is_normalized_and_respected(tmp_path: Path):
    """Backslash trees (non-git fallback on Windows) work; untracked files stay out."""
    _write(
        tmp_path,
        {
            "src/app/health.py": "# health\n",
            "src/app/main.py": "app = None\n",
            "src/app/untracked.py": "LOCAL = 1\n",
        },
    )
    tree = ["src\\app\\health.py", "src\\app\\main.py"]

    result = gather_context(tmp_path, {"outputs": ["src/app/health.py"]}, file_tree=tree)

    assert list(result) == ["src/app/health.py", "src/app/main.py"]


@pytest.mark.parametrize("test_path", ["tests/test_card_deck.py", "tests/unit/test_card_deck.py"])
def test_root_level_module_includes_its_tests(tmp_path: Path, test_path: str):
    """t2 rewrote tests/test_card_deck.py blind because it never saw it."""
    _write(tmp_path, {"card_deck.py": _CARD_DECK, test_path: _TEST_CARD_DECK})

    result = gather_context(tmp_path, {"outputs": ["card_deck.py"]})

    assert list(result) == ["card_deck.py", test_path]


@pytest.mark.parametrize(
    "source", ["card_deck.py", "src/card_deck.py", "cards/card_deck.py", "src/cards/card_deck.py"]
)
def test_test_file_maps_back_to_its_module(tmp_path: Path, source: str):
    _write(tmp_path, {source: _CARD_DECK, "tests/test_card_deck.py": "def test_x():\n    pass\n"})

    result = gather_context(tmp_path, review_feedback=[{"evidence": "tests/test_card_deck.py:2"}])

    assert source in result


def test_files_are_ordered_by_priority(tmp_path: Path):
    _write(
        tmp_path,
        {
            "simulate_deal.py": "print('deal')\n",
            "tests/test_card_deck.py": _TEST_CARD_DECK,
            "card_deck.py": _CARD_DECK,
            "lib/helpers.py": "def helper():\n    pass\n",
            "lib/other.py": "OTHER = 1\n",
            "util.py": "def util():\n    pass\n",
        },
    )

    result = gather_context(
        tmp_path,
        {"outputs": ["simulate_deal.py"]},
        review_feedback=[{"file": "lib/helpers.py"}],
        prior_files=["util.py"],
        test_report=_FAILED_SHUFFLE,
    )

    assert list(result) == [
        "simulate_deal.py",  # task output
        "tests/test_card_deck.py",  # failing test
        "card_deck.py",  # ... and the module it imports
        "lib/helpers.py",  # cited by review feedback
        "util.py",  # written by an earlier task
        "lib/other.py",  # sibling
    ]


def test_unparseable_failing_test_still_pulls_imported_module(tmp_path: Path):
    _write(
        tmp_path,
        {
            "card_deck.py": _CARD_DECK,
            "tests/test_deal.py": "from card_deck import Deck\n\ndef test_deal(:\n",
        },
    )
    report = {"passed": False, "failed_tests": ["tests/test_deal.py"]}  # collection error

    result = gather_context(tmp_path, test_report=report)

    assert list(result)[:2] == ["tests/test_deal.py", "card_deck.py"]


def test_test_imports_resolve_from_its_directory_and_packages(tmp_path: Path):
    _write(
        tmp_path,
        {
            "tests/__init__.py": "",
            "tests/helpers.py": "def make_deck():\n    pass\n",
            "tests/factories.py": "def card():\n    pass\n",
            "tests/test_deal.py": (
                "import os\n"
                "from helpers import make_deck\n"
                "from .factories import card\n"
                "from pkg import cards\n"
            ),
            "pkg/__init__.py": "",
            "pkg/cards.py": "CARDS = []\n",
        },
    )
    report = {"passed": False, "failed_tests": ["tests/test_deal.py::test_deal"]}

    result = gather_context(tmp_path, test_report=report)

    assert list(result)[:5] == [
        "tests/test_deal.py",
        "tests/helpers.py",
        "tests/factories.py",
        "pkg/__init__.py",
        "pkg/cards.py",
    ]


def test_import_targets_named_in_description(tmp_path: Path):
    _write(tmp_path, {"src/app/models.py": "class User:\n    pass\n"})
    task = {"outputs": [], "description": "Use `from app.models import User` in the view."}

    assert "src/app/models.py" in gather_context(tmp_path, task)


def test_paths_outside_workspace_are_blocked(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "secret.py").write_text("KEY = 1\n")

    result = gather_context(
        workspace,
        {"outputs": ["../secret.py", str(tmp_path / "secret.py")]},
        review_feedback=[{"evidence": "../secret.py:1"}],
        prior_files=["../secret.py"],
    )

    assert result == {}


def test_review_mode_reads_top_level_source_files(tmp_path: Path):
    tree = ["main.py", "docs/notes.md", "pkg/deep/mod.py"]
    _write(tmp_path, dict.fromkeys(tree, "x = 1\n"))

    result = gather_context(tmp_path, file_tree=tree)

    assert "main.py" in result
    assert "pkg/deep/mod.py" in result
    assert "docs/notes.md" not in result


def test_repo_map_ranks_prior_files_first(tmp_path: Path):
    _card_workspace(tmp_path)
    _write(tmp_path, {"aaa.py": "".join(f"def f{i}():\n    pass\n" for i in range(30))})
    task = {"title": "Write report", "description": "Format output", "outputs": ["report.py"]}

    without_prior = gather_repo_map(tmp_path, task, max_tokens=4096)
    with_prior = gather_repo_map(tmp_path, task, max_tokens=4096, prior_files=["card_deck.py"])

    assert without_prior.index("aaa.py") < without_prior.index("card_deck.py")
    assert with_prior.index("card_deck.py") < with_prior.index("aaa.py")
    assert "class Deck" in with_prior


# ---------------------------------------------------------------------------
# Large and binary files (#98)
# ---------------------------------------------------------------------------


def _write_bytes(root: Path, files: dict[str, str]) -> None:
    """Write UTF-8 files byte-exact (no newline translation), so sizes are known."""
    for rel_path, content in files.items():
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))


def _module_of_size(size: int) -> str:
    """ASCII Python-like source of exactly *size* bytes, ending with a newline."""
    text = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(size // 10))
    return text[: size - 1] + "\n"


def test_target_over_the_budget_is_excerpted_not_dropped(tmp_path: Path):
    """Local budget (30 KB): a 48 KB task output used to vanish from the prompt."""
    _write_bytes(tmp_path, {"engine.py": _module_of_size(48_780)})
    task = {"description": "Add logging to f3 in engine.py", "outputs": ["engine.py"]}

    selection = select_context(tmp_path, task, max_total_size=30_000)

    shown = selection.files["engine.py"]
    assert len(shown.encode()) <= 30_000
    assert shown.startswith("def f0():\n")
    assert shown.endswith("\n")
    assert selection.partial == {"engine.py": 48_780}


def test_small_target_stays_whole_beside_an_excerpted_one(tmp_path: Path):
    _write_bytes(tmp_path, {"engine.py": _module_of_size(78_780), "zz_small.py": "SMALL = 1\n"})

    selection = select_context(
        tmp_path, {"outputs": ["engine.py", "zz_small.py"]}, max_total_size=50_000
    )

    assert list(selection.files) == ["engine.py", "zz_small.py"]
    assert selection.files["zz_small.py"] == "SMALL = 1\n"
    assert selection.partial == {"engine.py": 78_780}
    assert sum(len(c.encode()) for c in selection.files.values()) <= 50_000


def test_excerpt_shows_whole_head_and_tail_lines_and_counts_the_cut(tmp_path: Path):
    lines = [f"line {i:05d}\n" for i in range(10_000)]
    _write_bytes(tmp_path, {"parser.py": "".join(lines)})

    shown = select_context(tmp_path, {"outputs": ["parser.py"]}).files["parser.py"]

    match = re.fullmatch(r"(.*)\n\.\.\. \[truncated (\d+) bytes\] \.\.\.\n(.*)", shown, re.DOTALL)
    assert match is not None
    head, cut, tail = match.groups()
    assert head.startswith("line 00000\n")
    assert head.endswith("\n")
    assert tail.startswith("line ")
    assert tail.endswith("line 09999\n")
    assert int(cut) == len("".join(lines)) - len(head) - len(tail)
    assert set((head + tail).splitlines()) <= {line.strip() for line in lines}


def test_excerpt_of_multibyte_text_stays_within_the_budget(tmp_path: Path):
    _write_bytes(tmp_path, {"cards.py": "".join(f"# Pokémon {i} — ✓ 🎴\n" for i in range(3_000))})

    selection = select_context(tmp_path, {"outputs": ["cards.py"]}, max_total_size=10_000)

    shown = selection.files["cards.py"]
    assert len(shown.encode()) <= 10_000
    assert shown.startswith("# Pokémon 0 — ✓ 🎴\n")
    assert "\ufffd" not in shown


def test_target_is_left_out_when_too_little_budget_remains(tmp_path: Path):
    _write_bytes(tmp_path, {"a_big.py": "x = 1\n" * 1_000, "b_small.py": "y = 1\n"})

    selection = select_context(
        tmp_path, {"outputs": ["a_big.py", "b_small.py"]}, max_total_size=1_000
    )

    assert list(selection.files) == ["b_small.py"]
    assert selection.omitted == {"a_big.py": 6_000}
    assert selection.partial == {}


def test_oversized_non_target_is_left_out_not_excerpted(tmp_path: Path):
    _write_bytes(tmp_path, {"app.py": "x = 1\n", "huge.py": "y = 2\n" * 10_000})

    selection = select_context(tmp_path, {"outputs": ["app.py"]}, max_total_size=5_000)

    assert list(selection.files) == ["app.py"]
    assert selection.omitted == {"huge.py": 60_000}
    assert selection.partial == {}


def test_binary_named_in_the_task_is_skipped_not_decoded(tmp_path: Path):
    png = b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + random.Random(0).randbytes(18_008 - 16)
    _write_bytes(tmp_path, {"src/header.js": "export const header = () => null;\n"})
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "logo.png").write_bytes(png)
    task = {
        "description": "Show the image assets/logo.png in the page header",
        "outputs": ["src/header.js"],
    }

    selection = select_context(tmp_path, task, max_total_size=30_000)

    assert selection.binary == {"assets/logo.png": 18_008}
    assert "src/header.js" in selection.files
    assert "assets/logo.png" not in selection.files


@pytest.mark.parametrize(
    ("name", "data"),
    [("manual.pdf", b"%PDF-1.7\n" + b"x" * 9_000), ("data.bin", b"abc\0def\n")],
    ids=["pdf_without_nul", "nul_byte"],
)
def test_binary_files_are_detected(tmp_path: Path, name: str, data: bytes):
    (tmp_path / name).write_bytes(data)

    selection = select_context(tmp_path, review_feedback=[{"file": name}])

    assert selection.binary == {name: len(data)}
    assert selection.files == {}


def test_gather_context_returns_the_selected_files(tmp_path: Path):
    _write_bytes(tmp_path, {"engine.py": _module_of_size(48_780), "logo.png": "\0"})
    task = {"description": "See logo.png", "outputs": ["engine.py"]}

    selection = select_context(tmp_path, task, max_total_size=30_000)

    assert gather_context(tmp_path, task, max_total_size=30_000) == selection.files


# ---------------------------------------------------------------------------
# Path safety (#110) and reference normalization (#67)
# ---------------------------------------------------------------------------


def test_unc_drive_and_absolute_refs_never_reach_the_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _card_workspace(tmp_path)
    inside = (tmp_path, tmp_path.resolve())
    touched: list[str] = []
    for name in ("resolve", "is_file", "exists", "stat", "open", "read_bytes"):
        original = getattr(Path, name)

        def spy(self: Path, *args: Any, _original: Any = original, **kwargs: Any) -> Any:
            touched.append(str(self))
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(Path, name, spy)
    task = {
        "description": "Patch //evil.example/share/fix.py",
        "outputs": ["//evil.example/share/out.py", "\\\\evil.example\\share\\o.py", "D:/evil/o.py"],
    }
    feedback = [
        {
            "recommended_fix": "see //evil.example/share/fix.py",
            "evidence": "\\\\evil.example\\share\\x.py:1",
            "file": "C:\\evil\\deck.py",
        }
    ]

    result = gather_context(
        tmp_path, task, review_feedback=feedback, prior_files=["\\\\evil.example\\share\\p.py"]
    )

    monkeypatch.undo()
    outside = [p for p in touched if not any(Path(p).is_relative_to(root) for root in inside)]
    assert outside == []
    assert not [p for p in result if "evil" in p]


@pytest.mark.parametrize(
    "evidence",
    [
        "/workspace/tests/test_card_deck.py:15",
        "C:\\Users\\me\\ws\\tests\\test_card_deck.py:15",
        'File "/workspace/tests/test_card_deck.py", line 15, in test_shuffle',
        "FAILED tests/test_card_deck.py::TestDeck::test_shuffle - AssertionError",
        "test_card_deck.py (line 15)",
        "`./tests/test_card_deck.py`",
    ],
)
def test_feedback_refs_resolve_to_workspace_files(tmp_path: Path, evidence: str):
    _card_workspace(tmp_path)

    result = gather_context(tmp_path, review_feedback=[{"severity": "major", "evidence": evidence}])

    assert list(result)[:2] == ["tests/test_card_deck.py", "card_deck.py"]


def test_bare_name_resolves_to_the_only_file_with_that_name(tmp_path: Path):
    _write(tmp_path, {"src/cards/card_deck.py": _CARD_DECK, "src/cards/other.py": "X = 1\n"})

    result = gather_context(tmp_path, review_feedback=[{"fix_file": "card_deck.py"}])

    assert next(iter(result)) == "src/cards/card_deck.py"


def test_same_file_cited_in_different_forms_is_read_once(tmp_path: Path):
    _card_workspace(tmp_path)
    feedback = [
        {"evidence": "./card_deck.py:3"},
        {"file": "card_deck.py"},
        {"recommended_fix": "Fix shuffle in /workspace/card_deck.py"},
    ]

    selection = select_context(
        tmp_path, review_feedback=feedback, max_total_size=len(_CARD_DECK) + 10
    )

    assert list(selection.files) == ["card_deck.py"]


def test_repo_map_ranks_files_cited_with_sandbox_paths_first(tmp_path: Path):
    _write(
        tmp_path,
        {
            "pkg/big.py": "".join(f"def f{i}() -> None: pass\n" for i in range(30)),
            "pkg/target.py": "def wanted() -> None: pass\n",
        },
    )
    feedback = [{"evidence": "/workspace/pkg/target.py:1"}]

    repo_map = gather_repo_map(tmp_path, review_feedback=feedback, max_tokens=4096)

    assert repo_map.index("pkg/target.py:") < repo_map.index("pkg/big.py:")


def test_delegatron_internal_dirs_are_never_context(tmp_path: Path):
    files = {
        "app.py": "x = 1\n",
        "runs/abc/state.py": "S = 1\n",
        "runs/abc/summary.json": "{}\n",
        "reviews/review-1/notes.py": "N = 1\n",
        "_sandbox/workspace/app.py": "x = 1\n",
        "src/runs/job.py": "J = 1\n",
    }
    _write(tmp_path, files)

    result = gather_context(
        tmp_path,
        {"outputs": ["app.py"]},
        review_feedback=[{"evidence": "runs/abc/state.py:1"}],
        prior_files=["reviews/review-1/notes.py", "_sandbox/workspace/app.py", "src/runs/job.py"],
        file_tree=list(files),
    )

    assert set(result) == {"app.py", "src/runs/job.py"}


@pytest.mark.parametrize("file_tree", [None, ["app.py", "runs/job.py", "reviews/check.py"]])
def test_user_packages_named_runs_or_reviews_are_context(
    tmp_path: Path, file_tree: list[str] | None
):
    """D4: without Delegatron's layout inside, ``runs/`` and ``reviews/`` are user code."""
    _write(tmp_path, {"app.py": "x = 1\n", "runs/job.py": "J = 1\n", "reviews/check.py": "C = 1\n"})

    result = gather_context(
        tmp_path,
        {"outputs": ["app.py"]},
        review_feedback=[{"evidence": "runs/job.py:1"}],
        prior_files=["reviews/check.py"],
        file_tree=file_tree,
    )

    assert set(result) == {"app.py", "runs/job.py", "reviews/check.py"}


def test_repo_map_failure_logs_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    def fail(**_kwargs: Any) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr("delegatron.context.generate_repo_map", fail)
    # Another test may have stopped "delegatron" records reaching caplog's root handler.
    monkeypatch.setattr(logging.getLogger("delegatron"), "propagate", True)

    with caplog.at_level(logging.WARNING, logger="delegatron.context"):
        assert gather_repo_map(tmp_path) == ""

    assert "Repo map generation failed: boom" in caplog.text


# ---------------------------------------------------------------------------
# Review mode (#125)
# ---------------------------------------------------------------------------

_REVIEW_TREE = [
    *(f"benchmarks/bench_{i}.py" for i in range(4)),
    "scripts/x.py",
    "docs/conf.py",
    "src/pkg/__init__.py",
    "src/pkg/core.py",
    "src/pkg/cli.py",
    "src/pkg/testing_utils.py",
    "src/contest/rules.py",
    "tests/conftest.py",
    "tests/unit/test_core.py",
]


def test_review_ranks_package_code_before_helper_scripts():
    ranked = select_review_files(_REVIEW_TREE)

    package_code = {
        "src/pkg/core.py",
        "src/pkg/cli.py",
        "src/pkg/testing_utils.py",
        "src/contest/rules.py",
    }
    assert package_code <= set(ranked)
    assert not [p for p in ranked if p.startswith("tests/")]
    last_src = max(i for i, p in enumerate(ranked) if p.startswith("src/"))
    first_helper = min(i for i, p in enumerate(ranked) if not p.startswith("src/"))
    assert last_src < first_helper


def test_review_ranks_top_level_python_packages_first():
    tree = ["main.py", "app_pkg/__init__.py", "app_pkg/core.py"]

    assert select_review_files(tree) == ["app_pkg/core.py", "app_pkg/__init__.py", "main.py"]


@pytest.mark.parametrize(
    "test_path",
    [
        "tests/conftest.py",
        "tests/unit/__init__.py",
        "web/__tests__/a.js",
        "spec/models/user.rb",
        "src/a_test.go",
        "web/b.spec.ts",
        "c.test.js",
        "test_x.py",
    ],
)
def test_review_leaves_out_tests_when_there_is_other_code(test_path: str):
    assert select_review_files([test_path, "src/app.py"]) == ["src/app.py"]


def test_review_falls_back_to_helpers_then_tests():
    assert select_review_files(["scripts/a.py", "README.md"]) == ["scripts/a.py"]
    assert select_review_files(["tests/test_a.py"]) == ["tests/test_a.py"]


def test_review_mode_reads_in_rank_order_with_a_per_file_cap(tmp_path: Path):
    benchmarks = [f"benchmarks/bench_{i}.py" for i in range(4)]
    files = dict.fromkeys(benchmarks, "# bench\n" * 1_500)
    files |= {
        "src/pkg/__init__.py": "",
        "src/pkg/core.py": "def core():\n    pass\n" * 2_000,
        "src/pkg/cli.py": "def cli():\n    pass\n",
        "tests/unit/test_core.py": "def test_core():\n    pass\n",
    }
    _write_bytes(tmp_path, files)

    selection = select_context(tmp_path, file_tree=sorted(files))

    assert list(selection.files) == [
        "src/pkg/cli.py",
        "src/pkg/core.py",
        "src/pkg/__init__.py",
        *benchmarks,
    ]
    assert len(selection.files["src/pkg/core.py"].encode()) <= MAX_TOTAL_SIZE // 8
    assert set(selection.partial) == {"src/pkg/core.py", *benchmarks}
    # Non-git workspace: an empty file list falls back to walking the disk.
    assert select_context(tmp_path, file_tree=[]).files == selection.files


@pytest.mark.parametrize(
    ("path", "is_test"),
    [
        ("src/main/java/com/acme/FooTest.java", True),
        ("app/src/main/kotlin/BarTest.kt", True),
        ("Lib/WidgetTests.cs", True),
        ("lib/test/helper.rb", False),
        ("src/myapp/spec/openapi.py", False),
    ],
)
def test_review_uses_the_shared_test_file_rule(path: str, is_test: bool):
    """#92: one test-path predicate (suite_guard.is_test_path) for every caller."""
    assert is_test_path(path) is is_test
    assert (path in select_review_files([path, "src/app.py"])) is not is_test


@pytest.mark.parametrize(
    "node_id",
    ["/workspace/tests/test_gone.py::test_a", "C:/sandbox/gone/test_gone.py::test_a"],
)
def test_unmatched_failing_tests_do_not_switch_to_review_mode(tmp_path: Path, node_id: str):
    """The pipeline reviewer must not get unrelated top-ranked files as related files."""
    tree = ["src/pkg/__init__.py", "src/pkg/core.py", "src/pkg/other.py"]
    _write(tmp_path, dict.fromkeys(tree, "x = 1\n"))
    report = {"passed": False, "failed_tests": [node_id]}

    assert select_context(tmp_path, file_tree=tree, test_report=report).files == {}
    assert select_context(tmp_path, file_tree=tree, prior_files=["gone.py"]).files == {}


def test_review_context_is_the_ranked_files_after_the_repo_map(tmp_path: Path):
    _write(
        tmp_path,
        {
            "src/pkg/__init__.py": "",
            "src/pkg/core.py": "def core() -> int:\n    return 1\n",
            "tests/test_core.py": "def test_core():\n    pass\n",
        },
    )

    review = gather_review_context(tmp_path, file_tree=[], languages=["python"])

    assert review.files == select_review_context(tmp_path).files == select_context(tmp_path).files
    assert list(review.files) == ["src/pkg/core.py", "src/pkg/__init__.py"]
    assert "def core() -> int" in review.repo_map
    dump = review.source_dump()
    assert dump.startswith(f"\n--- Repository Structure ---\n{review.repo_map}\n")
    assert dump.index("--- File: src/pkg/core.py ---") < dump.index("--- File: src/pkg/__init__")
    assert "tests/test_core.py ---" not in dump
    assert ReviewContext(files={"a.py": "x\n"}).source_dump() == "\n--- File: a.py ---\nx\n\n"


# ---------------------------------------------------------------------------
# Selection tiers and budget (#67, #98)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("other_root_modules", "siblings_shown"),
    [(MAX_ROOT_SIBLINGS - 1, True), (MAX_ROOT_SIBLINGS, False)],
    ids=["small_root", "flat_root"],
)
def test_root_siblings_gated_by_count(
    tmp_path: Path, other_root_modules: int, siblings_shown: bool
):
    """A flat root of unrelated modules must not fill a local model's budget."""
    modules = {f"mod_{i:02d}.py": f"X{i} = {i}\n" for i in range(other_root_modules)}
    _write(tmp_path, {"app.py": "APP = 1\n", **modules, "pkg/helper.py": "H = 1\n"})

    result = gather_context(tmp_path, {"outputs": ["app.py"]})

    expected = ["app.py", *sorted(modules)] if siblings_shown else ["app.py"]
    assert list(result) == expected


def test_root_siblings_are_read_after_directory_siblings(tmp_path: Path):
    _write(
        tmp_path,
        {
            "aaa.py": "A = 1\n",
            "app.py": "APP = 1\n",
            "lib/helpers.py": "H = 1\n",
            "lib/other.py": "O = 1\n",
        },
    )

    result = gather_context(tmp_path, {"outputs": ["app.py", "lib/helpers.py"]})

    assert list(result) == ["app.py", "lib/helpers.py", "lib/other.py", "aaa.py"]


def test_description_named_file_ranks_below_failing_test_imports(tmp_path: Path):
    """A file the description names cannot push out the module a failing test imports."""
    _card_workspace(tmp_path)
    _write_bytes(tmp_path, {"notes_helper.py": _module_of_size(3_000)})
    task = {"description": "Follow the rules in notes_helper.py.", "outputs": []}

    selection = select_context(tmp_path, task, test_report=_FAILED_SHUFFLE, max_total_size=3_100)

    assert list(selection.files) == ["tests/test_card_deck.py", "card_deck.py", "notes_helper.py"]
    assert selection.partial == {"notes_helper.py": 3_000}
    assert selection.omitted == {}


def test_description_named_file_precedes_its_counterparts(tmp_path: Path):
    _write(
        tmp_path,
        {
            "app.py": "A = 1\n",
            "deal.py": "D = 1\n",
            "tests/test_deal.py": "def test_deal():\n    pass\n",
        },
    )
    task = {"description": "Use deal.py in main.py.", "outputs": ["main.py"]}

    result = gather_context(tmp_path, task)

    assert list(result) == ["deal.py", "tests/test_deal.py", "app.py"]


def test_task_outputs_outrank_prior_files_under_budget(tmp_path: Path):
    _write_bytes(tmp_path, {"aa.py": "A = 1\n" * 100, "zz.py": "Z = 1\n" * 100})

    selection = select_context(
        tmp_path, {"outputs": ["zz.py"]}, prior_files=["aa.py"], max_total_size=1_000
    )

    assert list(selection.files) == ["zz.py"]
    assert selection.omitted == {"aa.py": 600}


def test_evidence_on_a_test_named_after_no_module_pulls_its_imports(tmp_path: Path):
    """it6, second case: ``tests/test_misc.py`` tests no ``misc.py`` but imports card_deck."""
    _card_workspace(tmp_path)
    _write(
        tmp_path,
        {
            "tests/test_misc.py": (
                "import card_deck\n\n\n"
                "def test_misc():\n"
                "    assert card_deck.Deck([]).cards == []\n"
            )
        },
    )

    result = gather_context(tmp_path, review_feedback=[{"evidence": "tests/test_misc.py:5"}])

    assert list(result)[:2] == ["tests/test_misc.py", "card_deck.py"]


@pytest.mark.parametrize(
    ("test_path", "source"),
    [
        ("tests/card_deck_test.py", "card_deck.py"),
        ("card_deck_test.py", "card_deck.py"),
        ("tests/card_deck_test.py", "src/cards/card_deck.py"),
        ("pkg/deck_test.go", "pkg/deck.go"),
        ("web/deck.test.ts", "web/deck.ts"),
        ("web/deck.spec.ts", "web/deck.ts"),
    ],
)
def test_suffix_named_test_file_maps_back_to_its_module(
    tmp_path: Path, test_path: str, source: str
):
    """``x_test.py`` (and ``x_test.go``, ``x.test.ts``) -> ``x.<ext>``, ahead of siblings."""
    source_dir = posixpath.dirname(source)
    decoy = posixpath.join(source_dir, "aaa" + posixpath.splitext(source)[1])
    _write(tmp_path, {source: "X = 1\n", decoy: "A = 1\n", test_path: "def test_x():\n    pass\n"})

    result = gather_context(tmp_path, review_feedback=[{"evidence": f"{test_path}:2"}])

    assert list(result)[:2] == [test_path, source]


def test_target_files_that_fit_whole_are_read_smallest_first(tmp_path: Path):
    """#98: a 28.5 KB output must not crowd a 5 KB one out of the 30 KB local budget."""
    _write_bytes(tmp_path, {"a.py": _module_of_size(28_500), "b.py": _module_of_size(5_000)})

    selection = select_context(tmp_path, {"outputs": ["a.py", "b.py"]}, max_total_size=30_000)

    assert list(selection.files) == ["a.py", "b.py"]  # still in priority order
    assert selection.files["b.py"] == _module_of_size(5_000)
    assert selection.partial == {"a.py": 28_500}
    assert selection.omitted == {}
    assert sum(len(text.encode()) for text in selection.files.values()) <= 30_000


def test_excerpted_left_out_and_binary_files_are_logged_with_their_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    _write_bytes(
        tmp_path,
        {
            "engine.py": _module_of_size(48_780),
            "app.py": "x = 1\n",
            "huge.py": "y = 2\n" * 10_000,
        },
    )
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0")
    task = {"description": "Show logo.png", "outputs": ["engine.py", "app.py"]}
    # Another test may have stopped "delegatron" records reaching caplog's root handler.
    monkeypatch.setattr(logging.getLogger("delegatron"), "propagate", True)

    with caplog.at_level(logging.INFO, logger="delegatron.context"):
        selection = select_context(tmp_path, task, max_total_size=30_000)

    shown = len(selection.files["engine.py"].encode())
    assert f"Context: engine.py (48780 bytes) shown as a {shown}-byte excerpt" in caplog.text
    assert "Context: left out huge.py (60000 bytes, " in caplog.text
    assert "Context: skipped binary file logo.png (10 bytes)" in caplog.text
