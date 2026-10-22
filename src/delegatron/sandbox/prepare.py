"""Preparing a sandbox run: which install and test commands it runs.

The install list a sandbox is created with describes the workspace at
pipeline start.  :func:`derive_commands` re-derives it on every run so
manifests a patch adds are installed, :func:`select_targeted_tests` decides
which of a patch's ``targeted_tests`` may run, and :func:`step_failure`
words the report for a step that failed before the tests ran.
"""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from delegatron.sandbox.base import UnsafeCommandError, sanitize_shell_command
from delegatron.sandbox.toolchains import canonical_language, host_install_commands
from delegatron.utils.test_output import failed_test_ids

logger = logging.getLogger(__name__)

# Characters of command output kept in failure messages and targeted reports
OUTPUT_TAIL = 2000

_PYTHON_EXECUTABLE = re.compile(r"^python(3(\.\d+)?)?$")

# A command word that names a subcommand ("test" in "npm test"), not an argument
_COMMAND_WORD = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

# Options that pick tests by name: pytest -k/-m, go -run, jest -t, dotnet --filter,
# gradle --tests, maven -Dtest=...
_SELECTING_OPTIONS = frozenset(
    {"-k", "-m", "-run", "--run", "-t", "--testNamePattern", "--filter", "--tests"}
)
# A test target argument: a path, package or node id (no option syntax)
_TARGET_ARG = re.compile(r"^[A-Za-z0-9_.@/\[\]-]+(?:::.+)?$")
# Targets that mean "the whole suite" in common layouts
_WHOLE_SUITE_TARGETS = frozenset({"", ".", "...", "tests", "test", "spec", "__tests__"})


def derive_commands(
    base_install: list[str],
    base_test: list[str],
    root: Path,
    patch_files: Mapping[str, str],
    language: str = "",
) -> tuple[list[str], list[str]]:
    """Return the ``(install, test)`` commands for *root*, which holds the patched project.

    Resolved as the Docker sandbox does: *base_install* / *base_test* were
    built for *language* (the project's detected language; Python when
    none was detected) and are kept unless the patched code is in another
    language — in practice a greenfield project — whose toolchain commands
    then replace them.  For Python, an install for a manifest the commands
    do not cover yet (``pyproject.toml``, ``requirements.txt``,
    ``Pipfile``) goes first.  The local and E2B sandboxes use this, so
    container-only system installs are dropped and module installs go to
    the current user (:func:`~delegatron.sandbox.toolchains.host_install_commands`).
    Install commands are infrastructure and are returned unsanitized;
    patch-supplied ``test_commands`` are the caller's to apply (and
    sanitize) on top.
    """
    # docker_runner owns the toolchain rules and imports this module.
    from delegatron.sandbox.docker_runner import (
        _detect_sandbox_language,
        _with_python_manifest_install,
    )

    base_language = canonical_language(language) or "python"
    detected = _detect_sandbox_language(root, patch_files, hint=language)
    resolved, install, test = base_language, list(base_install), list(base_test)
    if detected is not None and detected[0] != base_language:
        resolved, test_command, install_command = detected
        logger.info("Auto-detected %s project — using: %s", resolved, test_command)
        install, test = ([install_command] if install_command else []), [test_command]
    if resolved == "python":
        install = _with_python_manifest_install(root, install)
    return host_install_commands(install), test


def select_targeted_tests(
    entries: Iterable[Any],
    test_commands: list[str],
) -> tuple[list[str], list[str]]:
    """Split a patch's ``targeted_tests`` into commands to run and rejected entries.

    An entry runs only when :func:`sanitize_shell_command` accepts it as is,
    it starts with the leading command words of one of *test_commands* (the
    project's own test runner; ``python -m X`` and ``X`` count as the same
    runner) and it narrows the suite: it names a test file, node id or
    package, or selects tests by name (``-k``, ``-run``, ``-t``...).  An
    entry that would only rerun the whole suite is rejected.  An accepted
    entry is rewritten into the configured command's form, so ``pytest
    tests/test_x.py`` runs as ``python -m pytest tests/test_x.py`` when the
    project runs ``python -m pytest`` (and imports the same way).
    """
    runners = [(runner, command) for command in test_commands if (runner := _runner(command))]
    accepted: list[str] = []
    rejected: list[str] = []
    for entry in entries:
        command = entry.strip() if isinstance(entry, str) else ""
        words = _command_words(command) if command and _is_safe(command) else []
        match = next(
            (
                (runner, configured)
                for runner, configured in runners
                if tuple(words[: len(runner)]) == runner
            ),
            None,
        )
        if match is None or not _narrows(words[len(match[0]) :], match[1], len(match[0])):
            rejected.append(str(entry))
            continue
        accepted.append(_in_configured_form(command, *match))
    if rejected:
        logger.warning("Rejected targeted test command(s): %s", rejected)
    return accepted, rejected


