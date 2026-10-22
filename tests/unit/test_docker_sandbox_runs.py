"""Docker sandbox runs with ``subprocess.run`` stubbed (no Docker needed).

Covers per-run toolchain and image selection (#93), the install cache,
container identity, timeouts and the build toolchain (#94), staging (#95,
#109, #110, #117), per-language commands (#96), patch test commands and
targeted tests (#104) and the parse-check gate wiring (#119).
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch as mock_patch

import pytest

from delegatron.factories import sandbox_factory
from delegatron.sandbox import base as sandbox_base
from delegatron.sandbox import docker_runner, toolchains
from delegatron.sandbox.base import BaseSandbox
from delegatron.sandbox.docker_runner import (
    DockerSandbox,
    _compute_cache_key,
    _strip_stdlib_from_requirements,
    _validate_sandbox_path,
)
from delegatron.sandbox.parse_check import ParseCheckResult, ParseError
from delegatron.sandbox.toolchains import CMAKE_TOOLCHAIN_INSTALL

# The real subprocess.run, for the few tests that run GNU patch with Docker stubbed.
_REAL_RUN = subprocess.run


class _Result:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakeDocker:
    """``subprocess.run`` stand-in that records every command and answers docker.

    ``docker run`` answers come from ``responses`` keyed by container phase
    (the phase part of the container name, ``"gate"`` for the unnamed
    parse-check containers); so do ``"commit"``, ``"rm"`` and ``"ps"``.  An
    answer is a ``_Result``, an exception to raise, a callable taking the
    command, or a list of those consumed one per call.  Default: success.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.responses: dict[str, Any] = {}
        self.missing_images: set[str] = set()

    def __call__(self, cmd: list[str], **_kwargs: Any) -> Any:
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[0] != "docker":
            return _Result()
        sub = cmd[1]
        if sub == "version":
            return _Result(stdout="linux\n")
        if sub == "image":
            if "--format" in cmd:
                return _Result(stdout="100000000\n")
            return _Result(returncode=1 if cmd[3] in self.missing_images else 0)
        key = _phase(cmd) if sub == "run" else sub
        default = _Result(stdout="1 passed in 0.1s\n") if sub == "run" else _Result()
        return self._answer(self.responses.get(key, default), cmd)

    @staticmethod
    def _answer(response: Any, cmd: list[str]) -> Any:
        if isinstance(response, list):
            response = response.pop(0) if len(response) > 1 else response[0]
        if isinstance(response, BaseException):
            raise response
        return response(cmd) if callable(response) else response

    def runs(self, phase: str) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["docker", "run"] and _phase(c) == phase]


def _phase(cmd: list[str]) -> str:
    if "--name" not in cmd:
        return "gate"
    return cmd[cmd.index("--name") + 1].split("-")[2]


def _name(cmd: list[str]) -> str:
    return cmd[cmd.index("--name") + 1]


def _staged_root(cmd: list[str]) -> Path:
    mount = next(arg for arg in cmd if arg.endswith(":/workspace:rw"))
    return Path(mount[: -len(":/workspace:rw")])


@pytest.fixture
def fake_docker() -> Iterator[FakeDocker]:
    fake = FakeDocker()
    with mock_patch.object(subprocess, "run", side_effect=fake):
        yield fake


@pytest.fixture
def make_sandbox(fake_docker: FakeDocker) -> Iterator[Callable[..., DockerSandbox]]:
    """Build sandboxes that are cleaned up while ``subprocess.run`` is still stubbed."""
    created: list[DockerSandbox] = []

    def make(workspace: Path, **kwargs: Any) -> DockerSandbox:
        sandbox = DockerSandbox(workspace_root=workspace, **kwargs)
        created.append(sandbox)
        return sandbox

    yield make
    for sandbox in created:
        sandbox.cleanup()


