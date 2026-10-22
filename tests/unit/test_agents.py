"""Tests for agent message building."""

import pytest

from delegatron.agents.architect import ArchitectAgent, format_file_tree
from delegatron.agents.implementer import ImplementerAgent
from delegatron.agents.providers.base import BaseProvider, ProviderResponse
from delegatron.agents.reviewer import ReviewerAgent
from delegatron.schemas.patch_set import PatchSet
from delegatron.schemas.review_report import ReviewReport
from delegatron.schemas.work_plan import WorkPlan
from delegatron.utils.test_output import format_test_failures


class MockProvider(BaseProvider):
    """Mock provider that returns a configurable response."""

    def __init__(self, response_content: str):
        self._response_content = response_content

    async def generate(
        self,
        system_prompt,
        user_message,
        json_schema=None,
        max_tokens=8192,
        temperature=0.0,
    ):
        return ProviderResponse(
            content=self._response_content,
            model="mock-model",
            input_tokens=100,
            output_tokens=50,
        )


def test_architect_builds_user_message():
    provider = MockProvider("{}")
    agent = ArchitectAgent(provider=provider, system_prompt="test")

    msg = agent._build_user_message(
        {
            "request": "Add a health endpoint",
            "project_info": {"languages": ["python"]},
        }
    )

    assert "Add a health endpoint" in msg
    assert "python" in msg


def test_implementer_builds_user_message():
    provider = MockProvider("{}")
    agent = ImplementerAgent(provider=provider, system_prompt="test")

    msg = agent._build_user_message(
        {
            "task": {"task_id": "t1", "title": "Add endpoint"},
            "plan_summary": "Build a REST API",
        }
    )

    assert "t1" in msg
    assert "Build a REST API" in msg


def test_reviewer_builds_user_message():
    provider = MockProvider("{}")
    agent = ReviewerAgent(provider=provider, system_prompt="test")

    msg = agent._build_user_message(
        {
            "request": "Add health endpoint",
            "diff": "--- a/x.py\n+++ b/x.py",
            "iteration": 7,
            "task_attempt": 2,
            "previous_issues": [{"severity": "major", "title": "No test"}],
        }
    )

    assert "health endpoint" in msg
    assert "--- a/x.py" in msg
    assert "attempt 2 of the current task" in msg
    # never the global iteration number (the old "This is iteration 7 ..." wording)
    assert "iteration 7" not in msg.lower()
    assert "attempt 7" not in msg


def test_architect_output_schema():
    provider = MockProvider("{}")
    agent = ArchitectAgent(provider=provider, system_prompt="test")
    schema = agent._get_output_schema()
    assert schema["title"] == "WorkPlan"
    assert "tasks" in schema["properties"]


def test_implementer_output_schema():
    provider = MockProvider("{}")
    agent = ImplementerAgent(provider=provider, system_prompt="test")
    schema = agent._get_output_schema()
    assert schema["title"] == "PatchSet"
    assert "diff_unified" in schema["properties"]


def test_reviewer_output_schema():
    provider = MockProvider("{}")
    agent = ReviewerAgent(provider=provider, system_prompt="test")
    schema = agent._get_output_schema()
    assert schema["title"] == "ReviewReport"
    assert "issues" in schema["properties"]


@pytest.mark.asyncio
async def test_architect_invoke_with_mock():
    """Test full invoke with a mock provider returning valid WorkPlan JSON."""
    plan = WorkPlan(
        summary="Add health endpoint",
        tasks=[
            {
                "task_id": "t1",
                "title": "Create /health",
                "description": "Add GET /health",
                "outputs": ["app/health.py"],
                "risk_level": "low",
            }
        ],
        acceptance={"criteria": ["Returns 200"], "tests": ["test_health"]},
        termination={
            "done_when": ["Tests pass"],
            "stop_limits": {
                "max_iterations": 5,
                "max_cost_usd": 5.0,
                "max_wall_time_minutes": 30,
            },
        },
    )

    provider = MockProvider(plan.model_dump_json())
    agent = ArchitectAgent(provider=provider, system_prompt="test")

    result = await agent.invoke({"request": "Add a health endpoint"})

    assert result["summary"] == "Add health endpoint"
    assert len(result["tasks"]) == 1
    assert "_meta" in result
    assert result["_meta"]["model"] == "mock-model"


