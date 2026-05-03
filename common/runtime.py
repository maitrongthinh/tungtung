from __future__ import annotations

import signal
from asyncio import Event
from threading import Lock


class RuntimeControl:
    def __init__(self) -> None:
        self.pause_flag = False
        self.shutdown_requested = Event()
        self._lock = Lock()

    def pause(self) -> None:
        with self._lock:
            self.pause_flag = True

    def resume(self) -> None:
        with self._lock:
            self.pause_flag = False

    def is_paused(self) -> bool:
        with self._lock:
            return self.pause_flag

    def request_shutdown(self) -> None:
        self.shutdown_requested.set()


runtime_control = RuntimeControl()


def install_signal_handlers() -> None:
    def _handle_signal(_signum: int, _frame: object) -> None:
        runtime_control.request_shutdown()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
