"""Shared utilities for cookie-based Facebook drivers.

Handles: cookie parsing, CSRF token extraction, httpx client factory.
Supports two cookie formats:
  - JSON array (EditThisCookie extension format): [{"name":"c_user","value":"123",...},...]
  - Semicolon-separated string: "c_user=123; xs=abc; ..."
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from common.logging import get_logger

logger = get_logger(__name__)

# Default browser headers to look like a real Chrome session
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}


# ── Cookie parsing ──────────────────────────────────────────────

def parse_cookies(raw: str) -> dict[str, str]:
    """Parse cookies from JSON array or semicolon-separated string.

    Returns dict of {cookie_name: cookie_value}.
    Essential cookies for Facebook: c_user, xs, fr, datr, sb.
    """
    if not raw:
        return {}
    raw = raw.strip()

    # Try JSON array first (EditThisCookie / browser export format)
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return {
                c["name"]: c["value"]
                for c in data
                if isinstance(c, dict) and "name" in c and "value" in c
            }
    except (json.JSONDecodeError, TypeError):
        pass

    # Try semicolon-separated: "name1=val1; name2=val2"
    result: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            name = name.strip()
            value = value.strip()
            if name:
                result[name] = value
    return result


def build_cookie_header(cookies: dict[str, str]) -> str:
    """Build a Cookie header string from a dict."""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def validate_essential_cookies(cookies: dict[str, str]) -> tuple[bool, str]:
    """Check that essential Facebook session cookies are present.

    Returns (is_valid, reason).
    Minimum required: c_user (user ID) and xs (session token).
    """
    if not cookies:
        return False, "no cookies provided"
    if "c_user" not in cookies:
        return False, "missing 'c_user' cookie — login session not found"
    if "xs" not in cookies:
        return False, "missing 'xs' cookie — session token not found"
    return True, "ok"


# ── Token extraction from HTML ─────────────────────────────────

def extract_fb_dtsg(html: str) -> str | None:
    """Extract fb_dtsg CSRF token from Facebook HTML page."""
    patterns = [
        r'"DTSGInitialData".*?"token"\s*:\s*"([^"]+)"',
        r'"token"\s*:\s*"([^"]+)".*?"DTSGInitialData"',
        r'name="fb_dtsg"\s+value="([^"]+)"',
        r'"fb_dtsg"\s*:\s*\[.*?"token"\s*:\s*"([^"]+)"',
        r'fb_dtsg.*?value="([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.DOTALL)
        if match:
            return match.group(1)
    return None


def extract_jazoest(html: str) -> str | None:
    """Extract jazoest anti-CSRF token from Facebook HTML."""
    patterns = [
        r'name="jazoest"\s+value="(\d+)"',
        r'"jazoest"\s*:\s*"?(\d+)"?',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return None


def extract_user_id(html: str) -> str | None:
    """Extract logged-in user's Facebook ID from page source."""
    patterns = [
        r'"USER_ID"\s*:\s*"(\d+)"',
        r'"actorID"\s*:\s*"(\d+)"',
        r'"viewerID"\s*:\s*"(\d+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return None


# ── Token extraction via requests ──────────────────────────────

async def fetch_tokens(client: httpx.AsyncClient) -> tuple[str | None, str | None, str | None]:
    """Get fb_dtsg, jazoest, and user_id from Facebook homepage.

    Returns (fb_dtsg, jazoest, user_id). Any may be None on failure.
    """
    try:
        resp = await client.get("https://www.facebook.com/", follow_redirects=True)
        if resp.status_code != 200:
            logger.warning("Facebook homepage returned %d", resp.status_code)
            return None, None, None
        html = resp.text
        return extract_fb_dtsg(html), extract_jazoest(html), extract_user_id(html)
    except Exception as exc:
        logger.warning("fetch_tokens failed: %s", exc)
        return None, None, None


async def fetch_tokens_mobile(client: httpx.AsyncClient) -> tuple[str | None, str | None, str | None]:
    """Same as fetch_tokens but from m.facebook.com (mobile site)."""
    try:
        resp = await client.get("https://m.facebook.com/home.php", follow_redirects=True)
        if resp.status_code != 200:
            return None, None, None
        html = resp.text
        return extract_fb_dtsg(html), extract_jazoest(html), extract_user_id(html)
    except Exception as exc:
        logger.warning("fetch_tokens_mobile failed: %s", exc)
        return None, None, None


# ── Client factory ─────────────────────────────────────────────

def create_client(cookies: dict[str, str], *, mobile: bool = False) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient with Facebook cookies and realistic browser headers."""
    headers = dict(_BROWSER_HEADERS)
    if mobile:
        headers["User-Agent"] = (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.5 Mobile/15E148 Safari/604.1"
        )
    return httpx.AsyncClient(
        timeout=60.0,
        follow_redirects=True,
        headers=headers,
        cookies=cookies,
    )


# ── Post ID extraction ─────────────────────────────────────────

def extract_post_id_from_url(url: str) -> str | None:
    """Try to extract a Facebook post ID from a URL."""
    patterns = [
        r'story_fbid=(\d+)',
        r'/posts/(\d+)',
        r'/permalink/(\d+)',
        r'/feed/post/(\d+)',
        r'"post_id":"(\d+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def extract_post_id_from_html(html: str) -> str | None:
    """Try to extract a post ID from HTML response body."""
    patterns = [
        r'"post_id"\s*:\s*"(\d+)"',
        r'story_fbid=(\d+)',
        r'data-post-id="(\d+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return None
