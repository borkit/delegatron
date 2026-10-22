"""E2B microVM sandbox runner.

Applies patches to an E2B Firecracker microVM, runs tests,
and captures results as a TestReport.
"""

from __future__ import annotations

import logging
import shlex
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from delegatron.sandbox.base import (
    ENTRYPOINT_TIMEOUT,
    BaseSandbox,
    UnsafeCommandError,
    entrypoint_paths,
    entrypoint_run,
    per_test_fields,
    report_extras,
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
from delegatron.sandbox.toolchains import PYTEST_COMMAND
from delegatron.schemas.test_report import EntrypointRun
from delegatron.schemas.test_report import TestReport as SandboxTestReport
from delegatron.utils.paths import internal_dirs_for, normalize_rel_path

logger = logging.getLogger(__name__)


def _upload_payload(content: str) -> str | bytes:
    """*content* as the workspace writes it: text, or its surrogateescape bytes.

    A lone surrogate has no UTF-8 form: ``UnicodeEncodeError`` (a
    ``ValueError``), and the file is skipped like an unsafe path.
    """
    try:
        content.encode("utf-8")
    except UnicodeEncodeError:
        return content.encode("utf-8", errors="surrogateescape")
    return content


# Patterns to ignore when uploading workspace files
_IGNORE_PATTERNS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache"}

# Default E2B template for Python projects
DEFAULT_TEMPLATE = "base"

# Timeout for sandbox operations (seconds)
SANDBOX_TIMEOUT = 300

# Characters of command output kept in reports
_OUTPUT_TAIL = 5000

_WORKSPACE = "/home/user/workspace"

# Every command runs with the workspace on the import path, as in the Docker
# sandbox, so a bare ``pytest`` imports root-level modules too.
_COMMAND_ENV = {"PYTHONPATH": _WORKSPACE}

# Exit status of coreutils ``timeout`` when the command ran out of time
_TIMEOUT_EXIT_CODE = 124

# Seconds the SDK waits for an entrypoint run, around its own ``timeout``
_ENTRYPOINT_SDK_TIMEOUT = ENTRYPOINT_TIMEOUT + 10


class _SDKMissing(Exception):
    """Stands in for ``CommandExitException`` when the e2b SDK is not installed."""


_AsyncSandbox: Any
_CommandExitException: type[Exception]
try:
    from e2b_code_interpreter import (  # type: ignore[no-redef, import-untyped, unused-ignore]
        AsyncSandbox as _AsyncSandbox,
    )
    from e2b_code_interpreter import (  # type: ignore[no-redef, import-untyped, unused-ignore]
        CommandExitException as _CommandExitException,
    )

    _E2B_AVAILABLE = True
except ImportError:  # pragma: no cover
    _AsyncSandbox = None
    _CommandExitException = _SDKMissing
    _E2B_AVAILABLE = False


def e2b_available() -> bool:
    """Return whether the e2b_code_interpreter package is installed."""
    return _E2B_AVAILABLE


class E2BSandbox(BaseSandbox):
    """Runs code in an E2B Firecracker microVM.

    Provides hardware-level isolation with ~150ms cold starts.
    Requires ``E2B_API_KEY`` and the e2b extra:
    ``pip install delegatron[e2b]``

    Lifecycle per test run:

    1. Create an E2B sandbox session
    2. Upload workspace files into ``/home/user/workspace``
    3. Apply patch (files and deletions, or unified diff)
    4. Run install commands, then targeted tests, then the test commands
    5. Smoke-run the patch's ``entrypoints``
    6. Capture stdout/stderr, parse test results
    7. Destroy the sandbox
    """

    def __init__(
        self,
        workspace_root: Path,
        test_commands: list[str] | None = None,
        install_commands: list[str] | None = None,
        template: str = DEFAULT_TEMPLATE,
        timeout: int = SANDBOX_TIMEOUT,
        *,
        language: str = "",
    ) -> None:
        self._workspace_root = workspace_root
        self._test_commands = test_commands or [PYTEST_COMMAND]
        self._install_commands = install_commands or []
        self._template = template
        self._timeout = timeout
        # Detected project language the commands were built for ("" when none
        # was detected: they are then the Python fallback).
        self._language = language

    async def apply_and_test(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply file changes and run tests in an E2B microVM.

        The *patch* dict should contain:

        - ``files``: dict of ``{path: content}`` for files to write/update
        - ``delete_files``: paths to remove
        - ``diff_unified``: optional unified diff (alternative to files)
        - ``test_commands``: optional override for test commands
        - ``targeted_tests``: test-runner commands for the current task, run
          after the install and before the test commands; reported as
          ``targeted``
        - ``entrypoints``: relative ``.py`` scripts to smoke-run after the
          tests (``timeout 10 python <file> </dev/null``)

        Install commands are re-derived from the workspace and patch on every
        run, so a manifest the patch adds is installed.
        """
        if not _E2B_AVAILABLE:
            return _failure(
                "e2b_code_interpreter is not installed. "
                "Install it with: pip install e2b-code-interpreter"
            )

        start_time = time.monotonic()
        sandbox = None

        try:
            # 1. Create sandbox session
            sandbox = await _AsyncSandbox.create(
                template=self._template,
                timeout=self._timeout,
            )
            logger.info("Created E2B sandbox: %s", sandbox.sandbox_id)

            # 2. Upload workspace files
            await self._upload_workspace(sandbox)

            # 3. Apply patch
            patch_error, rejected = await self._apply_patch(sandbox, patch)
            note = rejected_paths_note(rejected)
            if patch_error is not None:
                return _failure(note + patch_error, start_time)

            # 4. Install, targeted tests, tests
            install_cmds, test_cmds = derive_commands(
                self._install_commands,
                self._test_commands,
                self._workspace_root,
                patch.get("files") or {},
                language=self._language,
            )
            if patch.get("test_commands"):
                test_cmds = [sanitize_shell_command(cmd) for cmd in patch["test_commands"]]

            if install_cmds:
                install_script = " && ".join(install_cmds)
                logger.info("Installing in E2B sandbox: %s", install_script)
                installed = await self._run(sandbox, install_script)
                if installed.exit_code != 0:
                    message = step_failure(
                        "Install failed",
                        install_script,
                        installed.exit_code,
                        installed.stdout or "",
                        installed.stderr or "",
                    )
                    return _failure(note + message, start_time, stdout=installed.stdout or "")

            targeted_cmds, _ = select_targeted_tests(
                patch.get("targeted_tests") or [], [*self._test_commands, *test_cmds]
            )
            targeted = None
            if targeted_cmds:
                ran = await self._run(sandbox, " && ".join(targeted_cmds))
                targeted = targeted_report(
                    ran.exit_code == 0, (ran.stdout or "") + "\n" + (ran.stderr or "")
                )

            test_script = " && ".join(test_cmds)
            logger.info("Running tests in E2B sandbox: %s", test_script)
            result = await self._run(sandbox, test_script)

            stdout = result.stdout or ""
            stderr = result.stderr or ""
            passed = result.exit_code == 0

            # 5. Smoke-run the scripts (the result never changes ``passed``)
            entrypoint_runs = [
                await self._run_entrypoint(sandbox, file) for file in self._entrypoints(patch)
            ]

            # 6. Parse test results (check both stdout and stderr — Jest writes to stderr)
            output = stdout + "\n" + stderr
            test_results = self._parse_test_output(output)

            report: dict[str, Any] = SandboxTestReport(
                passed=passed,
                total=test_results.get("total", 0),
                failures=test_results.get("failures", 0),
                stdout=stdout[-_OUTPUT_TAIL:],
                stderr=note + stderr[-_OUTPUT_TAIL:],
                duration_ms=(time.monotonic() - start_time) * 1000,
                entrypoint_runs=entrypoint_runs,
                **per_test_fields(output, passed),
            ).model_dump()
            report.update(report_extras(stdout, stderr, _OUTPUT_TAIL, _OUTPUT_TAIL))
            if targeted is not None:
                report["targeted"] = targeted
            return report

        except UnsafeCommandError as exc:
            return _failure(str(exc), start_time)

        except Exception as exc:
            logger.exception("E2B sandbox error")
            return _failure(f"E2B sandbox error: {exc}", start_time)

        finally:
            # 6. Clean up
            if sandbox is not None:
                try:
                    await sandbox.kill()
                    logger.info("Destroyed E2B sandbox")
                except Exception:
                    logger.warning("Failed to destroy E2B sandbox", exc_info=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run(self, sandbox: Any, command: str, timeout: float | None = None) -> Any:
        """Run *command* in the workspace and return its result, whatever its exit code.

        The workspace is on ``PYTHONPATH``.  The SDK raises
        ``CommandExitException`` for a non-zero exit; it carries the same
        ``stdout``/``stderr``/``exit_code`` as a result, and a failing command
        is an ordinary outcome here, not a sandbox error.
        """
        try:
            return await sandbox.commands.run(
                f"cd {_WORKSPACE} && {command}",
                timeout=timeout or self._timeout,
                envs=dict(_COMMAND_ENV),
            )
        except _CommandExitException as exc:
            return exc

    def _entrypoints(self, patch: dict[str, Any]) -> list[str]:
        """The patch's entrypoints that exist in the sandbox: written by the patch or uploaded."""
        deleted = {p for p in map(normalize_rel_path, patch.get("delete_files") or []) if p}
        written = list(patch.get("files") or {})
        return [
            path
            for path in entrypoint_paths(patch, self._workspace_root, written=written)
            if path not in deleted
        ]

    async def _run_entrypoint(self, sandbox: Any, file: str) -> EntrypointRun:
        """Smoke-run *file* with ``python`` (empty stdin, ``timeout`` of ENTRYPOINT_TIMEOUT s)."""
        logger.info("E2B: smoke-running entrypoint %s", file)
        command = f"timeout {ENTRYPOINT_TIMEOUT} python {shlex.quote(file)} </dev/null"
        try:
            ran = await self._run(sandbox, command, timeout=_ENTRYPOINT_SDK_TIMEOUT)
        except Exception as exc:  # the SDK's own timeout or a transport error
            logger.warning("E2B: entrypoint run of %s failed: %s", file, exc)
            timed_out = "timeout" in type(exc).__name__.lower()
            return entrypoint_run(file, None, "", str(exc), timed_out=timed_out)
        return entrypoint_run(
            file,
            ran.exit_code,
            ran.stdout or "",
            ran.stderr or "",
            timed_out=ran.exit_code == _TIMEOUT_EXIT_CODE,
        )

    async def _upload_workspace(self, sandbox: Any) -> None:
        """Recursively upload workspace files into ``/home/user/workspace``.

        Skips Delegatron's own directories at the root (``runs/`` and
        ``reviews/`` only when they hold Delegatron's layout), ignored
        directories at any depth, and files that are not UTF-8 text.
        """
        if not self._workspace_root.is_dir():
            return

        resolved_root = self._workspace_root.resolve()
        internal = internal_dirs_for(self._workspace_root)

        for path in sorted(self._workspace_root.rglob("*")):
            rel = path.relative_to(self._workspace_root).as_posix()
            parts = rel.split("/")
            if parts[0] in internal or any(part in _IGNORE_PATTERNS for part in parts):
                continue

            # Symlink validation: ensure resolved target is within workspace
            if path.is_symlink():
                try:
                    resolved = path.resolve()
                except OSError:
                    logger.warning("Skipping unresolvable symlink: %s", path)
                    continue
                if not resolved.is_relative_to(resolved_root):
                    logger.warning("Skipping symlink escaping workspace: %s -> %s", path, resolved)
                    continue

            target = f"{_WORKSPACE}/{rel}"
            if path.is_dir():
                await sandbox.commands.run(f"mkdir -p {shlex.quote(target)}")
            elif path.is_file():
                try:
                    content = path.read_bytes().decode("utf-8")
                except (UnicodeDecodeError, OSError):
                    # Skip binary or unreadable files
                    continue
                try:
                    await sandbox.files.write(target, content)
                except Exception as e:
                    raise RuntimeError(f"E2B upload failed at {rel}: {e}") from e

    @staticmethod
    def _validate_e2b_path(file_path: str) -> str:
        """Return *file_path* as a normalized workspace-relative path.

        Raises :class:`ValueError` for a path that is absolute (posix root,
        drive letter, UNC), escapes the workspace via ``..``, or is empty.
        """
        normalized = normalize_rel_path(file_path)
        if normalized is not None:
            return normalized
        windows_path = PureWindowsPath(file_path)
        if windows_path.anchor:
            raise ValueError(f"Absolute path not allowed in patch: {file_path!r}")
        if ".." in windows_path.parts:
            raise ValueError(f"Path traversal not allowed in patch: {file_path!r}")
        raise ValueError(f"Empty path not allowed in patch: {file_path!r}")

    async def _write(self, sandbox: Any, rel_path: str, content: str | bytes) -> None:
        """Write *content* to *rel_path* under the workspace, creating its directory."""
        parent = str(PurePosixPath(rel_path).parent)
        if parent != ".":
            await sandbox.commands.run(f"mkdir -p {shlex.quote(f'{_WORKSPACE}/{parent}')}")
        try:
            await sandbox.files.write(f"{_WORKSPACE}/{rel_path}", content)
        except Exception as e:
            raise RuntimeError(f"E2B upload failed at {rel_path}: {e}") from e

    async def _apply_patch(
        self, sandbox: Any, patch: dict[str, Any]
    ) -> tuple[str | None, list[str]]:
        """Apply *patch* in the sandbox.

        Returns ``(error, rejected)``: an error string when the diff did not
        apply (else ``None``) and the patch paths skipped as unsafe.
        """
        files: dict[str, str] = patch.get("files") or {}
        diff: str = patch.get("diff_unified", "")
        if not files and diff:
            # Try to extract new-file contents from the diff first
            files = self._parse_diff_to_files(diff)
            if not files:
                diff_error = await self._apply_diff(sandbox, diff)
                if diff_error is not None:
                    return diff_error, []

        rejected: list[str] = []
        for raw_path, content in files.items():
            try:
                payload = _upload_payload(content)
                await self._write(sandbox, self._validate_e2b_path(raw_path), payload)
            except ValueError:
                rejected.append(raw_path)
        for raw_path in patch.get("delete_files") or []:
            try:
                target = f"{_WORKSPACE}/{self._validate_e2b_path(raw_path)}"
            except ValueError:
                rejected.append(raw_path)
                continue
            await sandbox.commands.run(f"rm -f {shlex.quote(target)}")
        if rejected:
            logger.warning("Skipped unsafe patch path(s): %s", ", ".join(rejected))
        return None, rejected

    async def _apply_diff(self, sandbox: Any, diff: str) -> str | None:
        """Apply *diff* with ``patch``; returns an error string when it does not apply.

        ``--forward`` makes an already-applied hunk a failure instead of
        silently reversing it.
        """
        try:
            await sandbox.files.write("/tmp/patch.diff", diff)
        except Exception as e:
            raise RuntimeError(f"E2B failed to write patch diff: {e}") from e
        command = "patch -p1 --forward --batch < /tmp/patch.diff"
        result = await self._run(sandbox, command)
        if result.exit_code != 0:
            return step_failure(
                "Patch failed to apply",
                command,
                result.exit_code,
                result.stdout or "",
                result.stderr or "",
            )
        return None

    @staticmethod
    def _parse_test_output(stdout: str) -> dict[str, int]:
        """Test counts from the runner output (``counts.parse_test_counts``)."""
        total, failures = parse_test_counts(stdout)
        return {"total": total, "failures": failures}

    @staticmethod
    def _parse_diff_to_files(diff: str) -> dict[str, str]:
        """Extract new file contents from a unified diff.

        Handles diffs that create new files (``--- /dev/null``).
        Returns empty dict for modification diffs so the caller
        falls back to ``patch``.
        """
        files: dict[str, str] = {}
        current_path: str | None = None
        current_lines: list[str] = []
        is_new_file = False

        for line in diff.split("\n"):
            if line.startswith("--- "):
                is_new_file = line.strip() in ("--- /dev/null", "--- a//dev/null")
            elif line.startswith("+++ "):
                if current_path and current_lines:
                    content = "\n".join(current_lines)
                    if content and not content.endswith("\n"):
                        content += "\n"
                    files[current_path] = content

                path = line[4:].strip()
                if path.startswith("b/"):
                    path = path[2:]
                current_path = path if is_new_file else None
                current_lines = []
            elif line.startswith("@@"):
                continue
            elif current_path is not None and (line.startswith("+") or line.startswith(" ")):
                current_lines.append(line[1:])

        if current_path and current_lines:
            content = "\n".join(current_lines)
            if content and not content.endswith("\n"):
                content += "\n"
            files[current_path] = content

        return files


def _failure(stderr: str, start_time: float | None = None, *, stdout: str = "") -> dict[str, Any]:
    """Report for a run that stopped before its tests ran."""
    return SandboxTestReport(
        passed=False,
        total=0,
        failures=0,
        stdout=stdout[-5000:],
        stderr=stderr,
        duration_ms=0.0 if start_time is None else (time.monotonic() - start_time) * 1000,
    ).model_dump()
