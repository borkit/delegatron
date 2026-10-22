"""Git PR creation — push branch and create pull requests.

Supports pluggable backends:
- GitHubPRCreator: uses `gh` CLI or GitHub API
- MockPRCreator: for testing without a remote

The PR includes an audit summary with:
- Task plan, per-task outcomes and acceptance criteria
- Iteration count and cost breakdown
- EMCS score and review summary
- Full event timeline link
"""

from __future__ import annotations

import contextlib
import logging
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# scp-like remote ``[user@]host:path``, and a Windows drive path it must not be confused with
_SCP_LIKE_REMOTE = re.compile(r"^(?:[^@/:]+@)?([^@/:]+):")
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")


# Seconds a push or ``gh`` call may take before the PR attempt is given up.
_PUSH_TIMEOUT_SECONDS = 300
_GH_TIMEOUT_SECONDS = 120


def check_gh_available() -> tuple[bool, str]:
    """Return (ok, reason). ok=True means `gh` is installed AND authenticated.

    Never raises: a ``gh`` that hangs or cannot be started counts as unavailable.
    """
    if shutil.which("gh") is None:
        return False, "`gh` CLI not installed"
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return False, "`gh auth status` timed out"
    except OSError as e:
        return False, f"`gh` could not be run: {e}"
    if result.returncode != 0:
        return False, f"`gh` not authenticated: {result.stderr.strip() or 'run `gh auth login`'}"
    return True, ""


def git_remote_url(workspace: Path, remote: str = "origin") -> str:
    """URL of *remote* in *workspace*, or "" when it is not configured (or git is missing)."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", remote],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def check_git_remote(workspace: Path, remote: str = "origin") -> tuple[bool, str]:
    """Return (ok, reason). ok=True means `origin` remote exists."""
    if not git_remote_url(workspace, remote):
        return False, f"no git remote `{remote}` configured"
    return True, ""


def remote_host(url: str) -> str:
    """Host a git remote URL points at: ``https://h/o/r.git`` and ``git@h:o/r.git`` give ``h``.

    Local remotes (``file://`` URLs and plain paths) give ``localhost``; "" for an empty
    or malformed URL (e.g. an unclosed IPv6 bracket).  Never raises.
    """
    url = url.strip()
    if "://" in url:
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
        except ValueError:
            return ""
        return "localhost" if parsed.scheme == "file" else hostname or ""
    scp_like = _SCP_LIKE_REMOTE.match(url)
    if scp_like and not _WINDOWS_PATH.match(url):
        return scp_like.group(1)
    return "localhost" if url else ""


@dataclass
class PRResult:
    """Result of a PR creation attempt."""

    success: bool
    url: str = ""
    number: int = 0
    error: str = ""


class BasePRCreator(ABC):
    """Abstract interface for PR creation."""

    @abstractmethod  # pragma: no cover
    def create_pr(
        self,
        workspace: Path,
        branch: str,
        title: str,
        body: str,
        base: str = "main",
        draft: bool = False,
    ) -> PRResult: ...


class GitHubPRCreator(BasePRCreator):
    """Create PRs using the `gh` CLI."""

    def create_pr(
        self,
        workspace: Path,
        branch: str,
        title: str,
        body: str,
        base: str = "main",
        draft: bool = False,
    ) -> PRResult:
        # Push the branch first
        try:
            push_result = subprocess.run(
                ["git", "push", "-u", "origin", branch],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=False,
                timeout=_PUSH_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return PRResult(success=False, error=f"Git push failed: {e}")
        if push_result.returncode != 0:
            return PRResult(
                success=False,
                error=f"Git push failed: {push_result.stderr.strip()}",
            )

        # Create PR via gh CLI
        cmd = [
            "gh",
            "pr",
            "create",
            "--title",
            title,
            "--body",
            body,
            "--base",
            base,
            "--head",
            branch,
        ]
        if draft:
            cmd.append("--draft")

        try:
            result = subprocess.run(
                cmd,
                cwd=workspace,
                capture_output=True,
                text=True,
                check=False,
                timeout=_GH_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return PRResult(success=False, error=f"gh pr create failed: {e}")

        if result.returncode == 0:
            url = result.stdout.strip()
            # Extract PR number from URL
            number = 0
            if "/pull/" in url:
                with contextlib.suppress(ValueError):
                    number = int(url.rstrip("/").split("/")[-1])
            return PRResult(success=True, url=url, number=number)

        return PRResult(
            success=False,
            error=f"gh pr create failed: {result.stderr.strip()}",
        )


class MockPRCreator(BasePRCreator):
    """Mock PR creator for testing."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def create_pr(
        self,
        workspace: Path,
        branch: str,
        title: str,
        body: str,
        base: str = "main",
        draft: bool = False,
    ) -> PRResult:
        self.calls.append(
            {
                "branch": branch,
                "title": title,
                "body": body,
                "base": base,
                "draft": str(draft),
            }
        )
        return PRResult(
            success=True,
            url=f"https://github.com/mock/repo/pull/{len(self.calls)}",
            number=len(self.calls),
        )


