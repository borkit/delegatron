"""Tests for the per-language parse-check gate."""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import delegatron.sandbox.parse_check as parse_check
from delegatron.sandbox.parse_check import (
    ParseError,
    PSAnalyzerCache,
    _check_python,
    run_parse_checks,
)

PWSH_IMAGE = "mcr.microsoft.com/powershell:preview"
WINDOWS_IMAGE = "mcr.microsoft.com/powershell:lts-nanoserver-ltsc2022"


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def _lint_result(tmp_path: Path, stdout: str) -> parse_check.ParseCheckResult:
    (tmp_path / "x.ps1").write_text("Get-Users\n", encoding="utf-8")
    with patch("subprocess.run", return_value=_completed(stdout)):
        return run_parse_checks(tmp_path, {"x.ps1": "Get-Users\n"}, image=PWSH_IMAGE)


class TestPythonCheck:
    def test_valid_python_returns_none(self) -> None:
        err = _check_python(Path("good.py"), "x = 1\n")
        assert err is None

    def test_syntax_error_returns_error(self) -> None:
        err = _check_python(Path("bad.py"), "def foo(:\n    pass\n")
        assert err is not None
        assert err.language == "Python"
        assert err.path == "bad.py"
        assert "line 1" in err.message

    def test_empty_file_is_valid(self) -> None:
        assert _check_python(Path("empty.py"), "") is None


class TestRunParseChecks:
    def test_only_python_host_path(self, tmp_path: Path) -> None:
        """When only .py files are present, no docker call should fire."""
        patch_files = {
            "good.py": "x = 1\n",
            "bad.py": "def f(:\n",
        }
        with patch("subprocess.run") as mock_run:
            result = run_parse_checks(tmp_path, patch_files, image=None)
        mock_run.assert_not_called()
        assert len(result.errors) == 1
        assert result.errors[0].path == "bad.py"
        assert result.errors[0].language == "Python"
        assert result.advisories == []

    def test_non_source_files_ignored(self, tmp_path: Path) -> None:
        """Files with unknown extensions are silently passed through."""
        patch_files = {
            "README.md": "# hi\n",
            "data.json": "{invalid json",
            "script.sh": "#!/bin/bash\necho hi\n",
        }
        result = run_parse_checks(tmp_path, patch_files, image=None)
        assert result.errors == []
        assert result.advisories == []

    def test_powershell_without_image_is_skipped(self, tmp_path: Path) -> None:
        """PS files only trigger docker when an image is configured."""
        patch_files = {"bad.ps1": "Set-StrictMode\nparam($x)\n"}
        # image=None → PS check skipped, no docker subprocess
        with patch("subprocess.run") as mock_run:
            result = run_parse_checks(tmp_path, patch_files, image=None)
        mock_run.assert_not_called()
        assert result.errors == []
        assert result.advisories == ["PowerShell parse-check skipped: no sandbox image"]

    def test_powershell_docker_parse_error(self, tmp_path: Path) -> None:
        """PARSE lines from the container surface as ParseError."""
        (tmp_path / "bad.ps1").write_text("definitely invalid & syntax here")
        patch_files = {"bad.ps1": "definitely invalid"}

        fake = _completed("PARSE bad.ps1|line 1: Unexpected token")
        with patch("subprocess.run", return_value=fake) as mock_run:
            result = run_parse_checks(tmp_path, patch_files, image=PWSH_IMAGE)
        assert mock_run.call_count == 1
        args = mock_run.call_args[0][0]
        assert args[0] == "docker"
        assert PWSH_IMAGE in args
        assert len(result.errors) == 1
        assert result.errors[0].language == "PowerShell"
        assert result.errors[0].path == "bad.ps1"
        assert "Unexpected token" in result.errors[0].message

    def test_powershell_docker_lint_finding(self, tmp_path: Path) -> None:
        """LINT lines from PSScriptAnalyzer surface as ParseError with rule name."""
        (tmp_path / "wmi.ps1").write_text("Get-WmiObject -Class Win32_LogicalDisk\n")
        patch_files = {"wmi.ps1": "Get-WmiObject -Class Win32_LogicalDisk\n"}

        fake = _completed(
            "LINT wmi.ps1|PSAvoidUsingWMICmdlet|line 1: File 'wmi.ps1' uses WMI cmdlet."
        )
        with patch("subprocess.run", return_value=fake):
            result = run_parse_checks(tmp_path, patch_files, image=PWSH_IMAGE)
        assert len(result.errors) == 1
        assert result.errors[0].path == "wmi.ps1"
        assert "PSAvoidUsingWMICmdlet" in result.errors[0].message

    def test_powershell_docker_unavailable_is_graceful(self, tmp_path: Path) -> None:
        """If docker isn't installed, the gate blocks nothing and says it was skipped."""
        patch_files = {"bad.ps1": "any content\n"}
        (tmp_path / "bad.ps1").write_text("any content\n")

        with patch(
            "subprocess.run",
            side_effect=FileNotFoundError("docker not found"),
        ):
            result = run_parse_checks(tmp_path, patch_files, image="some/image:tag")
        assert result.errors == []  # graceful skip, not crash
        assert result.advisories == ["PowerShell parse-check skipped: docker not found"]