def _workspace(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    for rel_path, content in (files or {}).items():
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


_PY_PATCH = {"files": {"card.py": "X = 1\n", "tests/test_card.py": "def test_x():\n    pass\n"}}
_PIP = ["pip install -r requirements.txt -q", "pip install pytest -q"]


# ---------------------------------------------------------------------------
# Toolchain and image per run (#93, #96)
# ---------------------------------------------------------------------------


class TestToolchainSelection:
    @pytest.mark.parametrize("language", ["", "python"])
    def test_python_project_ignores_incidental_markers(
        self,
        tmp_path: Path,
        fake_docker: FakeDocker,
        make_sandbox: Callable[..., DockerSandbox],
        language: str,
    ) -> None:
        ws = _workspace(
            tmp_path,
            {
                "pyproject.toml": "[project]\nname = 'x'\n",
                "Makefile": "test:\n\tpytest\n",
                "install.ps1": "Write-Output hi\n",
                "package.json": "{}",
            },
        )
        sandbox = make_sandbox(ws, language=language, install_commands=["pip install pytest -q"])
        report = sandbox._apply_and_test_sync(_PY_PATCH)

        assert report["passed"] is True
        [install] = fake_docker.runs("install")
        [test] = fake_docker.runs("test")
        assert "python:3.12-slim" in install
        assert install[-1] == "pip install -e . -q && pip install pytest -q"
        assert test[-1] == "python -m pytest -v"
        for tool in ("make", "pwsh", "npm"):
            assert tool not in install[-1] and tool not in test[-1]
        assert "PYTHONPATH=/workspace" in test

    def test_greenfield_node_project_switches_image(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        sandbox = make_sandbox(_workspace(tmp_path), install_commands=["pip install pytest -q"])
        package_json = '{"name": "t", "scripts": {"test": "node --test"}}'
        report = sandbox._apply_and_test_sync(
            {"files": {"package.json": package_json, "index.js": "module.exports = 1;\n"}}
        )

        [install] = fake_docker.runs("install")
        [test] = fake_docker.runs("test")
        assert "node:22-slim" in install
        assert install[-1] == "npm install"
        assert test[-1] == "npm test"
        same_manifests = tmp_path / "same"
        same_manifests.mkdir()
        (same_manifests / "package.json").write_text(package_json, encoding="utf-8")
        tag = sandbox._cache_tag(
            _compute_cache_key("node:22-slim", ["npm install"], same_manifests)
        )
        assert ["docker", "commit", _name(install), tag] in fake_docker.calls
        assert tag in test
        assert report["_sandbox"]["image"] == "node:22-slim"

    def test_explicit_image_is_never_switched(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        sandbox = make_sandbox(
            _workspace(tmp_path), image="registry.local/ci:1", image_explicit=True
        )
        report = sandbox._apply_and_test_sync({"files": {"package.json": "{}"}})

        [install] = fake_docker.runs("install")
        assert "registry.local/ci:1" in install
        assert install[-1] == "npm install"
        assert report["_sandbox"]["image"] == "registry.local/ci:1"

    def test_missing_toolchain_names_tool_and_image(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        fake_docker.responses["probe"] = _Result(returncode=127)
        sandbox = make_sandbox(_workspace(tmp_path), image_explicit=True)
        report = sandbox._apply_and_test_sync(
            {"files": {"Cargo.toml": "[package]\nname = 'x'\n", "src/main.rs": "fn main() {}\n"}}
        )

        assert report["passed"] is False
        assert "toolchain 'cargo' for rust not available in python:3.12-slim" in report["stderr"]
        [probe] = fake_docker.runs("probe")
        assert probe[-1] == "command -v cargo"
        assert probe[probe.index("--network") + 1] == "none"
        assert not fake_docker.runs("install")

    def test_cmake_builds_in_the_test_script(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        sandbox = make_sandbox(_workspace(tmp_path))
        sandbox._apply_and_test_sync(
            {"files": {"CMakeLists.txt": "project(x)\n", "main.cpp": "int main() {}\n"}}
        )

        [install] = fake_docker.runs("install")
        [test] = fake_docker.runs("test")
        assert "gcc:14" in install
        assert install[-1] == CMAKE_TOOLCHAIN_INSTALL
        assert "cmake --build build" in test[-1]
        assert "ctest --test-dir build --no-tests=error" in test[-1]
        assert "Blocked unsafe command" not in test[-1]

    def test_marker_workspace_without_cache_activates_the_venv(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        """#93 criterion 6 on the uncached path: apt retry, venv install, venv-activated tests."""
        ws = _workspace(
            tmp_path,
            {
                "pyproject.toml": "[project]\nname = 'x'\n",
                "Makefile": "test:\n\tpytest\n",
                "install.ps1": "Write-Output hi\n",
            },
        )
        gcc_missing = _Result(returncode=1, stderr="error: command 'gcc' failed: No such file")
        fake_docker.responses["install"] = [gcc_missing, _Result(), _Result()]
        fake_docker.responses["commit"] = _Result(returncode=1, stderr="commit failed")
        sandbox = make_sandbox(ws, install_commands=["pip install pytest -q"])

        report = sandbox._apply_and_test_sync(_PY_PATCH)

        assert report["passed"] is True
        first, retry, venv_install = fake_docker.runs("install")
        assert first[-1] == "pip install -e . -q && pip install pytest -q"
        assert retry[-1].startswith("apt-get update -qq && apt-get install -y -qq gcc g++ make")
        assert retry[-1].endswith("pip install -e . -q && pip install pytest -q")
        assert venv_install[-1] == f"{docker_runner._VENV_PREFIX} && {retry[-1]}"
        [test] = fake_docker.runs("test")
        assert test[-1] == f"{docker_runner._VENV_PREFIX} && python -m pytest -v"
        for run in (first, retry, venv_install, test):
            assert "PYTHONPATH=/workspace" in run
        for run in (first, test):  # the apt step installs the "make" package, not a make run
            for tool in ("make", "pwsh", "npm"):
                assert tool not in run[-1].split()
        assert report["_sandbox"]["cached_image"] is None

    def test_cmake_install_is_skipped_when_the_image_has_cmake(self) -> None:
        """#96: the apt step runs only when cmake is missing, so a non-apt image works."""
        assert CMAKE_TOOLCHAIN_INSTALL.startswith("command -v cmake >/dev/null 2>&1 || (")
        assert "apt-get install -y -qq cmake g++ make" in CMAKE_TOOLCHAIN_INSTALL

    def test_explicit_non_apt_image_is_not_refused_for_apt(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        """#96: the toolchain probe no longer asks a cmake image for apt-get."""
        fake_docker.responses["probe"] = lambda cmd: _Result(
            returncode=0 if cmd[-1] in ("command -v command", "command -v cmake") else 127
        )
        sandbox = make_sandbox(_workspace(tmp_path), image="alpine-cmake:1", image_explicit=True)
        report = sandbox._apply_and_test_sync(
            {"files": {"CMakeLists.txt": "project(x)\n", "main.cpp": "int main() {}\n"}}
        )
        assert "not available" not in report["stderr"]
        [install] = fake_docker.runs("install")
        assert install[-1] == CMAKE_TOOLCHAIN_INSTALL

    def test_windows_image_gets_no_apt_install(
        self,
        tmp_path: Path,
        fake_docker: FakeDocker,
        make_sandbox: Callable[..., DockerSandbox],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(docker_runner, "_get_docker_os", lambda: "windows")
        sandbox = make_sandbox(
            _workspace(tmp_path),
            image="mcr.microsoft.com/windows/servercore:ltsc2022",
            image_explicit=True,
            install_commands=[CMAKE_TOOLCHAIN_INSTALL],
            language="cpp",
            test_commands=["ctest"],
        )
        sandbox._apply_and_test_sync({"files": {"main.cpp": "int main() {}\n"}})
        assert not fake_docker.runs("install")
        assert not [c for c in fake_docker.calls if "apt-get" in " ".join(c)]

    def test_rust_fetches_then_tests_offline(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        ws = _workspace(tmp_path, {"Cargo.toml": "[package]\nname = 'x'\n"})
        sandbox = make_sandbox(
            ws,
            image="rust:1.82-slim-bookworm",
            language="rust",
            install_commands=["cargo fetch"],
            test_commands=["cargo test"],
        )
        sandbox._apply_and_test_sync({"files": {"src/lib.rs": "pub fn f() {}\n"}})

        [install] = fake_docker.runs("install")
        [test] = fake_docker.runs("test")
        assert install[-1] == "cargo fetch"
        assert "CARGO_NET_OFFLINE=true" not in install
        assert "CARGO_NET_OFFLINE=true" in test
        assert test[test.index("--network") + 1] == "none"


class TestPatchCommands:
    def test_unsafe_patch_test_command_never_runs(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        sandbox = make_sandbox(_workspace(tmp_path))
        report = sandbox._apply_and_test_sync({**_PY_PATCH, "test_commands": ["pytest; rm -rf /"]})

        assert report["passed"] is False
        assert report["stderr"] == "Blocked unsafe command: pytest; rm -rf /"
        assert not [c for c in fake_docker.calls if c[:2] == ["docker", "run"]]

    def test_patch_test_commands_bypass_auto_detection(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        sandbox = make_sandbox(_workspace(tmp_path))
        sandbox._apply_and_test_sync(
            {"files": {"package.json": "{}"}, "test_commands": ["npx jest tests/a.test.js"]}
        )

        [test] = fake_docker.runs("test")
        assert test[-1] == "npx jest tests/a.test.js"

    def test_targeted_tests_run_before_the_suite(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        sandbox = make_sandbox(_workspace(tmp_path))
        report = sandbox._apply_and_test_sync(
            {
                **_PY_PATCH,
                "targeted_tests": ["python -m pytest tests/test_card.py -v", "rm -rf /tmp/x"],
            }
        )

        phases = [_phase(c) for c in fake_docker.calls if c[:2] == ["docker", "run"]]
        assert phases.index("targeted") < phases.index("test")
        [targeted] = fake_docker.runs("targeted")
        assert targeted[-1].endswith("python -m pytest tests/test_card.py -v")
        assert "rm -rf" not in targeted[-1]
        assert targeted[targeted.index("--network") + 1] == "none"
        assert report["targeted"]["passed"] is True


# ---------------------------------------------------------------------------
# Install cache, container identity, timeouts, build toolchain (#94)
# ---------------------------------------------------------------------------


class TestCacheKey:
    def test_changes_with_manifest_contents_only(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("requests\n")
        key = _compute_cache_key("img", _PIP, tmp_path)
        (tmp_path / "notes.md").write_text("x")
        (tmp_path / "app.py").write_text("x = 1\n")
        assert _compute_cache_key("img", _PIP, tmp_path) == key
        (tmp_path / "requirements.txt").write_text("requests\nnumpy\n")
        assert _compute_cache_key("img", _PIP, tmp_path) != key

    @pytest.mark.parametrize(
        "manifest", ["package.json", "go.mod", "src/App/App.csproj", "Pipfile.lock"]
    )
    def test_dependency_manifests(self, tmp_path: Path, manifest: str) -> None:
        path = tmp_path / manifest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("v1")
        key = _compute_cache_key("img", [], tmp_path)
        path.write_text("v2")
        assert _compute_cache_key("img", [], tmp_path) != key

    def test_installed_dependency_dirs_are_not_scanned(self, tmp_path: Path) -> None:
        nested = tmp_path / "node_modules" / "left-pad" / "package.json"
        nested.parent.mkdir(parents=True)
        nested.write_text("v1")
        key = _compute_cache_key("img", [], tmp_path)
        nested.write_text("v2")
        assert _compute_cache_key("img", [], tmp_path) == key

    def test_image_and_commands_count(self, tmp_path: Path) -> None:
        key = _compute_cache_key("img", ["a", "b"], tmp_path)
        assert _compute_cache_key("img", ["b", "a"], tmp_path) == key
        assert _compute_cache_key("img2", ["a", "b"], tmp_path) != key
        assert _compute_cache_key("img", ["a"], tmp_path) != key


class TestInstallCache:
    def _python_sandbox(
        self, tmp_path: Path, make_sandbox: Callable[..., DockerSandbox]
    ) -> DockerSandbox:
        ws = _workspace(tmp_path, {"requirements.txt": "requests\n"})
        return make_sandbox(ws, language="python", install_commands=list(_PIP))

    def test_new_dependency_reinstalls(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        sandbox = self._python_sandbox(tmp_path, make_sandbox)
        first = sandbox._apply_and_test_sync(_PY_PATCH)
        second = sandbox._apply_and_test_sync({"files": {"requirements.txt": "requests\nnumpy\n"}})

        assert len(fake_docker.runs("install")) == 2
        assert first["_sandbox"]["cache_hit"] is False
        assert second["_sandbox"]["cache_hit"] is False
        [_, second_test] = fake_docker.runs("test")
        assert second["_sandbox"]["cached_image"] in second_test
        assert first["_sandbox"]["cached_image"] not in second_test

    def test_unchanged_manifests_hit_the_cache(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        sandbox = self._python_sandbox(tmp_path, make_sandbox)
        sandbox._apply_and_test_sync(_PY_PATCH)
        second = sandbox._apply_and_test_sync(_PY_PATCH)

        assert len(fake_docker.runs("install")) == 1
        assert second["_sandbox"]["cache_hit"] is True

    def test_package_json_change_mounts_a_new_node_modules_volume(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        sandbox = make_sandbox(
            _workspace(tmp_path),
            image="node:22-slim",
            language="javascript",
            install_commands=["npm install"],
            test_commands=["npm test"],
        )
        sandbox._apply_and_test_sync({"files": {"package.json": '{"dependencies": {"a": "1"}}'}})
        sandbox._apply_and_test_sync(
            {"files": {"package.json": '{"dependencies": {"a": "1", "b": "2"}}'}}
        )

        volumes = [
            next(arg for arg in run if arg.endswith(":/workspace/node_modules"))
            for run in fake_docker.runs("test")
        ]
        assert len(set(volumes)) == 2

    def test_instances_never_share_tags_or_volumes(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        ws = _workspace(tmp_path, {"package.json": "{}"})
        kwargs: dict[str, Any] = {
            "image": "node:22-slim",
            "language": "javascript",
            "install_commands": ["npm install"],
            "test_commands": ["npm test"],
        }
        one, two = make_sandbox(ws, **kwargs), make_sandbox(ws, **kwargs)
        one._apply_and_test_sync({"files": {"index.js": ""}})
        two._apply_and_test_sync({"files": {"index.js": ""}})

        assert one._cached_images.isdisjoint(two._cached_images)
        assert one._cached_volumes.isdisjoint(two._cached_volumes)
        two_tags = set(two._cached_images)
        one.cleanup()
        [rmi] = [c for c in fake_docker.calls if c[:2] == ["docker", "rmi"]]
        assert two_tags.isdisjoint(rmi)

    def test_containers_are_addressed_by_name(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        sandbox = self._python_sandbox(tmp_path, make_sandbox)
        sandbox._apply_and_test_sync(_PY_PATCH)

        assert not [c for c in fake_docker.calls if "-lq" in c]
        [install] = fake_docker.runs("install")
        assert "--rm" not in install
        assert f"delegatron.sandbox={sandbox._instance_id}" in install
        [commit] = [c for c in fake_docker.calls if c[:2] == ["docker", "commit"]]
        assert commit[2] == _name(install)
        assert ["docker", "rm", "-f", _name(install)] in fake_docker.calls

    def test_vanished_cached_image_is_reinstalled(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        sandbox = self._python_sandbox(tmp_path, make_sandbox)
        first = sandbox._apply_and_test_sync(_PY_PATCH)
        fake_docker.missing_images.add(first["_sandbox"]["cached_image"])
        second = sandbox._apply_and_test_sync(_PY_PATCH)

        assert len(fake_docker.runs("install")) == 2
        assert second["_sandbox"]["cache_hit"] is False

    @pytest.mark.parametrize(
        "failure",
        [_Result(returncode=1, stderr="no space left"), subprocess.TimeoutExpired("docker", 120)],
    )
    def test_failed_commit_installs_into_the_venv(
        self,
        tmp_path: Path,
        fake_docker: FakeDocker,
        make_sandbox: Callable[..., DockerSandbox],
        failure: Any,
    ) -> None:
        fake_docker.responses["commit"] = [_Result(), failure]
        sandbox = self._python_sandbox(tmp_path, make_sandbox)
        first = sandbox._apply_and_test_sync(_PY_PATCH)
        second = sandbox._apply_and_test_sync({"files": {"requirements.txt": "requests\nnumpy\n"}})

        assert second["passed"] is True
        assert second["_sandbox"]["cached_image"] is None
        _, kept, reinstall = fake_docker.runs("install")
        assert "--rm" not in kept
        assert "--rm" in reinstall
        assert reinstall[-1].startswith(docker_runner._VENV_PREFIX)
        assert reinstall[-1].endswith(kept[-1])
        [_, test] = fake_docker.runs("test")
        assert "python:3.12-slim" in test
        assert first["_sandbox"]["cached_image"] not in test
        assert test[-1].startswith(docker_runner._VENV_PREFIX)

    def test_failed_commit_keeps_go_modules_on_a_volume(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        """#94: without a committed image the offline Go tests still find the modules."""
        fake_docker.responses["commit"] = _Result(returncode=1, stderr="no space left")
        ws = _workspace(tmp_path, {"go.mod": "module x\n"})
        sandbox = make_sandbox(
            ws,
            image="golang:1.23-bookworm",
            language="go",
            install_commands=["go mod download"],
            test_commands=["go test -v ./..."],
        )

        report = sandbox._apply_and_test_sync({"files": {"x_test.go": "package x\n"}})

        assert report["passed"] is True
        assert report["_sandbox"]["cached_image"] is None
        kept, reinstall = fake_docker.runs("install")
        [test] = fake_docker.runs("test")
        volume = next(arg for arg in reinstall if arg.endswith(":/go/pkg/mod"))
        assert volume.startswith(f"delegatron-deps-{sandbox._instance_id}-")
        assert volume not in kept
        assert volume in test
        assert test[test.index("--network") + 1] == "none"
        assert "golang:1.23-bookworm" in test
        sandbox.cleanup()
        [volume_rm] = [c for c in fake_docker.calls if c[:3] == ["docker", "volume", "rm"]]
        assert volume.split(":")[0] in volume_rm

    def test_slow_daemon_on_the_cache_check_reinstalls(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        """#94: a timed-out ``docker image inspect`` is a cache miss, not a sandbox error."""
        sandbox = self._python_sandbox(tmp_path, make_sandbox)
        first = sandbox._apply_and_test_sync(_PY_PATCH)
        tag = first["_sandbox"]["cached_image"]

        def slow_inspect(cmd: list[str], **kwargs: Any) -> Any:
            if cmd[:3] == ["docker", "image", "inspect"] and tag in cmd:
                raise subprocess.TimeoutExpired(cmd, 10)
            return fake_docker(cmd, **kwargs)

        with mock_patch.object(subprocess, "run", side_effect=slow_inspect):
            second = sandbox._apply_and_test_sync(_PY_PATCH)

        assert second["passed"] is True
        assert second["_sandbox"]["cache_hit"] is False
        assert len(fake_docker.runs("install")) == 2

    def test_pull_timeout_is_reported_not_raised(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        fake_docker.missing_images.add("python:3.12-slim")
        fake_docker.responses["pull"] = subprocess.TimeoutExpired("docker pull", 600)
        report = make_sandbox(_workspace(tmp_path))._apply_and_test_sync(_PY_PATCH)
        assert report["_sandbox"]["pull"]["status"] == "pull_failed"


class TestTimeouts:
    def test_hung_test_is_named_and_its_container_removed(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        fake_docker.responses["test"] = subprocess.TimeoutExpired(
            "docker", 600, output=b"tests/test_x.py::test_hang "
        )
        sandbox = make_sandbox(_workspace(tmp_path))
        report = sandbox._apply_and_test_sync(_PY_PATCH)

        assert report["passed"] is False
        assert "Test phase timed out after 600s" in report["stderr"]
        assert "tests/test_x.py::test_hang" in report["stdout"]
        [test] = fake_docker.runs("test")
        assert ["docker", "rm", "-f", _name(test)] in fake_docker.calls

    def test_hung_install_is_named_and_its_container_removed(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        fake_docker.responses["install"] = subprocess.TimeoutExpired(
            "docker", 600, output=b"Collecting numpy\n", stderr=b"Building wheel for numpy\n"
        )
        sandbox = make_sandbox(_workspace(tmp_path), install_commands=["pip install numpy -q"])
        report = sandbox._apply_and_test_sync(_PY_PATCH)

        assert "Install phase timed out after 600s" in report["stderr"]
        assert "Building wheel for numpy" in report["stderr"]
        assert "Collecting numpy" in report["stdout"]
        [install] = fake_docker.runs("install")
        assert ["docker", "rm", "-f", _name(install)] in fake_docker.calls
        assert not fake_docker.runs("test")


class TestBuildToolchain:
    _GCC_MISSING = _Result(returncode=1, stderr="error: command 'gcc' failed: No such file")

    def test_first_install_has_no_apt(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        sandbox = make_sandbox(_workspace(tmp_path), install_commands=["pip install x -q"])
        sandbox._apply_and_test_sync(_PY_PATCH)

        [install] = fake_docker.runs("install")
        assert "apt-get" not in install[-1]

    def test_missing_compiler_retries_once_with_apt(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        fake_docker.responses["install"] = [self._GCC_MISSING, _Result()]
        sandbox = make_sandbox(_workspace(tmp_path), install_commands=["pip install x -q"])
        report = sandbox._apply_and_test_sync(_PY_PATCH)

        first, retry = fake_docker.runs("install")
        assert "apt-get" not in first[-1]
        assert retry[-1].startswith("apt-get update -qq && apt-get install -y -qq gcc g++ make")
        assert "/dev/null" not in retry[-1]
        assert ["docker", "rm", "-f", _name(first)] in fake_docker.calls
        assert report["passed"] is True

    def test_other_failures_do_not_retry(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        fake_docker.responses["install"] = _Result(
            returncode=1, stderr="No matching distribution found for nump"
        )
        sandbox = make_sandbox(_workspace(tmp_path), install_commands=["pip install nump -q"])
        report = sandbox._apply_and_test_sync(_PY_PATCH)

        assert len(fake_docker.runs("install")) == 1
        assert report["stderr"].startswith("Dependency install failed:")
        assert "No matching distribution" in report["stderr"]

    def test_failed_retry_reports_the_apt_output(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        fake_docker.responses["install"] = [
            self._GCC_MISSING,
            _Result(returncode=100, stderr="E: Unable to locate package g++"),
        ]
        sandbox = make_sandbox(_workspace(tmp_path), install_commands=["pip install x -q"])
        report = sandbox._apply_and_test_sync(_PY_PATCH)

        assert report["passed"] is False
        assert "E: Unable to locate package g++" in report["stderr"]


class TestLifecycle:
    def test_base_sandbox_cleanup_is_a_no_op(self) -> None:
        class _Sandbox(BaseSandbox):
            async def apply_and_test(self, patch: dict[str, Any]) -> dict[str, Any]:
                return {}

        sandbox = _Sandbox()
        sandbox.cleanup()
        sandbox.cleanup()

    def test_cleanup_removes_tags_volumes_and_leftover_containers(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        ws = _workspace(tmp_path, {"requirements.txt": "requests\n"})
        sandbox = make_sandbox(ws, language="python", install_commands=list(_PIP))
        fake_docker.responses["rm"] = _Result(returncode=1)  # removal not confirmed
        fake_docker.responses["ps"] = _Result(stdout="c0ffee\n")
        sandbox._apply_and_test_sync(_PY_PATCH)
        sandbox._apply_and_test_sync({"files": {"requirements.txt": "flask\n"}})
        tags = sorted(sandbox._cached_images)
        assert len(tags) == 2

        sandbox.cleanup()
        label = f"label=delegatron.sandbox={sandbox._instance_id}"
        assert ["docker", "ps", "-aq", "--filter", label] in fake_docker.calls
        assert ["docker", "rm", "-f", "c0ffee"] in fake_docker.calls
        assert ["docker", "rmi", *tags] in fake_docker.calls
        assert not sandbox._cached_images

    def test_cleanup_sweeps_labelled_containers_it_believes_gone(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        """#94: every container removal confirmed, yet a labelled one is still swept."""
        fake_docker.responses["ps"] = _Result(stdout="deadbeef\n")
        sandbox = make_sandbox(_workspace(tmp_path))
        sandbox._apply_and_test_sync(_PY_PATCH)
        assert not sandbox._unreaped

        sandbox.cleanup()

        label = f"label=delegatron.sandbox={sandbox._instance_id}"
        assert ["docker", "ps", "-aq", "--filter", label] in fake_docker.calls
        assert ["docker", "rm", "-f", "deadbeef"] in fake_docker.calls
        calls = len(fake_docker.calls)
        sandbox.cleanup()  # nothing new to sweep
        assert len(fake_docker.calls) == calls

    def test_unused_sandbox_cleanup_calls_no_docker(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        make_sandbox(_workspace(tmp_path)).cleanup()
        assert fake_docker.calls == []

    def test_install_network_is_reported(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        sandbox = make_sandbox(_workspace(tmp_path))
        assert sandbox.get_info()["install_network"] is True
        report = sandbox._apply_and_test_sync(_PY_PATCH)
        assert report["_sandbox"]["install_network"] is True
        assert report["_sandbox"]["network_isolation"] is True


# ---------------------------------------------------------------------------
# Staging (#95, #109, #110, #117)
# ---------------------------------------------------------------------------


class TestStaging:
    def _root_owned(self, staging: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
        """Make ``shutil.rmtree`` fail on *staging* while its leftover file exists."""
        real_rmtree = shutil.rmtree
        (staging / ".pytest_cache").mkdir(parents=True)
        leftover = staging / ".pytest_cache" / "nodeids"
        leftover.write_text("[]")

        def rmtree(path: Any, ignore_errors: bool = False, **kwargs: Any) -> None:
            if Path(path).resolve() == staging.resolve() and leftover.exists():
                if ignore_errors:
                    return
                raise PermissionError(13, "Permission denied", str(leftover))
            real_rmtree(path, ignore_errors=ignore_errors, **kwargs)

        monkeypatch.setattr(docker_runner, "_POSIX_HOST", True)
        monkeypatch.setattr(docker_runner.shutil, "rmtree", rmtree)
        return real_rmtree

    def test_root_owned_leftovers_are_removed_in_a_container(
        self,
        tmp_path: Path,
        fake_docker: FakeDocker,
        make_sandbox: Callable[..., DockerSandbox],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws = _workspace(tmp_path)
        staging = ws / "_sandbox" / "workspace"
        real_rmtree = self._root_owned(staging, monkeypatch)

        def remove_as_root(cmd: list[str]) -> _Result:
            real_rmtree(staging)
            return _Result()

        fake_docker.responses["cleanup"] = remove_as_root
        report = make_sandbox(ws)._apply_and_test_sync(_PY_PATCH)

        [cleanup] = fake_docker.runs("cleanup")
        assert cleanup[-3:] == ["rm", "-rf", "/s/workspace"]
        assert cleanup[cleanup.index("--network") + 1] == "none"
        assert report["passed"] is True
        assert _staged_root(fake_docker.runs("test")[0]).name == "workspace"

    def test_unremovable_leftovers_stage_elsewhere(
        self,
        tmp_path: Path,
        fake_docker: FakeDocker,
        make_sandbox: Callable[..., DockerSandbox],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws = _workspace(tmp_path)
        self._root_owned(ws / "_sandbox" / "workspace", monkeypatch)
        fake_docker.responses["cleanup"] = _Result(returncode=1)
        report = make_sandbox(ws)._apply_and_test_sync(_PY_PATCH)

        assert report["passed"] is True
        assert "Errno 13" not in report["stderr"]
        staged = _staged_root(fake_docker.runs("test")[0])
        assert staged.parent.name == "_sandbox"
        assert staged.name.startswith("workspace-")
        assert not staged.exists()

    def test_python_containers_opt_out_of_bytecode_and_pytest_cache(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        make_sandbox(_workspace(tmp_path))._apply_and_test_sync(_PY_PATCH)
        make_sandbox(_workspace(tmp_path / "js"))._apply_and_test_sync(
            {"files": {"package.json": "{}"}}
        )

        python_test, node_test = fake_docker.runs("test")
        assert "PYTHONDONTWRITEBYTECODE=1" in python_test
        assert "PYTEST_ADDOPTS=-p no:cacheprovider" in python_test
        assert "PYTHONDONTWRITEBYTECODE=1" not in node_test
        assert "PYTEST_ADDOPTS=-p no:cacheprovider" not in node_test

    def test_nested_logs_and_runs_are_staged(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        ws = _workspace(
            tmp_path,
            {
                "app/logs/formatter.py": "def fmt(): ...\n",
                "tests/fixtures/runs/a.json": "{}",
                "src/reviews/b.py": "",
                "runs/abc123/summary.json": "{}",  # Delegatron's runs/ layout
                "logs/y.log": "",
                "reviews/review-1/summary.json": "{}",
                ".delegatron-runs/abc/f.txt": "",
                "pkg/__pycache__/m.cpython-312.pyc": "",
            },
        )
        staged: list[str] = []

        def list_staged(cmd: list[str]) -> _Result:
            root = _staged_root(cmd)
            staged.extend(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
            return _Result(stdout="1 passed\n")

        fake_docker.responses["test"] = list_staged
        make_sandbox(ws)._apply_and_test_sync(_PY_PATCH)

        assert {"app/logs/formatter.py", "tests/fixtures/runs/a.json", "src/reviews/b.py"} <= set(
            staged
        )
        for internal in ("runs/", "logs/", "reviews/", ".delegatron-runs/", "_sandbox/"):
            assert not [p for p in staged if p.startswith(internal)]
        assert not [p for p in staged if "__pycache__" in p]

    def test_user_packages_named_runs_or_reviews_are_staged(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        """D4: only Delegatron's own runs/ and reviews/ layouts are internal."""
        ws = _workspace(
            tmp_path, {"reviews/models.py": "class Review: ...\n", "runs/jobs.py": "JOBS = []\n"}
        )
        staged: list[str] = []

        def list_staged(cmd: list[str]) -> _Result:
            root = _staged_root(cmd)
            staged.extend(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
            return _Result(stdout="1 passed\n")

        fake_docker.responses["test"] = list_staged
        make_sandbox(ws)._apply_and_test_sync(_PY_PATCH)
        assert {"reviews/models.py", "runs/jobs.py"} <= set(staged)

    def test_dangling_symlink_is_skipped(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        ws = _workspace(tmp_path)
        try:
            os.symlink(ws / "missing", ws / "current")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available")
        report = make_sandbox(ws)._apply_and_test_sync(_PY_PATCH)
        assert report["passed"] is True

    def test_copy_failure_is_a_setup_failure(
        self,
        tmp_path: Path,
        fake_docker: FakeDocker,
        make_sandbox: Callable[..., DockerSandbox],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def broken_copytree(*args: Any, **kwargs: Any) -> None:
            raise shutil.Error([("a", "b", "boom")])

        monkeypatch.setattr(docker_runner.shutil, "copytree", broken_copytree)
        report = make_sandbox(_workspace(tmp_path))._apply_and_test_sync(_PY_PATCH)
        assert report["passed"] is False
        assert report["stderr"].startswith("Sandbox setup failed (not caused by the patch):")

    def test_staged_bytes_are_exact(
        self,
        tmp_path: Path,
        fake_docker: FakeDocker,
        make_sandbox: Callable[..., DockerSandbox],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real_write_text = Path.write_text

        def write_text(self: Path, data: str, encoding: str | None = None, **kwargs: Any) -> int:
            assert encoding is not None, f"write_text without encoding: {self}"
            return real_write_text(self, data, encoding=encoding, **kwargs)

        monkeypatch.setattr(Path, "write_text", write_text)
        files = {
            "run.sh": "#!/bin/bash\necho hi\n",
            "p.py": "print('Pokémon ✓ — 🎴')\n",
            "win.py": "a = 1\r\nb = 2\r\n",
        }
        written: dict[str, bytes] = {}

        def read_staged(cmd: list[str]) -> _Result:
            root = _staged_root(cmd)
            written.update({name: (root / name).read_bytes() for name in files})
            return _Result(stdout="1 passed\n")

        fake_docker.responses["test"] = read_staged
        make_sandbox(_workspace(tmp_path))._apply_and_test_sync({"files": files})

        assert written == {name: content.encode("utf-8") for name, content in files.items()}

    def test_utf16_requirements_keep_their_packages(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_bytes(
            "flask==3.0\nsqlite3\nrequests\n".encode("utf-16")
        )
        _strip_stdlib_from_requirements(tmp_path)
        lines = (tmp_path / "requirements.txt").read_text(encoding="utf-8").splitlines()
        assert lines == ["flask==3.0", "requests"]


class TestStagePatch:
    def test_moved_file(self, tmp_path: Path) -> None:
        (tmp_path / "test_a.py").write_text("old\n")
        staged = DockerSandbox._stage_patch(
            tmp_path, {"files": {"tests/test_a.py": "new\n"}, "delete_files": ["test_a.py"]}
        )
        assert not (tmp_path / "test_a.py").exists()
        assert (tmp_path / "tests" / "test_a.py").read_text() == "new\n"
        assert staged.files == {"tests/test_a.py": "new\n"}
        assert staged.skipped == []

    def test_deleting_a_missing_file_is_a_no_op(self, tmp_path: Path) -> None:
        staged = DockerSandbox._stage_patch(tmp_path, {"delete_files": ["gone.py"]})
        assert staged.skipped == []

    def test_unsafe_paths_are_skipped_and_the_rest_written(self, tmp_path: Path) -> None:
        sandbox_dir = tmp_path / "sandbox"
        (sandbox_dir / "pkg").mkdir(parents=True)
        (tmp_path / "outside.txt").write_text("keep")
        unsafe = ["../escaped.txt", "C:/x.py", "//host/share/x.py", "/abs.py"]
        staged = DockerSandbox._stage_patch(
            sandbox_dir,
            {
                "files": {"a.py": "a\n", **dict.fromkeys(unsafe, "x"), "b\\c.py": "c\n"},
                "delete_files": ["../outside.txt", "pkg"],
            },
        )
        assert staged.skipped == [*unsafe, "../outside.txt", "pkg"]
        assert staged.files == {"a.py": "a\n", "b/c.py": "c\n"}
        assert (sandbox_dir / "b" / "c.py").exists()
        assert not (tmp_path / "escaped.txt").exists()
        assert (tmp_path / "outside.txt").exists()
        assert (sandbox_dir / "pkg").is_dir()

    def test_skipped_paths_are_reported(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        patch = {"files": {**_PY_PATCH["files"], "../x.py": ""}}
        report = make_sandbox(_workspace(tmp_path))._apply_and_test_sync(patch)
        assert report["stderr"].startswith("Skipped unsafe patch path(s): ../x.py\n")
        assert report["passed"] is True

    def test_unc_paths_are_never_resolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resolved: list[str] = []
        real_resolve = Path.resolve

        def resolve(self: Path, strict: bool = False) -> Path:
            resolved.append(str(self))
            return real_resolve(self, strict)

        monkeypatch.setattr(Path, "resolve", resolve)
        for unsafe in ("//evil.example/share/x.py", r"\\evil.example\share\x.py"):
            with pytest.raises(ValueError, match="Path escapes sandbox"):
                _validate_sandbox_path(tmp_path, unsafe)
        assert not [path for path in resolved if "evil.example" in path]

    def test_patch_fallback_refuses_reversed_hunks(
        self, tmp_path: Path, fake_docker: FakeDocker
    ) -> None:
        DockerSandbox._stage_patch(tmp_path, {"diff_unified": _MODIFY_DIFF})
        assert [c for c in fake_docker.calls if c[0] == "patch"] == [
            ["patch", "-p1", "--forward", "--batch"]
        ]

    @pytest.mark.skipif(shutil.which("patch") is None, reason="GNU patch not installed")
    def test_already_applied_diff_is_refused(self, tmp_path: Path) -> None:
        calc = tmp_path / "calc.py"
        calc.write_bytes(b"def add(a, b):\n    return a + b\n")
        staged = DockerSandbox._stage_patch(tmp_path, {"diff_unified": _MODIFY_DIFF})
        assert staged.error.startswith("Patch failed to apply")
        assert calc.read_bytes() == b"def add(a, b):\n    return a + b\n"

    @pytest.mark.skipif(shutil.which("patch") is None, reason="GNU patch not installed")
    def test_already_applied_diff_fails_the_run(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        """#95 through ``_apply_and_test_sync``: real ``patch``, Docker stubbed."""
        ws = _workspace(tmp_path)
        (ws / "calc.py").write_bytes(b"def add(a, b):\n    return a + b\n")
        staged: list[bytes] = []
        real_rmtree = shutil.rmtree

        def run(cmd: list[str], **kwargs: Any) -> Any:
            if cmd[0] == "patch":
                result = _REAL_RUN(cmd, **kwargs)
                staged.append((Path(kwargs["cwd"]) / "calc.py").read_bytes())
                return result
            return fake_docker(cmd, **kwargs)

        with (
            mock_patch.object(subprocess, "run", side_effect=run),
            mock_patch.object(docker_runner.shutil, "rmtree", side_effect=real_rmtree),
        ):
            report = make_sandbox(ws)._apply_and_test_sync({"diff_unified": _MODIFY_DIFF})

        assert report["passed"] is False
        assert report["stderr"].startswith("Patch failed to apply")
        assert staged == [b"def add(a, b):\n    return a + b\n"]  # not reverted
        assert not fake_docker.runs("test")

    @pytest.mark.skipif(shutil.which("patch") is None, reason="GNU patch not installed")
    def test_new_diff_is_tested_with_its_change(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        ws = _workspace(tmp_path)
        (ws / "calc.py").write_bytes(b"def add(a, b):\n    return a - b\n")
        tested: list[bytes] = []

        def read_staged(cmd: list[str]) -> _Result:
            tested.append((_staged_root(cmd) / "calc.py").read_bytes())
            return _Result(stdout="1 passed\n")

        def run(cmd: list[str], **kwargs: Any) -> Any:
            return _REAL_RUN(cmd, **kwargs) if cmd[0] == "patch" else fake_docker(cmd, **kwargs)

        fake_docker.responses["test"] = read_staged
        with mock_patch.object(subprocess, "run", side_effect=run):
            report = make_sandbox(ws)._apply_and_test_sync({"diff_unified": _MODIFY_DIFF})

        assert report["passed"] is True
        assert tested == [b"def add(a, b):\n    return a + b\n"]

    @pytest.mark.skipif(shutil.which("patch") is None, reason="GNU patch not installed")
    def test_applied_diff_files_reach_the_gate(self, tmp_path: Path) -> None:
        (tmp_path / "calc.py").write_bytes(b"def add(a, b):\n    return a - b\n")
        staged = DockerSandbox._stage_patch(tmp_path, {"diff_unified": _MODIFY_DIFF})
        assert staged.error == ""
        assert staged.files == {"calc.py": "def add(a, b):\n    return a + b\n"}


_MODIFY_DIFF = (
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a - b\n"
    "+    return a + b\n"
)


# ---------------------------------------------------------------------------
# Parse-check gate wiring (#119)
# ---------------------------------------------------------------------------


class TestParseGate:
    def test_diff_only_patch_is_checked(
        self,
        tmp_path: Path,
        fake_docker: FakeDocker,
        make_sandbox: Callable[..., DockerSandbox],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        checked: dict[str, str] = {}

        def gate(sandbox_dir: Path, files: dict[str, str], *args: Any, **kwargs: Any) -> Any:
            checked.update(files)
            return ParseCheckResult()

        monkeypatch.setattr(docker_runner, "run_parse_checks", gate)
        diff = "--- /dev/null\n+++ b/new_mod.py\n@@ -0,0 +1 @@\n+x = 1\n"
        make_sandbox(_workspace(tmp_path))._apply_and_test_sync({"diff_unified": diff})
        assert checked == {"new_mod.py": "x = 1\n"}

    def test_errors_block_and_advisories_are_reported(
        self,
        tmp_path: Path,
        fake_docker: FakeDocker,
        make_sandbox: Callable[..., DockerSandbox],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lint = "[lint] a.ps1: [PSUseSingularNouns] use singular nouns"
        results = [
            ParseCheckResult(errors=[ParseError("a.py", "Python", "invalid syntax")]),
            ParseCheckResult(advisories=[lint]),
        ]
        monkeypatch.setattr(docker_runner, "run_parse_checks", lambda *a, **k: results.pop(0))
        sandbox = make_sandbox(_workspace(tmp_path))

        blocked = sandbox._apply_and_test_sync(_PY_PATCH)
        assert blocked["passed"] is False
        assert blocked["stderr"].startswith("Parse-check gate failed:\nPython parse error in a.py")
        assert not fake_docker.runs("test")

        advised = sandbox._apply_and_test_sync(_PY_PATCH)
        assert advised["passed"] is True
        assert advised["lint_warnings"] == [lint]

    def test_gate_runs_after_the_mode_switch(
        self,
        tmp_path: Path,
        fake_docker: FakeDocker,
        make_sandbox: Callable[..., DockerSandbox],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        image = "mcr.microsoft.com/powershell:lts-nanoserver-ltsc2022"
        events: list[tuple[str, str]] = []

        def switch(target: str) -> bool:
            events.append(("switch", target))
            return True

        def gate(sandbox_dir: Path, files: dict[str, str], gate_image: str, **kwargs: Any) -> Any:
            events.append(("gate", gate_image))
            return ParseCheckResult()

        monkeypatch.setattr(docker_runner, "_get_docker_os", lambda: "linux")
        monkeypatch.setattr(docker_runner, "_switch_docker_mode", switch)
        monkeypatch.setattr(docker_runner, "run_parse_checks", gate)
        sandbox = make_sandbox(_workspace(tmp_path), image=image, image_explicit=True)
        sandbox._apply_and_test_sync({"files": {"Get-User.ps1": "function Get-User {}\n"}})

        assert events == [("switch", "windows"), ("gate", image), ("switch", "linux")]

    def test_gate_containers_carry_the_sandbox_label(
        self, tmp_path: Path, fake_docker: FakeDocker, make_sandbox: Callable[..., DockerSandbox]
    ) -> None:
        """#94: cleanup() can sweep a gate container (named and labelled like the others)."""
        sandbox = make_sandbox(_workspace(tmp_path))
        sandbox._apply_and_test_sync(_PY_PATCH)

        [gate] = fake_docker.runs("gate")
        assert _name(gate).startswith(f"delegatron-{sandbox._instance_id}-gate-")
        assert f"delegatron.sandbox={sandbox._instance_id}" in gate
        assert gate[gate.index("--network") + 1] == "none"

    def test_incidental_python_in_another_language_does_not_block(
        self,
        tmp_path: Path,
        fake_docker: FakeDocker,
        make_sandbox: Callable[..., DockerSandbox],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#119: a node project's helper script is checked, but never blocks on the host grammar."""
        seen: dict[str, Any] = {}

        def gate(*args: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return ParseCheckResult()

        monkeypatch.setattr(docker_runner, "run_parse_checks", gate)
        sandbox = make_sandbox(_workspace(tmp_path), language="javascript", image="node:22-slim")
        sandbox._apply_and_test_sync({"files": {"package.json": "{}", "tools/gen.py": "x = 1\n"}})
        assert seen["incidental_python"] is True
        assert seen["python_image"] is None
        assert seen["owner"] == sandbox._instance_id

    def test_analyzer_volume_is_cleaned_up(
        self,
        tmp_path: Path,
        fake_docker: FakeDocker,
        make_sandbox: Callable[..., DockerSandbox],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        volumes: list[str] = []

        def gate(*args: Any, ps_analyzer: Any, **kwargs: Any) -> Any:
            ps_analyzer.status = "ready"
            volumes.append(ps_analyzer.volume)
            return ParseCheckResult()

        monkeypatch.setattr(docker_runner, "run_parse_checks", gate)
        sandbox = make_sandbox(_workspace(tmp_path))
        sandbox._apply_and_test_sync(_PY_PATCH)
        sandbox.cleanup()

        [volume_rm] = [c for c in fake_docker.calls if c[:3] == ["docker", "volume", "rm"]]
        assert volumes[0] in volume_rm


# ---------------------------------------------------------------------------
# Cancellation (the orchestrator's wall-time deadline)
# ---------------------------------------------------------------------------


class TestCancellation:
    async def test_cancelled_call_removes_running_containers_and_starts_none(
        self,
        tmp_path: Path,
        fake_docker: FakeDocker,
        make_sandbox: Callable[..., DockerSandbox],
    ) -> None:
        import asyncio
        import threading

        sandbox = make_sandbox(_workspace(tmp_path))
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        outcome: list[str] = []

        def _sync(_patch: dict[str, Any]) -> dict[str, Any]:
            # A test container is running when the deadline cancels the call ...
            sandbox._docker_run("test", ["img", "pytest"])
            entered.set()
            release.wait(10)
            # ... and the worker thread goes on to its next phase afterwards.
            try:
                sandbox._docker_run("rerun", ["img", "pytest"])
                outcome.append("started")
            except docker_runner._RunCancelled:
                outcome.append("refused")
            finished.set()
            return {}

        sandbox._apply_and_test_sync = _sync  # type: ignore[method-assign]
        fake_docker.responses["ps"] = _Result(stdout="c0ffee\n")
        task = asyncio.create_task(sandbox.apply_and_test(dict(_PY_PATCH)))
        while not entered.is_set():
            await asyncio.sleep(0.01)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        label = f"label=delegatron.sandbox={sandbox._instance_id}"
        assert ["docker", "ps", "-q", "--filter", label] in fake_docker.calls
        assert ["docker", "rm", "-f", "c0ffee"] in fake_docker.calls
        release.set()
        assert await asyncio.to_thread(finished.wait, 10)
        assert outcome == ["refused"]
        assert fake_docker.runs("rerun") == []

    async def test_uncancelled_calls_are_not_affected(
        self,
        tmp_path: Path,
        fake_docker: FakeDocker,
        make_sandbox: Callable[..., DockerSandbox],
    ) -> None:
        sandbox = make_sandbox(_workspace(tmp_path))

        first = await sandbox.apply_and_test(dict(_PY_PATCH))
        second = await sandbox.apply_and_test(dict(_PY_PATCH))

        assert first["passed"] is True
        assert second["passed"] is True
        assert not any(c[:3] == ["docker", "ps", "-q"] for c in fake_docker.calls)

    def test_a_sandbox_that_started_nothing_runs_no_docker_on_cancel(
        self,
        tmp_path: Path,
        fake_docker: FakeDocker,
        make_sandbox: Callable[..., DockerSandbox],
    ) -> None:
        sandbox = make_sandbox(_workspace(tmp_path))

        sandbox._kill_running_containers()

        assert fake_docker.calls == []


# ---------------------------------------------------------------------------
# Text I/O never depends on the host locale (#117)
# ---------------------------------------------------------------------------

_TEXT_IO = {"read_text", "write_text", "open"}


def _text_io_without_encoding(source: str) -> list[int]:
    """Lines of ``read_text``/``write_text``/text-mode ``open`` calls with no ``encoding=``."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in _TEXT_IO or any(k.arg == "encoding" for k in node.keywords):
            continue
        modes = [k.value for k in node.keywords if k.arg == "mode"] + node.args[1:2]
        if name == "open" and any(
            isinstance(m, ast.Constant) and "b" in str(m.value) for m in modes
        ):
            continue
        lines.append(node.lineno)
    return lines


@pytest.mark.parametrize(
    "module", [docker_runner, sandbox_base, toolchains, sandbox_factory], ids=lambda m: m.__name__
)
def test_text_io_names_its_encoding(module: Any) -> None:
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert _text_io_without_encoding(source) == []
