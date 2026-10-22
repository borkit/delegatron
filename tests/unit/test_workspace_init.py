"""Workspace.initialize(): idempotent, transactional run-branch setup (#87, #88)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from delegatron.workspace import DirtyWorkspaceError, Workspace, WorkspaceInitError

RUN_BRANCH = "delegatron/run-abc123"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@test.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(cwd: Path, message: str) -> None:
    _git(cwd, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "app.py").write_bytes(b"x = 1\n")
    _git(root, "add", "app.py")
    _commit(root, "init")
    return root


@pytest.fixture
def clone(repo: Path, tmp_path: Path) -> Path:
    """A clone of ``repo`` (origin/HEAD -> main), the precondition for resume bugs."""
    target = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(repo), str(target))
    return target


def _branch(cwd: Path) -> str:
    return _git(cwd, "branch", "--show-current")


class TestIdempotentInitialize:
    def test_reinitialize_checks_out_existing_run_branch(self, clone: Path) -> None:
        ws = Workspace(repo_path=clone, run_id="abc123")
        ws.initialize()
        ws.apply_files({"feature.py": "x = 1\n"})
        ws.commit("chore(delegatron): iteration 1 — feature")
        _git(clone, "checkout", "-q", "main")
        main_before = _git(clone, "rev-parse", "main")
        commits_before = _git(clone, "rev-list", "--count", RUN_BRANCH)

        ws2 = Workspace(repo_path=clone, run_id="abc123")
        ws2.initialize()

        assert _branch(clone) == RUN_BRANCH
        assert ws2.resumed is True
        assert ws2.original_branch == "main"
        assert (clone / "feature.py").exists()
        assert ws2.initial_sha == _git(clone, "merge-base", "main", RUN_BRANCH)
        assert _git(clone, "rev-list", "--count", RUN_BRANCH) == commits_before

        ws2.apply_files({"more.py": "y = 2\n"})
        ws2.commit("chore(delegatron): iteration 2 — more")
        assert _git(clone, "rev-parse", "main") == main_before
        assert _git(clone, "log", "-1", "--format=%s", RUN_BRANCH).endswith("— more")

    def test_reinitialize_without_remote_keeps_extra_commit(self, repo: Path) -> None:
        """No origin: the existing run branch, with a commit made outside the run, is reused."""
        ws = Workspace(repo_path=repo, run_id="abc123")
        ws.initialize()
        fork = ws.initial_sha
        (repo / "feature.py").write_bytes(b"f = 1\n")
        _git(repo, "add", "feature.py")
        _commit(repo, "extra commit on the run branch")
        head = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-q", "main")

        ws2 = Workspace(repo_path=repo, run_id="abc123")
        ws2.initialize()

        assert _branch(repo) == RUN_BRANCH
        assert _git(repo, "rev-parse", "HEAD") == head
        assert (repo / "feature.py").exists()
        assert ws2.resumed is True
        assert ws2.initial_sha == fork != ""
        assert ws2.list_changed_files() == ["feature.py"]

    def test_resume_keeps_the_recorded_fork_point_after_the_base_moves(self, repo: Path) -> None:
        ws = Workspace(repo_path=repo, run_id="abc123")
        ws.initialize()
        fork = ws.initial_sha
        _git(repo, "checkout", "-q", "main")
        (repo / "later.py").write_bytes(b"l = 1\n")
        _git(repo, "add", "later.py")
        _commit(repo, "main moves on")
        _git(repo, "branch", "-q", "-m", "main", "trunk")

        ws2 = Workspace(repo_path=repo, run_id="abc123")
        ws2.initialize()

        assert ws2.initial_sha == fork
        assert ws2.snapshot().base_branch == "main"

    def test_dirty_tree_allowed_when_already_on_run_branch(self, repo: Path) -> None:
        Workspace(repo_path=repo, run_id="abc123").initialize()
        (repo / "half_written.py").write_text("partial\n", encoding="utf-8")

        ws = Workspace(repo_path=repo, run_id="abc123")
        ws.initialize()

        assert ws.branch == RUN_BRANCH

    def test_never_pulls_or_fetches(self, clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        commands: list[list[str]] = []
        real_run = subprocess.run

        def recording_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
            commands.append(list(cmd))
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", recording_run)
        Workspace(repo_path=clone, run_id="abc123").initialize()

        assert not any("pull" in cmd or "fetch" in cmd for cmd in commands)


class TestCreateRunBranch:
    def test_linked_worktree_with_base_checked_out_elsewhere(
        self, repo: Path, tmp_path: Path
    ) -> None:
        linked = tmp_path / "wt"
        _git(repo, "worktree", "add", "-q", "-b", "feature-x", str(linked))

        ws = Workspace(repo_path=linked, run_id="abc123", base_branch="main")
        ws.initialize()

        assert _branch(linked) == RUN_BRANCH
        assert _git(linked, "merge-base", "main", RUN_BRANCH) == _git(repo, "rev-parse", "main")

    def test_unborn_head_gets_a_base_commit(self, tmp_path: Path) -> None:
        _git(tmp_path, "init", "-q", "-b", "master")

        ws = Workspace(repo_path=tmp_path, run_id="abc123")
        ws.initialize()

        assert _branch(tmp_path) == RUN_BRANCH
        assert _git(tmp_path, "rev-list", "--count", "master") == "1"

    def test_unborn_base_commit_never_sweeps_the_index(self, tmp_path: Path) -> None:
        _git(tmp_path, "init", "-q", "-b", "main")
        (tmp_path / "mine.py").write_bytes(b"mine = 1\n")
        _git(tmp_path, "add", "mine.py")

        ws = Workspace(repo_path=tmp_path, run_id="abc123")
        ws.initialize(allow_dirty=True)
        ws.apply_files({"run.py": "r = 1\n"})
        ws.commit("chore(delegatron): iteration 1 — run")

        assert _git(tmp_path, "show", "--name-only", "--format=", "main") == ""
        assert _git(tmp_path, "show", "--name-only", "--format=", "HEAD") == "run.py"
        assert "A  mine.py" in _git(tmp_path, "status", "--porcelain")

    def test_forks_from_the_checked_out_branch_by_default(self, repo: Path) -> None:
        _git(repo, "checkout", "-q", "-b", "feature")
        (repo / "feat.py").write_bytes(b"f = 1\n")
        _git(repo, "add", "feat.py")
        _commit(repo, "feature work")
        feature = _git(repo, "rev-parse", "feature")

        ws = Workspace(repo_path=repo, run_id="abc123")
        ws.initialize()

        assert ws.initial_sha == feature
        assert ws.snapshot().base_branch == "feature"
        assert (repo / "feat.py").exists()

    def test_requested_base_branch_wins_over_the_checkout(self, repo: Path) -> None:
        main = _git(repo, "rev-parse", "main")
        _git(repo, "checkout", "-q", "-b", "dev")
        (repo / "dev.py").write_bytes(b"d = 1\n")
        _git(repo, "add", "dev.py")
        _commit(repo, "dev work")
        dev = _git(repo, "rev-parse", "dev")
        _git(repo, "checkout", "-q", "main")

        Workspace(repo_path=repo, run_id="abc123", base_branch="dev").initialize()

        assert _git(repo, "merge-base", "dev", RUN_BRANCH) == dev != main
        assert (repo / "dev.py").exists()

    def test_clone_of_a_non_default_branch_forks_from_it(self, repo: Path, tmp_path: Path) -> None:
        """``git clone -b dev``: only local ``dev`` exists while origin/HEAD is ``main``."""
        _git(repo, "checkout", "-q", "-b", "dev")
        (repo / "dev.py").write_bytes(b"d = 1\n")
        _git(repo, "add", "dev.py")
        _commit(repo, "dev work")
        _git(repo, "checkout", "-q", "main")
        clone = tmp_path / "devclone"
        _git(tmp_path, "clone", "-q", "-b", "dev", str(repo), str(clone))

        for run_id, base in (("a1", ""), ("a2", "main"), ("a3", "dev")):
            _git(clone, "checkout", "-q", "dev")
            ws = Workspace(repo_path=clone, run_id=run_id, base_branch=base)
            ws.initialize()
            assert (clone / "dev.py").exists(), base
            assert ws.initial_sha == _git(clone, "rev-parse", "dev")

    def test_requested_base_only_on_origin_is_used(self, repo: Path, tmp_path: Path) -> None:
        _git(repo, "branch", "-q", "release")
        clone = tmp_path / "rclone"
        _git(tmp_path, "clone", "-q", str(repo), str(clone))
        assert "release" not in _git(clone, "branch", "--list")

        ws = Workspace(repo_path=clone, run_id="abc123", base_branch="release")
        ws.initialize()

        assert ws.initial_sha == _git(clone, "rev-parse", "origin/release")
        assert _branch(clone) == RUN_BRANCH
        assert "release" not in _git(clone, "branch", "--list").split()

    def test_base_can_be_a_commit_sha(self, repo: Path) -> None:
        """SWE-bench checks out the instance's base commit (detached) and asks for it."""
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "later.py").write_bytes(b"l = 1\n")
        _git(repo, "add", "later.py")
        _commit(repo, "later")
        _git(repo, "checkout", "-q", "--detach", base)

        ws = Workspace(repo_path=repo, run_id="abc123", base_branch=base)
        ws.initialize()

        assert ws.initial_sha == base
        assert not (repo / "later.py").exists()
        assert Workspace.checked_out_branch(repo) == RUN_BRANCH

    def test_checked_out_branch_is_empty_when_detached_or_not_git(
        self, repo: Path, tmp_path: Path
    ) -> None:
        assert Workspace.checked_out_branch(repo) == "main"
        _git(repo, "checkout", "-q", "--detach", "HEAD")
        assert Workspace.checked_out_branch(repo) == ""
        plain = tmp_path / "plain"
        plain.mkdir()
        assert Workspace.checked_out_branch(plain) == ""

    def test_detached_head_forks_from_the_default_branch(self, clone: Path) -> None:
        _git(clone, "checkout", "-q", "--detach", "HEAD")

        ws = Workspace(repo_path=clone, run_id="abc123")
        ws.initialize()

        assert ws.snapshot().base_branch == "main"
        assert ws.initial_sha == _git(clone, "rev-parse", "main")