@pytest.mark.asyncio
async def test_implementer_invoke_with_mock():
    """Test full invoke with a mock provider returning valid PatchSet JSON."""
    patch = PatchSet(
        status="ok",
        diff_unified="--- a/x.py\n+++ b/x.py\n@@ +1 @@\n+pass",
        rationale="Added placeholder",
        tests_to_run=["pytest"],
    )

    provider = MockProvider(patch.model_dump_json())
    agent = ImplementerAgent(provider=provider, system_prompt="test")

    result = await agent.invoke({"task": {"task_id": "t1", "title": "test"}})

    assert result["status"] == "ok"
    assert "diff_unified" in result


@pytest.mark.asyncio
async def test_reviewer_invoke_with_mock():
    """Test full invoke with a mock provider returning valid ReviewReport JSON."""
    report = ReviewReport(
        summary="Code looks good",
        acceptance_met=True,
        issues=[],
    )

    provider = MockProvider(report.model_dump_json())
    agent = ReviewerAgent(provider=provider, system_prompt="test")

    result = await agent.invoke({"request": "test", "diff": "test diff"})

    assert result["acceptance_met"] is True
    assert result["issues"] == []


# --- Agent input rendering (implementer C7, reviewer C8, architect re-plan) ---

PYTEST_FAILURE = """\
============================= test session starts ==============================
collected 2 items

tests/test_deal.py::test_deal_size FAILED                                [ 50%]
tests/test_deal.py::test_shuffle PASSED                                  [100%]

=================================== FAILURES ===================================
________________________________ test_deal_size ________________________________

    def test_deal_size():
>       assert len(deal("Pikachu")) == 10
E       AssertionError: assert 12 == 10

tests/test_deal.py:5: AssertionError
=========================== short test summary info ============================
FAILED tests/test_deal.py::test_deal_size - AssertionError: assert 12 == 10
========================= 1 failed, 1 passed in 0.05s ==========================
"""

TRACEBACK_LINES = (
    '>       assert len(deal("Pikachu")) == 10\nE       AssertionError: assert 12 == 10'
)


def _implementer_message(input_data: dict) -> str:
    agent = ImplementerAgent(provider=MockProvider("{}"), system_prompt="test")
    return agent._build_user_message(input_data)


def _reviewer_message(input_data: dict) -> str:
    agent = ReviewerAgent(provider=MockProvider("{}"), system_prompt="test")
    return agent._build_user_message(input_data)


def _headings(message: str) -> list[str]:
    return [line[3:] for line in message.splitlines() if line.startswith("## ")]


def test_implementer_sections_follow_contract_order():
    msg = _implementer_message(
        {
            "constraints": {"max_files": 3},
            "review_feedback": [{"severity": "major", "title": "Deal size wrong"}],
            "import_warnings": [{"file": "deal.py", "title": "Unresolvable import: cards"}],
            "security_findings": [{"severity": "major", "title": "Hard-coded key"}],
            "director_guidance": [{"title": "Deal size", "decision": "REVISE"}],
            "previous_attempt": {"no_changes": True, "files": ["deal.py"], "still_failing": []},
            "repo_map": "deal.py:\n  def deal(name)",
            "existing_files": {"deal.py": "def deal(name):\n    return []\n"},
            "removed_tests": [{"file": "tests/test_deal.py", "removed": ["test_a", "test_b"]}],
            "flaky_tests": ["tests/test_deal.py::test_shuffle"],
            "failing_tests": "E   AssertionError",
            "framework_hints": ["Use pytest"],
            "project_info": {"languages": ["python"]},
            "plan_summary": "Deal cards",
            "task": {"task_id": "t2", "title": "Deal"},
            "request": "Simulate a first deal",
        }
    )

    assert _headings(msg) == [
        "Original Request",
        "Task Specification",
        "Plan Summary",
        "Project Context",
        "Framework Hints",
        "Failing Tests",
        "Flaky Tests",
        "Removed Tests",
        "Current Files",
        "Repository Map",
        "Previous Attempt",
        "Director Guidance",
        "Security Findings",
        "Import Warnings (advisory)",
        "Reviewer Notes",
        "Constraints",
    ]
    assert "- tests/test_deal.py: test_a, test_b" in msg
    assert '"no_changes": true' in msg


