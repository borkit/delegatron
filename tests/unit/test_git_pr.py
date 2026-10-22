"""Tests for PR creation."""

import subprocess
from pathlib import Path

import pytest

from delegatron.integrations.git_pr import (
    MockPRCreator,
    build_pr_body,
    check_git_remote,
    git_remote_url,
    remote_host,
)


def test_mock_pr_creator():
    creator = MockPRCreator()
    result = creator.create_pr(
        workspace="/tmp/test",
        branch="delegatron/run-001",
        title="feat: add health endpoint",
        body="## Summary\nAdded health endpoint",
    )
    assert result.success is True
    assert result.number == 1
    assert "pull/1" in result.url
    assert len(creator.calls) == 1
    assert creator.calls[0]["title"] == "feat: add health endpoint"


def test_mock_pr_creator_multiple():
    creator = MockPRCreator()
    creator.create_pr("/tmp", "b1", "PR 1", "body")
    result = creator.create_pr("/tmp", "b2", "PR 2", "body")
    assert result.number == 2
    assert len(creator.calls) == 2


def test_build_pr_body():
    state = {
        "request": "Add health endpoint",
        "plan": {
            "summary": "Create GET /health",
            "acceptance": {
                "criteria": ["Returns 200", "Has status field"],
            },
        },
        "review_report": {
            "summary": "Clean implementation",
        },
    }
    summary = {
        "status": "approved",
        "iterations": 2,
        "total_cost_usd": 0.035,
        "elapsed_seconds": 6.8,
        "cost_breakdown": {
            "by_role": {
                "architect": {"calls": 1, "cost_usd": 0.007},
                "implementer": {"calls": 2, "cost_usd": 0.014},
            },
        },
    }
    body = build_pr_body(state, summary)
    assert "Add health endpoint" in body
    assert "Create GET /health" in body
    assert "approved" in body
    assert "$0.0350" in body
    assert "architect" in body
    assert "Returns 200" in body
    assert "delegatron.com" in body


def test_build_pr_body_minimal():
    state = {"request": "Fix bug"}
    summary = {"status": "exhausted", "iterations": 5, "total_cost_usd": 1.0}
    body = build_pr_body(state, summary)
    assert "Fix bug" in body
    assert "exhausted" in body


OUTCOMES = [
    {"task_id": "t1", "title": "Card | Deck", "status": "exhausted", "iterations": 5},
    {"task_id": "t2", "title": "Deal", "status": "approved", "iterations": 1},
]


def test_build_pr_body_renders_task_outcomes():
    summary = {"status": "approved", "task_outcomes": OUTCOMES}

    body = build_pr_body({"request": "r"}, summary)

    assert r"| t1 | Card \| Deck | exhausted | 5 |" in body
    assert "| t2 | Deal | approved | 1 |" in body


@pytest.mark.parametrize(("status", "mark"), [("approved", "[x]"), ("partial", "[ ]")])
def test_criteria_ticked_only_for_an_approved_run(status: str, mark: str):
    state = {"request": "r", "plan": {"acceptance": {"criteria": ["Returns 200"]}}}

    body = build_pr_body(state, {"status": status})

    assert f"- {mark} Returns 200" in body


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://github.com/o/r.git", "github.com"),
        ("https://user:token@gitlab.example.com:8443/o/r.git", "gitlab.example.com"),
        ("ssh://git@github.com:22/o/r.git", "github.com"),
        ("git@github.com:o/r.git", "github.com"),
        ("gitea.lan:o/r.git", "gitea.lan"),
        ("file:///srv/repos/r.git", "localhost"),
        ("/srv/repos/r.git", "localhost"),
        (r"C:\repos\r.git", "localhost"),
        ("C:/repos/r.git", "localhost"),
        ("../r.git", "localhost"),
        ("", ""),
    ],
)
def test_remote_host(url: str, host: str):
    assert remote_host(url) == host


def test_git_remote_url(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert git_remote_url(tmp_path) == ""
    assert check_git_remote(tmp_path) == (False, "no git remote `origin` configured")

    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/o/r.git"], cwd=tmp_path, check=True
    )

    assert git_remote_url(tmp_path) == "https://github.com/o/r.git"
    assert check_git_remote(tmp_path) == (True, "")


def test_remote_host_of_a_malformed_url_is_empty():
    assert remote_host("https://[github.com/o/r.git") == ""
    assert remote_host("https://[::1/x") == ""


def test_check_gh_available_never_raises(monkeypatch: pytest.MonkeyPatch):
    from delegatron.integrations import git_pr

    monkeypatch.setattr(git_pr.shutil, "which", lambda _name: "/usr/bin/gh")

    def hang(cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd, 10)

    monkeypatch.setattr(git_pr.subprocess, "run", hang)
    ok, reason = git_pr.check_gh_available()
    assert ok is False
    assert "timed out" in reason

    def missing(cmd, **_kwargs):
        raise FileNotFoundError(2, "No such file", "gh")

    monkeypatch.setattr(git_pr.subprocess, "run", missing)
    assert git_pr.check_gh_available()[0] is False


def test_push_that_hangs_is_a_failed_pr_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from delegatron.integrations import git_pr

    def hang(cmd, **kwargs):
        assert kwargs.get("timeout")
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(git_pr.subprocess, "run", hang)

    result = git_pr.GitHubPRCreator().create_pr(tmp_path, "delegatron/run-x", "t", "b")

    assert result.success is False
    assert "Git push failed" in result.error


def test_build_pr_body_tolerates_missing_and_null_numbers():
    summary = {
        "status": "error",
        "total_cost_usd": None,
        "cost_breakdown": {"by_role": {"architect": {"calls": None}}},
    }

    body = build_pr_body({"request": "r"}, summary)

    assert "$0.0000" in body
    assert "| architect | 0 | $0.0000 |" in body


def test_build_pr_body_lists_regressed_and_pre_existing_failures():
    summary = {
        "status": "approved",
        "regressed_tests": {"tests/test_deck.py::test_size": "t1"},
        "baseline_failures": {"tests/test_old.py::test_flaky": "pre_existing"},
        "fixed_baseline_failures": {"tests/test_old.py::test_fixed": "pre_existing"},
    }

    body = build_pr_body({"request": "build"}, summary)

    assert "### Regressed Tests" in body
    assert "- `tests/test_deck.py::test_size` (t1)" in body
    assert "### Pre-existing Failures\n" in body
    assert "- `tests/test_old.py::test_flaky` (pre_existing)" in body
    assert "### Pre-existing Failures Fixed" in body
    assert "tests/test_old.py::test_fixed" in body
