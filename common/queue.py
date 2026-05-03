from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Any

try:
    from redis import Redis
    from rq import Queue
    _HAS_REDIS = True
except ImportError:
    Redis = None  # type: ignore
    Queue = None  # type: ignore
    _HAS_REDIS = False

from common.config import load_settings

_QUEUE_NAMES = ("crawl", "analysis", "publish", "memory")
_LOCAL_LOCK = Lock()
_LOCAL_COUNTS = {name: 0 for name in _QUEUE_NAMES}
_LOCAL_EXECUTOR: ThreadPoolExecutor | None = None
_LOCAL_WORKERS: int | None = None


def _execution_mode() -> str:
    return load_settings(refresh=True).runtime.execution_mode.strip().lower() or "local"


def _is_local_mode() -> bool:
    return _execution_mode() == "local"


def _local_executor() -> ThreadPoolExecutor:
    global _LOCAL_EXECUTOR, _LOCAL_WORKERS
    workers = max(1, load_settings(refresh=True).runtime.local_queue_workers)
    with _LOCAL_LOCK:
        if _LOCAL_EXECUTOR is None or _LOCAL_WORKERS != workers:
            old = _LOCAL_EXECUTOR
            _LOCAL_EXECUTOR = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="agent-local")
            _LOCAL_WORKERS = workers
            if old is not None:
                # shutdown old executor in background — don't block the scheduler
                import threading
                threading.Thread(target=old.shutdown, kwargs={"wait": True, "cancel_futures": False}, daemon=True).start()
    return _LOCAL_EXECUTOR


def get_redis_connection() -> "Redis":
    if not _HAS_REDIS:
        raise RuntimeError("Redis not installed. Install with: pip install redis rq")
    return Redis.from_url(load_settings(refresh=True).integrations.redis_url)


def get_queue(name: str) -> Queue:
    return Queue(name, connection=get_redis_connection(), default_timeout=900)


def get_all_queues() -> list[Queue]:
    return [get_queue(name) for name in _QUEUE_NAMES]


def enqueue(name: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    if _is_local_mode():
        return _enqueue_local(name, func, *args, **kwargs)
    return get_queue(name).enqueue(func, *args, **kwargs)


def get_queue_stats() -> dict[str, int]:
    if _is_local_mode():
        with _LOCAL_LOCK:
            return dict(_LOCAL_COUNTS)
    stats: dict[str, int] = {}
    try:
        for name in _QUEUE_NAMES:
            stats[name] = len(get_queue(name))
    except Exception:
        return {name: 0 for name in _QUEUE_NAMES}
    return stats


def shutdown_local_executor() -> None:
    global _LOCAL_EXECUTOR
    if _LOCAL_EXECUTOR is not None:
        _LOCAL_EXECUTOR.shutdown(wait=True, cancel_futures=False)
        _LOCAL_EXECUTOR = None


def _enqueue_local(name: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
    if name not in _LOCAL_COUNTS:
        raise ValueError(f"Unknown queue: {name}")
    with _LOCAL_LOCK:
        _LOCAL_COUNTS[name] += 1
    return _local_executor().submit(_run_local_job, name, func, *args, **kwargs)


def _run_local_job(name: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return func(*args, **kwargs)
    finally:
        with _LOCAL_LOCK:
            _LOCAL_COUNTS[name] = max(0, _LOCAL_COUNTS[name] - 1)