def test_implementer_renders_repo_map_and_current_files():
    msg = _implementer_message(
        {
            "task": {"task_id": "t3"},
            "existing_files": {"card_deck.py": "class Deck:\n    pass\n"},
            "repo_map": "card_deck.py:\n  class Deck\n    def shuffle(self)",
        }
    )

    assert "## Current Files\n\n### card_deck.py\n```\nclass Deck:\n    pass\n```" in msg
    assert (
        "## Repository Map\n\n```\ncard_deck.py:\n  class Deck\n    def shuffle(self)\n```" in msg
    )


def test_implementer_failing_tests_are_not_json_escaped():
    msg = _implementer_message({"task": {"task_id": "t1"}, "failing_tests": PYTEST_FAILURE})

    assert TRACEBACK_LINES in msg
    assert "\\n" not in msg
    assert '\\"' not in msg


def test_implementer_falls_back_to_failed_test_report():
    report = {"passed": False, "total": 2, "failures": 1, "stdout": PYTEST_FAILURE, "stderr": ""}

    msg = _implementer_message({"task": {"task_id": "t1"}, "test_report": report})

    assert "## Failing Tests" in msg
    assert TRACEBACK_LINES in msg
    assert '"stdout"' not in msg
    assert "test session starts" not in msg  # excerpt starts at the FAILURES banner


def test_implementer_prefers_failing_tests_over_test_report():
    report = {"passed": False, "stdout": PYTEST_FAILURE}

    msg = _implementer_message({"failing_tests": "", "test_report": report})

    assert "## Failing Tests" not in msg


def test_implementer_skips_passing_test_report():
    msg = _implementer_message({"test_report": {"passed": True, "stdout": "1 passed"}})

    assert "## Failing Tests" not in msg
    assert "1 passed" not in msg


def test_implementer_has_no_hardcoded_fix_policy():
    msg = _implementer_message(
        {"review_feedback": [{"severity": "major", "title": "Deal size wrong"}]}
    )

    assert "## Reviewer Notes" in msg
    assert "Fix ONLY" not in msg


def test_implementer_fence_survives_backticks_in_files():
    readme = "# Usage\n\n```bash\npython deal.py\n```\n"

    msg = _implementer_message({"existing_files": {"README.md": readme}})

    assert "### README.md\n````\n# Usage\n\n```bash\npython deal.py\n```\n````" in msg


def test_reviewer_renders_task_diff_and_changed_files():
    msg = _reviewer_message(
        {
            "request": "Simulate a first deal",
            "task": {"task_id": "t2", "title": "Deal seven cards"},
            "later_tasks": ["Add a CLI"],
            "acceptance": {"criteria": ["Deal 7 cards"]},
            "diff": "--- a/deal.py\n+++ b/deal.py\n@@ -1 +1 @@\n-HAND = 6\n+HAND = 7",
            "changed_files": {"deal.py": "HAND = 7\n"},
            "related_files": {"cards.py": "CARDS = []\n"},
        }
    )

    assert _headings(msg) == [
        "Original Request",
        "Current Task",
        "Later Tasks (out of scope)",
        "Overall Acceptance Criteria (whole plan)",
        "Code Changes (Unified Diff)",
        "Changed Files",
        "Related Files (unchanged)",
    ]
    assert '"title": "Deal seven cards"' in msg
    assert "- Add a CLI" in msg
    assert "```diff\n--- a/deal.py\n+++ b/deal.py\n@@ -1 +1 @@\n-HAND = 6\n+HAND = 7\n```" in msg
    assert "### deal.py\n```\nHAND = 7\n```" in msg
    assert "### cards.py\n```\nCARDS = []\n```" in msg


def test_reviewer_iteration_context_requires_previous_issues():
    without = _reviewer_message({"request": "x", "iteration": 3, "task_attempt": 2})
    with_issues = _reviewer_message(
        {
            "request": "x",
            "iteration": 3,
            "task_attempt": 2,
            "previous_issues": [{"severity": "major", "title": "Deal size wrong"}],
        }
    )

    assert "Iteration Context" not in without
    assert "iteration" not in without.lower()
    assert "## Iteration Context" in with_issues
    assert "This is attempt 2 of the current task." in with_issues
    assert '"title": "Deal size wrong"' in with_issues


def test_reviewer_never_shows_the_global_iteration():
    """Without a task attempt number the context names no number at all (#68)."""
    msg = _reviewer_message(
        {"iteration": 5, "previous_issues": [{"severity": "major", "title": "Deal size wrong"}]}
    )

    assert "## Iteration Context\n\nIssues from the previous review:" in msg
    assert "iteration 5" not in msg.lower()
    assert "attempt 5" not in msg


