"""MetaPublisher — facade that delegates to the appropriate driver.

Each account chooses its auth_mode:
  - "api":           Graph API with page access token
  - "cookie_page":   Browser cookies, post on Page timeline
  - "cookie_profile": Browser cookies, post on personal timeline

The driver layer handles the actual HTTP calls. This class provides
backward-compatible methods (publish_post, schedule_post, fetch_post_insights)
plus the human-approval / dry-run guard.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from common.config import load_settings
from common.logging import get_logger
from common.models import AccountConfig, PostRecord
from modules.meta.drivers import get_driver_for_account
from modules.meta.drivers.graph_api import GraphAPIDriver

logger = get_logger(__name__)


class MetaPublisher:
    """High-level publisher that routes to the correct driver per account."""

    async def publish_post(self, account: AccountConfig, post: PostRecord) -> str:
        settings = load_settings(refresh=True)
        if settings.meta.require_human_approval and post.status != "approved":
            raise PermissionError(f"Post {post.post_id} is not approved for publishing")
        if settings.meta.publish_mode != "publish":
            logger.info("Dry-run publish for %s on %s", post.post_id, account.id)
            return f"dryrun-{post.post_id}"

        driver = get_driver_for_account(account)
        logger.info(
            "Publishing via %s driver: account=%s post=%s",
            account.auth_mode, account.id, post.post_id[:8],
        )
        return await driver.publish_post(account, post)

    async def schedule_post(
        self, account: AccountConfig, post: PostRecord, publish_time: datetime
    ) -> str:
        """Schedule a post for future publishing. Only supported for API mode."""
        driver = get_driver_for_account(account)
        if isinstance(driver, GraphAPIDriver):
            return await driver.schedule_post(account, post, publish_time)
        # Cookie modes: publish immediately (scheduling not supported)
        logger.warning(
            "schedule_post not supported for auth_mode=%s, publishing immediately",
            account.auth_mode,
        )
        return await self.publish_post(account, post)

    async def fetch_post_insights(
        self, account: AccountConfig, fb_post_id: str
    ) -> dict[str, Any]:
        driver = get_driver_for_account(account)
        return await driver.fetch_post_insights(account, fb_post_id)