def targeted_report(passed: bool, output: str) -> dict[str, Any]:
    """The ``targeted`` entry of a sandbox report for the targeted test pass."""
    return {
        "passed": passed,
        "failing_ids": failed_test_ids(output),
        "stdout_tail": output[-OUTPUT_TAIL:],
    }


def step_failure(
    what: str,
    command: str,
    returncode: int | None,
    stdout: str,
    stderr: str,
) -> str:
    """Report text for a step (venv, patch, install) that failed before the tests ran."""
    status = "timed out" if returncode is None else f"exit {returncode}"
    output = "\n".join(part for part in (stdout.strip(), stderr.strip()) if part)
    return f"{what} ({status}): {command}\n{output[-OUTPUT_TAIL:]}".rstrip()


def rejected_paths_note(rejected: list[str]) -> str:
    """Report line for patch paths a sandbox skipped as unsafe (``""`` for none)."""
    return f"Skipped unsafe patch path(s): {', '.join(rejected)}\n" if rejected else ""


def _is_safe(command: str) -> bool:
    try:
        return sanitize_shell_command(command) == command
    except UnsafeCommandError:
        return False


def _command_words(command: str) -> list[str]:
    """Shell words of *command*, with a leading ``python -m`` dropped."""
    try:
        words = shlex.split(command)
    except ValueError:
        return []
    if len(words) > 2 and _PYTHON_EXECUTABLE.match(words[0]) and words[1] == "-m":
        return words[2:]
    return words


def _runner(command: str) -> tuple[str, ...]:
    """Leading command words of a test command: ``pytest``, ``npm test``, ``go test``."""
    words = _command_words(command)
    if not words:
        return ()
    runner = [words[0]]
    for word in words[1:]:
        if not _COMMAND_WORD.match(word):
            break
        runner.append(word)
    return tuple(runner)


def _targets(args: list[str]) -> set[str]:
    """Test-target arguments among *args*, normalized (``./tests/`` → ``tests``)."""
    return {
        arg.removeprefix("./").rstrip("/")
        for arg in args
        if not arg.startswith("-") and _TARGET_ARG.match(arg)
    }


def _narrows(args: list[str], configured: str, runner_len: int) -> bool:
    """Whether a targeted entry's *args* (after the runner) select less than the whole suite.

    Targets the configured command already runs (``tests/`` in ``pytest
    tests/``) and the usual whole-suite targets do not count.
    """
    if any(arg.split("=", 1)[0] in _SELECTING_OPTIONS or arg.startswith("-Dtest=") for arg in args):
        return True
    whole_suite = _WHOLE_SUITE_TARGETS | _targets(_command_words(configured)[runner_len:])
    return any(
        target not in whole_suite and not target.endswith("...") for target in _targets(args)
    )


def _in_configured_form(command: str, runner: tuple[str, ...], configured: str) -> str:
    """*command* with its runner words spelled as in *configured*.

    ``pytest tests/test_x.py`` becomes ``python -m pytest tests/test_x.py``
    when the project runs ``python -m pytest -v`` (and the other way round),
    so the targeted run imports the way the full run does.  An entry with
    quotes is kept as it is (its words could not be joined back faithfully).
    """
    entry_words, configured_words = _shell_words(command), _shell_words(configured)
    entry_lead = _lead_length(entry_words, runner)
    configured_lead = _lead_length(configured_words, runner)
    if entry_words[:entry_lead] == configured_words[:configured_lead] or any(
        quote in command for quote in "'\""
    ):
        return command
    return " ".join([*configured_words[:configured_lead], *entry_words[entry_lead:]])


def _shell_words(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _lead_length(words: list[str], runner: tuple[str, ...]) -> int:
    """Number of leading *words* that spell *runner* (with or without ``python -m``)."""
    python_m = len(words) > 2 and bool(_PYTHON_EXECUTABLE.match(words[0])) and words[1] == "-m"
    return len(runner) + (2 if python_m else 0)