class TestPowerShellClassification:
    @pytest.mark.parametrize(
        "rule",
        ["PSUseSingularNouns", "PSAvoidUsingCmdletAliases", "PSUseToExportFieldsInManifest"],
    )
    def test_style_warnings_are_advisories(self, tmp_path: Path, rule: str) -> None:
        result = _lint_result(tmp_path, f"LINT x.ps1|{rule}|line 1: style\nOK x.ps1")
        assert result.errors == []
        assert result.advisories == [f"[lint] x.ps1: [{rule}] line 1: style"]

    def test_bom_rule_dropped(self, tmp_path: Path) -> None:
        result = _lint_result(tmp_path, "LINT x.ps1|PSUseBOMForUnicodeEncodedFile|line 1: BOM")
        assert result.errors == []
        assert result.advisories == []

    def test_error_severity_blocks(self, tmp_path: Path) -> None:
        stdout = "LINTERR x.ps1|PSAvoidUsingComputerNameHardcoded|line 2: hardcoded"
        result = _lint_result(tmp_path, stdout)
        assert result.errors == [
            ParseError(
                "x.ps1", "PowerShell", "[PSAvoidUsingComputerNameHardcoded] line 2: hardcoded"
            )
        ]
        assert result.advisories == []

    def test_blocking_and_advisory_mixed(self, tmp_path: Path) -> None:
        stdout = (
            "LINT x.ps1|PSUseCmdletCorrectly|line 1: missing parameter\n"
            "LINT x.ps1|PSUseSingularNouns|line 3: plural noun\n"
        )
        result = _lint_result(tmp_path, stdout)
        assert [e.message for e in result.errors] == [
            "[PSUseCmdletCorrectly] line 1: missing parameter"
        ]
        assert result.advisories == ["[lint] x.ps1: [PSUseSingularNouns] line 3: plural noun"]

    def test_image_without_pwsh_is_an_advisory(self, tmp_path: Path) -> None:
        missing = _completed(returncode=127, stderr='exec: "pwsh": executable file not found\n')
        with patch("subprocess.run", return_value=missing):
            result = run_parse_checks(tmp_path, {"x.ps1": "1\n"}, image="python:3.12-slim")
        assert result.errors == []
        assert result.advisories == [
            'PowerShell parse-check skipped: exec: "pwsh": executable file not found'
        ]

    def test_gate_is_offline_and_mounts_read_only(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_completed("OK x.ps1")) as run:
            run_parse_checks(tmp_path, {"x.ps1": "1\n"}, image=PWSH_IMAGE)
        args = run.call_args[0][0]
        assert args[:5] == ["docker", "run", "--rm", "--network", "none"]
        assert f"{str(tmp_path).replace(chr(92), '/')}:/workspace:ro" in args
        assert args[args.index("-w") + 1] == "/workspace"
        assert "'x.ps1'" in args[-1]

    def test_windows_image_uses_windows_paths(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_completed("OK x.ps1")) as run:
            run_parse_checks(tmp_path, {"x.ps1": "1\n"}, image=WINDOWS_IMAGE)
        args = run.call_args[0][0]
        assert f"{str(tmp_path).replace(chr(92), '/')}:C:\\workspace:ro" in args
        assert args[args.index("-w") + 1] == "C:\\workspace"


