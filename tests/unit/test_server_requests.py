"""Server request validation: workspaces, branches, role overrides, constraints."""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import delegatron.server.app as app_module
from delegatron.config import CLOUD_TIMEOUT_SECONDS, LOCAL_TIMEOUT_SECONDS, load_config
from delegatron.constraints import merge_constraints
from delegatron.multirepo import RepoSyncError
from delegatron.server.app import app, configure
from delegatron.templates import TemplateStore

SECRET = "stream-secret-value-1234"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DELEGATRON_AUTH_ENABLED", "false")
    monkeypatch.setenv("DELEGATRON_STREAM_SECRET", SECRET)
    for name in (
        "DELEGATRON_WORKSPACE_ROOT",
        "DELEGATRON_GIT_ALLOWED_HOSTS",
        "DELEGATRON_ALLOWED_BASE_URLS",
        "DELEGATRON_ALLOWED_KEY_ENVS",
    ):
        monkeypatch.delenv(name, raising=False)
    configure(workspace=tmp_path, artifacts=tmp_path / "runs")
    return TestClient(app)


@pytest.fixture
def started_runs(monkeypatch: pytest.MonkeyPatch) -> list[app_module.RunRequest]:
    """Record runs instead of executing pipelines."""
    calls: list[app_module.RunRequest] = []

    def _fake_execute_run(run_id: str, req: app_module.RunRequest, **_kwargs: Any) -> Any:
        calls.append(req)
        return asyncio.sleep(0)

    monkeypatch.setattr(app_module, "_execute_run", _fake_execute_run)
    return calls


@pytest.fixture
def no_clone(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    """Fail the test if a workspace is ever cloned; record calls otherwise."""
    calls: list[tuple[str, str | None]] = []

    def _resolve(workspace: str, branch: str | None = None) -> tuple[Path, Any]:
        calls.append((workspace, branch))
        raise AssertionError("resolve_workspace must not run for a rejected request")

    monkeypatch.setattr(app_module, "resolve_workspace", _resolve)
    return calls


def _run_dirs(tmp_path: Path) -> list[Path]:
    runs = tmp_path / "runs"
    return list(runs.iterdir()) if runs.exists() else []


# --- Workspace and branch (git URL hardening) -------------------------------


@pytest.mark.parametrize(
    "workspace",
    [
        "--upload-pack=echo INJECTED > M.txt; exit 1; github.com/",
        "-c core.sshCommand=x github.com/",
        "ext::sh -c x github.com/",
        "http://10.0.0.5/internal.git",
        "https://internal.example/x.git",
        "/no/such/dir",
    ],
)
@pytest.mark.parametrize("endpoint", ["/api/runs", "/api/runs/plan", "/api/review"])
def test_unsafe_or_unknown_workspaces_are_rejected(
    client: TestClient,
    started_runs: list[app_module.RunRequest],
    no_clone: list[tuple[str, str | None]],
    endpoint: str,
    workspace: str,
) -> None:
    resp = client.post(endpoint, json={"request": "x", "preset": "mock", "workspace": workspace})

    assert resp.status_code == 422
    assert no_clone == []
    assert started_runs == []


def test_git_host_allow_list_is_configurable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, started_runs: list[app_module.RunRequest]
) -> None:
    url = "https://git.internal.example/team/repo.git"
    assert client.post("/api/runs", json={"request": "x", "workspace": url}).status_code == 422

    monkeypatch.setenv("DELEGATRON_GIT_ALLOWED_HOSTS", "git.internal.example")
    resp = client.post("/api/runs", json={"request": "x", "workspace": url, "branch": "develop"})

    assert resp.status_code == 200
    assert started_runs[0].branch == "develop"


