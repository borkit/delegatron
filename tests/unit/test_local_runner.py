"""Tests for the local (no-container) sandbox runner."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import delegatron.sandbox.local_runner as local_runner
from delegatron.sandbox.local_runner import _CMD_TIMEOUT, LocalSandbox

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _venv_root(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every venv these tests touch out of the real temp dir."""
    root = tmp_path_factory.mktemp("venvs")
    monkeypatch.setattr(local_runner, "_VENV_ROOT", root)
    return root


def _make_sandbox(
    tmp_path: Path,
    test_commands: list[str] | None = None,
    install_commands: list[str] | None = None,
) -> LocalSandbox:
    """Create a LocalSandbox rooted at *tmp_path*."""
    return LocalSandbox(
        workspace_root=tmp_path,
        test_commands=test_commands or ["pytest"],
        install_commands=install_commands or [],
    )


def _without_venv(sandbox: LocalSandbox) -> LocalSandbox:
    """Skip venv creation: commands then run with the host's PATH."""
    sandbox._ensure_venv = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return sandbox


def _fake_venv(sandbox: LocalSandbox) -> Path:
    """Make the sandbox's venv look created (its python executable exists)."""
    bin_dir = sandbox.venv_dir / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True)
    (bin_dir / ("python.exe" if os.name == "nt" else "python")).write_bytes(b"")
    return bin_dir


def _smoke_tested(files: dict[str, str]) -> dict[str, Any]:
    """The patch the orchestrator sends once it has added its synthetic smoke test."""
    from delegatron.orchestrator.graph import Pipeline

    return Pipeline._ensure_tests({"files": dict(files)})


def _recording_run(sandbox: LocalSandbox, results: dict[str, tuple[int, str, str]]) -> list[str]:
    """Stub ``_run``: answer by the first key found in the command (default: success)."""
    calls: list[str] = []

    async def _run(cmd: str) -> tuple[int, str, str]:
        calls.append(cmd)
        return next((r for key, r in results.items() if key in cmd), (0, "1 passed", ""))

    sandbox._run = AsyncMock(side_effect=_run)  # type: ignore[method-assign]
    return calls


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


class TestLocalSandboxInit:
    """Constructor stores the parameters correctly."""

    def test_stores_workspace_root(self, tmp_path: Path):
        s = _make_sandbox(tmp_path)
        assert s.workspace_root == tmp_path

    def test_stores_test_commands(self, tmp_path: Path):
        s = _make_sandbox(tmp_path, test_commands=["pytest -v", "mypy ."])
        assert s.test_commands == ["pytest -v", "mypy ."]

    def test_stores_install_commands(self, tmp_path: Path):
        s = _make_sandbox(tmp_path, install_commands=["pip install -e ."])
        assert s.install_commands == ["pip install -e ."]


# ---------------------------------------------------------------------------
# _parse_test_counts  (static, synchronous)
# ---------------------------------------------------------------------------


class TestParseTestCounts:
    """Tests for the static _parse_test_counts method."""

    def test_all_passed(self):
        total, failures = LocalSandbox._parse_test_counts("5 passed in 1.2s")
        assert total == 5
        assert failures == 0

    def test_mixed_results(self):
        total, failures = LocalSandbox._parse_test_counts("3 passed, 2 failed in 2.5s")
        assert total == 5
        assert failures == 2

    def test_all_failed(self):
        total, failures = LocalSandbox._parse_test_counts("0 passed, 4 failed")
        assert total == 4
        assert failures == 4

    def test_no_numbers(self):
        total, failures = LocalSandbox._parse_test_counts("no tests ran")
        assert total == 0
        assert failures == 0

    def test_empty_string(self):
        total, failures = LocalSandbox._parse_test_counts("")
        assert total == 0
        assert failures == 0

    def test_only_passed(self):
        total, failures = LocalSandbox._parse_test_counts("10 passed")
        assert total == 10
        assert failures == 0

    def test_only_failed(self):
        total, failures = LocalSandbox._parse_test_counts("7 failed")
        assert total == 7
        assert failures == 7

    def test_jest_style_output(self):
        """Jest says 'X passed', similar enough for the regex."""
        output = "Tests: 3 passed, 1 failed, 4 total"
        total, failures = LocalSandbox._parse_test_counts(output)
        assert total == 4
        assert failures == 1


# ---------------------------------------------------------------------------
# _run  (async)
# ---------------------------------------------------------------------------


