"""Shared test fixtures for Delegatron.

Safety net for plain ``pytest``: an integration test never reaches Ollama
(``ollama stop`` from the GPU cleanup), a real sandbox (Docker, or a local
venv that installs packages from the network), or the network.  For every
test under ``tests/integration``:

- the GPU cleanup calls are no-ops and sandbox creation returns
  :class:`StubSandbox`, whose every test run passes;
- starting a ``docker`` or ``ollama`` process raises ``FileNotFoundError``
  (as if the CLI were not installed);
- a socket may connect only to a loopback address, and not to Ollama's port.

A test marked ``slow`` or ``live`` (both deselected by ``addopts``) keeps the
real calls.  The paid-API tests (``_LIVE_MODULES``) are marked ``live``.

Unit tests are not patched: those that test ``gpu_cleanup`` or the sandboxes
exercise the real functions, and the rest patch what they need themselves.
"""

from __future__ import annotations

import os
import tempfile

# The server opens its SQLite database under DELEGATRON_DATA_DIR when
# ``delegatron.server.app`` is imported; keep that out of the repository.
os.environ.setdefault("DELEGATRON_DATA_DIR", tempfile.mkdtemp(prefix="delegatron-test-data-"))

import ipaddress
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

_INTEGRATION_DIR = Path(__file__).parent / "integration"

# Live tests against paid provider APIs (run with ``-m live``).
_LIVE_MODULES = frozenset({"test_providers_live.py"})

# Programs an integration test may not start, and Ollama's port.
_BLOCKED_PROGRAMS = frozenset({"docker", "ollama"})
_OLLAMA_PORT = 11434


class StubSandbox:
    """A deterministic sandbox: every test run passes; records the patches it ran."""

    def __init__(self, runs: list[dict[str, Any]]) -> None:
        self.runs = runs

    async def apply_and_test(self, patch: dict[str, Any]) -> dict[str, Any]:
        self.runs.append(patch)
        return {
            "passed": True,
            "total": 1,
            "failures": 0,
            "stdout": "tests/test_health.py::test_ok PASSED\n1 passed in 0.01s",
            "stderr": "",
            "failed_tests": [],
            "results": [{"name": "tests/test_health.py::test_ok", "passed": True}],
        }

    def cleanup(self) -> None:
        pass


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark the paid-API tests ``live`` so plain ``pytest`` deselects them."""
    for item in items:
        if item.path.name in _LIVE_MODULES:
            item.add_marker(pytest.mark.live)


def _program_name(args: Any) -> str:
    first = args if isinstance(args, str | bytes) else next(iter(args), "")
    if isinstance(first, bytes):
        first = first.decode(errors="replace")
    words = str(first).split()
    name = Path(words[0]).name.lower() if words else ""
    return name.removesuffix(".exe")


class _GuardedPopen(subprocess.Popen):  # type: ignore[type-arg]
    """``subprocess.Popen`` that refuses to start ``docker`` or ``ollama``."""

    def __init__(self, args: Any, *rest: Any, **kwargs: Any) -> None:
        if _program_name(args) in _BLOCKED_PROGRAMS:
            raise FileNotFoundError(f"blocked in integration tests: {_program_name(args)}")
        super().__init__(args, *rest, **kwargs)


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _check_address(address: Any) -> None:
    """Refuse a connection to a non-loopback address or to Ollama's port."""
    if not isinstance(address, tuple):  # AF_UNIX and the like
        return
    host, port = str(address[0]), int(address[1])
    try:
        loopback = ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback or port == _OLLAMA_PORT:
        raise ConnectionRefusedError(f"blocked in integration tests: {host}:{port}")


def _guarded_connect(self: socket.socket, address: Any) -> None:
    _check_address(address)
    _real_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    _check_address(address)
    return _real_connect_ex(self, address)


def _is_integration(request: pytest.FixtureRequest) -> bool:
    return _INTEGRATION_DIR in request.node.path.parents


@pytest.fixture()
def sandbox_runs() -> list[dict[str, Any]]:
    """The patches the stub sandbox ran in this test (integration tests only)."""
    return []


@pytest.fixture(autouse=True)
def _no_ollama_or_docker(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Integration tests: no Ollama, Docker or network, unless marked ``slow``/``live``."""
    if not _is_integration(request):
        return
    if request.node.get_closest_marker("slow") or request.node.get_closest_marker("live"):
        return

    monkeypatch.setattr(subprocess, "Popen", _GuardedPopen)
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)

    from delegatron.factories import sandbox_factory
    from delegatron.orchestrator import async_runner, graph
    from delegatron.utils import gpu_cleanup

    def _no_cleanup(*args: Any, **kwargs: Any) -> None:
        return None

    # graph imports the names; the server and async_runner import them lazily
    # from the module, so both places are patched.
    for module in (graph, gpu_cleanup):
        monkeypatch.setattr(module, "cleanup_after_run", _no_cleanup)
        monkeypatch.setattr(module, "unload_except", _no_cleanup)
    monkeypatch.setattr(gpu_cleanup, "unload_model", lambda model: False)
    monkeypatch.setattr(gpu_cleanup, "unload_ollama_models", lambda config: [])

    runs: list[dict[str, Any]] = request.getfixturevalue("sandbox_runs")

    def _stub_sandbox(*args: Any, **kwargs: Any) -> StubSandbox:
        return StubSandbox(runs)

    for module in (graph, async_runner, sandbox_factory):
        monkeypatch.setattr(module, "create_sandbox", _stub_sandbox)
