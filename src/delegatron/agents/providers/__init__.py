"""LLM provider adapters for Delegatron.

Supported providers:
- anthropic: Anthropic Claude API
- openai: OpenAI API
- google: Google Gemini API
- openai-compatible: Any OpenAI-compatible endpoint (Ollama, Grok, Copilot, etc.)
"""

from delegatron.agents.providers.base import BaseProvider, ProviderResponse
from delegatron.agents.providers.registry import get_provider

__all__ = ["BaseProvider", "ProviderResponse", "get_provider"]
