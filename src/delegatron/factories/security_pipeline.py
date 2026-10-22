"""Centralized security check pipeline.

Replaces the duplicated constraint/secrets/deps checks in graph.py and
async_runner.py steps 2b-2d with a single function.  The checks only judge
the lines a patch adds, so call it before the patch is applied.  With a
:class:`~delegatron.security.patch_lines.SecurityBaseline` they judge
everything the run has changed so far, so a finding from an earlier,
already applied patch is reported until it is removed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from delegatron.security.patch_lines import SecurityBaseline, added_lines, resolve_edits

__all__ = [
    "SecurityBaseline",
    "SecurityResult",
    "blocking_findings",
    "run_security_checks",
    "should_block",
]

logger = logging.getLogger(__name__)

_BLOCKING_SEVERITIES = frozenset({"blocker", "major"})

_BLOCK_LABELS = {"constraints": "Constraint violations", "secrets": "Secrets detected"}


def _finding(
    severity: str, title: str, details: str, file: str, line: int | None, source: str
) -> dict[str, Any]:
    return {
        "severity": severity,
        "title": title,
        "details": details,
        "file": file,
        "line": line,
        "source": source,
    }


@dataclass
class SecurityResult:
    """Aggregate result from all security checks on a patch."""

    constraint_violations: list[dict[str, Any]] = field(default_factory=list)
    secret_findings: list[dict[str, Any]] = field(default_factory=list)
    dependency_findings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_blockers(self) -> bool:
        """Return True if any check found issues."""
        return bool(self.constraint_violations or self.secret_findings or self.dependency_findings)

    @property
    def findings(self) -> list[dict[str, Any]]:
        """Every finding as ``{severity, title, details, file, line, source}``.

        ``severity`` uses the review scale ("blocker" / "major" / "minor");
        ``source`` is "constraints", "secrets" or "dependencies".
        """
        items = [
            _finding(
                "major",
                f"Constraint violation: {v.get('rule', '')}",
                v.get("detail", ""),
                v.get("file", ""),
                v.get("line"),
                "constraints",
            )
            for v in self.constraint_violations
        ]
        items.extend(
            _finding(
                # Critical patterns are private keys and known provider tokens.
                "blocker" if s.get("severity") == "critical" else "major",
                f"Possible secret: {s.get('pattern', '')}",
                f"Line matches the {s.get('pattern', '')} pattern: {s.get('line_content', '')}",
                s.get("file", ""),
                s.get("line"),
                "secrets",
            )
            for s in self.secret_findings
        )
        items.extend(
            _finding(
                "minor",
                f"New dependency: {d.get('name', '')}",
                f"`{d.get('import_line', '')}` imports a package the project does not declare",
                d.get("file", ""),
                d.get("line"),
                "dependencies",
            )
            for d in self.dependency_findings
        )
        return items

    @property
    def summary_events(self) -> list[dict[str, Any]]:
        """Return event dicts suitable for appending to state messages."""
        events: list[dict[str, Any]] = []
        if self.constraint_violations:
            events.append(
                {
                    "event": "constraint_violation",
                    "violations": self.constraint_violations,
                }
            )
        if self.secret_findings:
            events.append(
                {
                    "event": "secrets_detected",
                    "findings": self.secret_findings,
                }
            )
        if self.dependency_findings:
            events.append(
                {
                    "event": "new_dependencies",
                    "packages": self.dependency_findings,
                }
            )
        return events

    @property
    def store_events(self) -> list[dict[str, Any]]:
        """Return compact event dicts (count and affected files) for the artifact store."""
        events: list[dict[str, Any]] = []
        for name, entries in (
            ("constraint_violation", self.constraint_violations),
            ("secrets_detected", self.secret_findings),
            ("new_dependencies", self.dependency_findings),
        ):
            if entries:
                files = sorted({e["file"] for e in entries if e.get("file")})
                events.append({"event": name, "count": len(entries), "files": files})
        return events


def run_security_checks(
    patch: dict[str, Any],
    constraints: Any | None,
    workspace_root: Path,
    *,
    secrets_allowlist: Sequence[str] = (),
    baseline: SecurityBaseline | None = None,
) -> SecurityResult:
    """Run all security checks on the lines a patch adds.

    Executes in order:
    1. Constraint validation (forbidden patterns, protected files)
    2. Secrets scanning (leaked credentials)
    3. Dependency auditing (new third-party imports)

    Search/replace ``edits`` are folded into the files they change first, so
    the checks see the content that will be written.

    Args:
        patch: The PatchSet dict from the implementer.
        constraints: Optional Constraints object (from config).
        workspace_root: Path to the project workspace, still holding the
            pre-patch files.
        secrets_allowlist: Regexes for lines the secrets scanner ignores
            (``[security] secrets_allowlist``).
        baseline: The run's :class:`SecurityBaseline`.  When given, the
            paths *patch* touches are recorded in it (call this before the
            patch is applied) and the checks cover every file the run has
            changed since the baseline, each diffed against its original,
            not only against the previous iteration.

    Returns:
        SecurityResult with findings from all checks.
    """
    result = SecurityResult()
    checked = resolve_edits(patch, workspace_root)
    if baseline is not None:
        baseline.record(patch)
        checked = baseline.widen(checked)
    added = added_lines(checked, workspace_root, baseline=baseline)

    # 1. Constraint validation
    if constraints:
        from delegatron.constraints import validate_patch

        violations = validate_patch(checked, constraints, added=added)
        if violations:
            result.constraint_violations = [
                {"rule": v.rule, "file": v.file, "line": v.line, "detail": v.detail}
                for v in violations
            ]
            logger.info("Constraint violations: %d", len(violations))

    # 2. Secrets scanning
    from delegatron.security.secrets import scan_patch

    findings = scan_patch(checked, allowlist=list(secrets_allowlist), added=added)
    if findings:
        result.secret_findings = [
            {
                "pattern": f.pattern_name,
                "severity": f.severity,
                "file": f.file,
                "line": f.line_number,
                "line_content": f.line_content,
            }
            for f in findings
        ]
        logger.warning("Secrets detected in patch: %d finding(s)", len(findings))

    # 3. Dependency auditing
    from delegatron.security.deps import audit_imports

    dep_findings = audit_imports(checked, workspace_root, added=added)
    if dep_findings:
        result.dependency_findings = [
            {
                "name": f.package_name,
                "import_line": f.import_line,
                "severity": f.severity,
                "file": f.file,
                "line": f.line_number,
            }
            for f in dep_findings
        ]
        logger.info(
            "New dependencies detected: %s",
            ", ".join(f.package_name for f in dep_findings),
        )

    return result


def blocking_findings(result: SecurityResult, security_mode: str) -> list[dict[str, Any]]:
    """Findings that must block approval: blocker/major ones in "block" mode, none otherwise."""
    if security_mode.strip().lower() != "block":
        return []
    return [f for f in result.findings if f["severity"] in _BLOCKING_SEVERITIES]


def should_block(result: SecurityResult, security_mode: str) -> str | None:
    """Describe why the patch is blocked, naming the kinds and files.

    Args:
        result: The SecurityResult from run_security_checks.
        security_mode: "block", "warn", or "allow".

    Returns:
        A message when :func:`blocking_findings` is non-empty, None otherwise.
    """
    blocking = blocking_findings(result, security_mode)
    if not blocking:
        return None
    counts: dict[str, int] = {}
    for finding in blocking:
        counts[finding["source"]] = counts.get(finding["source"], 0) + 1
    kinds = ", ".join(f"{_BLOCK_LABELS[source]} ({n})" for source, n in counts.items())
    files = sorted({f["file"] for f in blocking if f["file"]})
    where = f" in {', '.join(files)}" if files else ""
    return f"{kinds}{where} — blocked by security_mode=block"
