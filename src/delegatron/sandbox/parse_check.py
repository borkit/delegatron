"""Per-language parse-check gate.

Runs lightweight syntax validation on every file the implementer wrote
BEFORE the sandbox spends time installing deps or running tests.

Why this exists:
    Pester / pytest / jest idioms often dot-source or import individual
    functions, bypassing full-file parsing.  A file with `Set-StrictMode`
    before `param()` (a real case) parses fine when you call its functions
    piecemeal but dies on `pwsh -File script.ps1`.  The reviewer sees a
    green test run and approves.  This gate closes that hole.

What blocks (:attr:`ParseCheckResult.errors`, terminal for the iteration):
    parser errors, Error-severity PSScriptAnalyzer findings, and the
    Warning-severity correctness rules in ``BLOCKING_PS_RULES``.

What does not (:attr:`ParseCheckResult.advisories`, reported, never blocking):
    every other PSScriptAnalyzer Warning (``[lint] <path>: [<Rule>] <msg>``);
    Python findings the host parser cannot vouch for: from a host
    interpreter older than the sandbox's, or for incidental Python files in
    a project of another language, whose Python version is unknown; and a
    note whenever a check could not run (the PowerShell parser pass, or
    PSScriptAnalyzer when it could not be installed).  A Python check that
    cannot run inside the image falls back to the host parser.
    ``DROPPED_PS_RULES`` (file encoding, which the orchestrator decides) are
    not reported at all.

Supported:
    .py, .pyi          — compiled by the Python inside *python_image*
                         (``--network none``), i.e. the grammar the tests run
                         with; else ``ast.parse`` on the host, using the
                         grammar version named by *image*'s ``python:X.Y`` tag
    .ps1, .psm1, .psd1 — parser + PSScriptAnalyzer inside *image*
                         (``--network none``); with a :class:`PSAnalyzerCache`
                         the analyzer is installed once into a named volume

With an *owner* (the sandbox instance), every gate container is named and
labelled like the sandbox's own containers, so the sandbox's cleanup removes
one left behind; a gate container that times out is removed right away.

Future: node --check, tsc --noEmit, go vet.
"""

from __future__ import annotations

import ast
import hashlib
import logging
import re
import subprocess
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from delegatron.utils.paths import normalize_rel_path

logger = logging.getLogger(__name__)

_PYTHON_EXTS: frozenset[str] = frozenset({".py", ".pyi"})
_POWERSHELL_EXTS: frozenset[str] = frozenset({".ps1", ".psm1", ".psd1"})

# Warning-severity PSScriptAnalyzer rules that flag defects, not style: they block.
BLOCKING_PS_RULES: frozenset[str] = frozenset({"PSAvoidUsingWMICmdlet", "PSUseCmdletCorrectly"})
# Rules about file encoding, which the orchestrator controls, so the model cannot act on them.
DROPPED_PS_RULES: frozenset[str] = frozenset({"PSUseBOMForUnicodeEncodedFile"})

# Gate containers may pull the image on first use.
_GATE_TIMEOUT = 300

# Label key the Docker sandbox puts on its containers (see docker_runner._SANDBOX_LABEL)
_SANDBOX_LABEL = "delegatron.sandbox"

_PYTHON_TAG = re.compile(r"(?:^|/)python:(\d+)\.(\d+)")

# Compiles each file named on the command line as the sandbox's Python would
# (bytes as on disk, so encoding problems surface too).
_PY_COMPILE_SCRIPT = (
    "import sys\n"
    "for p in sys.argv[1:]:\n"
    "    try:\n"
    "        with open(p, 'rb') as f:\n"
    "            compile(f.read(), p, 'exec', dont_inherit=True)\n"
    "    except SyntaxError as e:\n"
    "        print('PARSE %s|%s at line %s, col %s' % (p, e.msg, e.lineno, e.offset))\n"
    "    except (OSError, ValueError) as e:\n"
    "        print('PARSE %s|%s' % (p, e))\n"
    "print('DONE')\n"
)


