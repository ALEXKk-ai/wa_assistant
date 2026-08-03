"""Structured logging.

Every log line emitted during a request carries the same correlation_id, set
once when a webhook call comes in and read via a contextvar everywhere else
(engine, workflows, ai.py, payments.py) - so you can grep one id and see a
single conversation turn's full path through the system, which is the
single highest-value observability feature for debugging a live bot without
needing a full tracing stack.
"""
import contextvars
import json
import logging
import sys
import uuid
from datetime import datetime, timezone

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)


def new_correlation_id() -> str:
    cid = uuid.uuid4().hex[:16]
    _correlation_id.set(cid)
    return cid


def get_correlation_id() -> str:
    return _correlation_id.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": get_correlation_id(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_extra(**fields) -> dict:
    """Helper: logger.info("msg", extra=log_extra(business_id=1))"""
    return {"extra_fields": fields}
