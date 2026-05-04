from __future__ import annotations

from collections import Counter
import random
from datetime import UTC, date, datetime, time, timedelta

import httpx

from common.config import load_settings
from common.files import load_accounts, save_account
from common.logging import get_logger
from common.models import AccountConfig, PostRecord, WindowSlot

logger = get_logger(__name__)


class MetaSessionManager:
    def windows(self) -> list[WindowSlot]:
        settings = load_settings(refresh=True)
        return [
            WindowSlot(name="window_a", start=settings.meta.window_a_start, end=settings.meta.window_a_end),
            WindowSlot(name="window_b", start=settings.meta.window_b_start, end=settings.meta.window_b_end),
        ]

    def load_accounts(self) -> list[AccountConfig]:
        settings = load_settings(refresh=True)
        return [account for account in load_accounts(settings.accounts_dir) if account.status == "active"]

    def is_within_window(self, now: datetime | None = None) -> tuple[bool, WindowSlot | None]:
        current = now or datetime.now().astimezone()
        for window in self.windows():
            start_dt, end_dt = self._window_bounds(window, current.date())
            if start_dt <= current <= end_dt:
                return True, window
        return False, None

    def next_window(self, now: datetime | None = None) -> tuple[WindowSlot, datetime]:
        current = now or datetime.now().astimezone()
        candidates: list[tuple[WindowSlot, datetime]] = []
        for offset in (0, 1):
            target_date = current.date() + timedelta(days=offset)
            for window in self.windows():
                start_dt, _ = self._window_bounds(window, target_date)
                if start_dt > current:
                    candidates.append((window, start_dt))
        if not candidates:
            window = self.windows()[0]
            start_dt, _ = self._window_bounds(window, current.date() + timedelta(days=1))
            return window, start_dt
        return sorted(candidates, key=lambda item: item[1])[0]

    async def verify_accounts(self, accounts: list[AccountConfig]) -> dict[str, str]:
        """Verify each account using its designated driver.

        For API accounts, also attempts token refresh if needed.
        For cookie accounts, checks session validity via the cookie driver.
        """
        from modules.meta.drivers import get_driver_for_account
        from modules.meta.drivers.graph_api import GraphAPIDriver

        health: dict[str, str] = {}
        for account in accounts:
            driver = get_driver_for_account(account)
            try:
                result = await driver.verify(account)
                health[account.id] = result

                # For API mode: try token refresh if verification passed
                if result == "ok" and isinstance(driver, GraphAPIDriver):
                    token = account.resolved_access_token()
                    if token:
                        async with httpx.AsyncClient(timeout=20.0) as client:
                            await self.refresh_token_if_needed(client, account)
            except Exception as exc:
                logger.warning("Account health check failed for %s (%s): %s", account.id, account.auth_mode, exc)
                health[account.id] = "error"
        return health

    async def refresh_token_if_needed(self, client: httpx.AsyncClient, account: AccountConfig) -> None:
        settings = load_settings(refresh=True)
        try:
            expires_at = datetime.fromisoformat(account.token_expires_at).replace(tzinfo=UTC)
        except (ValueError, AttributeError, TypeError):
            return
        days_left = (expires_at - datetime.now(UTC)).days
        if days_left > settings.meta.token_refresh_days_before_expiry:
            return
        app_id = settings.integrations.meta_app_id
        app_secret = settings.integrations.meta_app_secret
        token = account.resolved_access_token()
        if not all([app_id, app_secret, token]):
            logger.warning("Skipping token refresh for %s due to missing app credentials", account.id)
            return
        params = {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": token,
        }
        response = await client.get(self._graph_url("/oauth/access_token", authless=True), params=params)
        if response.status_code >= 400:
            logger.warning("Token refresh failed for %s: %s", account.id, response.text)
            return
        payload = response.json()
        new_token = payload.get("access_token")
        expires_in = int(payload.get("expires_in") or 0)
        if not new_token:
            logger.warning("Token refresh returned no access_token for %s", account.id)
            return
        account.access_token = new_token
        if expires_in > 0:
            account.token_expires_at = (datetime.now(UTC) + timedelta(seconds=expires_in)).date().isoformat()
        save_account(settings.accounts_dir, account)
        logger.info("Refreshed Meta token for %s and persisted updated account file", account.id)

    def schedule_posts_for_windows(
        self,
        posts: list[PostRecord],
        accounts: list[AccountConfig],
        now: datetime | None = None,
        *,
        horizon_days: int = 0,
        max_same_category: int = 3,
        existing_account_totals: dict[str, dict[str, int]] | None = None,
        existing_category_usage: dict[str, dict[str, int]] | None = None,
        reserved_product_ids: set[str] | None = None,
    ) -> list[PostRecord]:
        current = now or datetime.now().astimezone()
        scheduled: list[PostRecord] = []
        reserved = set(reserved_product_ids or set())
        account_queues: dict[str, list[PostRecord]] = {
            account.id: [post for post in posts if post.account == account.id and post.product.product_id not in reserved]
            for account in accounts
        }
        accounts_in_order = sorted(accounts, key=lambda account: account.id)

        for day_offset in range(horizon_days + 1):
            target_date = current.date() + timedelta(days=day_offset)
            day_key = target_date.isoformat()
            account_totals: Counter[str] = Counter((existing_account_totals or {}).get(day_key, {}))
            raw_category = (existing_category_usage or {}).get(day_key, {})
            # keys are "acc_id::category" strings serialized from orchestrator
            category_usage: Counter[tuple[str, str]] = Counter({
                (k.split("::", 1)[0], k.split("::", 1)[1]): v
                for k, v in raw_category.items()
                if "::" in k
            })
            for window in self.windows():
                start_dt, end_dt = self._window_bounds(window, target_date)
                if day_offset == 0 and end_dt <= current:
                    continue
                cluster_time = max(start_dt, current) if day_offset == 0 else start_dt
                while cluster_time <= end_dt:
                    scheduled_any = False
                    previous_post_time: datetime | None = None
                    for position, account in enumerate(accounts_in_order):
                        if account_totals[account.id] >= account.daily_post_limit:
                            continue
                        queue = account_queues.get(account.id, [])
                        candidate = self._pop_next_eligible_post(
                            queue,
                            reserved,
                            account.id,
                            category_usage,
                            max_same_category,
                        )
                        if not candidate:
                            continue
                        if position == 0 or previous_post_time is None:
                            scheduled_time = cluster_time
                        else:
                            scheduled_time = previous_post_time + timedelta(minutes=random.randint(8, 15))
                        if scheduled_time > end_dt:
                            queue.insert(0, candidate)
                            continue
                        candidate.scheduled_at = scheduled_time.astimezone(UTC)
                        candidate.status = "scheduled"
                        scheduled.append(candidate)
                        reserved.add(candidate.product.product_id)
                        account_totals[account.id] += 1
                        category_usage[(account.id, candidate.product.category)] += 1
                        previous_post_time = scheduled_time
                        scheduled_any = True
                    if not scheduled_any:
                        break
                    cluster_anchor = previous_post_time or cluster_time
                    cluster_time = cluster_anchor + timedelta(minutes=random.randint(5, 15))
        return scheduled

    def pre_window_cutoff(self, window: WindowSlot, on_date: date) -> datetime:
        settings = load_settings(refresh=True)
        start_dt, _ = self._window_bounds(window, on_date)
        return start_dt - timedelta(minutes=settings.meta.verify_before_window_minutes)

    def _window_bounds(self, window: WindowSlot, on_date: date) -> tuple[datetime, datetime]:
        start_hour, start_minute = [int(part) for part in window.start.split(":")]
        end_hour, end_minute = [int(part) for part in window.end.split(":")]
        tz_now = datetime.now().astimezone().tzinfo
        start_dt = datetime.combine(on_date, time(start_hour, start_minute, tzinfo=tz_now))
        end_dt = datetime.combine(on_date, time(end_hour, end_minute, tzinfo=tz_now))
        return start_dt, end_dt

    def _graph_url(self, path: str, authless: bool = False) -> str:
        version = load_settings(refresh=True).meta.graph_api_version
        if authless:
            return f"https://graph.facebook.com{path}"
        return f"https://graph.facebook.com/{version}{path}"

    def _pop_next_eligible_post(
        self,
        queue: list[PostRecord],
        reserved_products: set[str],
        account_id: str,
        category_usage: Counter[tuple[str, str]],
        max_same_category: int,
    ) -> PostRecord | None:
        for index, post in enumerate(queue):
            if post.product.product_id in reserved_products:
                continue
            if category_usage[(account_id, post.product.category)] >= max_same_category:
                continue
            return queue.pop(index)
        return None
