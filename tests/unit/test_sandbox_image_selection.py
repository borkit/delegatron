"""Tests for language-based Docker image auto-selection.

Regression for the "React generation silently runs in python:3.12-slim"
bug: when a JS/TS (or any non-Python) workspace was detected, the Docker
sandbox still ran the Python default image, so `npm install` failed with
`bash: line 1: npm: command not found`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from delegatron.factories import sandbox_factory
from delegatron.factories.sandbox_factory import (
    create_sandbox,
    select_docker_image,
)


@pytest.fixture(autouse=True)
def _docker_cli_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Docker preflight looks for the CLI; these tests never run a container.
    monkeypatch.setattr(sandbox_factory.shutil, "which", lambda name: f"/usr/bin/{name}")


class TestSelectDockerImage:
    def test_python(self) -> None:
        assert select_docker_image("python") == "python:3.12-slim"

    def test_javascript_maps_to_node(self) -> None:
        assert select_docker_image("javascript") == "node:22-slim"

    def test_typescript_maps_to_node(self) -> None:
        assert select_docker_image("typescript") == "node:22-slim"

    def test_go(self) -> None:
        assert select_docker_image("go") == "golang:1.23-bookworm"

    def test_rust(self) -> None:
        assert select_docker_image("rust") == "rust:1.82-slim-bookworm"

    def test_csharp_maps_to_dotnet(self) -> None:
        assert select_docker_image("c#") == "mcr.microsoft.com/dotnet/sdk:9.0"
        assert select_docker_image("csharp") == "mcr.microsoft.com/dotnet/sdk:9.0"

    def test_java(self) -> None:
        assert select_docker_image("java") == "maven:3.9-eclipse-temurin-21"
        # Kotlin also builds via Maven/Gradle, shares the image.
        assert select_docker_image("kotlin") == "maven:3.9-eclipse-temurin-21"

    def test_ruby(self) -> None:
        assert select_docker_image("ruby") == "ruby:3.3-slim"

    def test_php(self) -> None:
        # composer image bundles PHP + composer; php:*-cli lacks composer.
        assert select_docker_image("php") == "composer:2"

    def test_unknown_language_returns_empty(self) -> None:
        assert select_docker_image("cobol") == ""

    def test_empty_language_returns_empty(self) -> None:
        assert select_docker_image("") == ""

    def test_case_insensitive(self) -> None:
        assert select_docker_image("Python") == "python:3.12-slim"
        assert select_docker_image("JAVASCRIPT") == "node:22-slim"


class TestCreateSandboxImageSelection:
    """create_sandbox should pick the right image from the detected language."""

    def _make(self, **kw) -> object:
        return create_sandbox(
            sandbox_type="docker",
            workspace_root=Path("."),
            test_commands=["echo ok"],
            install_commands=[],
            **kw,
        )

    def test_js_project_gets_node_image(self) -> None:
        sandbox = self._make(primary_language="javascript")
        assert getattr(sandbox, "_image", None) == "node:22-slim"

    def test_explicit_override_wins_over_language(self) -> None:
        sandbox = self._make(
            primary_language="javascript",
            docker_image="my-custom/image:latest",
        )
        assert getattr(sandbox, "_image", None) == "my-custom/image:latest"

    def test_no_language_uses_default(self) -> None:
        """Bare call with no hint falls back to DockerSandbox's built-in default."""
        sandbox = self._make()
        # DEFAULT_IMAGE is python:3.12-slim in docker_runner.py
        assert getattr(sandbox, "_image", None) == "python:3.12-slim"

    def test_unknown_language_uses_default(self) -> None:
        sandbox = self._make(primary_language="cobol")
        assert getattr(sandbox, "_image", None) == "python:3.12-slim"
