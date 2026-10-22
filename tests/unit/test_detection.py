"""Tests for project detection."""

import codecs
from pathlib import Path

import pytest

from delegatron.detection import detect_project, read_text_lenient


def test_detect_python_project(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")

    info = detect_project(tmp_path)

    assert "python" in info.languages
    assert "fastapi" in info.frameworks
    # One command for both markers; `python -m` lets tests/ import root modules.
    assert info.test_commands == ["python -m pytest -v"]


def test_detect_requirements_only_project(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("requests\n")

    info = detect_project(tmp_path)

    assert info.test_commands == ["python -m pytest -v"]
    assert info.package_manager == "pip"


def test_detect_django_keeps_manage_py_test(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("django\n")
    (tmp_path / "manage.py").write_text("")

    info = detect_project(tmp_path)

    assert info.test_commands == ["python -m pytest -v", "python manage.py test"]
    assert "django" in info.frameworks


def test_detect_javascript_project(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name": "test"}')
    (tmp_path / "tsconfig.json").write_text("{}")

    info = detect_project(tmp_path)

    assert "javascript" in info.languages
    assert "typescript" in info.languages


def test_detect_go_project(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module example.com/test\n")

    info = detect_project(tmp_path)

    assert "go" in info.languages
    assert "go test ./..." in info.test_commands


def test_detect_empty_directory(tmp_path: Path):
    info = detect_project(tmp_path)

    assert info.languages == []
    assert info.frameworks == []


def _touch(root: Path, *paths: str) -> None:
    for rel in paths:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("")


# ---------------------------------------------------------------------------
# Incidental markers and the primary language (#93)
# ---------------------------------------------------------------------------


def test_root_scripts_do_not_hijack_a_python_project(tmp_path: Path):
    _touch(tmp_path, "pyproject.toml", "bootstrap.ps1", "setup.sh")

    info = detect_project(tmp_path)

    assert info.languages == ["python"]
    assert info.test_commands == ["python -m pytest -v"]


@pytest.mark.parametrize(
    "markers",
    [("pyproject.toml", "Makefile"), ("requirements.txt", "package.json")],
    ids=["makefile", "tooling_package_json"],
)
def test_incidental_markers_add_no_language_or_test_command(
    tmp_path: Path, markers: tuple[str, ...]
):
    _touch(tmp_path, *markers, "card.py", "tests/test_card.py")

    info = detect_project(tmp_path)

    assert info.languages == ["python"]
    assert info.test_commands == ["python -m pytest -v"]
    assert info.package_manager == "pip"


def test_script_only_repo_is_still_shell(tmp_path: Path):
    _touch(tmp_path, "install.sh", "lib/util.sh")

    info = detect_project(tmp_path)

    assert info.languages == ["shell"]
    assert info.test_commands == ["bash -n *.sh"]


def test_powershell_and_cmake_use_the_sandbox_toolchain_commands(tmp_path: Path):
    from delegatron.sandbox.toolchains import CMAKE_TEST_COMMAND, PESTER_TEST_COMMAND

    _touch(tmp_path, "build.ps1")
    assert detect_project(tmp_path).test_commands == [PESTER_TEST_COMMAND]

    cmake = tmp_path / "cmake"
    _touch(cmake, "CMakeLists.txt", "main.cpp")
    assert detect_project(cmake).test_commands == [CMAKE_TEST_COMMAND]


def test_second_language_with_real_source_is_listed_without_its_tests(tmp_path: Path):
    _touch(tmp_path, "requirements.txt", "app.py", "api.py", "package.json", "web/index.js")

    info = detect_project(tmp_path)

    assert info.languages == ["python", "javascript"]
    assert info.test_commands == ["python -m pytest -v"]


def test_language_with_most_source_files_is_primary(tmp_path: Path):
    _touch(
        tmp_path,
        "requirements.txt",
        "tools/gen.py",
        "package.json",
        "tsconfig.json",
        "src/a.ts",
        "src/b.tsx",
        "src/c.js",
    )

    info = detect_project(tmp_path)

    assert info.languages == ["javascript", "typescript", "python"]
    assert info.test_commands == ["npm test"]
    assert info.package_manager == "npm"


def test_tsconfig_without_package_json_does_not_take_over_a_python_project(tmp_path: Path):
    """A lone tsconfig.json names no test runner, so it cannot be the primary toolchain."""
    _touch(
        tmp_path,
        "pyproject.toml",
        "tsconfig.json",
        "app.py",
        "tests/test_app.py",
        "web/a.ts",
        "web/b.ts",
        "web/c.tsx",
    )

    info = detect_project(tmp_path)

    assert info.languages == ["python", "typescript"]
    assert info.test_commands == ["python -m pytest -v"]
    assert info.package_manager == "pip"


def test_typescript_with_package_json_still_wins_by_source_count(tmp_path: Path):
    _touch(tmp_path, "pyproject.toml", "tsconfig.json", "package.json", "app.py", "web/a.ts")
    _touch(tmp_path, "web/b.ts")

    info = detect_project(tmp_path)

    assert info.languages[0] == "javascript"
    assert info.test_commands == ["npm test"]


# ---------------------------------------------------------------------------
# Projects without a manifest
# ---------------------------------------------------------------------------


def test_manifest_less_python_project_is_detected(tmp_path: Path):
    """An empty repo that gained plain modules and tests is a pytest project."""
    _touch(tmp_path, "card_deck.py", "simulate_deal.py", "tests/test_card_deck.py")

    info = detect_project(tmp_path)

    assert info.languages == ["python"]
    assert info.test_commands == ["python -m pytest -v"]
    assert info.package_manager == "pip"
    assert info.config_files == []


def test_python_sources_outvote_a_root_script_without_manifest(tmp_path: Path):
    _touch(tmp_path, "card.py", "tests/test_card.py", "run_tests.sh")

    info = detect_project(tmp_path)

    assert info.languages == ["python", "shell"]
    assert info.test_commands == ["python -m pytest -v"]


def test_manifest_less_non_python_sources_add_no_language(tmp_path: Path):
    _touch(tmp_path, "main.go", "util.go")

    assert detect_project(tmp_path).languages == []


def test_delegatron_run_dirs_do_not_count_but_a_user_runs_package_does(tmp_path: Path):
    """D4: only Delegatron's own ``runs/`` (it holds run summaries) is skipped."""
    _touch(tmp_path, "_sandbox/workspace/app.py", "runs/abc/summary.json", "runs/abc/x.py")

    assert detect_project(tmp_path).languages == []

    _touch(tmp_path / "user", "runs/__init__.py", "runs/job.py")

    assert detect_project(tmp_path / "user").languages == ["python"]


def test_dotnet_solution_with_nested_projects(tmp_path: Path):
    _touch(
        tmp_path,
        "App.sln",
        "src/App/App.csproj",
        "src/App/Calc.cs",
        "tests/App.Tests/App.Tests.csproj",
    )

    info = detect_project(tmp_path)

    assert info.languages == ["csharp"]
    assert info.test_commands == ["dotnet test"]
    assert info.config_files == [
        "App.sln",
        "src/App/App.csproj",
        "tests/App.Tests/App.Tests.csproj",
    ]


def test_nested_project_search_skips_build_and_hidden_dirs(tmp_path: Path):
    _touch(tmp_path, "obj/Debug/Gen.csproj", ".git/x/Hidden.csproj", "a/b/c/d/TooDeep.csproj")

    assert detect_project(tmp_path).languages == []


# ---------------------------------------------------------------------------
# Manifest encodings (#117)
# ---------------------------------------------------------------------------


def test_utf16_requirements_are_read(tmp_path: Path):
    """PowerShell 5.1 ``pip freeze > requirements.txt`` writes UTF-16 with a BOM."""
    (tmp_path / "requirements.txt").write_bytes("flask==3.0.0\n".encode("utf-16"))

    info = detect_project(tmp_path)

    assert info.languages == ["python"]
    assert "flask" in info.frameworks


def test_utf8_bom_pyproject_is_read(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_bytes(
        codecs.BOM_UTF8 + b'[project]\ndependencies = ["fastapi"]\n'
    )

    assert "fastapi" in detect_project(tmp_path).frameworks


def test_undecodable_manifest_gives_marker_only_result(tmp_path: Path):
    (tmp_path / "requirements.txt").write_bytes(b"\x81\x8d\xff\xfe\x00")

    info = detect_project(tmp_path)

    assert info.languages == ["python"]
    assert info.frameworks == []


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"flask\n", "flask\n"),
        (codecs.BOM_UTF8 + "café\n".encode(), "café\n"),
        (codecs.BOM_UTF16_LE + "café\n".encode("utf-16-le"), "café\n"),
        (codecs.BOM_UTF16_BE + "café\n".encode("utf-16-be"), "café\n"),
        (b"\x81\x8d\xff", 3 * chr(0xFFFD)),
    ],
    ids=["utf8", "utf8_bom", "utf16le_bom", "utf16be_bom", "undecodable"],
)
def test_read_text_lenient(tmp_path: Path, data: bytes, expected: str):
    path = tmp_path / "requirements.txt"
    path.write_bytes(data)

    assert read_text_lenient(path) == expected


def test_read_text_lenient_missing_file_is_empty(tmp_path: Path):
    assert read_text_lenient(tmp_path / "missing.txt") == ""