def test_reviewer_renders_test_report_as_text():
    report = {
        "passed": False,
        "total": 2,
        "failures": 1,
        "stdout": "truncated tail",
        "stderr": "",
        "failure_excerpt": format_test_failures(PYTEST_FAILURE),
        "flaky_tests": ["tests/test_deal.py::test_shuffle"],
    }

    msg = _reviewer_message({"test_report": report})

    assert "## Test Results\n\nResult: FAILED (1 failed of 2)" in msg
    assert (
        "Flaky tests (passed and failed with no code change):\n- tests/test_deal.py::test_shuffle"
        in msg
    )
    assert "### Test Output\n\n```text\n=====" in msg
    assert TRACEBACK_LINES in msg
    assert "truncated tail" not in msg
    assert '"passed"' not in msg
    assert "\\n" not in msg


def test_reviewer_test_report_falls_back_to_output():
    report = {
        "passed": True,
        "total": 0,
        "failures": 0,
        "stdout": "",
        "stderr": "Sandbox skipped: powershell cannot be tested",
        "skipped_reason": "non_testable_language",
    }

    msg = _reviewer_message({"test_report": report})

    assert "Result: SKIPPED (no tests ran)" in msg  # neither PASSED nor FAILED
    assert "0 failed of 0" not in msg
    assert "skipped_reason: non_testable_language" in msg
    assert "Sandbox skipped: powershell cannot be tested" in msg


def test_reviewer_renders_entrypoint_runs():
    report = {
        "passed": True,
        "total": 1,
        "failures": 0,
        "entrypoint_runs": [
            {"file": "deal.py", "returncode": 0, "stdout_head": "", "stderr_tail": ""},
            {
                "file": "loop.py",
                "returncode": None,
                "timed_out": True,
                "stderr_tail": "KeyboardInt",
            },
            {"file": "cli.py", "returncode": 1, "stdout_head": "Dealing...\n"},
        ],
    }

    msg = _reviewer_message({"test_report": report})

    assert "### Entrypoint Run: python deal.py\n\nStatus: exit code 0\n\nstdout: (empty)" in msg
    assert "### Entrypoint Run: python loop.py\n\nStatus: timed out" in msg
    assert "stderr:\n```text\nKeyboardInt\n```" in msg
    assert "Status: exit code 1\n\nstdout:\n```text\nDealing...\n```" in msg


def test_reviewer_reports_noop_patch():
    msg = _reviewer_message({"diff": "", "patch_noop": True, "changed_files": {}})

    assert "## Code Changes\n\nThis attempt changed no files." in msg
    assert "Unified Diff" not in msg
    assert "## Changed Files" not in msg


def test_reviewer_renders_import_warnings_as_advisory():
    msg = _reviewer_message(
        {"import_warnings": [{"file": "deal.py", "title": "Unresolvable import: cards"}]}
    )

    assert "## Import Warnings (advisory)" in msg
    assert '"title": "Unresolvable import: cards"' in msg


@pytest.mark.asyncio
async def test_reviewer_trims_whole_files_before_the_diff():
    report = ReviewReport(summary="ok", acceptance_met=True)
    agent = ReviewerAgent(
        provider=MockProvider(report.model_dump_json()),
        system_prompt="test",
        context_window=4096,
    )
    big_file = "VALUE = 1\n" * 2000

    result = await agent.invoke(
        {
            "diff": "--- a/deal.py\n+++ b/deal.py",
            "changed_files": {"deal.py": big_file},
            "related_files": {"cards.py": big_file},
        }
    )

    trim = result["_meta"]["context_trimmed"]
    assert trim["keys"] == ["related_files", "changed_files"]
    assert trim["dropped"] == {"related_files": ["cards.py"], "changed_files": ["deal.py"]}


@pytest.mark.asyncio
async def test_implementer_lists_files_the_guard_dropped_as_not_shown():
    # A dropped file still exists: the model must know not to recreate it blind.
    sent: list[str] = []

    class _Recording(MockProvider):
        async def generate(self, system_prompt, user_message, *args, **kwargs):
            sent.append(user_message)
            return await super().generate(system_prompt, user_message, *args, **kwargs)

    patch = PatchSet(files={"deal.py": "x = 1\n"}, rationale="r")
    agent = ImplementerAgent(
        provider=_Recording(patch.model_dump_json()), system_prompt="test", context_window=4096
    )
    big_file = "VALUE = 1\n" * 2000

    result = await agent.invoke(
        {
            "request": "deal",
            "task": {"task_id": "t1", "outputs": ["deal.py"]},
            "existing_files": {"deal.py": "x = 0\n", "cards.py": big_file},
        }
    )

    assert result["_meta"]["context_trimmed"]["dropped"] == {"existing_files": ["cards.py"]}
    [message] = sent
    assert "## Files Not Shown" in message
    assert f"- cards.py ({len(big_file)} bytes)" in message
    assert "### deal.py" in message


