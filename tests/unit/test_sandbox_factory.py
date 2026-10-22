"""Tests for factories/sandbox_factory.py — create_sandbox and get_install_commands."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from delegatron.factories import sandbox_factory
from delegatron.factories.sandbox_factory import (
    SANDBOX_PATTERN,
    SANDBOX_TYPES,
    SandboxUnavailableError,
    create_sandbox,
    get_install_commands,
)


@pytest.fixture(autouse=True)
def _docker_cli_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Docker preflight looks for the CLI; no test here needs a real one.
    monkeypatch.setattr(sandbox_factory.shutil, "which", lambda name: f"/usr/bin/{name}")


# ---------------------------------------------------------------------------
# create_sandbox tests
# ---------------------------------------------------------------------------


class TestCreateSandbox:
    """Test sandbox creation for each type."""

    def test_docker_without_the_cli_fails_before_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """#123: a missing docker CLI is a clear error, not a failure on the first test run."""
        monkeypatch.setattr(sandbox_factory.shutil, "which", lambda name: None)
        with (
            patch("delegatron.sandbox.docker_runner.DockerSandbox") as mock_cls,
            pytest.raises(SandboxUnavailableError, match="docker CLI"),
        ):
            create_sandbox(
                sandbox_type="docker",
                workspace_root=tmp_path,
                test_commands=["pytest"],
                install_commands=[],
            )
        mock_cls.assert_not_called()

    def test_create_local_sandbox(self, tmp_path: Path):
        """sandbox_type='local' should create LocalSandbox."""
        with patch("delegatron.sandbox.local_runner.LocalSandbox") as mock_cls:
            create_sandbox(
                sandbox_type="local",
                workspace_root=tmp_path,
                test_commands=["pytest"],
                install_commands=["pip install -e ."],
            )
        mock_cls.assert_called_once_with(
            workspace_root=tmp_path,
            test_commands=["pytest"],
            install_commands=["pip install -e ."],
            language="",
            run_entrypoints=False,
        )

    @pytest.mark.parametrize(("setting", "runs"), [(None, False), (False, False), (True, True)])
    def test_local_sandbox_runs_entrypoints_only_on_opt_in(
        self, tmp_path: Path, setting: bool | None, runs: bool
    ):
        """Generated scripts run on the host only when run_entrypoints is explicitly on."""
        sandbox = create_sandbox(
            sandbox_type="local",
            workspace_root=tmp_path,
            test_commands=["pytest"],
            install_commands=[],
            primary_language="python",
            run_entrypoints=setting,
        )
        assert sandbox.run_entrypoints is runs  # type: ignore[attr-defined]
        assert sandbox.language == "python"  # type: ignore[attr-defined]

    def test_create_none_sandbox(self, tmp_path: Path):
        """sandbox_type='none' should create LocalSandbox."""
        with patch("delegatron.sandbox.local_runner.LocalSandbox") as mock_cls:
            create_sandbox(
                sandbox_type="none",
                workspace_root=tmp_path,
                test_commands=["pytest"],
                install_commands=[],
            )
        mock_cls.assert_called_once()

    def test_create_e2b_sandbox(self, tmp_path: Path):
        """sandbox_type='e2b' should create E2BSandbox."""
        with patch("delegatron.sandbox.e2b_runner.E2BSandbox") as mock_cls:
            create_sandbox(
                sandbox_type="e2b",
                workspace_root=tmp_path,
                test_commands=["pytest"],
                install_commands=[],
                primary_language="go",
            )
        mock_cls.assert_called_once_with(
            workspace_root=tmp_path,
            test_commands=["pytest"],
            install_commands=[],
            language="go",
        )

    def test_create_docker_sandbox_default(self, tmp_path: Path):
        """sandbox_type='docker' should create DockerSandbox."""
        with patch("delegatron.sandbox.docker_runner.DockerSandbox") as mock_cls:
            create_sandbox(
                sandbox_type="docker",
                workspace_root=tmp_path,
                test_commands=["pytest"],
                install_commands=[],
            )
        mock_cls.assert_called_once_with(
            workspace_root=tmp_path,
            test_commands=["pytest"],
            install_commands=[],
            language="",
            image_explicit=False,
        )

    def test_create_docker_sandbox_with_custom_image(self, tmp_path: Path):
        """Docker sandbox with custom image should pass image kwarg."""
        with patch("delegatron.sandbox.docker_runner.DockerSandbox") as mock_cls:
            create_sandbox(
                sandbox_type="docker",
                workspace_root=tmp_path,
                test_commands=["pytest"],
                install_commands=[],
                docker_image="node:22-slim",
            )
        mock_cls.assert_called_once_with(
            workspace_root=tmp_path,
            test_commands=["pytest"],
            install_commands=[],
            language="",
            image_explicit=True,
            image="node:22-slim",
        )

    def test_create_unknown_type_defaults_to_docker(self, tmp_path: Path):
        """Unknown sandbox type should default to DockerSandbox."""
        with patch("delegatron.sandbox.docker_runner.DockerSandbox") as mock_cls:
            create_sandbox(
                sandbox_type="unknown_type",
                workspace_root=tmp_path,
                test_commands=["pytest"],
                install_commands=[],
            )
        mock_cls.assert_called_once()


# ---------------------------------------------------------------------------
# SANDBOX_TYPES and SANDBOX_PATTERN tests
# ---------------------------------------------------------------------------


