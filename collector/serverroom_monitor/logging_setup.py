"""Structured logging to journald.

Under systemd, records go straight to the journal with extra fields
(``journalctl -u serverroom-monitor -o verbose`` shows them, and you can filter
e.g. ``journalctl -u serverroom-monitor EVENT=reading``). Elsewhere, logs go to
stderr in logfmt.

Attach fields with ``log.info("msg", extra={"fields": {"temperature": 22.1}})``.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

_PRIORITY = {
    logging.CRITICAL: 2,
    logging.ERROR: 3,
    logging.WARNING: 4,
    logging.INFO: 6,
    logging.DEBUG: 7,
}


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    fields = getattr(record, "fields", None)
    return dict(fields) if isinstance(fields, dict) else {}


class LogfmtFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        parts = [
            f"level={record.levelname.lower()}",
            f"logger={record.name}",
            f"msg={_quote(record.getMessage())}",
        ]
        parts += [f"{k}={_quote(str(v))}" for k, v in _fields(record).items()]
        if record.exc_info:
            parts.append(f"exc={_quote(self.formatException(record.exc_info))}")
        return " ".join(parts)


def _quote(value: str) -> str:
    if value and not any(c in value for c in ' "=\n\t'):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


class JournalHandler(logging.Handler):
    """Send records to journald with custom fields (uses python3-systemd)."""

    def __init__(self, identifier: str) -> None:
        super().__init__()
        from systemd import journal  # type: ignore[attr-defined,unused-ignore]  # noqa: PLC0415

        self._send = journal.send
        self._identifier = identifier

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + logging.Formatter().formatException(record.exc_info)
            extra = {k.upper(): str(v) for k, v in _fields(record).items()}
            self._send(
                message,
                PRIORITY=str(_PRIORITY.get(record.levelno, 6)),
                SYSLOG_IDENTIFIER=self._identifier,
                LOGGER=record.name,
                CODE_FILE=record.pathname,
                CODE_LINE=str(record.lineno),
                CODE_FUNC=record.funcName,
                **extra,
            )
        except Exception:
            self.handleError(record)


def setup_logging(level: str = "INFO", identifier: str = "serverroom-monitor") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    handler: logging.Handler
    if os.environ.get("JOURNAL_STREAM"):
        try:
            handler = JournalHandler(identifier)
        except ImportError:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(LogfmtFormatter())
    else:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(LogfmtFormatter())
    root.addHandler(handler)
    # urllib3 logs every retry at WARNING; the collector logs failures itself.
    logging.getLogger("urllib3").setLevel(logging.ERROR)
