"""POST /api/estimate classifies each role by its endpoint, like CostTracker and the CLI (#112)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

import delegatron.cli as cli_module
from delegatron.config import Config, RoleConfig, is_local_model, load_config
from delegatron.server.app import app, configure
from delegatron.utils.cost import CostTracker, _is_local_model

DEEPSEEK = {
    "provider": "openai-compatible",
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com/v1",
}
ROLES = ("architect", "implementer", "reviewer")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DELEGATRON_AUTH_ENABLED", "false")
    configure(workspace=tmp_path, artifacts=tmp_path / "runs")
    return TestClient(app)


def _deepseek_config() -> Config:
    """The default preset with its implementer on DeepSeek's cloud API."""
    config = load_config(preset="default")
    return replace(config, implementer=replace(config.implementer, **DEEPSEEK))


def _api_locality(breakdown: dict[str, Any]) -> dict[str, bool]:
    return {role: breakdown[role]["is_local"] for role in ROLES}


def _config_locality(config: Config) -> dict[str, bool]:
    return {role: is_local_model(getattr(config, role)) for role in ROLES}


def _tracker_cost(role: RoleConfig, tokens: tuple[int, int]) -> float:
    return CostTracker().estimate_cost(role.model, *tokens, is_local=is_local_model(role))


def test_local_oss_roles_are_all_local_with_no_api_cost(client: TestClient) -> None:
    """gpt-oss:20b on Ollama is local: no $5/$15 fallback price on top of its GPU estimate."""
    config = load_config(preset="local-oss")

    data = client.post("/api/estimate", json={"preset": "local-oss", "max_iterations": 3}).json()

    breakdown = data["breakdown"]
    assert _api_locality(breakdown) == _config_locality(config) == dict.fromkeys(ROLES, True)
    for role in ROLES:
        assert breakdown[role]["min_cost_usd"] == breakdown[role]["max_cost_usd"] == 0
        assert _tracker_cost(getattr(config, role), (3000, 2000)) == 0
    assert breakdown["local_gpu"]["roles"] == sorted(ROLES)
    assert data["min_cost_usd"] == pytest.approx(breakdown["local_gpu"]["min_local_usd"], abs=1e-4)


def test_a_local_model_name_on_a_cloud_endpoint_is_priced_as_cloud(client: TestClient) -> None:
    config = _deepseek_config()
    body = {"preset": "default", "implementer": DEEPSEEK, "max_iterations": 2}

    data = client.post("/api/estimate", json=body).json()

    breakdown = data["breakdown"]
    assert _api_locality(breakdown) == _config_locality(config) == dict.fromkeys(ROLES, False)
    assert "local_gpu" not in breakdown
    expected_min = _tracker_cost(config.implementer, (3000, 2000)) * 2
    assert expected_min > 0
    assert breakdown["implementer"]["min_cost_usd"] == pytest.approx(expected_min, abs=1e-6)
    assert data["models"]["implementer"] == "deepseek-chat"


def test_a_cloud_model_name_on_a_local_endpoint_is_local(client: TestClient) -> None:
    override = {"provider": "openai", "model": "gpt-4.1", "base_url": "http://localhost:11434/v1"}
    body = {"preset": "default", "implementer": override}

    breakdown = client.post("/api/estimate", json=body).json()["breakdown"]

    assert breakdown["implementer"]["is_local"] is True
    assert breakdown["implementer"]["max_cost_usd"] == 0
    assert breakdown["local_gpu"]["roles"] == ["implementer"]
    assert breakdown["architect"]["is_local"] is False


@pytest.mark.parametrize("preset", ["local-oss", "deepseek"])
def test_cli_estimate_agrees_with_the_api(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, preset: str
) -> None:
    """`delegatron estimate` classifies every role as /api/estimate does (needs no LLM)."""
    if preset == "deepseek":
        config = _deepseek_config()
        body: dict[str, Any] = {"preset": "default", "implementer": DEEPSEEK}
        monkeypatch.setattr(cli_module, "load_config", lambda *_args, **_kwargs: config)
        args = ["estimate", "x"]
    else:
        config = load_config(preset=preset)
        body = {"preset": preset}
        args = ["estimate", "x", "--preset", preset]
    api = client.post("/api/estimate", json=body).json()["breakdown"]
    seen: dict[str, bool] = {}
    real_estimate = CostTracker.estimate_cost

    def _spy(
        self: CostTracker, model: str, inp: int, out: int, is_local: bool | None = None
    ) -> float:
        seen[model] = _is_local_model(model) if is_local is None else is_local
        return real_estimate(self, model, inp, out, is_local=is_local)

    monkeypatch.setattr(CostTracker, "estimate_cost", _spy)

    result = CliRunner().invoke(cli_module.main, args)

    assert result.exit_code == 0, result.output
    assert {role: seen[getattr(config, role).model] for role in ROLES} == _api_locality(api)
    assert ("Local GPU cost" in result.output) == ("local_gpu" in api)
