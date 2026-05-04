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

import re
import tempfile
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
    fetch_tokens_mobile,
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
            # 1. Load page to find composer form
            composer_url = (
                f"https://mbasic.facebook.com/composer/?mbasic=1"
                f"&target={page_id}"
                f"&redirect_uri=https%3A%2F%2Fmbasic.facebook.com%2F{page_id}"
            )
            resp = await client.get(composer_url, follow_redirects=True)
            if resp.status_code != 200:
                # Fallback: try getting form from the page itself
                resp = await client.get(
                    f"https://mbasic.facebook.com/{page_id}", follow_redirects=True
                )
                resp.raise_for_status()

            html = resp.text

            # 2. Extract form action + hidden fields
            form_data = self._parse_composer_form(html)
            if not form_data:
                raise RuntimeError(
                    f"Could not find composer form for page {page_id}. "
                    "Cookies may be expired or page inaccessible."
                )

            # 3. Fill in message
            form_data["xc_message"] = message
            form_data["view_post"] = "Post"

            # 4. Submit form
            action_url = form_data.pop("__action_url", "")
            if not action_url:
                action_url = f"https://mbasic.facebook.com/composer/?mbasic=1&target={page_id}"

            resp = await client.post(action_url, data=form_data, follow_redirects=True)

            # 5. Extract post ID from response
            post_id = (
                extract_post_id_from_url(str(resp.url))
                or extract_post_id_from_html(resp.text)
            )
            if post_id:
                logger.info("Cookie page post success: %s", post_id)
                return post_id

            # Fallback: return a synthetic ID
            logger.warning("Post submitted but could not extract post_id from response")
            return f"cookie_page_{hash(message) % 10**10}"

    # ── Photo post ──────────────────────────────────────────────

    async def _post_photo(
        self, cookies: dict[str, str], page_id: str, message: str, image_path: str
    ) -> str:
        """Post with photo to a Facebook Page via mbasic.facebook.com."""
        async with create_client(cookies) as client:
            # Load page / composer
            page_url = f"https://mbasic.facebook.com/{page_id}"
            resp = await client.get(page_url, follow_redirects=True)
            resp.raise_for_status()
            html = resp.text

            # Find the photo upload form
            # mbasic has a "Photo/Video" link that leads to a form with file input
            photo_form = self._find_photo_form(html, page_id)
            if not photo_form:
                # Try direct composer with photo
                photo_form = self._parse_composer_form(html)

            if not photo_form:
                raise RuntimeError("Could not find photo upload form")

            form_data = photo_form
            form_data["xc_message"] = message

            # Determine action URL
            action_url = form_data.pop("__action_url", "")
            if not action_url:
                action_url = f"https://mbasic.facebook.com/composer/?mbasic=1&target={page_id}"

            # Submit with file
            files_data: dict[str, Any] = {}
            with open(image_path, "rb") as fh:
                files_data["file1"] = (Path(image_path).name, fh, "image/jpeg")
                # Some mbasic forms use 'add_photo_file_0' instead of 'file1'
                resp = await client.post(
                    action_url,
                    data={**form_data, "add_photo_done": "Post"},
                    files=files_data,
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
            return f"cookie_page_photo_{hash(message) % 10**10}"

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
                    # Check if we see the page content (not a redirect to login)
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
        """Fetch comments by parsing the post page on mbasic.facebook.com.

        Returns list of dicts matching Graph API format for compatibility.
        """
        cookies = parse_cookies(account.fb_cookies)
        comments: list[dict[str, Any]] = []
        try:
            async with create_client(cookies) as client:
                # Try to find the post URL
                post_url = self._resolve_post_url(account.page_id, fb_post_id)
                resp = await client.get(post_url, follow_redirects=True)
                if resp.status_code != 200:
                    return []

                html = resp.text
                # Parse comments from mbasic HTML
                # Comments are in <div> blocks with author name and message
                comment_pattern = re.compile(
                    r'<h3[^>]*>\s*<a[^>]*>([^<]+)</a>.*?</h3>'
                    r'.*?<div[^>]*>([^<]+)</div>',
                    re.DOTALL,
                )
                for match in comment_pattern.finditer(html):
                    author = match.group(1).strip()
                    message = match.group(2).strip()
                    if message:
                        comments.append({
                            "id": f"mbasic_{hash(author + message) % 10**10}",
                            "message": message,
                            "from": {"name": author},
                            "created_time": "",
                        })
        except Exception as exc:
            logger.warning("Cookie fetch_comments failed: %s", exc)
        return comments

    async def reply_comment(self, account: AccountConfig, comment_id: str, message: str) -> bool:
        """Reply to a comment via mbasic.facebook.com form submission."""
        # mbasic comment reply is a form POST on the comment's page
        # This is limited — comment_id here is a synthetic ID from fetch_comments
        logger.info("Cookie reply_comment: limited support, comment_id=%s", comment_id)
        return False  # Not reliably supported for synthetic IDs

    # ── Insights ────────────────────────────────────────────────

    async def fetch_post_insights(self, account: AccountConfig, fb_post_id: str) -> dict[str, Any]:
        """Parse basic engagement counts from the post page on mbasic."""
        cookies = parse_cookies(account.fb_cookies)
        result: dict[str, Any] = {"likes": 0, "comments": 0, "shares": 0, "reach": 0}
        try:
            async with create_client(cookies) as client:
                post_url = self._resolve_post_url(account.page_id, fb_post_id)
                resp = await client.get(post_url, follow_redirects=True)
                if resp.status_code != 200:
                    return result
                html = resp.text

                # Parse like count: "X people reacted"
                like_match = re.search(r'(\d[\d,.]*)\s*(?:people|người)', html)
                if like_match:
                    result["likes"] = int(like_match.group(1).replace(",", "").replace(".", ""))

                # Count comment blocks
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
        # Look for the composer/post form
        # mbasic forms have method="post" and action containing "composer" or "home"
        form_patterns = [
            r'<form[^>]*method="post"[^>]*action="([^"]*(?:composer|home|timeline)[^"]*)"[^>]*>(.*?)</form>',
            r'<form[^>]*action="([^"]*(?:composer|home|timeline)[^"]*)"[^>]*method="post"[^>]*>(.*?)</form>',
            r'<form[^>]*method="post"[^>]*>(.*?)</form>',  # fallback: any POST form
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

            # Resolve relative URL
            if action and action.startswith("/"):
                action = f"https://mbasic.facebook.com{action}"

            # Extract hidden fields
            fields: dict[str, str] = {}
            hidden_pattern = re.compile(
                r'<input[^>]*type="hidden"[^>]*name="([^"]*)"[^>]*value="([^"]*)"',
                re.IGNORECASE,
            )
            for m in hidden_pattern.finditer(form_body):
                fields[m.group(1)] = m.group(2)

            # Also try reverse attribute order (name before type)
            hidden_pattern2 = re.compile(
                r'<input[^>]*name="([^"]*)"[^>]*type="hidden"[^>]*value="([^"]*)"',
                re.IGNORECASE,
            )
            for m in hidden_pattern2.finditer(form_body):
                if m.group(1) not in fields:
                    fields[m.group(1)] = m.group(2)

            # Ensure fb_dtsg and jazoest are present
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

        # Last resort: try to get tokens from page directly
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

    def _find_photo_form(self, html: str, page_id: str) -> dict[str, str] | None:
        """Find the photo upload form link/form on the page."""
        # Look for a link to the photo composer
        photo_link = re.search(
            r'href="(/composer/[^"]*(?:photo|photo)[^"]*)"', html
        )
        if photo_link:
            photo_url = f"https://mbasic.facebook.com{photo_link.group(1)}"
            # We'll need to GET this URL and parse the form — deferred to caller
            pass

        # Look for inline photo form
        return self._parse_composer_form(html)

    def _resolve_post_url(self, page_id: str, fb_post_id: str) -> str:
        """Build the mbasic URL for a specific post."""
        # Try various URL formats
        if fb_post_id.isdigit():
            return f"https://mbasic.facebook.com/story.php?story_fbid={fb_post_id}&id={page_id}"
        # If it's a composite ID like "pageid_postid"
        parts = fb_post_id.split("_")
        if len(parts) == 2:
            return f"https://mbasic.facebook.com/story.php?story_fbid={parts[1]}&id={parts[0]}"
        return f"https://mbasic.facebook.com/{page_id}/posts/{fb_post_id}"
