"""Docker-based sandbox runner.

Stages the workspace plus a patch in a scratch copy, installs dependencies
in a networked container (cached by ``docker commit``), runs the tests in a
container without network, and captures the results as a TestReport.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from delegatron.detection import read_text_lenient
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
from delegatron.sandbox.parse_check import ParseCheckResult, PSAnalyzerCache, run_parse_checks
from delegatron.sandbox.prepare import (
    rejected_paths_note,
    select_targeted_tests,
    targeted_report,
)
from delegatron.sandbox.toolchains import (
    LANG_RULES,
    PIPENV_INSTALL,
    PYTEST_COMMAND,
    LangRule,
    canonical_language,
    is_container_only_install,
    match_file,
    pyproject_has_test_extra,
    rule_for_language,
    select_docker_image,
)
from delegatron.schemas.test_report import EntrypointRun
from delegatron.schemas.test_report import TestReport as SandboxTestReport
from delegatron.utils.paths import internal_copy_ignore, normalize_rel_path
from delegatron.utils.test_output import parse_pytest_outcomes

logger = logging.getLogger(__name__)

# Default Docker image for Python projects
DEFAULT_IMAGE = "python:3.12-slim"

# Timeout for container operations (seconds)
CONTAINER_TIMEOUT = 600

_COMMIT_TIMEOUT = 120
_PROBE_TIMEOUT = 60

# Characters of container output kept in reports
_OUTPUT_TAIL = 5000

# Extra seconds allowed for container start-up around an entrypoint's own timeout
_ENTRYPOINT_CONTAINER_GRACE = 60

# Exit status of coreutils ``timeout`` when the command ran out of time
_TIMEOUT_EXIT_CODE = 124

# ``command -v`` exit codes meaning the executable (or the shell) is missing
_TOOL_MISSING_EXIT_CODES = frozenset({1, 127})

# Label on every container a sandbox starts, valued with its instance id
_SANDBOX_LABEL = "delegatron.sandbox"

# Per worker thread: the cancel flag of the apply_and_test call it runs.
_CALL = threading.local()

# Whether containers can leave files the host user cannot delete (rootful Linux)
_POSIX_HOST = os.name != "nt"

# Build toolchain for Python packages with C extensions; installed only when
# an install fails for want of a compiler.
_PYTHON_BUILD_TOOLCHAIN = "apt-get update -qq && apt-get install -y -qq gcc g++ make"
_COMPILER_MISSING = re.compile(
    r"\b(?:gcc|g\+\+|cc|make): (?:command )?not found"
    r"|error: command '[^']*(?:gcc|g\+\+|cc)' failed"
    r"|unable to execute '[^']*(?:gcc|g\+\+|cc)'"
    r"|Failed building wheel"
)

# Bash: create (once) and activate the bind-mounted venv for Python runs
_VENV_DIR = "/workspace/.venv"
_VENV_PREFIX = f"test -d {_VENV_DIR} || python -m venv {_VENV_DIR} && . {_VENV_DIR}/bin/activate"

# Never staged, at any depth: VCS data, caches and virtualenvs.
_STAGING_SKIP_NAMES = (
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
)

# Dependency manifests and lockfiles whose contents key the install cache.
_MANIFEST_PATTERNS = (
    "requirements*.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile*",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "pom.xml",
    "build.gradle*",
    "*.csproj",
    "*.fsproj",
    "*.sln",
    "Gemfile*",
    "composer.json",
    "composer.lock",
)
_MANIFEST_MAX_DEPTH = 3
_MANIFEST_SKIP_DIRS = frozenset(
    {"node_modules", "vendor", "target", "bin", "obj", "build", "dist", "__pycache__"}
)

# Windows container image patterns
_WINDOWS_IMAGE_PATTERNS = (
    "windows",
    "nanoserver",
    "servercore",
    "windowsservercore",
)


def _is_windows_image(image: str) -> bool:
    """Check if a Docker image is a Windows container image."""
    name = image.lower()
    return any(p in name for p in _WINDOWS_IMAGE_PATTERNS)


def _get_docker_os() -> str:
    """Return current Docker daemon OS: 'linux' or 'windows'."""
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Os}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.stdout.strip().lower() if result.returncode == 0 else "unknown"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"


def _switch_docker_mode(target: str) -> bool:
    """Switch Docker Desktop between 'linux' and 'windows' mode.

    Returns True if switch succeeded or already in target mode.
    Only works on Windows with Docker Desktop installed.
    """
    current = _get_docker_os()
    if current == target:
        return True

    logger.info(
        "Switching Docker from %s to %s containers...",
        current,
        target,
    )
    try:
        # Check no other containers are running
        result = subprocess.run(
            ["docker", "ps", "-q"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        running = [c for c in result.stdout.strip().split("\n") if c]
        if running:
            logger.warning(
                "Cannot switch Docker mode — %d container(s) running",
                len(running),
            )
            return False

        # Switch daemon
        cli = r"C:\Program Files\Docker\Docker\DockerCli.exe"
        result = subprocess.run(
            [cli, "-SwitchDaemon"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "Docker mode switch failed: %s",
                result.stderr.strip(),
            )
            return False

        # Wait for daemon to come up
        import time as _time

        for _ in range(15):
            _time.sleep(2)
            if _get_docker_os() == target:
                logger.info("Docker switched to %s containers", target)
                return True

        logger.warning("Docker mode switch timed out")
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("Docker mode switch error: %s", e)
        return False


def _validate_sandbox_path(sandbox_dir: Path, file_path: str) -> Path:
    """Join *file_path* onto *sandbox_dir*, refusing paths that leave it.

    The path is checked lexically first (no absolute, drive, UNC or ``..``
    path is ever resolved), then resolved to catch symlinks pointing out of
    the sandbox.  Raises :class:`ValueError` for an unsafe path.
    """
    rel_path = normalize_rel_path(file_path)
    if rel_path is None:
        raise ValueError(f"Path escapes sandbox: {file_path!r}")
    root = sandbox_dir.resolve()
    full_path = (root / rel_path).resolve()
    if not full_path.is_relative_to(root):
        raise ValueError(f"Path escapes sandbox: {file_path!r} resolved to {full_path}")
    return full_path


# Workspace-relative directories that hold installed dependencies for a given
# language.  These live inside the bind-mounted workspace, so Docker's commit
# won't persist them.
#
# Strategy: for each listed dir we create a Docker named volume and mount it
# OVER the bind-mount at that path (docker layers specific mounts on top of
# parent mounts).  ``npm install`` writes into the volume; the next iteration's
# test container mounts the same volume and finds ``node_modules`` intact.
# This avoids the symlink-through-bind-mount fragility on Docker Desktop
# (WSL2/macOS) where inner ``node_modules/.bin`` symlinks don't survive.
#
# Languages not listed here either keep their deps outside the workspace
# (e.g. Python → site-packages, Go → GOMODCACHE, Rust → ~/.cargo/registry)
# or haven't been validated for this caching strategy yet.
_CACHED_WORKSPACE_DIRS: dict[str, tuple[str, ...]] = {
    "javascript": ("node_modules",),
    "typescript": ("node_modules",),
    "php": ("vendor",),
}

# Where each language's toolchain keeps downloaded dependencies outside the
# workspace, in the official images (golang, rust, maven, dotnet/sdk, ruby).
# Normally ``docker commit`` keeps them for the offline test phase; when the
# commit fails, the install reruns with these directories on named volumes
# that the test container mounts too.  (Python reinstalls into the
# bind-mounted venv instead; node_modules and vendor live in the workspace.)
_DEP_CACHE_DIRS: dict[str, tuple[str, ...]] = {
    "go": ("/go/pkg/mod",),
    "rust": ("/usr/local/cargo/registry", "/usr/local/cargo/git"),
    "java": ("/root/.m2", "/root/.gradle"),
    "kotlin": ("/root/.m2", "/root/.gradle"),
    "scala": ("/root/.ivy2", "/root/.cache/coursier"),
    "csharp": ("/root/.nuget/packages",),
    "fsharp": ("/root/.nuget/packages",),
    "ruby": ("/usr/local/bundle",),
}


def _cache_volume_name(cache_key: str, dir_name: str) -> str:
    """Deterministic Docker named-volume identifier for a language's dep dir."""
    # Slash-safe: replace anything non-alphanumeric in dir_name.
    safe_dir = "".join(c if c.isalnum() else "_" for c in dir_name)
    return f"delegatron-deps-{cache_key}-{safe_dir}"


