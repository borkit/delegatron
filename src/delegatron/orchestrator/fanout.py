"""Multi-task fan-out — group tasks that can be worked on concurrently.

Analyzes task dependencies from the WorkPlan and groups tasks into
execution waves. Tasks within a wave have no mutual dependencies, so with
``parallel_tasks`` their first patches can be generated concurrently (each in
its own git worktree, see ``graph.Pipeline._run_parallel_implementers``).
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Any

logger = logging.getLogger(__name__)


def build_execution_waves(
    tasks: list[dict[str, Any]], done: Collection[str] = frozenset()
) -> list[list[dict[str, Any]]]:
    """Group tasks into parallel execution waves based on dependencies.

    Tasks in the same wave have no dependencies on each other and can
    run concurrently. Waves are ordered so that all dependencies of
    wave N are satisfied by waves 0..N-1 or by *done* (ids of tasks already
    finished, e.g. the completed tasks of a re-planned run).  Tasks whose
    dependencies can never be satisfied (a cycle, an unknown id) get one wave
    each, in plan order.

    Returns:
        List of waves, where each wave is a list of tasks.
    """
    if not tasks:
        return []

    # Build dependency graph
    task_map = {t.get("task_id", f"t{i}"): t for i, t in enumerate(tasks)}
    completed: set[str] = set(done)
    remaining = dict(task_map)
    waves: list[list[dict[str, Any]]] = []

    while remaining:
        # Find tasks whose dependencies are all completed
        ready = [
            (task_id, task)
            for task_id, task in remaining.items()
            if set(task.get("depends_on", [])) <= completed
        ]

        if not ready:
            logger.warning(
                "Cannot resolve dependencies for %d remaining tasks, running them one at a time",
                len(remaining),
            )
            waves.extend([task] for task in remaining.values())
            break

        waves.append([task for _, task in ready])

        for task_id, _ in ready:
            completed.add(task_id)
            del remaining[task_id]

    logger.info(
        "Execution plan: %d waves from %d tasks (max parallelism: %d)",
        len(waves),
        len(tasks),
        max(len(w) for w in waves),
    )

    return waves
