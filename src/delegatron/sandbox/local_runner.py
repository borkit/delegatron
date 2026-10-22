"""Local (no-container) sandbox — runs code directly on the host.

Use when the execution environment is already isolated (e.g. a disposable VM)
or when Docker is not available. Set ``DELEGATRON_SANDBOX=none`` to activate.

Tests run inside the workspace with a per-workspace venv kept under the
system temp dir, never inside the workspace, so it is never committed.  The
patch is staged into the workspace only for the run: files it adds are
removed and files it changes or deletes are put back afterwards, so the
workspace, which the orchestrator owns, is left as it was found.  Python
writes no bytecode there (``PYTHONDONTWRITEBYTECODE``), and the workspace is
first on ``PYTHONPATH`` as in the Docker sandbox.

Generated scripts (``entrypoints``) are smoke-run only when the sandbox is
created with ``run_entrypoints=True``: here they would run on the host.
Every child process runs in its own process group; on a timeout, or when the
run is cancelled (the orchestrator's wall-time deadline), the whole tree is
killed before the staged files are put back.

.. warning::

    This sandbox provides NO filesystem or network isolation.  Only use it
    in environments you consider disposable (test VMs, CI runners, etc.).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import shlex
import shutil
import signal
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from delegatron.sandbox.base import (
    ENTRYPOINT_TIMEOUT,
    BaseSandbox,
    RerunPlan,
    RerunResult,
    UnsafeCommandError,
    entrypoint_paths,
    entrypoint_run,
    flaky_tolerated,
    mark_flaky_tolerated,
    per_test_fields,
    plan_rerun,
    pytest_rerun_args,
    report_extras,
    rerun_outcome,
    sanitize_shell_command,
)
from delegatron.sandbox.counts import parse_test_counts
from delegatron.sandbox.prepare import (
    derive_commands,
    rejected_paths_note,
    select_targeted_tests,
    step_failure,
    targeted_report,
)
from delegatron.schemas.test_report import EntrypointRun
from delegatron.schemas.test_report import TestReport as SandboxTestReport
from delegatron.utils.paths import normalize_rel_path
from delegatron.utils.test_output import parse_pytest_outcomes

logger = logging.getLogger(__name__)

# Default timeout for each command (seconds)
_CMD_TIMEOUT = 300

# Seconds to wait for a killed (or finished) program's output pipes to close
_DRAIN_GRACE = 5

# Characters of test output kept in reports
_STDOUT_TAIL = 5000
_STDERR_TAIL = 2000

# Per-workspace venvs live here, outside every workspace.
_VENV_ROOT = Path(tempfile.gettempdir()) / "delegatron-venvs"

# Children start a new session (POSIX), so their whole process group can be killed.
_NEW_SESSION: dict[str, Any] = {} if os.name == "nt" else {"start_new_session": True}


@dataclass
class _Undo:
    """What a run changed in the workspace, so it can be put back."""

    files: dict[Path, bytes | None] = field(default_factory=dict)  # None: did not exist
    dirs: list[Path] = field(default_factory=list)  # created, outermost first

    def restore(self) -> None:
        for path, data in self.files.items():
            try:
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(data)
            except OSError as exc:
                logger.warning("Could not restore %s after the local test run: %s", path, exc)
        for directory in reversed(self.dirs):
            with contextlib.suppress(OSError):
                directory.rmdir()


class LocalSandbox(BaseSandbox):
    """Execute code directly on the host without container isolation."""

    def __init__(
        self,
        workspace_root: Path,
        test_commands: list[str],
        install_commands: list[str],
        *,
        language: str = "",
        run_entrypoints: bool = False,
    ) -> None:
        self.workspace_root = workspace_root
        self.test_commands = test_commands
        self.install_commands = install_commands
        # Detected project language the commands were built for ("" when none
        # was detected: they are then the Python fallback).
        self.language = language
        # Smoke-run generated scripts on this host (an explicit opt-in).
        self.run_entrypoints = run_entrypoints
        # The venv's bin/Scripts dir, put first on PATH once the venv exists.
        self._venv_path: str | None = None

    @property
    def venv_dir(self) -> Path:
        """The per-workspace venv, reused across iterations."""
        digest = hashlib.sha256(str(self.workspace_root.resolve()).encode("utf-8")).hexdigest()
        return _VENV_ROOT / digest[:16]

    def cleanup(self) -> None:
        """Remove the per-workspace venv."""
        self._venv_path = None
        shutil.rmtree(self.venv_dir, ignore_errors=True)

    async def apply_and_test(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Stage the patch in the workspace, install, run tests, then undo the staging.

        Besides ``files`` / ``delete_files`` / ``diff_unified``, the patch may
        set ``test_commands``, ``targeted_tests`` (test-runner commands for the
        current task, run before the test commands and reported as
        ``targeted``), ``flaky_rerun`` / ``flaky_reruns``,
        ``tolerate_preexisting_flaky`` with ``run_files``, and
        ``entrypoints`` (run only with ``run_entrypoints``), as for
        :class:`~delegatron.sandbox.docker_runner.DockerSandbox`.  A failed
        venv creation, ``git apply`` or install stops the run with
        ``passed=False`` before any test runs.
        """
        start_time = time.monotonic()
        venv_error = await self._ensure_venv()
        if venv_error:
            return _failure(venv_error, start_time)

        undo = _Undo()
        diff = patch.get("diff_unified", "")
        diff_applied = False
        try:
            note = rejected_paths_note(self._stage(patch, undo))
            if diff and not patch.get("files"):
                diff_error = await self._apply_diff(diff)
                if diff_error:
                    return _failure(note + diff_error, start_time)
                diff_applied = True
            return await self._install_and_test(patch, start_time, note)
        except UnsafeCommandError as exc:
            return _failure(str(exc), start_time)
        finally:
            if diff_applied and await self._apply_diff(diff, reverse=True):
                logger.warning("Could not revert the diff applied for the local test run")
            undo.restore()

    async def _install_and_test(
        self,
        patch: dict[str, Any],
        start_time: float,
        note: str,
    ) -> dict[str, Any]:
        """Run install commands, targeted tests and test commands on the staged workspace."""
        install_cmds, test_cmds = derive_commands(
            self.install_commands,
            self.test_commands,
            self.workspace_root,
            patch.get("files") or {},
            language=self.language,
        )
        if patch.get("test_commands"):
            test_cmds = [sanitize_shell_command(cmd) for cmd in patch["test_commands"]]

        for cmd in install_cmds:
            logger.info("Local install: %s", cmd)
            rc, stdout, stderr = await self._run(cmd)
            if rc != 0:
                message = step_failure("Install failed", cmd, rc, stdout, stderr)
                return _failure(note + message, start_time, stdout=stdout, exit_code=rc)

        targeted = await self._run_targeted(patch, [*self.test_commands, *test_cmds])

        test_stdout = ""
        test_stderr = ""
        test_passed = True
        test_rc = 0
        for cmd in test_cmds:
            logger.info("Local test: %s", cmd)
            rc, stdout, stderr = await self._run(cmd)
            test_stdout += stdout
            test_stderr += stderr
            if rc != 0:
                test_passed = False
                test_rc = rc

        # Parse test results
        output = test_stdout + "\n" + test_stderr
        total, failures = self._parse_test_counts(output)
        fields = per_test_fields(output, test_passed)

        plan = plan_rerun(patch, test_cmds, test_passed, fields["failed_tests"])
        reruns = await self._rerun_flaky(plan)
        entrypoint_runs = [await self._run_entrypoint(file) for file in self._entrypoints(patch)]

        report = SandboxTestReport(
            passed=test_passed,
            total=total,
            failures=failures,
            stdout=test_stdout[-_STDOUT_TAIL:],
            stderr=note + test_stderr[-_STDERR_TAIL:],
            duration_ms=(time.monotonic() - start_time) * 1000,
            flaky_tests=reruns.flaky,
            entrypoint_runs=entrypoint_runs,
            **fields,
        ).model_dump()
        report.update(report_extras(test_stdout, test_stderr, _STDOUT_TAIL, _STDERR_TAIL))
        report["rerun_attempts"] = reruns.attempts
        report["rerun_skipped"] = plan.skipped
        if not test_passed and flaky_tolerated(
            patch,
            fields["failed_tests"],
            reruns.flaky,
            outcomes=parse_pytest_outcomes(output),
            failures=failures,
        ):
            logger.info("Local: every failure was a pre-existing flaky test — tolerating it")
            mark_flaky_tolerated(report)
        result = {**report, "exit_code": test_rc, "sandbox": "local"}
        if targeted is not None:
            result["targeted"] = targeted
        return result

    def _entrypoints(self, patch: dict[str, Any]) -> list[str]:
        """The patch's scripts to smoke-run: none unless this sandbox opted in."""
        if not self.run_entrypoints:
            if patch.get("entrypoints"):
                logger.info(
                    "Local: not smoke-running entrypoints on the host "
                    "(the local sandbox runs them only with run_entrypoints enabled)"
                )
            return []
        return entrypoint_paths(patch, self.workspace_root)

    async def _ensure_venv(self) -> str | None:
        """Create the per-workspace venv if needed and put it first on PATH.

        Returns an error message when the venv cannot be created; PATH is
        then left unchanged and whatever the failed creation left behind is
        removed, so the next run tries again instead of using a broken venv.
        """
        venv_dir = self.venv_dir
        bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        if not python.exists():
            logger.info("Creating per-workspace venv at %s", venv_dir)
            venv_dir.parent.mkdir(parents=True, exist_ok=True)
            args = [sys.executable, "-m", "venv", str(venv_dir)]
            rc, stdout, stderr = await self._spawn(args)
            if rc != 0 or not python.exists():
                shutil.rmtree(venv_dir, ignore_errors=True)
                return step_failure("Venv creation failed", shlex.join(args), rc, stdout, stderr)
        self._venv_path = str(bin_dir)
        return None

    def _stage(self, patch: dict[str, Any], undo: _Undo) -> list[str]:
        """Write the patch's files and deletions into the workspace, recording them in *undo*.

        Every path is checked before anything is written; unsafe ones are
        skipped and returned.
        """
        changes: list[tuple[Path, bytes | None]] = []
        rejected: list[str] = []
        for raw_path, content in (patch.get("files") or {}).items():
            target = self._resolve(raw_path)
            data = _utf8(content) if isinstance(content, str) else None
            if target is None or data is None:
                rejected.append(raw_path)
            else:
                changes.append((target, data))
        for raw_path in patch.get("delete_files") or []:
            target = self._resolve(raw_path)
            if target is None:
                rejected.append(raw_path)
            else:
                changes.append((target, None))
        if rejected:
            logger.warning("Skipped unsafe patch path(s): %s", ", ".join(rejected))

        for target, data in changes:
            current = target.read_bytes() if target.is_file() else None
            if current == data:
                continue
            undo.files.setdefault(target, current)
            if data is None:
                target.unlink()
                continue
            missing = [d for d in target.parents if not d.exists()]
            target.parent.mkdir(parents=True, exist_ok=True)
            undo.dirs.extend(reversed(missing))
            target.write_bytes(data)
        return rejected

    def _resolve(self, rel_path: str) -> Path | None:
        """Workspace path for a patch path, or ``None`` when it is unsafe.

        Absolute, drive, UNC, ``..`` and ``.git`` paths (in any spelling) are
        rejected before the disk is touched; then paths that leave the
        workspace or reach into ``.git`` through a symlink, and paths naming
        a directory.  The workspace is the user's real repository, so its
        ``.git`` is never written or deleted, not even for one test run.
        """
        normalized = normalize_rel_path(rel_path)
        if normalized is None or _names_git_dir(normalized):
            return None
        target = self.workspace_root / normalized
        root = self.workspace_root.resolve()
        resolved = target.resolve()
        if not resolved.is_relative_to(root) or target.is_dir():
            return None
        if _names_git_dir(resolved.relative_to(root).as_posix()):
            return None
        return target

    async def _apply_diff(self, diff: str, *, reverse: bool = False) -> str | None:
        """Apply (or revert) a unified diff with ``git apply``; returns an error or ``None``.

        Line endings are left exactly as the diff and the files have them.
        """
        reverse_flag = ["--reverse"] if reverse else []
        args = ["git", "-c", "core.autocrlf=false", "apply", "--allow-empty", *reverse_flag, "-"]
        rc, stdout, stderr = await self._spawn(args, stdin=diff)
        if rc != 0:
            return step_failure("Patch failed to apply", shlex.join(args), rc, stdout, stderr)
        return None

    async def _run_targeted(
        self,
        patch: dict[str, Any],
        runner_commands: list[str],
    ) -> dict[str, Any] | None:
        """Run the patch's accepted ``targeted_tests``; ``None`` when there are none.

        An entry is accepted when it calls the runner of one of *runner_commands*
        (the configured and the detected test commands).
        """
        commands, _ = select_targeted_tests(patch.get("targeted_tests") or [], runner_commands)
        if not commands:
            return None
        passed = True
        output = ""
        for cmd in commands:
            logger.info("Local targeted test: %s", cmd)
            rc, stdout, stderr = await self._run(cmd)
            passed = passed and rc == 0
            output += stdout + stderr
        return targeted_report(passed, output)

    def _env(self) -> dict[str, str]:
        """Return the process environment for the workspace's commands.

        The per-job venv goes first on PATH (once it exists), the workspace
        first on ``PYTHONPATH`` (so a bare ``pytest`` imports root-level
        modules, as ``python -m pytest`` and the Docker sandbox do), and
        Python writes no bytecode into the user's repository.
        """
        env = os.environ.copy()
        if self._venv_path:
            env["PATH"] = self._venv_path + os.pathsep + env.get("PATH", "")
            env["VIRTUAL_ENV"] = str(self.venv_dir)
        root = str(self.workspace_root.resolve())
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = root + os.pathsep + existing if existing else root
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    async def _run(self, cmd: str) -> tuple[int, str, str]:
        """Run a command in the workspace directory, using per-job venv if available.

        On a timeout, or when the awaiting task is cancelled, the whole
        process tree is killed.
        """
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace_root,
                env=self._env(),
                **_NEW_SESSION,
            )
        except Exception as e:
            return 1, "", str(e)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_CMD_TIMEOUT)
        except TimeoutError:
            logger.warning("Command timed out: %s", cmd)
            await _kill_tree(proc)
            return 1, "", "Command timed out"
        except asyncio.CancelledError:
            await _kill_cancelled(proc, cmd)
            raise
        except Exception as e:
            return 1, "", str(e)
        return (
            proc.returncode or 0,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )

    async def _spawn(
        self, args: list[str], stdin: str | None = None
    ) -> tuple[int | None, str, str]:
        """Run *args* without a shell in the workspace, feeding *stdin* if given.

        Returns ``(returncode, stdout, stderr)``; ``returncode`` is ``None``
        when the process tree was killed after ``_CMD_TIMEOUT`` seconds.  A
        cancelled call kills the tree too.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace_root,
                env=self._env(),
                **_NEW_SESSION,
            )
        except OSError as exc:
            return 1, "", str(exc)
        data = None if stdin is None else stdin.encode("utf-8")
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(input=data), _CMD_TIMEOUT)
        except TimeoutError:
            logger.warning("Command timed out: %s", shlex.join(args))
            await _kill_tree(proc)
            return None, "", "Command timed out"
        except asyncio.CancelledError:
            await _kill_cancelled(proc, shlex.join(args))
            raise
        return proc.returncode, _decode(stdout), _decode(stderr)

    async def _exec(self, args: list[str], timeout: float) -> tuple[int | None, str, str]:
        """Run *args* without a shell and with empty stdin, in the per-job venv.

        The program is looked up on the venv-first PATH, as the shell would.
        Returns ``(returncode, stdout, stderr)``; ``returncode`` is ``None``
        when the process was killed after *timeout* seconds, and the output
        is then whatever it printed before that.  The program runs in its
        own process group: on a timeout its whole tree is killed, and output
        is collected for at most ``_DRAIN_GRACE`` more seconds, so a server
        or reloader it started (holding the output pipes) cannot hang the
        run.  On POSIX, processes it left running after it exited are killed
        too.  A cancelled call kills the tree and stops collecting output.
        """
        env = self._env()
        argv = [shutil.which(args[0], path=env.get("PATH")) or args[0], *args[1:]]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.workspace_root,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_NEW_SESSION,
            )
        except OSError as exc:
            return 1, "", str(exc)
        stdout, stderr = bytearray(), bytearray()
        assert proc.stdout is not None and proc.stderr is not None
        readers = [
            asyncio.create_task(_drain(proc.stdout, stdout)),
            asyncio.create_task(_drain(proc.stderr, stderr)),
        ]
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except TimeoutError:
            timed_out = True
            logger.warning("Command timed out after %ss: %s", timeout, " ".join(args))
            await _kill_tree(proc)
        except asyncio.CancelledError:
            await _kill_cancelled(proc, " ".join(args))
            for task in readers:
                task.cancel()
            raise
        _, pending = await asyncio.wait(readers, timeout=_DRAIN_GRACE)
        if pending and not timed_out and os.name != "nt":
            # The program exited but left processes holding its output pipes.
            _kill_group(proc.pid)
            _, pending = await asyncio.wait(pending, timeout=_DRAIN_GRACE)
        for task in pending:
            task.cancel()
        returncode = None if timed_out else proc.returncode
        return returncode, _decode(bytes(stdout)), _decode(bytes(stderr))

    async def _rerun_flaky(self, plan: RerunPlan) -> RerunResult:
        """Rerun the plan's failed tests, each time only those still failing.

        Up to ``plan.attempts`` reruns; stops early once none is still
        failing or a rerun proves nothing (timeout, usage error).  pytest is
        started the way the test command started it (``plan.prefix``).
        """
        flaky: set[str] = set()
        pending = list(plan.ids)
        attempts = 0
        while pending and attempts < plan.attempts:
            attempts += 1
            logger.info("Local: rerun %d of %d failed test(s)", attempts, len(pending))
            argv = pytest_rerun_args(pending, plan.prefix)
            rc, stdout, stderr = await self._exec(argv, _CMD_TIMEOUT)
            outcome = rerun_outcome(pending, rc, stdout + "\n" + stderr)
            if outcome is None:
                break
            flaky.update(outcome[0])
            pending = outcome[1]
        return RerunResult([nodeid for nodeid in plan.ids if nodeid in flaky], attempts)

    async def _run_entrypoint(self, file: str) -> EntrypointRun:
        """Smoke-run *file* with ``python`` (empty stdin, short timeout)."""
        logger.info("Local: smoke-running entrypoint %s", file)
        rc, stdout, stderr = await self._exec(["python", file], ENTRYPOINT_TIMEOUT)
        return entrypoint_run(file, rc, stdout, stderr, timed_out=rc is None)

    @staticmethod
    def _parse_test_counts(output: str) -> tuple[int, int]:
        """Test counts from the runner output (``counts.parse_test_counts``)."""
        return parse_test_counts(output)


def _failure(
    stderr: str,
    start_time: float,
    *,
    stdout: str = "",
    exit_code: int = 1,
) -> dict[str, Any]:
    """Report for a run that stopped before its tests ran (venv, patch or install)."""
    report = SandboxTestReport(
        passed=False,
        total=0,
        failures=0,
        stdout=stdout[-5000:],
        stderr=stderr,
        duration_ms=(time.monotonic() - start_time) * 1000,
    ).model_dump()
    return {**report, "exit_code": exit_code, "sandbox": "local"}


def _decode(data: bytes | None) -> str:
    """Decode captured process output (``None`` when nothing was captured)."""
    return data.decode(errors="replace") if data else ""


def _utf8(content: str) -> bytes | None:
    """*content* as the workspace writes it (UTF-8, surrogateescape); None when it cannot be."""
    try:
        return content.encode("utf-8", errors="surrogateescape")
    except UnicodeEncodeError:  # a lone surrogate has no UTF-8 form
        return None


def _names_git_dir(rel_path: str) -> bool:
    """Whether a workspace-relative posix path has a ``.git`` component (in any spelling)."""
    return any(part.casefold().rstrip(" .") in (".git", "git~1") for part in rel_path.split("/"))


async def _drain(stream: asyncio.StreamReader, sink: bytearray) -> None:
    """Read *stream* into *sink* until it closes."""
    while chunk := await stream.read(65536):
        sink.extend(chunk)


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill *proc* and every process it started (best effort), then reap it.

    POSIX: the whole process group (the child started a new session).
    Windows: ``taskkill /T`` while the process still runs, so its tree is known.
    """
    if os.name == "nt":
        if proc.returncode is None:
            with contextlib.suppress(OSError, TimeoutError):
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/F",
                    "/T",
                    "/PID",
                    str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), _DRAIN_GRACE)
    else:
        _kill_group(proc.pid)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), _DRAIN_GRACE)


async def _kill_cancelled(proc: asyncio.subprocess.Process, what: str) -> None:
    """Kill the tree of *proc*, whose awaiting task was cancelled, before the cancel goes on.

    Shielded, so a second cancellation cannot leave the tree running; the
    caller re-raises the :class:`asyncio.CancelledError`.
    """
    logger.warning("Local: run cancelled — killing %s", what)
    await asyncio.shield(_kill_tree(proc))


def _kill_group(pid: int) -> None:
    """SIGKILL the process group *pid* leads (POSIX; a no-op when it is gone)."""
    killpg = getattr(os, "killpg", None)
    sigkill = getattr(signal, "SIGKILL", None)
    if killpg is None or sigkill is None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        killpg(pid, sigkill)
