from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from common.logging import get_logger
from common.queue import get_queue_stats
from core.bootstrap import build_runtime

logger = get_logger(__name__)


def prepare_cycle_job() -> None:
    runtime = build_runtime()
    runtime.daily_planner.generate()
    asyncio.run(runtime.orchestrator.run_cycle("full"))


def publish_cycle_job() -> None:
    runtime = build_runtime()
    asyncio.run(runtime.orchestrator.run_cycle("publish_only"))


def wrap_up_cycle_job() -> None:
    runtime = build_runtime()
    asyncio.run(runtime.orchestrator.run_cycle("wrap_up"))


def compact_memory_job() -> None:
    runtime = build_runtime()
    runtime.orchestrator.compactor.compact_day()


def verify_pre_window_job() -> None:
    runtime = build_runtime()
    accounts = runtime.session_manager.load_accounts()
    health = asyncio.run(runtime.session_manager.verify_accounts(accounts))
    proxy_details = asyncio.run(runtime.proxy_pool.health_check()) if runtime.proxy_pool.proxies else {}
    status = runtime.database.get_runtime_status()
    status.account_health = health
    status.proxy_health = {**runtime.proxy_pool.summary(), "details": proxy_details}
    status.queue_stats = get_queue_stats()
    status.updated_at = datetime.now(UTC)
    status.message = "pre-window verification complete"
    runtime.database.save_runtime_status(status)
    logger.info("Verified accounts before window")


def cleanup_storage_job() -> None:
    runtime = build_runtime()
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
    runtime = build_runtime()
    state = CycleState(mode="monitor")
    asyncio.run(runtime.orchestrator.monitor_comments(state))


def sync_affiliate_links_job() -> None:
    runtime = build_runtime()
    asyncio.run(runtime.orchestrator.sync_affiliate_links())
