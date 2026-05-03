from __future__ import annotations

from dataclasses import dataclass

from common.config import load_settings
from common.database import Database
from common.farm import FarmManager
from common.logging import configure_logging
from core.orchestrator import AgentOrchestrator
from modules.ai.analyzer import ProductAnalyzer
from modules.ai.writer import ContentWriter
from modules.memory.compactor import ContextCompactor
from modules.memory.daily_planner import DailyPlanner
from modules.memory.improvement_updater import ImprovementUpdater
from modules.meta.monitor import MetaMonitor
from modules.meta.publisher import MetaPublisher
from modules.meta.session_manager import MetaSessionManager
from modules.shopee.affiliate_api import ShopeeAffiliateAPI
from modules.shopee.crawler import ShopeeCrawler
from modules.shopee.proxy_pool import ProxyPool
from modules.shopee.rate_limiter import TokenBucketRateLimiter


@dataclass(slots=True)
class RuntimeBundle:
    database: Database
    farm_manager: FarmManager
    session_manager: MetaSessionManager
    proxy_pool: ProxyPool
    settings: object
    orchestrator: AgentOrchestrator
    daily_planner: DailyPlanner


def build_runtime() -> RuntimeBundle:
    settings = load_settings()
    configure_logging(settings.log_dir)
    database = Database(settings.sqlite_path)
    farm_manager = FarmManager()
    session_manager = MetaSessionManager()
    rate_limiter = TokenBucketRateLimiter(settings.shopee.rate_limit_per_second)
    proxy_pool = ProxyPool(rotate_every=settings.shopee.proxy_rotate_every)
    affiliate_api = ShopeeAffiliateAPI(database)
    crawler = ShopeeCrawler(rate_limiter, proxy_pool, affiliate_api)
    analyzer = ProductAnalyzer(database=database)
    writer = ContentWriter(database=database)
    publisher = MetaPublisher()
    monitor = MetaMonitor(database=database)
    improvement = ImprovementUpdater(database)
    compactor = ContextCompactor(database)
    daily_planner = DailyPlanner(database)
    orchestrator = AgentOrchestrator(
        database=database,
        crawler=crawler,
        analyzer=analyzer,
        writer=writer,
        publisher=publisher,
        monitor=monitor,
        session_manager=session_manager,
        improvement=improvement,
        compactor=compactor,
        farm_manager=farm_manager,
    )
    return RuntimeBundle(
        database=database,
        farm_manager=farm_manager,
        session_manager=session_manager,
        proxy_pool=proxy_pool,
        settings=settings,
        orchestrator=orchestrator,
        daily_planner=daily_planner,
    )