@dataclass(frozen=True)
class ParseError:
    """A single parse failure found by the gate."""

    path: str
    language: str
    message: str

    def format(self) -> str:
        return f"{self.language} parse error in {self.path}:\n{self.message}"


@dataclass
class ParseCheckResult:
    """Outcome of the gate: blocking ``errors`` and non-blocking ``advisories``."""

    errors: list[ParseError] = field(default_factory=list)
    advisories: list[str] = field(default_factory=list)

    def extend(self, other: ParseCheckResult) -> None:
        self.errors.extend(other.errors)
        self.advisories.extend(other.advisories)


@dataclass
class PSAnalyzerCache:
    """PSScriptAnalyzer kept in a named Docker volume for one sandbox image.

    The first gate run that needs it installs the module into the volume
    (the only gate container with network access); later runs mount the
    volume read-only and stay offline.  If the install fails the gate keeps
    to the parser pass.  The owner removes :attr:`volume` when it cleans up,
    so the name includes *owner* (the sandbox instance): another run's
    cleanup never removes a volume this one still uses.
    """

    image: str
    owner: str = ""
    status: Literal["pending", "ready", "unavailable"] = "pending"
    # Why the install failed (for the advisory), when status is "unavailable"
    reason: str = ""

    @property
    def volume(self) -> str:
        digest = hashlib.sha256(self.image.encode("utf-8")).hexdigest()[:12]
        return f"delegatron-psmodules-{digest}" + (f"-{self.owner}" if self.owner else "")


def run_parse_checks(
    sandbox_dir: Path,
    patch_files: Mapping[str, str],
    image: str | None = None,
    *,
    python_image: str | None = None,
    ps_analyzer: PSAnalyzerCache | None = None,
    owner: str = "",
    incidental_python: bool = False,
) -> ParseCheckResult:
    """Run all available parse checks against the implementer's patch.

    Args:
        sandbox_dir: Absolute path to the sandbox workspace, with the patch
            files already written.
        patch_files: Mapping of relative path → file content (from PatchSet).
        image: Sandbox Docker image, used for PowerShell checks and for the
            Python grammar version on the host.  If None, PowerShell checks
            are skipped (with an advisory) and Python uses the host grammar.
        python_image: Image whose ``python`` compiles the Python files (the
            sandbox image, or its cached install image).  If None, or if it
            has no Python, Python files are parsed on the host.
        ps_analyzer: Per-sandbox PSScriptAnalyzer volume; without one the
            analyzer runs only if *image* already has it.
        owner: Sandbox instance id; gate containers are then named and
            labelled for its cleanup.
        incidental_python: The project is in another language, so its
            Python files run with an unknown Python: host parser findings
            without a known target version are advisories.

    Returns:
        A :class:`ParseCheckResult`; no ``errors`` means everything parses.
    """
    result = ParseCheckResult()
    python_files: dict[str, str] = {}
    ps_paths: list[str] = []
    for rel_path, content in patch_files.items():
        ext = Path(rel_path).suffix.lower()
        if ext in _PYTHON_EXTS:
            python_files[rel_path] = content
        elif ext in _POWERSHELL_EXTS and (normalized := normalize_rel_path(rel_path)):
            ps_paths.append(normalized)

    if python_files:
        result.extend(
            _check_python_files(
                sandbox_dir,
                python_files,
                image,
                python_image,
                owner=owner,
                incidental=incidental_python,
            )
        )
    if ps_paths and image:
        result.extend(_check_powershell_batch(sandbox_dir, ps_paths, image, ps_analyzer, owner))
    elif ps_paths:
        result.advisories.append("PowerShell parse-check skipped: no sandbox image")
    return result


# --------------------------------------------------------------------------- #
# Python
# --------------------------------------------------------------------------- #


def _check_python(
    path: Path,
    content: str,
    feature_version: tuple[int, int] | None = None,
) -> ParseError | None:
    try:
        ast.parse(content, filename=str(path), feature_version=feature_version)
        return None
    except SyntaxError as e:
        loc = f"line {e.lineno}, col {e.offset}" if e.lineno else "?"
        return ParseError(
            path=path.as_posix(),
            language="Python",
            message=f"{e.msg} at {loc}",
        )