def test_implementer_marks_excerpted_files_as_partial():
    agent = ImplementerAgent(provider=MockProvider("{}"), system_prompt="test")
    excerpt = "head\n... [truncated 900 bytes] ...\ntail\n"

    msg = agent._build_user_message(
        {
            "existing_files": {"big.py": excerpt, "small.py": "x = 1\n"},
            "partial_files": {"big.py": 920},
        }
    )

    assert f"### big.py (PARTIAL: {len(excerpt)} of 920 bytes shown)" in msg
    assert "### small.py\n" in msg


def test_architect_renders_completed_tasks_for_replanning():
    agent = ArchitectAgent(provider=MockProvider("{}"), system_prompt="test")

    msg = agent._build_user_message(
        {
            "request": "Simulate a first deal",
            "completed_tasks": [{"task_id": "t1", "title": "Card deck", "files": ["deck.py"]}],
        }
    )

    assert "## Completed Tasks\n\n```json" in msg
    assert "keep unchanged" not in msg  # the prompts say what to do with them (#79)
    assert '"task_id": "t1"' in msg
    assert "Completed Tasks" not in agent._build_user_message({"request": "x"})


def test_architect_renders_skipped_tasks_for_replanning():
    agent = ArchitectAgent(provider=MockProvider("{}"), system_prompt="test")
    skipped = {"task_id": "t1", "title": "Card deck", "outputs": ["deck.py"], "status": "stalled"}

    msg = agent._build_user_message(
        {
            "request": "deal",
            "completed_tasks": [{"task_id": "t2", "title": "Deal"}],
            "skipped_tasks": [skipped],
            "current_task": {"task_id": "t3", "title": "CLI"},
        }
    )

    assert "## Skipped Tasks\n\n```json" in msg
    assert '"status": "stalled"' in msg
    assert msg.index("## Completed Tasks") < msg.index("## Skipped Tasks")
    assert msg.index("## Skipped Tasks") < msg.index("## Current Task")
    assert "Skipped Tasks" not in agent._build_user_message({"request": "x"})


def test_architect_renders_the_current_task_for_replanning():
    agent = ArchitectAgent(provider=MockProvider("{}"), system_prompt="test")

    msg = agent._build_user_message(
        {"request": "deal", "current_task": {"task_id": "t2", "title": "Deal five cards"}}
    )

    assert "## Current Task\n\n```json" in msg
    assert '"task_id": "t2"' in msg
    assert "Current Task" not in agent._build_user_message({"request": "x"})


def test_reviewer_renders_constraints_and_security_findings_in_evidence_order():
    msg = _reviewer_message(
        {
            "request": "Add a client wrapper",
            "acceptance": {"criteria": ["Wraps the client"]},
            "constraints": {"no_modify": [".env"], "forbidden_patterns": ["shell=True"]},
            "diff": "--- a/client.py\n+++ b/client.py",
            "test_report": {"passed": True, "total": 1, "failures": 0},
            "security_findings": [
                {
                    "severity": "blocker",
                    "title": "aws_access_key",
                    "details": "AKIA... literal",
                    "file": "client.py",
                    "line": 3,
                    "source": "secrets",
                }
            ],
            "import_warnings": [{"file": "client.py", "title": "Unresolvable import: boto3"}],
        }
    )

    assert _headings(msg) == [
        "Original Request",
        "Overall Acceptance Criteria (whole plan)",
        "Constraints",
        "Code Changes (Unified Diff)",
        "Test Results",
        "Security Findings",
        "Import Warnings (advisory)",
    ]
    assert '"no_modify": [\n    ".env"\n  ]' in msg
    assert '"file": "client.py"' in msg


def test_reviewer_omits_empty_constraints_and_findings():
    msg = _reviewer_message({"request": "x", "constraints": {}, "security_findings": []})

    assert "Constraints" not in msg
    assert "Security Findings" not in msg


