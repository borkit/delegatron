"""CLI entry point for Delegatron."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from delegatron import __version__
from delegatron.autoselect import select_preset
from delegatron.config import PRESET_DESCRIPTIONS, PRESETS, load_config
from delegatron.templates import RunTemplate, TemplateStore
from delegatron.utils.logging import setup_logging

console = Console()


@click.group()
@click.version_option(version=__version__)
def main() -> None:
    """Delegatron — Autonomous multi-agent coding pipeline."""


@main.command()
@click.argument("request")
@click.option("--max-iterations", default=None, type=int, help="Max review loop iterations.")
@click.option("--max-cost", default=None, type=float, help="Max cost in USD per run.")
@click.option("--max-time", default=None, type=int, help="Max wall-time in minutes.")
@click.option("--sandbox", default=None, type=click.Choice(["docker", "e2b", "local", "none"]))
@click.option(
    "--preset",
    default=None,
    type=click.Choice(list(PRESETS.keys())),
    help="Config preset.",
)
@click.option("--interactive", is_flag=True, help="Approve plan before coding starts.")
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(exists=True),
    help="Path to delegatron.toml.",
)
@click.option(
    "--workspace",
    default=".",
    help="Path to target workspace/repo, or a git URL to clone.",
)
@click.option(
    "--branch",
    default=None,
    help="Branch to clone when --workspace is a git URL (default: the remote's default).",
)
@click.option("--log-file", default=None, type=click.Path(), help="Write JSON logs to file.")
@click.option("--json-output", is_flag=True, help="Output results as JSON.")
@click.option("--mock", is_flag=True, help="Use mock providers (no API keys needed).")
@click.option("--auto", "auto_select", is_flag=True, help="Auto-select preset by complexity.")
@click.option("--stream", is_flag=True, help="Stream LLM token output to stderr in real-time.")
@click.option(
    "--docker-image",
    default=None,
    help="Docker image for sandbox (e.g. node:22-slim).",
)
@click.option(
    "--create-pr",
    is_flag=True,
    help="Open a GitHub PR if the run is approved. Requires `gh` CLI authenticated.",
)
@click.option(
    "--pr-base",
    default=None,
    help="Base branch for the PR (default: main).",
)
@click.option(
    "--pr-draft",
    is_flag=True,
    help="Open the PR as a draft.",
)
@click.option(
    "--allow-dirty",
    is_flag=True,
    help="Start even if the git workspace has uncommitted changes.",
)
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
def run(
    request: str,
    max_iterations: int | None,
    max_cost: float | None,
    max_time: int | None,
    sandbox: str | None,
    preset: str | None,
    interactive: bool,
    config_path: str | None,
    workspace: str,
    branch: str | None,
    mock: bool,
    auto_select: bool,
    stream: bool,
    docker_image: str | None,
    create_pr: bool,
    pr_base: str | None,
    pr_draft: bool,
    log_file: str | None,
    json_output: bool,
    allow_dirty: bool,
    verbose: bool,
) -> None:
    """Run a full plan/code/test/review loop for a given request."""
    workspace_path = _resolve_workspace_arg(workspace, branch, quiet=json_output)
    config_workspace = _config_workspace(workspace, workspace_path)

    # Mock mode overrides preset
    if mock:
        preset = "mock"

    # Auto-select preset when --auto is used (and no explicit preset/mock)
    if auto_select and preset is None:
        from delegatron.detection import detect_project

        project_info = detect_project(workspace_path)

        project_data: dict[str, Any] = {
            "languages": project_info.languages,
            "frameworks": project_info.frameworks,
            "file_count": len(list(workspace_path.rglob("*"))),
        }

        preset, reason = select_preset(request, project_data)
        if not json_output:
            console.print(f"[cyan]Auto-selected preset:[/] [bold]{preset}[/] — {reason}\n")

    # Load config
    config = load_config(
        config_path=Path(config_path) if config_path else None,
        preset=preset,
        workspace=config_workspace,
    )

    # Override with CLI flags
    pipeline_overrides: dict[str, Any] = {}
    if max_iterations is not None:
        pipeline_overrides["max_iterations"] = max_iterations
    if max_cost is not None:
        pipeline_overrides["max_cost_usd"] = max_cost
    if max_time is not None:
        pipeline_overrides["max_wall_time_minutes"] = max_time
    if sandbox is not None:
        pipeline_overrides["sandbox"] = sandbox
    if docker_image is not None:
        pipeline_overrides["docker_image"] = docker_image
    if create_pr:
        pipeline_overrides["create_pr"] = True
    if pr_base is not None:
        pipeline_overrides["pr_base"] = pr_base
    if pr_draft:
        pipeline_overrides["pr_draft"] = True
    if interactive:
        pipeline_overrides["interactive"] = True

    if pipeline_overrides:
        from dataclasses import replace

        config = replace(config, pipeline=replace(config.pipeline, **pipeline_overrides))

    # Setup logging
    log_level = "DEBUG" if verbose else config.pipeline.log_level
    setup_logging(
        level=log_level,
        log_file=Path(log_file) if log_file else None,
        json_output=json_output,
    )

    # Generate run ID
    run_id = uuid.uuid4().hex[:12]

    # Display run info
    if not json_output:
        _display_run_header(request, config, run_id, workspace_path)

    # Interactive: show plan and ask for approval
    if config.pipeline.interactive and not json_output:
        console.print(
            "\n[yellow]Interactive mode:[/] Plan will be shown for approval before coding.\n"
        )

    # Execute pipeline
    from delegatron.orchestrator.graph import Pipeline

    step_cb = _make_stream_callback() if stream and not json_output else None

    pipeline = Pipeline(
        config=config,
        workspace_root=workspace_path,
        run_id=run_id,
        # The run branch forks from --branch, else from the branch checked out.
        base_branch=branch or "",
        step_callback=step_cb,
        allow_dirty=allow_dirty,
    )

    state = asyncio.run(pipeline.execute(request, stream=stream and not json_output))

    # Display results
    if json_output:
        click.echo(json.dumps(state, indent=2, default=str))
    else:
        _display_results(cast(dict[str, Any], state))


@main.command()
@click.argument("request")
@click.option(
    "--preset",
    default=None,
    type=click.Choice(list(PRESETS.keys())),
    help="Config preset.",
)
@click.option("--config", "config_path", default=None, type=click.Path(exists=True))
@click.option("--workspace", default=".", type=click.Path(exists=True))
@click.option("--json-output", is_flag=True)
def plan(
    request: str,
    preset: str | None,
    config_path: str | None,
    workspace: str,
    json_output: bool,
) -> None:
    """Generate an architect plan without executing code."""
    workspace_path = Path(workspace).resolve()
    config = load_config(
        config_path=Path(config_path) if config_path else None,
        preset=preset,
        workspace=workspace_path,
    )

    setup_logging(level=config.pipeline.log_level)

    run_id = uuid.uuid4().hex[:12]

    if not json_output:
        console.print(
            Panel(
                f"[bold]Planning:[/] {request}",
                title="Delegatron",
                subtitle=f"v{__version__}",
            )
        )

    from delegatron.constraints import ConstraintConfigError, merge_constraints
    from delegatron.context import gather_repo_map
    from delegatron.detection import detect_project
    from delegatron.factories.agent_factory import create_architect
    from delegatron.workspace import Workspace

    architect = create_architect(config)

    project_info = detect_project(workspace_path)
    file_tree = Workspace(repo_path=workspace_path, run_id=run_id).get_file_tree()

    # The same architect input `run` builds: repo map and project constraints too.
    input_data: dict[str, Any] = {
        "request": request,
        "project_info": {
            "languages": project_info.languages,
            "frameworks": project_info.frameworks,
            "test_commands": project_info.test_commands,
        },
        "file_tree": file_tree,
    }
    repo_map = gather_repo_map(
        workspace_path,
        task={"description": request},
        file_tree=file_tree,
        languages=project_info.languages,
    )
    if repo_map:
        input_data["repo_map"] = repo_map
    try:
        constraints = merge_constraints({}, config.pipeline.constraints)
    except ConstraintConfigError as e:
        raise click.ClickException(f"Invalid constraints: {e}") from e
    if constraints:
        input_data["constraints"] = constraints

    raw_plan = asyncio.run(architect.invoke(input_data))
    raw_plan.pop("_meta", None)
    # The plan `run` would execute (normalized like Pipeline._run_architect does).
    from delegatron.orchestrator.plan_normalize import normalize_plan

    result, plan_warnings = normalize_plan(raw_plan, file_tree)

    if json_output:
        for warning in plan_warnings:
            click.echo(f"Plan normalized: {warning}", err=True)
        click.echo(json.dumps(result, indent=2))
    else:
        for warning in plan_warnings:
            console.print(f"[yellow]Plan normalized:[/] {warning}")
        _display_plan(result)


@main.command()
@click.option(
    "--workspace",
    default=".",
    help="Path to repo or git URL to review.",
)
@click.option(
    "--branch",
    default=None,
    help="Branch to clone when --workspace is a git URL (default: the remote's default).",
)
@click.option("--focus", default=None, help="Focus area: 'security', 'bugs', 'performance', 'all'.")
@click.option(
    "--preset",
    default=None,
    type=click.Choice(list(PRESETS.keys())),
    help="Config preset (only reviewer model is used).",
)
@click.option("--config", "config_path", default=None, type=click.Path(exists=True))
@click.option("--with-suggestions", is_flag=True, help="Run architect for improvements.")
@click.option(
    "--logs",
    "log_files",
    multiple=True,
    type=click.Path(exists=True),
    help="Log files to analyze instead of source code.",
)
@click.option("--json-output", is_flag=True, help="Output results as JSON.")
@click.option("--mock", is_flag=True, help="Use mock providers.")
@click.option("--verbose", "-v", is_flag=True)
def review(
    workspace: str,
    branch: str | None,
    focus: str | None,
    preset: str | None,
    config_path: str | None,
    with_suggestions: bool,
    log_files: tuple[str, ...],
    json_output: bool,
    mock: bool,
    verbose: bool,
) -> None:
    """Review an existing codebase or log files (read-only). No code changes made.

    Scans the repo and produces a structured report covering bugs,
    security issues, code quality, and improvement suggestions.
    Accepts local paths or git URLs (GitHub, GitLab, Bitbucket, SSH).

    Use --logs to analyze log files instead of source code. The reviewer
    will identify errors, patterns, root causes, and actionable fixes.

    \b
    Examples:
      delegatron review
      delegatron review --workspace /path/to/repo --focus security
      delegatron review --workspace https://github.com/org/repo.git
      delegatron review --with-suggestions --preset local
      delegatron review --logs /var/log/app.log --logs /var/log/error.log
      delegatron review --logs crash.log --focus bugs
    """
    if mock:
        preset = "mock"

    workspace_path = _resolve_workspace_arg(workspace, branch, quiet=json_output)
    config = load_config(
        config_path=Path(config_path) if config_path else None,
        preset=preset,
        workspace=_config_workspace(workspace, workspace_path),
    )

    log_level = "DEBUG" if verbose else config.pipeline.log_level
    setup_logging(level=log_level)

    run_id = f"review-{uuid.uuid4().hex[:8]}"

    if not json_output:
        console.print(
            Panel(
                f"[bold]Reviewing:[/] {workspace_path.name}\n"
                f"[dim]Focus: {focus or 'all'} | Model: {config.reviewer.model}[/]",
                title="Delegatron Code Review",
                subtitle=f"v{__version__}",
            )
        )

    from delegatron.context import gather_review_context
    from delegatron.detection import detect_project
    from delegatron.factories.agent_factory import create_reviewer
    from delegatron.utils.cost import CostTracker
    from delegatron.workspace import Workspace

    reviewer_agent = create_reviewer(config)

    project_info = detect_project(workspace_path)
    file_tree = Workspace(repo_path=workspace_path, run_id=run_id).get_file_tree()
    cost_tracker = CostTracker()

    # Log analysis mode vs source code review
    is_log_mode = len(log_files) > 0
    review_files: dict[str, str] = {}

    if is_log_mode:
        # Read log files
        source_dump = ""
        total_lines = 0
        for lf in log_files:
            log_path = Path(lf)
            content = log_path.read_text(encoding="utf-8", errors="replace")
            # Truncate very large logs to last 2000 lines
            lines = content.split("\n")
            if len(lines) > 2000:
                content = "\n".join(lines[-2000:])
                total_lines += 2000
                line_label = f"{len(lines)} lines, showing last 2000"
            else:
                total_lines += len(lines)
                line_label = f"{len(lines)} lines"
            source_dump += f"\n--- Log: {log_path.name} ({line_label}) ---\n{content}\n"
        files_reviewed = len(log_files)

        if not json_output:
            console.print(
                f"[dim]Analyzing {len(log_files)} log file(s) ({total_lines} lines)...[/]"
            )
    else:
        # The source files, most significant first, sized to the reviewer's
        # context window when it is known; they go in as files the reviewer
        # agent can trim to fit, after the structural repo map.
        review_context = gather_review_context(
            workspace_path,
            file_tree=file_tree,
            languages=project_info.languages,
            max_total_size=_review_budget_bytes(config.reviewer),
        )
        repo_map = review_context.repo_map
        source_dump = f"\n--- Repository Structure ---\n{repo_map}\n" if repo_map else ""
        review_files = dict(review_context.files)
        files_reviewed = len(review_files)

        if not json_output:
            console.print(f"[dim]Scanning {files_reviewed} files...[/]")

    focus_instruction = ""
    if focus and focus != "all":
        from delegatron.context import REVIEW_FOCUS_INSTRUCTIONS

        focus_instruction = REVIEW_FOCUS_INSTRUCTIONS.get(focus, f"Focus on: {focus}")

    if is_log_mode:
        request_text = (
            f"Analyze these log files. Identify: errors, warnings, recurring patterns, "
            f"root causes, and actionable fixes. For each issue found, explain what happened, "
            f"why it happened, and how to fix it. {focus_instruction}"
        )
    else:
        request_text = (
            f"Review this existing codebase for quality, bugs, and security issues. "
            f"This is a READ-ONLY review — do not suggest a rewrite, focus on actionable findings. "
            f"{focus_instruction}\n\n"
            f"Project: {workspace_path.name}\n"
            f"Languages: {', '.join(project_info.languages)}\n"
            f"Frameworks: {', '.join(project_info.frameworks)}\n"
        )

    input_data: dict[str, Any] = {
        "request": request_text,
        "diff": source_dump,
    }
    if review_files:
        input_data["related_files"] = review_files

    result = _invoke_and_record(
        reviewer_agent, input_data, config.reviewer, "reviewer", cost_tracker
    )

    # Optionally run architect for improvement suggestions
    suggestions = None
    if with_suggestions:
        if not json_output:
            console.print("[dim]Running architect for improvement suggestions...[/]")

        from delegatron.factories.agent_factory import create_architect

        architect_agent = create_architect(config)

        issues_text = "\n".join(
            f"- [{i.get('severity', 'info')}] {i.get('title', '')}: {i.get('details', '')}"
            for i in (result.get("issues", []) if isinstance(result.get("issues"), list) else [])
        )

        arch_input: dict[str, Any] = {
            "request": (
                f"Based on a code review of {workspace_path.name}, create an improvement plan. "
                f"The review found these issues:\n{issues_text}\n\n"
                f"Create a prioritized action plan to address these issues."
            ),
            "project_info": {
                "languages": project_info.languages,
                "frameworks": project_info.frameworks,
            },
            "file_tree": file_tree,
        }

        suggestions = _invoke_and_record(
            architect_agent, arch_input, config.architect, "architect", cost_tracker
        )

    if json_output:
        result["_cost"] = cost_tracker.get_summary()
        result["_project"] = {
            "path": str(workspace_path),
            "languages": project_info.languages,
            "frameworks": project_info.frameworks,
            "files_reviewed": files_reviewed,
            "mode": "logs" if is_log_mode else "source",
        }
        if suggestions:
            result["_suggestions"] = suggestions
        click.echo(json.dumps(result, indent=2, default=str))
    else:
        _display_review(result, cost_tracker, files_reviewed, suggestions)


def _display_review(
    result: dict[str, Any],
    cost_tracker: Any,
    files_reviewed: int,
    suggestions: dict[str, Any] | None = None,
) -> None:
    """Display review results in a formatted table."""
    summary = result.get("summary", "No summary")
    accepted = result.get("acceptance_met", False)
    issues = result.get("issues", [])

    console.print()
    console.print(
        Panel(
            summary,
            title="Review Summary",
            border_style="green" if accepted else "red",
        )
    )

    if issues:
        table = Table(title=f"Issues ({len(issues)})")
        table.add_column("Severity", style="bold", width=10)
        table.add_column("Title", width=40)
        table.add_column("Details", width=50)

        severity_styles = {
            "blocker": "red",
            "major": "yellow",
            "minor": "cyan",
            "nit": "dim",
        }

        for issue in issues:
            sev = issue.get("severity", "info") if isinstance(issue, dict) else "info"
            title = issue.get("title", "") if isinstance(issue, dict) else str(issue)
            details = issue.get("details", "") if isinstance(issue, dict) else ""
            style = severity_styles.get(sev, "white")
            table.add_row(f"[{style}]{sev}[/]", title, details[:80])

        console.print(table)

    # Show architect suggestions if available
    if suggestions:
        console.print()
        console.print(
            Panel(
                suggestions.get("summary", "No suggestions"),
                title="Improvement Plan",
                border_style="blue",
            )
        )
        tasks = suggestions.get("tasks", [])
        if tasks:
            s_table = Table(title=f"Suggested Tasks ({len(tasks)})")
            s_table.add_column("Priority", width=8)
            s_table.add_column("Task", width=40)
            s_table.add_column("Description", width=50)
            for i, task in enumerate(tasks, 1):
                if isinstance(task, dict):
                    risk = task.get("risk_level", "low")
                    risk_style = {"high": "red", "medium": "yellow"}.get(risk, "green")
                    s_table.add_row(
                        f"[{risk_style}]{i}[/]",
                        task.get("title", ""),
                        task.get("description", "")[:80],
                    )
            console.print(s_table)

    cost_summary = cost_tracker.get_summary()
    console.print(
        f"\n[dim]Files reviewed: {files_reviewed} | "
        f"Cost: ${cost_summary['total_cost_usd']:.4f} | "
        f"Tokens: {cost_summary['total_input_tokens'] + cost_summary['total_output_tokens']:,}[/]"
    )


@main.command()
@click.argument("run_id")
@click.option(
    "--workspace",
    default=".",
    type=click.Path(exists=True),
    help="Path to target workspace/repo.",
)
@click.option("--preset", default=None, type=click.Choice(list(PRESETS.keys())))
@click.option("--config", "config_path", default=None, type=click.Path(exists=True))
@click.option("--mock", is_flag=True, help="Use mock providers.")
@click.option("--json-output", is_flag=True)
@click.option(
    "--allow-dirty",
    is_flag=True,
    help="Start even if the git workspace has uncommitted changes.",
)
@click.option("--verbose", "-v", is_flag=True)
def resume(
    run_id: str,
    workspace: str,
    preset: str | None,
    config_path: str | None,
    mock: bool,
    json_output: bool,
    allow_dirty: bool,
    verbose: bool,
) -> None:
    """Resume a pipeline from the last checkpoint.

    Loads saved state from runs/<run_id>/ and continues from
    where the pipeline left off.
    """
    if mock:
        preset = "mock"

    config = load_config(
        config_path=Path(config_path) if config_path else None,
        preset=preset,
        workspace=Path(workspace),
    )

    log_level = "DEBUG" if verbose else config.pipeline.log_level
    setup_logging(level=log_level)

    workspace_path = Path(workspace).resolve()
    artifacts_base = workspace_path / "runs"

    from delegatron.artifacts import ArtifactStore

    store = ArtifactStore(base_dir=artifacts_base, run_id=run_id)
    saved_state = store.load_state()

    if saved_state is None:
        console.print(f"[red]No checkpoint found for run '{run_id}'.[/]")
        console.print(f"Looked in: {store.run_dir}")
        raise SystemExit(1)

    if not json_output:
        console.print(
            Panel(
                f"[bold]Resuming run:[/] {run_id}\n"
                f"From iteration: {saved_state.get('iteration', 0)}\n"
                f"Status: {saved_state.get('status', 'unknown')}\n"
                f"Request: {saved_state.get('request', 'N/A')}",
                title=f"Delegatron v{__version__}",
                border_style="yellow",
            )
        )

    from delegatron.orchestrator.graph import Pipeline

    pipeline = Pipeline(
        config=config,
        workspace_root=workspace_path,
        run_id=run_id,
        artifacts_dir=artifacts_base,
        allow_dirty=allow_dirty,
    )

    state = asyncio.run(pipeline.execute(saved_state["request"], initial_state=saved_state))

    if json_output:
        click.echo(json.dumps(state, indent=2, default=str))
    else:
        _display_results(cast(dict[str, Any], state))


@main.command()
@click.argument("run_id")
@click.option(
    "--workspace",
    default=".",
    type=click.Path(exists=True),
    help="Path to target workspace/repo.",
)
@click.option("--preset", default=None, type=click.Choice(list(PRESETS.keys())))
@click.option("--config", "config_path", default=None, type=click.Path(exists=True))
@click.option(
    "--fresh",
    is_flag=True,
    help="Force a clean restart, ignoring review issues.",
)
@click.option("--mock", is_flag=True, help="Use mock providers.")
@click.option("--json-output", is_flag=True)
@click.option("--stream", is_flag=True, help="Stream LLM tokens to stderr.")
@click.option(
    "--allow-dirty",
    is_flag=True,
    help="Start even if the git workspace has uncommitted changes.",
)
@click.option("--verbose", "-v", is_flag=True)
def retry(
    run_id: str,
    workspace: str,
    preset: str | None,
    config_path: str | None,
    fresh: bool,
    mock: bool,
    json_output: bool,
    stream: bool,
    allow_dirty: bool,
    verbose: bool,
) -> None:
    """Smart retry — re-run a completed job, auto-fixing review issues.

    If the previous run has unresolved review issues, appends them to the
    request so the next attempt focuses on fixing them.  Use --fresh to
    force a clean restart that ignores prior review feedback.

    \b
    Examples:
      delegatron retry abc123def456
      delegatron retry abc123def456 --fresh
      delegatron retry abc123def456 --preset budget
    """
    if mock:
        preset = "mock"

    config = load_config(
        config_path=Path(config_path) if config_path else None,
        preset=preset,
        workspace=Path(workspace),
    )

    log_level = "DEBUG" if verbose else config.pipeline.log_level
    setup_logging(level=log_level)

    workspace_path = Path(workspace).resolve()
    artifacts_base = workspace_path / "runs"

    from delegatron.artifacts import ArtifactStore

    store = ArtifactStore(base_dir=artifacts_base, run_id=run_id)
    summary_path = store.run_dir / "summary.json"
    if not summary_path.exists():
        console.print(f"[red]Run '{run_id}' not found.[/]")
        console.print(f"Looked in: {store.run_dir}")
        raise SystemExit(1)

    summary = json.loads(summary_path.read_text())
    request_text = summary.get("request", "")
    if not request_text:
        console.print("[red]Original run has no request text.[/]")
        raise SystemExit(1)

    strategy = "rerun"
    if not fresh:
        # Check for review issues
        review_files = sorted(
            store.run_dir.glob("review_report_*.json"),
            reverse=True,
        )
        if review_files:
            review = json.loads(review_files[0].read_text())
            issues = review.get("issues", [])
            issue_lines = []
            for issue in issues:
                severity = issue.get("severity", "major")
                desc = issue.get(
                    "description",
                    issue.get("title", ""),
                )
                if desc:
                    issue_lines.append(f"- [{severity}] {desc}")
            if issue_lines:
                request_text += (
                    "\n\nFix the following issues from the "
                    "previous review:\n" + "\n".join(issue_lines)
                )
                strategy = "refine"

    new_run_id = uuid.uuid4().hex[:12]

    if not json_output:
        strategy_label = "Fixing review issues" if strategy == "refine" else "Fresh start"
        console.print(
            Panel(
                f"[bold]Retry:[/] {request_text[:120]}\n"
                f"[dim]Strategy: {strategy_label} | "
                f"Original: {run_id}[/]",
                title="Delegatron",
                subtitle=f"v{__version__}",
            )
        )

    from delegatron.orchestrator.graph import Pipeline

    step_cb = _make_stream_callback() if stream and not json_output else None

    pipeline = Pipeline(
        config=config,
        workspace_root=workspace_path,
        run_id=new_run_id,
        base_branch="",
        step_callback=step_cb,
        allow_dirty=allow_dirty,
    )

    state = asyncio.run(
        pipeline.execute(request_text, stream=stream and not json_output),
    )

    if json_output:
        output: dict[str, Any] = dict(state)
        output["strategy"] = strategy
        output["original_run_id"] = run_id
        click.echo(json.dumps(output, indent=2, default=str))
    else:
        _display_results(cast(dict[str, Any], state))


@main.command()
@click.argument("run_id")
@click.option(
    "--workspace",
    default=".",
    type=click.Path(exists=True),
    help="Path to target workspace/repo.",
)
@click.option("--preset", default=None, type=click.Choice(list(PRESETS.keys())))
@click.option("--config", "config_path", default=None, type=click.Path(exists=True))
@click.option("--mock", is_flag=True, help="Use mock providers.")
@click.option(
    "--allow-dirty",
    is_flag=True,
    help="Start even if the git workspace has uncommitted changes.",
)
@click.option("--verbose", "-v", is_flag=True)
def refine(
    run_id: str,
    workspace: str,
    preset: str | None,
    config_path: str | None,
    mock: bool,
    allow_dirty: bool,
    verbose: bool,
) -> None:
    """Iteratively refine a completed run with user feedback.

    Loads the artifacts from a previous run, shows the current state
    (plan, code, tests, review), then enters a loop where the user
    provides feedback.  Each round re-runs the implementer, sandbox,
    and reviewer with the feedback incorporated.
    """
    if mock:
        preset = "mock"

    config = load_config(
        config_path=Path(config_path) if config_path else None,
        preset=preset,
        workspace=Path(workspace),
    )

    log_level = "DEBUG" if verbose else config.pipeline.log_level
    setup_logging(level=log_level)

    workspace_path = Path(workspace).resolve()
    artifacts_base = workspace_path / "runs"

    from delegatron.artifacts import ArtifactStore

    store = ArtifactStore(base_dir=artifacts_base, run_id=run_id)
    saved_state = store.load_state()

    if saved_state is None:
        console.print(f"[red]No checkpoint found for run '{run_id}'.[/]")
        console.print(f"Looked in: {store.run_dir}")
        raise SystemExit(1)

    # Display current run state
    console.print(
        Panel(
            f"[bold]Refining run:[/] {run_id}\n"
            f"Request: {saved_state.get('request', 'N/A')}\n"
            f"Iterations so far: {saved_state.get('iteration', 0)}\n"
            f"Status: {saved_state.get('status', 'unknown')}\n"
            f"Cost so far: ${saved_state.get('total_cost_usd', 0):.4f}",
            title=f"Delegatron Refine v{__version__}",
            border_style="magenta",
        )
    )

    _display_refine_state(saved_state)

    # Enter refinement loop
    from delegatron.orchestrator.graph import Pipeline, restart_current_task

    refinement_round = 0
    state: dict[str, Any] = dict(saved_state)

    while True:
        console.print()
        try:
            feedback = click.prompt(
                click.style("What would you like to change?", fg="cyan"),
                default="done",
            )
        except (EOFError, KeyboardInterrupt, click.Abort):
            feedback = "done"

        feedback = feedback.strip()
        if feedback.lower() in ("done", "quit", "exit", "q"):
            console.print("\n[green]Refinement complete.[/]")
            break

        refinement_round += 1
        console.print(f"\n[bold magenta]--- Refinement round {refinement_round} ---[/]\n")

        # Prepare state for re-running implementer -> sandbox -> reviewer.
        # Each round gets its own iteration budget on top of the iterations so far.
        state["status"] = "running"
        state["error"] = ""
        state["is_approved"] = False
        state["_iteration_base"] = state.get("iteration", 0)
        state["_task_iteration"] = 0
        # The task the feedback is about is open again: no longer completed or
        # skipped, with a fresh attempt history.
        restart_current_task(cast(Any, state))
        # Inject user feedback as review feedback so the implementer sees it
        state["review_report"] = {
            "acceptance_met": False,
            "summary": f"User refinement feedback (round {refinement_round})",
            "issues": [
                {
                    "severity": "major",
                    "title": "User feedback",
                    "details": feedback,
                    "recommended_fix": feedback,
                    "evidence": "",
                }
            ],
        }

        # Build a modified request incorporating the original + feedback
        original_request = saved_state.get("request", "")
        refined_request = (
            f"{original_request}\n\n"
            f"--- User Refinement Feedback (Round {refinement_round}) ---\n"
            f"{feedback}"
        )
        state["request"] = refined_request

        # Re-run a single iteration through implementer -> sandbox -> reviewer
        pipeline = Pipeline(
            config=config,
            workspace_root=workspace_path,
            run_id=run_id,
            artifacts_dir=artifacts_base,
            allow_dirty=allow_dirty,
        )

        state = cast(
            dict[str, Any],
            asyncio.run(pipeline.execute(refined_request, initial_state=state)),
        )

        # Display updated results
        _display_refine_state(state)
        if state.get("iteration", 0) == state.get("_iteration_base", 0):
            console.print(
                "[yellow]This round ran no iteration[/] "
                f"(status: {state.get('status')}; {state.get('error') or 'no error'})."
            )

        # Save the refined state as a new iteration
        store.save_state(dict(state))

    # Save final summary after refinement, on top of the last pipeline summary
    summary_path = store.run_dir / "summary.json"
    summary: dict[str, Any] = (
        json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    )
    summary.update(
        {
            "run_id": run_id,
            "status": state.get("status", "unknown"),
            "iterations": state.get("iteration", 0),
            "total_cost_usd": state.get("total_cost_usd", 0),
            "request": state.get("request", ""),
            "plan_summary": (state.get("plan") or {}).get("summary", ""),
            "is_approved": state.get("is_approved", False),
            "error": state.get("error", ""),
            # why the last round stopped ("" when it did not stop on a limit)
            "termination_reason": state.get("_termination_reason", ""),
            **_files_summary(state),
            "refinement_rounds": refinement_round,
        }
    )
    store.save_summary(summary)
    console.print(f"\n[dim]Saved refined state to {store.run_dir}[/]")


@main.command()
@click.argument("run_id")
@click.option("--workspace", default=".", type=click.Path(exists=True))
def show(run_id: str, workspace: str) -> None:
    """Show the summary and artifacts for a completed run."""
    workspace_path = Path(workspace).resolve()
    artifacts_base = workspace_path / "runs"

    from delegatron.artifacts import ArtifactStore

    store = ArtifactStore(base_dir=artifacts_base, run_id=run_id)

    summary_path = store.run_dir / "summary.json"
    if not summary_path.exists():
        console.print(f"[red]No summary found for run '{run_id}'.[/]")
        raise SystemExit(1)

    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    console.print(
        Panel(
            f"[bold]Run:[/] {summary.get('run_id', run_id)}\n"
            f"[bold]Status:[/] {summary.get('status', 'unknown')}\n"
            f"[bold]Request:[/] {summary.get('request', 'N/A')}\n"
            f"[bold]Plan:[/] {summary.get('plan_summary', 'N/A')}\n"
            f"[bold]Iterations:[/] {summary.get('iterations', 0)}\n"
            f"[bold]Cost:[/] ${summary.get('total_cost_usd', 0):.4f}\n"
            f"[bold]Duration:[/] {summary.get('elapsed_seconds', 0):.1f}s\n"
            f"[bold]Approved:[/] {summary.get('is_approved', False)}"
            + (f"\n[bold]Error:[/] {escape(str(summary['error']))}" if summary.get("error") else "")
            + (
                f"\n[bold]Stopped:[/] {escape(str(summary['termination_reason']))}"
                if summary.get("termination_reason")
                else ""
            ),
            title=f"Run Summary — {run_id}",
            border_style="blue",
        )
    )

    # Show cost breakdown if available
    breakdown = summary.get("cost_breakdown", {})
    if breakdown.get("by_role"):
        table = Table(title="Cost Breakdown by Role")
        table.add_column("Role", style="bold")
        table.add_column("Calls", justify="right")
        table.add_column("Input Tokens", justify="right")
        table.add_column("Output Tokens", justify="right")
        table.add_column("Cost", justify="right")

        for role, data in breakdown["by_role"].items():
            table.add_row(
                role,
                str(int(data["calls"])),
                str(int(data["input_tokens"])),
                str(int(data["output_tokens"])),
                f"${data['cost_usd']:.4f}",
            )
        console.print(table)

    task_outcomes = summary.get("task_outcomes") or []
    if task_outcomes:
        console.print("\n[bold]Tasks:[/]")
        for row in task_outcomes:
            console.print(
                f"  {row.get('task_id', '')}: {row.get('title', '')} — {row.get('status', '?')}"
                f" ({row.get('iterations', 0)} iterations)"
            )

    _show_test_lists(summary)

    if summary.get("branch"):
        console.print(f"\n[bold]Branch:[/] {summary['branch']}")
    files_changed = summary.get("files_changed") or []
    if files_changed:
        console.print(f"\n[bold]Files changed ({len(files_changed)}):[/]")
        for path in files_changed:
            console.print(f"  {escape(path)}")
    files_deleted = summary.get("files_deleted") or []
    if files_deleted:
        console.print(f"\n[bold]Files deleted ({len(files_deleted)}):[/]")
        for path in files_deleted:
            console.print(f"  {escape(path)}")

    # List artifacts
    artifacts = sorted(store.run_dir.glob("*.json"))
    if artifacts:
        console.print(f"\n[bold]Artifacts:[/] ({store.run_dir})")
        for a in artifacts:
            console.print(f"  {a.name}")
    events = store.run_dir / "events.jsonl"
    if events.exists():
        console.print("  events.jsonl")
    calls = store.run_dir / "provider_calls.jsonl"
    if calls.exists():
        console.print("  provider_calls.jsonl")


def _show_test_lists(summary: dict[str, Any]) -> None:
    """Print a summary's test lists: regressed tests and pre-existing failures the run fixed."""
    sections = (
        ("Regressed tests (an approved task's test now fails)", "regressed_tests"),
        ("Pre-existing failures fixed", "fixed_baseline_failures"),
    )
    for title, key in sections:
        tests = summary.get(key) or {}
        if not tests:
            continue
        console.print(f"[bold]{title} ({len(tests)}):[/]")
        items = tests.items() if isinstance(tests, dict) else ((test, "") for test in tests)
        for test, origin in items:
            suffix = f" ({escape(str(origin))})" if origin else ""
            console.print(f"  {escape(str(test))}{suffix}")


