"""The integration safety net of tests/conftest.py is in place for every test here."""

from __future__ import annotations

import socket
import subprocess

import pytest

from delegatron.factories import sandbox_factory
from delegatron.orchestrator import async_runner, graph
from delegatron.utils import gpu_cleanup


def _guarded() -> bool:
    return subprocess.Popen.__name__ == "_GuardedPopen"


@pytest.mark.parametrize("program", ["ollama", "docker", "docker.exe", "/usr/bin/ollama"])
def test_docker_and_ollama_cannot_start(program: str) -> None:
    assert _guarded(), "subprocess.Popen is not guarded"  # never run the real CLI
    with pytest.raises(FileNotFoundError, match="blocked"):
        subprocess.run([program, "ps"], capture_output=True, check=False)


def test_other_programs_still_run() -> None:
    result = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True)
    assert "git" in result.stdout


@pytest.mark.parametrize(
    "address", [("8.8.8.8", 443), ("127.0.0.1", 11434), ("::1", 11434), ("api.openai.com", 443)]
)
def test_network_and_ollama_port_are_refused(address: tuple[str, int]) -> None:
    family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock, pytest.raises(ConnectionRefusedError):
        sock.connect(address)


def test_gpu_cleanup_and_sandbox_are_stubbed(sandbox_runs: list[dict]) -> None:
    assert graph.cleanup_after_run(None) is None  # type: ignore[arg-type]
    assert graph.unload_except(None, "m") is None  # type: ignore[arg-type]
    assert gpu_cleanup.cleanup_after_run.__name__ == "_no_cleanup"
    for module in (graph, async_runner, sandbox_factory):
        sandbox = module.create_sandbox(sandbox_type="docker")
        assert type(sandbox).__name__ == "StubSandbox"
    assert sandbox_runs == []
