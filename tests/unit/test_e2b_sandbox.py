"""Tests for E2B sandbox runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from delegatron.sandbox.e2b_runner import E2BSandbox

# ---------------------------------------------------------------------------
# Helpers: mock E2B SDK objects
# ---------------------------------------------------------------------------


class _FakeCommandResult:
    """Simulates the result of sandbox.commands.run()."""

    def __init__(self, exit_code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeCommandExit(Exception):
    """Mirrors e2b's ``CommandExitException``: raised by ``commands.run`` on a non-zero exit."""

    def __init__(self, stdout: str = "", stderr: str = "", exit_code: int = 1) -> None:
        super().__init__(f"Command exited with code {exit_code} and error:\n{stderr}")
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


def _make_mock_sandbox(
    cmd_results: list[Any] | None = None,
) -> AsyncMock:
    """Return an AsyncMock that behaves like an E2B AsyncSandbox instance."""
    sandbox = AsyncMock()
    sandbox.sandbox_id = "test-sandbox-123"

    # sandbox.commands.run(...)
    if cmd_results is None:
        cmd_results = [_FakeCommandResult(exit_code=0, stdout="3 passed in 0.5s")]
    sandbox.commands.run = AsyncMock(side_effect=cmd_results)

    # sandbox.files.write(...)
    sandbox.files.write = AsyncMock()

    # sandbox.kill()
    sandbox.kill = AsyncMock()

    return sandbox


def _scripted(results: dict[str, Any]) -> AsyncMock:
    """``commands.run`` answering by the first key found in the command (default: success)."""

    async def _run(command: str, **_kwargs: Any) -> Any:
        for marker, response in results.items():
            if marker in command:
                if isinstance(response, BaseException):
                    raise response
                return response
        return _FakeCommandResult(exit_code=0, stdout="1 passed in 0.1s")

    return AsyncMock(side_effect=_run)


def _commands(mock_sandbox: AsyncMock) -> list[str]:
    return [call.args[0] for call in mock_sandbox.commands.run.call_args_list]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestE2BSandboxInit:
    """Tests for E2BSandbox construction."""

    def test_defaults(self, tmp_path: Path) -> None:
        sb = E2BSandbox(workspace_root=tmp_path)
        assert sb._workspace_root == tmp_path
        assert sb._test_commands == ["python -m pytest -v"]
        assert sb._install_commands == []
        assert sb._template == "base"

    def test_custom_args(self, tmp_path: Path) -> None:
        sb = E2BSandbox(
            workspace_root=tmp_path,
            test_commands=["make test"],
            install_commands=["pip install -e ."],
            template="python-3.12",
            timeout=120,
        )
        assert sb._test_commands == ["make test"]
        assert sb._install_commands == ["pip install -e ."]
        assert sb._template == "python-3.12"
        assert sb._timeout == 120