def test_architect_renders_repo_map_only_when_present():
    agent = ArchitectAgent(provider=MockProvider("{}"), system_prompt="test")

    with_map = agent._build_user_message(
        {"request": "Fix retry", "repo_map": "src/retry.py:\n  def retry_with_backoff(fn)"}
    )
    empty = agent._build_user_message({"request": "Fix retry", "repo_map": ""})

    assert "## Repository Map\n\n```\nsrc/retry.py:\n  def retry_with_backoff(fn)\n```" in with_map
    assert "Repository Map" not in empty
    assert "Repository Map" not in agent._build_user_message({"request": "Fix retry"})


# ---------------------------------------------------------------------------
# Architect file tree (#124)
# ---------------------------------------------------------------------------


def _repo_like_tree() -> list[str]:
    """335 sorted paths laid out like this repository (git ls-files order)."""
    paths = [f"README{i}.md" for i in range(3)]
    paths += [f"dashboard/src/components/c{i:02}.tsx" for i in range(35)]
    paths += [f"docs/guide{i:02}.md" for i in range(16)]
    paths += [f"scripts/setup{i:02}.sh" for i in range(13)]
    paths += [f"src/delegatron/agents/a{i:02}.py" for i in range(40)]
    paths += [f"src/delegatron/core{i:02}.py" for i in range(60)]
    paths += [f"src/delegatron/server/s{i:02}.py" for i in range(10)]
    paths += [f"src/delegatron/utils/u{i:02}.py" for i in range(11)]
    paths.append("src/delegatron/utils/retry.py")
    paths += [f"templates/t{i:02}.json" for i in range(30)]
    paths += [f"tests/integration/test_i{i}.py" for i in range(8)]
    paths += [f"tests/unit/test_u{i:02}.py" for i in range(72)]
    paths.append("tests/unit/test_retry.py")
    paths += [f"zz/extra{i:02}.txt" for i in range(335 - len(paths))]
    return sorted(paths)


def test_small_file_tree_is_listed_whole_with_posix_paths():
    tree = ["b.py", "src\\a.py", "tests/test_a.py"]

    assert format_file_tree(tree) == "b.py\nsrc/a.py\ntests/test_a.py"


def test_long_file_tree_keeps_request_matches_and_counts_the_rest():
    tree = _repo_like_tree()

    text = format_file_tree(tree, "Make the retry helper's maximum delay configurable")
    lines = text.splitlines()

    assert len(tree) == 335
    assert len(lines) == 201
    assert "src/delegatron/utils/retry.py" in lines
    assert "tests/unit/test_retry.py" in lines
    assert lines[-1].startswith("... 135 more files not shown: ")
    shown = lines[:-1]
    assert shown == [p for p in tree if p in set(shown)]  # original order kept


def test_long_file_tree_represents_every_source_directory():
    tree = _repo_like_tree()
    source_dirs = {p.rsplit("/", 1)[0] for p in tree if p.endswith((".py", ".tsx"))}

    shown = format_file_tree(tree).splitlines()[:-1]

    assert {p.rsplit("/", 1)[0] for p in shown if "/" in p} >= source_dirs
    assert {p for p in tree if "/" not in p} <= set(shown)


def test_architect_message_uses_the_ranked_tree():
    agent = ArchitectAgent(provider=MockProvider("{}"), system_prompt="test")

    msg = agent._build_user_message(
        {
            "request": "make the retry helper's max delay configurable",
            "file_tree": _repo_like_tree(),
        }
    )

    assert "src/delegatron/utils/retry.py" in msg
    assert "tests/unit/test_retry.py" in msg
    assert "... 135 more files not shown" in msg


def test_omitted_summary_names_directories_by_count():
    tree = [f"assets/a{i}.png" for i in range(3)] + [f"docs/d{i}.md" for i in range(5)]

    text = format_file_tree(tree, max_entries=2)

    assert text.splitlines()[-1] == "... 6 more files not shown: docs/ (5), assets/ (1)"


def test_omitted_summary_caps_the_directory_list():
    tree = [f"d{i:02}/f{j}.md" for i in range(25) for j in range(2)]

    text = format_file_tree(tree, max_entries=1)

    assert text.splitlines()[-1].endswith(", +5 more dirs")


# --- Evidence the orchestrator sends that both agents render ------------------------


