"""Tests for prompt loader."""

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from delegatron.agents.implementer import ImplementerAgent
from delegatron.agents.providers.base import BaseProvider
from delegatron.agents.reviewer import ReviewerAgent
from delegatron.config import PRESETS, load_config
from delegatron.factories.agent_factory import DIRECTOR_PROMPT_VERSION, resolve_prompt_version
from delegatron.prompts import (
    get_latest_version,
    list_prompt_versions,
    load_prompt,
    set_prompts_dir,
)
from delegatron.schemas.patch_set import PatchSet
from delegatron.schemas.work_plan import WorkPlan


def test_load_prompt_from_repo(tmp_path: Path):
    # Create test prompt structure
    architect_dir = tmp_path / "architect"
    architect_dir.mkdir()
    (architect_dir / "v1.md").write_text("You are the architect.")
    (architect_dir / "v2.md").write_text("You are the improved architect.")

    set_prompts_dir(tmp_path)

    assert load_prompt("architect", version=1) == "You are the architect."
    assert load_prompt("architect", version=2) == "You are the improved architect."


def test_list_prompt_versions(tmp_path: Path):
    role_dir = tmp_path / "reviewer"
    role_dir.mkdir()
    (role_dir / "v1.md").write_text("v1")
    (role_dir / "v3.md").write_text("v3")
    (role_dir / "v2.md").write_text("v2")

    set_prompts_dir(tmp_path)

    versions = list_prompt_versions("reviewer")
    assert versions == [1, 2, 3]


def test_get_latest_version(tmp_path: Path):
    role_dir = tmp_path / "implementer"
    role_dir.mkdir()
    (role_dir / "v1.md").write_text("v1")
    (role_dir / "v5.md").write_text("v5")

    set_prompts_dir(tmp_path)

    assert get_latest_version("implementer") == 5


def test_list_versions_empty(tmp_path: Path):
    set_prompts_dir(tmp_path)
    assert list_prompt_versions("nonexistent") == []


def test_load_prompt_missing_raises(tmp_path: Path):
    set_prompts_dir(tmp_path)

    with pytest.raises(FileNotFoundError):
        load_prompt("nonexistent", version=99)


# --- Repository prompts, read through the loader --------------------------------

REPO_PROMPTS = Path(__file__).resolve().parents[2] / "prompts"


@pytest.fixture(autouse=True)
def _repo_prompts_dir():
    """Each test starts and ends with the loader on the repository's prompts/."""
    set_prompts_dir(REPO_PROMPTS)
    yield
    set_prompts_dir(REPO_PROMPTS)


def _repo_prompt(role: str, version: int) -> str:
    return load_prompt(role, version=version)


def _implementer_headings() -> list[str]:
    """Every ``## `` heading ImplementerAgent can render (each input key set)."""
    agent = ImplementerAgent(provider=MagicMock(spec=BaseProvider), system_prompt="")
    message = agent._build_user_message(
        {
            "request": "r",
            "task": {"task_id": "t1"},
            "plan_summary": "p",
            "project_info": {"languages": ["python"]},
            "framework_hints": ["h"],
            "failing_tests": "E   AssertionError",
            "test_counts": {"passed": False, "total": 1, "failures": 1},
            "baseline_failures": ["tests/t.py::a"],
            "flaky_tests": ["tests/t.py::b"],
            "removed_tests": [{"file": "tests/t.py", "removed": ["c"]}],
            "existing_files": {"a.py": "x"},
            "omitted_files": {"big.py": 1},
            "binary_files": {"logo.png": 1},
            "repo_map": "a.py",
            "previous_attempt": {"no_changes": True},
            "director_guidance": [{"title": "x"}],
            "security_findings": [{"title": "x"}],
            "import_warnings": [{"title": "x"}],
            "review_feedback": [{"title": "x"}],
            "constraints": {"no_modify": ["x"]},
        }
    )
    return [line[3:] for line in message.splitlines() if line.startswith("## ")]


