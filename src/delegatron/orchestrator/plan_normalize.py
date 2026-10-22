"""Work-plan normalization — make architect plans consistent before execution.

Architect plans (especially from small local models) can contradict the
implementer's conventions: duplicate or empty task ids, ``depends_on``
entries naming no task of the plan, unsafe or messy output paths, root-level
``test_*.py`` outputs while tests are written under ``tests/``, code tasks
that list no test file, or a follow-up task that only regenerates its
dependency's files and clobbers that dependency's tests.  ``normalize_plan``
repairs these deterministically so plan outputs can be trusted for task
selection, context selection and completion checks.
"""

from __future__ import annotations

import copy
import posixpath
from typing import Any

from pydantic import ValidationError

from delegatron.orchestrator.suite_guard import is_test_path
from delegatron.schemas.work_plan import WorkPlan
from delegatron.utils.paths import normalize_rel_path

TESTS_DIR = "tests"

# Change kinds reported by ``normalize_plan_changes``.
TASK_ID_CHANGED = "task_id_changed"
DEPENDENCY_DROPPED = "dependency_dropped"
OUTPUT_DROPPED = "output_dropped"
OUTPUT_NORMALIZED = "output_normalized"
TEST_PATH_MOVED = "test_path_moved"
REDUNDANT_TASK_MERGED = "redundant_task_merged"
DEPENDENCY_REWIRED = "dependency_rewired"
TEST_OUTPUT_ADDED = "test_output_added"

_RISK_ORDER = ("low", "medium", "high")


