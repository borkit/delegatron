"""Unit tests for Pipeline._apply_to_workspace.

Regression tests for the non-git workspace bug: runs against a workspace
that is not a git repo used to silently fail because the apply step was
gated on ``is_git_repo``, leaving ``patch_applied=False`` which the
acceptance hard-gate rejected — even when tests passed.

Also covers the post-apply bookkeeping (#66): pre-patch snapshot, changed
paths, no-op flag, per-file ownership and the file-tree refresh; commit
failures that must not un-apply a patch (#88); deletions (#109); unsafe paths
(#110); and import validation against the post-apply tree (#65).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from delegatron.config import Config, PipelineConfig, RoleConfig
from delegatron.orchestrator.graph import Pipeline
from delegatron.workspace import Workspace


def _mock_config() -> Config:
    return Config(
        architect=RoleConfig(provider="mock", model="mock-architect"),
        implementer=RoleConfig(provider="mock", model="mock-implementer"),
        reviewer=RoleConfig(provider="mock", model="mock-reviewer"),
        pipeline=PipelineConfig(max_iterations=1, max_cost_usd=1.0, max_wall_time_minutes=1),
    )


def _make_pipeline(workspace_root: Path) -> Pipeline:
    return Pipeline(
        config=_mock_config(),
        workspace_root=workspace_root,
        run_id="test-apply",
    )


def _fake_state(files: dict[str, str] | None = None, diff: str = "") -> dict:
    patch: dict = {"rationale": "unit test"}
    if files is not None:
        patch["files"] = files
    if diff:
        patch["diff_unified"] = diff
    return {
        "iteration": 1,
        "current_patch": patch,
        "messages": [],
    }


def _apply(
    pipeline: Pipeline,
    workspace: Workspace,
    state: dict,
    *,
    files: dict[str, str] | None = None,
    deletes: list[str] | None = None,
    iteration: int,
    task_id: str = "t1",
    git: bool = False,
) -> None:
    """Apply a files patch at *iteration* for *task_id*, reusing *state*."""
    patch: dict = {"rationale": "unit test", "files": files or {}}
    if deletes is not None:
        patch["delete_files"] = deletes
    state.update({"iteration": iteration, "current_task": {"task_id": task_id}})
    state["current_patch"] = patch
    pipeline._apply_to_workspace(state, workspace, is_git_repo=git)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@test.com", *args],
        cwd=cwd,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull},  # no global hooks/signing
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _events(state: dict, name: str) -> list[dict]:
    return [m for m in state["messages"] if m.get("event") == name]


CARD_DECK = "class Card:\n    pass\n"
CARD_TEST = "from card_deck import Card\n\n\ndef test_card():\n    assert Card()\n"


class TestApplyToNonGitWorkspace:
    """Files-map patches should apply to a non-git workspace."""

    def test_files_map_applies_without_git(self, tmp_path: Path) -> None:
        pipeline = _make_pipeline(tmp_path)
        workspace = Workspace(repo_path=tmp_path, run_id="test-apply")
        state = _fake_state(files={"hello.py": "print('hi')\n"})

        pipeline._apply_to_workspace(state, workspace, is_git_repo=False)

        assert state["patch_applied"] is True
        assert (tmp_path / "hello.py").read_text() == "print('hi')\n"
        # No commit event should be emitted for non-git workspaces
        assert not any(m.get("event") == "workspace_commit" for m in state["messages"])

    def test_diff_only_rejected_without_git(self, tmp_path: Path) -> None:
        """A unified-diff-only patch needs git apply; skip with clear signal."""
        pipeline = _make_pipeline(tmp_path)
        workspace = Workspace(repo_path=tmp_path, run_id="test-apply")
        state = _fake_state(diff="--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+new\n")

        pipeline._apply_to_workspace(state, workspace, is_git_repo=False)

        assert state["patch_applied"] is False

    def test_empty_patch_does_nothing(self, tmp_path: Path) -> None:
        pipeline = _make_pipeline(tmp_path)
        workspace = Workspace(repo_path=tmp_path, run_id="test-apply")
        state = _fake_state()

        pipeline._apply_to_workspace(state, workspace, is_git_repo=False)

        assert state["patch_applied"] is False


class TestApplyToGitWorkspace:
    """Git-repo path: apply files AND commit."""

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,  # no global hooks/signing
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
            "HOME": str(tmp_path),
        }
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=env)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-q", "-m", "init"],
            cwd=tmp_path,
            check=True,
            env=env,
        )
        return tmp_path

    def test_files_map_applies_and_commits(self, git_repo: Path) -> None:
        pipeline = _make_pipeline(git_repo)
        workspace = Workspace(repo_path=git_repo, run_id="test-apply-git")
        # Commits only land on an initialized run branch (#87).
        workspace.initialize()
        state = _fake_state(files={"fib.py": "def fib(n): return n\n"})

        pipeline._apply_to_workspace(state, workspace, is_git_repo=True)

        assert state["patch_applied"] is True
        assert (git_repo / "fib.py").read_text() == "def fib(n): return n\n"
        # Commit event should be emitted
        commit_events = [m for m in state["messages"] if m.get("event") == "workspace_commit"]
        assert len(commit_events) == 1
        assert commit_events[0]["iteration"] == 1


@pytest.fixture
def git_ws(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-q", "-m", "init")
    return tmp_path


class TestBookkeeping:
    @pytest.mark.parametrize("git", [False, True])
    def test_first_apply_records_everything(self, tmp_path: Path, git: bool) -> None:
        if git:
            _git(tmp_path, "init", "-q", "-b", "main")
            _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "i")
        pipeline = _make_pipeline(tmp_path)
        workspace = Workspace(repo_path=tmp_path, run_id="bk")
        if git:
            workspace.initialize()
        state: dict = {"messages": []}

        _apply(
            pipeline,
            workspace,
            state,
            files={"card_deck.py": CARD_DECK, "tests/test_card_deck.py": CARD_TEST},
            iteration=1,
            git=git,
        )

        assert state["patch_noop"] is False
        assert state["_last_changed_files"] == ["card_deck.py", "tests/test_card_deck.py"]
        assert state["_pre_patch_snapshot"] == {
            "card_deck.py": None,
            "tests/test_card_deck.py": None,
        }
        assert state["_file_history"] == {
            "card_deck.py": {"task_id": "t1", "iteration": 1},
            "tests/test_card_deck.py": {"task_id": "t1", "iteration": 1},
        }
        # Regression for state_004.json showing only `.gitignore`.
        assert {"card_deck.py", "tests/test_card_deck.py"} <= set(state["file_tree"])
        commits = _events(state, "workspace_commit")
        if git:
            assert commits[0]["changed_files"] == ["card_deck.py", "tests/test_card_deck.py"]
            assert _git(tmp_path, "log", "-1", "--format=%s").startswith(
                "chore(delegatron): iteration 1"
            )
        else:
            assert commits == []

    @pytest.mark.parametrize("git", [False, True])
    def test_identical_resubmission_is_a_noop(self, tmp_path: Path, git: bool) -> None:
        if git:
            _git(tmp_path, "init", "-q", "-b", "main")
            _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "i")
        pipeline = _make_pipeline(tmp_path)
        workspace = Workspace(repo_path=tmp_path, run_id="noop")
        if git:
            workspace.initialize()
        state: dict = {"messages": []}
        files = {"simulate_deal.py": "print(1)\n", "tests/test_simulate_deal.py": CARD_TEST}

        _apply(pipeline, workspace, state, files=files, iteration=5, task_id="t3", git=git)
        commits_before = len(_events(state, "workspace_commit"))
        _apply(pipeline, workspace, state, files=files, iteration=6, task_id="t3", git=git)

        assert state["patch_noop"] is True
        assert state["patch_applied"] is True
        assert state["_last_changed_files"] == []
        assert len(_events(state, "workspace_commit")) == commits_before
        assert all(entry["iteration"] == 5 for entry in state["_file_history"].values())

    def test_crlf_on_disk_vs_lf_in_patch_is_unchanged(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")
        pipeline = _make_pipeline(tmp_path)
        state: dict = {"messages": []}

        _apply(
            pipeline,
            Workspace(repo_path=tmp_path, run_id="crlf"),
            state,
            files={"a.py": "x = 1\ny = 2\n"},
            iteration=1,
        )

        assert state["patch_noop"] is True

    def test_only_the_differing_file_is_changed(self, tmp_path: Path) -> None:
        (tmp_path / "same.py").write_text("same\n", encoding="utf-8")
        (tmp_path / "diff.py").write_text("old\n", encoding="utf-8")
        pipeline = _make_pipeline(tmp_path)
        state: dict = {"messages": []}

        _apply(
            pipeline,
            Workspace(repo_path=tmp_path, run_id="one"),
            state,
            files={"same.py": "same\n", "diff.py": "new\n"},
            iteration=1,
        )

        assert state["_last_changed_files"] == ["diff.py"]
        assert state["_pre_patch_snapshot"] == {"diff.py": "old\n"}

    def test_disjoint_patches_add_up_in_the_history(self, tmp_path: Path) -> None:
        """#120: the history holds every file the run wrote, not only the last patch's."""
        pipeline = _make_pipeline(tmp_path)
        workspace = Workspace(repo_path=tmp_path, run_id="sum")
        state: dict = {"messages": []}

        _apply(pipeline, workspace, state, files={"a.py": "a = 1\n"}, iteration=1)
        _apply(pipeline, workspace, state, files={"b.py": "b = 1\n"}, iteration=2, task_id="t2")

        assert state["_file_history"] == {
            "a.py": {"task_id": "t1", "iteration": 1},
            "b.py": {"task_id": "t2", "iteration": 2},
        }
        assert state["_last_changed_files"] == ["b.py"]

    def test_a_commit_lists_the_files_its_patch_deleted(self, git_ws: Path) -> None:
        """#109: ``workspace_commit`` names the deleted files apart."""
        pipeline = _make_pipeline(git_ws)
        workspace = Workspace(repo_path=git_ws, run_id="del")
        workspace.initialize()
        state: dict = {"messages": []}
        _apply(pipeline, workspace, state, files={"old.py": "x = 1\n"}, iteration=1, git=True)

        _apply(
            pipeline,
            workspace,
            state,
            files={"new.py": "y = 2\n"},
            deletes=["old.py"],
            iteration=2,
            git=True,
        )

        first, second = _events(state, "workspace_commit")
        assert first["files_deleted"] == []
        assert second["changed_files"] == ["new.py", "old.py"]
        assert second["files_deleted"] == ["old.py"]
        assert "old.py" not in _git(git_ws, "ls-files")

    def test_ownership_moves_to_the_task_that_rewrites_a_file(self, tmp_path: Path) -> None:
        pipeline = _make_pipeline(tmp_path)
        workspace = Workspace(repo_path=tmp_path, run_id="own")
        state: dict = {"messages": []}
        t1_tests = "def test_one():\n    pass\n"

        _apply(pipeline, workspace, state, files={"tests/test_card_deck.py": t1_tests}, iteration=1)
        _apply(
            pipeline,
            workspace,
            state,
            files={"tests/test_card_deck.py": "def test_two():\n    pass\n"},
            iteration=2,
            task_id="t2",
        )

        assert state["_file_history"]["tests/test_card_deck.py"] == {
            "task_id": "t2",
            "iteration": 2,
        }
        assert state["_pre_patch_snapshot"]["tests/test_card_deck.py"] == t1_tests

    def test_diff_only_patch_in_git(self, git_ws: Path) -> None:
        (git_ws / "README.md").write_text("# T\n", encoding="utf-8")
        _git(git_ws, "add", "README.md")
        _git(git_ws, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "readme")
        pipeline = _make_pipeline(git_ws)
        workspace = Workspace(repo_path=git_ws, run_id="diff")
        workspace.initialize()
        state = _fake_state(diff="--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n # T\n+more\n")
        state["current_task"] = {"task_id": "t1"}

        pipeline._apply_to_workspace(state, workspace, is_git_repo=True)

        assert state["_last_changed_files"] == ["README.md"]
        assert state["_pre_patch_snapshot"] == {"README.md": "# T\n"}
        assert len(_events(state, "workspace_commit")) == 1

    def test_unsafe_paths_are_skipped_and_reported(self, tmp_path: Path) -> None:
        pipeline = _make_pipeline(tmp_path / "ws")
        (tmp_path / "ws").mkdir()
        workspace = Workspace(repo_path=tmp_path / "ws", run_id="unsafe")
        state: dict = {"messages": []}

        _apply(
            pipeline,
            workspace,
            state,
            files={"ok.py": "ok = 1\n", "../evil.py": "evil = 1\n"},
            iteration=1,
        )

        assert state["patch_applied"] is True
        assert state["_rejected_paths"] == ["../evil.py"]
        assert _events(state, "paths_rejected")[0]["paths"] == ["../evil.py"]
        assert not (tmp_path / "evil.py").exists()
        assert list(state["_file_history"]) == ["ok.py"]

    def test_all_paths_rejected_is_not_applied(self, tmp_path: Path) -> None:
        (tmp_path / "existing.py").write_text("e = 1\n", encoding="utf-8")
        pipeline = _make_pipeline(tmp_path)
        state: dict = {"messages": [], "_file_history": {"existing.py": {"task_id": "t0"}}}

        _apply(
            pipeline,
            Workspace(repo_path=tmp_path, run_id="bad"),
            state,
            files={"../evil.py": "x"},
            iteration=1,
        )

        assert state["patch_applied"] is False
        assert state["_file_history"] == {"existing.py": {"task_id": "t0"}}
        assert state["file_tree"] == ["existing.py"]

    def test_empty_response_keeps_applied_state_after_an_attempt(self, tmp_path: Path) -> None:
        pipeline = _make_pipeline(tmp_path)
        state = _fake_state()
        state.update({"patch_applied": True, "test_report": {"passed": True}})

        pipeline._apply_to_workspace(
            state, Workspace(repo_path=tmp_path, run_id="e"), is_git_repo=False
        )

        assert state["patch_applied"] is True
        assert state["patch_noop"] is True


