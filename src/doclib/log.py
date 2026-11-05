"""Structured internal logging: JSON to stdout, with an optional rotating file.

Every process logs what it does through `event()`. Document content must never be logged (spec
section 15), so `DocumentContentFilter` removes content-bearing fields whatever the caller passes.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import sys
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

MAX_SNIPPET = 200
# Field names that carry document text. Dropped above DEBUG, trimmed to a snippet at DEBUG.
CONTENT_FIELDS = frozenset({"text", "content", "chunk_text", "body", "markdown", "snippet", "query"})

_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("doclib_log_ctx", default=None)
_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


def bind(**fields: Any) -> contextvars.Token:
    """Attach fields (file_id, source_id, job_id, ...) to every log line in this context."""
    return _context.set({**(_context.get() or {}), **fields})


def unbind(token: contextvars.Token) -> None:
    _context.reset(token)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "event": record.getMessage(),
        }
        entry.update(_context.get() or {})
        entry.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED})
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            entry["trace_id"] = format(span.trace_id, "032x")
            entry["span_id"] = format(span.span_id, "016x")
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


class DocumentContentFilter(logging.Filter):
    """Backstop for the no-content rule: applies to all handlers."""

    def filter(self, record: logging.LogRecord) -> bool:
        for name in CONTENT_FIELDS & set(record.__dict__):
            value = record.__dict__[name]
            if record.levelno <= logging.DEBUG and isinstance(value, str):
                record.__dict__[name] = value[:MAX_SNIPPET]
            else:
                del record.__dict__[name]
        return True


def setup_logging(service: str, level: str = "INFO", file: str | None = None,
                  file_max_mb: int = 100, file_backups: int = 5) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.filters.clear()
    root.setLevel(level.upper())
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if file:
        handlers.append(logging.handlers.RotatingFileHandler(
            file, maxBytes=file_max_mb * 1024 * 1024, backupCount=file_backups, encoding="utf-8"))
    formatter = JsonFormatter(service)
    content_filter = DocumentContentFilter()
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(content_filter)
        root.addHandler(handler)
    # Library request logs are replaced by our own structured access log.
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def event(name: str, *, level: int = logging.INFO, logger: str = "doclib", exc_info: Any = None, **fields: Any) -> None:
    # logging raises on a field named like a LogRecord attribute, and a log line must never break the work it reports.
    safe = {f"{key}_" if key in _RESERVED else key: value for key, value in fields.items()}
    logging.getLogger(logger).log(level, name, extra=safe, exc_info=exc_info)


def safe_error(exc: BaseException, limit: int = 200) -> str:
    """Short error text for the database and webhooks: exception messages may quote document text."""
    return f"{type(exc).__name__}: {str(exc)[:limit]}"
