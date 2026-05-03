from __future__ import annotations

import asyncio

from common.database import Database
from common.logging import get_logger
from common.queue import shutdown_local_executor
from common.runtime import install_signal_handlers, runtime_control
from core.bootstrap import build_runtime
from core.scheduler import AgentScheduler

logger = get_logger(__name__)


class DailyLoopController:
    def __init__(self, database: Database, scheduler: AgentScheduler) -> None:
        self.database = database
        self.scheduler = scheduler

    async def run_forever(self) -> None:
        install_signal_handlers()
        self.scheduler.start()
        logger.info("Daily loop controller is running")
        # Trigger initial prepare cycle ngay khi khởi động (không đợi cron)
        asyncio.create_task(self._initial_startup_cycle())
        try:
            while not runtime_control.shutdown_requested.is_set():
                await self._process_commands()
                await self.scheduler.update_heartbeat(paused=runtime_control.is_paused())
                await asyncio.sleep(5)
        finally:
            logger.info("Graceful shutdown started")
            await self.scheduler.shutdown()
            shutdown_local_executor()
            # Close shared Playwright browser
            try:
                from modules.shopee.crawler import close_shared_browser
                await close_shared_browser()
                logger.info("Shared browser closed")
            except Exception:
                pass

    async def _initial_startup_cycle(self) -> None:
        """Chạy một prepare cycle ngay sau khi khởi động để bot làm việc liền."""
        await asyncio.sleep(10)   # Đợi các service khởi tạo xong
        if runtime_control.is_paused():
            logger.info("Startup cycle skipped — agent is paused")
            return
        logger.info("Running initial startup prepare cycle")
        await self.scheduler.run_prepare_cycle()

    async def _process_commands(self) -> None:
        commands = self.database.fetch_pending_commands()
        for command in commands:
            name = command["command"]
            if name == "pause_agent":
                runtime_control.pause()
                logger.info("Agent paused via command queue")
            elif name == "resume_agent":
                runtime_control.resume()
                logger.info("Agent resumed via command queue")
            elif name == "force_crawl":
                if runtime_control.is_paused():
                    runtime_control.resume()
                await self.scheduler.run_prepare_cycle()
            elif name == "reload_settings":
                await self.scheduler.reload_settings()
                logger.info("Scheduler settings reloaded via command queue")
            self.database.mark_command_processed(command["id"])


def main() -> None:
    runtime = build_runtime()
    scheduler = AgentScheduler(
        database=runtime.database,
        session_manager=runtime.session_manager,
        proxy_pool=runtime.proxy_pool,
    )
    controller = DailyLoopController(runtime.database, scheduler)
    asyncio.run(controller.run_forever())


if __name__ == "__main__":
    main()