def test_implementer_renders_pre_existing_failures_and_files_not_shown():
    msg = _implementer_message(
        {
            "failing_tests": "E   AssertionError",
            "baseline_failures": ["tests/test_old.py::test_x"],
            "existing_files": {"deal.py": "x = 1\n"},
            "omitted_files": {"engine.py": 80000},
            "binary_files": {"assets/logo.png": 18008},
            "repo_map": "deal.py:\n  x",
        }
    )

    assert _headings(msg) == [
        "Failing Tests",
        "Pre-existing Failures",
        "Current Files",
        "Files Not Shown",
        "Repository Map",
    ]
    assert "- tests/test_old.py::test_x" in msg
    assert "- engine.py (80000 bytes)\n- assets/logo.png (18008 bytes, binary)" in msg


def test_reviewer_renders_removed_tests_and_pre_existing_failures():
    msg = _reviewer_message(
        {
            "test_report": {"passed": False, "total": 2, "failures": 1},
            "baseline_failures": ["tests/test_old.py::test_x"],
            "removed_tests": [{"file": "tests/test_deal.py", "removed": ["test_a"]}],
        }
    )

    assert _headings(msg) == [
        "Test Results",
        "Pre-existing Failures",
        "Removed Tests",
    ]
    assert "- tests/test_old.py::test_x" in msg
    assert "- tests/test_deal.py: test_a" in msg


def test_architect_review_feedback_carries_no_policy_prose():
    """How to re-plan is the prompt's job; the message only carries the issues."""
    agent = ArchitectAgent(provider=MockProvider("{}"), system_prompt="test")
    issue = {"severity": "major", "title": "Wrong module split", "fix_target": "architecture"}

    msg = agent._build_user_message({"request": "x", "review_feedback": [issue]})

    assert "## Review Feedback (Architectural Issues)\n\n```json" in msg
    assert "Wrong module split" in msg
    assert "Update your WorkPlan" not in msg


def test_reviewer_test_report_shows_targeted_run_lint_and_synthetic_tests():
    report = {
        "passed": True,
        "total": 2,
        "failures": 0,
        "synthetic_tests": True,
        "targeted": {"passed": False, "failing_ids": ["tests/test_a.py::test_x"]},
        "lint_warnings": ["[lint] a.ps1: [PSAvoidUsingWriteHost] use Write-Output"],
    }

    msg = _reviewer_message({"test_report": report})

    assert "synthetic_tests: only the orchestrator's smoke tests ran" in msg
    assert "Targeted tests (this task's tests_to_run): FAILED" in msg
    assert "Targeted failures: tests/test_a.py::test_x" in msg
    assert "Lint warnings (advisory):\n- [lint] a.ps1: [PSAvoidUsingWriteHost]" in msg


# --- #67: an empty repository map renders no section ----------------------------------


@pytest.mark.parametrize("repo_map", [None, ""])
def test_implementer_omits_empty_repo_map(repo_map):
    msg = _implementer_message(
        {"task": {"task_id": "t1"}, "existing_files": {"a.py": "x = 1\n"}, "repo_map": repo_map}
    )

    assert "Repository Map" not in msg


# --- #71: the it7 evidence, verbatim and ranked ----------------------------------------

# test_report_006 of run 6f29441bbe8c, unittest-style assertion
IT7_STDOUT = """\
=================================== FAILURES ===================================
_____________________ TestSimulateDeal.test_simulate_deal ______________________

self = <test_simulate_deal.TestSimulateDeal testMethod=test_simulate_deal>

    def test_simulate_deal(self):
        with mock.patch('simulate_deal.print') as mock_print:
            simulate_deal()
            calls = mock_print.call_args_list
>           self.assertEqual(len(calls), 10)  # 5 cards for Player 1, 5 cards for Player 2, and 2 newlines
            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E           AssertionError: 12 != 10

tests/test_simulate_deal.py:33: AssertionError
=========================== short test summary info ============================
FAILED tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal - As...
========================= 1 failed, 6 passed in 0.21s ==========================
"""  # noqa: E501

# review_report_006's issue: vague advice that blames the (correct) code
REVIEW_006_ISSUE = {
    "severity": "major",
    "title": "Incorrect number of print calls in deal simulation",
    "details": "The assertion checks for 10 print calls, but there are actually 12.",
    "recommended_fix": (
        "Review and adjust the logic in the `simulate_deal` function to ensure that only "
        "the necessary information is printed."
    ),
    "evidence": "tests/test_simulate_deal.py:33",
}


