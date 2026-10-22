"""Secrets scanner — detect leaked credentials in patch content.

Scans the lines a patch adds for common secret patterns (cloud and
provider keys, private keys, credentialed connection strings, quoted
token/password literals, high-entropy strings) and returns findings with
the file, line number and a redacted copy of the line.  Supports an
allowlist to suppress known false positives such as test fixtures.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

from delegatron.security.patch_lines import AddedLines, added_lines

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Severity = Literal["critical", "high", "medium"]


@dataclass
class SecretFinding:
    """A single secret detected in patch content."""

    pattern_name: str
    line_number: int  # 1-based line in the file after the patch is applied
    line_content: str  # redacted
    severity: Severity
    file: str = ""


@dataclass
class SecretPattern:
    """Definition of a secret pattern to scan for.

    A named ``value`` group marks where redaction starts.  ``accept`` can
    reject a match (for example a connection string to a loopback host), and
    ``config_only`` limits the pattern to env/ini/YAML-style files, where
    unquoted values are literals rather than identifiers.
    """

    name: str
    regex: re.Pattern[str]
    severity: Severity
    accept: Callable[[re.Match[str]], bool] | None = None
    config_only: bool = False


# ---------------------------------------------------------------------------
# Built-in patterns
# ---------------------------------------------------------------------------

# A quoted literal of 20+ characters; braces, ``$`` and ``<>`` mark template
# placeholders such as f-string fields, which are not secrets.
_QUOTED_VALUE = r"""['"]?\s*[:=]\s*(?P<q>['"])(?P<value>[^'"\s{}$<>]{20,})(?P=q)"""
# In config files the value may be unquoted and fills the rest of the line.
_BARE_VALUE = r"""['"]?\s*[:=]\s*(?P<value>[^'"\s{}$<>#]{20,})\s*$"""
_API_KEY_NAMES = r"(?:api_key|apikey|api[-_]?secret|secret[-_]?key)"
_TOKEN_NAMES = r"(?:token|bearer|password|passwd|pwd)"

_CONFIG_SUFFIXES = (".env", ".ini", ".cfg", ".conf", ".properties", ".yaml", ".yml")


def _is_remote_credential(match: re.Match[str]) -> bool:
    """True when a connection string carries a real password for a non-loopback host."""
    password = match.group("value")
    host = match.group("host").strip("[]").lower()
    if any(ch in "{}$<>" for ch in password + host):
        return False  # template placeholder
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


_PATTERNS: list[SecretPattern] = [
    # Provider-specific formats first, so a line is reported under its most
    # precise name.
    SecretPattern(
        name="private_key",
        regex=re.compile(r"-----BEGIN\s+(?:RSA|EC|OPENSSH|DSA|PGP)\s+PRIVATE\s+KEY-----"),
        severity="critical",
    ),
    SecretPattern(
        name="aws_access_key",
        regex=re.compile(r"AKIA[0-9A-Z]{16}"),
        severity="critical",
    ),
    SecretPattern(
        name="github_token",
        regex=re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,}"),
        severity="critical",
    ),
    SecretPattern(
        name="github_pat",
        regex=re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
        severity="critical",
    ),
    SecretPattern(
        name="stripe_key",
        regex=re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}"),
        severity="critical",
    ),
    SecretPattern(
        name="anthropic_api_key",
        regex=re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
        severity="critical",
    ),
    SecretPattern(
        name="openai_api_key",
        regex=re.compile(r"\bsk-proj-[A-Za-z0-9_\-]{20,}"),
        severity="critical",
    ),
    SecretPattern(
        name="connection_string",
        regex=re.compile(
            r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb|rediss?|amqps?)(?:\+\w+)?://"
            r"[^\s:@/'\"]*:(?P<value>[^\s@/'\"]+)@(?P<host>\[[^\]\s]+\]|[^\s:/'\"?#]+)"
        ),
        severity="high",
        accept=_is_remote_credential,
    ),
    SecretPattern(
        name="generic_api_key",
        regex=re.compile(_API_KEY_NAMES + _QUOTED_VALUE, re.IGNORECASE),
        severity="high",
    ),
    SecretPattern(
        name="generic_api_key",
        regex=re.compile(_API_KEY_NAMES + _BARE_VALUE, re.IGNORECASE),
        severity="high",
        config_only=True,
    ),
    SecretPattern(
        name="generic_token",
        regex=re.compile(_TOKEN_NAMES + _QUOTED_VALUE, re.IGNORECASE),
        severity="high",
    ),
    SecretPattern(
        name="generic_token",
        regex=re.compile(_TOKEN_NAMES + _BARE_VALUE, re.IGNORECASE),
        severity="high",
        config_only=True,
    ),
]

# ---------------------------------------------------------------------------
# Entropy helpers
# ---------------------------------------------------------------------------