class TestDeletions:
    def test_deletion_only_patch_applies_and_commits(self, git_ws: Path) -> None:
        (git_ws / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")
        _git(git_ws, "add", "test_a.py")
        _git(git_ws, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "a")
        pipeline = _make_pipeline(git_ws)
        workspace = Workspace(repo_path=git_ws, run_id="del")
        workspace.initialize()
        state: dict = {"messages": []}

        _apply(pipeline, workspace, state, deletes=["test_a.py"], iteration=1, git=True)

        assert state["patch_applied"] is True
        assert state["_file_history"]["test_a.py"] == {
            "task_id": "t1",
            "iteration": 1,
            "deleted": True,
        }
        assert _git(git_ws, "show", "--name-status", "--format=", "HEAD").split() == [
            "D",
            "test_a.py",
        ]
        assert "test_a.py" not in state["file_tree"]


class TestCommitFailure:
    def test_failed_commit_keeps_patch_applied(
        self, git_ws: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pipeline = _make_pipeline(git_ws)
        workspace = Workspace(repo_path=git_ws, run_id="cf", run_git_hooks=True)
        workspace.initialize()
        hook = git_ws / ".git" / "hooks" / "commit-msg"
        hook.write_text("#!/bin/sh\necho commitlint says no >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        state: dict = {"messages": []}

        _apply(pipeline, workspace, state, files={"b.py": "b = 1\n"}, iteration=1, git=True)

        assert state["patch_applied"] is True
        assert "commitlint says no" in state["workspace_commit_error"]
        assert "commitlint says no" in _events(state, "commit_failed")[0]["error"]

        hook.unlink()
        _apply(pipeline, workspace, state, files={"c.py": "c = 1\n"}, iteration=2, git=True)

        assert state["workspace_commit_error"] == ""
        assert set(_git(git_ws, "show", "--name-only", "--format=", "HEAD").split()) == {
            "b.py",
            "c.py",
        }


class TestImportValidationAfterApply:
    def test_same_patch_modules_resolve(self, tmp_path: Path) -> None:
        pipeline = _make_pipeline(tmp_path)
        state: dict = {"messages": [], "import_issues": [{"title": "stale"}]}
        _apply(
            pipeline,
            Workspace(repo_path=tmp_path, run_id="imp"),
            state,
            files={"card_deck.py": CARD_DECK, "tests/test_card_deck.py": CARD_TEST},
            iteration=1,
        )

        pipeline._run_import_validation(state, None)

        assert state["import_issues"] == []
        assert _events(state, "import_issue") == []

    def test_issues_carry_file_evidence_and_source(self, tmp_path: Path) -> None:
        pipeline = _make_pipeline(tmp_path)
        state: dict = {"messages": []}
        _apply(
            pipeline,
            Workspace(repo_path=tmp_path, run_id="imp2"),
            state,
            files={"app.py": "import numpy_but_not_installed\n"},
            iteration=1,
        )

        pipeline._run_import_validation(state, None)

        [issue] = state["import_issues"]
        assert issue["file"] == issue["evidence"] == "app.py"
        assert issue["source"] == "import_validator"
        assert len(_events(state, "import_issue")) == 1

    def test_no_patch_resets_issues(self, tmp_path: Path) -> None:
        pipeline = _make_pipeline(tmp_path)
        state: dict = {"messages": [], "current_patch": None, "import_issues": [{"title": "x"}]}

        pipeline._run_import_validation(state, None)

        assert state["import_issues"] == []


class TestUnreadableFiles:
    """A file the change check cannot read never fails or hides the apply (#66)."""

    def test_unreadable_after_the_write_counts_as_changed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pipeline = _make_pipeline(tmp_path)
        workspace = Workspace(repo_path=tmp_path, run_id="unread")
        original = Workspace.snapshot_files
        calls: list[int] = []

        def snapshot(self: Workspace, paths: list[str]) -> dict[str, str | None]:
            # The second snapshot (after the write) cannot read b.py (a transient lock).
            calls.append(1)
            taken = original(self, paths)
            if len(calls) == 2:
                taken.pop("b.py", None)
            return taken

        monkeypatch.setattr(Workspace, "snapshot_files", snapshot)
        state: dict = {"messages": []}

        _apply(
            pipeline, workspace, state, files={"a.py": "a = 1\n", "b.py": "b = 1\n"}, iteration=1
        )

        assert state["patch_applied"] is True
        assert state["_last_changed_files"] == ["a.py", "b.py"]
        assert state["_file_history"]["b.py"] == {"task_id": "t1", "iteration": 1}
        assert {"a.py", "b.py"} <= set(state["file_tree"])

    def test_unreadable_before_the_write_does_not_reject_the_patch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "locked.py").write_text("old = 1\n", encoding="utf-8")
        pipeline = _make_pipeline(tmp_path)
        workspace = Workspace(repo_path=tmp_path, run_id="unread2")
        original = Workspace.snapshot_files
        calls: list[int] = []

        def snapshot(self: Workspace, paths: list[str]) -> dict[str, str | None]:
            calls.append(1)
            taken = original(self, paths)
            if len(calls) == 1:
                taken.pop("locked.py", None)
            return taken

        monkeypatch.setattr(Workspace, "snapshot_files", snapshot)
        state: dict = {"messages": []}

        _apply(pipeline, workspace, state, files={"locked.py": "new = 1\n"}, iteration=1)

        assert state["patch_applied"] is True
        assert state["_last_changed_files"] == ["locked.py"]
        assert (tmp_path / "locked.py").read_text(encoding="utf-8") == "new = 1\n"
        assert _events(state, "patch_apply_failed") == []

    def test_a_failing_change_check_still_refreshes_the_file_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pipeline = _make_pipeline(tmp_path)
        workspace = Workspace(repo_path=tmp_path, run_id="unread3")

        def broken(state: dict, workspace: Workspace, touched: list[str]) -> list[str]:
            raise OSError("disk went away")

        monkeypatch.setattr(Pipeline, "_record_changes", staticmethod(broken))
        state: dict = {"messages": []}

        with pytest.raises(OSError, match="disk went away"):
            _apply(pipeline, workspace, state, files={"a.py": "a = 1\n"}, iteration=1)

        assert "a.py" in state["file_tree"]


class TestProjectRedetection:
    """project_info follows what patches add to the project (#66)."""

    def test_requirements_add_python_and_a_py_patch_changes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from delegatron.orchestrator import graph

        created: list[dict] = []
        monkeypatch.setattr(graph, "create_sandbox", lambda **spec: created.append(spec) or spec)
        pipeline = _make_pipeline(tmp_path)
        workspace = Workspace(repo_path=tmp_path, run_id="detect")
        state: dict = {"messages": [], "project_info": {"languages": []}}
        spec = pipeline._sandbox_spec(graph.detect_project(tmp_path))
        sandbox: object = object()

        _apply(pipeline, workspace, state, files={"requirements.txt": "requests\n"}, iteration=1)
        spec, sandbox = pipeline._reconfigure_sandbox(state, None, spec, sandbox)  # type: ignore[arg-type]

        assert state["project_info"]["languages"] == ["python"]
        assert spec["primary_language"] == "python"
        assert len(created) == 1
        detected = dict(state["project_info"])

        _apply(pipeline, workspace, state, files={"app.py": "x = 1\n"}, iteration=2)
        same_spec, same_sandbox = pipeline._reconfigure_sandbox(state, None, spec, sandbox)  # type: ignore[arg-type]

        assert state["project_info"] == detected
        assert (same_spec, same_sandbox) == (spec, sandbox)
        assert len(created) == 1