class TestPSAnalyzerCache:
    def test_installs_once_then_stays_offline(self, tmp_path: Path) -> None:
        cache = PSAnalyzerCache(PWSH_IMAGE)
        calls: list[list[str]] = []

        def _run(cmd: list[str], **_kwargs: Any) -> MagicMock:
            calls.append(cmd)
            return _completed("OK x.ps1")

        with patch("subprocess.run", side_effect=_run):
            run_parse_checks(tmp_path, {"x.ps1": "1\n"}, image=PWSH_IMAGE, ps_analyzer=cache)
            run_parse_checks(tmp_path, {"x.ps1": "1\n"}, image=PWSH_IMAGE, ps_analyzer=cache)

        install, first_gate, second_gate = calls
        assert "--network" not in install
        assert f"{cache.volume}:/opt/delegatron-psmodules" in install
        assert "Save-Module PSScriptAnalyzer" in install[-1]
        assert cache.status == "ready"
        for gate in (first_gate, second_gate):
            assert gate[3:5] == ["--network", "none"]
            assert f"{cache.volume}:/opt/delegatron-psmodules:ro" in gate
            assert "/opt/delegatron-psmodules" in gate[-1]  # added to PSModulePath

    def test_failed_install_falls_back_to_parser_pass_without_retry(self, tmp_path: Path) -> None:
        cache = PSAnalyzerCache(PWSH_IMAGE)
        results = [
            _completed(returncode=1, stderr="no network"),
            _completed("OK"),
            _completed("OK"),
        ]
        with patch("subprocess.run", side_effect=results) as run:
            first = run_parse_checks(tmp_path, {"x.ps1": "1\n"}, PWSH_IMAGE, ps_analyzer=cache)
            run_parse_checks(tmp_path, {"x.ps1": "1\n"}, PWSH_IMAGE, ps_analyzer=cache)

        assert cache.status == "unavailable"
        assert run.call_count == 3  # one install attempt, two gate runs
        for call in run.call_args_list[1:]:
            assert cache.volume not in " ".join(call.args[0])
        assert first.errors == []

    def test_failed_install_is_reported_as_an_advisory(self, tmp_path: Path) -> None:
        """#119: a check that could not run leaves a note, every time it is skipped."""
        cache = PSAnalyzerCache(PWSH_IMAGE)
        results = [_completed(returncode=1, stderr="no network"), _completed("OK x.ps1")]
        with patch("subprocess.run", side_effect=results):
            result = run_parse_checks(tmp_path, {"x.ps1": "1\n"}, PWSH_IMAGE, ps_analyzer=cache)
        assert result.advisories == [
            "PowerShell lint skipped: PSScriptAnalyzer could not be installed (no network); "
            "the files got the parser check only"
        ]

    def test_volume_name_is_stable_per_image(self) -> None:
        assert PSAnalyzerCache(PWSH_IMAGE).volume == PSAnalyzerCache(PWSH_IMAGE).volume
        assert PSAnalyzerCache(PWSH_IMAGE).volume != PSAnalyzerCache(WINDOWS_IMAGE).volume
        assert PSAnalyzerCache(PWSH_IMAGE).volume.startswith("delegatron-psmodules-")

    def test_volume_name_is_per_owner(self) -> None:
        # Two sandboxes on one image never share (and so never delete) a volume.
        first = PSAnalyzerCache(PWSH_IMAGE, owner="a1b2c3d4").volume
        assert first != PSAnalyzerCache(PWSH_IMAGE, owner="e5f6a7b8").volume
        assert first.endswith("-a1b2c3d4")