def _cache_volume_mounts(
    lang: str | None,
    cache_key: str,
    workspace_mount: str,
    is_windows: bool,
) -> tuple[list[str], list[str]]:
    """Return ``(docker_args, volume_names)`` for every cached dep dir.

    ``docker_args`` is a flat list of ``-v name:path`` pairs to splice into a
    ``docker run/create`` command.  ``volume_names`` lists just the volume
    names, for cleanup via ``docker volume rm``.

    Both lists are empty when the language has no workspace-local deps, or
    on Windows (named volumes over bind-mounted paths are unreliable in
    Windows-container mode).
    """
    if is_windows or lang is None:
        return [], []
    dirs = _CACHED_WORKSPACE_DIRS.get(lang, ())
    if not dirs:
        return [], []
    args: list[str] = []
    names: list[str] = []
    for d in dirs:
        vol = _cache_volume_name(cache_key, d)
        args.extend(["-v", f"{vol}:{workspace_mount}/{d}"])
        names.append(vol)
    return args, names


def _rule_commands(rule: LangRule) -> tuple[str, str, str]:
    return rule.language, rule.test_command, rule.install_command


def _detect_sandbox_language(
    sandbox_dir: Path,
    patch_files: Mapping[str, Any] | None = None,
    hint: str = "",
) -> tuple[str, str, str] | None:
    """Detect the toolchain for a run as ``(language, test_command, install_command)``.

    A *hint* (the project's detected language) with a primary rule wins.
    Otherwise the patch's primary files decide by majority, ties going to
    rule order; then an auxiliary hint; then the workspace's primary marker
    files; then auxiliary patch files and markers.  Auxiliary files
    (scripts, Makefiles) never outvote primary ones.  ``None`` when nothing
    matched.
    """
    hint_rule = rule_for_language(hint) if hint else None
    if hint_rule is not None and hint_rule.primary:
        return _rule_commands(hint_rule)

    matched = [rule for rule in map(match_file, patch_files or ()) if rule is not None]
    primary = [rule for rule in matched if rule.primary]
    if primary:
        votes = Counter(rule.language for rule in primary)
        winner = min(primary, key=lambda rule: (-votes[rule.language], LANG_RULES.index(rule)))
        return _rule_commands(winner)
    if hint_rule is not None:
        return _rule_commands(hint_rule)

    markers = [
        rule
        for rule in LANG_RULES
        if "*" not in rule.pattern and (sandbox_dir / rule.pattern).exists()
    ]
    candidates = [*(rule for rule in markers if rule.primary), *matched, *markers]
    return _rule_commands(candidates[0]) if candidates else None


def _with_python_manifest_install(sandbox_dir: Path, install_cmds: list[str]) -> list[str]:
    """Prepend an install for a Python manifest the commands do not cover yet.

    Covers manifests a patch adds after the commands were built (e.g. a
    workspace that was empty when the pipeline started).
    """
    if any("requirements" in c or "pip install -e" in c or "pipenv" in c for c in install_cmds):
        return install_cmds
    pyproject = sandbox_dir / "pyproject.toml"
    if pyproject.exists():
        target = '".[test]"' if pyproject_has_test_extra(pyproject) else "."
        return [f"pip install -e {target} -q", *install_cmds]
    if (sandbox_dir / "requirements.txt").exists():
        return ["pip install -r requirements.txt -q", *install_cmds]
    if (sandbox_dir / "Pipfile").exists():
        return [*PIPENV_INSTALL, *install_cmds]
    return install_cmds


def _command_executable(command: str) -> str:
    """Return the program a shell command starts with ("" for a relative path)."""
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    program = next((word for word in words if "=" not in word), "")
    return "" if "/" in program or "\\" in program else program


def _manifest_paths(root: Path) -> list[str]:
    """Sorted posix paths of dependency manifests at most a few dirs below *root*."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        if len(rel_dir.parts) >= _MANIFEST_MAX_DEPTH:
            dirnames.clear()
        else:
            dirnames[:] = [
                d for d in dirnames if not d.startswith(".") and d not in _MANIFEST_SKIP_DIRS
            ]
        found.extend(
            (rel_dir / name).as_posix()
            for name in filenames
            if any(fnmatchcase(name, pattern) for pattern in _MANIFEST_PATTERNS)
        )
    return sorted(found)


def _compute_cache_key(image: str, install_cmds: list[str], sandbox_dir: Path) -> str:
    """Key for the install cache.

    Hashes the image, the install commands and the contents of every
    dependency manifest and lockfile in *sandbox_dir*, so adding a
    dependency to an existing manifest reinstalls.
    """
    digest = hashlib.sha256(image.encode())
    for command in sorted(install_cmds):
        digest.update(b"\0" + command.encode())
    for rel_path in _manifest_paths(sandbox_dir):
        try:
            content = (sandbox_dir / rel_path).read_bytes()
        except OSError:
            continue
        digest.update(b"\0" + rel_path.encode() + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()[:12]


def _staging_ignore(workspace_root: Path) -> Callable[[str, list[str]], set[str]]:
    """``copytree`` ignore callable for staging *workspace_root*.

    Delegatron's own directories and ``logs`` are dropped only at the root
    (a nested ``app/logs`` package is user code); VCS data, caches and
    virtualenvs at any depth; dangling symlinks, which would fail the copy,
    are skipped with a warning.
    """
    root = workspace_root.resolve()
    internal = internal_copy_ignore(root, extra=_STAGING_SKIP_NAMES)

    def _ignore(directory: str, names: list[str]) -> set[str]:
        ignored = internal(directory, names)
        if "logs" in names and Path(directory).resolve() == root:
            ignored.add("logs")
        for name in names:
            path = os.path.join(directory, name)
            if name.endswith(".pyc"):
                ignored.add(name)
            elif os.path.islink(path) and not os.path.exists(path):
                logger.warning("Sandbox staging: skipping dangling symlink %s", path)
                ignored.add(name)
        return ignored

    return _ignore


def _diff_target_paths(diff: str) -> list[str]:
    """Normalized ``+++`` paths a unified diff writes (``/dev/null`` excluded)."""
    paths: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("+++ "):
            continue
        target = line[4:].split("\t", 1)[0].strip()
        rel_path = normalize_rel_path(target[2:] if target.startswith("b/") else target)
        if rel_path and rel_path not in paths:
            paths.append(rel_path)
    return paths


def _decode(output: bytes | str | None) -> str:
    """Text of a ``TimeoutExpired``'s partial output (bytes on POSIX even in text mode)."""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output or ""


