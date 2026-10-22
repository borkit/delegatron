"""Claude architect agent — decomposes requirements into a WorkPlan."""

from __future__ import annotations

import itertools
import json
import posixpath
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel

from delegatron.agents.base import BaseAgent
from delegatron.agents.rendering import fenced, json_block
from delegatron.schemas.work_plan import WorkPlan

# Paths listed in "Existing Files"; a longer tree is ranked and summarised.
MAX_TREE_ENTRIES = 200

# Directories named in the "N more files not shown" summary line.
_SUMMARY_DIRS = 20

# Shortest file stem or directory name matched against the request's words.
_MIN_MATCH_LENGTH = 4

_SOURCE_EXTENSIONS = frozenset(
    {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".rb",
        ".php",
        ".cs",
        ".fs",
        ".swift",
        ".scala",
        ".dart",
        ".ex",
        ".exs",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".lua",
        ".zig",
    }
)

_WORD = re.compile(r"[a-z0-9_]+")
_TEST_AFFIX = re.compile(r"^test_|_test$|\.test$|\.spec$")


class ArchitectAgent(BaseAgent):
    """Uses the architect LLM to decompose user requests into structured WorkPlans.

    Responsibilities:
    - Translate user request into implementable tasks
    - Define measurable acceptance criteria
    - Set termination rules and rollback plans
    """

    def _build_user_message(self, input_data: dict[str, Any]) -> str:
        parts: list[str] = []

        # User request
        request = input_data.get("request", "")
        parts.append(f"## User Request\n\n{request}")

        # Project context (from detection)
        project_info = input_data.get("project_info")
        if project_info:
            context_json = json.dumps(project_info, indent=2)
            parts.append(f"## Project Context\n\n```json\n{context_json}\n```")

        # File tree
        file_tree = input_data.get("file_tree")
        if file_tree:
            tree_str = format_file_tree(file_tree, request)
            parts.append(f"## Existing Files\n\n```\n{tree_str}\n```")

        # Structural map (signatures) of the repository
        repo_map = input_data.get("repo_map")
        if repo_map:
            parts.append(f"## Repository Map\n\n{fenced(repo_map)}")

        # Tasks already done (on re-planning iterations)
        completed_tasks = input_data.get("completed_tasks")
        if completed_tasks:
            completed_json = json.dumps(completed_tasks, indent=2)
            parts.append(f"## Completed Tasks\n\n```json\n{completed_json}\n```")

        # Tasks given up without being completed (on re-planning iterations)
        skipped_tasks = input_data.get("skipped_tasks")
        if skipped_tasks:
            parts.append(f"## Skipped Tasks\n\n{json_block(skipped_tasks)}")

        # The task in progress when the re-plan was asked for
        current_task = input_data.get("current_task")
        if current_task:
            parts.append(f"## Current Task\n\n{json_block(current_task)}")

        # Review feedback (on re-planning iterations); the prompts say how to use it
        review_feedback = input_data.get("review_feedback")
        if review_feedback:
            parts.append(
                "## Review Feedback (Architectural Issues)\n\n"
                f"```json\n{json.dumps(review_feedback, indent=2)}\n```"
            )

        # Pipeline constraints
        constraints = input_data.get("constraints")
        if constraints:
            parts.append(f"## Constraints\n\n```json\n{json.dumps(constraints, indent=2)}\n```")

        return "\n\n".join(parts)

    def _get_output_schema(self) -> dict[str, Any]:
        return WorkPlan.model_json_schema()

    def _get_output_model(self) -> type[BaseModel]:
        return WorkPlan


def format_file_tree(
    file_tree: Sequence[str], request: str = "", max_entries: int = MAX_TREE_ENTRIES
) -> str:
    """Render the workspace file list, keeping the most relevant paths of a long tree.

    A tree of at most *max_entries* paths is listed whole.  A longer one keeps
    *max_entries* paths, chosen in this order: files whose name (minus test
    affixes) the request mentions, files in directories it mentions, root-level
    files, source and test files taken one per directory in turn (so every
    source directory appears), then everything else.  Kept paths stay in their
    original order; a last line counts the omitted files per directory.
    """
    paths = [path.replace("\\", "/") for path in file_tree]
    if len(paths) <= max_entries:
        return "\n".join(paths)

    words = {w for w in _WORD.findall(request.lower()) if len(w) >= _MIN_MATCH_LENGTH}
    tiers = (
        [p for p in paths if _stem(p) in words],
        [p for p in paths if words.intersection(posixpath.dirname(p).lower().split("/"))],
        [p for p in paths if "/" not in p],
        _round_robin_by_dir(
            p for p in paths if posixpath.splitext(p)[1].lower() in _SOURCE_EXTENSIONS
        ),
        paths,
    )
    chosen: set[str] = set()
    for path in itertools.chain.from_iterable(tiers):
        if len(chosen) == max_entries:
            break
        chosen.add(path)

    shown = [p for p in paths if p in chosen]
    omitted = Counter(_dir_label(p) for p in paths if p not in chosen)
    ranked = sorted(omitted.items(), key=lambda item: (-item[1], item[0]))
    summary = ", ".join(f"{directory} ({count})" for directory, count in ranked[:_SUMMARY_DIRS])
    if len(ranked) > _SUMMARY_DIRS:
        summary += f", +{len(ranked) - _SUMMARY_DIRS} more dirs"
    shown.append(f"... {sum(omitted.values())} more files not shown: {summary}")
    return "\n".join(shown)


def _stem(path: str) -> str:
    """Lower-case file name without extension or test affixes (``test_retry.py`` → ``retry``)."""
    stem = posixpath.splitext(posixpath.basename(path))[0].lower()
    return _TEST_AFFIX.sub("", stem)


def _round_robin_by_dir(paths: Iterable[str]) -> list[str]:
    """Interleave *paths* so each directory gives one file before any gives a second."""
    by_dir: dict[str, list[str]] = {}
    for path in paths:
        by_dir.setdefault(posixpath.dirname(path), []).append(path)
    rounds = itertools.zip_longest(*by_dir.values())
    return [path for batch in rounds for path in batch if path is not None]


def _dir_label(path: str) -> str:
    directory = posixpath.dirname(path)
    return f"{directory}/" if directory else "./"