class TestFreshRun:
    def test_fresh_run_never_reuses_an_existing_run_branch(self, repo: Path) -> None:
        first = Workspace(repo_path=repo, run_id="bench-default")
        first.initialize()
        first.apply_files({"gen.py": "old = 1\n"})
        first.commit("chore(delegatron): iteration 1 — gen")
        _git(repo, "checkout", "-q", "main")

        second = Workspace(repo_path=repo, run_id="bench-default")
        second.initialize(fresh=True)
        third = Workspace(repo_path=repo, run_id="bench-default")
        _git(repo, "checkout", "-q", "main")
        third.initialize(fresh=True)

        assert second.branch == "delegatron/run-bench-default-2"
        assert third.branch == "delegatron/run-bench-default-3"
        assert second.resumed is third.resumed is False
        assert not (repo / "gen.py").exists()

    def test_resume_reuses_the_branch(self, repo: Path) -> None:
        Workspace(repo_path=repo, run_id="r1").initialize()
        _git(repo, "checkout", "-q", "main")

        ws = Workspace(repo_path=repo, run_id="r1")
        ws.initialize(fresh=False)

        assert ws.branch == "delegatron/run-r1"
        assert ws.resumed is True


class TestSeededFromAnotherRun:
    def test_clone_holding_only_run_branches_finds_the_original_fork(self, tmp_path: Path) -> None:
        """A server-resumed workspace: a clone of a run workspace, continued under a new id."""
        source = tmp_path / "source"
        source.mkdir()
        _git(source, "init", "-q", "-b", "main")
        (source / "card_deck.py").write_bytes(b"deck = []\n")
        _git(source, "add", "-A")
        _commit(source, "init: workspace for run old")
        init_sha = _git(source, "rev-parse", "HEAD")
        _git(source, "checkout", "-q", "-b", "delegatron/run-old")
        for i in (1, 2):
            (source / f"step{i}.py").write_bytes(b"s = 1\n")
            _git(source, "add", "-A")
            _commit(source, f"chore(delegatron): iteration {i} — step {i}")
        clone = tmp_path / "clone"
        _git(tmp_path, "clone", "-q", str(source), str(clone))
        _git(clone, "remote", "remove", "origin")
        _git(clone, "checkout", "-q", "-b", "delegatron/run-new")

        ws = Workspace(repo_path=clone, run_id="new")
        ws.initialize()

        assert ws.resumed is True
        assert ws.initial_sha == init_sha
        assert sorted(ws.list_changed_files()) == ["step1.py", "step2.py"]
        assert "step1.py" in ws.get_diff_from_base()

    def test_missing_base_ref_fails_closed(self, repo: Path) -> None:
        ws = Workspace(repo_path=repo, run_id="abc123", base_branch="does-not-exist")

        with pytest.raises(WorkspaceInitError, match="does-not-exist"):
            ws.initialize()

        assert ws.branch == ""
        assert _branch(repo) == "main"
        with pytest.raises(RuntimeError):
            ws.commit("must not land on main")


