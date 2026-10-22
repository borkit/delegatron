"""Provider registry — maps provider names to adapter classes.

Routing strategy:
- "mock" → MockProvider (testing, no API keys needed)
- "openai-compatible" → OpenAIProvider (Ollama, local models — native adapter)
- "anthropic" → AnthropicProvider (requires anthropic SDK)
- "google" → GoogleProvider (requires google-genai SDK)
- "openai" or unknown → OpenAIProvider (fallback)
"""

from __future__ import annotations

import logging

from delegatron.agents.providers.base import BaseProvider
from delegatron.config import RoleConfig

logger = logging.getLogger(__name__)


def get_provider(role_config: RoleConfig) -> BaseProvider:
    """Create a provider adapter from a role configuration.

    Args:
        role_config: Role configuration with provider, model, api_key, etc.

    Returns:
        Configured provider adapter instance.
    """
    provider = role_config.provider
    model = role_config.model

    # --- Mock provider (testing) ---
    if provider == "mock":
        from delegatron.agents.providers.mock import MockProvider

        role = model.replace("mock-", "") if model.startswith("mock-") else "architect"
        return MockProvider(role=role)

    # The key comes only from RoleConfig.resolve_api_key(), which decides
    # whether an environment key may be sent to this role's endpoint.
    api_key = role_config.resolve_api_key()
    base_url = role_config.base_url
    timeout_seconds = role_config.timeout_seconds

    # --- Local / Ollama (native adapter with v2 prompt support, JSON repair) ---
    if provider == "openai-compatible":
        from delegatron.agents.providers.openai_provider import OpenAIProvider

        return OpenAIProvider(
            api_key=api_key, model=model, base_url=base_url, timeout_seconds=timeout_seconds
        )

    # --- Anthropic (requires `pip install anthropic`) ---
    if provider == "anthropic":
        from delegatron.agents.providers.anthropic import AnthropicProvider

        return AnthropicProvider(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            env_auth=role_config.allow_env_key,
        )

    # --- Google (requires `pip install google-genai`) ---
    if provider == "google":
        from delegatron.agents.providers.google import GoogleProvider

        return GoogleProvider(
            api_key=api_key, model=model, base_url=base_url, timeout_seconds=timeout_seconds
        )

    # --- OpenAI or unknown → OpenAI-compatible fallback ---
    from delegatron.agents.providers.openai_provider import OpenAIProvider

    return OpenAIProvider(
        api_key=api_key, model=model, base_url=base_url, timeout_seconds=timeout_seconds
    )
