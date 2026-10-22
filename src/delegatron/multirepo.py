"""Multi-repo support for Delegatron.

Manages cloning, syncing, and tracking multiple git repositories so that
Delegatron can operate across several codebases.

Usage::

    manager = RepoManager(base_dir=Path("/tmp/delegatron-repos"))
    local_path = manager.add_repo(
        RepoConfig(repo_url="https://github.com/org/repo.git")
    )
    manager.sync("https://github.com/org/repo.git")
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Upper bound for one git command; a clone or pull that hangs (e.g. waiting on
# a credential prompt) must not block the caller forever.
GIT_TIMEOUT_SECONDS = 600

# Branch names are passed to git as separate arguments; this also rejects
# option-like values such as "-x".  The server validates requests with it too.
BRANCH_PATTERN = r"^[A-Za-z0-9._/][A-Za-z0-9._/-]*$"
_BRANCH_RE = re.compile(BRANCH_PATTERN)


class RepoSyncError(RuntimeError):
    """A cached clone could not be switched to the requested branch."""


def validate_branch(branch: str) -> str:
    """Return *branch* if it is a safe branch name, else raise ``ValueError``."""
    if not _BRANCH_RE.match(branch):
        raise ValueError(f"Invalid branch name: {branch!r}")
    return branch


def _url_to_slug(url: str) -> str:
    """Convert a repository URL to a filesystem-safe slug.

    Examples:
        https://github.com/org/repo.git -> github-com-org-repo
        git@github.com:org/repo.git     -> github-com-org-repo
    """
    # Strip protocol and .git suffix
    cleaned = re.sub(r"^(https?://|git@|ssh://)", "", url)
    cleaned = re.sub(r"\.git$", "", cleaned)
    # Replace non-alphanumeric chars with hyphens
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", cleaned)
    # Clean up leading/trailing hyphens
    slug = slug.strip("-").lower()
    return slug


@dataclass
class RepoConfig:
    """Configuration for a single repository.

    Attributes:
        repo_url: The git remote URL (HTTPS or SSH) or a local repository path.
        branch: The branch to clone/checkout; ``None`` uses the remote's
            default branch.
        clone_dir: Override for the local clone directory.
            If not set, derived from repo_url.
        auth_token: Optional token for HTTPS authentication.
        auto_clone: Whether to automatically clone on add_repo.
    """

    repo_url: str
    branch: str | None = None
    clone_dir: Path = field(default_factory=lambda: Path())
    auth_token: str = ""
    auto_clone: bool = True

    def __post_init__(self) -> None:
        if self.branch is not None:
            validate_branch(self.branch)


class RepoManager:
    """Manages multiple local git repository clones.

    Repos are stored at ``{base_dir}/{url_slug}/`` where *url_slug* is
    a filesystem-safe version of the remote URL.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._repos: dict[str, RepoConfig] = {}
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_dir(self, config: RepoConfig) -> Path:
        """Determine the local directory for a repo."""
        if config.clone_dir and config.clone_dir != Path():
            return config.clone_dir
        return self._base_dir / _url_to_slug(config.repo_url)

    def _build_clone_url(self, config: RepoConfig) -> str:
        """Build the clone URL without embedding auth tokens.

        Returns the plain URL. Authentication is handled separately
        via ``_auth_env`` to avoid leaking tokens in process args,
        logs, or ``.git/config``.
        """
        return config.repo_url

    @staticmethod
    def _auth_env(config: RepoConfig) -> dict[str, str] | None:
        """Return extra environment variables that authenticate git over HTTPS.

        Uses a temporary ``GIT_ASKPASS`` script so the token never appears in
        the clone URL or command-line arguments.  Returns ``None`` when no
        authentication is needed.
        """
        if not config.auth_token or not config.repo_url.startswith("https://"):
            return None

        # Create a tiny helper script that prints the token on stdout.
        # git invokes GIT_ASKPASS with a single prompt argument; we ignore it.
        import sys

        if sys.platform == "win32":
            fd, script_path = tempfile.mkstemp(
                prefix="delegatron-askpass-",
                suffix=".bat",
            )
            # %~1 is the prompt from git; we ignore it and just echo the token.
            # Use delayed expansion to avoid issues with special characters.
            escaped_token = config.auth_token.replace("%", "%%")
            with os.fdopen(fd, "w") as fh:
                fh.write(f"@echo off\necho {escaped_token}\n")
        else:
            fd, script_path = tempfile.mkstemp(
                prefix="delegatron-askpass-",
                suffix=".sh",
            )
            escaped_token = config.auth_token.replace("'", "'\\''")
            with os.fdopen(fd, "w") as fh:
                fh.write(f"#!/bin/sh\necho '{escaped_token}'\n")
            os.chmod(script_path, 0o700)

        return {"GIT_ASKPASS": script_path}

    @staticmethod
    def _cleanup_auth_env(auth_env: dict[str, str] | None) -> None:
        """Remove temporary GIT_ASKPASS script if it exists."""
        if auth_env and "GIT_ASKPASS" in auth_env:
            with contextlib.suppress(OSError):
                os.unlink(auth_env["GIT_ASKPASS"])

    @staticmethod
    def _transport_env(config: RepoConfig, auth_env: dict[str, str] | None) -> dict[str, str]:
        """Environment for git commands that contact the remote.

        Only the https and ssh transports are allowed (``file`` for a local
        source repository), so ``ext::`` and other helpers never run.
        """
        protocols = "file" if Path(config.repo_url).is_dir() else "https:ssh"
        return {**(auth_env or {}), "GIT_ALLOW_PROTOCOL": protocols}

    @staticmethod
    def _git(
        *args: str,
        cwd: Path | None = None,
        check: bool = True,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a git command (times out after ``GIT_TIMEOUT_SECONDS``)."""
        env: dict[str, str] | None = None
        if extra_env:
            env = {**os.environ, **extra_env}
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=check,
            env=env,
            timeout=GIT_TIMEOUT_SECONDS,
        )

    def _default_branch(self, local_dir: Path) -> str:
        """The remote's default branch as recorded in a clone (``origin/HEAD``)."""
        head = self._git(
            "symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=local_dir, check=False
        )
        if head.returncode != 0 or not head.stdout.strip():
            msg = (
                f"Cannot determine the default branch of the clone at {local_dir}; "
                "pass an explicit branch"
            )
            raise RepoSyncError(msg)
        return head.stdout.strip().removeprefix("origin/")

    def _refresh(self, config: RepoConfig, local_dir: Path) -> None:
        """Check out the configured branch of a cached clone and fast-forward it.

        A failed checkout raises :class:`RepoSyncError` (never run on the wrong
        branch); a failed pull only logs a warning, so a cached clone stays
        usable offline.
        """
        branch = config.branch or self._default_branch(local_dir)
        # The "--" makes git read *branch* only as a ref: a name that matches
        # a tracked file must fail, not restore that file and stay put.
        checkout = self._git("checkout", branch, "--", cwd=local_dir, check=False)
        if checkout.returncode != 0:
            msg = f"git checkout {branch} failed in {local_dir}: {checkout.stderr.strip()}"
            raise RepoSyncError(msg)

        auth_env = self._auth_env(config)
        try:
            pull = self._git(
                "pull",
                "--ff-only",
                cwd=local_dir,
                check=False,
                extra_env=self._transport_env(config, auth_env),
            )
            error = pull.stderr.strip() if pull.returncode != 0 else ""
        except subprocess.TimeoutExpired:
            error = f"timed out after {GIT_TIMEOUT_SECONDS}s"
        finally:
            self._cleanup_auth_env(auth_env)
        if error:
            head = self._git("rev-parse", "HEAD", cwd=local_dir, check=False).stdout.strip()
            logger.warning(
                "git pull --ff-only failed in %s; using cached %s at %s: %s",
                local_dir,
                branch,
                head or "unknown HEAD",
                error,
            )

    def add_repo(self, config: RepoConfig) -> Path:
        """Register and optionally clone a repository.

        If the repo already exists locally, pulls the latest changes
        instead of cloning.

        Args:
            config: Repository configuration.

        Returns:
            The local path to the repository.
        """
        local_dir = self._resolve_dir(config)
        self._repos[config.repo_url] = config

        if local_dir.exists() and (local_dir / ".git").exists():
            logger.info("Repo already exists at %s, pulling latest", local_dir)
            self._refresh(config, local_dir)
            return local_dir

        created = not local_dir.exists()
        local_dir.mkdir(parents=True, exist_ok=True)
        if config.auto_clone:
            branch_args = ["--branch", config.branch] if config.branch else []
            auth_env = self._auth_env(config)
            logger.info(
                "Cloning %s into %s (branch=%s)",
                config.repo_url,
                local_dir,
                config.branch or "remote default",
            )
            try:
                self._git(
                    "clone",
                    *branch_args,
                    "--",
                    self._build_clone_url(config),
                    str(local_dir),
                    extra_env=self._transport_env(config, auth_env),
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if created:
                    shutil.rmtree(local_dir, ignore_errors=True)
                raise
            finally:
                self._cleanup_auth_env(auth_env)

        return local_dir

    def get_repo(self, repo_url: str) -> Path | None:
        """Get the local path for a previously added repository.

        Returns None if the repo_url has not been added.
        """
        config = self._repos.get(repo_url)
        if config is None:
            return None
        local_dir = self._resolve_dir(config)
        if local_dir.exists():
            return local_dir
        return None

    def list_repos(self) -> list[RepoConfig]:
        """Return all registered repository configurations."""
        return list(self._repos.values())

    def remove_repo(self, repo_url: str) -> None:
        """Remove a repository from management and delete its local clone.

        No-op if the repo_url is not registered.
        """
        config = self._repos.pop(repo_url, None)
        if config is None:
            return
        local_dir = self._resolve_dir(config)
        if local_dir.exists():
            logger.info("Removing repo at %s", local_dir)
            shutil.rmtree(local_dir)

    def sync(self, repo_url: str) -> None:
        """Pull the latest changes for a specific repository.

        Raises:
            KeyError: If the repo_url is not registered.
            FileNotFoundError: If the clone directory is missing.
            RepoSyncError: If the configured branch cannot be checked out.
        """
        config = self._repos.get(repo_url)
        if config is None:
            msg = f"Repository not registered: {repo_url}"
            raise KeyError(msg)
        local_dir = self._resolve_dir(config)
        if not local_dir.exists():
            msg = f"Repository directory does not exist: {local_dir}"
            raise FileNotFoundError(msg)
        logger.info("Syncing %s", repo_url)
        self._refresh(config, local_dir)

    def sync_all(self) -> None:
        """Pull latest changes for all registered repositories."""
        for repo_url in list(self._repos.keys()):
            try:
                self.sync(repo_url)
            except (KeyError, FileNotFoundError, RepoSyncError) as exc:
                logger.warning("Could not sync %s: %s", repo_url, exc)