def test_review_workspace_outside_the_allowed_root_is_rejected(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_clone: list[tuple[str, str | None]],
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setenv("DELEGATRON_WORKSPACE_ROOT", str(allowed))

    resp = client.post("/api/review", json={"preset": "mock", "workspace": str(outside)})

    assert resp.status_code == 422


@pytest.mark.parametrize("endpoint", ["/api/runs", "/api/review"])
def test_option_like_branch_is_rejected(
    client: TestClient,
    started_runs: list[app_module.RunRequest],
    no_clone: list[tuple[str, str | None]],
    endpoint: str,
) -> None:
    body = {"request": "x", "workspace": "https://github.com/org/repo.git", "branch": "-x"}
    assert client.post(endpoint, json=body).status_code == 422
    assert started_runs == []


def test_review_clones_the_requested_branch_off_the_event_loop(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "app.py").write_text("x = 1\n", encoding="utf-8")
    calls: list[tuple[str, str | None]] = []

    def _resolve(workspace: str, branch: str | None = None) -> tuple[Path, Any]:
        calls.append((workspace, branch))
        return clone, lambda: None

    monkeypatch.setattr(app_module, "resolve_workspace", _resolve)
    body = {"preset": "mock", "workspace": "https://github.com/org/repo.git", "branch": "dev"}

    resp = client.post("/api/review", json=body)

    assert resp.status_code == 200
    assert calls == [("https://github.com/org/repo.git", "dev")]
    assert resp.json()["project"]["path"] == str(clone)


def test_branch_is_saved_and_replayed(
    client: TestClient, tmp_path: Path, started_runs: list[app_module.RunRequest]
) -> None:
    body = {"request": "x", "workspace": "https://github.com/org/repo.git", "branch": "release/2"}
    run_id = client.post("/api/runs", json=body).json()["run_id"]
    meta = json.loads((tmp_path / "runs" / run_id / "meta.json").read_text())

    replayed = app_module._build_run_request_from_meta("x", meta)

    assert meta["branch"] == "release/2"
    assert replayed.branch == "release/2"


def test_pr_settings_are_replayed(
    client: TestClient, tmp_path: Path, started_runs: list[app_module.RunRequest]
) -> None:
    body = {"request": "x", "create_pr": True, "pr_base": "develop", "pr_draft": False}
    run_id = client.post("/api/runs", json=body).json()["run_id"]
    meta = json.loads((tmp_path / "runs" / run_id / "meta.json").read_text())

    replayed = app_module._build_run_request_from_meta("x", meta)

    assert (replayed.create_pr, replayed.pr_base, replayed.pr_draft) == (True, "develop", False)


# --- Role overrides (server keys never reach caller-chosen hosts) ------------


@pytest.mark.parametrize("endpoint", ["/api/runs", "/api/runs/plan", "/api/review"])
def test_arbitrary_api_key_env_is_rejected(
    client: TestClient,
    tmp_path: Path,
    started_runs: list[app_module.RunRequest],
    endpoint: str,
) -> None:
    body = {
        "request": "x",
        "preset": "local",
        "architect": {
            "provider": "openai-compatible",
            "base_url": "http://attacker.example:9000/v1",
            "api_key_env": "DELEGATRON_STREAM_SECRET",
        },
    }

    resp = client.post(endpoint, json=body)

    assert resp.status_code == 422
    assert "architect.api_key_env" in resp.text
    assert SECRET not in resp.text
    assert started_runs == []
    assert _run_dirs(tmp_path) == []


def test_retry_and_resume_reject_a_saved_disallowed_override(
    client: TestClient, tmp_path: Path, started_runs: list[app_module.RunRequest]
) -> None:
    run_dir = tmp_path / "runs" / "old-run"
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(json.dumps({"status": "error", "request": "x"}))
    (run_dir / "state_001.json").write_text(json.dumps({"request": "x", "iteration": 1}))
    run_workspace = tmp_path / ".delegatron-runs" / "old-run"
    (run_workspace / ".git").mkdir(parents=True)
    meta = {
        "request": "x",
        "preset": "mock",
        "reviewer": {"api_key_env": "AWS_SECRET"},
        "run_workspace": str(run_workspace),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta))

    assert client.post("/api/runs/old-run/retry").status_code == 422
    assert client.post("/api/runs/old-run/resume").status_code == 422
    assert started_runs == []