def _reviewer_headings() -> list[str]:
    """Every ``## `` heading ReviewerAgent can render."""
    agent = ReviewerAgent(provider=MagicMock(spec=BaseProvider), system_prompt="")
    data: dict[str, Any] = {
        "request": "r",
        "task": {"task_id": "t1"},
        "later_tasks": ["t2"],
        "acceptance": {"criteria": ["c"]},
        "constraints": {"no_modify": ["x"]},
        "diff": "d",
        "changed_files": {"a.py": "x"},
        "related_files": {"b.py": "y"},
        "test_report": {"passed": False, "total": 1, "failures": 1},
        "baseline_failures": ["tests/t.py::a"],
        "removed_tests": [{"file": "tests/t.py", "removed": ["c"]}],
        "security_findings": [{"title": "x"}],
        "import_warnings": [{"title": "x"}],
        "previous_issues": [{"title": "x"}],
    }
    messages = [agent._build_user_message(data), agent._build_user_message({"patch_noop": True})]
    return list(
        dict.fromkeys(
            line[3:]
            for message in messages
            for line in message.splitlines()
            if line.startswith("## ")
        )
    )


@pytest.mark.parametrize("role", ["architect", "implementer", "reviewer"])
def test_v5_prompt_exists_and_is_titled(role: str):
    title = _repo_prompt(role, 5).splitlines()[0]

    assert title.startswith(f"# {role.capitalize()} — System Prompt v5")


def test_implementer_v5_names_every_rendered_heading():
    """Header coupling (#71): every section the implementer can receive is named in v5."""
    prompt = _repo_prompt("implementer", 5)
    headings = _implementer_headings()

    assert len(headings) == 18
    for heading in headings:
        assert f'"{heading}"' in prompt, heading


def test_reviewer_v5_names_every_rendered_heading():
    prompt = _repo_prompt("reviewer", 5)

    for heading in _reviewer_headings():
        base = heading.split(" (Unified Diff)")[0]
        assert f'"{base}"' in prompt, heading


def test_implementer_v5_ranks_failing_tests_above_reviewer_notes():
    """#71: Reviewer Notes are advice; the failing tests win."""
    prompt = _repo_prompt("implementer", 5)

    assert '"Reviewer Notes" are advice' in prompt
    assert 'never follow a note that contradicts "Failing Tests"' in prompt
    assert "change ONLY that side" in prompt
    assert "`AssertionError: 12 != 10` means the code produced 12" in prompt
    assert "Change only code that a failing test or a note points at" in prompt
    fix_cycles = prompt[prompt.index("## On Fix Cycles") :]
    assert "count every print(), loop iteration and pop()" in fix_cycles
    assert "Include ALL files" not in prompt


def test_implementer_v5_flaky_and_noop_rules():
    prompt = _repo_prompt("implementer", 5)

    assert "Tests must be deterministic. Seed or mock randomness" in prompt  # #77 rule 10
    assert "change only the test, and keep the test's name so its node ID stays the same" in prompt
    assert '"Flaky Tests" passed and failed with no code change' in prompt
    assert '"no_changes": true' in prompt
    assert 'its "still_failing" lists the tests that still fail' in prompt  # #73
    assert "and the tests still fail" not in prompt


def test_implementer_v5_documents_edits_for_excerpts():
    """#98: excerpts, unseen and binary files change only through edits."""
    prompt = _repo_prompt("implementer", 5)

    assert '"edits"' in prompt
    assert 'Change such a file only through "edits"' in prompt
    assert "complete rewrite" not in prompt
    assert '"Edit not applied"' in prompt
    output = prompt[prompt.index("## Output") :]
    example = json.loads(output[output.index("{") : output.rindex("}") + 1])
    patch = PatchSet.model_validate(example)
    assert patch.edits[0].path == "path/to/big_module.py"


def test_implementer_v5_documents_paths_deletions_constraints_and_findings():
    prompt = _repo_prompt("implementer", 5)

    assert '"delete_files"' in prompt
    assert 'No leading "/", no "..", no drive letters' in prompt
    assert '"Constraints"' in prompt
    assert '"no_modify"' in prompt
    assert '"Security Findings"' in prompt
    for section in ("Pre-existing Failures", "Files Not Shown", "File not written", "guidelines"):
        assert f'"{section}"' in prompt
    assert '"tests_to_run": ["python -m pytest' in prompt


