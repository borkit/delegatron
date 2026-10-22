"""Prompt loader — loads versioned system prompts from external files.

Prompts are stored in the prompts/ directory as markdown files,
organized by role and version: prompts/{role}/v{n}.md

This keeps prompts out of source code so they can be iterated on
independently and tracked with provenance.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

# Default prompts directory (relative to package root, resolved at runtime)
_PROMPTS_DIR: Path | None = None


def _find_prompts_dir() -> Path:
    """Locate the prompts directory by walking up from the package."""
    global _PROMPTS_DIR
    if _PROMPTS_DIR is not None:
        return _PROMPTS_DIR

    # Try relative to this file (src/delegatron/prompts.py -> ../../prompts/)
    pkg_dir = Path(__file__).parent
    candidates = [
        pkg_dir / "../../prompts",  # development: src/delegatron/../../prompts
        pkg_dir / "prompts",  # installed: delegatron/prompts
        Path.cwd() / "prompts",  # fallback: cwd/prompts
    ]

    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_dir():
            _PROMPTS_DIR = resolved
            return resolved

    # Final fallback
    _PROMPTS_DIR = Path.cwd() / "prompts"
    return _PROMPTS_DIR


def set_prompts_dir(path: Path) -> None:
    """Override the prompts directory (useful for testing)."""
    global _PROMPTS_DIR
    _PROMPTS_DIR = path.resolve()
    load_prompt.cache_clear()


@lru_cache(maxsize=32)
def load_prompt(role: str, version: int = 1) -> str:
    """Load a system prompt for a given role and version.

    Args:
        role: Agent role name (architect, implementer, reviewer).
        version: Prompt version number.

    Returns:
        The prompt text content.

    Raises:
        FileNotFoundError: If the prompt file doesn't exist.
    """
    prompts_dir = _find_prompts_dir()
    prompt_path = prompts_dir / role / f"v{version}.md"

    if not prompt_path.exists():
        raise FileNotFoundError(
            f"Prompt not found: {prompt_path}. Ensure prompts/{role}/v{version}.md exists."
        )

    return prompt_path.read_text(encoding="utf-8")


def list_prompt_versions(role: str) -> list[int]:
    """List available prompt versions for a role."""
    prompts_dir = _find_prompts_dir()
    role_dir = prompts_dir / role

    if not role_dir.is_dir():
        return []

    versions = []
    for path in role_dir.glob("v*.md"):
        try:
            version = int(path.stem[1:])  # "v1" -> 1
            versions.append(version)
        except ValueError:
            continue

    return sorted(versions)


def get_latest_version(role: str) -> int:
    """Get the latest prompt version number for a role."""
    versions = list_prompt_versions(role)
    if not versions:
        raise FileNotFoundError(f"No prompts found for role: {role}")
    return versions[-1]