class TestSandboxConstants:
    """Test module-level constants."""

    def test_sandbox_types_contains_all(self):
        assert "docker" in SANDBOX_TYPES
        assert "e2b" in SANDBOX_TYPES
        assert "none" in SANDBOX_TYPES
        assert "local" in SANDBOX_TYPES

    def test_sandbox_types_frozen(self):
        assert isinstance(SANDBOX_TYPES, frozenset)

    def test_sandbox_pattern_regex(self):
        import re

        pattern = re.compile(SANDBOX_PATTERN)
        assert pattern.match("docker")
        assert pattern.match("e2b")
        assert pattern.match("none")
        assert pattern.match("local")
        assert not pattern.match("podman")
        assert not pattern.match("")


# ---------------------------------------------------------------------------
# get_install_commands tests
# ---------------------------------------------------------------------------


class TestGetInstallCommands:
    """Test install command generation for various package managers."""

    def test_pip_with_requirements_txt(self, tmp_path: Path):
        """pip + requirements.txt should generate pip install -r."""
        (tmp_path / "requirements.txt").write_text("flask>=2.0\n")
        project_info = SimpleNamespace(package_manager="pip")
        commands = get_install_commands(project_info, tmp_path)
        assert any("requirements.txt" in c for c in commands)
        assert any("pytest" in c for c in commands)

    def test_pip_with_pyproject_toml(self, tmp_path: Path):
        """pip + pyproject.toml should generate pip install -e ."""
        (tmp_path / "pyproject.toml").write_text("[build-system]\n")
        project_info = SimpleNamespace(package_manager="pip")
        commands = get_install_commands(project_info, tmp_path)
        assert any("pip install -e ." in c for c in commands)
        assert any("pytest" in c for c in commands)

    def test_pip_with_test_extra(self, tmp_path: Path):
        """pip + pyproject.toml with test extra should use .[test]."""
        toml_content = '[project.optional-dependencies]\ntest = ["pytest", "hypothesis"]\n'
        (tmp_path / "pyproject.toml").write_text(toml_content)
        project_info = SimpleNamespace(package_manager="pip")
        commands = get_install_commands(project_info, tmp_path)
        assert any(".[test]" in c for c in commands)

    def test_pip_no_config_files(self, tmp_path: Path):
        """pip with no config files should just install pytest."""
        project_info = SimpleNamespace(package_manager="pip")
        commands = get_install_commands(project_info, tmp_path)
        assert any("pytest" in c for c in commands)

    def test_npm(self, tmp_path: Path):
        project_info = SimpleNamespace(package_manager="npm")
        commands = get_install_commands(project_info, tmp_path)
        assert "npm install" in commands

    def test_yarn(self, tmp_path: Path):
        project_info = SimpleNamespace(package_manager="yarn")
        commands = get_install_commands(project_info, tmp_path)
        assert "yarn install" in commands

    def test_pnpm(self, tmp_path: Path):
        project_info = SimpleNamespace(package_manager="pnpm")
        commands = get_install_commands(project_info, tmp_path)
        assert "pnpm install" in commands

    def test_bun(self, tmp_path: Path):
        project_info = SimpleNamespace(package_manager="bun")
        commands = get_install_commands(project_info, tmp_path)
        assert "bun install" in commands

    def test_dotnet(self, tmp_path: Path):
        project_info = SimpleNamespace(package_manager="dotnet")
        commands = get_install_commands(project_info, tmp_path)
        assert "dotnet restore" in commands

    def test_go(self, tmp_path: Path):
        project_info = SimpleNamespace(package_manager="go")
        commands = get_install_commands(project_info, tmp_path)
        assert "go mod download" in commands

    def test_cargo(self, tmp_path: Path):
        """Cargo fetches crates while the install phase has network (tests run offline)."""
        project_info = SimpleNamespace(package_manager="cargo")
        commands = get_install_commands(project_info, tmp_path)
        assert commands == ["cargo fetch"]

    def test_maven(self, tmp_path: Path):
        project_info = SimpleNamespace(package_manager="maven")
        commands = get_install_commands(project_info, tmp_path)
        assert any("mvn" in c for c in commands)

    def test_gradle(self, tmp_path: Path):
        project_info = SimpleNamespace(package_manager="gradle")
        commands = get_install_commands(project_info, tmp_path)
        assert any("gradle" in c for c in commands)

    def test_bundler(self, tmp_path: Path):
        project_info = SimpleNamespace(package_manager="bundler")
        commands = get_install_commands(project_info, tmp_path)
        assert "bundle install" in commands

    def test_composer(self, tmp_path: Path):
        project_info = SimpleNamespace(package_manager="composer")
        commands = get_install_commands(project_info, tmp_path)
        assert "composer install" in commands

    def test_no_project_info(self, tmp_path: Path):
        """None project_info should install pytest as fallback."""
        commands = get_install_commands(None, tmp_path)
        assert any("pytest" in c for c in commands)

    def test_no_package_manager(self, tmp_path: Path):
        """Project info with no package_manager should install pytest."""
        project_info = SimpleNamespace(package_manager=None)
        commands = get_install_commands(project_info, tmp_path)
        assert any("pytest" in c for c in commands)

    def test_pip_test_extra_with_equals_format(self, tmp_path: Path):
        """Test extra using 'test=' format (no space before equals)."""
        toml_content = '[project.optional-dependencies]\ntest=["pytest"]\n'
        (tmp_path / "pyproject.toml").write_text(toml_content)
        project_info = SimpleNamespace(package_manager="pip")
        commands = get_install_commands(project_info, tmp_path)
        assert any(".[test]" in c for c in commands)
