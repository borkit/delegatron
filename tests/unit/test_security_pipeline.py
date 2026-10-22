"""Tests for factories/security_pipeline.py — run_security_checks and should_block."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from delegatron.constraints import Constraints
from delegatron.factories.security_pipeline import (
    SecurityBaseline,
    SecurityResult,
    blocking_findings,
    run_security_checks,
    should_block,
)

_AWS_LINE = 'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"'

# ---------------------------------------------------------------------------
# SecurityResult tests
# ---------------------------------------------------------------------------


class TestSecurityResult:
    """Test SecurityResult properties."""

    def test_has_blockers_false_when_empty(self):
        result = SecurityResult()
        assert result.has_blockers is False

    def test_has_blockers_true_with_constraint_violations(self):
        result = SecurityResult(
            constraint_violations=[{"rule": "no_modify", "file": "x.py", "detail": "d"}]
        )
        assert result.has_blockers is True

    def test_has_blockers_true_with_secret_findings(self):
        result = SecurityResult(
            secret_findings=[{"pattern": "api_key", "severity": "high", "line": 1}]
        )
        assert result.has_blockers is True

    def test_has_blockers_true_with_dependency_findings(self):
        result = SecurityResult(
            dependency_findings=[
                {
                    "name": "requests",
                    "import_line": "import requests",
                    "severity": "warning",
                }
            ]
        )
        assert result.has_blockers is True

    def test_summary_events_empty(self):
        result = SecurityResult()
        assert result.summary_events == []

    def test_summary_events_with_all_findings(self):
        result = SecurityResult(
            constraint_violations=[{"rule": "r", "file": "f", "detail": "d"}],
            secret_findings=[{"pattern": "p", "severity": "high", "line": 1}],
            dependency_findings=[{"name": "n", "import_line": "l", "severity": "w"}],
        )
        events = result.summary_events
        assert len(events) == 3
        event_types = [e["event"] for e in events]
        assert "constraint_violation" in event_types
        assert "secrets_detected" in event_types
        assert "new_dependencies" in event_types

    def test_summary_events_constraint_only(self):
        result = SecurityResult(constraint_violations=[{"rule": "r", "file": "f", "detail": "d"}])
        events = result.summary_events
        assert len(events) == 1
        assert events[0]["event"] == "constraint_violation"
        assert events[0]["violations"] == result.constraint_violations

    def test_summary_events_secrets_only(self):
        result = SecurityResult(secret_findings=[{"pattern": "p", "severity": "high", "line": 1}])
        events = result.summary_events
        assert len(events) == 1
        assert events[0]["event"] == "secrets_detected"

    def test_summary_events_deps_only(self):
        result = SecurityResult(
            dependency_findings=[{"name": "n", "import_line": "l", "severity": "w"}]
        )
        events = result.summary_events
        assert len(events) == 1
        assert events[0]["event"] == "new_dependencies"

    def test_store_events_empty(self):
        result = SecurityResult()
        assert result.store_events == []

    def test_store_events_with_all_findings(self):
        result = SecurityResult(
            constraint_violations=[{"rule": "r", "file": "f", "detail": "d"}],
            secret_findings=[
                {"pattern": "p1", "severity": "high", "line": 1},
                {"pattern": "p2", "severity": "medium", "line": 2},
            ],
            dependency_findings=[{"name": "n", "import_line": "l", "severity": "w"}],
        )
        events = result.store_events
        assert len(events) == 3
        for ev in events:
            assert "count" in ev
        constraint_ev = next(e for e in events if e["event"] == "constraint_violation")
        assert constraint_ev["count"] == 1
        secret_ev = next(e for e in events if e["event"] == "secrets_detected")
        assert secret_ev["count"] == 2
        dep_ev = next(e for e in events if e["event"] == "new_dependencies")
        assert dep_ev["count"] == 1

    def test_store_events_constraint_only(self):
        result = SecurityResult(
            constraint_violations=[
                {"rule": "r1", "file": "f1", "detail": "d1"},
                {"rule": "r2", "file": "f2", "detail": "d2"},
            ]
        )
        events = result.store_events
        assert len(events) == 1
        assert events[0]["count"] == 2
        assert events[0]["files"] == ["f1", "f2"]

    def test_findings_shape_and_severity(self):
        result = SecurityResult(
            constraint_violations=[
                {"rule": "no_modify", "file": ".env", "line": None, "detail": "protected"}
            ],
            secret_findings=[
                {
                    "pattern": "aws_access_key",
                    "severity": "critical",
                    "file": "a.py",
                    "line": 3,
                    "line_content": "AKIA****REDACTED****",
                },
                {"pattern": "high_entropy_string", "severity": "medium", "file": "b.py", "line": 1},
            ],
            dependency_findings=[
                {
                    "name": "requests",
                    "import_line": "import requests",
                    "severity": "warning",
                    "file": "c.py",
                    "line": 2,
                }
            ],
        )
        findings = result.findings
        assert [(f["severity"], f["source"], f["file"], f["line"]) for f in findings] == [
            ("major", "constraints", ".env", None),
            ("blocker", "secrets", "a.py", 3),
            ("major", "secrets", "b.py", 1),
            ("minor", "dependencies", "c.py", 2),
        ]
        for finding in findings:
            assert set(finding) == {"severity", "title", "details", "file", "line", "source"}
        assert "AKIA****REDACTED****" in findings[1]["details"]


# ---------------------------------------------------------------------------
# run_security_checks tests
# ---------------------------------------------------------------------------


class TestRunSecurityChecks:
    """Test the run_security_checks function with mocked sub-checks."""

    def test_no_findings(self, tmp_path: Path):
        """Clean patch should produce no findings."""
        patch_data = {"files": {"main.py": "print('hello')\n"}, "diff_unified": ""}
        with (
            patch("delegatron.security.secrets.scan_patch", return_value=[]),
            patch("delegatron.security.deps.audit_imports", return_value=[]),
        ):
            result = run_security_checks(patch_data, None, tmp_path)
        assert not result.has_blockers
        assert result.constraint_violations == []
        assert result.secret_findings == []
        assert result.dependency_findings == []

    def test_with_constraints_violations(self, tmp_path: Path):
        """Constraint violations should be captured."""
        mock_violation = MagicMock()
        mock_violation.rule = "no_modify"
        mock_violation.file = "setup.py"
        mock_violation.detail = "Cannot modify setup.py"

        mock_constraints = MagicMock()
        patch_data = {"files": {"setup.py": "code"}, "diff_unified": ""}

        with (
            patch(
                "delegatron.constraints.validate_patch",
                return_value=[mock_violation],
            ),
            patch("delegatron.security.secrets.scan_patch", return_value=[]),
            patch("delegatron.security.deps.audit_imports", return_value=[]),
        ):
            result = run_security_checks(patch_data, mock_constraints, tmp_path)

        assert result.has_blockers is True
        assert len(result.constraint_violations) == 1
        assert result.constraint_violations[0]["rule"] == "no_modify"
        assert result.constraint_violations[0]["file"] == "setup.py"

    def test_with_secret_findings(self, tmp_path: Path):
        """Secret findings should be captured."""
        mock_finding = MagicMock()
        mock_finding.pattern_name = "aws_access_key"
        mock_finding.severity = "critical"
        mock_finding.line_number = 5

        patch_data = {"files": {}, "diff_unified": "+AKIA1234567890ABCDEF"}

        with (
            patch(
                "delegatron.security.secrets.scan_patch",
                return_value=[mock_finding],
            ),
            patch("delegatron.security.deps.audit_imports", return_value=[]),
        ):
            result = run_security_checks(patch_data, None, tmp_path)

        assert result.has_blockers is True
        assert len(result.secret_findings) == 1
        assert result.secret_findings[0]["pattern"] == "aws_access_key"
        assert result.secret_findings[0]["severity"] == "critical"

    def test_with_dependency_findings(self, tmp_path: Path):
        """Dependency findings should be captured."""
        mock_dep = MagicMock()
        mock_dep.package_name = "flask"
        mock_dep.import_line = "import flask"
        mock_dep.severity = "warning"

        patch_data = {"files": {}, "diff_unified": "+import flask"}

        with (
            patch("delegatron.security.secrets.scan_patch", return_value=[]),
            patch(
                "delegatron.security.deps.audit_imports",
                return_value=[mock_dep],
            ),
        ):
            result = run_security_checks(patch_data, None, tmp_path)

        assert result.has_blockers is True
        assert len(result.dependency_findings) == 1
        assert result.dependency_findings[0]["name"] == "flask"

    def test_no_constraints_skips_validation(self, tmp_path: Path):
        """When constraints is None, validate_patch should not be called."""
        patch_data = {"files": {}, "diff_unified": ""}

        with (
            patch("delegatron.constraints.validate_patch") as mock_validate,
            patch("delegatron.security.secrets.scan_patch", return_value=[]),
            patch("delegatron.security.deps.audit_imports", return_value=[]),
        ):
            run_security_checks(patch_data, None, tmp_path)

        mock_validate.assert_not_called()

    def test_all_findings_combined(self, tmp_path: Path):
        """All three types of findings together."""
        mock_violation = MagicMock()
        mock_violation.rule = "forbidden_pattern"
        mock_violation.file = "app.py"
        mock_violation.detail = "eval() found"

        mock_secret = MagicMock()
        mock_secret.pattern_name = "generic_api_key"
        mock_secret.severity = "high"
        mock_secret.line_number = 10

        mock_dep = MagicMock()
        mock_dep.package_name = "numpy"
        mock_dep.import_line = "import numpy"
        mock_dep.severity = "warning"

        mock_constraints = MagicMock()
        patch_data = {
            "files": {"app.py": "eval(input())"},
            "diff_unified": "+import numpy",
        }

        with (
            patch(
                "delegatron.constraints.validate_patch",
                return_value=[mock_violation],
            ),
            patch(
                "delegatron.security.secrets.scan_patch",
                return_value=[mock_secret],
            ),
            patch(
                "delegatron.security.deps.audit_imports",
                return_value=[mock_dep],
            ),
        ):
            result = run_security_checks(patch_data, mock_constraints, tmp_path)

        assert result.has_blockers is True
        assert len(result.constraint_violations) == 1
        assert len(result.secret_findings) == 1
        assert len(result.dependency_findings) == 1


# ---------------------------------------------------------------------------
# run_security_checks with the real scanners
# ---------------------------------------------------------------------------


class TestRunSecurityChecksEndToEnd:
    def test_findings_carry_file_and_line(self, tmp_path: Path):
        patch_data = {"files": {"a.py": "x = 1\n", "b.py": f"import requests\n{_AWS_LINE}\n"}}
        result = run_security_checks(patch_data, Constraints(no_modify=["b.py"]), tmp_path)
        assert [(v["rule"], v["file"], v["line"]) for v in result.constraint_violations] == [
            ("no_modify", "b.py", None)
        ]
        assert [(s["pattern"], s["file"], s["line"]) for s in result.secret_findings] == [
            ("aws_access_key", "b.py", 2)
        ]
        assert "line_content" in result.secret_findings[0]
        assert [(d["name"], d["file"], d["line"]) for d in result.dependency_findings] == [
            ("requests", "b.py", 1)
        ]

    def test_existing_lines_are_not_attributed_to_the_patch(self, tmp_path: Path):
        (tmp_path / "config.py").write_text(f"{_AWS_LINE}\n# TODO\n", encoding="utf-8")
        patch_data = {"files": {"config.py": f"{_AWS_LINE}\n# TODO\nTIMEOUT = 30\n"}}
        constraints = Constraints.from_dict({"forbidden_patterns": ["TODO"]})
        result = run_security_checks(patch_data, constraints, tmp_path)
        assert result.findings == []

    def test_unchanged_files_give_no_findings(self, tmp_path: Path):
        (tmp_path / ".env").write_text(f"{_AWS_LINE}\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("import requests\n", encoding="utf-8")
        patch_data = {"files": {".env": f"{_AWS_LINE}\n", "app.py": "import requests\n"}}
        constraints = Constraints(no_modify=[".env"], forbidden_patterns=["AKIA"])
        assert run_security_checks(patch_data, constraints, tmp_path).findings == []

    def test_secrets_allowlist_suppresses_matching_lines(self, tmp_path: Path):
        patch_data = {"files": {"fixtures.py": f"{_AWS_LINE}\n"}}
        assert run_security_checks(patch_data, None, tmp_path).secret_findings
        result = run_security_checks(
            patch_data, None, tmp_path, secrets_allowlist=["AKIAIOSFODNN7EXAMPLE"]
        )
        assert result.secret_findings == []


# ---------------------------------------------------------------------------
# blocking_findings / should_block tests
# ---------------------------------------------------------------------------


def _secret(severity: str, pattern: str, file: str = "s.py") -> dict:
    return {"pattern": pattern, "severity": severity, "file": file, "line": 1}


class TestBlockingFindings:
    def test_block_mode_returns_blocker_and_major(self):
        result = SecurityResult(
            constraint_violations=[{"rule": "no_modify", "file": ".env", "detail": "d"}],
            secret_findings=[
                _secret("critical", "aws_access_key"),
                _secret("high", "connection_string"),
                _secret("medium", "high_entropy_string"),
            ],
            dependency_findings=[{"name": "n", "import_line": "import n", "severity": "warning"}],
        )
        blocking = blocking_findings(result, "block")
        assert [(f["source"], f["severity"]) for f in blocking] == [
            ("constraints", "major"),
            ("secrets", "blocker"),
            ("secrets", "major"),
            ("secrets", "major"),
        ]

    def test_dependencies_never_block(self):
        result = SecurityResult(
            dependency_findings=[{"name": "n", "import_line": "import n", "severity": "warning"}]
        )
        assert blocking_findings(result, "block") == []

    def test_mode_is_case_insensitive(self):
        result = SecurityResult(secret_findings=[_secret("critical", "private_key")])
        assert blocking_findings(result, " Block ")

    def test_warn_and_allow_return_nothing(self):
        result = SecurityResult(secret_findings=[_secret("critical", "private_key")])
        assert blocking_findings(result, "warn") == []
        assert blocking_findings(result, "allow") == []


class TestShouldBlock:
    """Test the should_block function."""

    def test_warn_mode_never_blocks(self):
        """security_mode='warn' should never return a block message."""
        result = SecurityResult(
            constraint_violations=[{"rule": "r", "file": "f", "detail": "d"}],
            secret_findings=[{"pattern": "p", "severity": "high", "line": 1}],
        )
        assert should_block(result, "warn") is None

    def test_allow_mode_never_blocks(self):
        """security_mode='allow' should never return a block message."""
        result = SecurityResult(
            secret_findings=[{"pattern": "p", "severity": "critical", "line": 1}],
        )
        assert should_block(result, "allow") is None

    def test_block_mode_no_findings(self):
        """security_mode='block' with no findings should not block."""
        result = SecurityResult()
        assert should_block(result, "block") is None

    def test_block_mode_constraint_violations(self):
        """security_mode='block' with constraint violations should block."""
        result = SecurityResult(constraint_violations=[{"rule": "r", "file": "f", "detail": "d"}])
        msg = should_block(result, "block")
        assert msg is not None
        assert "Constraint violations (1)" in msg

    def test_block_mode_secret_findings_name_the_files(self):
        """security_mode='block' with secrets should block and name the files."""
        result = SecurityResult(
            secret_findings=[
                _secret("high", "p1", file="a.py"),
                _secret("critical", "p2", file="b.py"),
            ]
        )
        msg = should_block(result, "block")
        assert msg is not None
        assert "Secrets detected (2)" in msg
        assert "a.py, b.py" in msg

    def test_block_mode_dependency_findings_do_not_block(self):
        """New dependencies are minor findings: advisory even in block mode."""
        result = SecurityResult(
            dependency_findings=[{"name": "n", "import_line": "l", "severity": "w"}]
        )
        assert should_block(result, "block") is None

    def test_block_mode_lists_every_blocking_kind(self):
        result = SecurityResult(
            constraint_violations=[{"rule": "r", "file": "f", "detail": "d"}],
            secret_findings=[{"pattern": "p", "severity": "high", "line": 1}],
            dependency_findings=[{"name": "n", "import_line": "l", "severity": "w"}],
        )
        msg = should_block(result, "block")
        assert msg is not None
        assert msg.startswith("Constraint violations (1), Secrets detected (1)")


# ---------------------------------------------------------------------------
# Findings persist across iterations with a SecurityBaseline (#115 + #113)
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="")


def _apply(root: Path, patch_data: dict) -> None:
    """What the orchestrator does after the checks: write files, delete paths."""
    for rel, text in (patch_data.get("files") or {}).items():
        _write(root, rel, text)
    for rel in patch_data.get("delete_files") or []:
        (root / rel).unlink(missing_ok=True)


def _blocking(result: SecurityResult) -> list[tuple[str, str]]:
    return [(f["source"], f["file"]) for f in blocking_findings(result, "block")]


class TestSecurityBaselineAcrossIterations:
    """A finding from an applied patch stays reported until it is removed.

    Without a baseline each pass only diffs against the workspace, which by
    then holds the previous (applied) iteration, so a re-sent or untouched
    secret would no longer be reported and block mode could approve it.
    """

    def test_secret_is_reported_until_removed(self, tmp_path: Path):
        _write(tmp_path, "app.py", "import os\n")
        baseline = SecurityBaseline(tmp_path)

        first = {"files": {"app.py": f"import os\n{_AWS_LINE}\n"}}
        result = run_security_checks(first, None, tmp_path, baseline=baseline)
        assert _blocking(result) == [("secrets", "app.py")]
        _apply(tmp_path, first)

        # The implementer re-sends the line with an unrelated change.
        resent = {"files": {"app.py": f"import os\n{_AWS_LINE}\nimport sys\n"}}
        assert run_security_checks(resent, None, tmp_path).secret_findings == []
        result = run_security_checks(resent, None, tmp_path, baseline=baseline)
        assert _blocking(result) == [("secrets", "app.py")]
        assert result.secret_findings[0]["line"] == 2
        _apply(tmp_path, resent)

        # A later patch does not touch app.py at all.
        elsewhere = {"files": {"util.py": "X = 1\n"}}
        result = run_security_checks(elsewhere, None, tmp_path, baseline=baseline)
        assert _blocking(result) == [("secrets", "app.py")]
        _apply(tmp_path, elsewhere)

        # Removing the line resolves the finding.
        fixed = {"files": {"app.py": "import os\nimport sys\n"}}
        result = run_security_checks(fixed, None, tmp_path, baseline=baseline)
        assert result.findings == []

    def test_pre_existing_lines_are_still_not_attributed(self, tmp_path: Path):
        _write(tmp_path, "config.py", f"{_AWS_LINE}\n# TODO\n")
        constraints = Constraints.from_dict({"forbidden_patterns": ["TODO"]})
        baseline = SecurityBaseline(tmp_path)
        for _ in range(2):
            patch_data = {"files": {"config.py": f"{_AWS_LINE}\n# TODO\nTIMEOUT = 30\n"}}
            result = run_security_checks(patch_data, constraints, tmp_path, baseline=baseline)
            assert result.findings == []
            _apply(tmp_path, patch_data)

    def test_forbidden_pattern_is_reported_until_removed(self, tmp_path: Path):
        constraints = Constraints.from_dict({"forbidden_patterns": ["eval("]})
        baseline = SecurityBaseline(tmp_path)
        first = {"files": {"calc.py": "def f(s):\n    return eval(s)\n"}}
        assert _blocking(run_security_checks(first, constraints, tmp_path, baseline=baseline))
        _apply(tmp_path, first)

        untouched = {"files": {"other.py": "pass\n"}}
        result = run_security_checks(untouched, constraints, tmp_path, baseline=baseline)
        assert [(v["rule"], v["file"], v["line"]) for v in result.constraint_violations] == [
            ("forbidden_pattern", "calc.py", 2)
        ]

    def test_protected_file_edit_is_reported_until_restored(self, tmp_path: Path):
        _write(tmp_path, ".env", "A=1\n")
        constraints = Constraints(no_modify=[".env"])
        baseline = SecurityBaseline(tmp_path)

        edit = {"files": {".env": "A=2\n"}}
        assert _blocking(run_security_checks(edit, constraints, tmp_path, baseline=baseline)) == [
            ("constraints", ".env")
        ]
        _apply(tmp_path, edit)

        for later in ({"files": {".env": "A=2\n"}}, {"files": {"main.py": "x = 1\n"}}):
            result = run_security_checks(later, constraints, tmp_path, baseline=baseline)
            assert _blocking(result) == [("constraints", ".env")]
            _apply(tmp_path, later)

        restore = {"files": {".env": "A=1\n"}}
        assert run_security_checks(restore, constraints, tmp_path, baseline=baseline).findings == []

    def test_deleted_protected_file_is_reported_until_restored(self, tmp_path: Path):
        _write(tmp_path, "LICENSE", "MIT\n")
        constraints = Constraints(no_modify=["LICENSE"])
        baseline = SecurityBaseline(tmp_path)

        delete = {"files": {}, "delete_files": ["LICENSE"]}
        assert _blocking(run_security_checks(delete, constraints, tmp_path, baseline=baseline))
        _apply(tmp_path, delete)

        later = {"files": {"main.py": "x = 1\n"}}
        result = run_security_checks(later, constraints, tmp_path, baseline=baseline)
        assert _blocking(result) == [("constraints", "LICENSE")]

        restore = {"files": {"LICENSE": "MIT\n"}}
        assert run_security_checks(restore, constraints, tmp_path, baseline=baseline).findings == []

    def test_resumed_run_rebuilds_the_baseline_from_git(self, tmp_path: Path):
        if shutil.which("git") is None:
            pytest.skip("git is not installed")

        def git(*args: str) -> str:
            identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
            return subprocess.run(
                ["git", "-C", str(tmp_path), *identity, *args],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        git("init", "-q")
        _write(tmp_path, "app.py", "import os\n")
        git("add", "app.py")
        git("commit", "-q", "-m", "base")
        fork_point = git("rev-parse", "HEAD")
        _write(tmp_path, "app.py", f"import os\n{_AWS_LINE}\n")
        git("commit", "-q", "-am", "iteration 1")

        # After a resume the checkpoint names the changed paths and the fork point.
        baseline = SecurityBaseline(tmp_path, changed_paths=["app.py"], git_ref=fork_point)
        later = {"files": {"util.py": "X = 1\n"}}
        result = run_security_checks(later, None, tmp_path, baseline=baseline)
        assert _blocking(result) == [("secrets", "app.py")]


class TestEditsAreScanned:
    """#98 search/replace edits are checked like whole-file writes."""

    def test_secret_introduced_by_an_edit_is_found(self, tmp_path: Path):
        _write(tmp_path, "settings.py", "DEBUG = False\nKEY = None\n")
        patch_data = {
            "files": {},
            "edits": [{"path": "settings.py", "search": "KEY = None", "replace": _AWS_LINE}],
        }
        result = run_security_checks(patch_data, None, tmp_path)
        assert [(s["file"], s["line"]) for s in result.secret_findings] == [("settings.py", 2)]

    def test_edit_to_a_protected_file_is_a_violation(self, tmp_path: Path):
        _write(tmp_path, ".env", "A=1\n")
        patch_data = {"edits": [{"path": ".env", "search": "A=1", "replace": "A=2"}]}
        result = run_security_checks(patch_data, Constraints(no_modify=[".env"]), tmp_path)
        assert [(v["rule"], v["file"]) for v in result.constraint_violations] == [
            ("no_modify", ".env")
        ]

    def test_edit_that_does_not_apply_is_not_scanned(self, tmp_path: Path):
        _write(tmp_path, "settings.py", "DEBUG = False\n")
        patch_data = {"edits": [{"path": "settings.py", "search": "nope", "replace": _AWS_LINE}]}
        assert run_security_checks(patch_data, None, tmp_path).findings == []