def _overridden(preset: str, role: str, **override: Any) -> Any:
    req = app_module.RunRequest(request="x", **{role: override})
    return getattr(app_module._apply_role_overrides(load_config(preset=preset), req), role)


def test_caller_chosen_base_url_gets_no_server_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-server-key")
    monkeypatch.delenv("DELEGATRON_ALLOWED_BASE_URLS", raising=False)

    for url in ("http://attacker.example:9000/v1", "http://10.attacker.example/v1"):
        role = _overridden("default", "implementer", provider="openai", base_url=url)
        assert role.allow_env_key is False
        assert role.resolve_api_key() == ""


def test_explicit_config_api_key_is_withheld_from_caller_chosen_base_url() -> None:
    config = load_config(preset="default")
    config = replace(config, architect=replace(config.architect, api_key="toml-key"))
    req = app_module.RunRequest(request="x", architect={"base_url": "https://attacker.example/v1"})

    role = app_module._apply_role_overrides(config, req).architect

    assert role.resolve_api_key() == ""


def test_allow_listed_or_configured_base_urls_keep_server_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DELEGATRON_ALLOWED_BASE_URLS", "https://gateway.example.com/v1")

    allowed = _overridden(
        "default", "implementer", base_url="https://gateway.example.com/openai/v1"
    )
    configured = _overridden("local", "implementer", base_url="http://localhost:11434/v1")
    other_port = _overridden("default", "implementer", base_url="https://gateway.example.com:8443")

    assert allowed.allow_env_key is True
    assert configured.allow_env_key is True
    assert other_port.allow_env_key is False


def test_prompt_version_is_resolved_again_for_a_new_endpoint() -> None:
    from delegatron.factories.agent_factory import resolve_prompt_version

    local = load_config(preset="local").implementer
    moved = _overridden(
        "local", "implementer", provider="anthropic", base_url="", model="claude-sonnet-4"
    )
    same_endpoint = _overridden("local", "implementer", model="qwen3-coder:30b")

    assert local.prompt_version == 5
    assert moved.prompt_version == 0
    assert resolve_prompt_version(moved) == 1  # the cloud prompt set, not local v5
    assert same_endpoint.prompt_version == 5


def test_provider_switch_to_its_own_api_keeps_server_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-server-key")

    role = _overridden("default", "architect", provider="openai", model="gpt-4.1")

    assert role.allow_env_key is True
    assert role.resolve_api_key() == "sk-server-key"


