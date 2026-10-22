"""Structured logging setup for Delegatron."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from typing import Any, ClassVar

# Default log directory — resolve relative to project root (where pyproject.toml lives)
# so logs always land in <project>/logs/ regardless of the process's CWD.
# Path: src/delegatron/utils/logging.py -> parents[3] is project root
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LOG_DIR = (
    Path(os.environ.get("DELEGATRON_LOG_DIR", ""))
    if os.environ.get("DELEGATRON_LOG_DIR")
    else _PROJECT_ROOT / "logs"
)

# Size-based rotation: max file size before rotating
MAX_LOG_BYTES = (
    int(
        os.environ.get(
            "DELEGATRON_LOG_MAX_MB",
            "100",
        )
    )
    * 1024
    * 1024
)

# Time-based rotation: rotate daily, keep N days
LOG_ROTATE_DAYS = int(os.environ.get("DELEGATRON_LOG_ROTATE_DAYS", "7"))

# Which rotation strategy to use: "time" (daily) or "size"
LOG_ROTATE_MODE = os.environ.get("DELEGATRON_LOG_ROTATE", "time")

# Timestamp mode: "utc" (default) or "local"
LOG_TIMEZONE = os.environ.get("DELEGATRON_LOG_TIMEZONE", "utc")


class StructuredFormatter(logging.Formatter):
    """JSON-lines formatter for structured log output."""

    def format(self, record: logging.LogRecord) -> str:
        if LOG_TIMEZONE == "local":
            ts = datetime.now().astimezone().isoformat()
        else:
            ts = datetime.now(UTC).isoformat()

        log_entry: dict[str, Any] = {
            "timestamp": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Include extra fields if present
        if hasattr(record, "run_id"):
            log_entry["run_id"] = record.run_id
        if hasattr(record, "iteration"):
            log_entry["iteration"] = record.iteration

        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = str(record.exc_info[1])

        return json.dumps(log_entry)


class RichConsoleHandler(logging.Handler):
    """Human-friendly console output using Rich."""

    LEVEL_STYLES: ClassVar[dict[str, str]] = {
        "DEBUG": "[dim]",
        "INFO": "[blue]",
        "WARNING": "[yellow]",
        "ERROR": "[red bold]",
        "CRITICAL": "[red bold reverse]",
    }

    def emit(self, record: logging.LogRecord) -> None:
        """Print *record*; its message is text, never Rich markup (git stderr has ``[/...]``)."""
        try:
            from rich.console import Console
            from rich.markup import escape
        except ImportError:
            # Fallback if rich not available
            print(
                f"{record.levelname}: {record.getMessage()}",
                file=sys.stderr,
            )
            return
        try:
            console = Console(stderr=True)
            style = self.LEVEL_STYLES.get(record.levelname, "")
            console.print(f"{style}{record.levelname}[/] {escape(record.getMessage())}")
        except Exception:
            self.handleError(record)


def setup_logging(
    level: str = "INFO",
    log_file: Path | None = None,
    json_output: bool = False,
    enable_file_logging: bool = True,
) -> None:
    """Configure logging for Delegatron.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR).
        log_file: Explicit file path for log output.
        json_output: If True, use JSON format for console output too.
        enable_file_logging: Auto-create rolling log files in logs/ dir.
    """
    root_logger = logging.getLogger("delegatron")
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Prevent the root logger (or uvicorn's reconfiguration) from interfering.
    root_logger.propagate = False

    # Remove only console handlers; preserve file handlers that
    # setup_server_logging() may have already attached.
    root_logger.handlers = [
        h
        for h in root_logger.handlers
        if isinstance(h, (RotatingFileHandler, TimedRotatingFileHandler))
    ]

    # Console handler
    console_handler: logging.Handler
    if json_output:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(StructuredFormatter())
    else:
        console_handler = RichConsoleHandler()

    root_logger.addHandler(console_handler)

    # File handler — explicit log_file takes priority
    if log_file:
        _add_file_handler(root_logger, log_file)
    elif enable_file_logging:
        # Only add the delegatron.log handler if one isn't already present
        delegatron_log = DEFAULT_LOG_DIR / "delegatron.log"
        already_has = any(
            isinstance(h, (RotatingFileHandler, TimedRotatingFileHandler))
            and getattr(h, "baseFilename", None) == str(delegatron_log.resolve())
            for h in root_logger.handlers
        )
        if not already_has:
            _add_file_handler(root_logger, delegatron_log)


def _add_file_handler(logger: logging.Logger, path: Path) -> logging.Handler | None:
    """Attach a rotating file handler for *path*; stay console-only if it can't be written.

    An unwritable log directory (e.g. a read-only install location) must not
    stop the CLI or the server from starting.
    """
    try:
        handler = _make_file_handler(path)
    except OSError as e:
        logger.warning(
            "File logging disabled: cannot write %s (%s). "
            "Set DELEGATRON_LOG_DIR to a writable directory.",
            path,
            e,
        )
        return None
    logger.addHandler(handler)
    return handler


def _make_file_handler(path: Path) -> logging.Handler:
    """Create a rotating file handler based on configuration.

    Rotation mode controlled by DELEGATRON_LOG_ROTATE env var:
    - "time" (default): rotate daily, keep DELEGATRON_LOG_ROTATE_DAYS (7)
    - "size": rotate at DELEGATRON_LOG_MAX_MB (100MB), keep 5 backups
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    formatter = StructuredFormatter()

    if LOG_ROTATE_MODE == "size":
        handler: logging.Handler = RotatingFileHandler(
            path,
            maxBytes=MAX_LOG_BYTES,
            backupCount=5,
        )
    else:
        # Time-based: rotate at midnight, keep N days
        handler = TimedRotatingFileHandler(
            path,
            when="midnight",
            backupCount=LOG_ROTATE_DAYS,
            utc=LOG_TIMEZONE != "local",
        )

    handler.setFormatter(formatter)
    return handler


