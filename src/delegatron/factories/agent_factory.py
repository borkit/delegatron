"""Centralized agent creation factory.

Replaces the duplicated _create_architect/_create_implementer/_create_reviewer
methods in graph.py and async_runner.py with a single creation point.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from delegatron.agents.architect import ArchitectAgent
from delegatron.agents.director import DirectorAgent
from delegatron.agents.implementer import ImplementerAgent
from delegatron.agents.providers.registry import get_provider
from delegatron.agents.reviewer import ReviewerAgent
from delegatron.config import Config, RoleConfig, clamp_temperature, is_local_model
from delegatron.prompts import load_prompt

logger = logging.getLogger(__name__)

# The director prompt is versioned on its own: the architect's prompt_version
# selects architect prompts, not director ones.
DIRECTOR_PROMPT_VERSION = 1

# Prompt sets for roles that do not pin prompt_version (0): cloud models get
# the cloud set, local models the newest set written for small local models.
# Pinned here so adding a prompt file never silently changes a role's prompt.
CLOUD_PROMPT_VERSION = 1
LOCAL_PROMPT_VERSION = 5


def resolve_prompt_version(role_config: RoleConfig) -> int:
    """Return the role's prompt version; 0 resolves by endpoint (cloud v1, local v5)."""
    if role_config.prompt_version:
        return role_config.prompt_version
    return LOCAL_PROMPT_VERSION if is_local_model(role_config) else CLOUD_PROMPT_VERSION


def create_architect(config: Config) -> ArchitectAgent:
    """Create an ArchitectAgent from pipeline config."""
    return cast(ArchitectAgent, _create_role_agent(config.architect, "architect", ArchitectAgent))


def create_implementer(config: Config) -> ImplementerAgent:
    """Create an ImplementerAgent from pipeline config."""
    return cast(
        ImplementerAgent,
        _create_role_agent(config.implementer, "implementer", ImplementerAgent),
    )


def create_reviewer(config: Config) -> ReviewerAgent:
    """Create a ReviewerAgent from pipeline config."""
    return cast(ReviewerAgent, _create_role_agent(config.reviewer, "reviewer", ReviewerAgent))


def create_director(config: Config) -> DirectorAgent:
    """Create a DirectorAgent that runs on the architect's provider and model."""
    return cast(
        DirectorAgent,
        _create_role_agent(
            config.architect,
            "director",
            DirectorAgent,
            prompt_version=DIRECTOR_PROMPT_VERSION,
        ),
    )


def _create_role_agent(
    role_config: RoleConfig,
    role_name: str,
    agent_cls: type,
    prompt_version: int = 0,
) -> Any:
    """Generic agent creation: provider + versioned prompt → agent instance.

    *prompt_version* overrides the role config's version (0 = use
    :func:`resolve_prompt_version`).
    """
    provider = get_provider(role_config)
    version = prompt_version or resolve_prompt_version(role_config)
    prompt = load_prompt(role_name, version=version)
    logger.debug(
        "Created %s agent: provider=%s model=%s prompt=v%d temp=%.2f",
        role_name,
        role_config.provider,
        role_config.model,
        version,
        clamp_temperature(role_config.temperature),
    )
    return agent_cls(
        provider=provider,
        system_prompt=prompt,
        max_tokens=role_config.max_tokens,
        temperature=clamp_temperature(role_config.temperature),
        context_window=role_config.context_window,
        timeout_seconds=role_config.timeout_seconds,
    )
