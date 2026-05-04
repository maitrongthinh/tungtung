"""Cookie-based Facebook Page driver.

Posts to a Facebook Page using browser cookies instead of Graph API tokens.
Uses mbasic.facebook.com (lightweight HTML interface) for maximum reliability.

Requires:
  - account.fb_cookies: browser cookies as JSON array or semicolon string
  - account.page_id: target Facebook Page ID
  - Cookies must be from a user who has admin/editor role on the page

Flow:
  1. Load cookies into httpx client
  2. GET the page's composer on mbasic.facebook.com
  3. Parse the HTML form (hidden fields: fb_dtsg, jazoest, etc.)
  4. Fill in message + optional photo
  5. POST the form
  6. Extract post ID from redirect/response
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any

import httpx

from common.logging import get_logger
from common.models import AccountConfig, PostRecord
from modules.meta.drivers.base import MetaDriver
from modules.meta.drivers.cookie_utils import (
    create_client,
    extract_fb_dtsg,
    extract_jazoest,
    extract_post_id_from_html,
    extract_post_id_from_url,
    extract_user_id,
    parse_cookies,
    validate_essential_cookies,
)

logger = get_logger(__name__)


class CookiePageDriver(MetaDriver):
    """Post to a Facebook Page using browser cookies (no Graph API needed)."""

    # ── Publish ─────────────────────────────────────────────────

    async def publish_post(self, account: AccountConfig, post: PostRecord) -> str:
        cookies = parse_cookies(account.fb_cookies)
        valid, reason = validate_essential_cookies(cookies)
        if not valid:
            raise ValueError(f"Invalid cookies for {account.id}: {reason}")

        message = self._compose_message(post)

        # Route: photo → link → text-only
        if post.image_path and Path(post.image_path).exists():
            return await self._post_photo(cookies, account.page_id, message, post.image_path)

        fetched = await self._fetch_product_image_to_temp(post)
        if fetched:
            try:
                return await self._post_photo(cookies, account.page_id, message, fetched)
            except Exception as exc:
                logger.warning("Photo post failed for %s: %s, falling back to text", account.id, exc)

        return await self._post_text(cookies, account.page_id, message)

    # ── Text post ───────────────────────────────────────────────

    async def _post_text(self, cookies: dict[str, str], page_id: str, message: str) -> str:
        """Post a text-only update to a Facebook Page via mbasic.facebook.com."""
        async with create_client(cookies) as client:
            composer_url = (
                f"https://mbasic.facebook.com/composer/?mbasic=1"
                f"&target={page_id}"
                f"&redirect_uri=https%3A%2F%2Fmbasic.facebook.com%2F{page_id}"
            )
            resp = await client.get(composer_url, follow_redirects=True)
            if resp.status_code != 200:
                resp = await client.get(
                    f"https://mbasic.facebook.com/{page_id}", follow_redirects=True
                )
                resp.raise_for_status()

            html = resp.text
            form_data = self._parse_composer_form(html)
            if not form_data:
                raise RuntimeError(
                    f"Could not find composer form for page {page_id}. "
                    "Cookies may be expired or page inaccessible."
                )

            form_data["xc_message"] = message
            form_data["view_post"] = "Post"

            action_url = form_data.pop("__action_url", "")
            if not action_url:
                action_url = composer_url

            resp = await client.post(action_url, data=form_data, follow_redirects=True)

            post_id = (
                extract_post_id_from_url(str(resp.url))
                or extract_post_id_from_html(resp.text)
            )
            if post_id:
                logger.info("Cookie page post success: %s", post_id)
                return post_id

            logger.warning("Post submitted but could not extract post_id from response")
            return _synthetic_id("cookie_page", message)

    # ── Photo post ──────────────────────────────────────────────

    async def _post_photo(
        self, cookies: dict[str, str], page_id: str, message: str, image_path: str
    ) -> str:
        """Post with photo to a Facebook Page via mbasic.facebook.com."""
        async with create_client(cookies) as client:
            # Load the composer page
            composer_url = (
                f"https://mbasic.facebook.com/composer/?mbasic=1"
                f"&target={page_id}"
                f"&redirect_uri=https%3A%2F%2Fmbasic.facebook.com%2F{page_id}"
            )
            resp = await client.get(composer_url, follow_redirects=True)
            if resp.status_code != 200:
                resp = await client.get(
                    f"https://mbasic.facebook.com/{page_id}", follow_redirects=True
                )
            resp.raise_for_status()
            html = resp.text

            # Try to find and follow the photo upload form/link
            form_data = await self._find_photo_form(client, html, page_id)
            if not form_data:
                form_data = self._parse_composer_form(html)

            if not form_data:
                raise RuntimeError("Could not find photo upload form")

            form_data["xc_message"] = message
            action_url = form_data.pop("__action_url", "")
            if not action_url:
                action_url = composer_url

            with open(image_path, "rb") as fh:
                files = {"file1": (Path(image_path).name, fh, "image/jpeg")}
                resp = await client.post(
                    action_url,
                    data={**form_data, "add_photo_done": "Post"},
                    files=files,
                    follow_redirects=True,
                )

            post_id = (
                extract_post_id_from_url(str(resp.url))
                or extract_post_id_from_html(resp.text)
            )
            if post_id:
                logger.info("Cookie page photo post success: %s", post_id)
                return post_id

            logger.warning("Photo post submitted but could not extract post_id")
            return _synthetic_id("cookie_page_photo", message)

    # ── Verification ────────────────────────────────────────────

    async def verify(self, account: AccountConfig) -> str:
        """Check if cookies allow access to the page."""
        cookies = parse_cookies(account.fb_cookies)
        valid, reason = validate_essential_cookies(cookies)
        if not valid:
            return reason

        try:
            async with create_client(cookies) as client:
                resp = await client.get(
                    f"https://mbasic.facebook.com/{account.page_id}",
                    follow_redirects=True,
                )
                if resp.status_code == 200:
                    if "login" in str(resp.url).lower() and "login" not in f"/{account.page_id}":
                        return "redirected_to_login"
                    if "This content isn" in resp.text or "Page Not Found" in resp.text:
                        return "page_not_found_or_no_access"
                    return "ok"
                return f"http_error:{resp.status_code}"
        except Exception as exc:
            logger.warning("Cookie page verify failed for %s: %s", account.id, exc)
            return f"error:{exc}"

    # ── Comments ────────────────────────────────────────────────

    async def fetch_comments(self, account: AccountConfig, fb_post_id: str) -> list[dict[str, Any]]:
        """Fetch comments by parsing the post page on mbasic.facebook.com."""
        cookies = parse_cookies(account.fb_cookies)
        comments: list[dict[str, Any]] = []
        try:
            async with create_client(cookies) as client:
                post_url = _resolve_post_url(account.page_id, fb_post_id)
                resp = await client.get(post_url, follow_redirects=True)
                if resp.status_code != 200:
                    return []

                html = resp.text
                comment_pattern = re.compile(
                    r'<h3[^>]*>\s*<a[^>]*>([^<]+)</a>.*?</h3>'
                    r'.*?<div[^>]*>([^<]+)</div>',
                    re.DOTALL,
                )
                for match in comment_pattern.finditer(html):
                    author = match.group(1).strip()
                    msg = match.group(2).strip()
                    if msg:
                        comments.append({
                            "id": f"mbasic_{hash(author + msg) % 10**10}",
                            "message": msg,
                            "from": {"name": author},
                            "created_time": "",
                        })
        except Exception as exc:
            logger.warning("Cookie fetch_comments failed: %s", exc)
        return comments

    async def reply_comment(self, account: AccountConfig, comment_id: str, message: str) -> bool:
        """Reply to a comment — limited support for synthetic IDs from HTML parsing."""
        logger.info("Cookie reply_comment: limited support, comment_id=%s", comment_id)
        return False

    # ── Insights ────────────────────────────────────────────────

    async def fetch_post_insights(self, account: AccountConfig, fb_post_id: str) -> dict[str, Any]:
        """Parse basic engagement counts from the post page on mbasic."""
        cookies = parse_cookies(account.fb_cookies)
        result: dict[str, Any] = {"likes": 0, "comments": 0, "shares": 0, "reach": 0}
        try:
            async with create_client(cookies) as client:
                post_url = _resolve_post_url(account.page_id, fb_post_id)
                resp = await client.get(post_url, follow_redirects=True)
                if resp.status_code != 200:
                    return result
                html = resp.text

                like_match = re.search(r'(\d[\d,.]*)\s*(?:people|người|thích)', html)
                if like_match:
                    result["likes"] = int(like_match.group(1).replace(",", "").replace(".", ""))

                comment_blocks = re.findall(r'<div[^>]*data-commentid=', html)
                result["comments"] = len(comment_blocks)

        except Exception as exc:
            logger.warning("Cookie fetch_post_insights failed: %s", exc)
        return result

    # ── Form parsing helpers ────────────────────────────────────

    def _parse_composer_form(self, html: str) -> dict[str, str] | None:
        """Parse the composer form from mbasic.facebook.com page HTML.

        Returns dict with form fields including __action_url, or None if not found.
        """
        form_patterns = [
            r'<form[^>]*method="post"[^>]*action="([^"]*(?:composer|home|timeline)[^"]*)"[^>]*>(.*?)</form>',
            r'<form[^>]*action="([^"]*(?:composer|home|timeline)[^"]*)"[^>]*method="post"[^>]*>(.*?)</form>',
            r'<form[^>]*method="post"[^>]*>(.*?)</form>',
        ]

        for pattern in form_patterns:
            match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
            if not match:
                continue

            if match.lastindex == 2:
                action = match.group(1).strip()
                form_body = match.group(2)
            else:
                form_body = match.group(1)
                action = ""

            if action and action.startswith("/"):
                action = f"https://mbasic.facebook.com{action}"

            fields: dict[str, str] = {}
            for m in re.finditer(
                r'<input[^>]*type="hidden"[^>]*name="([^"]*)"[^>]*value="([^"]*)"',
                form_body, re.IGNORECASE,
            ):
                fields[m.group(1)] = m.group(2)
            for m in re.finditer(
                r'<input[^>]*name="([^"]*)"[^>]*type="hidden"[^>]*value="([^"]*)"',
                form_body, re.IGNORECASE,
            ):
                if m.group(1) not in fields:
                    fields[m.group(1)] = m.group(2)

            if "fb_dtsg" not in fields:
                dtsg = extract_fb_dtsg(html)
                if dtsg:
                    fields["fb_dtsg"] = dtsg
            if "jazoest" not in fields:
                jazoest = extract_jazoest(html)
                if jazoest:
                    fields["jazoest"] = jazoest

            fields["__action_url"] = action
            if "fb_dtsg" in fields:
                return fields

        fb_dtsg = extract_fb_dtsg(html)
        jazoest = extract_jazoest(html)
        user_id = extract_user_id(html)
        if fb_dtsg:
            return {
                "fb_dtsg": fb_dtsg,
                "jazoest": jazoest or "",
                "__user": user_id or "",
                "__a": "1",
                "__action_url": "",
            }
        return None

    async def _find_photo_form(
        self, client: httpx.AsyncClient, html: str, page_id: str
    ) -> dict[str, str] | None:
        """Find the photo upload form on mbasic. Follows the photo link if needed.

        mbasic pages show a "Photo" link that navigates to a dedicated photo
        upload form with a file input.
        """
        # Check if current page already has a file input
        if 'type="file"' in html:
            form = self._parse_composer_form(html)
            if form:
                return form

        # Look for photo composer link
        photo_link = re.search(
            r'href="(/composer/[^"]*(?:photo|Photo)[^"]*)"', html
        )
        if photo_link:
            photo_url = f"https://mbasic.facebook.com{photo_link.group(1)}"
            try:
                resp = await client.get(photo_url, follow_redirects=True)
                if resp.status_code == 200 and 'type="file"' in resp.text:
                    return self._parse_composer_form(resp.text)
            except Exception as exc:
                logger.debug("Failed to fetch photo form: %s", exc)

        return None


# ── Module-level helpers ────────────────────────────────────────

def _resolve_post_url(page_id: str, fb_post_id: str) -> str:
    """Build the mbasic URL for a specific post."""
    if fb_post_id.isdigit():
        return f"https://mbasic.facebook.com/story.php?story_fbid={fb_post_id}&id={page_id}"
    parts = fb_post_id.split("_")
    if len(parts) == 2:
        return f"https://mbasic.facebook.com/story.php?story_fbid={parts[1]}&id={parts[0]}"
    return f"https://mbasic.facebook.com/{page_id}/posts/{fb_post_id}"


def _synthetic_id(prefix: str, message: str) -> str:
    """Generate a unique synthetic post ID when real extraction fails."""
    unique = f"{prefix}:{message}:{time.time_ns()}"
    return f"synth_{hashlib.md5(unique.encode()).hexdigest()[:16]}"
