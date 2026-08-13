# src/Infrastructure/logger.py
import logging
import os
import uuid
from contextvars import ContextVar
from src.config import get_settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def set_request_id(req_id: str | None = None) -> str:
    """Sets a correlation request ID into ContextVar context."""
    val = req_id or f"req-{uuid.uuid4().hex[:8]}"
    request_id_var.set(val)
    return val


def get_request_id() -> str:
    """Gets the active correlation request ID from ContextVar."""
    return request_id_var.get()


class CorrelationIdFilter(logging.Filter):
    """Logging filter that injects `request_id` into log record formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def setup_logging() -> None:
    """Configures centralized togglable file and console logging."""
    settings = get_settings()

    log_level_str = (settings.log_level or "DEBUG").upper()
    level = getattr(logging, log_level_str, logging.DEBUG)

    formatter = logging.Formatter(
        fmt="%(asctime)s.%(milliSeconds)03d [%(levelname)s] [%(request_id)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Custom Formatter subclass to compute milliSeconds string correctly
    class PrecisionFormatter(logging.Formatter):
        def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
            ct = self.converter(record.created)
            t = logging.Formatter.formatTime(self, record, datefmt)
            msecs = int(record.msecs)
            return f"{t}.{msecs:03d}"

    precision_formatter = PrecisionFormatter(
        fmt="%(asctime)s [%(levelname)s] [%(request_id)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger("receipt_api")
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    corr_filter = CorrelationIdFilter()
    root_logger.addFilter(corr_filter)

    # File Handler
    if settings.enable_file_logging:
        log_file = os.path.abspath(settings.log_file_path)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(precision_formatter)
        file_handler.addFilter(corr_filter)
        root_logger.addHandler(file_handler)

    # Console Handler
    if settings.enable_console_logging:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_handler.setFormatter(precision_formatter)
        console_handler.addFilter(corr_filter)
        root_logger.addHandler(console_handler)


def get_logger(name: str) -> logging.Logger:
    """Returns a logger instance prefixed with receipt_api."""
    return logging.getLogger(f"receipt_api.{name}")
