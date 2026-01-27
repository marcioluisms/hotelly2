"""Structured JSON logging with correlation ID support."""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from .correlation import get_correlation_id


class JsonFormatter(logging.Formatter):
    """JSON formatter that includes correlation ID."""

    def format(self, record: logging.LogRecord) -> str:
        log_obj: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        correlation_id = get_correlation_id()
        if correlation_id:
            log_obj["correlationId"] = correlation_id

        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)

        # Include extra fields if present
        if hasattr(record, "extra_fields"):
            log_obj.update(record.extra_fields)

        return json.dumps(log_obj, default=str)


def get_logger(name: str) -> logging.Logger:
    """Get a logger configured for JSON output."""
    logger = logging.getLogger(name)

    # Only configure if no handlers (avoid duplicate handlers)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

    return logger
