"""Tests for workspace management."""

import ast
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from delegatron.workspace import ApplyResult, Workspace, WorkspaceInfo

TEXT_IO_METHODS = {"read_text", "write_text", "open"}


_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.com",
}


def _git_env() -> dict[str, str]:
    """The caller's environment with a test identity and no global git config."""
    return {**os.environ, **_IDENTITY, "GIT_CONFIG_GLOBAL": os.devnull}


def _git(cwd: Path, *args: str) -> str:
    """Run git with a throwaway identity, no global config and no signing; returns stdout."""
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@test.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env=_git_env(),
    ).stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a temporary git repo on ``main`` with one commit."""
    env = _git_env()
    for args in (
        ["init", "-b", "main"],
        ["config", "user.email", "test@test.com"],
        ["config", "user.name", "Test"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, check=True, env=env)

    # Create initial commit
    (tmp_path / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        env=env,
    )
    return tmp_path


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """A repo on ``main`` with one commit and no repo-local identity or signing config."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "app.py").write_bytes(b"x = 1\n")  # LF: clean with or without autocrlf
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "init")
    return tmp_path


def test_get_file_tree(git_repo: Path):
    ws = Workspace(repo_path=git_repo, run_id="test123")
    tree = ws.get_file_tree()
    assert "README.md" in tree


def test_read_file(git_repo: Path):
    ws = Workspace(repo_path=git_repo, run_id="test123")
    content = ws.read_file("README.md")
    assert content == "# Test\n"


def test_read_file_missing(git_repo: Path):
    ws = Workspace(repo_path=git_repo, run_id="test123")
    assert ws.read_file("nonexistent.txt") is None


def test_read_file_path_traversal(git_repo: Path):
    """Path traversal should raise ValueError."""
    ws = Workspace(repo_path=git_repo, run_id="test123")
    with pytest.raises(ValueError, match="Path traversal denied"):
        ws.read_file("../../etc/passwd")


def test_initialize(git_repo: Path):
    """initialize() should create a run branch and return WorkspaceInfo."""
    ws = Workspace(repo_path=git_repo, run_id="init-test")
    info = ws.initialize()
    assert isinstance(info, WorkspaceInfo)
    assert info.root == git_repo
    assert "delegatron/run-init-test" in info.branch
    assert info.is_clean is True
    assert len(info.commit_sha) == 40  # full SHA


def test_snapshot(git_repo: Path):
    """snapshot() should capture current workspace state."""
    ws = Workspace(repo_path=git_repo, run_id="snap-test")
    ws.initialize()
    info = ws.snapshot()
    assert isinstance(info, WorkspaceInfo)
    assert info.is_clean is True
    assert len(info.commit_sha) == 40


def test_apply_patch_valid(git_repo: Path):
    """apply_patch() should successfully apply a valid unified diff."""
    ws = Workspace(repo_path=git_repo, run_id="patch-test")
    ws.initialize()

    # Create a valid unified diff
    diff = "--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n # Test\n+Added line\n"
    result = ws.apply_patch(diff)
    assert result is True

    content = ws.read_file("README.md")
    assert "Added line" in content


def test_apply_patch_invalid(git_repo: Path):
    """apply_patch() should return False for an invalid diff."""
    ws = Workspace(repo_path=git_repo, run_id="bad-patch")
    ws.initialize()

    diff = "this is not a valid diff at all"
    result = ws.apply_patch(diff)
    assert result is False


def test_commit(git_repo: Path):
    """commit() should commit applied changes and return the new SHA."""
    ws = Workspace(repo_path=git_repo, run_id="commit-test")
    ws.initialize()

    ws.apply_files({"new_file.txt": "hello\n"})
    sha = ws.commit("test commit message")
    assert sha is not None
    assert len(sha) == 40


def test_commit_nothing_to_commit(git_repo: Path):
    """commit() should return None when there are no changes."""
    ws = Workspace(repo_path=git_repo, run_id="no-commit")
    ws.initialize()

    result = ws.commit("nothing to commit")
    assert result is None


def test_rollback(git_repo: Path):
    """rollback() should reset to a previous state."""
    ws = Workspace(repo_path=git_repo, run_id="rollback-test")
    ws.initialize()

    # Make a change and commit
    ws.apply_files({"temp.txt": "temporary\n"})
    ws.commit("temp commit")

    # Rollback to initial state
    ws.rollback()

    # The temp file should be gone
    assert not (git_repo / "temp.txt").exists()