def test_reviewer_v5_scope_and_evidence_rules():
    """#68: plan-wide criteria, shown code only, one concrete fix, moving targets."""
    prompt = load_prompt("reviewer", 5)

    assert '"fix_target"' in prompt
    assert '"fix_file"' in prompt
    assert '"Later Tasks (out of scope)"' in prompt
    assert "a criterion that belongs to a later task is neither a failure nor met now" in prompt
    assert "Never describe how code behaves unless you were shown that code" in prompt
    assert 'Never write "review", "adjust" or "ensure"' in prompt
    assert "never offer either/or choices" in prompt
    assert "report it as a moving target" in prompt
    assert "nondeterministic" in prompt
    assert "seed the random generator or compare sorted contents" in prompt


def test_reviewer_v5_flaky_tests_by_owner():
    """#77: an owned flake is one test-file issue; an old one never blames the code."""
    prompt = _repo_prompt("reviewer", 5)

    assert 'is one "major" issue on that test file, with "fix_target": "test"' in prompt
    assert "report no issue against the implementation code for it" in prompt


def test_reviewer_v5_entrypoint_rule_is_bounded():
    """#80: no false runs claimed, timeouts and EOFError exempt, only for scripts."""
    prompt = _repo_prompt("reviewer", 5)
    section = prompt[prompt.index("## Entrypoint Runs") : prompt.index("## Severity Guide")]

    assert '"Entrypoint Run"' in section
    assert "Never claim that a script runs or prints something unless" in section
    assert "only when the task or the acceptance criteria say the file is a script" in section
    assert "timed out" in section
    assert "EOFError" in section
    assert "is not a failure" in section
    assert 'whose evidence and "fix_file" are that file' in section
    assert 'if __name__ == "__main__":' in section


def test_reviewer_v5_blocker_rules_agree():
    """#103: the reject reasons are blockers and rule 3 says so too."""
    prompt = _repo_prompt("reviewer", 5)

    assert 'Mark "blocker" only for code that will CRASH, a security hole, or one of the' in prompt
    assert 'Report each of these reasons as a "blocker" issue' in prompt
    assert 'Only mark "blocker" if code will CRASH or has a security hole' not in prompt


def test_reviewer_v5_treats_constraints_and_security_findings_as_evidence():
    prompt = _repo_prompt("reviewer", 5)

    assert '"Constraints"' in prompt
    assert '"Security Findings"' in prompt
    assert '"forbidden_patterns" entry, is a "blocker"' in prompt
    for section in ("Pre-existing Failures", "Removed Tests", "synthetic_tests", "guidelines"):
        assert f'"{section}"' in prompt
    assert "list at least one issue" in prompt


def test_architect_v5_example_is_a_valid_work_plan():
    """#78: the example plan in the prompt validates against WorkPlan."""
    prompt = _repo_prompt("architect", 5)
    output = prompt[prompt.index("## Output") : prompt.index("## Field Requirements")]

    plan = WorkPlan.model_validate(json.loads(output[output.index("{") : output.rindex("}") + 1]))

    assert plan.tasks[0].outputs == ["module.py", "tests/test_module.py"]
    assert '"Completed Tasks"' in prompt


def test_architect_v5_test_layout_follows_the_repository():
    prompt = _repo_prompt("architect", 5)

    assert 'unless "Existing Files" show another test layout' in prompt
    assert "files not shown" in prompt
    assert '"Repository Map"' in prompt
    assert '"Constraints"' in prompt
    assert "Every task_id is unique" in prompt


def test_director_v1_defer_cannot_resolve_a_failing_test():
    """#74: neither ACCEPT nor DEFER settles an issue a failing test confirms."""
    prompt = _repo_prompt("director", 1)

    assert "Neither ACCEPT nor DEFER can resolve an issue that a failing test confirms" in prompt