def _shell_for(is_windows: bool, commands: list[str]) -> tuple[str, list[str], str]:
    """Return ``(workspace_mount, shell argv, command separator)`` for an image."""
    if not is_windows:
        return "/workspace", ["bash", "-c"], " && "
    text = " ".join(commands).lower()
    if any(marker in text for marker in ("powershell", "pwsh", ".ps1")):
        return "C:\\workspace", ["powershell", "-NoProfile", "-Command"], " ; "
    return "C:\\workspace", ["cmd", "/c"], " && "


def _container_args(staging_dir: Path, workspace_mount: str, language: str) -> list[str]:
    """Limits, workspace bind mount and env shared by every container of a run."""
    args = [
        "--memory",
        "512m",
        "--cpus",
        "1",
        "-v",
        f"{staging_dir.as_posix()}:{workspace_mount}:rw",
        "-w",
        workspace_mount,
    ]
    if language == "python":
        args += [
            "-e",
            f"PYTHONPATH={workspace_mount}",
            # Prevent setuptools-scm from failing on shallow clones
            # or workspaces without full git history.
            "-e",
            "SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0",
            # Fewer root-owned files left in the bind mount.
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            "PYTEST_ADDOPTS=-p no:cacheprovider",
        ]
    return args


def _gate_failure(gate: ParseCheckResult, start_time: float) -> dict[str, Any] | None:
    """Report for files that fail to parse, or ``None`` when all parse.

    The gate catches syntax errors in the written files before any time is
    spent on installs and tests.
    """
    if not gate.errors:
        return None
    logger.info("Parse-check gate blocked %d file(s)", len(gate.errors))
    report_body = "\n".join(e.format() for e in gate.errors)
    return _failure_report(f"Parse-check gate failed:\n{report_body}", start_time)


def _with_lint_warnings(report: dict[str, Any], gate: ParseCheckResult) -> dict[str, Any]:
    """Attach the gate's non-blocking advisories to *report* as ``lint_warnings``."""
    if gate.advisories:
        report["lint_warnings"] = list(gate.advisories)
    return report


def _setup_failure(exc: OSError, start_time: float) -> dict[str, Any]:
    """Report for a staging error, so it is not mistaken for a fault in the patch."""
    logger.warning("Sandbox setup failed: %s", exc)
    return _failure_report(f"Sandbox setup failed (not caused by the patch): {exc}", start_time)


def _failure_report(stderr: str, start_time: float, stdout: str = "") -> dict[str, Any]:
    """A failed TestReport for a run that ended before (or instead of) the tests."""
    return SandboxTestReport(
        passed=False,
        total=0,
        failures=0,
        stdout=stdout,
        stderr=stderr,
        duration_ms=(time.monotonic() - start_time) * 1000,
    ).model_dump()


# --------------------------------------------------------------------------- #
# Post-patch fixups — sanitize LLM-generated config before install/test
# --------------------------------------------------------------------------- #


def _strip_stdlib_from_requirements(sandbox_dir: Path) -> None:
    """Remove Python stdlib modules from requirements.txt.

    Claude consistently adds stdlib modules (sqlite3, tkinter, etc.) to
    requirements.txt, causing pip install failures on the first iteration.
    """
    req_path = sandbox_dir / "requirements.txt"
    if not req_path.exists():
        return

    from delegatron.security.deps import _STDLIB_MODULES

    lines = read_text_lenient(req_path).splitlines()
    filtered: list[str] = []
    removed: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            filtered.append(line)
            continue
        # Extract package name before any version specifier
        pkg_name = re.split(r"[><=!~\[;@\s]", stripped)[0].strip().lower()
        # Normalize underscores/hyphens for comparison
        pkg_normalized = pkg_name.replace("-", "_")
        if pkg_normalized in _STDLIB_MODULES:
            removed.append(pkg_name)
        else:
            filtered.append(line)

    if removed:
        logger.info("Stripped stdlib modules from requirements.txt: %s", ", ".join(removed))
        req_path.write_text("\n".join(filtered) + "\n", encoding="utf-8", newline="")


