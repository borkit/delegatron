"""Tests for per-language sandbox toolchains (sandbox/toolchains.py).

Covers language detection for a sandbox run (incidental scripts and
Makefiles must not hijack a project, issue #93), the per-language commands
and images (issue #96) and the factory's install commands.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from delegatron.factories.sandbox_factory import get_install_commands
from delegatron.sandbox.docker_runner import _detect_sandbox_language, _shell_for
from delegatron.sandbox.toolchains import (
    CMAKE_TEST_COMMAND,
    CMAKE_TOOLCHAIN_INSTALL,
    LANG_RULES,
    PESTER_INSTALL,
    PESTER_INSTALL_CURRENT_USER,
    PESTER_TEST_COMMAND,
    PYTEST_COMMAND,
    LangRule,
    host_install_commands,
    is_container_only_install,
    match_file,
    rule_for_language,
    select_docker_image,
)


def _rule(language: str) -> LangRule:
    rule = rule_for_language(language)
    assert rule is not None
    return rule


# ---------------------------------------------------------------------------
# _detect_sandbox_language
# ---------------------------------------------------------------------------


def _files(*paths: str) -> dict[str, str]:
    return dict.fromkeys(paths, "")


class TestDetectSandboxLanguage:
    def test_python_sources_outvote_a_shell_script(self, tmp_path: Path) -> None:
        detected = _detect_sandbox_language(
            tmp_path, _files("card.py", "tests/test_card.py", "run_tests.sh")
        )
        assert detected == ("python", PYTEST_COMMAND, "pip install pytest -q")

    def test_python_patch_beats_root_makefile_and_package_json(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
        (tmp_path / "package.json").write_text("{}")
        detected = _detect_sandbox_language(tmp_path, _files("card.py"))
        assert detected is not None
        assert detected[0] == "python"

    def test_majority_of_primary_files_wins(self, tmp_path: Path) -> None:
        detected = _detect_sandbox_language(
            tmp_path, _files("src/App/Calc.cs", "tests/App.Tests/CalcTests.cs", "tool.py")
        )
        assert detected is not None
        assert detected[0] == "csharp"

    def test_primary_hint_wins_over_the_patch(self, tmp_path: Path) -> None:
        detected = _detect_sandbox_language(tmp_path, _files("package.json"), hint="python")
        assert detected is not None
        assert detected[0] == "python"

    def test_hint_aliases_are_normalized(self, tmp_path: Path) -> None:
        detected = _detect_sandbox_language(tmp_path, {}, hint="c++")
        assert detected == ("cpp", CMAKE_TEST_COMMAND, _rule("cpp").install_command)

    def test_auxiliary_hint_loses_to_primary_patch_files(self, tmp_path: Path) -> None:
        detected = _detect_sandbox_language(tmp_path, _files("tool.py"), hint="shell")
        assert detected is not None
        assert detected[0] == "python"

    def test_auxiliary_hint_kept_without_primary_files(self, tmp_path: Path) -> None:
        detected = _detect_sandbox_language(tmp_path, _files("deploy.ps1"), hint="powershell")
        assert detected == ("powershell", PESTER_TEST_COMMAND, PESTER_INSTALL)

    def test_greenfield_package_json(self, tmp_path: Path) -> None:
        detected = _detect_sandbox_language(tmp_path, _files("package.json", "index.js"))
        assert detected == ("javascript", "npm test", "npm install")

    def test_workspace_primary_marker_before_auxiliary_patch_file(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        detected = _detect_sandbox_language(tmp_path, _files("run_tests.sh"))
        assert detected is not None
        assert detected[0] == "python"

    def test_auxiliary_only(self, tmp_path: Path) -> None:
        detected = _detect_sandbox_language(tmp_path, _files("build.sh"))
        assert detected is not None
        assert detected[0] == "shell"
        (tmp_path / "Makefile").write_text("all:\n")
        assert _detect_sandbox_language(tmp_path, {}) == ("c", "make test", "make")

    def test_nothing_matches(self, tmp_path: Path) -> None:
        assert _detect_sandbox_language(tmp_path, _files("README.md")) is None


# ---------------------------------------------------------------------------
# Rule table, images, shells
# ---------------------------------------------------------------------------

# Languages whose tests need dependencies fetched while the install phase has network.
_NEEDS_INSTALL = (
    "rust",
    "go",
    "java",
    "kotlin",
    "javascript",
    "ruby",
    "php",
    "csharp",
    "fsharp",
    "swift",
    "dart",
    "elixir",
    "scala",
    "haskell",
    "powershell",
)


class TestRuleTable:
    @pytest.mark.parametrize("language", _NEEDS_INSTALL)
    def test_dependency_languages_have_an_install_command(self, language: str) -> None:
        rules = [rule for rule in LANG_RULES if rule.language == language]
        assert rules
        assert all(rule.install_command for rule in rules)

    @pytest.mark.parametrize("rule", LANG_RULES, ids=lambda rule: rule.pattern)
    def test_no_single_quoted_test_arguments(self, rule: LangRule) -> None:
        # cmd.exe (LocalSandbox on Windows) does not treat single quotes as quotes.
        assert "'" not in rule.test_command

    def test_shared_commands(self) -> None:
        assert _rule("powershell").test_command == PESTER_TEST_COMMAND
        assert _rule("cpp").test_command == CMAKE_TEST_COMMAND
        assert "--no-tests=error" in CMAKE_TEST_COMMAND
        assert "-CI" in PESTER_TEST_COMMAND
        assert _rule("rust").install_command == "cargo fetch"

    def test_auxiliary_rules(self) -> None:
        auxiliary = {rule.pattern for rule in LANG_RULES if not rule.primary}
        assert auxiliary == {"*.ps1", "*.sh", "Makefile"}

    @pytest.mark.parametrize("path", ["App.sln", "src/App/App.csproj", r"src\App\Calc.cs"])
    def test_dotnet_files_are_csharp(self, path: str) -> None:
        rule = match_file(path)
        assert rule is not None
        assert rule.language == "csharp"


class TestImages:
    def test_powershell_image_is_pinned(self) -> None:
        image = select_docker_image("powershell")
        assert image.startswith("mcr.microsoft.com/powershell:")
        assert image.split(":", 1)[1][0].isdigit()

    @pytest.mark.parametrize("language", ["c", "cpp", "c++", "C++"])
    def test_c_family_gets_a_compiler_image(self, language: str) -> None:
        assert select_docker_image(language) == "gcc:14"

    def test_existing_mappings_unchanged(self) -> None:
        assert select_docker_image("csharp") == "mcr.microsoft.com/dotnet/sdk:9.0"
        assert select_docker_image("typescript") == "node:22-slim"
        assert select_docker_image("shell") == ""


class TestWindowsShell:
    @pytest.mark.parametrize(
        "command",
        [PESTER_TEST_COMMAND, "powershell -File t.ps1", "Invoke-Pester ./tests/a.ps1"],
    )
    def test_powershell_commands_use_powershell(self, command: str) -> None:
        mount, shell, join = _shell_for(True, [command])
        assert mount == "C:\\workspace"
        assert shell[0] == "powershell"
        assert join == " ; "

    def test_other_commands_use_cmd(self) -> None:
        assert _shell_for(True, ["dotnet test"])[1] == ["cmd", "/c"]

    def test_linux_uses_bash(self) -> None:
        assert _shell_for(False, [PESTER_TEST_COMMAND]) == ("/workspace", ["bash", "-c"], " && ")


# ---------------------------------------------------------------------------
# get_install_commands
# ---------------------------------------------------------------------------


def _project(package_manager: str, *languages: str) -> SimpleNamespace:
    return SimpleNamespace(package_manager=package_manager, languages=list(languages))


class TestInstallCommands:
    def test_pipenv(self, tmp_path: Path) -> None:
        commands = get_install_commands(_project("pipenv", "python"), tmp_path)
        assert commands == [
            "pip install pipenv -q",
            "pipenv install --dev --system",
            "pip install pytest -q",
        ]

    def test_powershell_gets_pester_and_no_pip(self, tmp_path: Path) -> None:
        commands = get_install_commands(_project("", "powershell"), tmp_path)
        assert commands == [PESTER_INSTALL]

    def test_cmake_gets_the_toolchain(self, tmp_path: Path) -> None:
        commands = get_install_commands(_project("cmake", "c++"), tmp_path)
        assert commands == [_rule("cpp").install_command]

    def test_poetry_gets_pytest(self, tmp_path: Path) -> None:
        assert get_install_commands(_project("poetry", "python"), tmp_path) == [
            "pip install pytest -q"
        ]

    def test_language_without_install_step(self, tmp_path: Path) -> None:
        assert get_install_commands(_project("", "shell"), tmp_path) == []

    def test_swift_uses_its_rule(self, tmp_path: Path) -> None:
        assert get_install_commands(_project("swift", "swift"), tmp_path) == [
            "swift package resolve"
        ]

    def test_nothing_detected_assumes_python(self, tmp_path: Path) -> None:
        assert get_install_commands(_project(""), tmp_path) == ["pip install pytest -q"]


class TestHostInstalls:
    """#96: local and E2B sandboxes never run container-only system installs."""

    def test_apt_dropped_and_pester_per_user(self) -> None:
        commands = ["pip install pytest -q", CMAKE_TOOLCHAIN_INSTALL, PESTER_INSTALL]
        assert host_install_commands(commands) == [
            "pip install pytest -q",
            PESTER_INSTALL_CURRENT_USER,
        ]

    def test_only_the_cmake_toolchain_is_container_only(self) -> None:
        assert is_container_only_install(CMAKE_TOOLCHAIN_INSTALL)
        assert not is_container_only_install(PESTER_INSTALL)
        assert not is_container_only_install("npm install")

    def test_current_user_pester_skips_an_installed_pester_5(self) -> None:
        assert "-Scope CurrentUser" in PESTER_INSTALL_CURRENT_USER
        assert "Where-Object Version -ge '5.0'" in PESTER_INSTALL_CURRENT_USER
        assert "$" not in PESTER_INSTALL_CURRENT_USER  # nothing for bash to expand

    def test_cmake_install_only_when_cmake_is_missing(self) -> None:
        assert CMAKE_TOOLCHAIN_INSTALL.startswith("command -v cmake >/dev/null 2>&1 || ")