class TestApplyAndTestWithMockSandbox:
    """Tests that exercise apply_and_test with a mocked E2B SDK."""

    @pytest.fixture(autouse=True)
    def _patch_e2b(self) -> Any:
        """Patch the SDK names for every test in this class, so none needs the SDK."""
        # Use a default return value so any number of preceding commands.run
        # calls (mkdir, upload, etc.) succeed, and the last one (the test
        # command) also returns a sensible result.
        mock_sandbox = _make_mock_sandbox()
        mock_sandbox.commands.run = AsyncMock(
            return_value=_FakeCommandResult(exit_code=0, stdout="5 passed in 1.2s"),
        )
        mock_cls = AsyncMock()
        mock_cls.create = AsyncMock(return_value=mock_sandbox)

        with (
            patch("delegatron.sandbox.e2b_runner._E2B_AVAILABLE", True),
            patch("delegatron.sandbox.e2b_runner._AsyncSandbox", mock_cls),
            patch("delegatron.sandbox.e2b_runner._CommandExitException", _FakeCommandExit),
        ):
            self._mock_sandbox = mock_sandbox
            self._mock_cls = mock_cls
            yield

    def _use(self, mock_sandbox: AsyncMock) -> AsyncMock:
        self._mock_cls.create = AsyncMock(return_value=mock_sandbox)
        return mock_sandbox

    @pytest.mark.asyncio
    async def test_files_patch(self, tmp_path: Path) -> None:
        """Applies a patch with explicit files and runs tests."""
        sb = E2BSandbox(workspace_root=tmp_path)
        result = await sb.apply_and_test({"files": {"hello.py": "print('hello')\n"}})
        assert result["passed"] is True
        assert result["total"] == 5
        assert result["failures"] == 0
        # >= 0: Windows' ~16 ms monotonic clock can measure an instant mocked run as 0.
        assert result["duration_ms"] >= 0

        # Sandbox should have been created and killed
        self._mock_cls.create.assert_awaited_once()
        self._mock_sandbox.kill.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_files_written_to_sandbox(self, tmp_path: Path) -> None:
        """Verifies files are written via sandbox.files.write."""
        sb = E2BSandbox(workspace_root=tmp_path)
        await sb.apply_and_test(
            {"files": {"src/app.py": "x = 1\n", "tests/test_app.py": "assert True\n"}}
        )
        # Check that files.write was called for both patch files
        written_paths = [call.args[0] for call in self._mock_sandbox.files.write.call_args_list]
        assert "/home/user/workspace/src/app.py" in written_paths
        assert "/home/user/workspace/tests/test_app.py" in written_paths

    @pytest.mark.asyncio
    async def test_test_failure(self, tmp_path: Path) -> None:
        """A failing run raises CommandExitException in the SDK; its output is still reported."""
        stdout = (
            "tests/test_x.py::test_a PASSED\n"
            "tests/test_x.py::test_b FAILED\n"
            "tests/test_x.py::test_c FAILED\n"
            "E       assert 1 == 2\n"
            "1 passed, 2 failed in 0.8s\n"
        )
        mock_sandbox = self._use(_make_mock_sandbox())
        mock_sandbox.commands.run = _scripted(
            {"pytest": _FakeCommandExit(stdout=stdout, stderr="", exit_code=1)}
        )

        sb = E2BSandbox(workspace_root=tmp_path)
        with patch("delegatron.sandbox.e2b_runner.logger") as log:
            result = await sb.apply_and_test({"files": {"x.py": ""}})
        assert result["passed"] is False
        assert result["total"] == 3
        assert result["failures"] == 2
        assert "E       assert 1 == 2" in result["stdout"]
        assert result["failed_tests"] == ["tests/test_x.py::test_b", "tests/test_x.py::test_c"]
        log.exception.assert_not_called()  # an ordinary failure is not a sandbox error
        mock_sandbox.kill.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returned_non_zero_result_also_fails(self, tmp_path: Path) -> None:
        """A result object with a non-zero exit (older SDKs) is reported the same way."""
        mock_sandbox = self._use(_make_mock_sandbox())
        mock_sandbox.commands.run = _scripted(
            {"pytest": _FakeCommandResult(exit_code=1, stdout="1 passed, 2 failed")}
        )
        result = await E2BSandbox(workspace_root=tmp_path).apply_and_test({"files": {"x.py": ""}})
        assert result["passed"] is False
        assert result["total"] == 3

    @pytest.mark.asyncio
    async def test_per_test_fields_from_untruncated_output(self, tmp_path: Path) -> None:
        """results / failed_tests / failure_excerpt come from the full output."""
        passing = "".join(f"tests/test_a.py::test_{i} PASSED{' ' * 50}[ 99%]\n" for i in range(200))
        stdout = (
            f"{passing}tests/test_a.py::test_bad FAILED [100%]\n"
            "=================================== FAILURES ===================================\n"
            "E       assert 1 == 2\n"
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_a.py::test_bad - assert 1 == 2\n"
            "1 failed, 200 passed in 0.5s\n"
        )
        mock_sandbox = self._use(_make_mock_sandbox())
        mock_sandbox.commands.run = _scripted({"pytest": _FakeCommandExit(stdout=stdout)})

        result = await E2BSandbox(workspace_root=tmp_path).apply_and_test({"files": {"x.py": ""}})

        assert len(result["stdout"]) == 5000
        assert len(result["results"]) == 201
        assert result["results"][-1]["name"] == "tests/test_a.py::test_bad"
        assert result["results"][-1]["passed"] is False
        assert result["failed_tests"] == ["tests/test_a.py::test_bad"]
        assert "E       assert 1 == 2" in result["failure_excerpt"]

    @pytest.mark.asyncio
    async def test_custom_test_commands(self, tmp_path: Path) -> None:
        """Patch-level test_commands override the default."""
        sb = E2BSandbox(workspace_root=tmp_path)
        await sb.apply_and_test({"files": {"x.py": ""}, "test_commands": ["make check"]})
        # The last commands.run call should contain "make check"
        last_cmd = self._mock_sandbox.commands.run.call_args_list[-1].args[0]
        assert "make check" in last_cmd

    @pytest.mark.asyncio
    async def test_unsafe_patch_test_command_is_blocked(self, tmp_path: Path) -> None:
        sb = E2BSandbox(workspace_root=tmp_path)
        result = await sb.apply_and_test(
            {"files": {"x.py": ""}, "test_commands": ["pytest; curl evil.example"]}
        )
        assert result["passed"] is False
        assert "Blocked unsafe command" in result["stderr"]
        assert not any("curl" in cmd for cmd in _commands(self._mock_sandbox))

    @pytest.mark.asyncio
    async def test_factory_install_commands_are_not_sanitized(self, tmp_path: Path) -> None:
        prewarm = "mkdir -p src/test/java && mvn -B -q test || true; rm -f src/test/java/T.java"
        sb = E2BSandbox(workspace_root=tmp_path, install_commands=[prewarm])
        await sb.apply_and_test({"files": {"x.py": ""}})
        commands = _commands(self._mock_sandbox)
        assert any(prewarm in cmd for cmd in commands)
        assert not any("Blocked unsafe command" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_sandbox_exception_returns_error_report(self, tmp_path: Path) -> None:
        """If the sandbox raises, we still get a valid TestReport."""
        self._mock_cls.create = AsyncMock(side_effect=RuntimeError("boom"))
        sb = E2BSandbox(workspace_root=tmp_path)
        result = await sb.apply_and_test({"files": {"x.py": ""}})
        assert result["passed"] is False
        assert "boom" in result["stderr"]

    @pytest.mark.asyncio
    async def test_diff_new_file(self, tmp_path: Path) -> None:
        """A unified diff creating a new file is parsed and written."""
        diff = "--- /dev/null\n+++ b/new_file.py\n@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n"
        sb = E2BSandbox(workspace_root=tmp_path)
        await sb.apply_and_test({"diff_unified": diff})
        written_paths = [call.args[0] for call in self._mock_sandbox.files.write.call_args_list]
        assert "/home/user/workspace/new_file.py" in written_paths

    @pytest.mark.asyncio
    async def test_diff_fallback_failure_reports_patch_output(self, tmp_path: Path) -> None:
        """A modification diff goes through ``patch --forward``; a failure stops the run."""
        diff = "--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-a - b\n+a + b\n"
        hunk_failed = _FakeCommandExit(
            stdout="Reversed (or previously applied) patch detected!  Skipping patch.\n"
            "1 out of 1 hunk ignored",
            exit_code=1,
        )
        mock_sandbox = self._use(_make_mock_sandbox())
        mock_sandbox.commands.run = _scripted({"patch -p1": hunk_failed})

        result = await E2BSandbox(workspace_root=tmp_path).apply_and_test({"diff_unified": diff})

        assert result["passed"] is False
        assert result["stderr"].startswith("Patch failed to apply (exit 1): patch -p1 --forward")
        assert "Skipping patch." in result["stderr"]
        assert len(_commands(mock_sandbox)) == 1  # no install or test run

    @pytest.mark.asyncio
    async def test_greenfield_requirements_installed(self, tmp_path: Path) -> None:
        """A manifest the patch adds is installed (the start-time list did not know it)."""
        files = {
            "app.py": "import requests\n",
            "requirements.txt": "requests\n",
            "tests/test_app.py": "import app\n",
        }
        # The orchestrator applies the patch to the workspace before the sandbox runs.
        for rel_path, content in files.items():
            (tmp_path / rel_path).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel_path).write_bytes(content.encode())
        sb = E2BSandbox(workspace_root=tmp_path, install_commands=["pip install pytest -q"])

        await sb.apply_and_test({"files": files})

        install = next(cmd for cmd in _commands(self._mock_sandbox) if "pip install" in cmd)
        assert install.endswith("&& pip install -r requirements.txt -q && pip install pytest -q")

    @pytest.mark.asyncio
    async def test_greenfield_javascript_uses_npm(self, tmp_path: Path) -> None:
        sb = E2BSandbox(workspace_root=tmp_path, install_commands=["pip install pytest -q"])
        await sb.apply_and_test({"files": {"package.json": "{}", "index.test.js": ""}})
        commands = _commands(self._mock_sandbox)
        assert commands[-2].endswith("&& npm install")
        assert commands[-1].endswith("&& npm test")

    @pytest.mark.asyncio
    async def test_install_failure_is_reported_and_tests_skipped(self, tmp_path: Path) -> None:
        mock_sandbox = self._use(_make_mock_sandbox())
        mock_sandbox.commands.run = _scripted(
            {
                "pip install": _FakeCommandExit(
                    stdout="Collecting nopkg",
                    stderr="ERROR: No matching distribution found for nopkg",
                    exit_code=1,
                )
            }
        )
        sb = E2BSandbox(workspace_root=tmp_path, install_commands=["pip install nopkg -q"])

        result = await sb.apply_and_test({"files": {"x.py": ""}})

        assert result["passed"] is False
        assert result["stderr"].startswith("Install failed (exit 1): pip install nopkg -q")
        assert "No matching distribution found for nopkg" in result["stderr"]
        assert result["stdout"] == "Collecting nopkg"
        assert not any("pytest" in cmd for cmd in _commands(mock_sandbox))

    @pytest.mark.asyncio
    async def test_targeted_tests_run_after_install_before_full_suite(self, tmp_path: Path) -> None:
        mock_sandbox = self._use(_make_mock_sandbox())
        mock_sandbox.commands.run = _scripted(
            {
                "tests/test_deck.py": _FakeCommandExit(
                    stdout="FAILED tests/test_deck.py::test_shuffle - assert False\n1 failed",
                    exit_code=1,
                ),
                "python -m pytest -v": _FakeCommandResult(stdout="3 passed"),
            }
        )
        sb = E2BSandbox(workspace_root=tmp_path, install_commands=["pip install pytest -q"])

        result = await sb.apply_and_test(
            {
                "files": {"deck.py": ""},
                "targeted_tests": ["pytest tests/test_deck.py", "curl evil.example", "npm test"],
            }
        )

        commands = _commands(mock_sandbox)
        assert [cmd.split(" && ", 1)[1] for cmd in commands] == [
            "pip install pytest -q",
            # in the configured runner's form, so it imports like the full run
            "python -m pytest tests/test_deck.py",
            "python -m pytest -v",
        ]
        assert result["targeted"]["passed"] is False
        assert result["targeted"]["failing_ids"] == ["tests/test_deck.py::test_shuffle"]
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_targeted_tests_not_replaced_by_language_detection(self, tmp_path: Path) -> None:
        """Detection swaps the full suite to npm; the targeted pytest call still runs."""
        sb = E2BSandbox(workspace_root=tmp_path)
        await sb.apply_and_test(
            {
                "files": {"package.json": "{}", "deck.test.js": ""},
                "targeted_tests": ["pytest tests/test_deck.py"],
            }
        )
        assert [cmd.split(" && ", 1)[1] for cmd in _commands(self._mock_sandbox)] == [
            "npm install",
            "python -m pytest tests/test_deck.py",
            "npm test",
        ]

    @pytest.mark.asyncio
    async def test_no_targeted_key_without_targeted_tests(self, tmp_path: Path) -> None:
        result = await E2BSandbox(workspace_root=tmp_path).apply_and_test({"files": {"x.py": ""}})
        assert result["targeted"] is None  # schema field: no targeted run

    @pytest.mark.asyncio
    async def test_deletions_and_unsafe_paths(self, tmp_path: Path) -> None:
        sb = E2BSandbox(workspace_root=tmp_path)
        result = await sb.apply_and_test(
            {
                "files": {"tests/test_a.py": "", "../escape.py": "", "C:/x.py": ""},
                "delete_files": ["test_a.py", "/etc/passwd"],
            }
        )
        written = [call.args[0] for call in self._mock_sandbox.files.write.call_args_list]
        assert written == ["/home/user/workspace/tests/test_a.py"]
        assert "rm -f /home/user/workspace/test_a.py" in _commands(self._mock_sandbox)
        assert result["stderr"].startswith(
            "Skipped unsafe patch path(s): ../escape.py, C:/x.py, /etc/passwd\n"
        )
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_upload_reads_utf8_and_skips_internal_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "cards.py").write_bytes("NAME = 'Pokémon ✓'\n".encode())
        (tmp_path / "blob.bin").write_bytes(b"\x81\x8d\xff")
        for internal in ("runs/abc123", "_sandbox", ".delegatron-venv"):
            (tmp_path / internal).mkdir(parents=True)
            (tmp_path / internal / "x.py").write_bytes(b"x = 1\n")
        (tmp_path / "runs" / "abc123" / "summary.json").write_bytes(b"{}")  # Delegatron's runs/
        (tmp_path / "app" / "runs").mkdir(parents=True)
        (tmp_path / "app" / "runs" / "job.py").write_bytes(b"y = 2\r\n")
        (tmp_path / "reviews").mkdir()  # a user package that happens to be called reviews
        (tmp_path / "reviews" / "models.py").write_bytes(b"z = 3\n")

        await E2BSandbox(workspace_root=tmp_path).apply_and_test({})

        written = {
            call.args[0]: call.args[1] for call in self._mock_sandbox.files.write.call_args_list
        }
        assert written == {
            "/home/user/workspace/reviews/models.py": "z = 3\n",
            "/home/user/workspace/app/runs/job.py": "y = 2\r\n",  # line endings kept
            "/home/user/workspace/cards.py": "NAME = 'Pokémon ✓'\n",
        }

    @pytest.mark.asyncio
    async def test_commands_run_with_the_workspace_on_pythonpath(self, tmp_path: Path) -> None:
        await E2BSandbox(workspace_root=tmp_path).apply_and_test({"files": {"x.py": ""}})
        runs = [
            call
            for call in self._mock_sandbox.commands.run.call_args_list
            if call.args[0].startswith("cd /home/user/workspace && ")
        ]
        assert runs  # install and test
        for call in runs:
            assert call.kwargs["envs"] == {"PYTHONPATH": "/home/user/workspace"}

    @pytest.mark.asyncio
    async def test_python_patch_with_a_shell_script_keeps_pytest(self, tmp_path: Path) -> None:
        """#93: an incidental run_tests.sh never turns a Python patch into `bash -n`."""
        sb = E2BSandbox(workspace_root=tmp_path, install_commands=["pip install pytest -q"])
        await sb.apply_and_test(
            {
                "files": {
                    "card.py": "X = 1\n",
                    "tests/test_card.py": "def test_x():\n    pass\n",
                    "run_tests.sh": "#!/bin/sh\npytest\n",
                }
            }
        )
        commands = [
            cmd.split(" && ", 1)[1] for cmd in _commands(self._mock_sandbox) if " && " in cmd
        ]
        assert commands == ["pip install pytest -q", "python -m pytest -v"]

    @pytest.mark.asyncio
    async def test_language_hint_keeps_the_configured_toolchain(self, tmp_path: Path) -> None:
        """#93/#118: a Python repo whose patch only touches its tooling package.json."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
        sb = E2BSandbox(
            workspace_root=tmp_path,
            install_commands=["pip install -e . -q", "pip install pytest -q"],
            language="python",
        )
        await sb.apply_and_test({"files": {"package.json": "{}"}})
        commands = [
            cmd.split(" && ", 1)[1] for cmd in _commands(self._mock_sandbox) if " && " in cmd
        ]
        assert commands == ["pip install -e . -q && pip install pytest -q", "python -m pytest -v"]

    @pytest.mark.asyncio
    async def test_entrypoints_are_smoke_run_after_the_tests(self, tmp_path: Path) -> None:
        mock_sandbox = self._use(_make_mock_sandbox())
        mock_sandbox.commands.run = _scripted(
            {
                "timeout 10 python deal.py": _FakeCommandResult(stdout="dealt 5 cards\n"),
                "timeout 10 python hang.py": _FakeCommandExit(stdout="tick", exit_code=124),
            }
        )
        result = await E2BSandbox(workspace_root=tmp_path).apply_and_test(
            {
                "files": {"deal.py": "print('dealt 5 cards')\n", "hang.py": "input()\n"},
                "entrypoints": ["deal.py", "missing.py", "../x.py", "hang.py"],
            }
        )

        commands = _commands(mock_sandbox)
        smoke = [cmd for cmd in commands if "timeout 10 python" in cmd]
        assert smoke == [
            "cd /home/user/workspace && timeout 10 python deal.py </dev/null",
            "cd /home/user/workspace && timeout 10 python hang.py </dev/null",
        ]
        assert commands.index(smoke[0]) > max(
            i for i, cmd in enumerate(commands) if "python -m pytest -v" in cmd
        )
        smoke_calls = [
            call
            for call in mock_sandbox.commands.run.call_args_list
            if "timeout 10 python" in call.args[0]
        ]
        assert all(call.kwargs["timeout"] == 20 for call in smoke_calls)
        deal, hang = result["entrypoint_runs"]
        assert (deal["file"], deal["returncode"], deal["stdout_head"]) == (
            "deal.py",
            0,
            "dealt 5 cards\n",
        )
        assert (hang["file"], hang["timed_out"], hang["returncode"]) == ("hang.py", True, None)
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_entrypoint_sdk_error_is_recorded_not_fatal(self, tmp_path: Path) -> None:
        class TimeoutException(Exception):
            pass

        mock_sandbox = self._use(_make_mock_sandbox())
        mock_sandbox.commands.run = _scripted(
            {"timeout 10 python": TimeoutException("context deadline exceeded")}
        )
        result = await E2BSandbox(workspace_root=tmp_path).apply_and_test(
            {"files": {"deal.py": ""}, "entrypoints": ["deal.py"]}
        )
        [run] = result["entrypoint_runs"]
        assert run["timed_out"] is True
        assert "deadline" in run["stderr_tail"]
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_deleted_entrypoint_is_not_run(self, tmp_path: Path) -> None:
        (tmp_path / "old.py").write_text("print(1)\n", encoding="utf-8")
        result = await E2BSandbox(workspace_root=tmp_path).apply_and_test(
            {"delete_files": ["old.py"], "entrypoints": ["old.py"]}
        )
        assert not any("timeout 10 python" in cmd for cmd in _commands(self._mock_sandbox))
        assert result["entrypoint_runs"] == []

    @pytest.mark.asyncio
    async def test_output_truncation_is_reported(self, tmp_path: Path) -> None:
        mock_sandbox = self._use(_make_mock_sandbox())
        mock_sandbox.commands.run = _scripted(
            {"pytest": _FakeCommandResult(stdout="x" * 6000 + "\n1 passed in 0.1s\n")}
        )
        result = await E2BSandbox(workspace_root=tmp_path).apply_and_test({"files": {"x.py": ""}})
        assert result["output_truncated"] is True
        assert (result["total"], result["failures"]) == (1, 0)


class TestMissingSDK:
    """Tests behaviour when e2b_code_interpreter is not installed."""

    @pytest.mark.asyncio
    async def test_returns_error_when_sdk_missing(self, tmp_path: Path) -> None:
        with patch("delegatron.sandbox.e2b_runner._E2B_AVAILABLE", False):
            sb = E2BSandbox(workspace_root=tmp_path)
            result = await sb.apply_and_test({"files": {"x.py": ""}})
            assert result["passed"] is False
            assert "e2b_code_interpreter is not installed" in result["stderr"]


class TestParseTestOutput:
    """Tests for the static _parse_test_output helper."""

    def test_passing(self) -> None:
        result = E2BSandbox._parse_test_output("5 passed in 1.23s")
        assert result == {"total": 5, "failures": 0}

    def test_mixed(self) -> None:
        result = E2BSandbox._parse_test_output("3 passed, 2 failed in 2.5s")
        assert result == {"total": 5, "failures": 2}

    def test_empty(self) -> None:
        result = E2BSandbox._parse_test_output("no tests collected")
        assert result == {"total": 0, "failures": 0}

    def test_errors(self) -> None:
        result = E2BSandbox._parse_test_output("1 passed, 1 error in 0.3s")
        assert result == {"total": 2, "failures": 1}

    def test_collection_error_counted_once(self) -> None:
        output = (
            "collected 0 items / 1 error\n"
            "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!\n"
            "=============================== 1 error in 0.12s ===============================\n"
        )
        assert E2BSandbox._parse_test_output(output) == {"total": 1, "failures": 1}

    def test_jest_summary_is_not_double_counted(self) -> None:
        output = "Test Suites: 1 failed, 1 total\nTests:       2 failed, 5 passed, 7 total\n"
        assert E2BSandbox._parse_test_output(output) == {"total": 7, "failures": 2}


class TestParseDiffToFiles:
    """Tests for the static _parse_diff_to_files helper."""

    def test_new_file(self) -> None:
        diff = "--- /dev/null\n+++ b/hello.py\n@@ -0,0 +1 @@\n+print('hi')\n"
        result = E2BSandbox._parse_diff_to_files(diff)
        assert "hello.py" in result
        assert "print('hi')" in result["hello.py"]

    def test_modification_returns_empty(self) -> None:
        diff = "--- a/hello.py\n+++ b/hello.py\n@@ -1 +1 @@\n-old\n+new\n"
        result = E2BSandbox._parse_diff_to_files(diff)
        assert result == {}


class TestValidateE2BPath:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("./src/a.py", "src/a.py"), ("src\\a.py", "src/a.py"), ("a//b.py", "a/b.py")],
    )
    def test_normalizes(self, raw: str, expected: str) -> None:
        assert E2BSandbox._validate_e2b_path(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "message"),
        [
            ("//host/share/x.py", "Absolute path"),
            ("\\\\host\\share\\x.py", "Absolute path"),
            ("C:x.py", "Absolute path"),
            ("a\\..\\..\\x.py", "Path traversal"),
            ("", "Empty path"),
        ],
    )
    def test_rejects(self, raw: str, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            E2BSandbox._validate_e2b_path(raw)