def _fix_jest_testmatch(sandbox_dir: Path) -> None:
    """Fix common Jest testMatch misconfiguration.

    Claude generates ``tests/**/*.test.js`` which only matches files in
    subdirectories of tests/, not files directly in tests/.  Add the
    missing root-level pattern.
    """
    import json as _json

    pkg_path = sandbox_dir / "package.json"
    if not pkg_path.exists():
        return

    try:
        pkg = _json.loads(pkg_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return

    # Check jest config in package.json
    jest_cfg = pkg.get("jest", {})
    test_match = jest_cfg.get("testMatch")
    if not test_match:
        return

    if isinstance(test_match, list):
        # Check for the broken pattern and add the missing root-level match
        broken = [p for p in test_match if p.endswith("tests/**/*.test.js")]
        if broken and not any("tests/*.test.js" in p and "**" not in p for p in test_match):
            # Add root-level pattern alongside the existing recursive one
            test_match.append("<rootDir>/tests/*.test.js")
            jest_cfg["testMatch"] = test_match
            pkg["jest"] = jest_cfg
            pkg_path.write_text(_json.dumps(pkg, indent=2) + "\n", encoding="utf-8", newline="")
            logger.info("Fixed Jest testMatch — added root-level tests/*.test.js pattern")
    elif isinstance(test_match, str) and test_match.endswith("tests/**/*.test.js"):
        jest_cfg["testMatch"] = [test_match, "<rootDir>/tests/*.test.js"]
        pkg["jest"] = jest_cfg
        pkg_path.write_text(_json.dumps(pkg, indent=2) + "\n", encoding="utf-8", newline="")
        logger.info("Fixed Jest testMatch — expanded string to include root-level pattern")

    # Also check standalone jest.config.js
    jest_config_path = sandbox_dir / "jest.config.js"
    if jest_config_path.exists():
        try:
            content = jest_config_path.read_text(encoding="utf-8")
        except (ValueError, OSError):
            return
        # Simple pattern: replace the broken glob if found
        if "tests/**/*.test.js" in content and "tests/*.test.js" not in content:
            content = content.replace(
                '"tests/**/*.test.js"',
                '"tests/**/*.test.js", "tests/*.test.js"',
            ).replace(
                "'tests/**/*.test.js'",
                "'tests/**/*.test.js', 'tests/*.test.js'",
            )
            jest_config_path.write_text(content, encoding="utf-8", newline="")
            logger.info("Fixed jest.config.js testMatch — added root-level pattern")


@dataclass
class _StagedPatch:
    """What staging applied to the sandbox copy."""

    # Every file the patch wrote (normalized path → content), for the parse gate
    files: dict[str, str] = field(default_factory=dict)
    # Patch paths left out because they were unsafe or could not be written
    skipped: list[str] = field(default_factory=list)
    # Set when the diff did not apply
    error: str = ""

    def write(self, sandbox_dir: Path, files: Mapping[str, str]) -> None:
        """Write *files* into the sandbox copy as the workspace does (UTF-8, surrogateescape).

        A file that cannot be written (unsafe path, a lone surrogate that has
        no UTF-8 form, an OS error) is skipped and listed in ``skipped``.
        """
        for file_path, content in files.items():
            try:
                full_path = _validate_sandbox_path(sandbox_dir, file_path)
                data = content.encode("utf-8", errors="surrogateescape")
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_bytes(data)
            except (ValueError, OSError) as exc:
                logger.warning("Sandbox staging: skipping %r: %s", file_path, exc)
                self.skipped.append(file_path)
                continue
            self.files[full_path.relative_to(sandbox_dir.resolve()).as_posix()] = content

    def delete(self, sandbox_dir: Path, file_path: str) -> None:
        try:
            full_path = _validate_sandbox_path(sandbox_dir, file_path)
            if full_path.is_dir():
                raise IsADirectoryError(f"{file_path!r} is a directory")
            full_path.unlink(missing_ok=True)
        except (ValueError, OSError) as exc:
            logger.warning("Sandbox staging: not deleting %r: %s", file_path, exc)
            self.skipped.append(file_path)


@dataclass(frozen=True)
class _Toolchain:
    """Language, image and commands resolved for one sandbox run."""

    language: str
    image: str
    test_commands: list[str]
    install_commands: list[str]


class _InstallFailed(Exception):
    """The dependency install exited non-zero."""

    def __init__(self, result: subprocess.CompletedProcess[str]) -> None:
        super().__init__(f"dependency install failed (rc={result.returncode})")
        self.stdout = result.stdout
        self.stderr = result.stderr


class _RunCancelled(Exception):
    """The apply_and_test call was cancelled; no further container is started."""

    def __init__(self, phase: str) -> None:
        super().__init__(f"run cancelled before the {phase} container started")


class _ContainerTimeout(Exception):
    """A container ran past its timeout and was force-removed."""

    def __init__(self, phase: str, timeout: float, stdout: str, stderr: str) -> None:
        super().__init__(f"{phase.capitalize()} phase timed out after {timeout:.0f}s")
        self.stdout = stdout
        self.stderr = stderr


class DockerSandbox(BaseSandbox):
    """Runs code in isolated Docker containers.

    Lifecycle per test run:
    1. Copy the workspace to a scratch dir and apply the patch there
    2. Resolve language, image and commands for what is staged
    3. Install dependencies with network (cached as a committed image)
    4. Run the tests without network on the bind-mounted scratch dir
    5. Capture stdout/stderr and exit code, then clean up
    """

    def __init__(
        self,
        workspace_root: Path,
        image: str = DEFAULT_IMAGE,
        test_commands: list[str] | None = None,
        install_commands: list[str] | None = None,
        *,
        language: str = "",
        image_explicit: bool = False,
    ) -> None:
        self._workspace_root = workspace_root
        self._image = image
        # An explicitly configured image is used for every language.
        self._image_explicit = image_explicit
        # Detected project language the image and commands were built for
        # ("" when none was detected: the commands are then the Python fallback).
        self._language = canonical_language(language)
        self._test_commands = test_commands or [PYTEST_COMMAND]
        self._install_commands = install_commands or []
        # Tells this sandbox's images, volumes and containers apart from those
        # of other runs sharing the Docker daemon.
        self._instance_id = uuid.uuid4().hex[:8]
        self._container_seq = 0
        # Install-cache images committed by this sandbox, one per cache key.
        self._cached_images: set[str] = set()
        # Named Docker volumes used to persist workspace-local dep dirs
        # (e.g. ``node_modules``) across iterations.  Cleaned up on cleanup().
        self._cached_volumes: set[str] = set()
        # Containers whose removal is not confirmed; cleanup() sweeps them.
        self._unreaped: set[str] = set()
        # Whether any container was started with this sandbox's label; then
        # cleanup() removes whatever labelled containers are left.
        self._started_containers = False
        # (image, executable) pairs the toolchain probe found.
        self._probed_tools: set[tuple[str, str]] = set()
        # PSScriptAnalyzer volume per image, for the parse-check gate.
        self._ps_analyzers: dict[str, PSAnalyzerCache] = {}

    def get_info(self) -> dict[str, Any]:
        """Return sandbox configuration metadata for event reporting."""
        is_windows = _is_windows_image(self._image)
        return {
            "image": self._image,
            "container_os": "windows" if is_windows else "linux",
            "shell": "powershell" if is_windows else "bash",
            "workspace_mount": "C:\\workspace" if is_windows else "/workspace",
            "network_isolation": True,
            # The install phase has network; only the test phase is isolated.
            "install_network": True,
        }

    # ------------------------------------------------------------------ #
    # Containers, images and cleanup
    # ------------------------------------------------------------------ #

    def _docker_run(
        self,
        phase: str,
        args: list[str],
        *,
        remove: bool = True,
        timeout: float = CONTAINER_TIMEOUT,
    ) -> tuple[str, subprocess.CompletedProcess[str]]:
        """``docker run <args>`` in a container named and labelled for this sandbox.

        Returns ``(container name, result)``.  A container that times out is
        force-removed and :class:`_ContainerTimeout` carries its partial output.
        Raises :class:`_RunCancelled` instead of starting a container once the
        calling ``apply_and_test`` was cancelled.
        """
        cancelled: threading.Event | None = getattr(_CALL, "cancelled", None)
        if cancelled is not None and cancelled.is_set():
            raise _RunCancelled(phase)
        self._container_seq += 1
        self._started_containers = True
        name = f"delegatron-{self._instance_id}-{phase}-{self._container_seq}"
        if not remove:
            self._unreaped.add(name)
        cmd = [
            "docker",
            "run",
            *(["--rm"] if remove else []),
            "--name",
            name,
            "--label",
            f"{_SANDBOX_LABEL}={self._instance_id}",
            *args,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self._unreaped.add(name)
            self._remove_container(name)
            partial = _decode(exc.stdout), _decode(exc.stderr)
            raise _ContainerTimeout(phase, timeout, *partial) from exc
        return name, result

    def _remove_container(self, name: str) -> None:
        """Force-remove container *name*, best effort."""
        try:
            result = subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("Docker: could not remove container %s: %s", name, exc)
            return
        if result.returncode == 0:
            self._unreaped.discard(name)

    def _commit(self, container: str, tag: str) -> bool:
        """Commit the stopped install *container* as *tag*, then remove the container."""
        try:
            result = subprocess.run(
                ["docker", "commit", container, tag],
                capture_output=True,
                text=True,
                timeout=_COMMIT_TIMEOUT,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("Docker: cache commit error: %s — continuing without cache", exc)
            committed = False
        else:
            committed = result.returncode == 0
            if not committed:
                logger.warning(
                    "Docker: commit failed (rc=%d): %s — continuing without cache",
                    result.returncode,
                    result.stderr[:200],
                )
        finally:
            self._remove_container(container)
        if committed:
            self._cached_images.add(tag)
            logger.info("Docker: committed install container %s as %s", container, tag)
        return committed

    def _cache_tag(self, cache_key: str) -> str:
        return f"delegatron-cache-{self._instance_id}-{cache_key}"

    def _cache_usable(self, tag: str) -> bool:
        """Whether *tag* was committed by this sandbox and still exists on the daemon."""
        if tag not in self._cached_images:
            return False
        if self._image_exists(tag):
            return True
        logger.warning("Docker: cached image %s is gone — reinstalling", tag)
        self._cached_images.discard(tag)
        return False

    def _missing_tool(self, image: str, commands: list[str]) -> str:
        """Return the executable of ``commands[0]`` if *image* lacks it, else "".

        Probed once per image and executable.  An inconclusive probe (timeout,
        Docker error) counts as present, so the run reports the real failure.
        """
        tool = _command_executable(commands[0]) if commands else ""
        if not tool or (image, tool) in self._probed_tools:
            return ""
        script = f"command -v {shlex.quote(tool)}"
        try:
            _, result = self._docker_run(
                "probe",
                ["--network", "none", image, "sh", "-c", script],
                timeout=_PROBE_TIMEOUT,
            )
        except _ContainerTimeout:
            return ""
        if result.returncode == 0:
            self._probed_tools.add((image, tool))
        return tool if result.returncode in _TOOL_MISSING_EXIT_CODES else ""

    def _remove_tree(self, path: Path) -> bool:
        """Delete *path*; True when it is gone.

        Containers run as root, so on a rootful Linux daemon they leave files
        in the bind mount the host user cannot delete; those are removed from
        inside a container.
        """
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return True
        except OSError as exc:
            if not _POSIX_HOST:
                logger.warning("Sandbox: could not remove %s: %s", path, exc)
                return False
            logger.info("Sandbox: removing %s from a container (%s)", path, exc)
            image = DEFAULT_IMAGE if _is_windows_image(self._image) else self._image
            mount = f"{path.parent.as_posix()}:/s"
            try:
                self._docker_run(
                    "cleanup",
                    ["--network", "none", "-v", mount, image, "rm", "-rf", f"/s/{path.name}"],
                    timeout=_COMMIT_TIMEOUT,
                )
            except (_ContainerTimeout, OSError) as cleanup_exc:
                logger.warning("Sandbox: container cleanup of %s failed: %s", path, cleanup_exc)
            shutil.rmtree(path, ignore_errors=True)
        return not path.exists()

    def _prepare_staging_dir(self) -> Path:
        """Return an empty directory to stage this run in.

        The fixed ``_sandbox/workspace`` path avoids Docker Desktop (WSL2)
        mount races.  When leftovers there cannot be removed, a fresh
        directory beside it is used so the run still goes ahead.
        """
        staging_dir = Path(self._workspace_root).resolve() / "_sandbox" / "workspace"
        if self._remove_tree(staging_dir):
            staging_dir.mkdir(parents=True)
            return staging_dir
        fallback = Path(tempfile.mkdtemp(prefix="workspace-", dir=staging_dir.parent))
        logger.warning("Sandbox: could not clear %s — staging in %s", staging_dir, fallback)
        return fallback

    def cleanup(self) -> None:
        """Remove the images, volumes and leftover containers this sandbox created.

        Every container this sandbox started carries its label, so once any
        was started, labelled containers still on the daemon are removed
        whatever this sandbox believes about them (a ``--rm`` container whose
        CLI was killed, a gate container that timed out).  Safe to call
        multiple times.  Called automatically by ``__del__`` but callers
        should prefer invoking this explicitly for deterministic cleanup
        (e.g. when the orchestrator finishes a run).
        """
        if self._started_containers or self._unreaped:
            self._started_containers = False
            self._unreaped.clear()
            listed = self._docker_quietly(
                ["docker", "ps", "-aq", "--filter", f"label={_SANDBOX_LABEL}={self._instance_id}"],
                "list leftover containers",
            )
            if listed.split():
                self._docker_quietly(["docker", "rm", "-f", *listed.split()], "remove containers")
        if self._cached_images:
            tags = sorted(self._cached_images)
            self._cached_images.clear()
            self._docker_quietly(["docker", "rmi", *tags], "remove cached images")
        # `-f` forces removal even if a (stopped) container still references the volume.
        if self._cached_volumes:
            vols = sorted(self._cached_volumes)
            self._cached_volumes.clear()
            self._docker_quietly(["docker", "volume", "rm", "-f", *vols], "remove cached volumes")

    @staticmethod
    def _docker_quietly(cmd: list[str], what: str) -> str:
        """Run a housekeeping docker command; return its stdout ("" on failure)."""
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("Docker: could not %s: %s", what, exc)
            return ""
        if result.returncode != 0:
            logger.debug("Docker: could not %s: %s", what, result.stderr.strip())
            return ""
        logger.info("Docker: %s: done", what)
        return result.stdout

    def __del__(self) -> None:
        """Remove cached image on garbage collection (safety net)."""
        import contextlib

        with contextlib.suppress(Exception):
            self.cleanup()

    # ------------------------------------------------------------------ #

    async def apply_and_test(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply file changes and run tests in Docker.

        The patch dict should contain:
        - "files": dict of {path: content} for files to write/update
        - "delete_files": paths to remove
        - "diff_unified": optional unified diff (alternative to files)
        - "test_commands": optional override for test commands
        - "targeted_tests": test-runner commands for the current task, run
          before the test commands and reported as "targeted"
        - "flaky_rerun": rerun failed pytest tests once to spot flakes (default True)
        - "tolerate_flaky": count a run as passed when every failure was flaky
        - "entrypoints": relative ``.py`` scripts to smoke-run after the tests

        All blocking work (filesystem ops, subprocess calls) runs in a
        thread pool to avoid blocking the asyncio event loop.  The thread
        cannot be cancelled: when the call is (the orchestrator's wall-time
        deadline), it starts no further container and this sandbox's running
        containers are force-removed before the cancellation goes on.
        """
        import asyncio

        cancelled = threading.Event()
        try:
            return await asyncio.to_thread(self._apply_and_test_in_call, patch, cancelled)
        except asyncio.CancelledError:
            cancelled.set()
            await asyncio.shield(asyncio.to_thread(self._kill_running_containers))
            raise

    def _apply_and_test_in_call(
        self, patch: dict[str, Any], cancelled: threading.Event
    ) -> dict[str, Any]:
        """:meth:`_apply_and_test_sync` with *cancelled* as this thread's cancel flag."""
        _CALL.cancelled = cancelled
        try:
            return self._apply_and_test_sync(patch)
        finally:
            _CALL.cancelled = None

    def _kill_running_containers(self) -> None:
        """Force-remove this sandbox's running containers (a cancelled call's)."""
        if not self._started_containers:
            return
        listed = self._docker_quietly(
            ["docker", "ps", "-q", "--filter", f"label={_SANDBOX_LABEL}={self._instance_id}"],
            "list running containers",
        ).split()
        if listed:
            logger.warning("Docker: run cancelled — removing %d container(s)", len(listed))
            self._docker_quietly(["docker", "rm", "-f", *listed], "remove running containers")

    def _apply_and_test_sync(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Synchronous implementation of apply_and_test.

        Called via asyncio.to_thread() to keep the event loop responsive.
        """
        start_time = time.monotonic()
        try:
            staging_dir = self._prepare_staging_dir()
        except OSError as exc:
            return _setup_failure(exc, start_time)
        try:
            try:
                shutil.copytree(
                    self._workspace_root,
                    staging_dir,
                    ignore=_staging_ignore(Path(self._workspace_root)),
                    dirs_exist_ok=True,
                )
            except OSError as exc:
                return _setup_failure(exc, start_time)
            staged = self._stage_patch(staging_dir, patch)
            if staged.error:
                report = _failure_report(staged.error, start_time)
            else:
                report = self._test_staged(staging_dir, patch, staged.files, start_time)
            report["stderr"] = rejected_paths_note(staged.skipped) + report["stderr"]
            return report
        finally:
            self._remove_tree(staging_dir)

    @staticmethod
    def _stage_patch(sandbox_dir: Path, patch: Mapping[str, Any]) -> _StagedPatch:
        """Apply *patch* to the staged copy: its files (else its diff), then deletions.

        Each path is checked before it is touched; an unsafe or unwritable
        one is skipped and reported instead of aborting the rest of the patch.
        """
        staged = _StagedPatch()
        files: Mapping[str, str] = patch.get("files") or {}
        diff: str = patch.get("diff_unified") or ""
        if files:
            staged.write(sandbox_dir, files)
        elif diff:
            parsed = DockerSandbox._parse_diff_to_files(diff)
            if parsed:
                staged.write(sandbox_dir, parsed)
                logger.info("Wrote %d files from diff", len(staged.files))
            else:
                staged.error = DockerSandbox._apply_diff(sandbox_dir, diff)
                if not staged.error:
                    staged.files = {
                        rel_path: (sandbox_dir / rel_path).read_text(
                            encoding="utf-8", errors="replace"
                        )
                        for rel_path in _diff_target_paths(diff)
                        if (sandbox_dir / rel_path).is_file()
                    }
        for file_path in patch.get("delete_files") or []:
            staged.delete(sandbox_dir, file_path)
        return staged

    @staticmethod
    def _apply_diff(sandbox_dir: Path, diff: str) -> str:
        """Apply *diff* with ``patch``; return an error message, or "" on success.

        ``--forward`` refuses an already-applied hunk; ``--batch`` alone would
        silently apply it in reverse.
        """
        try:
            result = subprocess.run(
                ["patch", "-p1", "--forward", "--batch"],
                input=diff,
                cwd=sandbox_dir,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            return f"Patch failed to apply: {exc}"
        if result.returncode != 0:
            return f"Patch failed to apply:\n{result.stdout}{result.stderr}"
        return ""

    def _resolve_toolchain(self, sandbox_dir: Path, patch: Mapping[str, Any]) -> _Toolchain:
        """Choose the language, image and commands for this run.

        The configured commands (built for ``self._language``, Python when
        none was detected) are kept unless the staged code is in another
        language — in practice a greenfield project — whose toolchain rule
        and image then replace them.  An explicit image is never switched.
        Patch-supplied ``test_commands`` always win and are sanitized
        (raises :class:`UnsafeCommandError`).
        """
        base_lang = self._language or "python"
        detected = _detect_sandbox_language(
            sandbox_dir, patch.get("files") or {}, hint=self._language
        )
        lang, image = base_lang, self._image
        test_cmds, install_cmds = list(self._test_commands), list(self._install_commands)
        if detected is not None and detected[0] != base_lang:
            lang, det_test, det_install = detected
            test_cmds = [det_test]
            install_cmds = [det_install] if det_install else []
            if not self._image_explicit:
                image = select_docker_image(lang) or self._image
            logger.info("Auto-detected %s project — using %s with: %s", lang, image, det_test)
        if lang == "python":
            install_cmds = _with_python_manifest_install(sandbox_dir, install_cmds)
        if _is_windows_image(image):
            # apt-get system installs only work in Debian-based Linux images.
            install_cmds = [c for c in install_cmds if not is_container_only_install(c)]
        if patch.get("test_commands"):
            test_cmds = [sanitize_shell_command(c) for c in patch["test_commands"]]
        return _Toolchain(lang, image, test_cmds, install_cmds)

    def _test_staged(
        self,
        staging_dir: Path,
        patch: dict[str, Any],
        written: dict[str, str],
        start_time: float,
    ) -> dict[str, Any]:
        """Resolve the toolchain, gate on parse errors, then install and test."""
        # Post-patch fixups: sanitize generated config before install/test
        _strip_stdlib_from_requirements(staging_dir)
        _fix_jest_testmatch(staging_dir)
        try:
            toolchain = self._resolve_toolchain(staging_dir, patch)
        except UnsafeCommandError as exc:
            return _failure_report(str(exc), start_time)

        if not self._docker_available():
            gate = run_parse_checks(staging_dir, written)
            report = _gate_failure(gate, start_time) or self._run_local(
                staging_dir, patch, start_time
            )
            return _with_lint_warnings(report, gate)

        # Auto-switch Docker mode if needed (Windows ↔ Linux)
        is_windows = _is_windows_image(toolchain.image)
        needed_os = "windows" if is_windows else "linux"
        current_os = _get_docker_os()
        switched_mode = False
        if current_os not in (needed_os, "unknown"):
            switched_mode = _switch_docker_mode(needed_os)
            if not switched_mode:
                return _failure_report(
                    f"Docker is in {current_os} mode but image "
                    f"{toolchain.image} needs {needed_os} containers. "
                    f"Cannot auto-switch (other containers may be "
                    f"running). Switch manually via Docker Desktop.",
                    start_time,
                )
        try:
            gate = self._run_parse_gate(staging_dir, written, toolchain)
            report = _gate_failure(gate, start_time) or self._run_phases(
                staging_dir, patch, toolchain, start_time
            )
            return _with_lint_warnings(report, gate)
        finally:
            # Switch Docker back to Linux if we switched to Windows
            if switched_mode and is_windows:
                _switch_docker_mode("linux")

    def _run_parse_gate(
        self, staging_dir: Path, written: dict[str, str], toolchain: _Toolchain
    ) -> ParseCheckResult:
        """Parse-check the written files with the run's image (see parse_check.py).

        Runs after the Docker mode switch so Windows images work.  The
        PSScriptAnalyzer volume, once created, is removed by cleanup().
        """
        image = toolchain.image
        analyzer = self._ps_analyzers.setdefault(
            image, PSAnalyzerCache(image, owner=self._instance_id)
        )
        python_project = toolchain.language == "python"
        # Gate containers carry this sandbox's label, so cleanup() sweeps any left behind.
        self._started_containers = True
        gate = run_parse_checks(
            staging_dir,
            written,
            image,
            python_image=image if python_project else None,
            ps_analyzer=analyzer,
            owner=self._instance_id,
            incidental_python=not python_project,
        )
        if analyzer.status != "pending":
            self._cached_volumes.add(analyzer.volume)
        return gate

    def _run_phases(
        self,
        staging_dir: Path,
        patch: dict[str, Any],
        toolchain: _Toolchain,
        start_time: float,
    ) -> dict[str, Any]:
        """Install dependencies (networked, cached) and run the tests (offline)."""
        image, lang = toolchain.image, toolchain.language
        install_cmds, test_cmds = toolchain.install_commands, toolchain.test_commands
        is_windows = _is_windows_image(image)

        pull_info = self._ensure_image_pulled(image)
        image_size = self._get_image_size(image)
        if not is_windows:
            missing = self._missing_tool(image, install_cmds or test_cmds)
            if missing:
                return _failure_report(
                    f"toolchain '{missing}' for {lang} not available in {image}", start_time
                )

        workspace_mount, shell_cmd, cmd_join = _shell_for(is_windows, install_cmds + test_cmds)
        env_prefix = "" if is_windows or lang != "python" else _VENV_PREFIX
        run_args = _container_args(staging_dir, workspace_mount, lang)
        # install_cmds come from the factory and toolchain rules (trusted
        # infrastructure, never agent output), so they are not sanitized.
        logger.info("Sandbox install_cmds: %s", install_cmds)

        cache_key = _compute_cache_key(image, install_cmds, staging_dir)
        cache_tag = self._cache_tag(cache_key)
        use_cached = self._cache_usable(cache_tag)

        # For languages with workspace-local deps (node_modules, vendor),
        # mount a Docker named volume OVER the bind-mounted workspace at
        # the dep dir path.  The volume persists across containers, so
        # iteration 2 sees the dep dir populated from iteration 1 — no
        # symlink-through-bind-mount fragility.
        dep_volume_args, dep_volume_names = _cache_volume_mounts(
            lang, f"{self._instance_id}-{cache_key}", workspace_mount, is_windows
        )
        run_args.extend(dep_volume_args)
        self._cached_volumes.update(dep_volume_names)

        # With a cached image the deps are installed system-wide (no venv).
        test_image, test_prefix = (cache_tag, "") if use_cached else (image, env_prefix)
        install_ms = 0.0
        try:
            if install_cmds and not use_cached:
                # Phase 1: install deps WITH network.
                install_start = time.monotonic()
                test_image, test_prefix, dep_cache_args = self._install(
                    run_args,
                    image,
                    shell_cmd,
                    cmd_join,
                    install_cmds,
                    cache_tag=cache_tag,
                    env_prefix=env_prefix,
                    toolchain_retry=not is_windows and lang == "python",
                    dep_cache_dirs=() if is_windows else _DEP_CACHE_DIRS.get(lang, ()),
                )
                run_args = [*run_args, *dep_cache_args]
                install_ms = (time.monotonic() - install_start) * 1000
            elif use_cached:
                logger.info("Docker: skipping install phase — using cached image %s", cache_tag)

            # Phase 2: run tests WITHOUT network.
            setup_parts = [test_prefix] if test_prefix else []
            offline_env = ["-e", "CARGO_NET_OFFLINE=true"] if lang == "rust" else []
            test_container = [
                *run_args,
                *offline_env,
                "--network",
                "none",
                test_image,
                *shell_cmd,
            ]
            targeted_cmds, _ = select_targeted_tests(patch.get("targeted_tests") or [], test_cmds)
            targeted = self._run_targeted(test_container, setup_parts, cmd_join, targeted_cmds)

            logger.info("Docker: creating sandbox container (no network, image=%s)", test_image)
            test_start = time.monotonic()
            _, result = self._docker_run(
                "test", [*test_container, cmd_join.join([*setup_parts, *test_cmds])]
            )
            test_ms = (time.monotonic() - test_start) * 1000

            passed = result.returncode == 0
            output = result.stdout + "\n" + result.stderr
            test_results = self._parse_test_output(output)
            fields = per_test_fields(output, passed)
            logger.info(
                "Docker: test phase completed in %.0fms — %s (%d tests)",
                test_ms,
                "PASSED" if passed else "FAILED",
                test_results.get("total", 0),
            )

            # Follow-up runs reuse the test container setup: same image,
            # mounts and env, still without network.
            plan = plan_rerun(patch, test_cmds, passed, fields["failed_tests"])
            entrypoints = entrypoint_paths(patch, staging_dir)
            if is_windows and (plan.ids or entrypoints):
                # Follow-up scripts are bash; Windows containers run cmd/PowerShell.
                logger.info("Docker: skipping flaky rerun and entrypoint runs (Windows image)")
                plan, entrypoints = RerunPlan(skipped="windows_image"), []
            reruns = self._rerun_flaky(test_container, setup_parts, plan)
            entrypoint_runs = self._run_entrypoints(test_container, setup_parts, entrypoints)
        except _InstallFailed as exc:
            logger.warning("Docker: %s", exc)
            return _failure_report(
                f"Dependency install failed:\n{exc.stderr[-_OUTPUT_TAIL:]}",
                start_time,
                stdout=exc.stdout[-_OUTPUT_TAIL:],
            )
        except _ContainerTimeout as exc:
            logger.warning("Docker: %s", exc)
            return _failure_report(
                f"{exc}; the container was removed.\n{exc.stderr[-_OUTPUT_TAIL:]}".rstrip(),
                start_time,
                stdout=exc.stdout[-_OUTPUT_TAIL:],
            )

        failed_tests = fields["failed_tests"]
        report: dict[str, Any] = SandboxTestReport(
            passed=passed,
            total=test_results.get("total", 0),
            failures=test_results.get("failures", 0),
            stdout=result.stdout[-_OUTPUT_TAIL:],
            stderr=result.stderr[-_OUTPUT_TAIL:],
            duration_ms=(time.monotonic() - start_time) * 1000,
            flaky_tests=reruns.flaky,
            entrypoint_runs=entrypoint_runs,
            **fields,
        ).model_dump()
        report.update(report_extras(result.stdout, result.stderr, _OUTPUT_TAIL, _OUTPUT_TAIL))
        report["rerun_attempts"] = reruns.attempts
        report["rerun_skipped"] = plan.skipped
        if not passed and flaky_tolerated(
            patch,
            failed_tests,
            reruns.flaky,
            outcomes=parse_pytest_outcomes(output),
            failures=report["failures"],
        ):
            logger.info("Docker: every failure was a pre-existing flaky test — tolerating it")
            mark_flaky_tolerated(report)
        if targeted is not None:
            report["targeted"] = targeted

        # Attach sandbox metadata for event reporting
        report["_sandbox"] = {
            "image": image,
            "image_size": image_size,
            "container_os": "windows" if is_windows else "linux",
            "shell": shell_cmd[0],
            "network_isolation": True,
            "install_network": True,
            "pull": pull_info,
            "install_ms": round(install_ms),
            "test_ms": round(test_ms),
            "cache_hit": use_cached,
            "cached_image": test_image if test_image == cache_tag else None,
            "rerun_attempts": reruns.attempts,
        }
        return report

    def _install(
        self,
        run_args: list[str],
        image: str,
        shell_cmd: list[str],
        cmd_join: str,
        steps: list[str],
        *,
        cache_tag: str,
        env_prefix: str,
        toolchain_retry: bool,
        dep_cache_dirs: tuple[str, ...] = (),
    ) -> tuple[str, str, list[str]]:
        """Install dependencies with network; return the test phase's ``(image, setup, mounts)``.

        The install container is committed as *cache_tag*.  When that
        fails, the dependencies are installed again where the offline test
        container still finds them: Python deps into the bind-mounted venv
        it activates (*env_prefix*), other toolchains' downloads onto named
        volumes over *dep_cache_dirs*, which the test container mounts too
        (*mounts*: extra ``docker run`` arguments).  With
        *toolchain_retry*, an install that failed for want of a C compiler
        is retried once with the build toolchain installed first (its output
        is kept for the report).  Raises :class:`_InstallFailed`.
        """
        container_args = [*run_args, image, *shell_cmd]
        script = cmd_join.join(steps)
        logger.info("Docker: creating container for dependency install (image=%s)", image)
        container, result = self._docker_run("install", [*container_args, script], remove=False)
        if (
            result.returncode != 0
            and toolchain_retry
            and _COMPILER_MISSING.search(result.stdout + result.stderr)
        ):
            logger.info("Docker: install needs a C toolchain — retrying with it")
            self._remove_container(container)
            script = cmd_join.join([_PYTHON_BUILD_TOOLCHAIN, *steps])
            container, result = self._docker_run("install", [*container_args, script], remove=False)
        if result.returncode != 0:
            self._remove_container(container)
            raise _InstallFailed(result)
        if self._commit(container, cache_tag):
            return cache_tag, "", []
        mounts: list[str] = []
        if env_prefix:
            script = cmd_join.join([env_prefix, script])
        elif dep_cache_dirs:
            mounts = self._dep_cache_mounts(cache_tag, dep_cache_dirs)
            logger.info("Docker: keeping downloaded dependencies on volumes instead of the cache")
        else:
            # Workspace-local deps (node_modules, vendor) are on volumes already.
            logger.info("Docker: install not cached — the tests run on the base image")
            return image, "", []
        _, result = self._docker_run("install", [*run_args, *mounts, image, *shell_cmd, script])
        if result.returncode != 0:
            raise _InstallFailed(result)
        return image, env_prefix, mounts

    def _dep_cache_mounts(self, cache_tag: str, directories: tuple[str, ...]) -> list[str]:
        """``-v`` arguments putting each dependency directory on a named volume of this sandbox."""
        mounts: list[str] = []
        for directory in directories:
            volume = _cache_volume_name(cache_tag.removeprefix("delegatron-cache-"), directory)
            self._cached_volumes.add(volume)
            mounts += ["-v", f"{volume}:{directory}"]
        return mounts

    def _run_targeted(
        self,
        test_container: list[str],
        setup_parts: list[str],
        cmd_join: str,
        commands: list[str],
    ) -> dict[str, Any] | None:
        """Run the patch's accepted targeted test commands; ``None`` when there are none."""
        if not commands:
            return None
        logger.info("Docker: running %d targeted test command(s)", len(commands))
        _, result = self._docker_run(
            "targeted", [*test_container, cmd_join.join([*setup_parts, *commands])]
        )
        return targeted_report(result.returncode == 0, result.stdout + "\n" + result.stderr)

    def _rerun_flaky(
        self,
        test_container: list[str],
        setup_parts: list[str],
        plan: RerunPlan,
    ) -> RerunResult:
        """Rerun the plan's failed tests, each time only those still failing.

        Up to ``plan.attempts`` reruns, each in a fresh test container;
        stops early once none is still failing or a rerun proves nothing
        (timeout, usage error).  *test_container* is the ``docker run``
        arguments of the test phase up to the script and *setup_parts* its
        env setup (venv activation, dep symlinks).
        """
        flaky: set[str] = set()
        pending = list(plan.ids)
        attempts = 0
        while pending and attempts < plan.attempts:
            attempts += 1
            logger.info("Docker: rerun %d of %d failed test(s)", attempts, len(pending))
            argv = pytest_rerun_args(pending, plan.prefix)
            script = " && ".join([*setup_parts, shlex.join(argv)])
            try:
                _, rerun = self._docker_run("rerun", [*test_container, script])
            except _ContainerTimeout:
                logger.warning("Docker: flaky-test rerun timed out")
                break
            outcome = rerun_outcome(pending, rerun.returncode, rerun.stdout + "\n" + rerun.stderr)
            if outcome is None:
                break
            flaky.update(outcome[0])
            pending = outcome[1]
        return RerunResult([nodeid for nodeid in plan.ids if nodeid in flaky], attempts)

    def _run_entrypoints(
        self,
        test_container: list[str],
        setup_parts: list[str],
        files: list[str],
    ) -> list[EntrypointRun]:
        """Smoke-run each script in a fresh test container (empty stdin, short timeout)."""
        runs: list[EntrypointRun] = []
        for file in files:
            command = f"timeout {ENTRYPOINT_TIMEOUT} python {shlex.quote(file)} </dev/null"
            logger.info("Docker: smoke-running entrypoint %s", file)
            try:
                _, proc = self._docker_run(
                    "entrypoint",
                    [*test_container, " && ".join([*setup_parts, command])],
                    timeout=ENTRYPOINT_TIMEOUT + _ENTRYPOINT_CONTAINER_GRACE,
                )
            except _ContainerTimeout as exc:
                runs.append(entrypoint_run(file, None, exc.stdout, exc.stderr, timed_out=True))
                continue
            runs.append(
                entrypoint_run(
                    file,
                    proc.returncode,
                    proc.stdout,
                    proc.stderr,
                    timed_out=proc.returncode == _TIMEOUT_EXIT_CODE,
                )
            )
        return runs

    def _run_local(
        self, sandbox_dir: Path, patch: dict[str, Any], start_time: float
    ) -> dict[str, Any]:
        """Return a failed report when Docker is unavailable.

        Running agent-provided commands directly on the host would bypass the
        sandbox boundary, so we refuse and report an error instead.
        """
        logger.error(
            "Docker is not available. Refusing to run commands on the host — "
            "install Docker or use the E2B sandbox instead."
        )
        return _failure_report(
            "Docker is required but not available. "
            "Cannot run tests outside a sandboxed environment.",
            start_time,
        )

    @staticmethod
    def _image_exists(image: str) -> bool:
        """Whether *image* is present in the local Docker image store.

        A slow or unreachable daemon counts as "not present", so a cached
        install image is rebuilt instead of failing the run.
        """
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("Docker: could not inspect image %s: %s", image, exc)
            return False
        return result.returncode == 0

    def _ensure_image_pulled(self, image: str) -> dict[str, Any]:
        """Ensure Docker image is available locally. Pull if needed.

        Returns metadata about the pull operation; a failed or timed-out
        pull is reported there (the install or test phase then fails with
        Docker's own message).
        """
        if self._image_exists(image):
            return {"pulled": False, "image": image, "status": "cached"}

        # Need to pull
        logger.info("Docker: pulling image %s (this may take a while)...", image)
        pull_start = time.monotonic()
        try:
            pull_result = subprocess.run(
                ["docker", "pull", image],
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {
                "pulled": False,
                "image": image,
                "status": "pull_failed",
                "error": str(exc)[-500:],
                "duration_ms": round((time.monotonic() - pull_start) * 1000),
            }
        pull_ms = (time.monotonic() - pull_start) * 1000

        if pull_result.returncode != 0:
            return {
                "pulled": False,
                "image": image,
                "status": "pull_failed",
                "error": pull_result.stderr[-500:],
                "duration_ms": round(pull_ms),
            }

        logger.info("Docker: pulled %s in %.0fms", image, pull_ms)
        return {
            "pulled": True,
            "image": image,
            "status": "pulled",
            "duration_ms": round(pull_ms),
        }

    @staticmethod
    def _get_image_size(image: str) -> str:
        """Get the Docker image size as a human-readable string."""
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", image, "--format", "{{.Size}}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0:
                size_bytes = int(result.stdout.strip())
                if size_bytes > 1_000_000_000:
                    return f"{size_bytes / 1_000_000_000:.1f} GB"
                return f"{size_bytes / 1_000_000:.0f} MB"
        except (ValueError, subprocess.TimeoutExpired):
            pass
        return "unknown"

    def _docker_available(self) -> bool:
        """Check if Docker daemon is running."""
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def _parse_test_output(stdout: str) -> dict[str, int]:
        """Test counts from the runner output (``counts.parse_test_counts``)."""
        total, failures = parse_test_counts(stdout)
        return {"total": total, "failures": failures}

    @staticmethod
    def _parse_diff_to_files(diff: str) -> dict[str, str]:
        """Extract new file contents from a unified diff.

        Handles the common case where the diff creates new files (--- /dev/null).
        For modifications to existing files, returns empty dict to fall back
        to the patch command.
        """
        files: dict[str, str] = {}
        current_path: str | None = None
        current_lines: list[str] = []
        is_new_file = False

        for line in diff.split("\n"):
            if line.startswith("--- "):
                # Check if this is a new file (--- /dev/null)
                is_new_file = line.strip() in ("--- /dev/null", "--- a//dev/null")
            elif line.startswith("+++ "):
                # Save previous file if any
                if current_path and current_lines:
                    files[current_path] = "\n".join(current_lines)
                    if files[current_path] and not files[current_path].endswith("\n"):
                        files[current_path] += "\n"

                # Parse new path (strip b/ prefix)
                path = line[4:].strip()
                if path.startswith("b/"):
                    path = path[2:]
                current_path = path if is_new_file else None
                current_lines = []
            elif line.startswith("@@"):
                continue  # Skip hunk headers
            elif current_path is not None and (line.startswith("+") or line.startswith(" ")):
                current_lines.append(line[1:])
            # Skip lines starting with - (removed lines, shouldn't exist for new files)

        # Save last file
        if current_path and current_lines:
            files[current_path] = "\n".join(current_lines)
            if files[current_path] and not files[current_path].endswith("\n"):
                files[current_path] += "\n"

        return files
