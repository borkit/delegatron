"""The documentation describes what the code does.

Each test ties a documented claim to the code behind it, so a behaviour change
that leaves the docs behind (or docs that promise more than the code does)
fails here.  No network, LLM or Docker.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import json
import re
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest

from delegatron.config import (
    _PROVIDER_HOME_HOSTS,
    DEFAULT_API_KEY_ENV,
    PRESETS,
    Config,
    PipelineConfig,
    RoleConfig,
    is_local_model,
    load_config,
)
from delegatron.constraints import Constraints, _matches_protected, validate_patch
from delegatron.factories.agent_factory import LOCAL_PROMPT_VERSION, resolve_prompt_version
from delegatron.factories.security_pipeline import _finding
from delegatron.orchestrator.graph import Pipeline
from delegatron.security import import_validator
from delegatron.utils.paths import INTERNAL_DIRS

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "delegatron"

_ROLES = ("architect", "implementer", "reviewer")


def _read(rel: str) -> str:
    """A repository text file with ``\\n`` line endings."""
    return (REPO_ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def _section(text: str, heading: str) -> str:
    """The body under the markdown *heading* line, up to the next heading of its level or higher.

    Lines inside fenced code blocks never end a section (``# comment`` in a
    TOML example is not a heading).
    """
    lines = text.split("\n")
    start = lines.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    body: list[str] = []
    in_fence = False
    for line in lines[start + 1 :]:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and re.match(rf"#{{1,{level}}} ", line):
            break
        body.append(line)
    return "\n".join(body)


def _code_blocks(text: str, language: str) -> list[str]:
    """Contents of the fenced code blocks tagged *language* in *text*."""
    return re.findall(rf"```{language}\n(.*?)\n```", text, flags=re.DOTALL)


def _call_keywords(tree: ast.AST, name: str) -> set[str | None]:
    """Keyword argument names of every call to the plain name *name* in *tree*."""
    return {
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
        for keyword in node.keywords
    }


def _local_presets() -> list[str]:
    """Presets whose three roles all run on a local endpoint (the mock preset excluded)."""
    names = []
    for name, roles in PRESETS.items():
        configs = [
            RoleConfig(
                provider=str(roles[r]["provider"]),
                model=str(roles[r]["model"]),
                base_url=str(roles[r].get("base_url", "")),
            )
            for r in _ROLES
        ]
        if name != "mock" and all(is_local_model(c) for c in configs):
            names.append(name)
    return names


# ── #71: v5 prompts are the local default ─────────────────────────────────


def test_adr_prompt_section_names_the_prompt_every_local_preset_pins() -> None:
    local = _local_presets()
    assert local, "expected local presets"
    adr002 = _section(
        _read("docs/architecture/002-system-architecture.md"),
        "### Prompt Versions for Local Models",
    )
    adr001 = _section(
        _read("docs/architecture/001-pipeline-overview.md"), "### Prompts for Local Models"
    )

    assert f"prompt_version = {LOCAL_PROMPT_VERSION}" in adr002
    assert "prompt_version: 4" not in adr002
    for name in local:
        assert f"`{name}`" in adr002
        assert f"`{name}`" in adr001
        for role in _ROLES:
            assert PRESETS[name][role]["prompt_version"] == LOCAL_PROMPT_VERSION, (name, role)
    assert f"**v{LOCAL_PROMPT_VERSION}** (used by" in adr001


def test_changelog_notes_the_new_local_default_and_unset_prompt_version() -> None:
    changelog = _read("CHANGELOG.md")
    public_release = changelog.split("## [Public release]", 1)[1].split("\n## [", 1)[0]

    assert f"pins `prompt_version = {LOCAL_PROMPT_VERSION}`" in public_release
    assert "every role of the `mock` preset" in public_release
    assert "v5 on a local endpoint or the mock provider and v1 on a cloud API" in public_release
    # ...which is what the resolver does for a role that leaves it unset.
    assert resolve_prompt_version(RoleConfig(provider="mock", model="m")) == 5
    assert resolve_prompt_version(RoleConfig(provider="anthropic", model="m")) == 1
    local_unset = RoleConfig(
        provider="openai-compatible", model="m", base_url="http://localhost:11434/v1"
    )
    assert resolve_prompt_version(local_unset) == 5


# ── #74: the director rules; arbitration is no longer only logged ─────────


def test_architecture_docs_describe_the_director_ruling() -> None:
    adr002 = _read("docs/architecture/002-system-architecture.md")
    adr001 = _read("docs/architecture/001-pipeline-overview.md")
    flow = _code_blocks(_section(adr001, "### Main Pipeline Flow"), "")[0]

    assert "not fully automated" not in adr002
    assert "director call is stubbed" not in adr002
    assert "director" in flow
    assert "directed attempt" in flow
    assert "detects recurring" not in flow


def test_pipeline_flow_diagram_borders_line_up() -> None:
    adr001 = _read("docs/architecture/001-pipeline-overview.md")
    flow = _code_blocks(_section(adr001, "### Main Pipeline Flow"), "")[0]
    widths = {len(line) for line in flow.split("\n") if line.endswith("│    │")}
    assert len(widths) == 1, widths


def test_arbitration_docs_match_the_failure_signature_masking() -> None:
    from delegatron.utils.test_output import failure_signature

    adr002 = _section(
        _read("docs/architecture/002-system-architecture.md"),
        "### Arbitration (`orchestrator/arbitration.py`)",
    )
    assert "`AssertionError: 12 != 10` followed by `AssertionError: 14 != 12`" in adr002

    def report(left: int, right: int) -> dict[str, object]:
        stdout = (
            "FAILED tests/test_deal.py::test_deal - "
            f"AssertionError: {left} != {right}\n"
            f"E       AssertionError: {left} != {right}\n"
        )
        return {"passed": False, "stdout": stdout, "stderr": ""}

    assert failure_signature(report(12, 10)) == failure_signature(report(14, 12))


# ── #75: context window trimming ──────────────────────────────────────────


def test_install_guide_documents_the_context_trimmed_event_and_num_ctx_limit() -> None:
    section = _section(
        _read("docs/install.md"), "#### Context window (`OLLAMA_CONTEXT_LENGTH` / `num_ctx`)"
    )
    graph_source = (SRC / "orchestrator" / "graph.py").read_text(encoding="utf-8")
    provider_source = (SRC / "agents" / "providers" / "openai_provider.py").read_text(
        encoding="utf-8"
    )

    assert '"event": "context_trimmed"' in graph_source
    assert '"event": "context_trimmed"' in section
    assert "`events.jsonl`" in section
    assert "no per-request `num_ctx` option" in section
    assert "/api/version" in provider_source
    assert "`GET /api/version`" in section
    for event_json in _code_blocks(section, "json"):
        assert json.loads(event_json)["event"] == "context_trimmed"


# ── #76: role temperature ─────────────────────────────────────────────────


def test_role_temperature_is_documented_as_honored_with_its_default() -> None:
    assert RoleConfig(provider="openai", model="m").temperature == 0.0
    assert "`temperature` (default `0.0`) is honored" in _read("docs/user-guide.md")
    assert "`temperature` (0.0 to 2.0, default `0.0`) is honored" in _read("docs/api-reference.md")


# ── #81: server resume ────────────────────────────────────────────────────


def test_resume_endpoint_docs_describe_checkpoint_resume() -> None:
    section = _section(_read("docs/api-reference.md"), "#### `POST /api/runs/{run_id}/resume`")

    assert "starts a new pipeline run" not in section
    assert "keeps its `run_id`" in section
    for status in ("error", "cancelled", "stalled", "exhausted"):
        assert f'`"{status}"`' in section
    for code in ("`404`", "`400`", "`409`", "`422`"):
        assert code in section
    assert "no checkpoint" in section
    assert "run workspace no longer exists" in section
    assert "still active" in section
    body, response = (json.loads(block) for block in _code_blocks(section, "json"))
    assert set(body) == {"max_iterations", "max_wall_time_minutes", "max_cost_usd"}
    assert set(response) == {"run_id", "status", "message"}


def test_resume_body_fields_exist_on_the_server() -> None:
    from delegatron.server.app import ResumeRequest

    section = _section(_read("docs/api-reference.md"), "#### `POST /api/runs/{run_id}/resume`")
    body = json.loads(_code_blocks(section, "json")[0])
    assert set(body) == set(ResumeRequest.model_fields)
    ResumeRequest(**body)  # the example validates


# ── #83: graph.Pipeline is the SDK; AGENTS.md mirrors CLAUDE.md ──────────


def test_python_sdk_example_uses_graph_pipeline_with_real_names() -> None:
    section = _section(_read("docs/api-reference.md"), "## Python SDK")
    (code,) = _code_blocks(section, "python")
    tree = ast.parse(code)

    imported: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module is not None
            assert "async_runner" not in node.module
            module = importlib.import_module(node.module)
            for alias in node.names:
                assert hasattr(module, alias.name), f"{node.module}.{alias.name}"
                imported[alias.name] = getattr(module, alias.name)
    assert imported["Pipeline"] is Pipeline
    assert imported["load_config"] is load_config

    pipeline_fields = {f.name for f in dataclasses.fields(Pipeline) if f.init}
    load_config_params = set(inspect.signature(load_config).parameters)
    pipeline_kwargs = _call_keywords(tree, "Pipeline")
    assert "step_callback" in pipeline_kwargs
    assert pipeline_kwargs <= pipeline_fields
    assert _call_keywords(tree, "load_config") <= load_config_params


def test_agents_md_mirrors_claude_md() -> None:
    claude = _read("CLAUDE.md").split("\n", 1)
    agents = _read("AGENTS.md").split("\n", 1)
    assert claude[0] == "# CLAUDE.md — Delegatron Development Guide"
    assert agents[0] == "# AGENTS.md — Delegatron Development Guide"
    assert agents[1] == claude[1], "AGENTS.md must match CLAUDE.md below the title"
    assert "Async/event-driven execution with SSE support" not in agents[1]


# ── #85: import validator sources ─────────────────────────────────────────


def test_capabilities_lists_the_import_validator_sources() -> None:
    capabilities = _read("docs/capabilities.md")
    bullet = capabilities.split("- **Import validator:**", 1)[1].split("\n---", 1)[0]

    for config in import_validator._JS_ALIAS_CONFIGS:
        assert f"`{config}`" in bullet
    for section in import_validator._CARGO_DEP_SECTIONS:
        assert f"`[{section}]`" in bullet
    for token in (
        "`node:`",
        "`@/`",
        "`~/`",
        "`[dependency-groups]`",
        "`group.*`",
        "`setup.cfg`",
        "`requirements*.txt`",
        "`requirements/*.txt`",
        "`-r` includes",
        "`[target.<cfg>.*]`",
        "`go.mod`",
        "`pom.xml`",
    ):
        assert token in bullet, token
    assert "`review_feedback`" not in bullet


# ── #91: API keys go only where they belong ───────────────────────────────


def test_security_guide_tabulates_each_default_key_and_its_home_host() -> None:
    section = _section(_read("docs/security.md"), "## Provider API Keys")

    for provider, env_var in DEFAULT_API_KEY_ENV.items():
        row = f"| `{provider}` | `{env_var}` | `{_PROVIDER_HOME_HOSTS[provider]}` |"
        assert row in section
    assert "openai-compatible" not in DEFAULT_API_KEY_ENV
    assert "| `openai-compatible` | none |" in section


def test_default_keys_behave_as_documented(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")

    ollama = RoleConfig(
        provider="openai-compatible", model="m", base_url="http://localhost:11434/v1"
    )
    proxy = RoleConfig(provider="openai", model="m", base_url="https://proxy.example/v1")
    home = RoleConfig(provider="openai", model="m", base_url="https://api.openai.com/v1")
    groq = RoleConfig(
        provider="openai-compatible",
        model="m",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
    )
    assert ollama.resolve_api_key() == ""
    assert proxy.resolve_api_key() == ""
    assert home.resolve_api_key() == "sk-test-openai"
    assert groq.resolve_api_key() == "gsk-test"


def test_server_allow_list_variables_are_documented() -> None:
    app_source = (SRC / "server" / "app.py").read_text(encoding="utf-8")
    allow_lists = set(re.findall(r'"(DELEGATRON_ALLOWED_[A-Z_]+)"', app_source))
    assert allow_lists == {"DELEGATRON_ALLOWED_BASE_URLS", "DELEGATRON_ALLOWED_KEY_ENVS"}

    guide = _read("docs/user-guide.md")
    env_table = _section(guide, "### Environment Variables")
    overrides = _section(_read("docs/security.md"), "### Server role overrides")
    for name in allow_lists:
        assert f"| `{name}` |" in env_table, name
        assert f"`{name}`" in overrides, name
        assert f"`{name}`" in _read("CLAUDE.md"), name
    assert "has no default key" in guide
    assert "set `api_key_env`" in guide


# ── #113: security findings, block mode and summary.json ──────────────────


def test_security_guide_documents_block_mode_and_the_summary_section(tmp_path: Path) -> None:
    security = _read("docs/security.md")
    modes = _section(security, "### Security Modes")
    summary = _section(security, "### Security in `summary.json`")
    graph_source = (SRC / "orchestrator" / "graph.py").read_text(encoding="utf-8")

    assert "not written to the workspace or committed" in modes
    assert "never ends the run" in modes
    assert '"unresolved_findings"' in graph_source
    (example,) = _code_blocks(summary, "json")
    section = json.loads("{" + example + "}")["security"]
    pipeline = Pipeline(
        config=Config(pipeline=PipelineConfig(security_mode="allow")),
        workspace_root=tmp_path,
        run_id="docs",
    )
    assert set(section) == set(pipeline._security_summary(cast(Any, {"messages": []})))
    finding = section["unresolved_findings"][0]
    assert set(finding) == set(_finding("major", "t", "d", "f", 1, "secrets"))
    assert "until fixed" not in _read("docs/user-guide.md")


# ── #114: no_modify and required-pattern matching ─────────────────────────


def test_constraint_matching_details_are_documented() -> None:
    constraints_doc = _section(_read("docs/security.md"), "## Project-Level Constraints")

    assert "`docs/*.md` protects `docs/a/b.md` too" in constraints_doc
    assert _matches_protected("docs/a/b.md", "docs/*.md")

    assert "`re:(?m)^import os`" in constraints_doc
    patch = {"files": {"app.py": "x = 1\nimport os\n"}}
    anchored = Constraints(required_patterns=["re:^import os"])
    multiline = Constraints(required_patterns=["re:(?m)^import os"])
    assert [v.rule for v in validate_patch(patch, anchored)] == ["required_pattern"]
    assert validate_patch(patch, multiline) == []


# ── #121: internal dirs are excluded through .git/info/exclude ────────────


def test_capabilities_describes_info_exclude_for_every_internal_dir() -> None:
    capabilities = _read("docs/capabilities.md")
    bullet = capabilities.split("- **Internal directories kept out of git:**", 1)[1].split(
        "\n- **", 1
    )[0]
    workspace_source = (SRC / "workspace.py").read_text(encoding="utf-8")

    assert "Auto-gitignore" not in capabilities
    assert '"info/exclude"' in workspace_source
    assert "`.git/info/exclude`" in bullet
    for name in INTERNAL_DIRS:
        assert f"`{name}/`" in bullet, name


# ── Public release: removed features stay out of the docs; one license ─────


def _current_docs() -> list[str]:
    """README, CLAUDE.md, AGENTS.md and docs/ (the CHANGELOG records history and is excluded)."""
    docs = sorted(p.relative_to(REPO_ROOT).as_posix() for p in (REPO_ROOT / "docs").rglob("*.md"))
    return ["README.md", "CLAUDE.md", "AGENTS.md", *docs]


@pytest.mark.parametrize("rel", _current_docs())
def test_removed_features_are_not_documented(rel: str) -> None:
    mentions = re.findall(r"(?i)billing|white[- ]?label|openclaw", _read(rel))
    assert mentions == [], f"{rel} mentions removed features: {sorted(set(mentions))}"


def test_license_is_agpl_3_only_everywhere() -> None:
    license_text = _read("LICENSE")
    assert license_text.split("\n", 1)[0].strip() == "GNU AFFERO GENERAL PUBLIC LICENSE"
    assert "Version 3, 19 November 2007" in license_text

    project = tomllib.loads(_read("pyproject.toml"))["project"]
    assert project["license"] == {"text": "AGPL-3.0-only"}
    license_classifiers = [c for c in project["classifiers"] if c.startswith("License ::")]
    assert license_classifiers == [
        "License :: OSI Approved :: GNU Affero General Public License v3"
    ]

    readme = _read("README.md")
    section = _section(readme, "## License")
    assert "GNU Affero General Public License" in section
    assert "version 3." in section
    assert "any later version" not in section  # -only, not -or-later
    assert "license-AGPL--3.0-" in readme  # the badge
    assert not re.search(r"(?i)\bMIT License\b|Apache License", readme)