def test_rollback_to_specific_sha(git_repo: Path):
    """rollback() to a specific SHA."""
    ws = Workspace(repo_path=git_repo, run_id="rollback-sha")
    ws.initialize()

    # First commit
    ws.apply_files({"file1.txt": "first\n"})
    sha1 = ws.commit("first commit")

    # Second commit
    ws.apply_files({"file2.txt": "second\n"})
    ws.commit("second commit")

    # Rollback to first commit
    ws.rollback(to_sha=sha1)
    assert (git_repo / "file1.txt").exists()
    assert not (git_repo / "file2.txt").exists()


def test_rollback_without_initialize_raises(git_repo: Path):
    ws = Workspace(repo_path=git_repo, run_id="rollback-none")
    with pytest.raises(RuntimeError, match="never initialized"):
        ws.rollback()


def test_get_diff_from_base(git_repo: Path):
    """get_diff_from_base() should return the diff since branching."""
    ws = Workspace(repo_path=git_repo, run_id="diff-test")
    ws.initialize()

    ws.apply_files({"new.py": "print('hello')\n"})
    ws.commit("add new file")

    diff = ws.get_diff_from_base()
    assert "new.py" in diff
    assert "print('hello')" in diff


def test_list_changed_files(git_repo: Path):
    """list_changed_files() should list files changed since base."""
    ws = Workspace(repo_path=git_repo, run_id="changed-test")
    ws.initialize()

    ws.apply_files({"changed.py": "x = 1\n"})
    ws.commit("add changed file")

    changed = ws.list_changed_files()
    assert "changed.py" in changed


def test_apply_files(git_repo: Path):
    """apply_files() should write files to the workspace."""
    ws = Workspace(repo_path=git_repo, run_id="apply-files")
    ws.initialize()

    files = {
        "src/main.py": "print('hello')\n",
        "src/utils.py": "def helper(): pass\n",
    }
    result = ws.apply_files(files)
    assert result == ApplyResult(written=["src/main.py", "src/utils.py"])
    assert (git_repo / "src" / "main.py").read_text() == "print('hello')\n"
    assert (git_repo / "src" / "utils.py").read_text() == "def helper(): pass\n"


def test_apply_files_path_traversal(git_repo: Path):
    """apply_files() skips and reports a traversal path instead of writing it.

    Intentional change (#110): one bad key no longer aborts the whole patch.
    """
    ws = Workspace(repo_path=git_repo, run_id="apply-traversal")
    ws.initialize()

    result = ws.apply_files({"../../etc/passwd": "hacked"})
    assert result.rejected == ["../../etc/passwd"]
    assert result.written == []


def test_get_file_tree_fallback(git_repo: Path):
    """get_file_tree_fallback() should list files without git."""
    ws = Workspace(repo_path=git_repo, run_id="fallback-tree")
    files = ws.get_file_tree_fallback()
    assert "README.md" in files


def test_get_file_tree_fallback_skips_hidden(git_repo: Path):
    """get_file_tree_fallback() should skip __pycache__ and similar dirs."""
    ws = Workspace(repo_path=git_repo, run_id="fallback-skip")
    # Create files in excluded dirs
    (git_repo / "__pycache__").mkdir()
    (git_repo / "__pycache__" / "test.pyc").write_text("bytecode")
    (git_repo / "node_modules").mkdir()
    (git_repo / "node_modules" / "pkg.js").write_text("module")

    files = ws.get_file_tree_fallback()
    assert not any("__pycache__" in f for f in files)
    assert not any("node_modules" in f for f in files)
    assert "README.md" in files


def test_detect_default_branch_fallback_to_current(git_repo: Path):
    """When no remote exists, _detect_default_branch should fall back to current branch."""
    ws = Workspace(repo_path=git_repo, run_id="detect-branch")
    # No remote configured, so it should detect current branch
    current = _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    assert ws._detect_default_branch() == current == "main"


