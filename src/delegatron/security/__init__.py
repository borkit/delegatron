"""Security modules — secrets scanning and dependency auditing."""

from delegatron.security.secrets import (
    SecretFinding,
    SecretPattern,
    has_secrets,
    scan_patch,
)

__all__ = [
    "SecretFinding",
    "SecretPattern",
    "has_secrets",
    "scan_patch",
]
