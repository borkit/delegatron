"""Tests for delegatron.security.patch_lines — lines a patch adds."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from delegatron.security.patch_lines import (
    SecurityBaseline,
    added_lines,
    decode_text,
    patch_files,
    resolve_edits,
    touched_paths,
)


class TestDecodeText:
    def test_utf8(self) -> None:
        assert decode_text("Pokémon\n".encode()) == "Pokémon\n"

    def test_utf8_bom_is_stripped(self) -> None:
        assert decode_text(b"\xef\xbb\xbfrequests\n") == "requests\n"

    def test_utf16_bom(self) -> None:
        assert decode_text("requests==2.0\n".encode("utf-16")) == "requests==2.0\n"

    def test_invalid_bytes_are_replaced(self) -> None:
        assert decode_text(b"a\xffb") == "a�b"


class TestPatchFiles:
    def test_paths_are_normalized_and_invalid_entries_dropped(self) -> None:
        patch = {
            "files": {
                "./a.py": "x",
                "pkg\\b.py": "y",
                "../escape.py": "z",
                "/abs.py": "z",
                "c.py": None,
            }
        }
        assert patch_files(patch) == {"a.py": "x", "pkg/b.py": "y"}

    def test_missing_files(self) -> None:
        assert patch_files({"diff_unified": "x"}) == {}


class TestAddedLinesFromFiles:
    def test_new_file_adds_every_line(self, tmp_path: Path) -> None:
        patch = {"files": {"a.py": "x = 1\ny = 2\n"}}
        assert added_lines(patch, tmp_path) == {"a.py": [(1, "x = 1"), (2, "y = 2")]}

    def test_no_workspace_means_every_line_is_added(self) -> None:
        assert added_lines({"files": {"a.py": "x\ny"}}) == {"a.py": [(1, "x"), (2, "y")]}

    def test_unchanged_lines_are_not_added(self, tmp_path: Path) -> None:
        (tmp_path / "config.py").write_text("A = 1\nB = 2\nC = 3\n", encoding="utf-8")
        patch = {"files": {"config.py": "A = 1\nNEW = 9\nB = 2\nC = 4\n"}}
        assert added_lines(patch, tmp_path) == {"config.py": [(2, "NEW = 9"), (4, "C = 4")]}

    def test_identical_file_is_not_a_change(self, tmp_path: Path) -> None:
        (tmp_path / "same.py").write_text("x = 1\n", encoding="utf-8")
        assert added_lines({"files": {"same.py": "x = 1\n"}}, tmp_path) == {}

    def test_line_ending_only_difference_is_not_a_change(self, tmp_path: Path) -> None:
        (tmp_path / "w.py").write_bytes(b"x = 1\r\ny = 2\r\n")
        assert added_lines({"files": {"w.py": "x = 1\ny = 2\n"}}, tmp_path) == {}

    def test_deletion_only_change_maps_to_empty_list(self, tmp_path: Path) -> None:
        (tmp_path / "d.py").write_text("a\nb\nc\n", encoding="utf-8")
        assert added_lines({"files": {"d.py": "a\nc\n"}}, tmp_path) == {"d.py": []}

    def test_utf16_workspace_file_is_decoded(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_bytes("requests\n".encode("utf-16"))
        patch = {"files": {"requirements.txt": "requests\nflask\n"}}
        assert added_lines(patch, tmp_path) == {"requirements.txt": [(2, "flask")]}


class TestAddedLinesFromDiff:
    def test_hunk_headers_give_post_apply_line_numbers(self) -> None:
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -10,3 +10,4 @@ def f():\n"
            " keep\n"
            "-old\n"
            "+new one\n"
            "+new two\n"
            " tail\n"
            "@@ -40,1 +41,2 @@\n"
            " ctx\n"
            "+late\n"
        )
        assert added_lines({"diff_unified": diff}) == {
            "app.py": [(11, "new one"), (12, "new two"), (42, "late")]
        }

    def test_multiple_files_and_deleted_file(self) -> None:
        diff = (
            "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
            "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+a\n+b\n"
        )
        assert added_lines({"diff_unified": diff}) == {"new.py": [(1, "a"), (2, "b")]}

    def test_diff_without_hunk_header_counts_from_one(self) -> None:
        diff = "+++ b/app.py\n+import requests\n+import os"
        assert added_lines({"diff_unified": diff}) == {
            "app.py": [(1, "import requests"), (2, "import os")]
        }

    def test_removed_line_resembling_a_header_is_ignored(self) -> None:
        diff = "+++ b/q.sql\n@@ -1,2 +1,1 @@\n--- a comment\n+select 1;\n"
        assert added_lines({"diff_unified": diff}) == {"q.sql": [(1, "select 1;")]}

    def test_lines_outside_a_file_section_are_ignored(self) -> None:
        assert added_lines({"diff_unified": "+AKIAIOSFODNN7EXAMPLE"}) == {}

    def test_files_take_precedence_over_diff(self) -> None:
        patch = {"files": {"a.py": "full"}, "diff_unified": "+++ b/a.py\n+partial\n"}
        assert added_lines(patch) == {"a.py": [(1, "full")]}


class TestResolveEdits:
    """#98 edits are folded into ``files`` so the checks see what will be written."""

    def test_patch_without_edits_is_unchanged(self, tmp_path: Path) -> None:
        patch = {"files": {"a.py": "x"}, "rationale": "r"}
        assert resolve_edits(patch, tmp_path) == patch

    def test_edit_applies_to_the_workspace_copy(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("A = 1\nB = 2\n", encoding="utf-8", newline="")
        patch = {"edits": [{"path": "./app.py", "search": "B = 2", "replace": "B = 3"}]}
        assert resolve_edits(patch, tmp_path)["files"] == {"app.py": "A = 1\nB = 3\n"}

    def test_edit_applies_to_the_patch_entry_and_honours_count(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("disk\n", encoding="utf-8", newline="")
        patch = {
            "files": {"app.py": "x x x\n"},
            "edits": [
                {"path": "app.py", "search": "x", "replace": "y"},
                {"path": "app.py", "search": "x", "replace": "z", "count": 2},
            ],
        }
        assert resolve_edits(patch, tmp_path)["files"] == {"app.py": "y z z\n"}

    @pytest.mark.parametrize(
        "edit",
        [
            {"path": "app.py", "search": "missing", "replace": "y"},
            {"path": "app.py", "search": "", "replace": "y"},
            {"path": "app.py", "search": "A", "replace": None},
            {"path": "../app.py", "search": "A", "replace": "y"},
            {"path": "new.py", "search": "A", "replace": "y"},
            "not a mapping",
        ],
    )
    def test_edit_that_cannot_apply_is_left_out(self, tmp_path: Path, edit: object) -> None:
        (tmp_path / "app.py").write_text("A = 1\n", encoding="utf-8", newline="")
        assert resolve_edits({"edits": [edit]}, tmp_path)["files"] == {}


class TestTouchedPaths:
    def test_every_kind_of_change_is_listed_once(self) -> None:
        patch = {
            "files": {"a.py": "x", "./b.py": "y"},
            "delete_files": ["c.py", "../escape.py", 3],
            "edits": [{"path": "a.py"}, {"path": "d.py"}, "junk"],
            "diff_unified": (
                "--- a/a/e.py\n+++ b/a/e.py\n@@ -1,2 +1,1 @@\n--- removed\n+y\n"
                "--- a/gone.py\n+++ /dev/null\n"
            ),
        }
        assert touched_paths(patch) == ["a.py", "b.py", "c.py", "d.py", "a/e.py", "gone.py"]


class TestSecurityBaseline:
    """#115: the originals a run's checks are diffed against across iterations."""

    def test_record_keeps_the_first_snapshot(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("original\n", encoding="utf-8", newline="")
        baseline = SecurityBaseline(tmp_path)
        baseline.record({"files": {"app.py": "v1\n", "new.py": "n\n"}})
        (tmp_path / "app.py").write_text("v1\n", encoding="utf-8", newline="")
        (tmp_path / "new.py").write_text("n\n", encoding="utf-8", newline="")
        baseline.record({"files": {"app.py": "v2\n", "new.py": "n2\n"}})

        assert baseline.paths == ["app.py", "new.py"]
        assert baseline.original("app.py") == "original\n"
        assert baseline.has("new.py")
        assert baseline.original("new.py") is None
        assert not baseline.has("other.py")

    def test_added_lines_compare_with_the_original(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("A = 1\n", encoding="utf-8", newline="")
        baseline = SecurityBaseline(tmp_path)
        baseline.record({"files": {"app.py": "A = 1\nKEY = 2\n"}})
        (tmp_path / "app.py").write_text("A = 1\nKEY = 2\n", encoding="utf-8", newline="")

        resent = {"files": {"app.py": "A = 1\nKEY = 2\nC = 3\n"}}
        assert added_lines(resent, tmp_path) == {"app.py": [(3, "C = 3")]}
        assert added_lines(resent, tmp_path, baseline=baseline) == {
            "app.py": [(2, "KEY = 2"), (3, "C = 3")]
        }

    def test_widen_adds_recorded_changes_the_patch_omits(self, tmp_path: Path) -> None:
        for name, text in {"kept.py": "k\n", "gone.py": "g\n", "same.py": "s\n"}.items():
            (tmp_path / name).write_text(text, encoding="utf-8", newline="")
        baseline = SecurityBaseline(tmp_path)
        baseline.record(
            {
                "files": {"kept.py": "k2\n", "new.py": "n\n", "same.py": "s\n"},
                "delete_files": ["gone.py"],
            }
        )
        (tmp_path / "kept.py").write_text("k2\n", encoding="utf-8", newline="")
        (tmp_path / "new.py").write_text("n\n", encoding="utf-8", newline="")
        (tmp_path / "gone.py").unlink()

        widened = baseline.widen({"files": {"other.py": "o\n"}})
        assert widened["files"] == {
            "other.py": "o\n",
            "kept.py": "k2\n",
            "new.py": "n\n",
            "same.py": "s\n",
        }
        assert widened["delete_files"] == ["gone.py"]
        assert added_lines(widened, tmp_path, baseline=baseline) == {
            "other.py": [(1, "o")],
            "kept.py": [(1, "k2")],
            "new.py": [(1, "n")],
        }

    def test_widen_keeps_the_patch_version_of_its_own_paths(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("a\n", encoding="utf-8", newline="")
        baseline = SecurityBaseline(tmp_path)
        baseline.record({"files": {"a.py": "a2\n"}})
        (tmp_path / "a.py").write_text("a2\n", encoding="utf-8", newline="")

        assert baseline.widen({"files": {"a.py": "a3\n"}})["files"] == {"a.py": "a3\n"}
        assert baseline.widen({"delete_files": ["a.py"]}) == {
            "delete_files": ["a.py"],
            "files": {},
        }

    def test_changed_paths_without_a_usable_ref_are_read_from_the_workspace(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / "a.py").write_text("now\n", encoding="utf-8", newline="")
        logger = logging.getLogger("delegatron.security.patch_lines")
        logger.addHandler(caplog.handler)
        try:
            baseline = SecurityBaseline(
                tmp_path, changed_paths=["a.py", "../x.py", "a.py"], git_ref="--output=x"
            )
        finally:
            logger.removeHandler(caplog.handler)
        assert baseline.paths == ["a.py"]
        assert baseline.original("a.py") == "now\n"
        assert "not found" in caplog.text

    @pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
    def test_resume_reads_originals_from_a_git_ref(self, tmp_path: Path) -> None:
        def git(*args: str) -> str:
            identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
            return subprocess.run(
                ["git", "-C", str(tmp_path), *identity, *args],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        git("init", "-q")
        (tmp_path / "app.py").write_text("clean\n", encoding="utf-8", newline="")
        git("add", "app.py")
        git("commit", "-q", "-m", "base")
        fork_point = git("rev-parse", "HEAD")
        (tmp_path / "app.py").write_text("clean\nKEY = 1\n", encoding="utf-8", newline="")
        (tmp_path / "new.py").write_text("n\n", encoding="utf-8", newline="")
        git("add", "-A")
        git("commit", "-q", "-m", "run")

        baseline = SecurityBaseline(
            tmp_path, changed_paths=["app.py", "new.py"], git_ref=fork_point
        )
        assert baseline.original("app.py") == "clean\n"
        assert baseline.has("new.py")
        assert baseline.original("new.py") is None
        assert added_lines(baseline.widen({}), tmp_path, baseline=baseline) == {
            "app.py": [(2, "KEY = 1")],
            "new.py": [(1, "n")],
        }