@main.command(name="list-runs")
@click.option("--workspace", default=".", type=click.Path(exists=True))
def list_runs(workspace: str) -> None:
    """List all pipeline runs in the workspace."""
    workspace_path = Path(workspace).resolve()
    runs_dir = workspace_path / "runs"

    if not runs_dir.exists():
        console.print("[dim]No runs found.[/]")
        return

    table = Table(title="Pipeline Runs")
    table.add_column("Run ID", style="bold")
    table.add_column("Status")
    table.add_column("Iterations", justify="right")
    table.add_column("Cost", justify="right")

    for d in sorted(runs_dir.iterdir()):
        if not d.is_dir():
            continue
        summary_path = d / "summary.json"
        if summary_path.exists():
            s = json.loads(summary_path.read_text())
            status = s.get("status", "?")
            status_map = {
                "approved": "green",
                "partial": "yellow",
                "exhausted": "yellow",
                "stalled": "yellow",
                "error": "red",
            }
            status_style = status_map.get(status, "")
            table.add_row(
                d.name,
                f"[{status_style}]{status}[/]",
                str(s.get("iterations", "?")),
                f"${s.get('total_cost_usd', 0):.4f}",
            )
        else:
            # Check for state files (in-progress run)
            states = sorted(d.glob("state_*.json"))
            if states:
                table.add_row(d.name, "[yellow]in-progress[/]", str(len(states)), "?")
            else:
                table.add_row(d.name, "[dim]unknown[/]", "?", "?")

    console.print(table)