def test_detect_default_branch_keeps_slashes_in_remote_head(bare_repo: Path):
    """origin/HEAD pointing at ``release/2.0`` gives the whole branch name."""
    sha = _git(bare_repo, "rev-parse", "HEAD").strip()
    _git(bare_repo, "update-ref", "refs/remotes/origin/release/2.0", sha)
    _git(bare_repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/release/2.0")
    ws = Workspace(repo_path=bare_repo, run_id="slash")
    assert ws._detect_default_branch() == "release/2.0"


def test_detect_default_branch_ignores_run_branch(bare_repo: Path):
    """A leftover run branch is never taken as the base branch."""
    Workspace(repo_path=bare_repo, run_id="first").initialize()
    assert Workspace(repo_path=bare_repo, run_id="second")._detect_default_branch() is None


def test_workspace_info_fields():
    """WorkspaceInfo fields should be accessible."""
    info = WorkspaceInfo(
        root=Path("/tmp/test"),
        branch="feature",
        base_branch="main",
        commit_sha="a" * 40,
        is_clean=True,
    )
    assert info.root == Path("/tmp/test")
    assert info.branch == "feature"
    assert info.base_branch == "main"
    assert info.commit_sha == "a" * 40
    assert info.is_clean is True


def test_commit_failure_raises_runtime_error(git_repo: Path):
    """commit() should raise RuntimeError when git commit fails (not nothing-to-commit)."""
    ws = Workspace(repo_path=git_repo, run_id="fail-commit")
    ws.initialize()

    # Apply a change so git commit is attempted
    ws.apply_files({"testfile.txt": "content"})

    # Mock _git to simulate a non-"nothing to commit" failure
    original_git = ws._git

    def mock_git(*args, **kwargs):
        if "commit" in args:
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = "some other error"
            return result
        return original_git(*args, **kwargs)

    ws._git = mock_git
    with pytest.raises(RuntimeError, match="git commit failed"):
        ws.commit("should fail")


def test_commit_error_keeps_the_real_message_not_crlf_warnings(git_repo: Path):
    """Git for Windows prefixes commit stderr with per-file CRLF warnings (#88)."""
    ws = Workspace(repo_path=git_repo, run_id="crlf-warn")
    ws.initialize()
    ws.apply_files({f"f{i}.py": "x = 1\n" for i in range(4)})
    original_git = ws._git
    warnings = "".join(
        f"warning: in the working copy of 'f{i}.py', LF will be replaced by CRLF "
        "the next time Git touches it\n"
        for i in range(8)
    )

    def mock_git(*args, **kwargs):
        if "commit" in args:
            return MagicMock(returncode=1, stdout="", stderr=warnings + "hook: lint failed\n")
        return original_git(*args, **kwargs)

    ws._git = mock_git
    with pytest.raises(RuntimeError) as excinfo:
        ws.commit("x")

    assert "hook: lint failed" in str(excinfo.value)
    assert "in the working copy" not in str(excinfo.value)


def test_get_file_tree_can_leave_out_untracked_files(bare_repo: Path):
    (bare_repo / "new.py").write_bytes(b"n = 1\n")
    ws = Workspace(repo_path=bare_repo, run_id="t")

    assert ws.get_file_tree(include_untracked=False) == ["app.py"]
    assert ws.get_file_tree() == ["app.py", "new.py"]


class TestFileTree:
    def test_fallback_is_posix_and_skips_internal_and_tooling_dirs(self, tmp_path: Path) -> None:
        for rel in (
            "tests/test_x.py",
            "src/runs/a.py",
            "src/reviews/b.py",
            ".env.example",
            ".github/workflows/ci.yml",
            ".vscode/settings.json",
            ".delegatron-venv/bin/python",
            ".delegatron-runs/r1/app.py",
            ".delegatron-worktrees/r1/t1/app.py",
            ".git/config",
            ".venv/lib/x.py",
            ".idea/workspace.xml",
            "node_modules/pkg/index.js",
            "runs/abc/events.jsonl",
            "reviews/review-1/summary.json",
            "_sandbox/workspace/x.py",
        ):
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_text("x", encoding="utf-8")

        tree = Workspace(repo_path=tmp_path, run_id="t").get_file_tree_fallback()

        assert tree == [
            ".env.example",
            ".github/workflows/ci.yml",
            ".vscode/settings.json",
            "src/reviews/b.py",
            "src/runs/a.py",
            "tests/test_x.py",
        ]

    def test_fallback_lists_user_packages_named_runs_or_reviews(self, tmp_path: Path) -> None:
        for rel in ("reviews/models.py", "runs/__init__.py", "_sandbox/x.py"):
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_text("x = 1\n", encoding="utf-8")

        tree = Workspace(repo_path=tmp_path, run_id="t").get_file_tree_fallback()

        assert tree == ["reviews/models.py", "runs/__init__.py"]

    def test_get_file_tree_never_raises_outside_git(self, tmp_path: Path) -> None:
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "app.py").write_text("x = 1\n", encoding="utf-8")

        assert Workspace(repo_path=tmp_path, run_id="t").get_file_tree() == ["pkg/app.py"]

    def test_get_file_tree_lists_untracked_but_not_ignored(self, bare_repo: Path) -> None:
        (bare_repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
        (bare_repo / "new.py").write_text("y = 2\n", encoding="utf-8")
        (bare_repo / "debug.log").write_text("noise\n", encoding="utf-8")
        (bare_repo / "carte_pokémon.py").write_text("z = 3\n", encoding="utf-8")
        (bare_repo / "runs" / "abc").mkdir(parents=True)
        (bare_repo / "runs" / "abc" / "events.jsonl").write_text("{}\n", encoding="utf-8")

        tree = Workspace(repo_path=bare_repo, run_id="t").get_file_tree()

        assert {"app.py", "new.py", ".gitignore", "carte_pokémon.py"} <= set(tree)
        assert "debug.log" not in tree
        assert not any(path.startswith("runs/") for path in tree)

    def test_get_file_tree_of_untracked_subdirectory(self, bare_repo: Path) -> None:
        (bare_repo / "scratch").mkdir()
        (bare_repo / "scratch" / "tool.py").write_text("t = 1\n", encoding="utf-8")

        assert Workspace(repo_path=bare_repo / "scratch", run_id="t").get_file_tree() == ["tool.py"]


class TestApplyFiles:
    def test_unsafe_paths_are_skipped_and_reported(self, tmp_path: Path) -> None:
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        ws = Workspace(repo_path=ws_root, run_id="t")

        result = ws.apply_files(
            {
                "card_deck.py": "x = 1\n",
                "/tests/test_card_deck.py": "y = 2\n",
                "../escaped.txt": "no",
                "C:/x.py": "no",
                "//host/share/x.py": "no",
                ".git/hooks/pre-commit": "no",
                "a:stream.py": "no",
                "z.py": "z = 3\n",
            }
        )

        assert result.written == ["card_deck.py", "z.py"]
        assert result.rejected == [
            "/tests/test_card_deck.py",
            "../escaped.txt",
            "C:/x.py",
            "//host/share/x.py",
            ".git/hooks/pre-commit",
            "a:stream.py",
        ]
        assert not (tmp_path / "escaped.txt").exists()
        assert (ws_root / "z.py").read_text() == "z = 3\n"

    @pytest.mark.parametrize(
        "path", [".GIT/config", ".Git/config", ".git./config", ".git /config", "GIT~1/config"]
    )
    def test_git_dir_spellings_are_rejected(self, bare_repo: Path, path: str) -> None:
        """Case-folding / dot-stripping filesystems map these onto .git (host code execution)."""
        config = bare_repo / ".git" / "config"
        before = config.read_bytes()
        ws = Workspace(repo_path=bare_repo, run_id="gitdir")

        result = ws.apply_files({path: '[core]\n\tfsmonitor = "touch pwned"\n'}, [path])

        assert result.written == []
        assert result.rejected == [path, path]
        assert config.read_bytes() == before
        assert ws.render_edits([{"path": path, "search": "[core]", "replace": "x"}]).files == {}

    def test_unencodable_text_is_rejected_and_the_rest_written(self, tmp_path: Path) -> None:
        result = Workspace(repo_path=tmp_path, run_id="t").apply_files(
            {"a.py": "a = 1\n", "bad.py": "s = '\ud83c'\n", "z.py": "z = 3\n"}
        )

        assert result.written == ["a.py", "z.py"]
        assert result.rejected == ["bad.py"]
        assert not (tmp_path / "bad.py").exists()
        assert (tmp_path / "z.py").read_bytes() == b"z = 3\n"

    def test_writes_utf8_bytes_and_keeps_line_endings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_write_text = Path.write_text

        def strict_write_text(self: Path, data: str, *args: Any, **kwargs: Any) -> int:
            assert kwargs.get("encoding"), "text written with the locale's encoding"
            return real_write_text(self, data, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", strict_write_text)
        files = {
            "run.sh": "#!/bin/bash\necho hi\n",
            "p.py": "print('Pokémon ✓ —')\n",
            "win.py": "a = 1\r\nb = 2\r\n",
        }
        Workspace(repo_path=tmp_path, run_id="t").apply_files(files)

        for rel, content in files.items():
            assert (tmp_path / rel).read_bytes() == content.encode("utf-8")

    def test_delete_files(self, bare_repo: Path) -> None:
        (bare_repo / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")
        _git(bare_repo, "add", "test_a.py")
        _git(bare_repo, "commit", "-q", "--no-verify", "-m", "add test")
        (bare_repo / "pkg").mkdir()
        ws = Workspace(repo_path=bare_repo, run_id="del")
        ws.initialize()

        absolute = str(bare_repo / "app.py")
        result = ws.apply_files(
            {"tests/test_a.py": "def test_a(): pass\n"},
            ["test_a.py", "missing.py", "../x.py", ".git/config", "pkg", absolute, "/app.py"],
        )
        ws.commit("move test")

        assert result.written == ["tests/test_a.py"]
        assert result.deleted == ["test_a.py"]
        assert result.rejected == ["../x.py", ".git/config", "pkg", absolute, "/app.py"]
        assert (bare_repo / "app.py").read_bytes() == b"x = 1\n"
        assert not (bare_repo / "test_a.py").exists()
        assert (bare_repo / "pkg").is_dir()
        assert (bare_repo / ".git" / "config").exists()
        status = _git(bare_repo, "show", "--no-renames", "--name-status", "--format=", "HEAD")
        assert sorted(status.splitlines()) == ["A\ttests/test_a.py", "D\ttest_a.py"]


class TestSnapshotAndDiffPaths:
    def test_snapshot_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_bytes(b"x = 1\r\n")
        ws = Workspace(repo_path=tmp_path, run_id="t")

        snap = ws.snapshot_files(["a.py", "./missing.py", "../outside.py"])

        assert snap == {"a.py": "x = 1\n", "missing.py": None}

    def test_diff_paths_reads_without_applying(self, git_repo: Path) -> None:
        diff = (
            "--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n # Test\n+more\n"
            "--- /dev/null\n+++ b/src/new.py\n@@ -0,0 +1 @@\n+x = 1\n"
        )
        ws = Workspace(repo_path=git_repo, run_id="t")

        assert ws.diff_paths(diff) == ["README.md", "src/new.py"]
        assert (git_repo / "README.md").read_text() == "# Test\n"
        assert not (git_repo / "src").exists()

    def test_unreadable_file_is_left_out_of_the_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "locked.py").write_bytes(b"secret = 1\n")
        (tmp_path / "ok.py").write_bytes(b"ok = 1\n")
        real_read_text = Path.read_text

        def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            if self.name == "locked.py":
                raise PermissionError(13, "Permission denied", str(self))
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)

        snap = Workspace(repo_path=tmp_path, run_id="t").snapshot_files(["locked.py", "ok.py"])

        assert snap == {"ok.py": "ok = 1\n"}

    def test_rename_diff_commits_the_old_path_deletion(self, bare_repo: Path) -> None:
        (bare_repo / "old_é.py").write_bytes(b"value = 1\n")
        _git(bare_repo, "add", "old_é.py")
        _git(bare_repo, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "old")
        ws = Workspace(repo_path=bare_repo, run_id="ren")
        ws.initialize()
        # git C-quotes non-ASCII paths in diff headers
        diff = (
            r'diff --git "a/old_\303\251.py" "b/new_\303\251.py"' + "\n"
            "similarity index 100%\n"
            r'rename from "old_\303\251.py"' + "\n"
            r'rename to "new_\303\251.py"' + "\n"
        )

        assert ws.diff_paths(diff) == ["new_é.py", "old_é.py"]
        assert ws.apply_patch(diff)
        ws.commit("chore(delegatron): iteration 1 — rename")

        status = _git(
            bare_repo,
            "-c",
            "core.quotepath=false",
            "show",
            "--no-renames",
            "--name-status",
            "--format=",
            "HEAD",
        )
        assert sorted(status.splitlines()) == ["A\tnew_é.py", "D\told_é.py"]
        assert _git(bare_repo, "status", "--porcelain").strip() == ""


class TestRenderEdits:
    def test_unique_match_returns_new_content(self, tmp_path: Path) -> None:
        (tmp_path / "engine.py").write_bytes(b"def f():\n    return 1\n\nTAIL = 'kept'\n")
        ws = Workspace(repo_path=tmp_path, run_id="t")

        result = ws.render_edits(
            [{"path": "./engine.py", "search": "return 1", "replace": "return 2"}]
        )

        assert result.errors == {}
        assert result.files == {"engine.py": "def f():\n    return 2\n\nTAIL = 'kept'\n"}
        assert (tmp_path / "engine.py").read_bytes() == b"def f():\n    return 1\n\nTAIL = 'kept'\n"

    @pytest.mark.parametrize("text", ["x = 1\n", "y = 0\ny = 0\n"])
    def test_zero_or_several_matches_fail_the_path(self, tmp_path: Path, text: str) -> None:
        (tmp_path / "a.py").write_bytes(text.encode())
        (tmp_path / "b.py").write_bytes(b"b = 1\n")

        result = Workspace(repo_path=tmp_path, run_id="t").render_edits(
            [
                {"path": "a.py", "search": "y = 0", "replace": "y = 9"},
                {"path": "b.py", "search": "b = 1", "replace": "b = 2"},
            ]
        )

        assert set(result.errors) == {"a.py"}
        assert result.files == {"b.py": "b = 2\n"}

    def test_already_applied_edit_is_a_no_op(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_bytes(b"value = 2\n")

        result = Workspace(repo_path=tmp_path, run_id="t").render_edits(
            [{"path": "a.py", "search": "value = 1", "replace": "value = 2"}]
        )

        assert result.errors == {}
        assert result.files == {"a.py": "value = 2\n"}

    @pytest.mark.parametrize("path", ["../x.py", "/etc/passwd", ".git/config", "C:/x.py"])
    def test_unsafe_path_is_rejected(self, tmp_path: Path, path: str) -> None:
        result = Workspace(repo_path=tmp_path, run_id="t").render_edits(
            [{"path": path, "search": "a", "replace": "b"}]
        )

        assert result.files == {}
        assert list(result.errors) == [path]

    def test_edits_to_one_file_apply_in_order_and_keep_crlf(self, tmp_path: Path) -> None:
        (tmp_path / "w.py").write_bytes(b"a = 1\r\nb = 2\r\n")

        result = Workspace(repo_path=tmp_path, run_id="t").render_edits(
            [
                {"path": "w.py", "search": "a = 1\nb = 2", "replace": "a = 10\nb = 2"},
                {"path": "w.py", "search": "a = 10", "replace": "a = 11", "count": 1},
            ]
        )

        assert result.files == {"w.py": "a = 11\r\nb = 2\r\n"}

    def test_missing_file_and_bad_fields_fail(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_bytes(b"a = 1\n")

        result = Workspace(repo_path=tmp_path, run_id="t").render_edits(
            [
                {"path": "missing.py", "search": "a", "replace": "b"},
                {"path": "a.py", "search": "", "replace": "b"},
            ]
        )

        assert result.files == {}
        assert set(result.errors) == {"missing.py", "a.py"}


class TestScopedCommit:
    def test_commit_stages_only_written_paths(self, bare_repo: Path) -> None:
        (bare_repo / "app.py").write_text("x = 2  # user WIP\n", encoding="utf-8")
        (bare_repo / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
        (bare_repo / "staged.py").write_text("s = 1\n", encoding="utf-8")
        _git(bare_repo, "add", "staged.py")
        ws = Workspace(repo_path=bare_repo, run_id="scoped")
        ws.initialize(allow_dirty=True)

        ws.apply_files({"new.py": "n = 1\n"})
        ws.commit("chore(delegatron): iteration 1 — add new")

        assert _git(bare_repo, "show", "--name-only", "--format=", "HEAD").split() == ["new.py"]
        status = _git(bare_repo, "status", "--porcelain")
        assert " M app.py" in status
        assert "?? .env" in status
        assert "A  staged.py" in status

    def test_pathspecs_are_literal(self, bare_repo: Path) -> None:
        ws = Workspace(repo_path=bare_repo, run_id="literal")
        ws.initialize()
        ws.apply_files({"[ab].py": "lit = 1\n"})
        (bare_repo / "a.py").write_text("untracked\n", encoding="utf-8")

        ws.commit("literal")

        assert _git(bare_repo, "show", "--name-only", "--format=", "HEAD").split() == ["[ab].py"]

    def test_ignored_path_is_not_committed(self, bare_repo: Path) -> None:
        (bare_repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
        _git(bare_repo, "add", ".gitignore")
        _git(bare_repo, "commit", "-q", "--no-verify", "-m", "ignore")
        ws = Workspace(repo_path=bare_repo, run_id="ignored")
        ws.initialize()

        ws.apply_files({"debug.log": "x\n", "real.py": "r = 1\n"})
        ws.commit("mixed")

        assert _git(bare_repo, "show", "--name-only", "--format=", "HEAD").split() == ["real.py"]

    def test_commit_refuses_off_the_run_branch(self, bare_repo: Path) -> None:
        ws = Workspace(repo_path=bare_repo, run_id="guard")
        with pytest.raises(RuntimeError, match="never initialized"):
            ws.commit("x")

        ws.initialize()
        ws.apply_files({"g.py": "g = 1\n"})
        _git(bare_repo, "checkout", "-q", "main")
        before = _git(bare_repo, "rev-parse", "main")

        with pytest.raises(RuntimeError, match="HEAD is main"):
            ws.commit("x")
        assert _git(bare_repo, "rev-parse", "main") == before

    def test_hooks_are_bypassed_by_default(self, bare_repo: Path) -> None:
        hook = bare_repo / ".git" / "hooks" / "commit-msg"
        hook.write_text("#!/bin/sh\necho rejected >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        pre = bare_repo / ".git" / "hooks" / "pre-commit"
        pre.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        pre.chmod(0o755)
        ws = Workspace(repo_path=bare_repo, run_id="hooks")
        ws.initialize()

        ws.apply_files({"h.py": "h = 1\n"})

        assert ws.commit("not conventional") is not None

    def test_failing_hook_keeps_paths_pending(self, bare_repo: Path) -> None:
        hook = bare_repo / ".git" / "hooks" / "commit-msg"
        hook.write_text("#!/bin/sh\necho rejected by hook >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        ws = Workspace(repo_path=bare_repo, run_id="hooks-on", run_git_hooks=True)
        ws.initialize()
        ws.apply_files({"first.py": "f = 1\n"})

        with pytest.raises(RuntimeError, match="rejected by hook"):
            ws.commit("x")

        hook.unlink()
        ws.apply_files({"second.py": "s = 1\n"})
        ws.commit("y")
        committed = _git(bare_repo, "show", "--name-only", "--format=", "HEAD").split()
        assert committed == ["first.py", "second.py"]

    def test_signing_config_does_not_block_commits(self, bare_repo: Path) -> None:
        _git(bare_repo, "config", "commit.gpgsign", "true")
        _git(bare_repo, "config", "gpg.program", "definitely-not-a-gpg-binary")
        ws = Workspace(repo_path=bare_repo, run_id="gpg")
        ws.initialize()
        ws.apply_files({"s.py": "s = 1\n"})

        assert ws.commit("signed?") is not None

    def test_fallback_identity_when_none_configured(
        self, bare_repo: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch
    ) -> None:
        home = tmp_path_factory.mktemp("home")
        empty = home / "gitconfig"
        empty.write_text("", encoding="utf-8")
        for var in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
            monkeypatch.setenv(var, str(home))
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        for var in ("NAME", "EMAIL"):
            monkeypatch.delenv(f"GIT_AUTHOR_{var}", raising=False)
            monkeypatch.delenv(f"GIT_COMMITTER_{var}", raising=False)
        ws = Workspace(repo_path=bare_repo, run_id="ident")
        ws.initialize()
        ws.apply_files({"i.py": "i = 1\n"})

        ws.commit("who")

        author = _git(bare_repo, "log", "-1", "--format=%an <%ae>")
        assert author.strip() == "Delegatron <run@delegatron>"

        _git(bare_repo, "config", "user.name", "Repo User")
        _git(bare_repo, "config", "user.email", "repo@example.com")
        ws.apply_files({"j.py": "j = 1\n"})
        ws.commit("who now")
        assert _git(bare_repo, "log", "-1", "--format=%an").strip() == "Repo User"


@pytest.mark.parametrize("module", ["workspace.py", "utils/cost.py"])
def test_text_io_always_names_an_encoding(module: str) -> None:
    """Locale-dependent text I/O (cp1252 + CRLF on Windows) must not creep back (#117)."""
    source_root = Path(__file__).resolve().parents[2] / "src" / "delegatron"
    tree = ast.parse((source_root / module).read_text(encoding="utf-8"))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr in TEXT_IO_METHODS)
            or (isinstance(node.func, ast.Name) and node.func.id == "open")
        )
        and not any(keyword.arg == "encoding" for keyword in node.keywords)
    ]
    assert offenders == []