def _check_python_files(
    sandbox_dir: Path,
    files: dict[str, str],
    image: str | None,
    python_image: str | None,
    *,
    owner: str = "",
    incidental: bool = False,
) -> ParseCheckResult:
    if python_image:
        paths = [p for p in map(normalize_rel_path, files) if p]
        errors = _check_python_in_image(sandbox_dir, paths, python_image, owner)
        if errors is not None:
            return ParseCheckResult(errors=errors)
    return _check_python_on_host(
        files, _python_version(image or python_image), incidental=incidental
    )


def _check_python_in_image(
    sandbox_dir: Path,
    rel_paths: list[str],
    image: str,
    owner: str = "",
) -> list[ParseError] | None:
    """Compile *rel_paths* with the image's Python; ``None`` when that cannot run."""
    windows = _is_windows(image)
    program = ["python" if windows else "python3", "-I", "-c", _PY_COMPILE_SCRIPT, *rel_paths]
    result = _docker(_gate_command(sandbox_dir, image, program, windows=windows, owner=owner))
    if isinstance(result, str) or "DONE" not in result.stdout.splitlines():
        reason = result if isinstance(result, str) else _first_line(result)
        logger.info("In-image Python parse-check unavailable (%s); using the host parser", reason)
        return None
    return [
        ParseError(path=path, language="Python", message=message)
        for path, message in _tagged(result.stdout, "PARSE")
    ]


def _check_python_on_host(
    files: dict[str, str],
    target: tuple[int, int] | None,
    *,
    incidental: bool = False,
) -> ParseCheckResult:
    """Parse with the host's ``ast``, using the sandbox's grammar version when known.

    A host older than the sandbox cannot parse every newer construct, so its
    findings are only advisories then; so are findings for *incidental*
    Python files whose Python version is unknown.
    """
    errors = [
        err
        for rel_path, content in files.items()
        if (err := _check_python(Path(rel_path), content, target)) is not None
    ]
    host = sys.version_info[:2]
    host_version = ".".join(map(str, host))
    if target is None and incidental:
        return ParseCheckResult(
            advisories=[
                f"[parse] {err.path}: {err.message} (checked with host Python {host_version}; "
                "the project's Python version is unknown; not blocking)"
                for err in errors
            ]
        )
    if target is None or host >= target:
        return ParseCheckResult(errors=errors)
    target_version = ".".join(map(str, target))
    return ParseCheckResult(
        advisories=[
            f"[parse] {err.path}: {err.message} (host Python {host_version} is older "
            f"than the sandbox's {target_version}; not blocking)"
            for err in errors
        ]
    )


def _python_version(image: str | None) -> tuple[int, int] | None:
    """The Python version an image tag names: ``python:3.12-slim`` → ``(3, 12)``."""
    match = _PYTHON_TAG.search(image or "")
    return (int(match[1]), int(match[2])) if match else None


# --------------------------------------------------------------------------- #
# PowerShell
# --------------------------------------------------------------------------- #


def _check_powershell_batch(
    sandbox_dir: Path,
    rel_paths: list[str],
    image: str,
    analyzer: PSAnalyzerCache | None,
    owner: str = "",
) -> ParseCheckResult:
    """Validate all PowerShell files in one offline container.

    Runs two layers inside the container:

    1. Parser pass — tokenizer/AST check via
       ``[System.Management.Automation.Language.Parser]::ParseFile``.
       Catches genuine syntax errors.
    2. PSScriptAnalyzer pass — when the module is available, runs
       ``Invoke-ScriptAnalyzer -Severity Warning,Error`` against each file.
       Catches semantic bugs the parser accepts; findings are classified by
       :func:`_classify_powershell_output`.

    Output contract — one line per finding:
        ``PARSE <path>|<msg>`` · ``LINT <path>|<rule>|<msg>`` (Warning) ·
        ``LINTERR <path>|<rule>|<msg>`` (Error) · ``OK <path>``
    """
    if analyzer is not None and analyzer.status == "pending":
        _install_analyzer(analyzer)
    volume = analyzer.volume if analyzer is not None and analyzer.status == "ready" else None
    windows = _is_windows(image)
    script = _powershell_gate_script(rel_paths, _module_dir(windows) if volume else None)
    program = ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script]
    command = _gate_command(
        sandbox_dir, image, program, windows=windows, volume=volume, owner=owner
    )
    result = _docker(command)
    if isinstance(result, str) or (result.returncode != 0 and not result.stdout.strip()):
        reason = result if isinstance(result, str) else _first_line(result)
        logger.warning("PowerShell parse-check skipped (image=%s): %s", image, reason)
        return ParseCheckResult(advisories=[f"PowerShell parse-check skipped: {reason}"])
    checked = _classify_powershell_output(result.stdout)
    if analyzer is not None and analyzer.status == "unavailable":
        checked.advisories.append(
            "PowerShell lint skipped: PSScriptAnalyzer could not be installed"
            + (f" ({analyzer.reason})" if analyzer.reason else "")
            + "; the files got the parser check only"
        )
    return checked


