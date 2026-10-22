"""Tests for sandbox runner."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from delegatron.sandbox.docker_runner import DockerSandbox, _cache_volume_mounts

# Where the removed move-to-cache/symlink-back scheme kept dep dirs.
_OLD_CACHE_ROOT = "/opt/delegatron-cache"


def test_parse_test_output_passing():
    sandbox = DockerSandbox(workspace_root=Path("."))
    result = sandbox._parse_test_output("5 passed in 1.23s")
    assert result["total"] == 5
    assert result["failures"] == 0


def test_parse_test_output_mixed():
    sandbox = DockerSandbox(workspace_root=Path("."))
    result = sandbox._parse_test_output("3 passed, 2 failed in 2.5s")
    assert result["total"] == 5
    assert result["failures"] == 2


def test_parse_test_output_empty():
    sandbox = DockerSandbox(workspace_root=Path("."))
    result = sandbox._parse_test_output("no tests collected")
    assert result["total"] == 0
    assert result["failures"] == 0


# ---------------------------------------------------------------------------
# Install-artifact caching across iterations
# ---------------------------------------------------------------------------
#
# Regression coverage for a bug where iteration 2 of an npm project would fail
# with "vitest: not found" because node_modules lives in the bind-mounted
# workspace (not captured by ``docker commit``).  The fix mounts a named
# volume over each workspace-local dep dir, shared by the install and test
# containers.


class TestCachedWorkspaceDirs:
    @staticmethod
    def _mounted(lang: str | None, is_windows: bool = False) -> list[str]:
        args, _ = _cache_volume_mounts(lang, "k", "/workspace", is_windows)
        return [arg.split(":", 1)[1] for arg in args if arg != "-v"]

    def test_javascript_has_node_modules(self):
        assert self._mounted("javascript") == ["/workspace/node_modules"]

    def test_typescript_has_node_modules(self):
        assert self._mounted("typescript") == ["/workspace/node_modules"]

    def test_php_has_vendor(self):
        assert self._mounted("php") == ["/workspace/vendor"]

    def test_python_has_no_cached_dirs(self):
        # Python deps live in site-packages (outside /workspace) — no rescue needed.
        assert self._mounted("python") == []

    def test_none_language_has_no_cached_dirs(self):
        assert self._mounted(None) == []

    def test_unknown_language_has_no_cached_dirs(self):
        assert self._mounted("cobol") == []

    def test_windows_has_no_cached_dirs(self):
        assert self._mounted("javascript", is_windows=True) == []


# ---------------------------------------------------------------------------
# Iteration-2 regression: node_modules must survive across iterations
# ---------------------------------------------------------------------------


def _stub_run_factory(captured: list[list[str]]):
    """Make a ``subprocess.run`` stub that records invocations and fakes success."""

    class _Result:
        def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    def _run(*args: Any, **kwargs: Any) -> _Result:
        cmd = args[0] if args else kwargs.get("args", [])
        captured.append(list(cmd))

        # Route behavior by the docker sub-command
        if not cmd or cmd[0] != "docker":
            return _Result(returncode=0)

        sub = cmd[1] if len(cmd) > 1 else ""

        # Report a known Docker daemon state so the flow proceeds
        if sub == "version":
            return _Result(stdout="linux\n")
        if sub == "info":
            return _Result(returncode=0)
        if sub == "image" and len(cmd) > 2 and cmd[2] == "inspect":
            # Pretend image exists → skip pull
            if "Size" in " ".join(cmd):
                return _Result(stdout="100000000\n")
            return _Result(returncode=0)
        if sub == "ps":
            # Locate last container for commit
            return _Result(stdout="abc123\n")
        if sub == "commit":
            return _Result(returncode=0)
        if sub == "rm":
            return _Result(returncode=0)
        if sub == "run":
            # Fake successful install/test output
            return _Result(stdout="1 passed in 0.1s\n", returncode=0)
        return _Result(returncode=0)

    return _run


def test_install_phase_mounts_node_modules_volume(tmp_path: Path) -> None:
    """Iter 1 (JS project): install container must mount a named volume at
    ``/workspace/node_modules``. The postamble is NOT used — volumes handle
    persistence."""
    (tmp_path / "package.json").write_text('{"name":"t","scripts":{"test":"vitest run"}}')

    sandbox = DockerSandbox(
        workspace_root=tmp_path,
        image="node:22-slim",
        test_commands=["npm test"],
        install_commands=["npm install"],
    )

    captured: list[list[str]] = []
    with patch.object(subprocess, "run", side_effect=_stub_run_factory(captured)):
        sandbox._apply_and_test_sync({"files": {"foo.test.js": "test('x', () => {});\n"}})

    install_runs = [
        c
        for c in captured
        if len(c) >= 2
        and c[0] == "docker"
        and c[1] == "run"
        and "--rm" not in c
        and any("npm install" in arg for arg in c)
    ]
    assert install_runs, "expected a docker-run invocation for the install phase"
    install_cmd = install_runs[0]
    install_script = install_cmd[-1]

    # The named volume is mounted at /workspace/node_modules.
    node_modules_mounts = [arg for arg in install_cmd if arg.endswith(":/workspace/node_modules")]
    assert node_modules_mounts, (
        "install container must mount a named volume at /workspace/node_modules"
    )
    vol_name = node_modules_mounts[0].split(":", 1)[0]
    assert vol_name.startswith("delegatron-deps-")
    # Sandbox tracks the volume for cleanup.
    assert vol_name in sandbox._cached_volumes

    # Install script is just the install command — no move-to-cache postamble.
    assert install_script.strip() == "npm install"
    # Guard: the old postamble string must not leak in.
    assert f"mv /workspace/node_modules {_OLD_CACHE_ROOT}/node_modules" not in install_script
    with patch.object(subprocess, "run", side_effect=_stub_run_factory(captured)):
        sandbox.cleanup()


def test_test_phase_reuses_node_modules_volume_on_cache_hit(tmp_path: Path) -> None:
    """Iter 2 (JS project, cache hit): install is skipped; the test container
    mounts the SAME named volume so node_modules is already there."""
    (tmp_path / "package.json").write_text('{"name":"t","scripts":{"test":"vitest run"}}')

    sandbox = DockerSandbox(
        workspace_root=tmp_path,
        image="node:22-slim",
        test_commands=["npm test"],
        install_commands=["npm install"],
    )
    from delegatron.sandbox.docker_runner import _compute_cache_key

    # The staged copy has the same manifests as the workspace.
    cache_key = _compute_cache_key("node:22-slim", ["npm install"], tmp_path)
    cached_tag = sandbox._cache_tag(cache_key)
    sandbox._cached_images.add(cached_tag)

    captured: list[list[str]] = []
    with patch.object(subprocess, "run", side_effect=_stub_run_factory(captured)):
        sandbox._apply_and_test_sync({"files": {"foo.test.js": "test('x', () => {});\n"}})

    # Install phase is skipped on cache hit.
    install_runs = [
        c
        for c in captured
        if len(c) >= 2
        and c[0] == "docker"
        and c[1] == "run"
        and "--rm" not in c
        and any("npm install" in arg for arg in c)
    ]
    assert not install_runs

    # Test container mounts the cached named volume.
    test_runs = [
        c
        for c in captured
        if len(c) >= 2
        and c[0] == "docker"
        and c[1] == "run"
        and "--rm" in c
        and any("npm test" in arg for arg in c)
    ]
    assert test_runs
    test_cmd = test_runs[0]
    node_modules_mounts = [arg for arg in test_cmd if arg.endswith(":/workspace/node_modules")]
    assert node_modules_mounts, "test container must mount node_modules volume"
    # Script does NOT contain the old symlink-back preamble.
    test_script = test_cmd[-1]
    assert f"ln -s {_OLD_CACHE_ROOT}/node_modules /workspace/node_modules" not in test_script
    assert "npm test" in test_script
    # Uses the cached image for the test phase.
    assert cached_tag in test_cmd
    with patch.object(subprocess, "run", side_effect=_stub_run_factory(captured)):
        sandbox.cleanup()
