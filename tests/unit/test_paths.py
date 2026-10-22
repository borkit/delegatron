"""Tests for delegatron.utils.paths."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from delegatron.utils.paths import (
    ALWAYS_INTERNAL_DIRS,
    INTERNAL_DIRS,
    internal_copy_ignore,
    internal_dirs_for,
    is_internal_path,
    normalize_rel_path,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("card_deck.py", "card_deck.py"),
        ("./tests/test_card_deck.py", "tests/test_card_deck.py"),
        ("src\\app\\main.py", "src/app/main.py"),
        ("  src//app/./x.py ", "src/app/x.py"),
        ("/src/app.py", None),
        ("C:\\ws\\x.py", None),
        ("c:/ws/x.py", None),
        ("//host/share/x.py", None),
        ("\\\\host\\share\\x.py", None),
        ("../outside.py", None),
        ("src/../../outside.py", None),
        ("", None),
        ("   ", None),
        (".", None),
        ("C:x.py", None),
        ("a\x00.py", None),
        # .git in any spelling a case-insensitive or dot-stripping FS maps onto .git
        (".git/config", None),
        (".GIT/config", None),
        (".Git/hooks/post-commit", None),
        (".git./config", None),
        (".git /config", None),
        ("sub/.git/config", None),
        ("GIT~1/config", None),
        # NTFS alternate data streams and reserved device names
        ("app.py:stream", None),
        ("src/x.py::$DATA", None),
        ("CON", None),
        ("nul.txt", None),
        ("src/Aux.py", None),
        ("lpt1.log", None),
        ("COM9", None),
        # look-alikes that are ordinary files
        (".gitignore", ".gitignore"),
        (".github/workflows/ci.yml", ".github/workflows/ci.yml"),
        ("src/git/x.py", "src/git/x.py"),
        ("console.py", "console.py"),
        ("connect/aux_tools.py", "connect/aux_tools.py"),
        ("com10.txt", "com10.txt"),
    ],
)
def test_normalize_rel_path(raw: str, expected: str | None) -> None:
    assert normalize_rel_path(raw) == expected


def test_normalize_rel_path_rejects_non_strings() -> None:
    assert normalize_rel_path(None) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("runs/abc/summary.json", True),
        ("reviews/review-1/summary.json", True),
        (".delegatron-runs/x/card_deck.py", True),
        ("_sandbox", True),
        ("src/runs/model.py", False),
        ("tests/reviews.py", False),
        ("card_deck.py", False),
        ("../runs", False),
    ],
)
def test_is_internal_path(rel: str, expected: bool) -> None:
    assert is_internal_path(rel) is expected


def _delegatron_layout(root: Path) -> None:
    """Create Delegatron-shaped runs/ and reviews/ dirs plus the always-internal ones."""
    (root / "runs" / "abc123").mkdir(parents=True)
    (root / "runs" / "abc123" / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "reviews" / "review-1").mkdir(parents=True)
    (root / "reviews" / "review-1" / "summary.json").write_text("{}", encoding="utf-8")
    for name in ALWAYS_INTERNAL_DIRS:
        (root / name).mkdir()
        (root / name / "f.txt").write_text("x", encoding="utf-8")


def test_internal_dirs_for_recognises_delegatron_layouts(tmp_path: Path) -> None:
    _delegatron_layout(tmp_path)
    assert internal_dirs_for(tmp_path) == frozenset(INTERNAL_DIRS)


def test_internal_dirs_for_keeps_user_packages_named_runs_or_reviews(tmp_path: Path) -> None:
    (tmp_path / "reviews" / "migrations").mkdir(parents=True)
    (tmp_path / "reviews" / "models.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "__init__.py").write_text("", encoding="utf-8")

    internal = internal_dirs_for(tmp_path)

    assert internal == ALWAYS_INTERNAL_DIRS
    assert not is_internal_path("reviews/models.py", root=tmp_path)
    assert not is_internal_path("runs/__init__.py", root=tmp_path)
    assert is_internal_path("_sandbox/x.py", root=tmp_path)


def test_a_runs_child_with_only_checkpoints_is_a_delegatron_run(tmp_path: Path) -> None:
    (tmp_path / "runs" / "x").mkdir(parents=True)
    (tmp_path / "runs" / "x" / "state_001.json").write_text("{}", encoding="utf-8")
    assert "runs" in internal_dirs_for(tmp_path)


@pytest.mark.parametrize("name", ["state.json", "state_abc.json", "state_001.json.bak"])
def test_other_state_files_do_not_mark_a_run(tmp_path: Path, name: str) -> None:
    (tmp_path / "runs" / "x").mkdir(parents=True)
    (tmp_path / "runs" / "x" / name).write_text("{}", encoding="utf-8")
    (tmp_path / "runs" / "state_001").mkdir()  # a directory, not a checkpoint file
    (tmp_path / "runs" / "y" / "state_002.json").mkdir(parents=True)
    assert "runs" not in internal_dirs_for(tmp_path)


def test_always_internal_dirs_are_the_internal_dirs_without_shared_names() -> None:
    assert frozenset(INTERNAL_DIRS) - {"runs", "reviews"} == ALWAYS_INTERNAL_DIRS


def test_is_internal_path_with_root_uses_the_layout(tmp_path: Path) -> None:
    _delegatron_layout(tmp_path)
    assert is_internal_path("runs/abc123/events.jsonl", root=tmp_path)
    assert is_internal_path("reviews/review-1/summary.json", root=tmp_path)
    assert not is_internal_path("src/runs/model.py", root=tmp_path)
    assert not is_internal_path("src/reviews/models.py", root=tmp_path)


def test_internal_copy_ignore_only_drops_root_level_internal_dirs(tmp_path: Path) -> None:
    src = tmp_path / "src_ws"
    src.mkdir()
    _delegatron_layout(src)
    for d in ("src/runs", "src/reviews", "node_modules/x"):
        (src / d).mkdir(parents=True, exist_ok=True)
        (src / d / "f.txt").write_text("x", encoding="utf-8")
    (src / "keep.py").write_text("x = 1\n", encoding="utf-8")

    dest = tmp_path / "dest"
    shutil.copytree(src, dest, ignore=internal_copy_ignore(src, extra=["node_modules"]))

    for d in INTERNAL_DIRS:
        assert not (dest / d).exists()
    assert (dest / "src" / "runs" / "f.txt").exists()
    assert (dest / "src" / "reviews" / "f.txt").exists()
    assert not (dest / "node_modules").exists()
    assert (dest / "keep.py").exists()


def test_internal_copy_ignore_copies_a_user_reviews_package(tmp_path: Path) -> None:
    src = tmp_path / "src_ws"
    (src / "reviews").mkdir(parents=True)
    (src / "reviews" / "models.py").write_text("x = 1\n", encoding="utf-8")
    (src / "_sandbox").mkdir()

    dest = tmp_path / "dest"
    shutil.copytree(src, dest, ignore=internal_copy_ignore(src))

    assert (dest / "reviews" / "models.py").exists()
    assert not (dest / "_sandbox").exists()
