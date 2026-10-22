"""Tests for configuration loading."""

import logging
from pathlib import Path

import pytest

from delegatron import config as config_module
from delegatron.config import (
    CLOUD_TIMEOUT_SECONDS,
    LOCAL_TIMEOUT_SECONDS,
    PRESETS,
    Config,
    PipelineConfig,
    RoleConfig,
    is_local_model,
    load_config,
    normalize_security_mode,
)


def test_default_config():
    config = Config()
    assert config.architect.provider == "anthropic"
    assert config.implementer.provider == "openai"
    assert config.reviewer.provider == "google"
    assert config.pipeline.max_iterations == 5


def test_role_config_resolve_api_key_from_env(monkeypatch):
    monkeypatch.setenv("MY_KEY", "test-key-123")
    role = RoleConfig(provider="openai-compatible", model="test", api_key_env="MY_KEY")
    assert role.resolve_api_key() == "test-key-123"


def test_role_config_explicit_api_key():
    role = RoleConfig(provider="anthropic", model="test", api_key="explicit-key")
    assert role.resolve_api_key() == "explicit-key"


def test_load_config_from_toml(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    toml_content = """
[roles.architect]
provider = "google"
model = "gemini-2.5-flash"

[pipeline]
max_iterations = 10
"""
    (tmp_path / "delegatron.toml").write_text(toml_content)

    config = load_config()
    assert config.architect.provider == "google"
    assert config.architect.model == "gemini-2.5-flash"
    assert config.pipeline.max_iterations == 10


def test_load_config_with_preset(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config(preset="budget")
    assert config.architect.provider == "google"
    assert config.implementer.provider == "google"
    assert config.reviewer.provider == "google"


LOCAL_PRESETS = ("local", "local-phi4", "local-heavy", "local-oss", "local-oss-review")
ROLES = ("architect", "implementer", "reviewer")
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def test_role_context_window_defaults_to_auto():
    assert RoleConfig(provider="openai", model="gpt-4.1").context_window == 0


def test_pipeline_convergence_defaults():
    pipeline = PipelineConfig()
    assert pipeline.flaky_rerun is True
    assert pipeline.tolerate_flaky_tests is False
    assert pipeline.run_entrypoints is None  # D6: decided by the sandbox
    assert pipeline.entrypoints_enabled() is True  # docker
    assert pipeline.retry_temperature == 0.4


def test_load_config_parses_convergence_options(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    toml_content = """
[roles.implementer]
provider = "openai-compatible"
model = "qwen2.5-coder:14b"
temperature = 0.2
context_window = 16384

[pipeline]
flaky_rerun = false
tolerate_flaky_tests = true
run_entrypoints = false
retry_temperature = 0.6
"""
    (tmp_path / "delegatron.toml").write_text(toml_content)

    config = load_config()
    assert config.implementer.context_window == 16384
    assert config.implementer.temperature == 0.2
    assert config.architect.context_window == 0
    assert config.pipeline.flaky_rerun is False
    assert config.pipeline.tolerate_flaky_tests is True
    assert config.pipeline.run_entrypoints is False
    assert config.pipeline.retry_temperature == 0.6


def test_load_config_convergence_defaults_without_toml(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config(preset="mock")
    assert config.pipeline.flaky_rerun is True
    assert config.pipeline.tolerate_flaky_tests is False
    assert config.pipeline.run_entrypoints is None
    assert config.pipeline.retry_temperature == 0.4


@pytest.mark.parametrize("preset", LOCAL_PRESETS)
def test_local_presets_use_prompt_v5(preset: str):
    for role in ROLES:
        assert PRESETS[preset][role]["prompt_version"] == 5, f"{preset}.{role}"


def test_cloud_presets_keep_their_prompt_versions():
    assert [PRESETS["default"][role]["prompt_version"] for role in ROLES] == [1, 1, 1]
    assert [PRESETS["single-claude"][role]["prompt_version"] for role in ROLES] == [4, 1, 1]
    assert [PRESETS["single-gpt"][role]["prompt_version"] for role in ROLES] == [4, 1, 1]


def test_every_preset_prompt_version_has_a_prompt_file():
    for preset, roles in PRESETS.items():
        for role in ROLES:
            version = roles[role].get("prompt_version")
            if version:
                path = PROMPTS_DIR / role / f"v{version}.md"
                assert path.is_file(), f"{preset}.{role} needs {path}"


REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_toml(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "delegatron.toml"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def config_log(caplog: pytest.LogCaptureFixture):
    """Capture ``delegatron.config`` records even when package propagation is off."""
    logger = logging.getLogger("delegatron.config")
    caplog.set_level(logging.INFO, logger=logger.name)
    logger.addHandler(caplog.handler)
    yield caplog
    logger.removeHandler(caplog.handler)


# --- #111: TOML role tables merge over the preset ---------------------------


def test_partial_toml_role_keeps_the_presets_local_endpoint(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, '[roles.implementer]\nmodel = "gpt-oss:20b"\n')

    config = load_config(preset="local")

    implementer = config.implementer
    assert implementer.model == "gpt-oss:20b"
    assert implementer.provider == "openai-compatible"
    assert implementer.base_url == "http://localhost:11434/v1"
    assert implementer.prompt_version == 5
    assert implementer.max_tokens == 16384
    assert config.architect == load_config(preset="local", config_path=tmp_path / "none").architect
    assert config.reviewer.model == "phi4:14b"


def test_switching_provider_drops_the_presets_endpoint_keys(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_toml(
        tmp_path, '[roles.reviewer]\nprovider = "anthropic"\nmodel = "claude-sonnet-4-20250514"\n'
    )

    reviewer = load_config(preset="local").reviewer

    assert reviewer.provider == "anthropic"
    assert reviewer.base_url == ""
    assert reviewer.prompt_version == 0  # resolved for the new endpoint by the factory
    assert reviewer.max_tokens == 16384


def test_changing_only_the_model_keeps_a_cloud_prompt_version(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, '[roles.architect]\nmodel = "claude-opus-4-6"\n')

    architect = load_config(preset="default").architect

    assert architect.provider == "anthropic"
    assert architect.model == "claude-opus-4-6"
    assert architect.prompt_version == 1


def test_implicit_toml_overriding_an_explicit_preset_is_logged(
    tmp_path: Path, monkeypatch, config_log
):
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, '[roles.implementer]\nmodel = "gpt-oss:20b"\n')

    load_config(preset="local")

    assert "overrides the implementer role(s) of preset 'local'" in config_log.text


def test_explicit_toml_override_is_not_a_warning(tmp_path: Path, monkeypatch, config_log):
    monkeypatch.chdir(tmp_path)
    path = _write_toml(tmp_path, '[roles.implementer]\nmodel = "gpt-oss:20b"\n')

    load_config(config_path=path, preset="local")

    assert not [r for r in config_log.records if r.levelno >= logging.WARNING]


def test_non_table_role_is_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, 'roles = { implementer = "gpt-4.1" }\n')

    with pytest.raises(ValueError, match=r"\[roles.implementer\] must be a table"):
        load_config()


# --- #91: environment keys go only to the provider's own host -----------------


@pytest.fixture
def provider_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic")


@pytest.mark.parametrize(
    ("provider", "base_url", "expected"),
    [
        ("openai-compatible", "http://localhost:11434/v1", ""),
        ("openai-compatible", "", ""),
        ("openai", "", "sk-openai"),
        ("openai", "https://api.openai.com/v1", "sk-openai"),
        ("openai", "http://attacker.example:9000/v1", ""),
        ("openai", "attacker.example:9000/v1", ""),
        ("litellm", "http://localhost:4000", ""),
        ("anthropic", "", "sk-anthropic"),
        ("anthropic", "https://api.anthropic.com", "sk-anthropic"),
        ("anthropic", "http://attacker.example:9000", ""),
    ],
)
def test_default_key_is_sent_only_to_its_home_host(provider_keys, provider, base_url, expected):
    role = RoleConfig(provider=provider, model="m", base_url=base_url)
    assert role.resolve_api_key() == expected


def test_explicit_key_env_is_honoured_for_any_host(monkeypatch):
    monkeypatch.setenv("GROQ_KEY", "gsk-1")
    role = RoleConfig(
        provider="openai-compatible",
        model="m",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_KEY",
    )
    assert role.resolve_api_key() == "gsk-1"


def test_allow_env_key_false_uses_only_an_explicit_key(provider_keys, monkeypatch):
    monkeypatch.setenv("CUSTOM_KEY", "custom")
    no_env = RoleConfig(provider="openai", model="m", allow_env_key=False)
    named = RoleConfig(provider="openai", model="m", api_key_env="CUSTOM_KEY", allow_env_key=False)
    explicit = RoleConfig(provider="openai", model="m", api_key="own-key", allow_env_key=False)

    assert no_env.resolve_api_key() == ""
    assert named.resolve_api_key() == ""
    assert explicit.resolve_api_key() == "own-key"


def test_withheld_key_is_explained_without_the_value(provider_keys, config_log):
    config_module._log_withheld_key.cache_clear()
    role = RoleConfig(provider="openai", model="m", base_url="https://gpu-box.example.com/v1")

    role.resolve_api_key()

    assert "Not sending OPENAI_API_KEY to gpu-box.example.com" in config_log.text
    assert "(the role running m)" in config_log.text
    assert "sk-openai" not in config_log.text


# --- X8: request timeouts ------------------------------------------------------


def test_timeout_defaults_by_endpoint(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert RoleConfig(provider="openai", model="gpt-4.1").timeout_seconds == CLOUD_TIMEOUT_SECONDS
    assert load_config(preset="default").implementer.timeout_seconds == CLOUD_TIMEOUT_SECONDS
    assert load_config(preset="local").implementer.timeout_seconds == LOCAL_TIMEOUT_SECONDS


def test_timeout_is_read_from_toml(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, "[roles.implementer]\ntimeout_seconds = 1800\n")

    config = load_config(preset="local")

    assert config.implementer.timeout_seconds == 1800
    assert config.reviewer.timeout_seconds == LOCAL_TIMEOUT_SECONDS


@pytest.mark.parametrize("value", ["0", "-5", '"soon"'])
def test_a_non_positive_timeout_falls_back_to_the_default(tmp_path: Path, monkeypatch, value):
    # 0 would make every provider request time out at once (#106).
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, f"[roles.implementer]\ntimeout_seconds = {value}\n")

    assert load_config(preset="local").implementer.timeout_seconds == LOCAL_TIMEOUT_SECONDS


def test_new_pipeline_keys_are_read_from_toml(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_toml(
        tmp_path,
        "[pipeline]\nflaky_reruns = 9\ntolerate_preexisting_flaky = true\n"
        "max_replans = 1\nrun_git_hooks = true\n",
    )

    pipeline = load_config(preset="local").pipeline

    assert pipeline.flaky_reruns == 5  # capped like the sandbox caps it
    assert pipeline.tolerate_flaky_tests is True
    assert pipeline.max_replans == 1
    assert pipeline.run_git_hooks is True
    defaults = PipelineConfig()
    assert (defaults.flaky_reruns, defaults.max_replans, defaults.run_git_hooks) == (2, 2, False)


# --- #112: locality and the cost cap -------------------------------------------


@pytest.mark.parametrize(
    ("provider", "base_url", "expected"),
    [
        ("openai-compatible", "http://localhost:11434/v1", True),
        ("openai-compatible", "http://127.0.0.1:11434/v1", True),
        ("openai-compatible", "http://[::1]:11434/v1", True),
        ("openai-compatible", "http://host.docker.internal:11434/v1", True),
        ("openai-compatible", "http://10.0.0.5:11434/v1", True),  # LAN Ollama (D1)
        ("openai-compatible", "http://192.168.1.5:11434/v1", True),
        ("openai-compatible", "http://gpu-box.local:11434/v1", True),
        ("mock", "", True),
        ("openai-compatible", "https://api.deepseek.com/v1", False),
        ("openai-compatible", "https://api.groq.com/openai/v1", False),
        ("openai-compatible", "", False),
        ("anthropic", "http://localhost:8080", False),
        ("google", "", False),
    ],
)
def test_is_local_model_uses_the_endpoint_not_the_model_name(provider, base_url, expected):
    role = RoleConfig(provider=provider, model="gpt-oss:20b", base_url=base_url)
    assert is_local_model(role) is expected


def test_zero_cost_cap_from_toml_means_unlimited(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, "[pipeline]\nmax_cost_usd = 0\n")
    assert load_config().pipeline.max_cost_usd == 0.0


def test_zero_cost_cap_from_env_means_unlimited(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DELEGATRON_MAX_COST_USD", "0")
    assert load_config().pipeline.max_cost_usd == 0.0


def test_negative_cost_cap_clamps_to_zero(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DELEGATRON_MAX_COST_USD", "-3")
    assert load_config().pipeline.max_cost_usd == 0.0


# --- #113: security mode --------------------------------------------------------


def test_security_mode_is_case_insensitive(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, '[pipeline]\nsecurity_mode = "Block"\n')
    assert load_config().pipeline.security_mode == "block"


def test_unknown_security_mode_is_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, '[pipeline]\nsecurity_mode = "strict"\n')
    with pytest.raises(ValueError, match="security_mode must be one of allow, warn, block"):
        load_config()


@pytest.mark.parametrize("mode", ["allow", "warn", "block", " WARN "])
def test_normalize_security_mode(mode: str):
    assert normalize_security_mode(mode) == mode.strip().lower()


# --- #115: [security] table -----------------------------------------------------


def test_secrets_allowlist_is_read_from_toml(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, '[security]\nsecrets_allowlist = ["EXAMPLE_KEY_.*"]\n')
    assert load_config().security.secrets_allowlist == ["EXAMPLE_KEY_.*"]


def test_secrets_allowlist_defaults_to_empty(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_config().security.secrets_allowlist == []


def test_invalid_secrets_allowlist_is_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, '[security]\nsecrets_allowlist = "EXAMPLE"\n')
    with pytest.raises(ValueError, match="secrets_allowlist must be a list of strings"):
        load_config()


# --- #97: constraints -----------------------------------------------------------


def test_constraints_table_is_loaded(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_toml(
        tmp_path,
        '[constraints]\nno_modify = [".env"]\nforbidden_patterns = ["shell=True"]\n'
        '[pipeline]\nsecurity_mode = "block"\n',
    )

    pipeline = load_config().pipeline

    assert pipeline.constraints == {"no_modify": [".env"], "forbidden_patterns": ["shell=True"]}
    assert pipeline.security_mode == "block"


def test_pipeline_constraints_merge_and_win(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_toml(
        tmp_path,
        '[constraints]\nno_modify = [".env"]\nforbidden_patterns = ["eval("]\n'
        '[pipeline.constraints]\nforbidden_patterns = ["shell=True"]\n',
    )

    constraints = load_config().pipeline.constraints

    assert constraints == {"no_modify": [".env"], "forbidden_patterns": ["shell=True"]}


def test_example_toml_constraints_load():
    constraints = load_config(config_path=REPO_ROOT / "examples" / "delegatron.toml")
    for key in ("no_modify", "forbidden_patterns", "guidelines"):
        assert constraints.pipeline.constraints[key]
    # Prose rules are guidelines; a prose required pattern would fail every patch.
    assert "required_patterns" not in constraints.pipeline.constraints


def test_invalid_constraint_regex_fails_at_load(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, '[constraints]\nforbidden_patterns = ["re:("]\n')
    with pytest.raises(ValueError, match="Invalid regex"):
        load_config()


def test_no_toml_means_no_constraints(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_config(preset="mock").pipeline.constraints == {}


def test_non_table_constraints_are_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, 'constraints = "x"\n')
    with pytest.raises(ValueError, match=r"\[constraints\] must be a table"):
        load_config()


# --- #76: temperatures are numbers in [0, 2] -------------------------------------


@pytest.mark.parametrize(
    ("toml_value", "expected"),
    [('"abc"', 0.0), ('"0.3"', 0.3), ("3.5", 2.0), ("-1", 0.0), ("0.7", 0.7), (None, 0.0)],
)
def test_role_temperature_is_parsed_and_clamped(tmp_path: Path, monkeypatch, toml_value, expected):
    monkeypatch.chdir(tmp_path)
    line = "" if toml_value is None else f"temperature = {toml_value}\n"
    _write_toml(tmp_path, f'[roles.implementer]\nprovider = "openai"\nmodel = "m"\n{line}')
    temperature = load_config().implementer.temperature
    assert isinstance(temperature, float)
    assert temperature == expected


@pytest.mark.parametrize(
    ("toml_value", "expected"), [("7.5", 2.0), ("-0.5", 0.0), ('"hot"', 0.4), ("1.2", 1.2)]
)
def test_retry_temperature_is_clamped(tmp_path: Path, monkeypatch, toml_value, expected):
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, f"[pipeline]\nretry_temperature = {toml_value}\n")
    assert load_config().pipeline.retry_temperature == expected


def test_clamp_temperature_rejects_nan_and_non_numbers():
    assert config_module.clamp_temperature(float("nan")) == 0.0
    assert config_module.clamp_temperature([1], default=0.4) == 0.4
    assert config_module.clamp_temperature("1.5") == 1.5


# --- D2: request timeout default ----------------------------------------------------


def test_cloud_timeout_matches_the_sdk_default():
    from delegatron.agents.providers.base import DEFAULT_TIMEOUT_SECONDS

    assert CLOUD_TIMEOUT_SECONDS == 600 == DEFAULT_TIMEOUT_SECONDS
    assert RoleConfig(provider="anthropic", model="m").timeout_seconds == 600


# --- D6: entrypoint runs by sandbox ---------------------------------------------------


@pytest.mark.parametrize(
    ("setting", "sandbox", "expected"),
    [
        (None, "docker", True),
        (None, "e2b", True),
        (None, "local", False),
        (None, "LOCAL", False),
        (True, "local", True),
        (False, "docker", False),
    ],
)
def test_entrypoints_enabled_by_sandbox(setting, sandbox, expected):
    pipeline = PipelineConfig(run_entrypoints=setting, sandbox="docker")
    assert pipeline.entrypoints_enabled(sandbox) is expected


def test_entrypoints_default_follows_the_configured_sandbox():
    assert PipelineConfig(sandbox="local").entrypoints_enabled() is False
    assert PipelineConfig(sandbox="e2b").entrypoints_enabled() is True


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("false", False), ('""', None)])
def test_run_entrypoints_from_toml(tmp_path: Path, monkeypatch, value, expected):
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, f"[pipeline]\nrun_entrypoints = {value}\n")
    assert load_config().pipeline.run_entrypoints is expected


# --- #104: pre-run baseline -------------------------------------------------------------


def test_baseline_tests_default_on(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DELEGATRON_BASELINE_TESTS", raising=False)
    assert PipelineConfig().baseline_tests is True
    assert load_config(preset="mock").pipeline.baseline_tests is True


def test_baseline_tests_from_toml_and_env(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DELEGATRON_BASELINE_TESTS", "false")
    assert load_config().pipeline.baseline_tests is False
    _write_toml(tmp_path, "[pipeline]\nbaseline_tests = true\n")
    assert load_config().pipeline.baseline_tests is True


# --- #116: pipeline.local_hosts ---------------------------------------------------------


def test_local_hosts_from_toml(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DELEGATRON_LOCAL_HOSTS", "env-host.example")
    _write_toml(
        tmp_path, '[pipeline]\nlocal_hosts = ["GPU.Example.com.", "llm.corp.example", ""]\n'
    )
    assert load_config().pipeline.local_hosts == ("gpu.example.com", "llm.corp.example")


def test_local_hosts_from_env_or_comma_string(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DELEGATRON_LOCAL_HOSTS", raising=False)
    assert load_config().pipeline.local_hosts == ()
    monkeypatch.setenv("DELEGATRON_LOCAL_HOSTS", "a.example, b.example ,a.example")
    assert load_config().pipeline.local_hosts == ("a.example", "b.example")
    _write_toml(tmp_path, '[pipeline]\nlocal_hosts = "c.example"\n')
    assert load_config().pipeline.local_hosts == ("c.example",)


def test_invalid_local_hosts_is_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, "[pipeline]\nlocal_hosts = [1, 2]\n")
    with pytest.raises(ValueError, match="local_hosts"):
        load_config()


# --- D3 / #91 follow-up: a remote role without a key is reported, a local one is not ------


def test_withheld_key_warns(provider_keys, config_log):
    config_module._log_withheld_key.cache_clear()
    role = RoleConfig(provider="openai", model="m", base_url="https://api.groq.com/openai/v1")
    role.resolve_api_key()
    records = [r for r in config_log.records if "Not sending OPENAI_API_KEY" in r.getMessage()]
    assert records
    assert records[0].levelno == logging.WARNING


def test_keyless_remote_compatible_endpoint_warns(provider_keys, config_log):
    config_module._log_keyless_endpoint.cache_clear()
    role = RoleConfig(provider="openai-compatible", model="m", base_url="https://api.groq.com/v1")
    assert role.resolve_api_key() == ""
    assert "No API key for the openai-compatible endpoint api.groq.com" in config_log.text
    assert "api_key_env" in config_log.text


@pytest.mark.parametrize(
    ("provider", "base_url"),
    [
        ("openai-compatible", "http://localhost:11434/v1"),
        ("openai-compatible", "http://192.168.1.5:11434/v1"),
        ("openai", "http://localhost:11434/v1"),
        ("mock", ""),
    ],
)
def test_local_endpoints_stay_quiet(provider_keys, config_log, provider, base_url):
    config_module._log_keyless_endpoint.cache_clear()
    config_module._log_withheld_key.cache_clear()
    assert RoleConfig(provider=provider, model="m", base_url=base_url).resolve_api_key() == ""
    assert config_log.records == []


def test_operator_roles_keep_env_keys(tmp_path: Path, monkeypatch):
    """D3: roles from presets/TOML never turn allow_env_key off, custom base_url or not."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROQ_KEY", "gsk-1")
    _write_toml(
        tmp_path,
        '[roles.implementer]\nprovider = "openai-compatible"\nmodel = "m"\n'
        'base_url = "https://api.groq.com/openai/v1"\napi_key_env = "GROQ_KEY"\n',
    )
    config = load_config(preset="local")
    assert all(getattr(config, r).allow_env_key for r in ("architect", "implementer", "reviewer"))
    assert config.implementer.resolve_api_key() == "gsk-1"
