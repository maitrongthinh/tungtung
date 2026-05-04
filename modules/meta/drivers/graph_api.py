"""Graph API driver — uses Facebook Graph API with page access tokens.

Requires: Meta App in live mode, page access token with appropriate permissions.
This is the original publishing mechanism extracted from MetaPublisher.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from common.config import load_settings
from common.logging import get_logger
from common.models import AccountConfig, PostRecord
from modules.meta.drivers.base import MetaDriver

logger = get_logger(__name__)


class GraphAPIDriver(MetaDriver):
    """Facebook publishing via Graph API. Works for Pages with long-lived tokens."""

    # ── Publish ─────────────────────────────────────────────────

    async def publish_post(self, account: AccountConfig, post: PostRecord) -> str:
        message = self._compose_message(post)
        token = account.resolved_access_token() or ""

        # Prefer photo post
        if post.image_path and Path(post.image_path).exists():
            return await self._post_photo(account.page_id, message, post.image_path, token)

        # Try fetching product image
        fetched = await self._fetch_product_image_to_temp(post)
        if fetched:
            try:
                return await self._post_photo(account.page_id, message, fetched, token)
            except Exception as exc:
                logger.warning("Photo post with fetched image failed: %s, falling back", exc)

        affiliate_link = post.content.affiliate_link or post.product.affiliate_link or ""
        if self._should_use_link_field(affiliate_link):
            return await self._post_link(account.page_id, message, affiliate_link, token)

        return await self._post_message(account.page_id, message, token)

    async def _post_message(self, page_id: str, message: str, access_token: str) -> str:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.post(
                self._graph_url(f"/{page_id}/feed"),
                data={"message": message, "access_token": access_token},
            )
            resp.raise_for_status()
            return str(resp.json().get("id"))

    async def _post_photo(self, page_id: str, message: str, image_path: str, access_token: str) -> str:
        """Upload unpublished photo then attach to feed post (appears on timeline)."""
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            # Step 1: upload unpublished
            with open(image_path, "rb") as fh:
                upload_resp = await client.post(
                    self._graph_url(f"/{page_id}/photos"),
                    data={"published": "false", "no_story": "true", "access_token": access_token},
                    files={"source": (Path(image_path).name, fh, "image/jpeg")},
                )
            upload_resp.raise_for_status()
            media_fbid = upload_resp.json().get("id")
            if not media_fbid:
                raise RuntimeError("Photo upload returned no media id")

            # Step 2: create feed post with attached media
            feed_resp = await client.post(
                self._graph_url(f"/{page_id}/feed"),
                json={
                    "message": message,
                    "attached_media": [{"media_fbid": media_fbid}],
                    "access_token": access_token,
                },
            )
            feed_resp.raise_for_status()
            return str(feed_resp.json().get("id"))

    async def _post_link(self, page_id: str, message: str, link: str, access_token: str) -> str:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.post(
                self._graph_url(f"/{page_id}/feed"),
                data={"message": message, "link": link, "access_token": access_token},
            )
            resp.raise_for_status()
            return str(resp.json().get("id"))

    # ── Schedule ────────────────────────────────────────────────

    async def schedule_post(
        self, account: AccountConfig, post: PostRecord, publish_time: datetime
    ) -> str:
        """Schedule a post for future publishing via Graph API."""
        message = self._compose_message(post)
        token = account.resolved_access_token() or ""
        publish_ts = int(publish_time.timestamp())

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            if post.image_path and Path(post.image_path).exists():
                with open(post.image_path, "rb") as fh:
                    upload_resp = await client.post(
                        self._graph_url(f"/{account.page_id}/photos"),
                        data={"published": "false", "no_story": "true", "access_token": token},
                        files={"source": (Path(post.image_path).name, fh, "image/jpeg")},
                    )
                upload_resp.raise_for_status()
                media_fbid = upload_resp.json().get("id")
                response = await client.post(
                    self._graph_url(f"/{account.page_id}/feed"),
                    json={
                        "message": message,
                        "attached_media": [{"media_fbid": media_fbid}],
                        "published": False,
                        "scheduled_publish_time": publish_ts,
                        "access_token": token,
                    },
                )
            else:
                params: dict = {
                    "message": message,
                    "published": "false",
                    "scheduled_publish_time": publish_ts,
                    "access_token": token,
                }
                if post.content.affiliate_link:
                    params["link"] = post.content.affiliate_link
                response = await client.post(
                    self._graph_url(f"/{account.page_id}/feed"), data=params
                )
            response.raise_for_status()
            payload = response.json()
            return str(payload.get("id") or payload.get("post_id"))

    # ── Verification ────────────────────────────────────────────

    async def verify(self, account: AccountConfig) -> str:
        token = account.resolved_access_token()
        if not token:
            return "missing_token"
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                resp = await client.get(
                    self._graph_url(f"/{account.page_id}"),
                    params={"fields": "id,name", "access_token": token},
                )
                if resp.status_code == 200:
                    return "ok"
                return f"error:{resp.status_code}"
        except Exception as exc:
            logger.warning("Graph API verify failed for %s: %s", account.id, exc)
            return "error"

    # ── Comments ────────────────────────────────────────────────

    async def fetch_comments(self, account: AccountConfig, fb_post_id: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(
                self._graph_url(f"/{fb_post_id}/comments"),
                params={
                    "fields": "id,message,from,created_time",
                    "access_token": account.resolved_access_token() or "",
                },
            )
            resp.raise_for_status()
            return resp.json().get("data", [])

    async def reply_comment(self, account: AccountConfig, comment_id: str, message: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.post(
                    self._graph_url(f"/{comment_id}/comments"),
                    data={
                        "message": message,
                        "access_token": account.resolved_access_token() or "",
                    },
                )
                resp.raise_for_status()
                return True
        except Exception as exc:
            logger.warning("Graph API reply_comment failed for %s: %s", comment_id, exc)
            return False

    # ── Insights ────────────────────────────────────────────────

    async def fetch_post_insights(self, account: AccountConfig, fb_post_id: str) -> dict[str, Any]:
        fields = "likes.summary(true),comments.summary(true),shares,insights.metric(post_impressions_unique)"
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(
                self._graph_url(f"/{fb_post_id}"),
                params={"fields": fields, "access_token": account.resolved_access_token() or ""},
            )
            resp.raise_for_status()
            payload = resp.json()
            return {
                "likes": payload.get("likes", {}).get("summary", {}).get("total_count", 0),
                "comments": payload.get("comments", {}).get("summary", {}).get("total_count", 0),
                "shares": payload.get("shares", {}).get("count", 0),
                "reach": self._extract_reach(payload),
            }

    # ── Internals ───────────────────────────────────────────────

    def _extract_reach(self, payload: dict[str, Any]) -> int:
        insights = payload.get("insights", {}).get("data", [])
        for item in insights:
            if item.get("name") == "post_impressions_unique":
                values = item.get("values") or []
                if values:
                    return int(values[0].get("value") or 0)
        return 0

    def _graph_url(self, path: str, *, authless: bool = False) -> str:
        version = load_settings(refresh=True).meta.graph_api_version
        if authless:
            return f"https://graph.facebook.com{path}"
        return f"https://graph.facebook.com/{version}{path}"
