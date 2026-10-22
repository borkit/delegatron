"""Tests for the Rich console log handler."""

from __future__ import annotations

import logging

import pytest

from delegatron.utils import logging as log_utils
from delegatron.utils.logging import RichConsoleHandler


def _record(message: str, level: int = logging.WARNING) -> logging.LogRecord:
    return logging.LogRecord("t", level, __file__, 1, message, None, None)


def test_message_text_is_never_read_as_markup(capsys: pytest.CaptureFixture[str]) -> None:
    # git stderr can hold "[/...]": as markup it raised MarkupError out of
    # logger.warning (turning a server GET into a 500).
    RichConsoleHandler().emit(_record("git said: [/refs/heads/x] closing tag [bold]"))

    err = capsys.readouterr().err
    assert "[/refs/heads/x] closing tag [bold]" in err
    assert "WARNING" in err


def test_a_failing_print_goes_to_handle_error(monkeypatch: pytest.MonkeyPatch) -> None:
    handled: list[logging.LogRecord] = []
    handler = RichConsoleHandler()
    monkeypatch.setattr(handler, "handleError", handled.append)
    monkeypatch.setattr("rich.console.Console.print", lambda *a, **k: 1 / 0)

    handler.emit(_record("boom"))

    assert len(handled) == 1


class TestUnwritableLogDir:
    """An unwritable log directory leaves logging console-only instead of crashing."""

    @pytest.fixture(autouse=True)
    def _isolated_logger(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("delegatron", "uvicorn", "uvicorn.access", "uvicorn.error", "fastapi"):
            monkeypatch.setattr(logging.getLogger(name), "handlers", [])

        def _deny(path: object) -> logging.Handler:
            raise PermissionError(13, "Permission denied", str(path))

        monkeypatch.setattr(log_utils, "_make_file_handler", _deny)

    def test_cli_logging_starts_console_only(self, capsys: pytest.CaptureFixture[str]) -> None:
        # setup_logging replaces console handlers, so read the warning from stderr.
        log_utils.setup_logging(enable_file_logging=True)

        logger = logging.getLogger("delegatron")
        assert not any(isinstance(h, logging.FileHandler) for h in logger.handlers)
        err = " ".join(capsys.readouterr().err.split())
        assert "File logging disabled" in err
        assert "DELEGATRON_LOG_DIR" in err

    def test_server_logging_starts_console_only(self) -> None:
        log_utils.setup_server_logging()

        assert not any(
            isinstance(h, logging.FileHandler) for h in logging.getLogger("uvicorn").handlers
        )
        # Some console handler remains (pytest's own capture handlers also count).
        assert any(
            isinstance(h, (RichConsoleHandler, logging.StreamHandler))
            and not isinstance(h, logging.FileHandler)
            for h in logging.getLogger("delegatron").handlers
        )
