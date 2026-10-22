"""Tests for delegatron.multirepo — multi-repo management."""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from delegatron.multirepo import (
    GIT_TIMEOUT_SECONDS,
    RepoConfig,
    RepoManager,
    RepoSyncError,
    _url_to_slug,
)

_REMOTE_ENV = {"GIT_ALLOW_PROTOCOL": "https:ssh"}


def _result(stdout: str = "", returncode: int = 0, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(["git"], returncode, stdout=stdout, stderr=stderr)


def _fake_git(
    *, head: str = "origin/main", checkout_rc: int = 0, pull_rc: int = 0, pull_err: str = ""
) -> Callable[..., Any]:
    """A ``RepoManager._git`` stand-in for an existing clone."""

    def fake(*args: str, **_kwargs: Any) -> Any:
        if args[0] == "symbolic-ref":
            return _result(f"{head}\n", 0) if head else _result(returncode=128)
        if args[0] == "checkout":
            stderr = "error: local changes would be overwritten" if checkout_rc else ""
            return _result(returncode=checkout_rc, stderr=stderr)
        if args[0] == "pull":
            return _result(returncode=pull_rc, stderr=pull_err)
        if args[0] == "rev-parse":
            return _result("abc123\n")
        return _result()

    return fake


@pytest.fixture()
def multirepo_log(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """Capture multirepo warnings even if another test disabled log propagation."""
    logger = logging.getLogger("delegatron.multirepo")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger="delegatron.multirepo")
    yield caplog
    logger.removeHandler(caplog.handler)


class TestUrlToSlug:
    """Tests for the URL-to-slug helper."""

    def test_https_url(self) -> None:
        assert _url_to_slug("https://github.com/org/repo.git") == "github-com-org-repo"

    def test_ssh_url(self) -> None:
        assert _url_to_slug("git@github.com:org/repo.git") == "github-com-org-repo"

    def test_no_suffix(self) -> None:
        assert _url_to_slug("https://github.com/org/repo") == "github-com-org-repo"

    def test_nested_path(self) -> None:
        slug = _url_to_slug("https://gitlab.com/group/sub/repo.git")
        assert slug == "gitlab-com-group-sub-repo"


class TestRepoConfig:
    """Tests for RepoConfig dataclass."""

    def test_defaults(self) -> None:
        config = RepoConfig(repo_url="https://example.com/repo.git")
        assert config.branch is None  # remote default branch
        assert config.auto_clone is True
        assert config.auth_token == ""

    def test_custom_branch(self) -> None:
        config = RepoConfig(repo_url="https://example.com/repo.git", branch="develop")
        assert config.branch == "develop"

    @pytest.mark.parametrize("branch", ["-x", "--upload-pack=x", "a b", ""])
    def test_invalid_branch_is_rejected(self, branch: str) -> None:
        with pytest.raises(ValueError, match="Invalid branch"):
            RepoConfig(repo_url="https://example.com/repo.git", branch=branch)

    @pytest.mark.parametrize("branch", ["main", "release/1.2", "feature_x-y", "v1.0"])
    def test_valid_branch(self, branch: str) -> None:
        assert RepoConfig(repo_url="https://example.com/repo.git", branch=branch).branch == branch


class TestRepoManager:
    """Tests for RepoManager."""

    @pytest.fixture()
    def base_dir(self, tmp_path: Path) -> Path:
        return tmp_path / "repos"

    @pytest.fixture()
    def manager(self, base_dir: Path) -> RepoManager:
        return RepoManager(base_dir=base_dir)

    def test_init_creates_base_dir(self, base_dir: Path) -> None:
        RepoManager(base_dir=base_dir)
        assert base_dir.exists()

    @patch("delegatron.multirepo.RepoManager._git")
    def test_add_repo_clones(self, mock_git: MagicMock, manager: RepoManager) -> None:
        config = RepoConfig(repo_url="https://github.com/org/repo.git")
        result = manager.add_repo(config)

        expected_dir = manager._base_dir / "github-com-org-repo"
        assert result == expected_dir

        mock_git.assert_called_once_with(
            "clone",
            "--",
            "https://github.com/org/repo.git",
            str(expected_dir),
            extra_env=_REMOTE_ENV,
        )

    @patch("delegatron.multirepo.RepoManager._git")
    def test_add_repo_clones_requested_branch(
        self, mock_git: MagicMock, manager: RepoManager
    ) -> None:
        config = RepoConfig(repo_url="https://github.com/org/repo.git", branch="develop")
        result = manager.add_repo(config)

        mock_git.assert_called_once_with(
            "clone",
            "--branch",
            "develop",
            "--",
            "https://github.com/org/repo.git",
            str(result),
            extra_env=_REMOTE_ENV,
        )

    @pytest.mark.parametrize(
        "error",
        [
            subprocess.CalledProcessError(128, ["git", "clone"]),
            subprocess.TimeoutExpired(["git", "clone"], GIT_TIMEOUT_SECONDS),
        ],
    )
    def test_failed_clone_leaves_no_directory(self, manager: RepoManager, error: Exception) -> None:
        config = RepoConfig(repo_url="https://github.com/org/missing.git")
        with (
            patch("delegatron.multirepo.RepoManager._git", side_effect=error),
            pytest.raises(type(error)),
        ):
            manager.add_repo(config)
        assert not manager._resolve_dir(config).exists()

    def test_failed_clone_keeps_a_directory_it_did_not_create(
        self, manager: RepoManager, tmp_path: Path
    ) -> None:
        existing = tmp_path / "mine"
        existing.mkdir()
        config = RepoConfig(repo_url="https://github.com/org/x.git", clone_dir=existing)
        error = subprocess.CalledProcessError(128, ["git", "clone"])
        with (
            patch("delegatron.multirepo.RepoManager._git", side_effect=error),
            pytest.raises(subprocess.CalledProcessError),
        ):
            manager.add_repo(config)
        assert existing.is_dir()

    def test_git_commands_have_a_timeout(self) -> None:
        with patch("delegatron.multirepo.subprocess.run") as mock_run:
            RepoManager._git("status")
        assert mock_run.call_args.kwargs["timeout"] == GIT_TIMEOUT_SECONDS

    @patch("delegatron.multirepo.RepoManager._git")
    def test_add_repo_with_auth_token(self, mock_git: MagicMock, manager: RepoManager) -> None:
        config = RepoConfig(
            repo_url="https://github.com/org/private.git",
            auth_token="ghp_abc123",
        )
        manager.add_repo(config)

        clone_call = mock_git.call_args
        clone_url = clone_call[0][-2]  # the URL precedes the target directory
        # Token must NOT appear in the URL (security fix)
        assert "ghp_abc123" not in clone_url
        assert clone_url == "https://github.com/org/private.git"
        # Auth must be provided via extra_env (GIT_ASKPASS)
        extra_env = clone_call[1].get("extra_env")
        assert extra_env is not None
        assert "GIT_ASKPASS" in extra_env

    @staticmethod
    def _existing_clone(base_dir: Path) -> Path:
        repo_dir = base_dir / "github-com-org-repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()
        return repo_dir

    @patch("delegatron.multirepo.RepoManager._git", side_effect=_fake_git(head="origin/master"))
    def test_add_repo_existing_pulls(
        self, mock_git: MagicMock, manager: RepoManager, base_dir: Path
    ) -> None:
        repo_dir = self._existing_clone(base_dir)

        config = RepoConfig(repo_url="https://github.com/org/repo.git")
        result = manager.add_repo(config)

        assert result == repo_dir
        # Should checkout the remote default branch and pull, not clone
        mock_git.assert_any_call("checkout", "master", "--", cwd=repo_dir, check=False)
        mock_git.assert_any_call(
            "pull",
            "--ff-only",
            cwd=repo_dir,
            check=False,
            extra_env=_REMOTE_ENV,
        )
        assert all(call.args[0] != "clone" for call in mock_git.call_args_list)

    @patch("delegatron.multirepo.RepoManager._git", side_effect=_fake_git(checkout_rc=1))
    def test_existing_clone_checkout_failure_raises(
        self, mock_git: MagicMock, manager: RepoManager, base_dir: Path
    ) -> None:
        self._existing_clone(base_dir)
        config = RepoConfig(repo_url="https://github.com/org/repo.git", branch="develop")
        with pytest.raises(RepoSyncError, match="local changes"):
            manager.add_repo(config)
        assert all(call.args[0] != "pull" for call in mock_git.call_args_list)

    @patch("delegatron.multirepo.RepoManager._git", side_effect=_fake_git(head=""))
    def test_existing_clone_without_origin_head_raises(
        self, mock_git: MagicMock, manager: RepoManager, base_dir: Path
    ) -> None:
        self._existing_clone(base_dir)
        with pytest.raises(RepoSyncError, match="default branch"):
            manager.add_repo(RepoConfig(repo_url="https://github.com/org/repo.git"))

    @patch(
        "delegatron.multirepo.RepoManager._git",
        side_effect=_fake_git(pull_rc=1, pull_err="fatal: unable to access"),
    )
    def test_existing_clone_pull_failure_warns_and_returns_path(
        self,
        mock_git: MagicMock,
        manager: RepoManager,
        base_dir: Path,
        multirepo_log: pytest.LogCaptureFixture,
    ) -> None:
        repo_dir = self._existing_clone(base_dir)
        result = manager.add_repo(RepoConfig(repo_url="https://github.com/org/repo.git"))
        assert result == repo_dir
        messages = [r.getMessage() for r in multirepo_log.records]
        assert any("fatal: unable to access" in m and "abc123" in m for m in messages)

    @patch("delegatron.multirepo.RepoManager._git")
    def test_add_repo_no_auto_clone(self, mock_git: MagicMock, manager: RepoManager) -> None:
        config = RepoConfig(
            repo_url="https://github.com/org/manual.git",
            auto_clone=False,
        )
        result = manager.add_repo(config)

        assert result.exists()
        mock_git.assert_not_called()

    @patch("delegatron.multirepo.RepoManager._git")
    def test_get_repo_returns_path(self, mock_git: MagicMock, manager: RepoManager) -> None:
        config = RepoConfig(repo_url="https://github.com/org/repo.git")
        added_path = manager.add_repo(config)

        result = manager.get_repo("https://github.com/org/repo.git")
        assert result == added_path

    def test_get_repo_unknown_returns_none(self, manager: RepoManager) -> None:
        assert manager.get_repo("https://unknown.com/repo.git") is None

    @patch("delegatron.multirepo.RepoManager._git")
    def test_list_repos(self, mock_git: MagicMock, manager: RepoManager) -> None:
        manager.add_repo(RepoConfig(repo_url="https://github.com/org/a.git"))
        manager.add_repo(RepoConfig(repo_url="https://github.com/org/b.git"))
        repos = manager.list_repos()
        assert len(repos) == 2
        urls = {r.repo_url for r in repos}
        assert "https://github.com/org/a.git" in urls
        assert "https://github.com/org/b.git" in urls

    @patch("delegatron.multirepo.shutil.rmtree")
    @patch("delegatron.multirepo.RepoManager._git")
    def test_remove_repo(
        self,
        mock_git: MagicMock,
        mock_rmtree: MagicMock,
        manager: RepoManager,
    ) -> None:
        config = RepoConfig(repo_url="https://github.com/org/repo.git")
        path = manager.add_repo(config)

        manager.remove_repo("https://github.com/org/repo.git")

        mock_rmtree.assert_called_once_with(path)
        assert manager.get_repo("https://github.com/org/repo.git") is None

    def test_remove_repo_unknown_noop(self, manager: RepoManager) -> None:
        # Should not raise
        manager.remove_repo("https://unknown.com/repo.git")

    @patch("delegatron.multirepo.RepoManager._git", side_effect=_fake_git())
    def test_sync_repo(self, mock_git: MagicMock, manager: RepoManager) -> None:
        config = RepoConfig(repo_url="https://github.com/org/repo.git", branch="develop")
        path = manager.add_repo(config)
        mock_git.reset_mock()

        manager.sync("https://github.com/org/repo.git")

        mock_git.assert_any_call("checkout", "develop", "--", cwd=path, check=False)
        mock_git.assert_any_call("pull", "--ff-only", cwd=path, check=False, extra_env=_REMOTE_ENV)

    def test_sync_unregistered_raises(self, manager: RepoManager) -> None:
        with pytest.raises(KeyError, match="not registered"):
            manager.sync("https://unknown.com/repo.git")

    @patch("delegatron.multirepo.RepoManager._git")
    def test_sync_missing_dir_raises(self, mock_git: MagicMock, manager: RepoManager) -> None:
        config = RepoConfig(
            repo_url="https://github.com/org/repo.git",
            auto_clone=False,
        )
        manager.add_repo(config)
        # Remove the directory to simulate missing
        path = manager._resolve_dir(config)
        if path.exists():
            import shutil

            shutil.rmtree(path)

        with pytest.raises(FileNotFoundError, match="does not exist"):
            manager.sync("https://github.com/org/repo.git")

    @patch("delegatron.multirepo.RepoManager._git", side_effect=_fake_git())
    def test_sync_all(self, mock_git: MagicMock, manager: RepoManager) -> None:
        manager.add_repo(RepoConfig(repo_url="https://github.com/org/a.git"))
        manager.add_repo(RepoConfig(repo_url="https://github.com/org/b.git"))
        mock_git.reset_mock()

        manager.sync_all()

        # Default branch lookup, checkout and pull for both repos
        assert [c.args[0] for c in mock_git.call_args_list] == [
            "symbolic-ref",
            "checkout",
            "pull",
        ] * 2

    @patch("delegatron.multirepo.RepoManager._git", side_effect=_fake_git(checkout_rc=1))
    def test_sync_all_continues_after_a_sync_error(
        self, mock_git: MagicMock, manager: RepoManager, multirepo_log: pytest.LogCaptureFixture
    ) -> None:
        manager.add_repo(RepoConfig(repo_url="https://github.com/org/a.git"))
        manager.add_repo(RepoConfig(repo_url="https://github.com/org/b.git"))

        manager.sync_all()

        assert sum("Could not sync" in r.getMessage() for r in multirepo_log.records) >= 2

    @patch("delegatron.multirepo.RepoManager._git")
    def test_custom_clone_dir(
        self, mock_git: MagicMock, manager: RepoManager, tmp_path: Path
    ) -> None:
        custom_dir = tmp_path / "custom-location"
        config = RepoConfig(
            repo_url="https://github.com/org/repo.git",
            clone_dir=custom_dir,
        )
        result = manager.add_repo(config)
        assert result == custom_dir


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_clones_local_repo_on_its_default_branch(tmp_path: Path) -> None:
    """A source repo whose default branch is ``master`` clones and refreshes."""
    src = tmp_path / "src"
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "init", "-q", "-b", "master", str(src)], check=True)
    (src / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(src), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(src), *identity, "commit", "-q", "-m", "init"], check=True)

    manager = RepoManager(base_dir=tmp_path / "repos")
    config = RepoConfig(repo_url=str(src))
    clone = manager.add_repo(config)

    def current_branch() -> str:
        out = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()

    assert (clone / "app.py").is_file()
    assert current_branch() == "master"

    # Reusing the cached clone resolves origin/HEAD and fast-forwards.
    assert manager.add_repo(config) == clone
    assert current_branch() == "master"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_cached_clone_never_treats_a_branch_as_a_path(tmp_path: Path) -> None:
    """#86: a requested branch named like a tracked file fails instead of
    discarding that file's local edits and staying on the current branch."""
    src = tmp_path / "src"
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "init", "-q", "-b", "master", str(src)], check=True)
    (src / "a.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(src), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(src), *identity, "commit", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(src), "branch", "develop"], check=True)

    manager = RepoManager(base_dir=tmp_path / "repos")
    clone = manager.add_repo(RepoConfig(repo_url=str(src)))
    (clone / "a.txt").write_text("local edit\n", encoding="utf-8")

    with pytest.raises(RepoSyncError, match=r"a\.txt"):
        manager.add_repo(RepoConfig(repo_url=str(src), branch="a.txt"))
    assert (clone / "a.txt").read_text(encoding="utf-8") == "local edit\n"

    # A branch that exists only on the remote is still checked out (DWIM).
    (clone / "a.txt").write_text("1\n", encoding="utf-8")
    assert manager.add_repo(RepoConfig(repo_url=str(src), branch="develop")) == clone
    head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert head.stdout.strip() == "develop"
