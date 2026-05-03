import time
from types import SimpleNamespace

from common import queue as queue_module


def test_local_queue_executes_without_redis(monkeypatch) -> None:
    fake_settings = SimpleNamespace(
        runtime=SimpleNamespace(execution_mode="local", local_queue_workers=1),
        integrations=SimpleNamespace(redis_url="redis://unused"),
    )
    monkeypatch.setattr("common.queue.load_settings", lambda *args, **kwargs: fake_settings)
    queue_module.shutdown_local_executor()
    for key in ("crawl", "analysis", "publish", "memory"):
        queue_module._LOCAL_COUNTS[key] = 0

    future = queue_module.enqueue("crawl", time.sleep, 0.05)
    stats_during = queue_module.get_queue_stats()
    assert stats_during["crawl"] >= 1
    future.result(timeout=2)
    stats_after = queue_module.get_queue_stats()
    assert stats_after["crawl"] == 0
    queue_module.shutdown_local_executor()