@pytest.mark.parametrize(
    ("override", "allowed_envs"),
    [
        ({"provider": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"}, ""),
        ({"api_key_env": "GROQ_API_KEY"}, "GROQ_API_KEY,OTHER"),
    ],
)
def test_default_or_allow_listed_key_envs_are_accepted(
    monkeypatch: pytest.MonkeyPatch, override: dict[str, str], allowed_envs: str
) -> None:
    monkeypatch.setenv("DELEGATRON_ALLOWED_KEY_ENVS", allowed_envs)

    role = _overridden("default", "architect", **override)

    assert role.api_key_env == override["api_key_env"]


@pytest.mark.parametrize("url", ["ftp://example.com/v1", "localhost:11434", "file:///etc"])
def test_non_http_base_url_is_rejected(client: TestClient, url: str) -> None:
    body = {"request": "x", "implementer": {"base_url": url}}

    resp = client.post("/api/runs/plan", json=body)

    assert resp.status_code == 422
    assert "implementer.base_url" in resp.text


# --- Constraints -------------------------------------------------------------


def test_invalid_regex_constraint_is_a_422(
    client: TestClient, tmp_path: Path, started_runs: list[app_module.RunRequest]
) -> None:
    body = {"request": "x", "constraints": {"forbidden_patterns": ["re:eval("]}}

    resp = client.post("/api/runs", json=body)

    assert resp.status_code == 422
    assert started_runs == []
    assert _run_dirs(tmp_path) == []


def test_guidelines_round_trip_through_meta(
    client: TestClient, tmp_path: Path, started_runs: list[app_module.RunRequest]
) -> None:
    constraints = {"forbidden_patterns": ["eval("], "guidelines": ["Validate all input"]}
    run_id = client.post("/api/runs", json={"request": "x", "constraints": constraints}).json()[
        "run_id"
    ]
    meta = json.loads((tmp_path / "runs" / run_id / "meta.json").read_text())

    replayed = app_module._build_run_request_from_meta("x", meta)

    assert replayed.constraints is not None
    assert replayed.constraints.guidelines == ["Validate all input"]
    assert replayed.constraints.forbidden_patterns == ["eval("]


def test_template_run_carries_template_constraints(
    client: TestClient, started_runs: list[app_module.RunRequest]
) -> None:
    resp = client.post("/api/templates/security-hardening/run", json={})

    assert resp.status_code == 200
    constraints = started_runs[0].constraints
    assert constraints is not None
    assert "shell=True" in constraints.forbidden_patterns


def test_template_with_invalid_constraints_is_a_422(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    started_runs: list[app_module.RunRequest],
) -> None:
    monkeypatch.setattr(app_module, "_template_store", TemplateStore(tmp_path / "templates"))
    template = {
        "name": "broken-rules",
        "request_pattern": "do it",
        "constraints": {"forbidden_patterns": ["re:("]},
    }
    assert client.post("/api/templates", json=template).status_code == 200

    resp = client.post("/api/templates/broken-rules/run", json={})

    assert resp.status_code == 422
    assert started_runs == []


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(
            128,
            ["git", "clone"],
            stderr="fatal: repository 'https://bot:s3cret@github.com/org/repo.git/' not found",
        ),
        subprocess.TimeoutExpired(["git", "clone"], 600),
        RepoSyncError("git checkout dev failed in /cache/repo: pathspec 'dev' did not match"),
    ],
)
@pytest.mark.parametrize("endpoint", ["/api/runs/plan", "/api/review"])
def test_a_failed_clone_is_a_bad_gateway_with_gits_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, endpoint: str, error: Exception
) -> None:
    def _fail(workspace: str, branch: str | None = None) -> tuple[Path, Any]:
        raise error

    monkeypatch.setattr(app_module, "resolve_workspace", _fail)
    body = {"request": "x", "preset": "mock", "workspace": "https://github.com/org/repo.git"}

    resp = client.post(endpoint, json=body)

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail.startswith("Could not fetch workspace https://github.com/org/repo.git")
    assert any(word in detail for word in ("not found", "timed out", "did not match"))
    assert "s3cret" not in detail


