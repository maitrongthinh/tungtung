from __future__ import annotations

import tempfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx

from common.config import load_settings
from common.logging import get_logger
from common.models import AccountConfig, PostRecord

logger = get_logger(__name__)


class MetaPublisher:
    async def publish_post(self, account: AccountConfig, post: PostRecord) -> str:
        settings = load_settings(refresh=True)
        if settings.meta.require_human_approval and post.status != "approved":
            raise PermissionError(f"Post {post.post_id} is not approved for publishing")
        if settings.meta.publish_mode != "publish":
            logger.info("Dry-run publish for %s on %s", post.post_id, account.id)
            return f"dryrun-{post.post_id}"
        message = self._compose_message(post)
        token = account.resolved_access_token() or ""
        # Prefer photo post (shows image preview in feed)
        if post.image_path and Path(post.image_path).exists():
            return await self.post_photo(account.page_id, message, post.image_path, token)
        # Try fetching image from product if no local file
        fetched_image = await self._fetch_product_image(post)
        if fetched_image:
            try:
                return await self.post_photo(account.page_id, message, fetched_image, token)
            except Exception as exc:
                logger.warning("Photo post with fetched image failed: %s, falling back to link", exc)
        affiliate_link = post.content.affiliate_link or post.product.affiliate_link or ""
        # Only use link field for short URLs (s.shopee.vn) — raw Shopee product URLs get rejected by FB API
        if affiliate_link and ("s.shopee.vn" in affiliate_link or "bit.ly" in affiliate_link or "/r/" in affiliate_link):
            return await self.post_link(account.page_id, message, affiliate_link, token)
        # No short link — post message only (affiliate link already embedded in CTA text)
        return await self.post_message(account.page_id, message, token)

    async def post_message(self, page_id: str, message: str, access_token: str) -> str:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.post(
                self._graph_url(f"/{page_id}/feed"),
                data={"message": message, "access_token": access_token},
            )
            response.raise_for_status()
            payload = response.json()
            return str(payload.get("id"))

    async def _fetch_product_image(self, post: PostRecord) -> str | None:
        images = post.product.images or []
        if not images:
            return None
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            for url in images[:3]:
                try:
                    response = await client.get(url)
                    if response.status_code == 200 and response.headers.get("content-type", "").startswith("image/"):
                        suffix = ".jpg"
                        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                            tmp.write(response.content)
                            return tmp.name
                except Exception:
                    continue
        return None

    async def post_photo(self, page_id: str, message: str, image_path: str, access_token: str) -> str:
        """Đăng bài có ảnh lên feed (hiện trên timeline, không chỉ trong album ảnh).

        Dùng 2-step: upload ảnh với no_story=true trước, rồi tạo feed post đính kèm media_fbid.
        Cách này đảm bảo bài hiện trên News Feed thay vì chỉ ở tab Ảnh.
        """
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            # Step 1: upload ảnh unpublished
            with open(image_path, "rb") as image_handle:
                upload_resp = await client.post(
                    self._graph_url(f"/{page_id}/photos"),
                    data={"published": "false", "no_story": "true", "access_token": access_token},
                    files={"source": (Path(image_path).name, image_handle, "image/jpeg")},
                )
            upload_resp.raise_for_status()
            media_fbid = upload_resp.json().get("id")
            if not media_fbid:
                raise RuntimeError("Photo upload returned no media id")

            # Step 2: tạo feed post với ảnh đính kèm
            feed_resp = await client.post(
                self._graph_url(f"/{page_id}/feed"),
                json={
                    "message": message,
                    "attached_media": [{"media_fbid": media_fbid}],
                    "access_token": access_token,
                },
            )
            feed_resp.raise_for_status()
            payload = feed_resp.json()
            return str(payload.get("id"))

    async def post_link(self, page_id: str, message: str, link: str, access_token: str) -> str:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.post(
                self._graph_url(f"/{page_id}/feed"),
                data={"message": message, "link": link, "access_token": access_token},
            )
            response.raise_for_status()
            payload = response.json()
            return str(payload.get("id"))

    async def schedule_post(self, account: AccountConfig, post: PostRecord, publish_time: datetime) -> str:
        message = self._compose_message(post)
        token = account.resolved_access_token() or ""
        publish_ts = int(publish_time.timestamp())
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            if post.image_path and Path(post.image_path).exists():
                # Upload ảnh unpublished, rồi schedule feed post
                with open(post.image_path, "rb") as image_handle:
                    upload_resp = await client.post(
                        self._graph_url(f"/{account.page_id}/photos"),
                        data={"published": "false", "no_story": "true", "access_token": token},
                        files={"source": (Path(post.image_path).name, image_handle, "image/jpeg")},
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
                feed_params: dict = {
                    "message": message,
                    "published": "false",
                    "scheduled_publish_time": publish_ts,
                    "access_token": token,
                }
                if post.content.affiliate_link:
                    feed_params["link"] = post.content.affiliate_link
                response = await client.post(self._graph_url(f"/{account.page_id}/feed"), data=feed_params)
            response.raise_for_status()
            payload = response.json()
            return str(payload.get("id") or payload.get("post_id"))

    async def fetch_post_insights(self, post_id: str, access_token: str) -> dict[str, Any]:
        fields = "likes.summary(true),comments.summary(true),shares,insights.metric(post_impressions_unique)"
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(
                self._graph_url(f"/{post_id}"),
                params={"fields": fields, "access_token": access_token},
            )
            response.raise_for_status()
            payload = response.json()
            return {
                "likes": payload.get("likes", {}).get("summary", {}).get("total_count", 0),
                "comments": payload.get("comments", {}).get("summary", {}).get("total_count", 0),
                "shares": payload.get("shares", {}).get("count", 0),
                "reach": self._extract_reach(payload),
            }

    def _extract_reach(self, payload: dict[str, Any]) -> int:
        insights = payload.get("insights", {}).get("data", [])
        for item in insights:
            if item.get("name") == "post_impressions_unique":
                values = item.get("values") or []
                if values:
                    return int(values[0].get("value") or 0)
        return 0

    def _compose_message(self, post: PostRecord) -> str:
        hashtags = " ".join(post.content.hashtags)
        return f"{post.content.title}\n\n{post.content.body}\n\n{hashtags}\n\n{post.content.cta}"

    def _graph_url(self, path: str) -> str:
        version = load_settings(refresh=True).meta.graph_api_version
        return f"https://graph.facebook.com/{version}{path}"
