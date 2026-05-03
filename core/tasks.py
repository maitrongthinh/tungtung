from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from common.logging import get_logger
from common.queue import get_queue_stats

logger = get_logger(__name__)

# ── Singleton runtime to avoid rebuilding on every job ─────────
_runtime_bundle: Any = None
_runtime_lock = asyncio.Lock() if hasattr(asyncio, "Lock") else None


def _get_runtime() -> Any:
    """Return shared runtime bundle, creating it only once."""
    global _runtime_bundle
    if _runtime_bundle is None:
        from core.bootstrap import build_runtime
        _runtime_bundle = build_runtime()
    return _runtime_bundle


def _run_async(coro: Any) -> Any:
    """Run an async coroutine safely, reusing existing event loop if available."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # We are inside an event loop (e.g. APScheduler async context)
        # Use a new thread to run the coroutine
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result()
    return asyncio.run(coro)


def prepare_cycle_job() -> None:
    runtime = _get_runtime()
    runtime.daily_planner.generate()
    _run_async(runtime.orchestrator.run_cycle("full"))


def publish_cycle_job() -> None:
    runtime = _get_runtime()
    _run_async(runtime.orchestrator.run_cycle("publish_only"))


def wrap_up_cycle_job() -> None:
    runtime = _get_runtime()
    _run_async(runtime.orchestrator.run_cycle("wrap_up"))


def compact_memory_job() -> None:
    runtime = _get_runtime()
    runtime.orchestrator.compactor.compact_day()


def verify_pre_window_job() -> None:
    runtime = _get_runtime()
    accounts = runtime.session_manager.load_accounts()
    health = _run_async(runtime.session_manager.verify_accounts(accounts))
    proxy_details = _run_async(runtime.proxy_pool.health_check()) if runtime.proxy_pool.proxies else {}
    status = runtime.database.get_runtime_status()
    status.account_health = health
    status.proxy_health = {**runtime.proxy_pool.summary(), "details": proxy_details}
    status.queue_stats = get_queue_stats()
    status.updated_at = datetime.now(UTC)
    status.message = "pre-window verification complete"
    runtime.database.save_runtime_status(status)
    logger.info("Verified accounts before window")


def cleanup_storage_job() -> None:
    runtime = _get_runtime()
    runtime.daily_planner.generate()
    cleanup_result = runtime.farm_manager.cleanup_storage(
        asset_retention_days=runtime.settings.storage.asset_retention_days,
        temp_dir=runtime.settings.temp_dir,
        temp_retention_hours=runtime.settings.storage.temp_retention_hours,
    )
    runtime.database.purge_expired_cache()
    runtime.database.purge_processed_commands(runtime.settings.storage.processed_command_retention_days)
    deleted_logs = runtime.database.purge_old_activity_log(retention_days=60)
    logger.info("Cleanup complete: %s | activity_log_deleted=%d", cleanup_result, deleted_logs)


def monitor_recent_posts_job() -> None:
    from core.orchestrator import CycleState
    runtime = _get_runtime()
    state = CycleState(mode="monitor")
    _run_async(runtime.orchestrator.monitor_comments(state))


def sync_affiliate_links_job() -> None:
    runtime = _get_runtime()
    _run_async(runtime.orchestrator.sync_affiliate_links())