def normalize_plan(
    plan: dict[str, Any],
    file_tree: list[str],
    *,
    merge_redundant: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    """Return a normalized copy of *plan* and one warning per change made.

    The warnings are the ``message`` of each ``normalize_plan_changes`` change
    (``"<task id>: <what changed>"``).
    """
    normalized, changes = normalize_plan_changes(plan, file_tree, merge_redundant=merge_redundant)
    return normalized, [change["message"] for change in changes]


def normalize_plan_changes(
    plan: dict[str, Any],
    file_tree: list[str],
    *,
    merge_redundant: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return a normalized copy of *plan* and the changes made.

    Each change is ``{"kind", "task_id", "before", "after", "message"}``.  In
    order:

    1. Task ids are stripped and made unique and non-empty (the first holder
       of an id keeps it; the others get the next free ``t<n>``:
       ``task_id_changed``); ``depends_on`` entries are stripped, and those
       naming the task itself or no task of the plan are dropped
       (``dependency_dropped``).
    2. Path hygiene: outputs become clean relative posix paths (backslashes,
       ``./`` and duplicates go: ``output_normalized``); absolute, ``..`` and
       otherwise unsafe paths are dropped (``output_dropped``).
    3. Test layout (Python): root-level ``test_*.py`` / ``*_test.py`` outputs
       move to ``tests/`` — or to wherever *file_tree* already has a test file
       of that name — unless the project keeps its tests at the root
       (*file_tree* has root-level test modules and no ``tests/``):
       ``test_path_moved``.
    4. Redundant tasks (only when *merge_redundant*: a re-plan with completed
       tasks must not merge pending work into a finished task).  Task B is
       merged into its dependency A when B's non-test outputs are non-empty
       and all produced by A, and B's other dependencies already precede A
       (no cycle or reordering).  A takes B's description, the union of both
       outputs and the higher ``risk_level`` (``redundant_task_merged``);
       references to B now name A (``dependency_rewired``).  Test-only tasks
       are never merged.
    5. Python code tasks with no test output gain ``tests/test_<stem>.py``
       (or the root-layout / existing path) for their first module, unless
       another task already lists that path (``test_output_added``).
    6. When *plan* is a valid ``WorkPlan``, the result is validated as one.

    Pure (the input is not mutated) and idempotent; never drops the last task.
    """
    normalized = copy.deepcopy(plan)
    tasks: list[dict[str, Any]] = normalized.get("tasks") or []
    tree = [path for path in map(normalize_rel_path, file_tree) if path]
    root_layout = _uses_root_layout(tree)
    changes: list[dict[str, Any]] = []

    _normalize_task_ids(tasks, changes)
    for task in tasks:
        _clean_outputs(task, changes)
        _relocate_root_tests(task, tree, root_layout, changes)

    while merge_redundant and (pair := _find_redundant_task(tasks)) is not None:
        _merge_task(tasks, *pair, changes)

    for task in tasks:
        _add_missing_test(task, tasks, tree, root_layout, changes)

    _validate(plan, normalized)
    return normalized, changes


def _change(
    changes: list[dict[str, Any]],
    kind: str,
    task: dict[str, Any],
    before: Any,
    after: Any,
    text: str,
) -> None:
    """Record one change of *task* (message ``"<task id>: <text>"``)."""
    label = _label(task)
    changes.append(
        {
            "kind": kind,
            "task_id": label,
            "before": before,
            "after": after,
            "message": f"{label}: {text}",
        }
    )


def _normalize_task_ids(tasks: list[dict[str, Any]], changes: list[dict[str, Any]]) -> None:
    """Give every task a unique non-empty id and drop dangling or self dependencies."""
    seen: set[str] = set()
    taken = {_clean_id(task.get("task_id")) for task in tasks}
    next_number = 1
    for task in tasks:
        original = task.get("task_id")
        task_id = _clean_id(original)
        if not task_id or task_id in seen:
            while f"t{next_number}" in taken:
                next_number += 1
            new_id = f"t{next_number}"
            taken.add(new_id)
            task["task_id"] = new_id
            _change(
                changes,
                TASK_ID_CHANGED,
                task,
                original,
                new_id,
                f"task id {task_id!r} is empty or taken — now {new_id}",
            )
            task_id = new_id
        elif original != task_id:
            task["task_id"] = task_id
        seen.add(task_id)

    for task in tasks:
        deps: list[Any] = task.get("depends_on") or []
        cleaned = [_clean_id(dep) for dep in deps]
        kept = [dep for dep in dict.fromkeys(cleaned) if dep in seen and dep != task["task_id"]]
        if kept != deps:
            dropped = sorted(set(cleaned) - set(kept))
            if dropped:
                _change(
                    changes,
                    DEPENDENCY_DROPPED,
                    task,
                    list(deps),
                    kept,
                    f"dropped unknown dependencies {dropped}",
                )
            task["depends_on"] = kept


def _clean_id(value: Any) -> str:
    """A task id or dependency as a stripped string (``""`` for none)."""
    return "" if value is None else str(value).strip()


def _clean_outputs(task: dict[str, Any], changes: list[dict[str, Any]]) -> None:
    """Make the task's outputs safe, clean, de-duplicated relative posix paths."""
    outputs: list[Any] = task.get("outputs") or []
    cleaned: list[str] = []
    for path in outputs:
        clean = normalize_rel_path(path) if isinstance(path, str) else None
        if clean is None:
            _change(changes, OUTPUT_DROPPED, task, path, None, f"dropped unsafe output {path!r}")
        elif clean not in cleaned:
            cleaned.append(clean)
    if cleaned != outputs:
        kept_before = [
            path for path in outputs if isinstance(path, str) and normalize_rel_path(path)
        ]
        if kept_before != cleaned:
            _change(
                changes,
                OUTPUT_NORMALIZED,
                task,
                kept_before,
                cleaned,
                f"normalized outputs {kept_before} to {cleaned}",
            )
        task["outputs"] = cleaned


def _relocate_root_tests(
    task: dict[str, Any], tree: list[str], root_layout: bool, changes: list[dict[str, Any]]
) -> None:
    """Move the task's root-level Python test outputs to the project's test location."""
    outputs: list[str] = task.get("outputs") or []
    relocated: list[str] = []
    for path in outputs:
        target = path
        if "/" not in path and _is_python_test_name(path):
            target = _test_file_path(path, tree, root_layout)
            if target != path:
                _change(
                    changes,
                    TEST_PATH_MOVED,
                    task,
                    path,
                    target,
                    f"moved test output {path} to {target}",
                )
        if target not in relocated:
            relocated.append(target)
    if relocated != outputs:
        task["outputs"] = relocated


def _add_missing_test(
    task: dict[str, Any],
    tasks: list[dict[str, Any]],
    tree: list[str],
    root_layout: bool,
    changes: list[dict[str, Any]],
) -> None:
    """Give a Python code task without any test output a test file for its first module.

    Skipped when another task of the plan already lists that test file: two
    tasks owning one test file would regenerate each other's tests.
    """
    outputs: list[str] = task.get("outputs") or []
    # conftest.py holds fixtures, not tests
    if any(is_test_path(path) and posixpath.basename(path) != "conftest.py" for path in outputs):
        return
    module = next((path for path in outputs if _is_python_module(path)), None)
    if module is None:
        return
    stem = posixpath.splitext(posixpath.basename(module))[0]
    test_path = _test_file_path(f"test_{stem}.py", tree, root_layout)
    if any(test_path in (other.get("outputs") or []) for other in tasks):
        return
    task["outputs"] = [*outputs, test_path]
    _change(changes, TEST_OUTPUT_ADDED, task, None, test_path, f"added test output {test_path}")


def _find_redundant_task(
    tasks: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return ``(dependency, task)`` for the first code task whose sources a dependency produces."""
    by_id = {task["task_id"]: task for task in tasks if task.get("task_id")}
    for task in tasks:
        sources = _sources(task)
        if not sources:  # test-only (or empty) tasks are never merged
            continue
        deps: list[str] = task.get("depends_on") or []
        for dep_id in deps:
            dep = by_id.get(dep_id)
            if dep is None or dep is task or not sources <= _sources(dep):
                continue
            # Merging must not make the dependency wait for anything new.
            if set(deps) - {dep_id} <= _ancestors(dep_id, by_id):
                return dep, task
    return None


def _merge_task(
    tasks: list[dict[str, Any]],
    target: dict[str, Any],
    redundant: dict[str, Any],
    changes: list[dict[str, Any]],
) -> None:
    """Fold *redundant* into *target* and repoint references to it."""
    target_id = target["task_id"]
    redundant_id = redundant.get("task_id")
    descriptions = [target.get("description", ""), redundant.get("description", "")]
    target["description"] = "\n\n".join(text for text in descriptions if text)
    outputs = [*(target.get("outputs") or []), *(redundant.get("outputs") or [])]
    target["outputs"] = list(dict.fromkeys(outputs))
    risk = _max_risk(target.get("risk_level"), redundant.get("risk_level"))
    if risk is not None:
        target["risk_level"] = risk
    tasks[:] = [task for task in tasks if task is not redundant]
    _change(
        changes,
        REDUNDANT_TASK_MERGED,
        redundant,
        redundant_id,
        target_id,
        f"outputs already produced by dependency {target_id} — merged into {target_id}",
    )

    for task in tasks:
        deps: list[str] = task.get("depends_on") or []
        if redundant_id is None or redundant_id not in deps:
            continue
        repointed = (target_id if dep_id == redundant_id else dep_id for dep_id in deps)
        rewired = [dep_id for dep_id in dict.fromkeys(repointed) if dep_id != task.get("task_id")]
        task["depends_on"] = rewired
        _change(
            changes,
            DEPENDENCY_REWIRED,
            task,
            list(deps),
            rewired,
            f"dependency {redundant_id} rewired to {target_id}",
        )


def _sources(task: dict[str, Any]) -> set[str]:
    """The task's non-test outputs."""
    return {path for path in task.get("outputs") or [] if not is_test_path(path)}


def _max_risk(first: Any, second: Any) -> str | None:
    """The higher of two ``risk_level`` values (unknown values rank lowest)."""
    known = [risk for risk in (first, second) if risk in _RISK_ORDER]
    if not known:
        return first if isinstance(first, str) else None
    return str(max(known, key=_RISK_ORDER.index))


def _ancestors(task_id: str, by_id: dict[str, dict[str, Any]]) -> set[str]:
    """Return every task id that *task_id* depends on, directly or transitively."""
    seen: set[str] = set()
    stack = list(by_id[task_id].get("depends_on") or [])
    while stack:
        dep_id = stack.pop()
        if dep_id in seen:
            continue
        seen.add(dep_id)
        if dep_id in by_id:
            stack.extend(by_id[dep_id].get("depends_on") or [])
    return seen


def _uses_root_layout(tree: list[str]) -> bool:
    """True when the project keeps Python tests at the root: root test modules, no ``tests/``."""
    has_tests_dir = any(path.split("/", 1)[0] == TESTS_DIR for path in tree)
    has_root_tests = any("/" not in path and _is_python_test_name(path) for path in tree)
    return has_root_tests and not has_tests_dir


def _test_file_path(name: str, tree: list[str], root_layout: bool) -> str:
    """Return where test file *name* lives: its shallowest existing path, else the convention.

    Reusing an existing path avoids two test modules with the same basename,
    which pytest's default import mode rejects.  Otherwise ``tests/<name>``,
    or ``<name>`` in a root-layout project.
    """
    existing = [path for path in tree if posixpath.basename(path) == name]
    if existing:
        return min(existing, key=lambda path: (path.count("/"), path))
    return name if root_layout else f"{TESTS_DIR}/{name}"


def _is_python_test_name(name: str) -> bool:
    """True for a pytest module name: ``test_*.py`` or ``*_test.py``."""
    base = posixpath.basename(name)
    return base.endswith(".py") and (base.startswith("test_") or base.endswith("_test.py"))


def _is_python_module(path: str) -> bool:
    """Return True for a Python source module that warrants its own test file."""
    name = posixpath.basename(path)
    return (
        name.endswith(".py")
        and not name.startswith("__")
        and name not in ("conftest.py", "setup.py")
        and not is_test_path(path)
    )


def _validate(original: dict[str, Any], normalized: dict[str, Any]) -> None:
    """Validate *normalized* as a ``WorkPlan`` when *original* is one.

    Raises ``ValidationError`` when normalization broke a valid plan; a
    partial input (callers that validate the plan themselves) is not checked.
    """
    try:
        WorkPlan.model_validate(original)
    except ValidationError:
        return
    WorkPlan.model_validate(normalized)


def _label(task: dict[str, Any]) -> str:
    """Return a short name for *task* in warnings."""
    return str(task.get("task_id") or task.get("title") or "task")
