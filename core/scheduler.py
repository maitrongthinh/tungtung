from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from common.config import load_settings
from common.database import Database
from common.logging import get_logger
from common.models import AgentRuntimeStatus
from common.queue import enqueue
from common.queue import get_queue_stats
from common.runtime import runtime_control
from core import tasks
from modules.meta.session_manager import MetaSessionManager
from modules.shopee.proxy_pool import ProxyPool

logger = get_logger(__name__)


class AgentScheduler:
    def __init__(
        self,
        database: Database,
        session_manager: MetaSessionManager,
        proxy_pool: ProxyPool,
    ) -> None:
        self.settings = load_settings()
        self.database = database
        self.session_manager = session_manager
        self.proxy_pool = proxy_pool
        self.scheduler = AsyncIOScheduler(timezone=datetime.now().astimezone().tzinfo)
        self._started = False
        self._register_jobs()

    def start(self) -> None:
        if self._started:
            return
        self.scheduler.start()
        self._started = True
        logger.info("Scheduler started")

    async def shutdown(self) -> None:
        if not self._started:
            return
        self.scheduler.shutdown(wait=True)
        self._started = False
        logger.info("Scheduler stopped")

    async def reload_settings(self) -> None:
        self.settings = load_settings(refresh=True)
        self.scheduler.remove_all_jobs()
        self._register_jobs()
        await self.update_heartbeat(paused=runtime_control.is_paused())
        logger.info("Scheduler settings reloaded")

    async def run_prepare_cycle(self) -> None:
        await self._enqueue_job("prepare", "crawl", tasks.prepare_cycle_job, "RUNNING")

    async def run_publish_cycle(self) -> None:
        await self._enqueue_job("publish_window", "publish", tasks.publish_cycle_job, "IN_WINDOW")

    async def run_monitor_cycle(self) -> None:
        await self._enqueue_job("monitor_window", "publish", tasks.monitor_recent_posts_job, "IN_WINDOW")

    async def run_wrap_up_cycle(self) -> None:
        await self._enqueue_job("wrap_up", "memory", tasks.wrap_up_cycle_job, "RUNNING")

    async def run_compaction(self) -> None:
        await self._enqueue_job("compact", "memory", tasks.compact_memory_job, "RUNNING")

    async def run_cleanup(self) -> None:
        await self._enqueue_job("cleanup", "memory", tasks.cleanup_storage_job, "SLEEPING")

    async def run_sync_affiliate_links(self) -> None:
        await self._enqueue_job("sync_links", "crawl", tasks.sync_affiliate_links_job, "RUNNING")

    async def run_refresh_proxies(self) -> None:
        from modules.shopee.proxy_scraper import refresh_proxy_pool
        try:
            result = await refresh_proxy_pool()
            logger.info("Proxy refresh result: %s", result)
            self.proxy_pool._sync_from_settings(force=True)
        except Exception as exc:
            logger.warning("Proxy refresh failed: %s", exc)

    async def verify_pre_window(self) -> None:
        accounts = self.session_manager.load_accounts()
        health = await self.session_manager.verify_accounts(accounts)
        proxy_details = await self.proxy_pool.health_check() if self.proxy_pool.proxies else {}
        status = self.database.get_runtime_status()
        status.account_health = health
        status.proxy_health = {**self.proxy_pool.summary(), "details": proxy_details}
        status.queue_stats = get_queue_stats()
        status.updated_at = datetime.now(UTC)
        self.database.save_runtime_status(status)
        logger.info("Completed pre-window verification")

    async def update_heartbeat(self, paused: bool = False) -> None:
        self.settings = load_settings(refresh=True)
        now = datetime.now().astimezone()
        within_window, current_window = self.session_manager.is_within_window(now)
        next_window, next_window_at = self.session_manager.next_window(now)
        kpi = self.database.get_daily_kpi(datetime.now(UTC))
        status_name = "PAUSED" if paused else ("IN_WINDOW" if within_window else ("SLEEPING" if 0 <= now.hour < self.settings.loop.crawl_start_hour else "RUNNING"))
        status = AgentRuntimeStatus(
            status=status_name,
            current_phase=current_window.name if current_window else "idle",
            next_window_name=next_window.name,
            next_window_at=next_window_at.astimezone(UTC),
            paused=paused,
            proxy_health=self.proxy_pool.summary(),
            account_health=self.database.get_runtime_status().account_health,
            queue_stats=get_queue_stats(),
            today_posts=kpi.get("posts_published", 0),
            target_posts=self.settings.kpi.posts_per_day,
            updated_at=datetime.now(UTC),
            message="scheduler heartbeat",
        )
        self.database.save_runtime_status(status)

    @staticmethod
    def _window_hour_range(start: str, end: str) -> str:
        """Parse 'HH:MM' thành cron hour range như '11-13'."""
        sh = int(start.split(":")[0])
        eh = int(end.split(":")[0])
        return str(sh) if sh == eh else f"{sh}-{eh}"

    @staticmethod
    def _pre_window_hour(start: str, offset_minutes: int = 10) -> tuple[int, int]:
        """Trả (hour, minute) của thời điểm trước window offset_minutes phút."""
        sh, sm = (int(p) for p in start.split(":"))
        total = sh * 60 + sm - offset_minutes
        return total // 60, total % 60

    def _register_jobs(self) -> None:
        self.settings = load_settings(refresh=True)
        m = self.settings.meta
        crawl_start = self.settings.loop.crawl_start_hour

        wa_range = self._window_hour_range(m.window_a_start, m.window_a_end)
        wb_range = self._window_hour_range(m.window_b_start, m.window_b_end)
        wa_sh = int(m.window_a_start.split(":")[0])
        wb_sh = int(m.window_b_start.split(":")[0])
        # Giờ crawl trước window a (từ crawl_start đến trước window_a)
        pre_a_hour = max(crawl_start, wa_sh - 1)
        # Giờ crawl giữa hai window (sau window_a kết thúc đến trước window_b)
        wa_eh = int(m.window_a_end.split(":")[0])
        wb_eh = int(m.window_b_end.split(":")[0])
        between_range = f"{wa_eh + 1}-{wb_sh - 1}" if wb_sh - wa_eh > 2 else str(wa_eh + 1)
        # Pre-window verification
        pre_a_h, pre_a_m = self._pre_window_hour(m.window_a_start, m.verify_before_window_minutes)
        pre_b_h, pre_b_m = self._pre_window_hour(m.window_b_start, m.verify_before_window_minutes)
        # Wrap-up sau window_b
        wrap_h = wb_eh + 1

        self.scheduler.add_job(self.run_compaction, CronTrigger(hour=self.settings.memory.compact_trigger_hour, minute=0), max_instances=1, coalesce=True)
        self.scheduler.add_job(self.run_cleanup, CronTrigger(hour=0, minute=15), max_instances=1, coalesce=True)
        self.scheduler.add_job(self.run_prepare_cycle, CronTrigger(hour=f"{crawl_start}-{pre_a_hour}", minute="0,30"), max_instances=1, coalesce=True)
        self.scheduler.add_job(self.run_prepare_cycle, CronTrigger(hour=between_range, minute="0"), max_instances=1, coalesce=True)
        self.scheduler.add_job(self.verify_pre_window, CronTrigger(hour=pre_a_h, minute=pre_a_m), max_instances=1, coalesce=True)
        self.scheduler.add_job(self.verify_pre_window, CronTrigger(hour=pre_b_h, minute=pre_b_m), max_instances=1, coalesce=True)
        self.scheduler.add_job(self.run_publish_cycle, CronTrigger(hour=wa_range, minute="*/5"), max_instances=1, coalesce=True)
        self.scheduler.add_job(self.run_publish_cycle, CronTrigger(hour=wb_range, minute="*/5"), max_instances=1, coalesce=True)
        self.scheduler.add_job(self.run_monitor_cycle, CronTrigger(hour=wa_range, minute="0,10,20,30,40,50"), max_instances=1, coalesce=True)
        self.scheduler.add_job(self.run_monitor_cycle, CronTrigger(hour=wb_range, minute="0,10,20,30,40,50"), max_instances=1, coalesce=True)
        self.scheduler.add_job(self.run_wrap_up_cycle, CronTrigger(hour=wrap_h, minute=15), max_instances=1, coalesce=True)
        self.scheduler.add_job(self.run_sync_affiliate_links, CronTrigger(minute=20), max_instances=1, coalesce=True)
        self.scheduler.add_job(self.run_refresh_proxies, CronTrigger(hour=crawl_start - 1 if crawl_start > 0 else 5, minute=30), max_instances=1, coalesce=True)

    async def _enqueue_job(
        self,
        phase: str,
        queue_name: str,
        job: Callable[[], object],
        status_name: str,
    ) -> None:
        if runtime_control.is_paused():
            logger.info("Skipping job %s because agent is paused", phase)
            await self.update_heartbeat(paused=True)
            return
        logger.info("Starting job %s", phase)
        status = self.database.get_runtime_status()
        status.status = status_name  # type: ignore[assignment]
        status.current_phase = phase
        status.updated_at = datetime.now(UTC)
        self.database.save_runtime_status(status)
        try:
            enqueue(queue_name, job)
        except Exception as exc:
            logger.exception("Scheduled job %s failed: %s", phase, exc)
            error_status = self.database.get_runtime_status()
            error_status.status = "ERROR"
            error_status.current_phase = phase
            error_status.message = str(exc)
            error_status.updated_at = datetime.now(UTC)
            self.database.save_runtime_status(error_status)
        else:
            await self.update_heartbeat()
