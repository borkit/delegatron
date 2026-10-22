"""GPU memory cleanup — unload Ollama models after pipeline runs.

Ollama keeps models resident in VRAM for a configurable keep-alive period
(default 5 minutes). For pipelines using multiple local models (e.g.
local: qwen3.5:9b + qwen3-coder:30b), this can exhaust GPU
memory. This module provides automatic cleanup after runs complete.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any

from delegatron.config import Config

logger = logging.getLogger(__name__)

# Providers that use Ollama (models loaded into GPU)
_OLLAMA_PROVIDERS = frozenset({"openai-compatible"})

# Base URLs that indicate Ollama
_OLLAMA_URL_MARKERS = ("11434", "ollama")


def _is_ollama_role(role_config: Any) -> bool:
    """Check if a role config points to an Ollama instance."""
    if role_config.provider not in _OLLAMA_PROVIDERS:
        return False
    base_url = getattr(role_config, "base_url", "") or ""
    return any(marker in base_url.lower() for marker in _OLLAMA_URL_MARKERS)


def get_ollama_models(config: Config) -> set[str]:
    """Extract unique Ollama model names from a pipeline config."""
    models: set[str] = set()
    for role in (config.architect, config.implementer, config.reviewer):
        if _is_ollama_role(role):
            models.add(role.model)
    return models


def unload_ollama_models(config: Config) -> list[str]:
    """Unload all Ollama models used in the pipeline config.

    Calls ``ollama stop <model>`` for each model, freeing GPU memory.
    Safe to call even if Ollama isn't running or models aren't loaded.

    Returns list of models that were successfully unloaded.
    """
    models = get_ollama_models(config)
    if not models:
        return []

    unloaded: list[str] = []
    for model in sorted(models):
        try:
            result = subprocess.run(
                ["ollama", "stop", model],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                unloaded.append(model)
                logger.info("Unloaded Ollama model: %s", model)
            else:
                # Model might not be loaded — that's fine
                logger.debug(
                    "ollama stop %s returned %d: %s",
                    model,
                    result.returncode,
                    result.stderr.strip(),
                )
        except FileNotFoundError:
            logger.debug("ollama CLI not found — skipping GPU cleanup")
            break
        except subprocess.TimeoutExpired:
            logger.warning("ollama stop %s timed out", model)

    if unloaded:
        logger.info("GPU cleanup: unloaded %d model(s): %s", len(unloaded), ", ".join(unloaded))

    return unloaded


def unload_model(model: str) -> bool:
    """Unload a single Ollama model from GPU memory.

    Returns True if successfully unloaded, False otherwise.
    """
    try:
        result = subprocess.run(
            ["ollama", "stop", model],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            logger.info("Unloaded Ollama model: %s", model)
            return True
        logger.debug("ollama stop %s returned %d", model, result.returncode)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return False


def unload_except(config: Config, keep_model: str) -> None:
    """Unload all Ollama models EXCEPT the one about to be used.

    Call this before each agent invocation to ensure only the active
    model is loaded in VRAM. Critical for systems with limited GPU
    memory (e.g. 12GB) running multi-model presets like local.

    Args:
        config: Pipeline config with all role model definitions.
        keep_model: The model name that should stay loaded.
    """
    models = get_ollama_models(config)
    models.discard(keep_model)

    for model in sorted(models):
        unload_model(model)


def cleanup_after_run(config: Config) -> None:
    """Run GPU cleanup after a pipeline run completes.

    Called from Pipeline.execute() finalization. Non-blocking — errors
    are logged but never propagate to the caller.
    """
    try:
        unloaded = unload_ollama_models(config)
        if not unloaded:
            logger.debug("No Ollama models to unload")
    except Exception as e:
        logger.debug("GPU cleanup error (non-fatal): %s", e)