@pytest.mark.parametrize("preset", sorted(PRESETS))
def test_every_preset_role_loads_its_prompt(preset: str):
    """#78: each preset role resolves to a prompt file the loader can read."""
    config = load_config(preset=preset, config_path=Path("does-not-exist.toml"))
    for role in ("architect", "implementer", "reviewer"):
        version = resolve_prompt_version(getattr(config, role))
        prompt = load_prompt(role, version=version)
        assert f"v{version}" in prompt.splitlines()[0], (preset, role)
    assert load_prompt("director", version=DIRECTOR_PROMPT_VERSION)


# --- v1 (cloud) prompts: neutral documentation of inputs and fields (D6, D7, D12) ------


def test_implementer_v1_carries_fix_only_policy():
    """The policy moved out of ImplementerAgent; cloud presets read it from v1."""
    prompt = _repo_prompt("implementer", 1)

    assert 'Fix ONLY the issues listed under "Reviewer Notes"' in prompt
    assert "Do not refactor unrelated code" in prompt


def test_implementer_v1_documents_deletions_edits_and_excerpts():
    prompt = _repo_prompt("implementer", 1)

    assert "`delete_files` lists files to delete" in prompt
    assert '"edits": [' in prompt
    assert "may only be changed through `edits`, never through `files`" in prompt
    assert "unless the Constraints list `requirements.txt` under `no_modify`" in prompt  # #97
    assert '"Pre-existing Failures" lists tests that were already failing' in prompt  # #104


def test_reviewer_v1_documents_fix_hints_and_inputs():
    prompt = _repo_prompt("reviewer", 1)

    assert '"fix_target": "code|test|architecture"' in prompt  # D7: cloud runs can re-plan
    assert '"architecture" when the plan itself must change' in prompt
    assert '"fix_file"' in prompt
    assert '"Entrypoint Run"' in prompt  # D6
    assert "`synthetic_tests`" in prompt  # #102
    assert "0 tests means no test ran" in prompt
    assert '"Pre-existing Failures"' in prompt  # #104
    assert '"Constraints"' in prompt  # #97


def test_architect_v1_documents_task_ids_completed_tasks_and_constraints():
    prompt = _repo_prompt("architect", 1)

    assert "Every `task_id` is unique" in prompt  # #99
    assert '"Completed Tasks"' in prompt  # #79
    assert "no task may change a `no_modify` path" in prompt  # #97


# The legacy prompt sets stay byte-for-byte as benchmarked (line endings aside).
LEGACY_PROMPT_SHA256 = {
    ("architect", 2): "d453de1e79ad320e1646725ce81fae0a535f4a744c758d65d8d36fdf17981b5f",
    ("architect", 3): "f0560f8a1c0db5324d18d3b3db010da66ac2f5e881f94668374910093859e438",
    ("architect", 4): "789ca68b76972d9a9faefddb48edf5c6dea8cdc1549145ac7d50d6474c588c96",
    ("implementer", 2): "e2fcca448df53c2367048ca9e07f18c1706529a7b910423cbec77ccd0d5ebabb",
    ("implementer", 3): "2a86e27641986c98ade550a2fde8067a5209d724722e6746b9919116ccdaefa6",
    ("implementer", 4): "ea75faac18d6ec9b55e2e2efe3f04abbeff0eab03cb46d6615eec0f142c0b30c",
    ("reviewer", 2): "a3a68e5030df0fb4463c0f1f21c795b3c13cf70aa3c51dfde857aabfc18e201d",
    ("reviewer", 3): "4e0222804cab36bd87566e2db94cc3050c27bbd93aed229b631c1b2b82818e76",
    ("reviewer", 4): "4e0222804cab36bd87566e2db94cc3050c27bbd93aed229b631c1b2b82818e76",
}


@pytest.mark.parametrize(("role", "version"), sorted(LEGACY_PROMPT_SHA256))
def test_legacy_prompts_are_unchanged(role: str, version: int):
    raw = (REPO_PROMPTS / role / f"v{version}.md").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == LEGACY_PROMPT_SHA256[(role, version)]


# --- retry prompts (#107) --------------------------------------------------------------


def test_retry_prompts_have_their_placeholders():
    truncation = load_prompt("truncation_retry", version=1)
    assert "{max_tokens}" in truncation
    assert "{output_tokens}" in truncation
    assert "only the files you create or change" in truncation
    assert "{error}" in load_prompt("validation_retry", version=1)