def setup_server_logging(level: str = "INFO") -> None:
    """Configure logging for the server process.

    Writes to logs/server.log with rotation. Also logs uvicorn
    and fastapi loggers to the same file.

    Safe to call multiple times — duplicate handlers are not added.
    """
    server_log = DEFAULT_LOG_DIR / "server.log"

    # Configure delegatron logger
    root_logger = logging.getLogger("delegatron")
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Prevent uvicorn's dictConfig from interfering with our logger.
    root_logger.propagate = False

    # Only add the server.log handler if one isn't already present
    already_has_server = any(
        isinstance(h, (RotatingFileHandler, TimedRotatingFileHandler))
        and getattr(h, "baseFilename", None) == str(server_log.resolve())
        for h in root_logger.handlers
    )
    server_handler: logging.Handler | None
    if not already_has_server:
        server_handler = _add_file_handler(root_logger, server_log)
    else:
        # Reuse existing handler for uvicorn loggers below
        server_handler = next(
            h
            for h in root_logger.handlers
            if isinstance(h, (RotatingFileHandler, TimedRotatingFileHandler))
            and getattr(h, "baseFilename", None) == str(server_log.resolve())
        )

    # Ensure a console handler exists (setup_logging may not have run yet)
    has_console = any(
        isinstance(h, (RichConsoleHandler, logging.StreamHandler))
        and not isinstance(h, (RotatingFileHandler, TimedRotatingFileHandler))
        for h in root_logger.handlers
    )
    if not has_console:
        root_logger.addHandler(RichConsoleHandler())

    # Also capture uvicorn logs to the server log file
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi"):
        uvi_logger = logging.getLogger(name)
        already_has = any(
            isinstance(h, (RotatingFileHandler, TimedRotatingFileHandler))
            and getattr(h, "baseFilename", None) == str(server_log.resolve())
            for h in uvi_logger.handlers
        )
        if not already_has and server_handler is not None:
            uvi_logger.addHandler(server_handler)
