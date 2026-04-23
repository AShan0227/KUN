"""Structured logging with structlog, OTel trace correlation.

日志格式 = JSON, 带租户 / trace_id / span_id 标签.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from opentelemetry import trace
from structlog.typing import EventDict, Processor

from kun.core.config import settings
from kun.core.tenancy import current_tenant


def _add_tenant(_logger: Any, _name: str, event_dict: EventDict) -> EventDict:
    try:
        ctx = current_tenant()
        event_dict.setdefault("tenant_id", ctx.tenant_id)
        if ctx.user_id:
            event_dict.setdefault("user_id", ctx.user_id)
    except LookupError:
        pass
    return event_dict


def _add_trace(_logger: Any, _name: str, event_dict: EventDict) -> EventDict:
    span = trace.get_current_span()
    if span.is_recording():
        ctx = span.get_span_context()
        event_dict.setdefault("trace_id", format(ctx.trace_id, "032x"))
        event_dict.setdefault("span_id", format(ctx.span_id, "016x"))
    return event_dict


def configure_logging() -> None:
    """Configure structlog once per process."""
    level = getattr(logging, settings().log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level)

    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        _add_tenant,
        _add_trace,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings().env == "dev":
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "kun") -> structlog.stdlib.BoundLogger:
    """Get a structured logger instance."""
    return structlog.get_logger(name)
