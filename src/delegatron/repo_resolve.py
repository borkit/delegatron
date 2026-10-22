"""Resolve a workspace argument — local path or git URL.

An existing path is always a local workspace.  Otherwise the value must be
a git URL in one of the accepted forms (``https://host/path``,
``ssh://[user@]host/path``, ``user@host:path``, or a bare
``github.com/…``, ``gitlab.com/…`` or ``bitbucket.org/…`` that is cloned over
https); it is cloned and the clone is reused on later calls.  Anything that
looks like a remote but is not an accepted form — option strings such as
``--upload-pack=…``, ``ext::``/``file:`` transports, plain ``http://`` — is
rejected with :class:`InvalidWorkspaceError` before git runs.

Usage:
    path, cleanup = resolve_workspace("https://github.com/org/repo.git")
    # ... use path ...
    cleanup()  # no-op: clones are kept for reuse
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote, urlsplit

from delegatron.multirepo import RepoConfig, RepoManager

logger = logging.getLogger(__name__)

_BARE_HOSTS_RE = re.compile(r"^(?:github\.com|gitlab\.com|bitbucket\.org)/\S", re.IGNORECASE)
_SCP_RE = re.compile(r"^[A-Za-z0-9._-]+@(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*):[^\s]+$")
# urlsplit().hostname is lower-cased and unbracketed; git rejects hosts starting with "-".
_HOST_RE = re.compile(r"^(?:[a-z0-9][a-z0-9.-]*|[0-9a-f:.]+)$")
_UNSAFE_CHARS_RE = re.compile(r"[\s\x00-\x1f\x7f]")
# Values that name a remote (scheme or transport prefix) rather than a path.
_REMOTE_HINT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]+(?:://|::)")


class InvalidWorkspaceError(ValueError):
    """A workspace value is neither an existing path nor an accepted git URL."""


def _existing_path(value: str) -> bool:
    try:
        return Path(value).exists()
    except (OSError, ValueError):
        return False


def normalize_git_url(value: str) -> str:
    """Return the clone URL for *value*, or raise :class:`InvalidWorkspaceError`.

    Accepts ``https://host/path``, ``ssh://[user@]host[:port]/path`` and
    scp-style ``user@host:path`` unchanged, and rewrites a bare
    ``github.com/…``, ``gitlab.com/…`` or ``bitbucket.org/…`` to ``https://``.
    """
    if not value:
        raise InvalidWorkspaceError("Workspace is empty")
    if value.startswith("-"):
        raise InvalidWorkspaceError(f"Workspace must not start with '-': {value!r}")
    if _UNSAFE_CHARS_RE.search(value):
        raise InvalidWorkspaceError(f"Workspace contains whitespace or control chars: {value!r}")
    if _BARE_HOSTS_RE.match(value):
        return f"https://{value}"
    if _SCP_RE.match(value):
        return value
    unsupported = InvalidWorkspaceError(
        f"Unsupported git URL {value!r}: use https://host/path, "
        "ssh://[user@]host/path or user@host:path"
    )
    try:
        parts = urlsplit(value)
        _ = parts.port  # validates the port
    except ValueError:
        raise unsupported from None
    # A user name such as "-oProxyCommand=..." (also percent-encoded) would
    # reach ssh as an option.
    option_like_user = unquote(parts.username or "").startswith("-")
    if (
        parts.scheme.lower() in ("https", "ssh")
        and _HOST_RE.match(parts.hostname or "")
        and parts.path.strip("/")
        and not option_like_user
    ):
        return value
    raise unsupported


def git_url_host(url: str) -> str:
    """Lower-case host of a URL accepted by :func:`normalize_git_url`."""
    url = normalize_git_url(url)
    scp = _SCP_RE.match(url)
    if scp:
        return scp.group("host").lower()
    return (urlsplit(url).hostname or "").lower()


def is_git_url(value: str) -> bool:
    """True when *value* is an accepted git URL and not an existing local path."""
    if _existing_path(value):
        return False
    try:
        normalize_git_url(value)
    except InvalidWorkspaceError:
        return False
    return True


def _looks_remote(value: str) -> bool:
    return value.startswith("-") or bool(_REMOTE_HINT_RE.match(value))


def resolve_workspace(
    workspace: str,
    branch: str | None = None,
    base_dir: Path | None = None,
) -> tuple[Path, Callable[..., object]]:
    """Resolve a workspace argument to a local path.

    Args:
        workspace: Local path or git URL.
        branch: Branch to clone (git URLs only); ``None`` uses the remote's
            default branch.
        base_dir: Base directory for cloned repos.

    Returns:
        Tuple of (local_path, cleanup_function).
        Call cleanup_function when done to remove temp clones.

    Raises:
        InvalidWorkspaceError: *workspace* looks like a remote but is not an
            accepted git URL.
        ValueError: *branch* is not a valid branch name.
    """
    if not is_git_url(workspace):
        if not _existing_path(workspace) and _looks_remote(workspace):
            normalize_git_url(workspace)  # raises with the reason
        # Local path — no cleanup needed
        return Path(workspace).resolve(), lambda: None

    url = normalize_git_url(workspace)
    if base_dir is None:
        base_dir = Path.home() / ".delegatron" / "repos"

    manager = RepoManager(base_dir=base_dir)
    config = RepoConfig(repo_url=url, branch=branch)

    logger.info("Cloning %s (branch: %s)...", url, branch or "remote default")
    local_path = manager.add_repo(config)
    logger.info("Cloned to %s", local_path)

    # Return a cleanup function that does nothing — we keep clones for reuse
    # (RepoManager handles deduplication via URL slugs)
    return local_path, lambda: None
