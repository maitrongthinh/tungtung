from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from common.config import load_settings
from common.database import Database
from common.farm import FarmManager
from common.links import build_tracking_link
from common.logging import get_logger
from common.models import AccountConfig, PostContent, PostRecord, ProductRecord
from modules.ai.analyzer import ProductAnalyzer
from modules.ai.fun_writer import FunContentWriter
from modules.ai.writer import ContentWriter
from modules.memory.compactor import ContextCompactor
from modules.memory.improvement_updater import ImprovementUpdater
from modules.meta.monitor import MetaMonitor
from modules.meta.publisher import MetaPublisher
from modules.meta.session_manager import MetaSessionManager
from modules.shopee.crawler import ShopeeCrawler

logger = get_logger(__name__)


@dataclass
class CycleState:
    mode: str = "full"
    categories: list[str] = field(default_factory=list)
    crawled_products: list[ProductRecord] = field(default_factory=list)
    scored_products: list[ProductRecord] = field(default_factory=list)
    drafted_posts: list[PostRecord] = field(default_factory=list)
    scheduled_posts: list[PostRecord] = field(default_factory=list)
    published_posts: list[PostRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class AgentOrchestrator:
    def __init__(
        self,
        database: Database,
        crawler: ShopeeCrawler,
        analyzer: ProductAnalyzer,
        writer: ContentWriter,
        publisher: MetaPublisher,
        monitor: MetaMonitor,
        session_manager: MetaSessionManager,
        improvement: ImprovementUpdater,
        compactor: ContextCompactor,
        farm_manager: FarmManager,
    ) -> None:
        self.settings = load_settings()
        self.database = database
        self.crawler = crawler
        self.analyzer = analyzer
        self.writer = writer
        self.publisher = publisher
        self.monitor = monitor
        self.session_manager = session_manager
        self.improvement = improvement
        self.compactor = compactor
        self.farm_manager = farm_manager

    async def run_cycle(self, mode: str = "full") -> CycleState:
        state = CycleState(mode=mode)
        state = await self.load_context(state)
        state = await self.crawl_products(state)
        state = await self.score_products(state)
        state = await self.draft_posts(state)
        state = await self.insert_fun_post_if_lucky(state)
        state = await self.schedule_posts(state)
        state = await self.publish_due_posts(state)
        state = await self.monitor_comments(state)
        state = await self.update_memory(state)
        if mode in {"wrap_up"}:
            state = await self.compact_memory(state)
        return state

    async def load_context(self, state: CycleState) -> CycleState:
        categories = self.improvement.category_watch_list()
        state.notes.append(f"Loaded {len(categories)} categories from improvement memory")
        state.categories = categories
        return state

    async def crawl_products(self, state: CycleState) -> CycleState:
        if state.mode == "publish_only":
            return state
        if not state.categories:
            self.database.log_activity("crawl", "Bỏ qua crawl — không có category nào", phase="crawl")
            return state
        limit = max(1, self.settings.shopee.max_products_per_cycle // max(len(state.categories), 1))
        self.database.log_activity("crawl", f"Bắt đầu crawl {len(state.categories)} category: {', '.join(state.categories[:5])}", phase="crawl")
        products = await self.crawler.crawl_categories(state.categories, limit_per_category=limit)
        logger.info("Crawl complete: %d products found across %s", len(products), state.categories)
        state.notes.append(f"Crawled {len(products)} products")
        state.crawled_products = products
        self.database.log_activity(
            "crawl_done",
            f"Crawl xong: tìm được {len(products)} sản phẩm",
            phase="crawl",
            detail={"count": len(products), "categories": state.categories},
        )
        return state

    async def score_products(self, state: CycleState) -> CycleState:
        if state.mode == "publish_only" or not state.crawled_products:
            return state
        self.database.log_activity("score", f"Bắt đầu chấm điểm {len(state.crawled_products)} sản phẩm", phase="score")
        base_improvement = self.improvement.load_context()
        avg_price = self.analyzer.category_average(state.crawled_products)
        ranked_products = sorted(
            state.crawled_products,
            key=lambda item: self.analyzer.preview_score(item, avg_price, base_improvement),
            reverse=True,
        )
        scored: list[ProductRecord] = []
        for index, product in enumerate(ranked_products):
            memory_insights = self._memory_insights_for_product(product)
            improvement = base_improvement.model_copy(deep=True)
            improvement.long_term_insights = memory_insights
            scored_product = await self.analyzer.score_product(
                product,
                category_average_price=avg_price,
                improvement=improvement,
                memory_insights=memory_insights,
                use_ai=self.settings.ai.enabled and index < self.settings.ai.score_top_products_per_cycle,
            )
            logger.info("Scored product %s: trend_score=%.1f (min=%.1f)", scored_product.product_id, scored_product.trend_score, self.settings.kpi.min_product_score)
            if scored_product.trend_score >= self.settings.kpi.min_product_score:
                scored.append(scored_product)
        logger.info("Scoring done: %d/%d products passed threshold", len(scored), len(ranked_products))
        scored.sort(key=lambda item: item.trend_score, reverse=True)
        state.scored_products = scored
        self.database.log_activity(
            "score_done",
            f"Chấm điểm xong: {len(scored)}/{len(ranked_products)} sản phẩm đạt ngưỡng",
            phase="score",
            detail={"passed": len(scored), "total": len(ranked_products)},
        )
        return state

    async def draft_posts(self, state: CycleState) -> CycleState:
        if state.mode == "publish_only":
            return state
        accounts = self.session_manager.load_accounts()
        base_improvement = self.improvement.load_context()
        today = datetime.now(UTC)
        existing_usage = self.database.get_account_category_usage(today)
        remaining_slots = max(0, self.settings.kpi.posts_per_day - self.database.count_committed_posts(today))
        if self.settings.loop.idle_crawl:
            target_drafts = self.settings.kpi.draft_buffer
        else:
            target_drafts = min(self.settings.kpi.draft_buffer, remaining_slots)
        planning_future = target_drafts > remaining_slots
        usage_for_assignment = {} if planning_future else existing_usage
        reserved_products = self.database.get_all_active_product_ids()
        products = [p for p in state.scored_products if p.product_id not in reserved_products]
        products.sort(key=lambda item: item.trend_score, reverse=True)
        products = products[:target_drafts]
        drafted: list[PostRecord] = []
        for product in products:
            account = self._choose_account(product, accounts, drafted, usage_for_assignment)
            if not account:
                continue
            recent_posts = self.database.get_recent_post_texts(account.id, limit=12)
            memory_insights = self._memory_insights_for_account(account.id, product.category)
            improvement = base_improvement.model_copy(deep=True)
            improvement.long_term_insights = memory_insights
            generated = await self.writer.write_post(
                product,
                account,
                improvement,
                recent_posts,
                memory_insights=memory_insights,
                use_ai=self.settings.ai.enabled and len(drafted) < self.settings.ai.write_top_posts_per_cycle,
            )
            post_id = str(uuid4())
            post = PostRecord(
                post_id=post_id,
                account=account.id,
                product=product,
                content=PostContent(
                    title=generated.title,
                    body=generated.body,
                    hashtags=generated.hashtags,
                    cta=generated.cta,
                    affiliate_link=product.affiliate_link,
                ),
                image_path=generated.image_path or product.image_path or "",
                status="draft",
            )
            tracked_link = build_tracking_link(post.post_id, product.affiliate_link)
            post.content.affiliate_link = tracked_link
            post.content.cta = self._rewrite_cta(post.content.cta, product.affiliate_link, tracked_link)
            drafted.append(post)
            self.database.upsert_post(post)
            self.farm_manager.save_draft(post)
            logger.info("Draft created: %s for %s (img=%s)", post.post_id[:8], product.name[:30], bool(post.image_path))
        logger.info("Draft cycle done: %d posts drafted", len(drafted))
        state.drafted_posts = drafted
        self.database.log_activity(
            "draft_done",
            f"Tạo xong {len(drafted)} bài draft mới",
            phase="draft",
            detail={"count": len(drafted), "products": [p.product.name[:40] for p in drafted]},
        )
        return state

    async def insert_fun_post_if_lucky(self, state: CycleState) -> CycleState:
        settings = load_settings(refresh=True)
        if not settings.features.fun_post_enabled:
            return state
        if random.random() > settings.features.fun_post_probability:
            return state
        post_types = settings.features.fun_post_types or ["meme", "tip"]
        post_type = random.choice(post_types)  # type: ignore[arg-type]
        accounts = self.session_manager.load_accounts()
        if not accounts:
            return state
        account = random.choice(accounts)
        category_context = state.categories[0] if state.categories else (account.niche or "shopee")
        fun_writer = FunContentWriter(database=self.database, client=self.writer.client)
        try:
            generated = await fun_writer.write_fun_post(
                account,
                post_type,
                category_context,
                use_ai=settings.ai.enabled,
            )
            post_id = str(uuid4())
            fun_product = ProductRecord(
                product_id=f"fun_{post_type}_{post_id[:8]}",
                name=generated.title[:80] if generated.title else f"Fun post ({post_type})",
                price=0.0,
                original_price=0.0,
                discount_percent=0.0,
                category=category_context,
                product_url="",
                affiliate_link="",
            )
            post = PostRecord(
                post_id=post_id,
                account=account.id,
                product=fun_product,
                content=PostContent(
                    title=generated.title,
                    body=generated.body,
                    hashtags=generated.hashtags,
                    cta=generated.cta,
                    affiliate_link="",
                ),
                image_path="",
                status="draft",
            )
            state.drafted_posts.append(post)
            self.database.upsert_post(post)
            self.farm_manager.save_draft(post)
            self.database.log_activity(
                "fun_post_created",
                f"Tạo bài vui ({post_type}): {generated.title[:50]}",
                phase="draft",
                detail={"post_id": post_id, "type": post_type, "account": account.id},
            )
            logger.info("Fun post inserted (%s) for %s", post_type, account.id)
        except Exception as exc:
            logger.warning("insert_fun_post_if_lucky failed: %s", exc)
        return state

    async def schedule_posts(self, state: CycleState) -> CycleState:
        if state.mode == "publish_only":
            state.scheduled_posts = self.database.get_due_posts(datetime.now(UTC), limit=50)
            return state
        accounts = self.session_manager.load_accounts()
        posts = [p for p in state.drafted_posts if p.content.affiliate_link and p.content.affiliate_link.startswith("http")]
        now = datetime.now(UTC)
        horizon_days = 2 if self.settings.loop.idle_crawl else 0
        existing_account_totals = {
            (now + timedelta(days=offset)).date().isoformat(): self.database.get_account_post_totals(now + timedelta(days=offset))
            for offset in range(horizon_days + 1)
        }
        existing_category_usage = {
            (now + timedelta(days=offset)).date().isoformat(): {
                f"{acc}::{cat}": total
                for (acc, cat), total in self.database.get_account_category_usage(now + timedelta(days=offset)).items()
            }
            for offset in range(horizon_days + 1)
        }
        reserved_product_ids = self.database.get_reserved_product_ids(now, now + timedelta(days=horizon_days + 1))
        scheduled = self.session_manager.schedule_posts_for_windows(
            posts,
            accounts,
            now=now.astimezone(),
            horizon_days=horizon_days,
            max_same_category=self.settings.kpi.max_same_category,
            existing_account_totals=existing_account_totals,
            existing_category_usage=existing_category_usage,
            reserved_product_ids=reserved_product_ids,
        )
        for post in scheduled:
            self.database.upsert_post(post)
            self.farm_manager.save_scheduled(post)
        state.scheduled_posts = scheduled
        return state

    async def publish_due_posts(self, state: CycleState) -> CycleState:
        accounts = {account.id: account for account in self.session_manager.load_accounts()}
        within_window, _window = self.session_manager.is_within_window()
        if not within_window:
            return state
        due_posts = self.database.get_due_posts(datetime.now(UTC), limit=50)
        published: list[PostRecord] = []
        for post in due_posts:
            account = accounts.get(post.account)
            if not account:
                continue
            try:
                fb_post_id = await self.publisher.publish_post(account, post)
                post.fb_post_id = fb_post_id
                post.status = "published"
                post.published_at = datetime.now(UTC)
                if not fb_post_id.startswith("dryrun-"):
                    insights = await self.publisher.fetch_post_insights(fb_post_id, account.resolved_access_token() or "")
                    post.performance.likes = insights.get("likes", 0)
                    post.performance.comments = insights.get("comments", 0)
                    post.performance.shares = insights.get("shares", 0)
                    post.performance.reach = insights.get("reach", 0)
                self.database.upsert_post(post)
                self.farm_manager.save_published(post)
                published.append(post)
                self.database.log_activity(
                    "published",
                    f"Đăng thành công: {post.product.name[:50]}",
                    phase="publish",
                    detail={"post_id": post.post_id, "fb_post_id": fb_post_id, "account": post.account, "product": post.product.name[:60]},
                )
            except Exception as exc:
                logger.warning("Publish failed for %s: %s", post.post_id, exc)
                post.status = "failed"
                post.error_message = str(exc)
                self.database.upsert_post(post)
                self.farm_manager.save_failed(post)
                self.database.log_activity(
                    "publish_failed",
                    f"Đăng thất bại: {post.product.name[:50]} — {str(exc)[:100]}",
                    phase="publish",
                    detail={"post_id": post.post_id, "error": str(exc)},
                )
        state.published_posts = published
        return state

    async def monitor_comments(self, state: CycleState) -> CycleState:
        if not self.settings.features.comment_monitoring_enabled:
            return state
        accounts = {account.id: account for account in self.session_manager.load_accounts()}
        recent = self.database.list_recent_published_posts(hours=self.settings.meta.recent_post_refresh_hours, limit=100)
        published_map = {post.post_id: post for post in [*recent, *state.published_posts]}
        monitored_posts = list(published_map.values())
        if not monitored_posts:
            return state
        comment_map = await self.monitor.monitor_posts(monitored_posts, accounts)
        for post in monitored_posts:
            if post.post_id in comment_map:
                post.comments = comment_map[post.post_id]
                post.performance.comments = len(post.comments)
                self.database.upsert_post(post)
                self.farm_manager.save_published(post)
        return state

    async def update_memory(self, state: CycleState) -> CycleState:
        posts = state.published_posts or self.database.list_recent_published_posts(hours=24, limit=100) or state.scheduled_posts
        category_counter = Counter(post.product.category for post in posts)
        top_categories = [(category, float(count)) for category, count in category_counter.most_common(5)]
        self.improvement.update(
            posts=posts,
            top_categories=top_categories,
            audience_insights={
                "best_hours": f"{self.settings.meta.window_a_start} - {self.settings.meta.window_b_end}",
                "best_content_type": "deal review",
                "triggers": ["hoi ve gia", "nhac sale ro rang", "call-to-action nhe"],
            },
            blacklist_products=[post.product.product_id for post in posts if post.status == "failed"],
            blacklist_keywords=[],
        )
        return state

    async def compact_memory(self, state: CycleState) -> CycleState:
        self.compactor.compact_day()
        return state

    def _choose_account(
        self,
        product: ProductRecord,
        accounts: list[AccountConfig],
        drafted: list[PostRecord],
        existing_usage: dict[tuple[str, str], int],
    ) -> AccountConfig | None:
        if not accounts:
            return None
        usage = Counter(post.account for post in drafted)
        category_usage = Counter((post.account, post.product.category) for post in drafted)
        existing_account_totals = Counter()
        for (account_id, _category), total in existing_usage.items():
            existing_account_totals[account_id] += total
        sorted_accounts = sorted(accounts, key=lambda account: usage[account.id])
        for account in sorted_accounts:
            committed_total = usage[account.id] + existing_account_totals[account.id]
            if committed_total >= min(account.daily_post_limit, self.settings.kpi.posts_per_account):
                continue
            total_category_usage = category_usage[(account.id, product.category)] + existing_usage.get((account.id, product.category), 0)
            if total_category_usage >= self.settings.kpi.max_same_category:
                continue
            if account.niche.lower() in product.category.lower() or product.category.lower() in account.niche.lower():
                return account
        for account in sorted_accounts:
            committed_total = usage[account.id] + existing_account_totals[account.id]
            if committed_total < min(account.daily_post_limit, self.settings.kpi.posts_per_account):
                return account
        return None

    def _rewrite_cta(self, cta: str, original_link: str, tracked_link: str) -> str:
        if original_link and original_link in cta:
            return cta.replace(original_link, tracked_link)
        if tracked_link in cta:
            return cta
        return f"{cta}\n{tracked_link}".strip()

    def _memory_insights_for_product(self, product: ProductRecord) -> list[str]:
        queries = [
            f"sản phẩm nào perform tốt trong 30 ngày qua ở category {product.category}",
            f"category nào đang tăng xu hướng liên quan tới {product.category}",
        ]
        return self._collect_memory_insights(queries)

    def _memory_insights_for_account(self, account_id: str, category: str) -> list[str]:
        queries = [
            f"tone nào engagement cao nhất với {account_id}",
            f"category nào đang tăng xu hướng cho {category}",
            f"sản phẩm nào perform tốt trong 30 ngày qua ở category {category}",
        ]
        return self._collect_memory_insights(queries)

    def _collect_memory_insights(self, queries: list[str]) -> list[str]:
        insights: list[str] = []
        seen: set[str] = set()
        for query in queries:
            for item in self.compactor.query_insights(query, limit=3):
                line = f"{item['metadata'].get('category', 'unknown')}|{item['metadata'].get('account', 'unknown')}: {item['document']}"
                if line in seen:
                    continue
                insights.append(line)
        return insights[:8]

    async def sync_affiliate_links(self) -> None:
        self.settings = load_settings(refresh=True)
        token = self.settings.integrations.shopee_affiliate_token
        auth_mode = self.settings.shopee.affiliate_auth_mode.lower()
        cookie = self.settings.integrations.shopee_affiliate_cookie
        # Skip only when there's no API token AND no cookie configured
        if not token and auth_mode != "sha256" and not cookie:
            return

        from common.models import PostFilters
        draft_posts = self.database.list_posts(PostFilters(status="draft", limit=100))
        synced_count = 0
        for post in draft_posts:
            aff = post.content.affiliate_link or ""
            # Only re-sync posts that don't have a proper short link
            if not aff or "s.shopee.vn" not in aff and "/r/" not in aff:
                canonical_url = self.crawler._canonical_product_url(post.product.product_url)
                new_link = await self.crawler.affiliate_api.generate_affiliate_link(canonical_url)
                if new_link and "s.shopee.vn" in new_link:
                    tracked = build_tracking_link(post.post_id, new_link)
                    post.content.affiliate_link = tracked
                    post.product.affiliate_link = new_link
                    post.content.cta = self._rewrite_cta(post.content.cta, "", tracked)
                    self.database.upsert_post(post)
                    self.farm_manager.save_draft(post)
                    synced_count += 1
        if synced_count > 0:
            logger.info("Synced affiliate links for %d draft posts", synced_count)