class TestRun:
    """Tests for the _run helper that executes shell commands."""

    @pytest.fixture()
    def sandbox(self, tmp_path: Path) -> LocalSandbox:
        return _make_sandbox(tmp_path)

    async def test_successful_command(self, sandbox: LocalSandbox):
        """A simple command that exits 0."""
        rc, stdout, _stderr = await sandbox._run("echo hello")
        assert rc == 0
        assert "hello" in stdout

    async def test_failing_command(self, sandbox: LocalSandbox):
        """A command that exits non-zero."""
        rc, _stdout, _stderr = await sandbox._run('python -c "import sys; sys.exit(42)"')
        assert rc == 42

    async def test_venv_path_prepended_when_set(self, sandbox: LocalSandbox):
        """When _venv_path is set, PATH is modified."""
        sandbox._venv_path = "/fake/venv/bin"
        assert sandbox._env()["PATH"].startswith("/fake/venv/bin" + os.pathsep)
        assert sandbox._env()["VIRTUAL_ENV"] == str(sandbox.venv_dir)
        rc, _stdout, _stderr = await sandbox._run("echo ok")
        assert rc == 0

    async def test_timeout_returns_error(self, sandbox: LocalSandbox, tmp_path: Path):
        """When a command times out, we get rc=1 and a timeout message."""
        hang = f'"{sys.executable}" -c "import time; time.sleep(60)"'
        with patch("delegatron.sandbox.local_runner._CMD_TIMEOUT", 0.5):
            rc, _stdout, stderr = await sandbox._run(hang)
        assert rc == 1
        assert stderr == "Command timed out"

    async def test_exception_returns_error(self, sandbox: LocalSandbox):
        """If create_subprocess_shell raises, we get rc=1."""
        with patch("asyncio.create_subprocess_shell", side_effect=OSError("boom")):
            rc, _stdout, stderr = await sandbox._run("anything")
            assert rc == 1
            assert "boom" in stderr


# ---------------------------------------------------------------------------
# _apply_diff  (async)
# ---------------------------------------------------------------------------


def _proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


class TestApplyDiff:
    """Tests for unified diff application via git apply."""

    @pytest.fixture()
    def sandbox(self, tmp_path: Path) -> LocalSandbox:
        return _make_sandbox(tmp_path)

    async def test_calls_git_apply(self, sandbox: LocalSandbox):
        """Verify git apply is invoked with the diff on stdin."""
        with patch("asyncio.create_subprocess_exec", return_value=_proc()) as mock_exec:
            assert await sandbox._apply_diff("--- a/foo.py\n+++ b/foo.py\n@@ ...\n") is None
            mock_exec.assert_called_once()
            args = mock_exec.call_args[0]
            assert args == ("git", "-c", "core.autocrlf=false", "apply", "--allow-empty", "-")

    async def test_reverse(self, sandbox: LocalSandbox):
        with patch("asyncio.create_subprocess_exec", return_value=_proc()) as mock_exec:
            await sandbox._apply_diff("diff", reverse=True)
        assert mock_exec.call_args[0][-2:] == ("--reverse", "-")

    async def test_diff_sent_as_stdin(self, sandbox: LocalSandbox):
        """The diff content should be sent as stdin bytes."""
        proc = _proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            diff_text = "--- a/test.py\n+++ b/test.py\n"
            await sandbox._apply_diff(diff_text)
            proc.communicate.assert_called_once_with(input=diff_text.encode())

    async def test_git_apply_failure_is_returned(self, sandbox: LocalSandbox):
        """A failing git apply is reported, never swallowed."""
        proc = _proc(1, stderr=b"error: patch failed: foo.py:1\n")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            error = await sandbox._apply_diff("some diff")
        assert error is not None
        assert error.startswith("Patch failed to apply (exit 1): git -c core.autocrlf=false apply")
        assert "patch failed: foo.py:1" in error

    async def test_missing_git_is_returned(self, sandbox: LocalSandbox):
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("git")):
            error = await sandbox._apply_diff("some diff")
        assert error is not None
        assert "git" in error

    @pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
    async def test_diff_applied_for_the_run_and_reverted(self, tmp_path: Path):
        (tmp_path / "calc.py").write_bytes(b"def add(a, b):\n    return a - b\n")
        diff = (
            "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n-    return a - b\n+    return a + b\n"
        )
        sandbox = _without_venv(_make_sandbox(tmp_path))
        seen: list[bytes] = []

        async def _run(cmd: str) -> tuple[int, str, str]:
            seen.append((tmp_path / "calc.py").read_bytes())
            return 0, "1 passed", ""

        sandbox._run = AsyncMock(side_effect=_run)  # type: ignore[method-assign]

        result = await sandbox.apply_and_test({"diff_unified": diff})

        assert result["passed"] is True
        assert seen == [b"def add(a, b):\n    return a + b\n"]
        assert (tmp_path / "calc.py").read_bytes() == b"def add(a, b):\n    return a - b\n"


# ---------------------------------------------------------------------------
# Venv
# ---------------------------------------------------------------------------