def test_it7_failing_tests_are_verbatim_and_rank_above_reviewer_notes():
    msg = _implementer_message(
        {
            "task": {"task_id": "t3", "title": "Print each player's hand"},
            "failing_tests": IT7_STDOUT,
            "test_counts": {"passed": False, "total": 7, "failures": 1},
            "review_feedback": [REVIEW_006_ISSUE],
        }
    )

    assert "\nE           AssertionError: 12 != 10\n" in msg
    failing = msg[msg.index("## Failing Tests") : msg.index("## Reviewer Notes")]
    assert "\\n" not in failing
    assert failing.startswith("## Failing Tests\n\nResult: FAILED (1 failed of 7)\n\n```text\n")
    assert msg.index("## Failing Tests") < msg.index("## Reviewer Notes")
    assert "Review and adjust the logic" in msg[msg.index("## Reviewer Notes") :]


def test_failing_tests_counts_come_from_the_report_too():
    report = {"passed": False, "total": 2, "failures": 1, "stdout": PYTEST_FAILURE, "stderr": ""}

    msg = _implementer_message({"test_report": report})

    assert "## Failing Tests\n\nResult: FAILED (1 failed of 2)\n\n```text" in msg


def test_failing_tests_without_counts_have_no_result_line():
    msg = _implementer_message({"failing_tests": "E   AssertionError"})

    assert "## Failing Tests\n\n```text\nE   AssertionError\n```" in msg


def test_pre_existing_failures_show_their_origin():
    msg = _implementer_message(
        {"baseline_failures": {"tests/test_old.py::test_x": "pre_existing", "tests/t.py::y": "t1"}}
    )

    assert "- tests/test_old.py::test_x (pre_existing)\n- tests/t.py::y (t1)" in msg


# --- #69: the reviewer and implementer keep the assertion of a long traceback ------------


def test_long_traceback_keeps_the_assertion_for_both_agents():
    source = "".join(
        f"    line_{i:02d} = deck.cards[{i} % len(deck.cards)]  # traceback source line\n"
        for i in range(80)
    )
    stdout = IT7_STDOUT.replace(
        ">           self.assertEqual", source + ">           self.assertEqual"
    ).replace(
        "===\n_____",
        "===\n"
        + "_" * 5
        + " TestDeck.test_shuffle "
        + "_" * 5
        + "\n"
        + source
        + "E       AssertionError: [A, B, C] == [A, B, C]\n\n_____",
        1,
    )
    report = {
        "passed": False,
        "total": 7,
        "failures": 2,
        "stdout": stdout[-2000:],
        "failure_excerpt": format_test_failures(stdout),
    }

    reviewer = _reviewer_message({"test_report": report})
    implementer = _implementer_message({"test_report": report})

    for msg in (reviewer, implementer):
        assert "AssertionError: 12 != 10" in msg
        assert "AssertionError: [A, B, C] == [A, B, C]" in msg


# --- #124: the request tier, not the alphabet, keeps a matched file --------------------


def test_long_file_tree_shows_a_request_match_that_sorts_last():
    siblings = [f"pkg/a{i:03}.py" for i in range(250)]
    tree = sorted([*siblings, "pkg/scheduler.py"])

    text = format_file_tree(tree, "Fix the scheduler's timezone handling", max_entries=50)
    lines = text.splitlines()

    assert "pkg/scheduler.py" in lines
    assert "pkg/scheduler.py" not in format_file_tree(tree, "", max_entries=50).splitlines()
    assert lines[-1].startswith("... 201 more files not shown")


def test_reviewer_shows_a_failed_targeted_run_and_who_owned_removed_tests():
    report = {
        "passed": False,
        "total": 3,
        "failures": 1,
        "targeted": {
            "passed": False,
            "failing_ids": ["tests/test_deal.py::test_five"],
            "stdout_tail": "E   AssertionError: 4 != 5",
        },
    }

    msg = _reviewer_message(
        {
            "test_report": report,
            "removed_tests": [
                {"file": "tests/test_deck.py", "removed": ["test_size"], "owner_task": "t1"}
            ],
        }
    )

    assert "### Targeted Test Output\n\n```text\nE   AssertionError: 4 != 5\n```" in msg
    assert "tests/test_deck.py: test_size (accepted in task t1)" in msg
    passing = _reviewer_message({"test_report": {**report, "targeted": {"passed": True}}})
    assert "Targeted Test Output" not in passing