def _classify_powershell_output(stdout: str) -> ParseCheckResult:
    """Sort the gate's ``PARSE``/``LINT``/``LINTERR`` lines into errors and advisories."""
    result = ParseCheckResult()
    for path, message in _tagged(stdout, "PARSE"):
        result.errors.append(ParseError(path=path, language="PowerShell", message=message))
    for tag in ("LINTERR", "LINT"):
        for path, body in _tagged(stdout, tag):
            rule, sep, message = body.partition("|")
            rule, message = (rule.strip(), message.strip()) if sep else ("Unknown", body)
            if rule in DROPPED_PS_RULES:
                continue
            if tag == "LINTERR" or rule in BLOCKING_PS_RULES:
                result.errors.append(
                    ParseError(path=path, language="PowerShell", message=f"[{rule}] {message}")
                )
            else:
                result.advisories.append(f"[lint] {path}: [{rule}] {message}")
    return result


def _install_analyzer(analyzer: PSAnalyzerCache) -> None:
    """Save PSScriptAnalyzer into the analyzer volume (networked, once per sandbox)."""
    module_dir = _module_dir(_is_windows(analyzer.image))
    script = (
        "$ErrorActionPreference='Stop'; "
        f"$env:PSModulePath = '{module_dir}' + [IO.Path]::PathSeparator + $env:PSModulePath; "
        "if (-not (Get-Module -ListAvailable PSScriptAnalyzer)) { "
        f"Save-Module PSScriptAnalyzer -Path '{module_dir}' -Repository PSGallery -Force "
        "}"
    )
    mount = f"{analyzer.volume}:{module_dir}"
    program = ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script]
    identity = _container_identity(analyzer.owner)
    result = _docker(["docker", "run", "--rm", *identity, "-v", mount, analyzer.image, *program])
    if isinstance(result, str) or result.returncode != 0:
        reason = result if isinstance(result, str) else _first_line(result)
        logger.warning(
            "PSScriptAnalyzer install failed (image=%s): %s; PowerShell gets the parser pass only",
            analyzer.image,
            reason,
        )
        analyzer.status = "unavailable"
        analyzer.reason = reason[:200]
    else:
        analyzer.status = "ready"


