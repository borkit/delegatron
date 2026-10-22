"""The published image must be able to run pipelines (checked as text, no Docker)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _stages() -> dict[str, str]:
    """Dockerfile text per ``FROM ... AS <name>`` stage."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    parts = re.split(r"^FROM\s+\S+\s+AS\s+(\w+)\s*$", text, flags=re.MULTILINE | re.IGNORECASE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_builder_copies_every_force_included_source() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    builder = _stages()["builder"]

    for source in force_include:
        assert re.search(rf"^COPY\s+{re.escape(source)}/?\s", builder, re.MULTILINE), source


def test_builder_installs_the_cloud_provider_extra() -> None:
    builder = _stages()["builder"]

    assert re.search(r"^ARG\s+EXTRAS=\S*cloud-providers", builder, re.MULTILINE)
    assert 'pip install --no-cache-dir ".[${EXTRAS}]"' in builder


def test_runtime_stage_installs_git() -> None:
    runtime = _stages()["runtime"]

    assert re.search(r"apt-get install[^\n]*\bgit\b", runtime)


def test_runtime_configures_workspace_artifacts_and_data_dirs() -> None:
    runtime = _stages()["runtime"]

    assert "DELEGATRON_WORKSPACE=/app/workspace" in runtime
    assert "DELEGATRON_ARTIFACTS_DIR=/app/runs" in runtime
    assert "DELEGATRON_DATA_DIR=/app/data" in runtime


def test_runtime_logs_to_a_dir_the_app_user_owns() -> None:
    # The default log dir resolves inside the root-owned /opt/venv.
    runtime = _stages()["runtime"]

    assert "DELEGATRON_LOG_DIR=/app/logs" in runtime
    assert re.search(r"^RUN mkdir -p [^\n]*/app/logs\b", runtime, re.MULTILINE)
    assert re.search(r"chown -R delegatron:delegatron /app\b", runtime)


def test_compose_mounts_those_dirs_and_starts_no_unused_services() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    for mount in ("./workspace:/app/workspace", "./runs:/app/runs", "./data:/app/data"):
        assert mount in compose
    assert "redis" not in compose
    assert "DELEGATRON_MAX_WALL_TIME:-60" in compose
