"""Automatic preset selection based on task complexity and project characteristics.

Provides heuristic-based selection of the best model preset for a given
task request and project context. This helps users who don't know which
preset to use get reasonable defaults without manual configuration.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Keywords that suggest a simple/fast task
_FAST_KEYWORDS: set[str] = {
    "simple",
    "quick",
    "small",
    "trivial",
    "minor",
    "typo",
    "rename",
    "bump",
    "version",
}

# Keywords that suggest a complex/heavy task
_HEAVY_KEYWORDS: set[str] = {
    "security",
    "complex",
    "refactor large",
    "refactor",
    "architecture",
    "redesign",
    "migrate",
    "performance",
    "optimize",
    "concurrent",
    "async",
    "distributed",
    "database",
    "authentication",
}

# Languages that are well-supported by smaller models
_EASY_LANGUAGES: set[str] = {"python", "javascript", "typescript"}

# Languages where larger models produce significantly better results
_HARD_LANGUAGES: set[str] = {"rust", "go", "haskell", "scala", "kotlin", "c++"}

# Threshold for "many files" in a project
_LARGE_PROJECT_FILE_COUNT = 50


def select_preset(request: str, project_info: dict[str, Any]) -> tuple[str, str]:
    """Select the best preset based on the request text and project info.

    Args:
        request: The user's natural-language task description.
        project_info: Detected project metadata (languages, frameworks,
            file count, etc.).

    Returns:
        A tuple of ``(preset_name, reason)`` explaining why the preset
        was chosen.
    """
    request_lower = request.lower()
    reasons: list[str] = []

    # Score: negative = fast, positive = heavy, 0 = balanced
    score = 0

    # --- Analyse the request text ---

    # Check for fast keywords
    for keyword in _FAST_KEYWORDS:
        if keyword in request_lower:
            score -= 1
            reasons.append(f"request contains '{keyword}'")
            break  # one match is enough

    # Check for heavy keywords (multi-word keywords checked first)
    for keyword in sorted(_HEAVY_KEYWORDS, key=len, reverse=True):
        if keyword in request_lower:
            score += 2
            reasons.append(f"request contains '{keyword}'")
            break

    # Check for multi-file tasks (mentioning many files)
    file_mentions = len(re.findall(r"\b\w+\.\w{1,5}\b", request_lower))
    if file_mentions > 5:
        score += 1
        reasons.append(f"request references ~{file_mentions} files")

    # --- Analyse the project ---

    languages = {lang.lower() for lang in project_info.get("languages", [])}

    # Language difficulty
    if languages & _HARD_LANGUAGES:
        hard_langs = languages & _HARD_LANGUAGES
        score += 1
        msg = f"project uses {', '.join(sorted(hard_langs))} (benefits from larger models)"
        reasons.append(msg)
    elif languages and languages <= _EASY_LANGUAGES:
        score -= 1
        msg = f"project uses {', '.join(sorted(languages))} (well-supported by all models)"
        reasons.append(msg)

    # Project size
    file_count = project_info.get("file_count", 0)
    if file_count > _LARGE_PROJECT_FILE_COUNT:
        score += 1
        reasons.append(f"large project ({file_count} files)")

    # --- Map score to preset ---

    if score <= -1:
        preset = "budget"
        summary = "simple task / well-supported stack"
    elif score >= 2:
        preset = "claude-heavy"
        summary = "complex task / challenging stack"
    else:
        preset = "default"
        summary = "balanced complexity"

    reason = f"{summary} ({'; '.join(reasons)})" if reasons else summary
    logger.info("Auto-selected preset '%s': %s", preset, reason)
    return preset, reason