@main.command(name="estimate")
@click.argument("request")
@click.option("--preset", default=None, type=click.Choice(list(PRESETS.keys())))
@click.option("--max-iterations", default=3, type=int, help="Assumed iterations for estimate.")
def estimate(request: str, preset: str | None, max_iterations: int) -> None:
    """Estimate the cost of a pipeline run before executing."""
    from delegatron.config import is_local_model
    from delegatron.utils.cost import CostTracker, estimate_local_cost

    config = load_config(preset=preset)
    # Where each role runs comes from its endpoint, not the model name.
    local = {
        role: is_local_model(getattr(config, role))
        for role in ("architect", "implementer", "reviewer")
    }

    # Rough token estimates per role per iteration
    # Based on typical prompt sizes and response lengths
    ESTIMATES = {
        "architect": {"input": 2000, "output": 1500},  # 1 call total
        "implementer": {"input": 3000, "output": 2000},  # per iteration
        "reviewer": {"input": 4000, "output": 1000},  # per iteration
    }

    tracker = CostTracker()

    # Architect: called once
    arch_model = config.architect.model
    arch_input = ESTIMATES["architect"]["input"]
    arch_output = ESTIMATES["architect"]["output"]
    arch_cost = tracker.estimate_cost(arch_model, arch_input, arch_output, local["architect"])

    # Implementer + reviewer: called per iteration
    impl_model = config.implementer.model
    rev_model = config.reviewer.model
    impl_input = ESTIMATES["implementer"]["input"]
    impl_output = ESTIMATES["implementer"]["output"]
    rev_input = ESTIMATES["reviewer"]["input"]
    rev_output = ESTIMATES["reviewer"]["output"]
    impl_cost = tracker.estimate_cost(impl_model, impl_input, impl_output, local["implementer"])
    rev_cost = tracker.estimate_cost(rev_model, rev_input, rev_output, local["reviewer"])
    iter_cost = impl_cost + rev_cost

    total_low = arch_cost + iter_cost  # 1 iteration (best case)
    total_high = arch_cost + (iter_cost * max_iterations)  # max iterations

    table = Table(title="Cost Estimate")
    table.add_column("Role", style="bold")
    table.add_column("Model")
    table.add_column("Est. Cost/Call", justify="right")

    table.add_row("Architect", arch_model, f"${arch_cost:.4f}")
    table.add_row("Implementer", impl_model, f"${impl_cost:.4f}")
    table.add_row("Reviewer", rev_model, f"${rev_cost:.4f}")

    console.print(table)
    console.print(
        f"\n[bold]Estimated total:[/] ${total_low:.4f} (1 iteration)"
        f" — ${total_high:.4f} ({max_iterations} iterations)"
    )

    # Show local GPU cost estimate for the roles that run locally
    local_models = [
        (role, model)
        for role, model in [
            ("architect", arch_model),
            ("implementer", impl_model),
            ("reviewer", rev_model),
        ]
        if local[role]
    ]
    if local_models:
        # Estimate GPU time from output tokens at ~30 tok/s
        tok_per_sec = 30.0
        min_output_tokens = 0
        max_output_tokens = 0
        for role, _model in local_models:
            out = ESTIMATES[role]["output"]
            mult = 1 if role == "architect" else max_iterations
            min_output_tokens += out  # 1 iteration
            max_output_tokens += out * mult

        min_local = estimate_local_cost(min_output_tokens / tok_per_sec)
        max_local = estimate_local_cost(max_output_tokens / tok_per_sec)

        console.print(
            f"\n[bold purple]Local GPU cost:[/] "
            f"${min_local['total_local_usd']:.6f} — ${max_local['total_local_usd']:.6f}"
        )
        console.print(
            f"[dim]  Electricity: ${min_local['electricity_usd']:.6f} — "
            f"${max_local['electricity_usd']:.6f} "
            f"({min_local['gpu_watts']:.0f}W @ ${min_local['electricity_rate_kwh']}/kWh)[/]"
        )
        console.print(
            f"[dim]  Hardware wear: ${min_local['hardware_usd']:.6f} — "
            f"${max_local['hardware_usd']:.6f}[/]"
        )

    console.print(
        "[dim]Note: Actual costs depend on prompt/response sizes. This is a rough estimate.[/]"
    )