class TestPythonInImage:
    def test_compiled_by_image_python(self, tmp_path: Path) -> None:
        output = "PARSE src/app.py|invalid syntax at line 3, col 5\nDONE\n"
        with patch("subprocess.run", return_value=_completed(output)) as run:
            result = run_parse_checks(
                tmp_path,
                {"src/app.py": "x = (\n", "ok.py": "x = 1\n"},
                image="python:3.12-slim",
                python_image="delegatron-cache-abc123",
            )
        args = run.call_args[0][0]
        assert args[:5] == ["docker", "run", "--rm", "--network", "none"]
        image_at = args.index("delegatron-cache-abc123")
        assert args[image_at + 1 : image_at + 4] == ["python3", "-I", "-c"]
        assert args[-2:] == ["src/app.py", "ok.py"]
        assert result.errors == [
            ParseError("src/app.py", "Python", "invalid syntax at line 3, col 5")
        ]

    def test_image_without_python_falls_back_to_host(self, tmp_path: Path) -> None:
        missing = _completed(returncode=127, stderr="python3: not found")
        with patch("subprocess.run", return_value=missing):
            result = run_parse_checks(
                tmp_path, {"bad.py": "def f(:\n"}, image="node:20", python_image="node:20"
            )
        assert [e.path for e in result.errors] == ["bad.py"]

    def test_compile_script_reports_like_the_host(self, tmp_path: Path) -> None:
        """The in-image script itself, run here with the host Python."""
        (tmp_path / "bad.py").write_text("def f(:\n", encoding="utf-8")
        (tmp_path / "latin.py").write_bytes("x = 'Pok\u00e9mon'\n".encode("cp1252"))
        (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
        out = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                parse_check._PY_COMPILE_SCRIPT,
                "bad.py",
                "latin.py",
                "ok.py",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        lines = out.splitlines()
        assert lines[-1] == "DONE"
        assert [line.split("|")[0] for line in lines[:-1]] == ["PARSE bad.py", "PARSE latin.py"]


class TestPythonHostFallback:
    @pytest.mark.skipif(sys.version_info < (3, 14), reason="needs a host that accepts PEP 758")
    def test_target_grammar_from_image_tag(self, tmp_path: Path) -> None:
        source = "try:\n    pass\nexcept ValueError, TypeError:\n    pass\n"
        assert run_parse_checks(tmp_path, {"a.py": source}).errors == []
        result = run_parse_checks(tmp_path, {"a.py": source}, image="python:3.12-slim")
        assert [e.path for e in result.errors] == ["a.py"]

    def test_older_host_findings_are_advisories(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(parse_check, "sys", SimpleNamespace(version_info=(3, 11, 9)))
        with patch("subprocess.run", side_effect=FileNotFoundError("docker")):
            result = run_parse_checks(
                tmp_path, {"bad.py": "def f(:\n"}, image="python:3.12-slim", python_image="x"
            )
        assert result.errors == []
        [advisory] = result.advisories
        assert advisory.startswith("[parse] bad.py: ")
        assert "host Python 3.11 is older than the sandbox's 3.12" in advisory

    def test_incidental_python_findings_are_advisories(self, tmp_path: Path) -> None:
        """#119: the Python of a non-Python project is unknown; the host grammar cannot block."""
        result = run_parse_checks(
            tmp_path, {"tools/gen.py": "def f(:\n"}, image="node:22-slim", incidental_python=True
        )
        assert result.errors == []
        [advisory] = result.advisories
        assert advisory.startswith("[parse] tools/gen.py: ")
        assert "the project's Python version is unknown; not blocking" in advisory

    def test_python_project_findings_still_block(self, tmp_path: Path) -> None:
        result = run_parse_checks(tmp_path, {"bad.py": "def f(:\n"}, image="node:22-slim")
        assert [e.path for e in result.errors] == ["bad.py"]

    @pytest.mark.parametrize(
        ("image", "version"),
        [
            ("python:3.12-slim", (3, 12)),
            ("docker.io/library/python:3.11", (3, 11)),
            ("python:3-slim", None),
            ("node:20", None),
            (None, None),
        ],
    )
    def test_python_version_from_tag(self, image: str | None, version: object) -> None:
        assert parse_check._python_version(image) == version


class TestGateContainerIdentity:
    """#94: with an owner, gate containers can be found and removed by the sandbox."""

    def test_named_and_labelled_for_the_owner(self, tmp_path: Path) -> None:
        cache = PSAnalyzerCache(PWSH_IMAGE, owner="a1b2c3d4")
        with patch("subprocess.run", return_value=_completed("OK x.ps1")) as run:
            run_parse_checks(
                tmp_path, {"x.ps1": "1\n"}, PWSH_IMAGE, ps_analyzer=cache, owner="a1b2c3d4"
            )
        install, gate = (call.args[0] for call in run.call_args_list)
        for cmd in (install, gate):
            name = cmd[cmd.index("--name") + 1]
            assert name.startswith("delegatron-a1b2c3d4-gate-")
            assert "delegatron.sandbox=a1b2c3d4" in cmd
        assert gate[:5] == ["docker", "run", "--rm", "--network", "none"]

    def test_without_owner_no_identity(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_completed("OK x.ps1")) as run:
            run_parse_checks(tmp_path, {"x.ps1": "1\n"}, PWSH_IMAGE)
        assert "--name" not in run.call_args.args[0]

    def test_timed_out_gate_container_is_removed(self, tmp_path: Path) -> None:
        calls: list[list[str]] = []

        def _run(cmd: list[str], **_kwargs: Any) -> MagicMock:
            calls.append(cmd)
            if cmd[:2] == ["docker", "run"]:
                raise subprocess.TimeoutExpired(cmd, 300)
            return _completed()

        with patch("subprocess.run", side_effect=_run):
            result = run_parse_checks(tmp_path, {"x.ps1": "1\n"}, PWSH_IMAGE, owner="a1b2c3d4")

        gate, remove = calls
        assert remove == ["docker", "rm", "-f", gate[gate.index("--name") + 1]]
        assert result.advisories[0].startswith("PowerShell parse-check skipped: ")


class TestParseErrorFormat:
    def test_format_contains_path_and_message(self) -> None:
        err = ParseError(
            path="src/foo.py",
            language="Python",
            message="invalid syntax at line 5",
        )
        formatted = err.format()
        assert "Python" in formatted
        assert "src/foo.py" in formatted
        assert "invalid syntax" in formatted


def test_docstring_describes_severity_split() -> None:
    doc = inspect.getdoc(parse_check) or ""
    assert "-Severity Error" not in doc
    assert "BLOCKING_PS_RULES" in doc
    assert "advisories" in doc


class TestDockerRunnerIntegration:
    """Smoke test that docker_runner wires the gate in correctly."""

    def test_gate_import_works(self) -> None:
        # Just confirm the import succeeds; the gate is used in docker_runner.run()
        from delegatron.sandbox.docker_runner import run_parse_checks as imported

        assert imported is run_parse_checks
