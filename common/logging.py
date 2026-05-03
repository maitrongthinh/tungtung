from __future__ import annotations

import logging
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Iterable

from common.models import LogEvent

_LOG_EVENTS: deque[LogEvent] = deque(maxlen=500)
_LOG_LOCK = Lock()
_CONFIGURED = False


class InMemoryLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        event = LogEvent(level=record.levelname, module=record.name, message=record.getMessage())
        with _LOG_LOCK:
            _LOG_EVENTS.append(event)


class ModuleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.module_name = record.name
        return super().format(record)


def configure_logging(log_dir: Path) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = ModuleFormatter("[%(asctime)s] [%(levelname)s] [%(module_name)s] %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    file_handler = logging.FileHandler(log_dir / "agent.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    memory_handler = InMemoryLogHandler()
    memory_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(memory_handler)
    root_logger.addHandler(stream_handler)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def get_recent_logs(limit: int = 200) -> list[LogEvent]:
    with _LOG_LOCK:
        return list(_LOG_EVENTS)[-limit:]


def iter_recent_logs() -> Iterable[LogEvent]:
    with _LOG_LOCK:
        return list(_LOG_EVENTS)