def _number(value: Any) -> float:
    """*value* as a float; 0.0 for a missing or non-numeric value."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# Summary test lists shown in a PR body: ``{node id: origin}`` per section.
_PR_TEST_SECTIONS = (
    ("Regressed Tests", "regressed_tests"),  # an approved task's test fails again
    ("Pre-existing Failures", "baseline_failures"),  # failed before the run started
    ("Pre-existing Failures Fixed", "fixed_baseline_failures"),
)


def build_pr_body(state: dict[str, Any], summary: dict[str, Any]) -> str:
    """Build a PR description from pipeline state and summary."""
    parts: list[str] = []

    # Header
    parts.append("## Delegatron Automated PR\n")
    parts.append(f"**Request:** {state.get('request', 'N/A')}\n")

    # Plan
    plan = state.get("plan") or {}
    if plan.get("summary"):
        parts.append(f"**Plan:** {plan['summary']}\n")

    # Results
    parts.append("### Results\n")
    parts.append(f"- **Status:** {summary.get('status', 'unknown')}")
    parts.append(f"- **Iterations:** {summary.get('iterations', 0)}")
    parts.append(f"- **Cost:** ${_number(summary.get('total_cost_usd')):.4f}")
    parts.append(f"- **Duration:** {_number(summary.get('elapsed_seconds')):.1f}s")

    # Cost breakdown
    breakdown = summary.get("cost_breakdown") or {}
    if breakdown.get("by_role"):
        parts.append("\n### Cost Breakdown\n")
        parts.append("| Role | Calls | Cost |")
        parts.append("|------|-------|------|")
        for role, data in breakdown["by_role"].items():
            calls = int(_number(data.get("calls")))
            parts.append(f"| {role} | {calls} | ${_number(data.get('cost_usd')):.4f} |")

    # How each planned task ended
    outcomes = summary.get("task_outcomes") or []
    if outcomes:
        parts.append("\n### Tasks\n")
        parts.append("| Task | Title | Outcome | Iterations |")
        parts.append("|------|-------|---------|------------|")
        for row in outcomes:
            cells = (row.get(key, "") for key in ("task_id", "title", "status", "iterations"))
            parts.append("| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |")

    # Tests the reviewer of this PR should know about
    for title, key in _PR_TEST_SECTIONS:
        tests = summary.get(key) or {}
        if not isinstance(tests, dict):
            tests = {str(test): "" for test in tests}
        if tests:
            parts.append(f"\n### {title}\n")
            for test, origin in sorted(tests.items()):
                note = f" ({origin})" if origin else ""
                parts.append(f"- `{test}`{note}")

    # Review summary
    review = state.get("review_report") or {}
    if review.get("summary"):
        parts.append(f"\n### Review\n\n{review['summary']}")

    # Acceptance criteria: ticked only for a run approved as a whole
    acceptance = plan.get("acceptance", {})
    if acceptance.get("criteria"):
        mark = "x" if summary.get("status") == "approved" else " "
        parts.append("\n### Acceptance Criteria\n")
        for c in acceptance["criteria"]:
            parts.append(f"- [{mark}] {c}")

    # Footer
    parts.append(
        "\n---\n*Generated by [Delegatron](https://delegatron.com) "
        "— autonomous multi-agent coding pipeline*"
    )

    return "\n".join(parts)
