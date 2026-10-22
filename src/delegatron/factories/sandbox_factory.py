"""Centralized sandbox creation factory.

Replaces the duplicated sandbox selection logic in graph.py and
async_runner.py with a single creation point. Also defines the
valid sandbox types for server/app.py validation.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from delegatron.sandbox.base import BaseSandbox
from delegatron.sandbox.toolchains import (
    CARGO_FETCH,
    PIPENV_INSTALL,
    PYTEST_INSTALL,
    pyproject_has_test_extra,
    rule_for_language,
    select_docker_image,
)

logger = logging.getLogger(__name__)

# Valid sandbox type identifiers.
# Used by server/app.py for request validation.
SANDBOX_TYPES = frozenset({"docker", "e2b", "none", "local"})

# Regex pattern for Pydantic Field validation in server models.
SANDBOX_PATTERN = r"^(docker|e2b|none|local)$"


class SandboxUnavailableError(RuntimeError):
    """The configured sandbox cannot run on this machine (e.g. no Docker CLI)."""


def create_sandbox(
    sandbox_type: str,
    workspace_root: Path,
    test_commands: list[str],
    install_commands: list[str],
    docker_image: str = "",
    primary_language: str = "",
    run_entrypoints: bool | None = None,
) -> BaseSandbox:
    """Create a sandbox instance based on the configured type.

    Args:
        sandbox_type: One of "docker", "e2b", "local" or "none" (local).
        workspace_root: Path to the project workspace.
        test_commands: Commands to run tests in the sandbox.
        install_commands: Commands to install dependencies.
        docker_image: Explicit Docker image override (wins over auto-selection).
        primary_language: Detected language name (python/javascript/go/...).
            Used to auto-select an image from DOCKER_IMAGES when
            ``docker_image`` is not set, and handed to every sandbox as the
            language its commands were built for (so a patch touching an
            incidental file of another language does not swap them).
        run_entrypoints: ``PipelineConfig.run_entrypoints``.  The local
            sandbox, which runs on the host, smoke-runs generated scripts
            only when it is explicitly ``True``; ``None`` (the default)
            leaves them to the isolated sandboxes.

    Returns:
        Configured sandbox instance.

    Raises:
        SandboxUnavailableError: a Docker sandbox is requested but the
            ``docker`` CLI is not on PATH (checked before any run starts).
    """
    kwargs: dict[str, Any] = {
        "workspace_root": workspace_root,
        "test_commands": test_commands,
        "install_commands": install_commands,
    }

    if sandbox_type in ("none", "local"):
        from delegatron.sandbox.local_runner import LocalSandbox

        logger.info("Using local sandbox (no container isolation)")
        return LocalSandbox(
            **kwargs, language=primary_language, run_entrypoints=run_entrypoints is True
        )

    if sandbox_type == "e2b":
        from delegatron.sandbox.e2b_runner import E2BSandbox

        logger.info("Using E2B sandbox")
        return E2BSandbox(**kwargs, language=primary_language)

    # Default: Docker sandbox
    if shutil.which("docker") is None:
        raise SandboxUnavailableError(
            "The Docker sandbox needs the docker CLI, which is not on PATH; "
            "install Docker or choose the e2b or local sandbox"
        )
    from delegatron.sandbox.docker_runner import DockerSandbox

    # Explicit override wins (and is never switched per patch); otherwise
    # auto-select by detected language.
    kwargs["language"] = primary_language
    kwargs["image_explicit"] = bool(docker_image)
    resolved_image = docker_image or select_docker_image(primary_language)
    if resolved_image:
        kwargs["image"] = resolved_image
        if docker_image:
            logger.info("Using Docker sandbox with custom image: %s", resolved_image)
        else:
            logger.info(
                "Using Docker sandbox with auto-selected image for %s: %s",
                primary_language or "default",
                resolved_image,
            )
    else:
        logger.info("Using Docker sandbox")
    return DockerSandbox(**kwargs)


def get_install_commands(project_info: Any, workspace_root: Path) -> list[str]:
    """Generate install commands based on detected project.

    Package managers with a dedicated install step get it; anything else
    falls back to the primary language's toolchain install, treating a
    project with no detected language as Python.
    """
    commands: list[str] = []
    pm = getattr(project_info, "package_manager", None) if project_info else None
    languages = list(getattr(project_info, "languages", None) or []) if project_info else []
    if pm == "pip":
        req_file = workspace_root / "requirements.txt"
        pyproject = workspace_root / "pyproject.toml"
        if pyproject.exists() and pyproject_has_test_extra(pyproject):
            # Prefer editable install with test extras over requirements.txt
            commands.append('pip install -e ".[test]" -q')
        elif req_file.exists():
            commands.append("pip install -r requirements.txt -q")
        elif pyproject.exists():
            commands.append("pip install -e . -q")
        # Always ensure pytest is available for Python projects
        commands.append(PYTEST_INSTALL)
    elif pm == "pipenv":
        commands.extend([*PIPENV_INSTALL, PYTEST_INSTALL])
    elif pm in ("npm", "yarn", "pnpm", "bun"):
        commands.append(f"{pm} install")
    elif pm == "dotnet":
        commands.append("dotnet restore")
    elif pm == "go":
        commands.append("go mod download")
    elif pm == "cargo":
        # The test phase runs offline, so crates must be fetched here.
        commands.append(CARGO_FETCH)
    elif pm in ("maven",):
        # Pre-warm the local ~/.m2 repository so the test phase (which runs
        # with --network none) has every jar it needs:
        #   1. dependency:go-offline — declared deps + build plugins
        #   2. `mvn -B -q test -DskipTests` with a one-liner dummy test class —
        #      forces surefire to resolve its test-framework provider
        #      (surefire-junit-platform for JUnit 5), which go-offline
        #      DOESN'T catch because it's loaded lazily based on the
        #      detected framework.  Remove the dummy test afterwards so it
        #      doesn't interfere with the real test sources that arrive in
        #      the patch phase.
        commands.append("mvn -B -q dependency:go-offline")
        # Name must match surefire's default include pattern (**/*Test.java
        # or **/Test*.java) — otherwise surefire finds no tests, skips
        # provider loading, and doesn't download surefire-junit-platform.
        commands.append(
            "mkdir -p src/test/java && "
            "printf 'import org.junit.jupiter.api.Test;"
            "public class DelegatronPrewarmTest{@Test void p(){}}' "
            "> src/test/java/DelegatronPrewarmTest.java && "
            "mvn -B -q test || true; "
            "rm -f src/test/java/DelegatronPrewarmTest.java"
        )
    elif pm in ("gradle",):
        commands.append("gradle dependencies")
    elif pm == "bundler":
        commands.append("bundle install")
    elif pm == "composer":
        commands.append("composer install")
    else:
        rule = rule_for_language(languages[0] if languages else "python")
        if rule is not None and rule.install_command:
            commands.append(rule.install_command)
    return commands
