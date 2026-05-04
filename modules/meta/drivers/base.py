from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import httpx

from common.models import AccountConfig, PostRecord


class MetaDriver(ABC):
    """Abstract base for Facebook publishing drivers.

    Each concrete driver (Graph API, cookie-based page, cookie-based profile)
    implements all five operations so the publisher/monitor layer can stay
    driver-agnostic.
    """

    # ------------------------------------------------------------------ #
    #  Core operations                                                    #
    # ------------------------------------------------------------------ #

    @abstractmethod
    async def publish_post(self, account: AccountConfig, post: PostRecord) -> str:
        """Full publish flow: handles photo/link/message routing."""
        ...

    @abstractmethod
    async def verify(self, account: AccountConfig) -> str:
        """Verify account accessibility. Returns 'ok' or error description."""
        ...

    @abstractmethod
    async def fetch_comments(self, account: AccountConfig, fb_post_id: str) -> list[dict[str, Any]]:
        """Fetch comments. Returns list of dicts: {id, message, from: {name}, created_time}."""
        ...

    @abstractmethod
    async def reply_comment(self, account: AccountConfig, comment_id: str, message: str) -> bool:
        """Reply to a comment. Returns True on success."""
        ...

    @abstractmethod
    async def fetch_post_insights(self, account: AccountConfig, fb_post_id: str) -> dict[str, Any]:
        """Fetch engagement metrics: {likes, comments, shares, reach}."""
        ...

    # ------------------------------------------------------------------ #
    #  Shared helpers                                                     #
    # ------------------------------------------------------------------ #

    def _compose_message(self, post: PostRecord) -> str:
        """Build the full post message from structured content."""
        hashtags = " ".join(post.content.hashtags)
        return f"{post.content.title}\n\n{post.content.body}\n\n{hashtags}\n\n{post.content.cta}"

    def _should_use_link_field(self, affiliate_link: str) -> bool:
        """Only use Graph API link field for short URLs — raw Shopee URLs get rejected."""
        return bool(affiliate_link) and any(
            tag in affiliate_link for tag in ("s.shopee.vn", "bit.ly", "/r/")
        )

    async def _fetch_product_image_to_temp(self, post: PostRecord) -> str | None:
        """Download product image to a temp file. Returns path or None."""
        import tempfile

        images = post.product.images or []
        if not images:
            return None
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            for url in images[:3]:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/"):
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                            tmp.write(resp.content)
                            return tmp.name
                except Exception:
                    continue
        return None
