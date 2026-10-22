"""Unit tests for GitHub PR creation wiring in Pipeline._maybe_create_pr.

Verifies:
- PR created only when status is approved AND create_pr is enabled
- Disabled by default (no PR when create_pr=False)
- Skipped when not approved (exhausted/stalled/error)
- Skipped when workspace isn't a git repo
- Skipped when `gh` CLI is unavailable (graceful, no raise)
- PR creator exceptions don't propagate; error recorded in summary
- Draft flag passes through
- pr_base config is respected
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from delegatron.config import Config, PipelineConfig, RoleConfig
from delegatron.integrations.git_pr import MockPRCreator, PRResult
from delegatron.orchestrator.graph import Pipeline


def _mock_config(**pipeline_kwargs) -> Config:
    defaults = {"max_iterations": 1, "max_cost_usd": 1.0, "max_wall_time_minutes": 1}
    defaults.update(pipeline_kwargs)
    return Config(
        architect=RoleConfig(provider="mock", model="mock-architect"),
        implementer=RoleConfig(provider="mock", model="mock-implementer"),
        reviewer=RoleConfig(provider="mock", model="mock-reviewer"),
        pipeline=PipelineConfig(**defaults),
    )


def _make_pipeline(tmp_path: Path, pr_creator=None, **pipeline_kwargs) -> Pipeline:
    return Pipeline(
        config=_mock_config(**pipeline_kwargs),
        workspace_root=tmp_path,
        run_id="test-pr",
        pr_creator=pr_creator,
    )


class _FakeWorkspace:
    """Minimal stand-in for Workspace (exposes repo_path and branch)."""

    def __init__(self, repo_path: Path, branch: str) -> None:
        self.repo_path = repo_path
        self._branch = branch

    @property
    def branch(self) -> str:
        return self._branch


def _fake_workspace(tmp_path: Path, branch: str = "delegatron/run-test") -> _FakeWorkspace:
    return _FakeWorkspace(tmp_path, branch)


def _approved_state(**extra) -> dict:
    return {
        "is_approved": True,
        "status": "approved",
        "plan": {"summary": "Add a /health endpoint"},
        "review_report": {"summary": "LGTM"},
        "messages": [],
        **extra,
    }


class TestPRGateDisabled:
    def test_skipped_when_create_pr_false(self, tmp_path: Path) -> None:
        mock = MockPRCreator()
        p = _make_pipeline(tmp_path, pr_creator=mock, create_pr=False)
        out = p._maybe_create_pr(_approved_state(), _fake_workspace(tmp_path), {}, True)
        assert out == {"created": False}
        assert mock.calls == []


class TestPRGateNotApproved:
    @pytest.mark.parametrize("status", ["exhausted", "stalled", "error", "running", "partial"])
    def test_skipped_when_not_approved(self, tmp_path: Path, status: str) -> None:
        mock = MockPRCreator()
        p = _make_pipeline(tmp_path, pr_creator=mock, create_pr=True)
        state = _approved_state(is_approved=False, status=status)
        out = p._maybe_create_pr(state, _fake_workspace(tmp_path), {}, True)
        assert out["created"] is False
        assert "not approved" in out["skipped_reason"]
        assert mock.calls == []


class TestPRGateSecurity:
    def test_skipped_while_blocking_security_findings_are_unresolved(self, tmp_path: Path) -> None:
        mock = MockPRCreator()
        p = _make_pipeline(tmp_path, pr_creator=mock, create_pr=True)
        state = _approved_state(
            _security_blockers=[{"severity": "blocker", "title": "Possible secret", "file": "a"}]
        )
        out = p._maybe_create_pr(state, _fake_workspace(tmp_path), {}, True)
        assert out["skipped_reason"] == "unresolved blocking security findings"
        assert mock.calls == []


class TestPRGateNonGit:
    def test_skipped_when_not_git_repo(self, tmp_path: Path) -> None:
        mock = MockPRCreator()
        p = _make_pipeline(tmp_path, pr_creator=mock, create_pr=True)
        out = p._maybe_create_pr(
            _approved_state(), _fake_workspace(tmp_path), {}, is_git_repo=False
        )
        assert out["created"] is False
        assert out["skipped_reason"] == "workspace is not a git repo"
        assert mock.calls == []

    def test_skipped_when_branch_empty(self, tmp_path: Path) -> None:
        mock = MockPRCreator()
        p = _make_pipeline(tmp_path, pr_creator=mock, create_pr=True)
        ws = _fake_workspace(tmp_path, branch="")
        out = p._maybe_create_pr(_approved_state(), ws, {}, is_git_repo=True)
        assert out["created"] is False
        assert "branch" in out["skipped_reason"].lower()


class TestPRGateHappyPath:
    def test_creates_pr_when_approved(self, tmp_path: Path) -> None:
        mock = MockPRCreator()
        p = _make_pipeline(tmp_path, pr_creator=mock, create_pr=True)

        # Skip the gh/remote checks — we're testing the creator invocation.
        with (
            patch("delegatron.orchestrator.graph.check_gh_available", return_value=(True, "")),
            patch("delegatron.orchestrator.graph.check_git_remote", return_value=(True, "")),
        ):
            out = p._maybe_create_pr(
                _approved_state(),
                _fake_workspace(tmp_path),
                {"status": "approved", "iterations": 2, "total_cost_usd": 0.01},
                is_git_repo=True,
            )

        assert out["created"] is True
        assert out["url"].startswith("https://github.com/mock/repo/pull/")
        assert out["number"] == 1
        assert len(mock.calls) == 1
        call = mock.calls[0]
        assert call["branch"] == "delegatron/run-test"
        assert call["base"] == "main"
        assert call["draft"] == "False"
        # Plan summary flows into the PR title (truncated to 72 chars).
        assert "Add a /health endpoint" in call["title"]

    def test_draft_flag_propagates(self, tmp_path: Path) -> None:
        mock = MockPRCreator()
        p = _make_pipeline(tmp_path, pr_creator=mock, create_pr=True, pr_draft=True)
        with (
            patch("delegatron.orchestrator.graph.check_gh_available", return_value=(True, "")),
            patch("delegatron.orchestrator.graph.check_git_remote", return_value=(True, "")),
        ):
            p._maybe_create_pr(
                _approved_state(),
                _fake_workspace(tmp_path),
                {},
                is_git_repo=True,
            )
        assert mock.calls[0]["draft"] == "True"

    def test_custom_pr_base(self, tmp_path: Path) -> None:
        mock = MockPRCreator()
        p = _make_pipeline(tmp_path, pr_creator=mock, create_pr=True, pr_base="develop")
        with (
            patch("delegatron.orchestrator.graph.check_gh_available", return_value=(True, "")),
            patch("delegatron.orchestrator.graph.check_git_remote", return_value=(True, "")),
        ):
            p._maybe_create_pr(
                _approved_state(),
                _fake_workspace(tmp_path),
                {},
                is_git_repo=True,
            )
        assert mock.calls[0]["base"] == "develop"

    def test_emits_pr_created_event(self, tmp_path: Path) -> None:
        mock = MockPRCreator()
        p = _make_pipeline(tmp_path, pr_creator=mock, create_pr=True)
        state = _approved_state()
        with (
            patch("delegatron.orchestrator.graph.check_gh_available", return_value=(True, "")),
            patch("delegatron.orchestrator.graph.check_git_remote", return_value=(True, "")),
        ):
            p._maybe_create_pr(state, _fake_workspace(tmp_path), {}, is_git_repo=True)
        events = [m for m in state["messages"] if m.get("event") == "pr_created"]
        assert len(events) == 1
        assert events[0]["url"].startswith("https://github.com/mock/repo/pull/")


class TestPREnvironmentFailures:
    def test_skipped_when_gh_unavailable(self, tmp_path: Path) -> None:
        mock = MockPRCreator()
        p = _make_pipeline(tmp_path, pr_creator=mock, create_pr=True)
        with patch(
            "delegatron.orchestrator.graph.check_gh_available",
            return_value=(False, "`gh` CLI not installed"),
        ):
            out = p._maybe_create_pr(
                _approved_state(), _fake_workspace(tmp_path), {}, is_git_repo=True
            )
        assert out["created"] is False
        assert "not installed" in out["skipped_reason"]
        assert mock.calls == []  # creator never called

    def test_skipped_when_no_remote(self, tmp_path: Path) -> None:
        mock = MockPRCreator()
        p = _make_pipeline(tmp_path, pr_creator=mock, create_pr=True)
        with (
            patch("delegatron.orchestrator.graph.check_gh_available", return_value=(True, "")),
            patch(
                "delegatron.orchestrator.graph.check_git_remote",
                return_value=(False, "no git remote `origin` configured"),
            ),
        ):
            out = p._maybe_create_pr(
                _approved_state(), _fake_workspace(tmp_path), {}, is_git_repo=True
            )
        assert out["created"] is False
        assert "remote" in out["skipped_reason"]
        assert mock.calls == []


class TestPRCreatorErrors:
    def test_creator_failure_does_not_raise(self, tmp_path: Path) -> None:
        class BrokenCreator:
            def create_pr(self, **_kw):
                return PRResult(success=False, error="gh: permission denied")

        p = _make_pipeline(tmp_path, pr_creator=BrokenCreator(), create_pr=True)
        with (
            patch("delegatron.orchestrator.graph.check_gh_available", return_value=(True, "")),
            patch("delegatron.orchestrator.graph.check_git_remote", return_value=(True, "")),
        ):
            out = p._maybe_create_pr(
                _approved_state(), _fake_workspace(tmp_path), {}, is_git_repo=True
            )
        assert out["created"] is False
        assert "permission denied" in out["error"]

    def test_creator_exception_does_not_raise(self, tmp_path: Path) -> None:
        class ExplosiveCreator:
            def create_pr(self, **_kw):
                raise RuntimeError("kaboom")

        p = _make_pipeline(tmp_path, pr_creator=ExplosiveCreator(), create_pr=True)
        with (
            patch("delegatron.orchestrator.graph.check_gh_available", return_value=(True, "")),
            patch("delegatron.orchestrator.graph.check_git_remote", return_value=(True, "")),
        ):
            out = p._maybe_create_pr(
                _approved_state(), _fake_workspace(tmp_path), {}, is_git_repo=True
            )
        assert out["created"] is False
        assert "RuntimeError" in out["error"]
        assert "kaboom" in out["error"]