@main.command(name="list-presets")
def list_presets() -> None:
    """List available configuration presets."""
    table = Table(title="Available Presets", show_lines=True)
    table.add_column("Preset", style="bold")
    table.add_column("Architect")
    table.add_column("Implementer")
    table.add_column("Reviewer")
    table.add_column("Description", overflow="fold")

    for name, roles in PRESETS.items():
        table.add_row(
            name,
            f"{roles['architect']['provider']}/{roles['architect']['model']}",
            f"{roles['implementer']['provider']}/{roles['implementer']['model']}",
            f"{roles['reviewer']['provider']}/{roles['reviewer']['model']}",
            PRESET_DESCRIPTIONS.get(name, ""),
        )

    console.print(table)


@main.command()
@click.argument("request")
@click.option(
    "--presets",
    default="default,budget,mock",
    help="Comma-separated presets to compare.",
)
@click.option(
    "--workspace",
    default=".",
    type=click.Path(exists=True),
)
@click.option("--json-output", is_flag=True)
@click.option(
    "--allow-dirty",
    is_flag=True,
    help="Start even if the git workspace has uncommitted changes.",
)
def benchmark(
    request: str, presets: str, workspace: str, json_output: bool, allow_dirty: bool
) -> None:
    """Run the same task with multiple presets and compare results."""
    from delegatron.benchmark import run_benchmark

    preset_list = [p.strip() for p in presets.split(",") if p.strip()]
    workspace_path = Path(workspace).resolve()

    if not json_output:
        console.print(
            Panel(
                f"[bold]Benchmark:[/] {request}\nPresets: {', '.join(preset_list)}",
                title="Delegatron Benchmark",
                border_style="cyan",
            )
        )

    report = asyncio.run(
        run_benchmark(request, workspace_path, preset_list, allow_dirty=allow_dirty)
    )

    if json_output:
        click.echo(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        table = Table(title="Benchmark Results")
        table.add_column("Preset", style="bold")
        table.add_column("Status")
        table.add_column("Score", justify="right")
        table.add_column("Iterations", justify="right")
        table.add_column("Cost", justify="right")
        table.add_column("Time", justify="right")

        for r in report.results:
            status_style = "green" if r.approved else "yellow"
            table.add_row(
                r.preset,
                f"[{status_style}]{r.status}[/]",
                f"{r.emcs_score:.2f}",
                str(r.iterations),
                f"${r.cost_usd:.4f}",
                f"{r.elapsed_seconds:.1f}s",
            )

        console.print(table)

        d = report.to_dict()
        if d["best_quality"]:
            console.print(f"\n[bold]Best quality:[/] {d['best_quality']}")
        if d["cheapest"]:
            console.print(f"[bold]Cheapest:[/] {d['cheapest']}")
        if d["fastest"]:
            console.print(f"[bold]Fastest:[/] {d['fastest']}")


@main.command()
@click.option(
    "--preset",
    default="default",
    help="Preset to evaluate.",
)
@click.option(
    "--split",
    default="lite",
    type=click.Choice(["lite", "verified", "full"]),
    help="SWE-bench split.",
)
@click.option("--instances", default=10, type=int, help="Number of instances to run.")
@click.option(
    "--tier",
    default=None,
    type=click.Choice(["simple", "moderate", "complex", "feature"]),
    help="Filter by complexity tier.",
)
@click.option("--max-iterations", default=5, type=int)
@click.option("--max-cost", default=5.0, type=float, help="Max cost per instance.")
@click.option("--json-output", is_flag=True)
@click.option(
    "--work-dir",
    default="swebench-workdir",
    type=click.Path(),
    help="Working directory for cloned repos.",
)
def swebench(
    preset: str,
    split: str,
    instances: int,
    tier: str | None,
    max_iterations: int,
    max_cost: float,
    json_output: bool,
    work_dir: str,
) -> None:
    """Evaluate Delegatron on SWE-bench real-world GitHub issues.

    Downloads issues from SWE-bench, runs each through the pipeline,
    and evaluates whether the generated patches resolve the issues.

    \b
    Examples:
      delegatron swebench --preset local --instances 10
      delegatron swebench --preset default --split verified --instances 50
      delegatron swebench --preset single-claude --tier complex --instances 20
    """
    from delegatron.swebench import SWEBenchAdapter

    if not json_output:
        console.print(
            Panel(
                f"[bold]SWE-bench Evaluation[/]\n"
                f"Preset: {preset} | Split: {split} | Instances: {instances}"
                + (f" | Tier: {tier}" if tier else ""),
                title="Delegatron",
                border_style="cyan",
            )
        )

    adapter = SWEBenchAdapter(
        preset=preset,
        work_dir=Path(work_dir),
        max_iterations=max_iterations,
        max_cost_usd=max_cost,
    )

    report = asyncio.run(adapter.run(split=split, limit=instances, tier=tier))

    if json_output:
        click.echo(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        # Summary table
        table = Table(title="SWE-bench Results")
        table.add_column("Tier", style="bold")
        table.add_column("Total", justify="right")
        table.add_column("Resolved", justify="right")
        table.add_column("Rate", justify="right")
        table.add_column("Avg Cost", justify="right")
        table.add_column("Avg Iters", justify="right")

        for tier_name, tier_data in report.by_tier().items():
            rate = tier_data["resolve_rate"] * 100
            rate_style = "green" if rate >= 50 else "yellow" if rate >= 25 else "red"
            table.add_row(
                tier_name,
                str(tier_data["total"]),
                str(tier_data["resolved"]),
                f"[{rate_style}]{rate:.0f}%[/]",
                f"${tier_data['avg_cost']:.4f}",
                f"{tier_data['avg_iterations']:.1f}",
            )

        console.print(table)
        console.print(
            f"\n[bold]Overall:[/] {report.resolve_rate * 100:.1f}% resolved "
            f"({sum(1 for r in report.results if r.resolved)}/{len(report.results)}) "
            f"| ${report.total_cost:.4f} total | {report.total_time:.0f}s"
        )


@main.command()
@click.argument("request")
@click.option(
    "--preset",
    default=None,
    type=click.Choice(list(PRESETS.keys())),
    help="Config preset.",
)
@click.option("--workspace", default=".", type=click.Path(exists=True))
@click.option("--max-milestones", default=10, type=int, help="Max milestones.")
@click.option("--max-iterations", default=5, type=int, help="Max iterations per milestone.")
@click.option("--max-cost", default=50.0, type=float, help="Total cost cap (all milestones).")
@click.option("--max-time", default=120, type=int, help="Total wall-time cap (minutes).")
@click.option("--json-output", is_flag=True)
@click.option(
    "--allow-dirty",
    is_flag=True,
    help="Start even if the git workspace has uncommitted changes.",
)
@click.option("--verbose", "-v", is_flag=True)
def project(
    request: str,
    preset: str | None,
    workspace: str,
    max_milestones: int,
    max_iterations: int,
    max_cost: float,
    max_time: int,
    json_output: bool,
    allow_dirty: bool,
    verbose: bool,
) -> None:
    """Build a complete project from a high-level description.

    Decomposes the request into milestones and runs each through the
    full pipeline. The workspace accumulates code across milestones.

    \b
    Examples:
      delegatron project "Build a Flask todo app with auth, database, and tests"
      delegatron project --preset local "Build a REST API with user management"
      delegatron project --max-milestones 5 --max-cost 20 "Build a CLI tool"
    """
    from delegatron.project import ProjectRunner

    config = load_config(preset=preset, workspace=Path(workspace))
    log_level = "DEBUG" if verbose else config.pipeline.log_level
    setup_logging(level=log_level)

    run_id = f"proj-{uuid.uuid4().hex[:12]}"
    workspace_path = Path(workspace).resolve()

    if not json_output:
        console.print(
            Panel(
                f"[bold]Project Mode[/]\n{request}\n\n"
                f"Preset: {preset or 'default'} | Max milestones: {max_milestones}\n"
                f"Budget: ${max_cost} | Time limit: {max_time}m",
                title="Delegatron",
                border_style="magenta",
            )
        )

    runner = ProjectRunner(
        config=config,
        workspace_root=workspace_path,
        project_id=run_id,
        max_milestones=max_milestones,
        max_iterations_per_milestone=max_iterations,
        max_total_cost=max_cost,
        max_wall_time_minutes=max_time,
        allow_dirty=allow_dirty,
    )

    report = asyncio.run(runner.execute(request))

    if json_output:
        click.echo(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        # Display milestone results
        table = Table(title="Project Results")
        table.add_column("#", style="bold", width=3)
        table.add_column("Milestone")
        table.add_column("Status")
        table.add_column("Iter", justify="right")
        table.add_column("Cost", justify="right")
        table.add_column("Time", justify="right")

        for i, r in enumerate(report.results, 1):
            status_style = (
                "green"
                if r.status == "approved"
                else "yellow"
                if r.status == "exhausted"
                else "red"
            )
            table.add_row(
                str(i),
                r.title[:50],
                f"[{status_style}]{r.status}[/]",
                str(r.iterations),
                f"${r.cost_usd:.4f}",
                f"{r.elapsed_seconds:.0f}s",
            )

        console.print(table)
        console.print(
            f"\n[bold]Status:[/] {report.status} | "
            f"{report.milestones_completed}/{report.milestones_total} milestones | "
            f"${report.total_cost_usd:.4f} | {report.total_time_seconds:.0f}s"
        )


@main.command()
@click.option(
    "--host", default="127.0.0.1", help="Bind host (use 0.0.0.0 to expose on the network)."
)
@click.option("--port", default=8000, type=int, help="Bind port.")
@click.option(
    "--workspace",
    default=".",
    type=click.Path(exists=True),
)
@click.option("--verbose", "-v", is_flag=True)
def serve(host: str, port: int, workspace: str, verbose: bool) -> None:
    """Start the Delegatron API server."""
    setup_logging(level="DEBUG" if verbose else "INFO")

    import sys

    import uvicorn

    workspace_path = Path(workspace).resolve()

    # Use localhost for display URLs when binding to all interfaces
    display_host = "localhost" if host == "0.0.0.0" else host

    from delegatron.server.app import app, configure

    configure(workspace=workspace_path)

    # Re-read workspace after configure (may have redirected away from install dir)
    from delegatron.server.app import _workspace_root

    # Check if dashboard is built (.next directory from next build)
    install_dir = Path(__file__).resolve().parent.parent.parent
    dashboard_dir = install_dir / "dashboard"
    dashboard_built = (dashboard_dir / ".next").is_dir()
    dashboard_port = port + 1  # Dashboard on next port (e.g. 8001)
    dashboard_proc = None

    if dashboard_built:
        dashboard_line = f"Dashboard: http://{display_host}:{dashboard_port}"
    elif dashboard_dir.is_dir():
        dashboard_line = "[dim]Dashboard not built. Run: cd dashboard && npm run build[/]"
    else:
        dashboard_line = (
            "[dim]Dashboard not available in this install. "
            "Use start-dashboard.bat from a source checkout.[/]"
        )

    # Kill stale processes on our ports
    import contextlib

    for p in (port, dashboard_port):
        with contextlib.suppress(Exception):
            ps_cmd = (
                f"Get-NetTCPConnection -LocalPort {p} "
                "-State Listen -ErrorAction SilentlyContinue "
                "| ForEach-Object { Stop-Process -Id $_.OwningProcess "
                "-Force -ErrorAction SilentlyContinue }"
            )
            subprocess.run(
                ["powershell", "-Command", ps_cmd],
                capture_output=True,
                timeout=5,
            )

    console.print(
        Panel(
            f"[bold]API Server[/]\n"
            f"Workspace: {_workspace_root}\n"
            f"API: http://{display_host}:{port}\n"
            f"Docs: http://{display_host}:{port}/docs\n"
            f"{dashboard_line}",
            title=f"Delegatron v{__version__}",
            border_style="green",
        )
    )

    # Spawn dashboard (next start) as a subprocess
    if dashboard_built:
        env = {**os.environ, "NEXT_PUBLIC_API_URL": f"http://{display_host}:{port}"}
        npx = "npx.cmd" if sys.platform == "win32" else "npx"
        dashboard_proc = subprocess.Popen(
            [npx, "next", "start", "-p", str(dashboard_port)],
            cwd=str(dashboard_dir),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        console.print(f"INFO Dashboard running on http://{display_host}:{dashboard_port}")

        # Open browser after a short delay
        import threading
        import webbrowser

        def _open_browser() -> None:
            import time

            time.sleep(2)
            webbrowser.open(f"http://{display_host}:{dashboard_port}")

        threading.Thread(target=_open_browser, daemon=True).start()

    console.print(f"INFO API server running on http://{display_host}:{port} (Press CTRL+C to quit)")
    try:
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="info",
            # Prevent uvicorn from calling dictConfig() which can
            # interfere with our pre-configured file handlers.
            log_config=None,
        )
    finally:
        if dashboard_proc is not None:
            console.print("Stopping dashboard...")
            dashboard_proc.terminate()
            try:
                dashboard_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                dashboard_proc.kill()
        console.print("Server stopped.")


# ---------------------------------------------------------------------------
# Template sub-commands
# ---------------------------------------------------------------------------

DEFAULT_TEMPLATE_DIR = Path.home() / ".delegatron" / "templates"


@main.group()
def template() -> None:
    """Manage reusable run templates."""


@template.command(name="list")
@click.option(
    "--dir",
    "store_dir",
    default=None,
    type=click.Path(),
    help="Template store directory.",
)
def template_list(store_dir: str | None) -> None:
    """List all saved templates."""
    store = TemplateStore(Path(store_dir) if store_dir else DEFAULT_TEMPLATE_DIR)
    templates = store.list()
    if not templates:
        console.print("[dim]No templates found.[/]")
        return

    table = Table(title="Run Templates")
    table.add_column("Name", style="bold")
    table.add_column("Description")
    table.add_column("Preset")
    table.add_column("Request Pattern")

    for t in templates:
        console.print()
        table.add_row(
            t.name,
            t.description or "-",
            t.preset or "-",
            t.request_pattern[:60] + ("..." if len(t.request_pattern) > 60 else ""),
        )
    console.print(table)


@template.command(name="save")
@click.argument("name")
@click.option(
    "--request",
    "request_pattern",
    required=True,
    help="Request pattern (use {var} for placeholders).",
)
@click.option("--description", default="", help="Template description.")
@click.option(
    "--preset",
    default=None,
    type=click.Choice(list(PRESETS.keys())),
    help="Config preset.",
)
@click.option("--max-iterations", default=None, type=int, help="Override max iterations.")
@click.option("--max-cost", default=None, type=float, help="Override max cost USD.")
@click.option("--max-time", default=None, type=int, help="Override max wall-time minutes.")
@click.option("--var", "variables", multiple=True, help="Default variable as key=value.")
@click.option(
    "--dir",
    "store_dir",
    default=None,
    type=click.Path(),
    help="Template store directory.",
)
def template_save(
    name: str,
    request_pattern: str,
    description: str,
    preset: str | None,
    max_iterations: int | None,
    max_cost: float | None,
    max_time: int | None,
    variables: tuple[str, ...],
    store_dir: str | None,
) -> None:
    """Save a new run template."""
    overrides: dict[str, Any] = {}
    if max_iterations is not None:
        overrides["max_iterations"] = max_iterations
    if max_cost is not None:
        overrides["max_cost_usd"] = max_cost
    if max_time is not None:
        overrides["max_wall_time_minutes"] = max_time

    var_dict: dict[str, str] = {}
    for v in variables:
        if "=" not in v:
            raise click.BadParameter(f"Variable must be key=value, got: {v}")
        key, value = v.split("=", 1)
        var_dict[key] = value

    tpl = RunTemplate(
        name=name,
        description=description,
        request_pattern=request_pattern,
        preset=preset,
        overrides=overrides,
        variables=var_dict,
    )

    store = TemplateStore(Path(store_dir) if store_dir else DEFAULT_TEMPLATE_DIR)
    path = store.save(tpl)
    console.print(f"[green]Template '{name}' saved to {path}[/]")


@template.command(name="run")
@click.argument("name")
@click.option("--var", "variables", multiple=True, help="Variable as key=value.")
@click.option(
    "--dir",
    "store_dir",
    default=None,
    type=click.Path(),
    help="Template store directory.",
)
@click.option(
    "--workspace",
    default=".",
    type=click.Path(exists=True),
    help="Path to target workspace/repo.",
)
@click.option(
    "--preset",
    default=None,
    type=click.Choice(list(PRESETS.keys())),
    help="Config preset. Overrides the template's baked preset.",
)
@click.option(
    "--create-pr",
    is_flag=True,
    help="Open a GitHub PR if the run is approved. Requires `gh` CLI authenticated.",
)
@click.option("--json-output", is_flag=True, help="Output results as JSON.")
@click.option("--mock", is_flag=True, help="Use mock providers.")
@click.option(
    "--allow-dirty",
    is_flag=True,
    help="Start even if the git workspace has uncommitted changes.",
)
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
def template_run(
    name: str,
    variables: tuple[str, ...],
    store_dir: str | None,
    workspace: str,
    preset: str | None,
    create_pr: bool,
    json_output: bool,
    mock: bool,
    allow_dirty: bool,
    verbose: bool,
) -> None:
    """Run a pipeline from a saved template."""
    store = TemplateStore(Path(store_dir) if store_dir else DEFAULT_TEMPLATE_DIR)

    try:
        tpl = store.load(name)
    except FileNotFoundError:
        console.print(f"[red]Template '{name}' not found.[/]")
        raise SystemExit(1) from None

    var_dict: dict[str, str] = {}
    for v in variables:
        if "=" not in v:
            raise click.BadParameter(f"Variable must be key=value, got: {v}")
        key, value = v.split("=", 1)
        var_dict[key] = value

    try:
        rendered_request = tpl.render(var_dict)
    except KeyError as exc:
        console.print(f"[red]{exc}[/]")
        raise SystemExit(1) from None

    # Determine preset: explicit --preset > --mock > template-baked > default
    effective_preset: str | None
    if mock:
        effective_preset = "mock"
    elif preset is not None:
        effective_preset = preset
    else:
        effective_preset = tpl.preset

    # Load config and apply template overrides
    config = load_config(preset=effective_preset, workspace=Path(workspace))

    # Merge template pipeline overrides, template constraints (on top of any
    # from delegatron.toml) + explicit --create-pr; both are validated here,
    # not when the run starts.
    pipeline_overrides = _template_pipeline_overrides(tpl, config)
    if create_pr:
        pipeline_overrides["create_pr"] = True
    if pipeline_overrides:
        from dataclasses import replace

        config = replace(config, pipeline=replace(config.pipeline, **pipeline_overrides))

    # Setup logging
    log_level = "DEBUG" if verbose else config.pipeline.log_level
    setup_logging(level=log_level)

    # Generate run ID
    import uuid

    run_id = uuid.uuid4().hex[:12]
    workspace_path = Path(workspace).resolve()

    if not json_output:
        _display_run_header(rendered_request, config, run_id, workspace_path)

    from delegatron.orchestrator.graph import Pipeline

    pipeline = Pipeline(
        config=config,
        workspace_root=workspace_path,
        run_id=run_id,
        base_branch="",
        allow_dirty=allow_dirty,
    )

    state = asyncio.run(pipeline.execute(rendered_request))

    # Surface the PR URL if one was created (mirrors `delegatron run`).
    pr_info = None
    summary_path = workspace_path / "runs" / run_id / "summary.json"
    if summary_path.is_file():
        try:
            import json as _json

            pr_info = _json.loads(summary_path.read_text()).get("pull_request")
        except Exception:
            pass
    if pr_info and pr_info.get("created"):
        console.print(f"\n[green]PR Created:[/] {pr_info.get('url')}")
    elif pr_info and pr_info.get("skipped_reason"):
        console.print(f"\n[yellow]PR skipped:[/] {pr_info.get('skipped_reason')}")
    elif pr_info and pr_info.get("error"):
        console.print(f"\n[red]PR error:[/] {pr_info.get('error')}")

    if json_output:
        click.echo(json.dumps(state, indent=2, default=str))
    else:
        _display_results(cast(dict[str, Any], state))


@template.command(name="delete")
@click.argument("name")
@click.option(
    "--dir",
    "store_dir",
    default=None,
    type=click.Path(),
    help="Template store directory.",
)
def template_delete(name: str, store_dir: str | None) -> None:
    """Delete a saved template."""
    store = TemplateStore(Path(store_dir) if store_dir else DEFAULT_TEMPLATE_DIR)
    try:
        store.delete(name)
        console.print(f"[green]Template '{name}' deleted.[/]")
    except FileNotFoundError:
        console.print(f"[red]Template '{name}' not found.[/]")
        raise SystemExit(1) from None


def _template_pipeline_overrides(tpl: RunTemplate, config: Any) -> dict[str, Any]:
    """The template's pipeline overrides and constraints, validated (a usage error otherwise)."""
    from delegatron.config import normalize_security_mode
    from delegatron.constraints import Constraints, merge_constraints

    overrides = dict(tpl.overrides.get("pipeline", {}))
    try:
        if "security_mode" in overrides:
            overrides["security_mode"] = normalize_security_mode(overrides["security_mode"])
        if tpl.constraints:
            merged = merge_constraints(config.pipeline.constraints, tpl.constraints)
            Constraints.from_dict(merged)  # a malformed entry fails now, not mid-run
            overrides["constraints"] = merged
    except ValueError as e:
        raise click.BadParameter(f"template {tpl.name!r}: {e}", param_hint="NAME") from e
    return overrides


def _config_workspace(workspace_arg: str, workspace_path: Path) -> Path | None:
    """The workspace whose ``delegatron.toml`` may configure this run, if any.

    A repository cloned from ``--workspace <git URL>`` is not trusted to
    configure the run (its roles could send API keys to any endpoint): its
    ``delegatron.toml`` is used only when ``--config`` names it.
    """
    from delegatron.repo_resolve import is_git_url

    if not is_git_url(workspace_arg):
        return workspace_path
    if (workspace_path / "delegatron.toml").is_file():
        click.echo(
            "Ignoring the cloned repository's delegatron.toml; pass it with --config to use it.",
            err=True,
        )
    return None


def _review_budget_bytes(role: Any) -> int:
    """Bytes of source a review shows: half the reviewer's context window, when known."""
    from delegatron.context import review_budget_bytes

    return review_budget_bytes(int(getattr(role, "context_window", 0) or 0))


def _invoke_and_record(
    agent: Any, input_data: dict[str, Any], role_config: Any, role: str, cost_tracker: Any
) -> dict[str, Any]:
    """Invoke *agent* once and book the call (local GPU time, cache tokens) on *cost_tracker*."""
    import time

    from delegatron.config import is_local_model
    from delegatron.utils.cost import get_gpu_power_watts

    local = is_local_model(role_config)
    start = time.monotonic()
    result: dict[str, Any] = asyncio.run(agent.invoke(input_data))
    generation_seconds = time.monotonic() - start
    meta = result.pop("_meta", {}) or {}
    cost_tracker.record(
        meta.get("model") or role_config.model,
        meta.get("input_tokens", 0),
        meta.get("output_tokens", 0),
        request_id=meta.get("request_id", ""),
        provider_request_id=meta.get("provider_request_id", ""),
        role=role,
        generation_seconds=generation_seconds,
        gpu_watts=get_gpu_power_watts() if local else None,
        is_local=local,
        cache_read_tokens=meta.get("cache_read_tokens", 0),
        cache_write_tokens=meta.get("cache_write_tokens", 0),
        temperature=meta.get("temperature"),
    )
    return result


def _files_summary(state: dict[str, Any]) -> dict[str, list[str]]:
    """``files_changed`` (every path the run wrote) and ``files_deleted`` for a summary."""
    history: dict[str, dict[str, Any]] = state.get("_file_history") or {}
    return {
        "files_changed": sorted(p for p, entry in history.items() if not entry.get("deleted")),
        "files_deleted": sorted(p for p, entry in history.items() if entry.get("deleted")),
    }


def _test_result_line(test_report: dict[str, Any]) -> str:
    """One line for a test report: PASSED / FAILED / UNTESTED with its counts."""
    from delegatron.orchestrator.scoring import classify_test_evidence

    total = test_report.get("total", 0)
    failures = test_report.get("failures", 0)
    evidence = classify_test_evidence(test_report)
    if evidence == "untested":
        reason = test_report.get("skipped_reason") or (
            "only synthetic smoke tests" if test_report.get("synthetic_tests") else "no tests ran"
        )
        return f"[yellow]UNTESTED[/] ({reason})"
    color = "green" if evidence == "passed" else "red"
    return f"[{color}]{evidence.upper()}[/] ({total} total, {failures} failures)"


def _resolve_workspace_arg(workspace: str, branch: str | None, *, quiet: bool) -> Path:
    """The local directory for ``--workspace``: a path, or a git URL cloned at *branch*.

    An invalid value, branch or clone is a usage error, not a traceback.
    """
    from delegatron.multirepo import RepoSyncError
    from delegatron.repo_resolve import is_git_url, resolve_workspace

    if is_git_url(workspace) and not quiet:
        console.print(f"[dim]Cloning {workspace}...[/]")
    try:
        path, _cleanup = resolve_workspace(workspace, branch=branch)
    except (ValueError, RepoSyncError, subprocess.SubprocessError) as e:
        raise click.BadParameter(str(e), param_hint="--workspace") from e
    if not path.is_dir():
        raise click.BadParameter(
            f"{workspace!r} is neither a directory nor a git URL", param_hint="--workspace"
        )
    return path


def _display_changed_files(state: dict[str, Any], title: str) -> None:
    """List every file the run changed (``_file_history``), with current line counts."""
    history: dict[str, dict[str, Any]] = state.get("_file_history") or {}
    if not history:
        return
    root = Path(state.get("workspace_root") or ".")
    console.print(f"\n[bold]{title} ({len(history)}):[/]")
    for path in sorted(history):
        target = root / path
        if history[path].get("deleted") or not target.is_file():
            console.print(f"  [red]-[/] {path}")
            continue
        lines = target.read_text(encoding="utf-8", errors="replace").count("\n")
        console.print(f"  [green]+[/] {path} ({lines} lines)")


def _display_branch(state: dict[str, Any]) -> None:
    """Show the run branch and how to get back to the branch the run started from."""
    event = next(
        (
            m
            for m in reversed(state.get("messages") or [])
            if m.get("event") == "workspace_initialized"
        ),
        None,
    )
    if event is None:
        return
    console.print(f"\n[bold]Branch:[/] {event.get('branch')}")
    original = event.get("original_branch")
    if original and original != event.get("branch"):
        console.print(f"[dim]Return to your previous checkout with: git checkout {original}[/]")


def _display_refine_state(state: dict[str, Any]) -> None:
    """Show the current plan, code, tests, and review for a refinement session."""
    console.print(f"\n[bold]Status:[/] {state.get('status', 'unknown')}")

    # Plan
    plan = state.get("plan")
    if plan:
        console.print(f"\n[bold]Plan:[/] {plan.get('summary', '')}")

    _display_changed_files(state, "Files")
    _display_branch(state)

    # Test results
    test_report = state.get("test_report") or {}
    if test_report:
        console.print(f"\n[bold]Tests:[/] {_test_result_line(test_report)}")

    # Review
    review = state.get("review_report")
    if review:
        console.print(f"\n[bold]Review:[/] {review.get('summary', '')}")
        issues = review.get("issues", [])
        for issue in issues:
            sev = issue.get("severity", "")
            sev_map = {"blocker": "red", "major": "yellow", "minor": "blue", "nit": "dim"}
            sev_style = sev_map.get(sev, "")
            console.print(f"  [{sev_style}]{sev.upper()}[/]: {issue.get('title', '')}")


def _make_stream_callback() -> Callable[[str, dict[str, Any]], None]:
    """Create a step callback that prints streaming progress to stderr."""
    import sys

    def _cb(step: str, data: dict[str, Any]) -> None:
        label = step.replace("_", " ").title()
        if step in ("architect_start", "implementer_start", "reviewer_start"):
            role = step.replace("_start", "")
            console.print(
                f"\n[bold blue]{role.title()}[/] generating...",
                highlight=False,
            )
        elif step == "token":
            # Real-time token from streaming provider
            sys.stderr.write(data.get("text", ""))
            sys.stderr.flush()
        elif step in ("architect_complete", "implementer_complete", "reviewer_complete"):
            console.print()  # newline after streamed tokens
        elif "error" in step:
            console.print(f"[red]{label}:[/] {data.get('error', '')}")
        elif step == "sandbox_complete":
            passed = data.get("passed", False)
            color = "green" if passed else "red"
            console.print(
                f"[{color}]Tests: {'PASSED' if passed else 'FAILED'}[/{color}]"
                f" ({data.get('total', 0)} tests)"
            )

    return _cb


def _display_run_header(request: str, config: object, run_id: str, workspace: Path) -> None:
    """Display run configuration header."""
    console.print(
        Panel(
            f"[bold]{request}[/]\n\n"
            f"Run ID: {run_id}\n"
            f"Workspace: {workspace}\n"
            f"Architect: {config.architect.provider}/{config.architect.model}\n"  # type: ignore[attr-defined]
            f"Implementer: {config.implementer.provider}/{config.implementer.model}\n"  # type: ignore[attr-defined]
            f"Reviewer: {config.reviewer.provider}/{config.reviewer.model}\n"  # type: ignore[attr-defined]
            f"Limits: {config.pipeline.max_iterations} iterations, "  # type: ignore[attr-defined]
            f"${config.pipeline.max_cost_usd:.2f}, "  # type: ignore[attr-defined]
            f"{config.pipeline.max_wall_time_minutes}min",  # type: ignore[attr-defined]
            title=f"Delegatron v{__version__}",
            border_style="blue",
        )
    )


def _display_plan(plan: dict[str, Any]) -> None:
    """Display a work plan."""
    console.print(f"\n[bold]Summary:[/] {plan.get('summary', '')}\n")

    if plan.get("assumptions"):
        console.print("[bold]Assumptions:[/]")
        for a in plan["assumptions"]:
            console.print(f"  - {a}")

    if plan.get("open_questions"):
        console.print("\n[yellow bold]Open Questions:[/]")
        for q in plan["open_questions"]:
            console.print(f"  ? {q}")

    tasks = plan.get("tasks", [])
    if tasks:
        table = Table(title=f"\nTasks ({len(tasks)})")
        table.add_column("ID", style="dim")
        table.add_column("Title")
        table.add_column("Risk", justify="center")
        table.add_column("Depends On", style="dim")

        for task in tasks:
            risk_map = {"low": "green", "medium": "yellow", "high": "red"}
            risk_style = risk_map.get(task.get("risk_level", ""), "")
            table.add_row(
                task.get("task_id", ""),
                task.get("title", ""),
                f"[{risk_style}]{task.get('risk_level', '')}[/]",
                ", ".join(task.get("depends_on", [])) or "-",
            )
        console.print(table)

    acceptance = plan.get("acceptance", {})
    if acceptance.get("criteria"):
        console.print("\n[bold]Acceptance Criteria:[/]")
        for c in acceptance["criteria"]:
            console.print(f"  [green]\u2713[/] {c}")


def _display_results(state: dict[str, Any]) -> None:
    """Display pipeline results."""
    status = state.get("status", "unknown")
    status_style = {
        "approved": "[green bold]APPROVED[/]",
        "partial": "[yellow bold]PARTIAL[/] (some tasks were not approved)",
        "exhausted": "[yellow bold]EXHAUSTED[/]",
        "stalled": "[yellow bold]STALLED[/]",
        "error": "[red bold]ERROR[/]",
    }.get(status, f"[dim]{status}[/]")

    console.print(f"\n{'=' * 60}")
    console.print(f"[bold]Result:[/] {status_style}")
    console.print(f"[bold]Iterations:[/] {state.get('iteration', 0)}")
    console.print(f"[bold]Total Cost:[/] ${state.get('total_cost_usd', 0):.4f}")

    if state.get("error"):
        console.print(f"[red]Error:[/] {state['error']}")

    # Show plan summary
    plan = state.get("plan")
    if plan:
        console.print(f"\n[bold]Plan:[/] {plan.get('summary', '')}")
        outcomes: dict[str, dict[str, Any]] = state.get("_task_outcomes") or {}
        for t in plan.get("tasks", []):
            outcome = (outcomes.get(t.get("task_id", "")) or {}).get("status")
            suffix = f" [dim]({outcome})[/]" if outcome else ""
            console.print(f"  [{t.get('risk_level', '?')}] {t.get('title', '')}{suffix}")

    _display_changed_files(state, "Files created/modified")
    _display_branch(state)

    # Show test results
    test_report = state.get("test_report") or {}
    if test_report:
        passed = test_report.get("passed", False)
        console.print(f"\n[bold]Tests:[/] {_test_result_line(test_report)}")
        stderr = test_report.get("stderr", "")
        if stderr and not passed:
            # Show last few lines of stderr for failed tests
            lines = stderr.strip().split("\n")
            tail = "\n".join(lines[-5:])
            console.print(f"  [dim]{tail}[/]")

    # Show review summary
    review = state.get("review_report")
    if review:
        console.print(f"\n[bold]Review:[/] {review.get('summary', '')}")
        issues = review.get("issues", [])
        if issues:
            for issue in issues:
                sev = issue.get("severity", "")
                sev_map = {
                    "blocker": "red",
                    "major": "yellow",
                    "minor": "blue",
                    "nit": "dim",
                }
                sev_style = sev_map.get(sev, "")
                console.print(f"  [{sev_style}]{sev.upper()}[/]: {issue.get('title', '')}")

    # Show PR if created
    for msg in state.get("messages", []):
        if msg.get("event") == "pr_created":
            console.print(f"\n[bold green]PR Created:[/] {msg.get('url', '')}")

    console.print(f"\n{'=' * 60}\n")


if __name__ == "__main__":
    main()