def test_a_branch_git_rejects_is_a_422(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _reject(workspace: str, branch: str | None = None) -> tuple[Path, Any]:
        raise ValueError(f"Invalid branch name: {branch!r}")

    monkeypatch.setattr(app_module, "resolve_workspace", _reject)
    body = {"preset": "mock", "workspace": "https://github.com/org/repo.git", "branch": "dev"}

    resp = client.post("/api/review", json=body)

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Invalid branch name: 'dev'"


# --- Role overrides: endpoint moves (prompt set, timeout, stale settings) -----


def test_a_role_moved_to_a_local_endpoint_gets_the_local_prompt_set_and_timeout() -> None:
    from delegatron.factories.agent_factory import LOCAL_PROMPT_VERSION, resolve_prompt_version

    cloud = load_config(preset="default").implementer
    moved = _overridden("default", "implementer", base_url="http://localhost:11434/v1")

    assert cloud.timeout_seconds == CLOUD_TIMEOUT_SECONDS
    assert moved.prompt_version == 0
    assert resolve_prompt_version(moved) == LOCAL_PROMPT_VERSION
    assert moved.timeout_seconds == LOCAL_TIMEOUT_SECONDS


def test_an_explicit_prompt_version_or_timeout_in_the_override_wins() -> None:
    from delegatron.factories.agent_factory import resolve_prompt_version

    moved = _overridden(
        "default",
        "implementer",
        base_url="http://localhost:11434/v1",
        prompt_version=1,
        timeout_seconds=120,
    )
    same_endpoint = _overridden("local", "implementer", prompt_version=3)

    assert resolve_prompt_version(moved) == 1
    assert moved.timeout_seconds == 120
    assert same_endpoint.prompt_version == 3
    assert same_endpoint.base_url == "http://localhost:11434/v1"


def test_an_operator_configured_timeout_moves_with_the_role() -> None:
    config = load_config(preset="default")
    config = replace(config, implementer=replace(config.implementer, timeout_seconds=1234))
    req = app_module.RunRequest(request="x", implementer={"base_url": "http://localhost:11434/v1"})

    assert app_module._apply_role_overrides(config, req).implementer.timeout_seconds == 1234


def test_a_provider_switch_drops_the_old_endpoint() -> None:
    """local preset + {provider: anthropic}: the Anthropic SDK must not be sent to Ollama."""
    from delegatron.factories.agent_factory import CLOUD_PROMPT_VERSION, resolve_prompt_version

    role = _overridden("local", "implementer", provider="anthropic", model="claude-sonnet-4")

    assert role.base_url == ""
    assert role.api_key_env == ""
    assert resolve_prompt_version(role) == CLOUD_PROMPT_VERSION
    assert role.timeout_seconds == CLOUD_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "override", [{"prompt_version": -1}, {"timeout_seconds": 0}, {"prompt_version": "v5"}]
)
def test_invalid_prompt_version_or_timeout_is_a_422(
    client: TestClient, started_runs: list[app_module.RunRequest], override: dict[str, Any]
) -> None:
    resp = client.post("/api/runs", json={"request": "x", "implementer": override})

    assert resp.status_code == 422
    assert started_runs == []


def test_override_prompt_version_reaches_the_run_and_is_replayed(
    client: TestClient, tmp_path: Path, started_runs: list[app_module.RunRequest]
) -> None:
    body = {
        "request": "x",
        "preset": "default",
        "implementer": {"base_url": "http://localhost:11434/v1", "prompt_version": 1},
    }
    run_id = client.post("/api/runs", json=body).json()["run_id"]
    meta = json.loads((tmp_path / "runs" / run_id / "meta.json").read_text())

    replayed = app_module._build_run_request_from_meta("x", meta)

    assert replayed.implementer is not None
    assert replayed.implementer.prompt_version == 1
    assert app_module._request_config(replayed).implementer.prompt_version == 1


# --- Template constraints in the {name: pattern} table form (#97) ----------------


def test_template_with_table_form_constraints_runs(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    started_runs: list[app_module.RunRequest],
) -> None:
    monkeypatch.setattr(app_module, "_template_store", TemplateStore(tmp_path / "templates"))
    template = {
        "name": "table-rules",
        "request_pattern": "do it",
        "constraints": {
            "forbidden_patterns": {"no_eval": "eval("},
            "required_patterns": {"typed": r"re:def \w+\(.*\) ->"},
        },
    }
    assert client.post("/api/templates", json=template).status_code == 200

    resp = client.post("/api/templates/table-rules/run", json={})

    assert resp.status_code == 200
    constraints = started_runs[0].constraints
    assert constraints is not None
    assert constraints.forbidden_patterns == {"no_eval": "eval("}
    merged = merge_constraints({}, constraints.model_dump(exclude_defaults=True))
    assert merged["forbidden_patterns"] == {"no_eval": "eval("}
    meta = json.loads((tmp_path / "runs" / resp.json()["run_id"] / "meta.json").read_text())
    replayed = app_module._build_run_request_from_meta("x", meta)
    assert replayed.constraints == constraints


def test_table_form_constraint_with_an_invalid_regex_is_a_422(
    client: TestClient, started_runs: list[app_module.RunRequest]
) -> None:
    body = {"request": "x", "constraints": {"forbidden_patterns": {"bad": "re:("}}}

    assert client.post("/api/runs", json=body).status_code == 422
    assert started_runs == []
