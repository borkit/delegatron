"""Tests for repo_resolve.py — is_git_url, normalize_git_url and resolve_workspace."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from delegatron.repo_resolve import (
    InvalidWorkspaceError,
    git_url_host,
    is_git_url,
    normalize_git_url,
    resolve_workspace,
)

# ---------------------------------------------------------------------------
# is_git_url tests
# ---------------------------------------------------------------------------


class TestIsGitUrl:
    """Test URL detection patterns."""

    def test_https_github(self):
        assert is_git_url("https://github.com/org/repo.git") is True

    def test_https_gitlab(self):
        assert is_git_url("https://gitlab.com/org/repo.git") is True

    def test_plain_http_is_not_accepted(self):
        assert is_git_url("http://example.com/repo.git") is False

    def test_ssh_github(self):
        assert is_git_url("git@github.com:org/repo.git") is True

    def test_ssh_protocol(self):
        assert is_git_url("ssh://git@github.com/org/repo.git") is True

    def test_github_com_without_protocol(self):
        assert is_git_url("github.com/org/repo") is True

    def test_gitlab_com_without_protocol(self):
        assert is_git_url("gitlab.com/org/repo") is True

    def test_bitbucket_org(self):
        assert is_git_url("bitbucket.org/org/repo") is True

    def test_local_path(self):
        assert is_git_url("/home/user/project") is False

    def test_relative_path(self):
        assert is_git_url("./my-project") is False

    def test_dot_path(self):
        assert is_git_url(".") is False

    def test_empty_string(self):
        assert is_git_url("") is False

    def test_plain_word(self):
        assert is_git_url("myproject") is False

    def test_windows_path(self):
        assert is_git_url("C:\\Users\\project") is False

    def test_existing_path_containing_a_host_is_local(self, tmp_path: Path):
        gopath = tmp_path / "go" / "src" / "github.com" / "me" / "proj"
        gopath.mkdir(parents=True)
        assert is_git_url(gopath.as_posix()) is False

    @pytest.mark.parametrize(
        "value",
        [
            "--upload-pack=echo INJECTED > M.txt; exit 1; github.com/",
            "-c core.sshCommand=x github.com/",
            "ext::sh -c x github.com/",
        ],
    )
    def test_option_and_transport_strings_are_rejected(self, value: str):
        assert is_git_url(value) is False


# ---------------------------------------------------------------------------
# normalize_git_url / git_url_host tests
# ---------------------------------------------------------------------------


class TestNormalizeGitUrl:
    @pytest.mark.parametrize(
        "value",
        [
            "https://github.com/org/repo.git",
            "git@github.com:org/repo.git",
            "ssh://git@host/org/repo.git",
            "ssh://git@host:2222/org/repo.git",
            "https://[fd00::1]/org/repo.git",
        ],
    )
    def test_accepted_unchanged(self, value: str):
        assert normalize_git_url(value) == value

    @pytest.mark.parametrize("host", ["github.com", "gitlab.com", "bitbucket.org"])
    def test_bare_known_host_becomes_https(self, host: str):
        assert normalize_git_url(f"{host}/org/repo") == f"https://{host}/org/repo"

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "--upload-pack=x; github.com/",
            "-c core.sshCommand=x github.com/",
            "ext::sh -c x github.com/",
            "ext::sh",
            "file:///etc/repo",
            "http://example.com/repo.git",
            "https://github.com/org/repo.git\n",
            "https://github.com",
            "https://-oProxyCommand=x/repo",
            "https://host:notaport/repo",
            "git@-oProxyCommand=x:repo",
            "ssh://-oProxyCommand=x@github.com/org/repo",
            "ssh://-oProxyCommand=x:pw@github.com/org/repo",
            "ssh://%2DoProxyCommand=x@github.com/org/repo",
            "https://-u@github.com/org/repo",
            "myproject",
        ],
    )
    def test_rejected(self, value: str):
        with pytest.raises(InvalidWorkspaceError):
            normalize_git_url(value)

    @pytest.mark.parametrize(
        ("value", "host"),
        [
            ("https://GitHub.com/org/repo.git", "github.com"),
            ("git@gitlab.com:org/repo.git", "gitlab.com"),
            ("ssh://git@10.0.0.5:2222/org/repo.git", "10.0.0.5"),
            ("bitbucket.org/org/repo", "bitbucket.org"),
        ],
    )
    def test_git_url_host(self, value: str, host: str):
        assert git_url_host(value) == host


# ---------------------------------------------------------------------------
# resolve_workspace tests
# ---------------------------------------------------------------------------


class TestResolveWorkspace:
    """Test workspace resolution for local paths and git URLs."""

    def test_local_path_resolved(self, tmp_path: Path):
        """Local path should be resolved to absolute and cleanup is no-op."""
        local_dir = tmp_path / "myproject"
        local_dir.mkdir()

        path, cleanup = resolve_workspace(str(local_dir))
        assert path == local_dir.resolve()
        # cleanup should be callable and not raise
        cleanup()

    def test_local_path_cleanup_is_noop(self, tmp_path: Path):
        """Cleanup for local paths should do nothing."""
        path, cleanup = resolve_workspace(str(tmp_path))
        assert path.exists()
        cleanup()
        assert path.exists()  # still exists after cleanup

    def test_git_url_clones_repo(self, tmp_path: Path):
        """Git URL should trigger clone via RepoManager."""
        mock_manager = MagicMock()
        mock_manager.add_repo.return_value = tmp_path / "cloned-repo"

        with (
            patch("delegatron.repo_resolve.RepoManager", return_value=mock_manager),
            patch("delegatron.repo_resolve.RepoConfig") as mock_config_cls,
        ):
            path, cleanup = resolve_workspace(
                "https://github.com/org/repo.git",
                branch="develop",
                base_dir=tmp_path,
            )

        mock_config_cls.assert_called_once_with(
            repo_url="https://github.com/org/repo.git",
            branch="develop",
        )
        mock_manager.add_repo.assert_called_once()
        assert path == tmp_path / "cloned-repo"
        # cleanup should be callable
        cleanup()

    def test_git_url_default_base_dir(self):
        """When base_dir is None, should use ~/.delegatron/repos."""
        mock_manager = MagicMock()
        mock_manager.add_repo.return_value = Path("/tmp/fake-clone")

        with (
            patch("delegatron.repo_resolve.RepoManager", return_value=mock_manager) as mock_rm_cls,
            patch("delegatron.repo_resolve.RepoConfig"),
        ):
            _path, _cleanup = resolve_workspace("https://github.com/org/repo.git")

        # RepoManager should be created with default base_dir
        call_args = mock_rm_cls.call_args
        base_dir = call_args.kwargs.get("base_dir") or call_args.args[0]
        expected = Path.home() / ".delegatron" / "repos"
        assert base_dir == expected

    def test_local_relative_path_resolved_to_absolute(self):
        """Relative local path should be resolved to absolute."""
        path, cleanup = resolve_workspace(".")
        assert path.is_absolute()
        cleanup()

    def test_ssh_url_detected_as_git(self, tmp_path: Path):
        """SSH-style git URLs should be handled as git repos."""
        mock_manager = MagicMock()
        mock_manager.add_repo.return_value = tmp_path / "ssh-clone"

        with (
            patch("delegatron.repo_resolve.RepoManager", return_value=mock_manager),
            patch("delegatron.repo_resolve.RepoConfig") as mock_config_cls,
        ):
            path, _cleanup = resolve_workspace(
                "git@github.com:org/repo.git",
                base_dir=tmp_path,
            )

        mock_config_cls.assert_called_once_with(repo_url="git@github.com:org/repo.git", branch=None)
        mock_manager.add_repo.assert_called_once_with(mock_config_cls.return_value)
        assert path == tmp_path / "ssh-clone"

    def test_bare_host_is_cloned_over_https_with_remote_default_branch(self, tmp_path: Path):
        mock_manager = MagicMock()
        with (
            patch("delegatron.repo_resolve.RepoManager", return_value=mock_manager),
            patch("delegatron.repo_resolve.RepoConfig") as mock_config_cls,
        ):
            resolve_workspace("github.com/org/repo", base_dir=tmp_path)
        mock_config_cls.assert_called_once_with(repo_url="https://github.com/org/repo", branch=None)

    @pytest.mark.parametrize(
        "value",
        [
            "--upload-pack=echo INJECTED > M.txt; exit 1; github.com/",
            "http://10.0.0.5/internal.git",
            "ext::sh -c x",
            "file:///srv/repo.git",
        ],
    )
    def test_invalid_remote_raises_before_git(self, tmp_path: Path, value: str):
        with (
            patch("delegatron.repo_resolve.RepoManager") as mock_rm_cls,
            pytest.raises(InvalidWorkspaceError),
        ):
            resolve_workspace(value, base_dir=tmp_path)
        mock_rm_cls.assert_not_called()

    def test_option_like_branch_raises_before_clone(self, tmp_path: Path):
        mock_manager = MagicMock()
        with (
            patch("delegatron.repo_resolve.RepoManager", return_value=mock_manager),
            pytest.raises(ValueError, match="Invalid branch"),
        ):
            resolve_workspace("https://github.com/org/repo.git", branch="-x", base_dir=tmp_path)
        mock_manager.add_repo.assert_not_called()