_BASE64_RE = re.compile(r"[A-Za-z0-9+/=]{40,}")
_HEX_RE = re.compile(r"[0-9a-fA-F]{40,}")
_ASSIGNMENT_CONTEXT_RE = re.compile(r"""(?i)(?:key|secret|token|password|credential|auth)\s*[:=]""")


def _shannon_entropy(data: str) -> float:
    """Calculate Shannon entropy of a string."""
    if not data:
        return 0.0
    length = len(data)
    freq: dict[str, int] = {}
    for ch in data:
        freq[ch] = freq.get(ch, 0) + 1
    return -sum((count / length) * math.log2(count / length) for count in freq.values())


def _high_entropy_start(line: str) -> int | None:
    """Return where a high-entropy string starts if the line assigns one to a
    secret-ish name, otherwise None."""
    # Only flag if the line looks like an assignment to a secret-ish variable
    if not _ASSIGNMENT_CONTEXT_RE.search(line):
        return None

    for match in _BASE64_RE.finditer(line):
        if _shannon_entropy(match.group()) >= 4.5:
            return match.start()

    for match in _HEX_RE.finditer(line):
        if _shannon_entropy(match.group()) >= 3.5:
            return match.start()

    return None


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def _redact(line: str, secret_start: int, max_prefix: int = 60) -> str:
    """Keep the text before the secret (at most *max_prefix* characters) and
    the secret's first four characters."""
    prefix = line[:secret_start].lstrip()[-max_prefix:]
    return prefix + line[secret_start : secret_start + 4] + "****REDACTED****"


# ---------------------------------------------------------------------------
# Core scanning
# ---------------------------------------------------------------------------


def _is_config_file(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return name.startswith(".env") or name.endswith(_CONFIG_SUFFIXES)


def _scan_line(path: str, lineno: int, line: str) -> SecretFinding | None:
    """Return the first finding on *line*, or None."""
    is_config = _is_config_file(path)
    for pattern in _PATTERNS:
        if pattern.config_only and not is_config:
            continue
        has_value = "value" in pattern.regex.groupindex
        for match in pattern.regex.finditer(line):
            if pattern.accept is None or pattern.accept(match):
                return SecretFinding(
                    pattern_name=pattern.name,
                    line_number=lineno,
                    line_content=_redact(line, match.start("value" if has_value else 0)),
                    severity=pattern.severity,
                    file=path,
                )

    start = _high_entropy_start(line)
    if start is None:
        return None
    return SecretFinding(
        pattern_name="high_entropy_string",
        line_number=lineno,
        line_content=_redact(line, start),
        severity="medium",
        file=path,
    )


def scan_patch(
    patch: Mapping[str, Any],
    *,
    allowlist: list[str] | None = None,
    added: AddedLines | None = None,
) -> list[SecretFinding]:
    """Scan the lines a patch adds for secret patterns.

    Parameters
    ----------
    patch:
        A PatchSet dict with ``files`` (path -> content) and/or ``diff_unified``.
    allowlist:
        Regex patterns to ignore. If a line matches any allowlist pattern,
        it is skipped.
    added:
        Precomputed :func:`~delegatron.security.patch_lines.added_lines`.
        Pass it with the workspace root so lines already in the workspace
        are not scanned; by default every line of ``files`` is scanned.

    Returns
    -------
    list[SecretFinding]
        At most one finding per line, sorted by file and line number.
    """
    lines = added if added is not None else added_lines(patch)
    compiled_allowlist = _compile_allowlist(allowlist)

    findings: list[SecretFinding] = []
    for path, entries in lines.items():
        for lineno, line in entries:
            if _is_allowlisted(line, compiled_allowlist):
                continue
            finding = _scan_line(path, lineno, line)
            if finding is not None:
                findings.append(finding)

    if findings:
        logger.warning("Secrets scan found %d potential secret(s) in patch", len(findings))

    return sorted(findings, key=lambda f: (f.file, f.line_number))


def has_secrets(
    patch: Mapping[str, Any],
    *,
    allowlist: list[str] | None = None,
) -> bool:
    """Return True if the patch contains any detected secrets."""
    return len(scan_patch(patch, allowlist=allowlist)) > 0


# ---------------------------------------------------------------------------
# Allowlist helpers
# ---------------------------------------------------------------------------


def _compile_allowlist(patterns: list[str] | None) -> list[re.Pattern[str]]:
    """Compile allowlist regex patterns, skipping invalid ones."""
    if not patterns:
        return []
    compiled: list[re.Pattern[str]] = []
    for pat in patterns:
        try:
            compiled.append(re.compile(pat))
        except re.error as e:
            logger.warning("Invalid allowlist pattern '%s': %s", pat, e)
    return compiled


def _is_allowlisted(line: str, compiled: list[re.Pattern[str]]) -> bool:
    """Check if a line matches any allowlist pattern."""
    return any(pat.search(line) for pat in compiled)
