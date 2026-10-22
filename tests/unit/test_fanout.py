"""Tests for multi-task fan-out."""

import logging

import pytest

from delegatron.orchestrator.fanout import build_execution_waves


def test_single_task_one_wave():
    tasks = [{"task_id": "t1", "title": "Do thing", "depends_on": []}]
    waves = build_execution_waves(tasks)
    assert len(waves) == 1
    assert len(waves[0]) == 1


def test_independent_tasks_parallel():
    tasks = [
        {"task_id": "t1", "depends_on": []},
        {"task_id": "t2", "depends_on": []},
        {"task_id": "t3", "depends_on": []},
    ]
    waves = build_execution_waves(tasks)
    assert len(waves) == 1
    assert len(waves[0]) == 3


def test_sequential_dependencies():
    tasks = [
        {"task_id": "t1", "depends_on": []},
        {"task_id": "t2", "depends_on": ["t1"]},
        {"task_id": "t3", "depends_on": ["t2"]},
    ]
    waves = build_execution_waves(tasks)
    assert len(waves) == 3
    assert waves[0][0]["task_id"] == "t1"
    assert waves[1][0]["task_id"] == "t2"
    assert waves[2][0]["task_id"] == "t3"


def test_diamond_dependency():
    tasks = [
        {"task_id": "t1", "depends_on": []},
        {"task_id": "t2", "depends_on": ["t1"]},
        {"task_id": "t3", "depends_on": ["t1"]},
        {"task_id": "t4", "depends_on": ["t2", "t3"]},
    ]
    waves = build_execution_waves(tasks)
    assert len(waves) == 3
    assert len(waves[0]) == 1  # t1
    assert len(waves[1]) == 2  # t2, t3 in parallel
    assert len(waves[2]) == 1  # t4


def test_empty_tasks():
    assert build_execution_waves([]) == []


def _ids(waves: list[list[dict]]) -> list[list[str]]:
    return [[task["task_id"] for task in wave] for wave in waves]


def test_done_tasks_satisfy_dependencies():
    """A re-plan builds waves for the pending tasks only (#79)."""
    tasks = [{"task_id": "t3", "depends_on": ["t2"]}, {"task_id": "t4", "depends_on": ["t3"]}]

    assert _ids(build_execution_waves(tasks, done={"t2"})) == [["t3"], ["t4"]]


@pytest.mark.parametrize(
    "tasks",
    [
        pytest.param(
            [
                {"task_id": "t1", "depends_on": []},
                {"task_id": "t2", "depends_on": ["t3"]},
                {"task_id": "t3", "depends_on": ["t2"]},
                {"task_id": "t4", "depends_on": ["missing"]},
            ],
            id="cycle-and-unknown",
        ),
    ],
)
def test_unresolvable_tasks_get_one_wave_each(
    tasks: list[dict], caplog: pytest.LogCaptureFixture
) -> None:
    """Tasks that can never be ready are not batched into one concurrent wave (#100)."""
    with caplog.at_level(logging.WARNING, logger="delegatron.orchestrator.fanout"):
        waves = build_execution_waves(tasks)

    assert _ids(waves) == [["t1"], ["t2"], ["t3"], ["t4"]]