class TestVenv:
    def test_venv_is_outside_the_workspace(self, tmp_path: Path, _venv_root: Path):
        sandbox = _make_sandbox(tmp_path / "ws")
        assert not sandbox.venv_dir.is_relative_to(tmp_path / "ws")
        assert sandbox.venv_dir.parent == _venv_root
        assert sandbox.venv_dir == _make_sandbox(tmp_path / "ws").venv_dir  # reused

    async def test_created_with_sys_executable_as_one_argv_element(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(local_runner, "_VENV_ROOT", tmp_path / "dir with space")
        sandbox = _make_sandbox(tmp_path)

        async def _create(*args: str, **_kwargs: Any) -> AsyncMock:
            _fake_venv(sandbox)
            return _proc()

        with patch("asyncio.create_subprocess_exec", side_effect=_create) as create:
            assert await sandbox._ensure_venv() is None

        assert create.call_args[0] == (sys.executable, "-m", "venv", str(sandbox.venv_dir))
        assert " " in str(sandbox.venv_dir)
        assert sandbox._env()["PATH"].startswith(sandbox._venv_path + os.pathsep)

    async def test_failed_creation_leaves_no_broken_venv(self, tmp_path: Path):
        """A venv that failed half-way (python present, no pip) is removed, then retried."""
        sandbox = _make_sandbox(tmp_path)

        async def _half_created(*args: str, **_kwargs: Any) -> AsyncMock:
            _fake_venv(sandbox)
            return _proc(1, stderr=b"ensurepip is not available")

        with patch("asyncio.create_subprocess_exec", side_effect=_half_created) as create:
            first = await sandbox._ensure_venv()
            second = await sandbox._ensure_venv()

        assert first is not None and "ensurepip is not available" in first
        assert second is not None  # tried again and reported again, never reused
        assert create.call_count == 2
        assert not sandbox.venv_dir.exists()
        assert sandbox._venv_path is None

    async def test_failed_creation_stops_the_run_and_leaves_path(self, tmp_path: Path):
        sandbox = _make_sandbox(tmp_path)
        _recording_run(sandbox, {})
        proc = _proc(1, stderr=b"ensurepip is not available")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await sandbox.apply_and_test({"files": {"a.py": "x = 1\n"}})

        assert result["passed"] is False
        assert result["stderr"].startswith("Venv creation failed (exit 1): ")
        assert "ensurepip is not available" in result["stderr"]
        assert sandbox._venv_path is None
        assert sandbox._env()["PATH"] == os.environ.get("PATH", "")
        sandbox._run.assert_not_called()
        assert not (tmp_path / "a.py").exists()

    async def test_existing_venv_not_recreated(self, tmp_path: Path):
        sandbox = _make_sandbox(tmp_path)
        bin_dir = _fake_venv(sandbox)
        with patch("asyncio.create_subprocess_exec") as create:
            assert await sandbox._ensure_venv() is None
        create.assert_not_called()
        assert sandbox._venv_path == str(bin_dir)

    def test_cleanup_removes_the_venv(self, tmp_path: Path):
        sandbox = _make_sandbox(tmp_path)
        _fake_venv(sandbox)
        sandbox.cleanup()
        assert not sandbox.venv_dir.exists()
        sandbox.cleanup()  # idempotent


# ---------------------------------------------------------------------------
# apply_and_test  (full integration-style, with mocks)
# ---------------------------------------------------------------------------


class TestApplyAndTest:
    """Tests for the main apply_and_test orchestration method."""

    @pytest.fixture()
    def sandbox(self, tmp_path: Path) -> LocalSandbox:
        return _without_venv(
            _make_sandbox(
                tmp_path,
                test_commands=["pytest -v"],
                install_commands=["pip install -e ."],
            )
        )

    async def test_files_staged_for_the_run_then_removed(
        self, sandbox: LocalSandbox, tmp_path: Path
    ):
        """Patch files are on disk while the tests run; files the run added are gone after."""
        seen: dict[str, bytes] = {}

        async def _run(cmd: str) -> tuple[int, str, str]:
            for rel in ("hello.txt", "sub/dir/nested.py"):
                seen[rel] = (tmp_path / rel).read_bytes()
            return 0, "1 passed", ""

        sandbox._run = AsyncMock(side_effect=_run)  # type: ignore[method-assign]

        result = await sandbox.apply_and_test(
            {"files": {"hello.txt": "Hello, world!", "sub/dir/nested.py": "print('nested')"}}
        )

        assert result["passed"] is True
        assert seen == {"hello.txt": b"Hello, world!", "sub/dir/nested.py": b"print('nested')"}
        assert sorted(p.name for p in tmp_path.iterdir()) == []

    async def test_writes_are_utf8_byte_exact(self, sandbox: LocalSandbox, tmp_path: Path):
        files = {"run.sh": "#!/bin/bash\necho hi\n", "p.py": "print('Pokémon ✓ —')\r\n"}
        seen: dict[str, bytes] = {}

        async def _run(cmd: str) -> tuple[int, str, str]:
            seen.update({rel: (tmp_path / rel).read_bytes() for rel in files})
            return 0, "", ""

        sandbox._run = AsyncMock(side_effect=_run)  # type: ignore[method-assign]
        await sandbox.apply_and_test({"files": files})
        assert seen == {rel: content.encode("utf-8") for rel, content in files.items()}

    async def test_existing_files_restored(self, sandbox: LocalSandbox, tmp_path: Path):
        (tmp_path / "keep.py").write_bytes(b"original\n")
        (tmp_path / "gone.py").write_bytes(b"to delete\n")
        seen: dict[str, Any] = {}

        async def _run(cmd: str) -> tuple[int, str, str]:
            seen["keep"] = (tmp_path / "keep.py").read_bytes()
            seen["gone"] = (tmp_path / "gone.py").exists()
            return 0, "", ""

        sandbox._run = AsyncMock(side_effect=_run)  # type: ignore[method-assign]
        await sandbox.apply_and_test(
            {"files": {"keep.py": "patched\n"}, "delete_files": ["gone.py", "missing.py"]}
        )
        assert seen == {"keep": b"patched\n", "gone": False}
        assert (tmp_path / "keep.py").read_bytes() == b"original\n"
        assert (tmp_path / "gone.py").read_bytes() == b"to delete\n"
        assert not (tmp_path / "missing.py").exists()

    async def test_smoke_test_from_ensure_tests_removed(self, tmp_path: Path):
        """The synthetic smoke test never stays in the user's repo (or reaches a commit)."""
        sandbox = _without_venv(_make_sandbox(tmp_path))
        (tmp_path / "game.py").write_bytes(b"x = 1\n")  # applied by the orchestrator
        calls = _recording_run(sandbox, {})
        patch_data = _smoke_tested({"game.py": "x = 1\n"})
        assert "tests/test_smoke.py" in patch_data["files"]

        await sandbox.apply_and_test(patch_data)

        assert calls  # tests ran
        assert sorted(p.name for p in tmp_path.iterdir()) == ["game.py"]

    async def test_user_file_at_smoke_test_path_untouched(self, tmp_path: Path):
        sandbox = _without_venv(_make_sandbox(tmp_path))
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_smoke.py").write_bytes(b"def test_user():\n    pass\n")
        _recording_run(sandbox, {})

        await sandbox.apply_and_test(_smoke_tested({"game.py": "x = 1\n"}))

        assert (tmp_path / "tests" / "test_smoke.py").read_bytes() == (
            b"def test_user():\n    pass\n"
        )

    async def test_unsafe_paths_never_written(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        sandbox = _without_venv(_make_sandbox(ws))
        seen: list[bool] = []

        async def _run(cmd: str) -> tuple[int, str, str]:
            seen.append((ws / "b.py").exists())
            return 0, "", ""

        sandbox._run = AsyncMock(side_effect=_run)  # type: ignore[method-assign]

        result = await sandbox.apply_and_test(
            {
                "files": {
                    "a.py": "",
                    "../escaped.txt": "x",
                    "/abs.py": "",
                    "C:/x.py": "",
                    "b.py": "",
                },
                "delete_files": ["../../etc/passwd"],
            }
        )

        assert sorted(p.name for p in tmp_path.iterdir()) == ["ws"]
        assert seen == [True]  # valid paths after the bad one were still staged
        assert result["stderr"].startswith(
            "Skipped unsafe patch path(s): ../escaped.txt, /abs.py, C:/x.py, ../../etc/passwd\n"
        )

    async def test_files_are_encoded_as_the_workspace_writes_them(self, tmp_path: Path):
        # #117: a lone surrogate has no UTF-8 form (that file only is skipped);
        # a surrogateescape character is written as its raw byte, as the
        # workspace does.  Before, the first one aborted the whole staging.
        ws = tmp_path / "ws"
        ws.mkdir()
        sandbox = _without_venv(_make_sandbox(ws))
        staged: dict[str, bytes] = {}

        async def _run(cmd: str) -> tuple[int, str, str]:
            staged.update((p.name, p.read_bytes()) for p in ws.iterdir() if p.is_file())
            return 0, "", ""

        sandbox._run = AsyncMock(side_effect=_run)  # type: ignore[method-assign]

        result = await sandbox.apply_and_test(
            {"files": {"bad.py": "x = '\ud83c'\n", "raw.txt": "a\udcffb", "ok.py": "y = 1\n"}}
        )

        assert staged == {"raw.txt": b"a\xffb", "ok.py": b"y = 1\n"}
        assert result["stderr"].startswith("Skipped unsafe patch path(s): bad.py\n")

    async def test_unified_diff_applied_and_reverted(self, sandbox: LocalSandbox):
        """When patch has diff_unified but no files, the diff is applied, then reverted."""
        patch_data = {"diff_unified": "--- a/foo.py\n+++ b/foo.py\n"}
        _recording_run(sandbox, {})
        sandbox._apply_diff = AsyncMock(return_value=None)  # type: ignore[method-assign]

        result = await sandbox.apply_and_test(patch_data)

        assert sandbox._apply_diff.await_args_list[0].args == (patch_data["diff_unified"],)
        assert sandbox._apply_diff.await_args_list[1].kwargs == {"reverse": True}
        assert result["passed"] is True

    async def test_failed_diff_stops_the_run(self, sandbox: LocalSandbox):
        calls = _recording_run(sandbox, {})
        sandbox._apply_diff = AsyncMock(  # type: ignore[method-assign]
            return_value="Patch failed to apply (exit 1): git apply --allow-empty -\nerror"
        )

        result = await sandbox.apply_and_test({"diff_unified": "--- a/x\n+++ b/x\n"})

        assert result["passed"] is False
        assert result["stderr"].startswith("Patch failed to apply")
        assert calls == []
        sandbox._apply_diff.assert_awaited_once()  # nothing to revert

    async def test_greenfield_requirements_installed(self, tmp_path: Path):
        sandbox = _without_venv(
            _make_sandbox(
                tmp_path,
                test_commands=["python -m pytest -v"],
                install_commands=["pip install pytest -q"],
            )
        )
        calls = _recording_run(sandbox, {})

        await sandbox.apply_and_test(
            {"files": {"app.py": "import requests\n", "requirements.txt": "requests\n"}}
        )

        assert calls == [
            "pip install -r requirements.txt -q",
            "pip install pytest -q",
            "python -m pytest -v",
        ]

    async def test_install_failure_stops_before_tests(self, sandbox: LocalSandbox):
        calls = _recording_run(
            sandbox,
            {"pip install": (1, "Obtaining file:///ws", "ERROR: pyproject.toml is invalid")},
        )

        result = await sandbox.apply_and_test({"files": {"a.py": "pass"}})

        assert calls == ["pip install -e ."]
        assert result["passed"] is False
        assert result["exit_code"] == 1
        assert result["stderr"].startswith("Install failed (exit 1): pip install -e .")
        assert "ERROR: pyproject.toml is invalid" in result["stderr"]
        assert result["sandbox"] == "local"

    async def test_factory_install_commands_not_sanitized(self, tmp_path: Path):
        prewarm = "mkdir -p build && echo ok > build/marker || true; echo done"
        sandbox = _without_venv(_make_sandbox(tmp_path, install_commands=[prewarm]))
        calls = _recording_run(sandbox, {})
        await sandbox.apply_and_test({})
        assert calls[0] == prewarm

    async def test_patch_test_commands_honoured(self, sandbox: LocalSandbox):
        calls = _recording_run(sandbox, {})
        await sandbox.apply_and_test({"test_commands": ["python -m pytest tests/test_a.py"]})
        assert calls == ["pip install -e .", "python -m pytest tests/test_a.py"]

    async def test_unsafe_patch_test_command_blocked(self, sandbox: LocalSandbox):
        calls = _recording_run(sandbox, {})
        result = await sandbox.apply_and_test({"test_commands": ["pytest; rm -rf ~"]})
        assert result["passed"] is False
        assert "Blocked unsafe command" in result["stderr"]
        assert not any("rm -rf" in cmd for cmd in calls)

    async def test_targeted_tests_run_before_full_suite(self, tmp_path: Path):
        sandbox = _without_venv(
            _make_sandbox(
                tmp_path,
                test_commands=["python -m pytest -v"],
                install_commands=["pip install pytest -q"],
            )
        )
        calls = _recording_run(
            sandbox,
            {"tests/test_deck.py": (1, "FAILED tests/test_deck.py::test_deal - assert 0\n", "")},
        )

        result = await sandbox.apply_and_test(
            {"targeted_tests": ["pytest tests/test_deck.py", "python deal.py", "pytest a; rm b"]}
        )

        assert calls == [
            "pip install pytest -q",
            # in the configured runner's form, so it imports like the full run
            "python -m pytest tests/test_deck.py",
            "python -m pytest -v",
        ]
        assert result["targeted"] == {
            "passed": False,
            "failing_ids": ["tests/test_deck.py::test_deal"],
            "stdout_tail": "FAILED tests/test_deck.py::test_deal - assert 0\n",
        }
        assert result["passed"] is True

    async def test_test_failure_reported(self, sandbox: LocalSandbox):
        """When a test command exits non-zero, passed=False."""
        _recording_run(sandbox, {"pytest": (1, "1 passed, 2 failed", "ERRORS")})

        result = await sandbox.apply_and_test({"files": {"a.py": "pass"}})
        assert result["passed"] is False
        assert result["exit_code"] == 1
        assert result["failures"] == 2
        assert result["total"] == 3
        assert result["targeted"] is None  # schema field: no targeted run

    async def test_stdout_stderr_truncated(self, sandbox: LocalSandbox):
        """stdout is truncated to last 5000 chars, stderr to last 2000."""
        _recording_run(sandbox, {"pytest": (0, "x" * 10000, "y" * 5000)})

        result = await sandbox.apply_and_test({"files": {}})
        assert len(result["stdout"]) == 5000
        assert len(result["stderr"]) == 2000
        assert result["output_truncated"] is True

    async def test_long_jest_failure_on_stderr_is_in_the_excerpt(self, sandbox: LocalSandbox):
        jest = "FAIL tests/deck.test.js\n" + "  expect(received).toBe(expected)\n" * 85
        _recording_run(sandbox, {"pytest": (1, "", jest)})

        result = await sandbox.apply_and_test({"files": {}})

        assert len(jest) > 2000
        assert len(result["stderr"]) == 2000
        assert jest.strip() in result["failure_excerpt"]

    async def test_result_has_sandbox_field(self, sandbox: LocalSandbox):
        """Result dict always includes sandbox='local'."""
        _recording_run(sandbox, {})
        result = await sandbox.apply_and_test({"files": {}})
        assert result["sandbox"] == "local"

    async def test_multiple_test_commands(self, tmp_path: Path):
        """All test commands run; failure in any marks as failed."""
        s = _without_venv(_make_sandbox(tmp_path, test_commands=["pytest", "mypy ."]))
        _recording_run(s, {"pytest": (0, "5 passed", ""), "mypy": (1, "", "mypy error")})

        result = await s.apply_and_test({"files": {}})
        assert result["passed"] is False
        assert result["exit_code"] == 1

    async def test_empty_patch_no_files_no_diff(self, sandbox: LocalSandbox):
        """An empty patch still runs install and test commands."""
        calls = _recording_run(sandbox, {})
        result = await sandbox.apply_and_test({})
        assert result["passed"] is True
        assert calls == ["pip install -e .", "pytest -v"]


class TestEnvironment:
    async def test_workspace_first_on_pythonpath_and_no_bytecode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("PYTHONPATH", "/opt/other")
        sandbox = _make_sandbox(tmp_path)
        with patch("asyncio.create_subprocess_shell", return_value=_proc()) as shell:
            await sandbox._run("pytest")

        env = shell.call_args.kwargs["env"]
        root = str(tmp_path.resolve())
        assert env["PYTHONPATH"] == root + os.pathsep + "/opt/other"
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"

    def test_pythonpath_without_an_existing_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("PYTHONPATH", raising=False)
        assert _make_sandbox(tmp_path)._env()["PYTHONPATH"] == str(tmp_path.resolve())

    async def test_bare_pytest_imports_root_modules(self, tmp_path: Path):
        """A bare ``pytest`` (no ``python -m``) finds root-level modules, as in Docker."""
        (tmp_path / "card_deck.py").write_text("SIZE = 52\n", encoding="utf-8")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_deck.py").write_text(
            "from card_deck import SIZE\n\n\ndef test_size():\n    assert SIZE == 52\n",
            encoding="utf-8",
        )
        pytest_script = shutil.which("pytest")
        if pytest_script is None:
            pytest.skip("needs the pytest console script")
        sandbox = _without_venv(_make_sandbox(tmp_path))
        rc, stdout, _ = await sandbox._run(f'"{pytest_script}" -q -p no:cacheprovider tests')
        assert rc == 0, stdout
        assert not list(tmp_path.rglob("__pycache__"))


class TestStagingSafety:
    @pytest.mark.parametrize(
        "path",
        [".git/config", ".GIT/index", ".git./HEAD", "sub/.git/config", "git~1/config"],
    )
    async def test_git_paths_never_staged_or_deleted(self, tmp_path: Path, path: str):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_bytes(b"[core]\n")
        (tmp_path / ".git" / "index").write_bytes(b"idx")
        (tmp_path / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
        sandbox = _without_venv(_make_sandbox(tmp_path))
        seen: list[bool] = []

        async def _run(cmd: str) -> tuple[int, str, str]:
            seen.append((tmp_path / ".git" / "config").read_bytes() == b"[core]\n")
            return 0, "1 passed", ""

        sandbox._run = AsyncMock(side_effect=_run)  # type: ignore[method-assign]

        result = await sandbox.apply_and_test({"files": {path: "x"}, "delete_files": [path]})

        assert seen and all(seen)
        assert (tmp_path / ".git" / "index").read_bytes() == b"idx"
        assert (tmp_path / ".git" / "HEAD").read_bytes() == b"ref: refs/heads/main\n"
        assert result["stderr"].startswith("Skipped unsafe patch path(s): ")

    def test_symlink_into_git_is_refused(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        try:
            os.symlink(tmp_path / ".git", tmp_path / "meta", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available")
        assert _make_sandbox(tmp_path)._resolve("meta/config") is None
        assert _make_sandbox(tmp_path)._resolve("src/app.py") is not None

    async def test_a_path_through_a_symlink_out_of_the_workspace_is_refused(self, tmp_path: Path):
        ws = tmp_path / "ws"
        outside = tmp_path / "outside"
        ws.mkdir()
        outside.mkdir()
        try:
            os.symlink(outside, ws / "out", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available")
        sandbox = _without_venv(_make_sandbox(ws))
        _recording_run(sandbox, {})

        assert sandbox._resolve("out/new.py") is None
        result = await sandbox.apply_and_test({"files": {"out/new.py": "x\n", "a.py": "a\n"}})

        assert result["stderr"].startswith("Skipped unsafe patch path(s): out/new.py\n")
        assert list(outside.iterdir()) == []

    async def test_non_text_content_is_skipped(self, tmp_path: Path):
        sandbox = _without_venv(_make_sandbox(tmp_path))
        _recording_run(sandbox, {})
        result = await sandbox.apply_and_test({"files": {"a.py": None, "b.py": "x = 1\n"}})
        assert result["stderr"].startswith("Skipped unsafe patch path(s): a.py\n")


class TestCommandDerivation:
    async def test_language_hint_keeps_the_python_toolchain(self, tmp_path: Path):
        """#93/#118: a Python repo whose patch only touches its tooling package.json."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
        (tmp_path / "card.py").write_text("X = 1\n", encoding="utf-8")
        sandbox = _without_venv(
            LocalSandbox(
                workspace_root=tmp_path,
                test_commands=["python -m pytest -v"],
                install_commands=["pip install -e . -q", "pip install pytest -q"],
                language="python",
            )
        )
        calls = _recording_run(sandbox, {})
        await sandbox.apply_and_test({"files": {"package.json": '{"devDependencies": {}}'}})
        assert calls == ["pip install -e . -q", "pip install pytest -q", "python -m pytest -v"]

    async def test_detected_project_keeps_its_configured_commands(self, tmp_path: Path):
        """#118: an existing Maven project keeps the factory's install and test commands."""
        (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
        install = ["mvn -B -q dependency:go-offline"]
        sandbox = _without_venv(
            LocalSandbox(
                workspace_root=tmp_path,
                test_commands=["mvn test"],
                install_commands=install,
                language="java",
            )
        )
        calls = _recording_run(sandbox, {})
        await sandbox.apply_and_test({"files": {"src/App.java": "class App {}"}})
        assert calls == [*install, "mvn test"]

    async def test_container_only_installs_never_run_on_the_host(self, tmp_path: Path):
        from delegatron.sandbox.toolchains import (
            CMAKE_TOOLCHAIN_INSTALL,
            PESTER_INSTALL,
            PESTER_INSTALL_CURRENT_USER,
        )

        sandbox = _without_venv(
            LocalSandbox(
                workspace_root=tmp_path,
                test_commands=["make test"],
                install_commands=[CMAKE_TOOLCHAIN_INSTALL, PESTER_INSTALL],
                language="cpp",
            )
        )
        calls = _recording_run(sandbox, {})
        await sandbox.apply_and_test({})
        assert calls == [PESTER_INSTALL_CURRENT_USER, "make test"]
        assert not any("apt-get" in call or "AllUsers" in call for call in calls)


class TestCounts:
    """LocalSandbox uses the shared count parser (sandbox/counts.py)."""

    @pytest.mark.parametrize(
        ("output", "counts"),
        [
            ("=== 1 error in 0.12s ===", (1, 1)),
            (
                "ERROR tests/test_x.py\n"
                "!!!!!! Interrupted: 1 error during collection !!!!!!\n"
                "======= 1 error in 0.12s =======\n",
                (1, 1),
            ),
            ("  3 passing (12ms)\n  1 failing\n", (4, 1)),
            ("  3 passing (12ms)\n", (3, 0)),
            ("--- PASS: TestA (0.00s)\n--- FAIL: TestB (0.00s)\nFAIL\tpkg\t0.1s\n", (2, 1)),
            ("ok  \tpkg\t0.01s\n", (1, 0)),
        ],
    )
    def test_runner_output(self, output: str, counts: tuple[int, int]):
        assert LocalSandbox._parse_test_counts(output) == counts


# ---------------------------------------------------------------------------
# Cancellation (the orchestrator's wall-time deadline)
# ---------------------------------------------------------------------------

# Starts a grandchild that writes a marker after _GRANDCHILD_SLEEP seconds, then waits for it.
_GRANDCHILD_SLEEP = 2.0
_TREE_SCRIPT = f"""\
import pathlib, subprocess, sys
started, late = sys.argv[1], sys.argv[2]
code = (
    "import pathlib, sys, time; "
    "time.sleep({_GRANDCHILD_SLEEP}); "
    "pathlib.Path(sys.argv[1]).write_text('x')"
)
child = subprocess.Popen([sys.executable, "-c", code, late])
pathlib.Path(started).write_text("started")
child.wait()
"""


class TestCancellation:
    """A cancelled run kills the whole test process tree before it returns."""

    @pytest.fixture()
    def tree(self, tmp_path_factory: pytest.TempPathFactory) -> tuple[list[str], Path, Path]:
        """argv of the tree script, its 'started' marker and the grandchild's 'late' marker."""
        scratch = tmp_path_factory.mktemp("tree")
        script = scratch / "tree.py"
        script.write_text(_TREE_SCRIPT, encoding="utf-8")
        started, late = scratch / "started", scratch / "late"
        return [sys.executable, str(script), str(started), str(late)], started, late

    @staticmethod
    async def _wait_started(started: Path, task: asyncio.Future[Any] | None = None) -> float:
        """Wait for the tree to start; return the loop time it was seen."""
        loop = asyncio.get_running_loop()
        for _ in range(400):
            if started.exists() or (task is not None and task.done()):
                break
            await asyncio.sleep(0.05)
        assert started.exists(), "the test process never started"
        return loop.time()

    @staticmethod
    async def _assert_never_late(late: Path, seen_at: float) -> None:
        """The grandchild, started before *seen_at*, would have written *late* by now."""
        loop = asyncio.get_running_loop()
        await asyncio.sleep(max(seen_at + _GRANDCHILD_SLEEP + 1.0 - loop.time(), 0))
        assert not late.exists(), "the test process tree outlived the cancelled run"

    async def _cancel_once_started(self, task: asyncio.Task[Any], started: Path) -> float:
        seen_at = await self._wait_started(started, task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return seen_at

    async def test_cancelled_test_run_kills_the_tree_then_unstages(
        self, tmp_path: Path, tree: tuple[list[str], Path, Path]
    ):
        argv, started, late = tree
        command = " ".join(f'"{part}"' for part in argv)
        sandbox = _without_venv(_make_sandbox(tmp_path, test_commands=[command]))
        task = asyncio.create_task(sandbox.apply_and_test({"files": {"card_deck.py": "x = 1\n"}}))

        seen_at = await self._cancel_once_started(task, started)

        assert not (tmp_path / "card_deck.py").exists()
        await self._assert_never_late(late, seen_at)

    async def test_cancelled_exec_kills_the_tree(
        self, tmp_path: Path, tree: tuple[list[str], Path, Path]
    ):
        # Flaky-test reruns and entrypoint smoke runs go through _exec.
        argv, started, late = tree
        sandbox = _make_sandbox(tmp_path)
        task = asyncio.create_task(sandbox._exec(argv, timeout=60))

        seen_at = await self._cancel_once_started(task, started)

        await self._assert_never_late(late, seen_at)

    async def test_wait_for_deadline_kills_the_tree(
        self, tmp_path: Path, tree: tuple[list[str], Path, Path], monkeypatch: pytest.MonkeyPatch
    ):
        # How the orchestrator enforces its wall-time deadline: asyncio.wait_for
        # with the time left, here around _spawn (venv creation, git apply).
        argv, started, late = tree
        sandbox = _make_sandbox(tmp_path)
        killed: list[int] = []
        real_kill = local_runner._kill_tree

        async def _recording_kill(proc: Any) -> None:
            killed.append(proc.pid)
            await real_kill(proc)

        monkeypatch.setattr(local_runner, "_kill_tree", _recording_kill)
        spawn = asyncio.ensure_future(asyncio.wait_for(sandbox._spawn(argv), timeout=60))
        seen_at = await self._wait_started(started, spawn)
        spawn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await spawn

        assert len(killed) == 1
        await self._assert_never_late(late, seen_at)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    """Verify module-level constants are sensible."""

    def test_cmd_timeout_is_positive(self):
        assert _CMD_TIMEOUT > 0

    def test_cmd_timeout_is_300(self):
        assert _CMD_TIMEOUT == 300