def _powershell_gate_script(rel_paths: list[str], module_dir: str | None) -> str:
    # Single-quoted literals: no $-interpolation; embedded quotes are doubled.
    files_literal = ",".join("'" + path.replace("'", "''") + "'" for path in rel_paths)
    module_path = (
        f"$env:PSModulePath = '{module_dir}' + [IO.Path]::PathSeparator + $env:PSModulePath; "
        if module_dir
        else ""
    )
    return (
        "$ErrorActionPreference='Continue'; "
        f"{module_path}"
        "Import-Module PSScriptAnalyzer -ErrorAction SilentlyContinue | Out-Null; "
        f"$files=@({files_literal}); "
        "foreach ($f in $files) { "
        "  $full = (Resolve-Path -LiteralPath $f).Path; "
        "  $parseErrors = $null; "
        "  [System.Management.Automation.Language.Parser]::ParseFile("
        "    $full, [ref]$null, [ref]$parseErrors"
        "  ) | Out-Null; "
        "  if ($parseErrors -and $parseErrors.Count -gt 0) { "
        "    foreach ($e in $parseErrors) { "
        "      Write-Output "
        '        ("PARSE $f|line $($e.Extent.StartLineNumber): " + '
        '         "$($e.Message)") '
        "    } "
        "    continue "
        "  } "
        "  if (Get-Command Invoke-ScriptAnalyzer -ErrorAction SilentlyContinue) { "
        # Exclude noisy rules that fire on legitimate CLI script patterns.
        # Write-Host is intentional for colored console output in scripts;
        # unused-var false-positives trip on toy examples.
        "    $lint = Invoke-ScriptAnalyzer -Path $full -Severity Warning,Error "
        "            -ExcludeRule PSAvoidUsingWriteHost,"
        "PSUseDeclaredVarsMoreThanAssignments,"
        "PSReviewUnusedParameter,"
        "PSUseShouldProcessForStateChangingFunctions "
        "            -ErrorAction SilentlyContinue; "
        "    foreach ($r in $lint) { "
        "      $tag = if ($r.Severity -eq 'Error') { 'LINTERR' } else { 'LINT' }; "
        "      Write-Output "
        '        ("$tag $f|$($r.RuleName)|line $($r.Line): " + '
        '         "$($r.Message)") '
        "    } "
        "  } "
        '  Write-Output "OK $f" '
        "}"
    )


# --------------------------------------------------------------------------- #
# Docker plumbing
# --------------------------------------------------------------------------- #


def _module_dir(windows: bool) -> str:
    return "C:\\delegatron-psmodules" if windows else "/opt/delegatron-psmodules"


def _is_windows(image: str) -> bool:
    # docker_runner imports this module, so its helper is imported on use.
    from delegatron.sandbox.docker_runner import _is_windows_image

    return _is_windows_image(image)


def _gate_command(
    sandbox_dir: Path,
    image: str,
    program: list[str],
    *,
    windows: bool,
    volume: str | None = None,
    owner: str = "",
) -> list[str]:
    """``docker run`` for a gate check: no network, the workspace mounted read-only."""
    workspace = "C:\\workspace" if windows else "/workspace"
    host_dir = str(sandbox_dir).replace("\\", "/")
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        *_container_identity(owner),
        "-v",
        f"{host_dir}:{workspace}:ro",
    ]
    if volume:
        cmd += ["-v", f"{volume}:{_module_dir(windows)}:ro"]
    return [*cmd, "-w", workspace, image, *program]


def _container_identity(owner: str) -> list[str]:
    """``--name``/``--label`` for a gate container of sandbox *owner* (none without one)."""
    if not owner:
        return []
    name = f"delegatron-{owner}-gate-{uuid.uuid4().hex[:8]}"
    return ["--name", name, "--label", f"{_SANDBOX_LABEL}={owner}"]


def _docker(cmd: list[str]) -> subprocess.CompletedProcess[str] | str:
    """Run a gate container; returns its result, or why it could not start.

    On a timeout only the CLI is killed, so a named container is then
    force-removed (best effort; the owner's cleanup sweeps it otherwise).
    """
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GATE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        if "--name" in cmd:
            _remove_container(cmd[cmd.index("--name") + 1])
        return str(exc)
    except OSError as exc:
        return str(exc)


def _remove_container(name: str) -> None:
    try:
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, text=True, timeout=30, check=False
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Docker: could not remove gate container %s: %s", name, exc)


def _first_line(result: subprocess.CompletedProcess[str]) -> str:
    """First stderr line of a failed container run (or its exit status)."""
    lines = (result.stderr or "").strip().splitlines()
    return lines[0][:300] if lines else f"exit {result.returncode}"


def _tagged(stdout: str, tag: str) -> list[tuple[str, str]]:
    """``(path, rest)`` for each ``<tag> <path>|<rest>`` line of gate output."""
    prefix = f"{tag} "
    found: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            path, _, rest = line[len(prefix) :].partition("|")
            found.append((path.strip(), rest.strip() or "parse failed"))
    return found
