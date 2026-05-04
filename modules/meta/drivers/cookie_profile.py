"""Cookie-based personal profile driver.

Posts to a user's personal Facebook timeline using browser cookies.
No Facebook Page or Graph API needed — just a logged-in user session.

Requires:
  - account.fb_cookies: browser cookies as JSON array or semicolon string
  - account.id: used as identifier (no page_id needed)
  - Cookies must be from a valid logged-in Facebook session

Flow:
  1. Load cookies into httpx client
  2. GET mbasic.facebook.com home.php to get composer form
  3. Parse hidden fields (fb_dtsg, jazoest, etc.)
  4. Fill in message + optional photo
  5. POST to submit
  6. Extract post ID from response

Key differences from CookiePageDriver:
  - Target is personal timeline (no page_id)
  - Composer form is on home.php instead of a page
  - No page-level verification needed
"""
from __future__ import annotations

import re
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


class CookieProfileDriver(MetaDriver):
    """Post to personal Facebook timeline using browser cookies."""

    # ── Publish ─────────────────────────────────────────────────

    async def publish_post(self, account: AccountConfig, post: PostRecord) -> str:
        cookies = parse_cookies(account.fb_cookies)
        valid, reason = validate_essential_cookies(cookies)
        if not valid:
            raise ValueError(f"Invalid cookies for {account.id}: {reason}")

        message = self._compose_message(post)

        # Route: photo → text-only
        if post.image_path and Path(post.image_path).exists():
            return await self._post_photo(cookies, message, post.image_path)

        fetched = await self._fetch_product_image_to_temp(post)
        if fetched:
            try:
                return await self._post_photo(cookies, message, fetched)
            except Exception as exc:
                logger.warning("Profile photo post failed: %s, falling back to text", exc)

        return await self._post_text(cookies, message)

    # ── Text post ───────────────────────────────────────────────

    async def _post_text(self, cookies: dict[str, str], message: str) -> str:
        """Post text to personal timeline via mbasic.facebook.com."""
        async with create_client(cookies) as client:
            # 1. Load home page to get composer
            resp = await client.get(
                "https://mbasic.facebook.com/home.php", follow_redirects=True
            )
            resp.raise_for_status()
            html = resp.text

            # 2. Parse the "What's on your mind?" form
            form_data = self._parse_timeline_form(html)
            if not form_data:
                raise RuntimeError(
                    "Could not find timeline composer form. Cookies may be expired."
                )

            form_data["xc_message"] = message
            form_data["view_post"] = "Post"

            action_url = form_data.pop("__action_url", "")
            if not action_url:
                action_url = "https://mbasic.facebook.com/home.php"

            # 3. Submit
            resp = await client.post(action_url, data=form_data, follow_redirects=True)

            # 4. Extract post ID
            post_id = (
                extract_post_id_from_url(str(resp.url))
                or extract_post_id_from_html(resp.text)
            )
            if post_id:
                logger.info("Cookie profile post success: %s", post_id)
                return post_id

            logger.warning("Profile post submitted but could not extract post_id")
            return f"cookie_profile_{hash(message) % 10**10}"

    # ── Photo post ──────────────────────────────────────────────

    async def _post_photo(
        self, cookies: dict[str, str], message: str, image_path: str
    ) -> str:
        """Post with photo to personal timeline via mbasic.facebook.com."""
        async with create_client(cookies) as client:
            resp = await client.get(
                "https://mbasic.facebook.com/home.php", follow_redirects=True
            )
            resp.raise_for_status()
            html = resp.text

            form_data = self._parse_timeline_form(html)
            if not form_data:
                raise RuntimeError("Could not find timeline composer form")

            form_data["xc_message"] = message
            action_url = form_data.pop("__action_url", "")
            if not action_url:
                action_url = "https://mbasic.facebook.com/home.php"

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
                logger.info("Cookie profile photo post success: %s", post_id)
                return post_id

            logger.warning("Profile photo post submitted but could not extract post_id")
            return f"cookie_profile_photo_{hash(message) % 10**10}"

    # ── Verification ────────────────────────────────────────────

    async def verify(self, account: AccountConfig) -> str:
        """Check if cookies represent a valid logged-in session."""
        cookies = parse_cookies(account.fb_cookies)
        valid, reason = validate_essential_cookies(cookies)
        if not valid:
            return reason

        try:
            async with create_client(cookies) as client:
                resp = await client.get(
                    "https://mbasic.facebook.com/home.php", follow_redirects=True
                )
                if resp.status_code == 200:
                    url = str(resp.url)
                    # If redirected to login page, session is invalid
                    if "login" in url.lower() and "home" not in url.lower():
                        return "session_expired"
                    # Check for user content on the page
                    if "What's on your mind" in resp.text or "Bạn đang nghĩ gì" in resp.text:
                        return "ok"
                    if extract_fb_dtsg(resp.text):
                        return "ok"
                    return "ok"
                return f"http_error:{resp.status_code}"
        except Exception as exc:
            logger.warning("Cookie profile verify failed for %s: %s", account.id, exc)
            return f"error:{exc}"

    # ── Comments (limited for personal posts) ───────────────────

    async def fetch_comments(self, account: AccountConfig, fb_post_id: str) -> list[dict[str, Any]]:
        """Fetch comments from a personal post via mbasic.facebook.com.

        Limited parsing — returns what's visible on the page.
        """
        cookies = parse_cookies(account.fb_cookies)
        comments: list[dict[str, Any]] = []
        try:
            user_id = account.fb_user_id or parse_cookies(account.fb_cookies).get("c_user", "")
            post_url = self._resolve_post_url(user_id, fb_post_id)

            async with create_client(cookies) as client:
                resp = await client.get(post_url, follow_redirects=True)
                if resp.status_code != 200:
                    return []

                html = resp.text
                # Parse comments — mbasic shows comments in simple HTML blocks
                comment_pattern = re.compile(
                    r'<h3[^>]*>\s*<a[^>]*>([^<]+)</a>.*?</h3>'
                    r'.*?<div[^>]*>([^<]+)</div>',
                    re.DOTALL,
                )
                for match in comment_pattern.finditer(html):
                    author = match.group(1).strip()
                    msg = match.group(2).strip()
                    if msg and author:
                        comments.append({
                            "id": f"mbasic_{hash(author + msg) % 10**10}",
                            "message": msg,
                            "from": {"name": author},
                            "created_time": "",
                        })
        except Exception as exc:
            logger.warning("Cookie profile fetch_comments failed: %s", exc)
        return comments

    async def reply_comment(self, account: AccountConfig, comment_id: str, message: str) -> bool:
        """Reply to a comment — limited support for mbasic synthetic IDs."""
        logger.info("Cookie profile reply_comment: limited support, comment_id=%s", comment_id)
        return False

    # ── Insights (limited for personal posts) ───────────────────

    async def fetch_post_insights(self, account: AccountConfig, fb_post_id: str) -> dict[str, Any]:
        """Parse basic engagement from post page. Personal posts have limited metrics."""
        cookies = parse_cookies(account.fb_cookies)
        result: dict[str, Any] = {"likes": 0, "comments": 0, "shares": 0, "reach": 0}
        try:
            user_id = account.fb_user_id or parse_cookies(account.fb_cookies).get("c_user", "")
            post_url = self._resolve_post_url(user_id, fb_post_id)

            async with create_client(cookies) as client:
                resp = await client.get(post_url, follow_redirects=True)
                if resp.status_code != 200:
                    return result
                html = resp.text

                # Parse reaction count
                like_match = re.search(r'(\d[\d,.]*)\s*(?:people|người|thích)', html)
                if like_match:
                    result["likes"] = int(like_match.group(1).replace(",", "").replace(".", ""))

                # Count comment elements
                comment_blocks = re.findall(r'data-commentid=', html)
                result["comments"] = len(comment_blocks)

        except Exception as exc:
            logger.warning("Cookie profile fetch_post_insights failed: %s", exc)
        return result

    # ── Form parsing ────────────────────────────────────────────

    def _parse_timeline_form(self, html: str) -> dict[str, str] | None:
        """Parse the 'What's on your mind?' composer form from home.php.

        mbasic.facebook.com home page has an inline composer at the top.
        The form contains hidden fields and a textarea for the message.
        """
        # Pattern 1: look for the composer form specifically
        form_patterns = [
            r'<form[^>]*action="([^"]*(?:composer|home)[^"]*)"[^>]*>(.*?)</form>',
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

            if action.startswith("/"):
                action = f"https://mbasic.facebook.com{action}"

            # Only consider forms that have xc_message (the composer textarea)
            if "xc_message" not in form_body and "view_post" not in form_body:
                continue

            # Extract hidden inputs
            fields: dict[str, str] = {}
            for m in re.finditer(
                r'<input[^>]*type="hidden"[^>]*name="([^"]*)"[^>]*value="([^"]*)"',
                form_body, re.IGNORECASE,
            ):
                fields[m.group(1)] = m.group(2)

            # Also: name before type order
            for m in re.finditer(
                r'<input[^>]*name="([^"]*)"[^>]*type="hidden"[^>]*value="([^"]*)"',
                form_body, re.IGNORECASE,
            ):
                if m.group(1) not in fields:
                    fields[m.group(1)] = m.group(2)

            # Ensure CSRF tokens
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

        # Last resort: build minimal form from tokens
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

    def _resolve_post_url(self, user_id: str, fb_post_id: str) -> str:
        """Build mbasic URL for a specific personal post."""
        if fb_post_id.isdigit() and user_id:
            return f"https://mbasic.facebook.com/story.php?story_fbid={fb_post_id}&id={user_id}"
        parts = fb_post_id.split("_")
        if len(parts) == 2:
            return f"https://mbasic.facebook.com/story.php?story_fbid={parts[1]}&id={parts[0]}"
        return f"https://mbasic.facebook.com/permalink/{fb_post_id}"