class TestDirtyWorkspace:
    def test_dirty_tree_is_refused_and_left_intact(self, repo: Path) -> None:
        (repo / "app.py").write_bytes(b"x = 2  # WIP\n")
        (repo / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
        for internal in ("runs/r1/events.jsonl", "_sandbox/x.py", ".delegatron-venv/bin/py"):
            (repo / internal).parent.mkdir(parents=True, exist_ok=True)
            (repo / internal).write_text("{}", encoding="utf-8")
        ws = Workspace(repo_path=repo, run_id="abc123")

        with pytest.raises(DirtyWorkspaceError) as excinfo:
            ws.initialize()

        assert sorted(excinfo.value.paths) == [".env", "app.py"]
        assert ws.branch == ""
        assert _branch(repo) == "main"
        assert (repo / "app.py").read_bytes() == b"x = 2  # WIP\n"
        assert RUN_BRANCH not in _git(repo, "branch", "--list")

    def test_internal_dirs_alone_do_not_count_as_dirty(self, repo: Path) -> None:
        (repo / "runs" / "r1").mkdir(parents=True)
        (repo / "runs" / "r1" / "events.jsonl").write_text("{}\n", encoding="utf-8")

        Workspace(repo_path=repo, run_id="abc123").initialize()

        assert _branch(repo) == RUN_BRANCH

    def test_allow_dirty_starts_anyway(self, repo: Path) -> None:
        (repo / "notes.txt").write_text("mine\n", encoding="utf-8")

        Workspace(repo_path=repo, run_id="abc123").initialize(allow_dirty=True)

        assert _branch(repo) == RUN_BRANCH
        assert (repo / "notes.txt").exists()


class TestFindRepoRoot:
    def test_subdirectory_resolves_to_top_level(self, repo: Path) -> None:
        sub = repo / "services" / "billing"
        sub.mkdir(parents=True)

        root = Workspace.find_repo_root(sub)

        assert root is not None
        assert root.resolve() == repo.resolve()

    def test_plain_directory_is_not_a_repo(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()

        assert Workspace.find_repo_root(plain) is None
