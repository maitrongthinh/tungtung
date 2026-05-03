from __future__ import annotations

import asyncio
import json
from typing import Any

from common.logging import get_logger

logger = get_logger(__name__)

_ENDPOINT = "https://affiliate.shopee.vn/api/v3/gql?q=batchCustomLink"
_PORTAL_URL = "https://affiliate.shopee.vn/tool/customLink"

_GQL_QUERY = (
    "query batchGetCustomLink($linkParams:[CustomLinkParam!],$sourceCaller:SourceCaller)"
    "{batchCustomLink(linkParams:$linkParams,sourceCaller:$sourceCaller)"
    "{shortLink longLink failCode}}"
)


class CookieAffiliateClient:
    """
    Tạo affiliate link Shopee qua API batchCustomLink.
    Dùng Playwright để chạy fetch() từ trong browser context (giữ nguyên
    device fingerprint và CSRF mà Shopee yêu cầu).
    """

    def __init__(self, cookie_source: str | list[dict[str, Any]]) -> None:
        self._cookie_source = cookie_source
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate_link(self, product_url: str) -> str | None:
        results = await self.generate_links_batch([product_url])
        return results.get(product_url)

    async def generate_links_batch(self, product_urls: list[str]) -> dict[str, str]:
        if not product_urls:
            return {}
        async with self._lock:
            return await self._run_batch(product_urls)

    async def validate_cookie(self) -> bool:
        try:
            results = await self._run_batch(
                ["https://shopee.vn/product/123456789/987654321"],
                expect_valid=False,
            )
            return True  # nếu không raise _AuthError thì cookie sống
        except _AuthError:
            return False
        except Exception:
            return True

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run_batch(
        self,
        product_urls: list[str],
        *,
        expect_valid: bool = True,
    ) -> dict[str, str]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("Playwright not installed - cookie affiliate not available")
            return {}

        cookies = self._parse_cookies()
        results: dict[str, str] = {}

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    locale="vi-VN",
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/123.0.0.0 Safari/537.36"
                    ),
                )
                if cookies:
                    await context.add_cookies(cookies)

                page = await context.new_page()
                # Mở trang portal trước để browser lấy đúng fingerprint context
                await page.goto(_PORTAL_URL, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(1500)

                # Chia batch max 10 URL mỗi lần
                for i in range(0, len(product_urls), 10):
                    chunk = product_urls[i : i + 10]
                    chunk_result = await self._fetch_batch_in_page(page, chunk)
                    results.update(chunk_result)
                    if i + 10 < len(product_urls):
                        await asyncio.sleep(1.5)
            finally:
                await browser.close()

        return results

    async def _fetch_batch_in_page(self, page: Any, product_urls: list[str]) -> dict[str, str]:
        body = {
            "operationName": "batchGetCustomLink",
            "query": _GQL_QUERY,
            "variables": {
                "linkParams": [
                    {"originalLink": url, "advancedLinkParams": {}}
                    for url in product_urls
                ],
                "sourceCaller": "CUSTOM_LINK_CALLER",
            },
        }
        body_json = json.dumps(body)

        js = f"""
        async () => {{
            const resp = await fetch("{_ENDPOINT}", {{
                method: "POST",
                headers: {{
                    "content-type": "application/json",
                    "referer": "{_PORTAL_URL}",
                }},
                credentials: "include",
                body: {repr(body_json)},
            }});
            const data = await resp.json();
            return {{ status: resp.status, body: data }};
        }}
        """

        raw = await page.evaluate(js)
        status = raw.get("status", 0)
        data = raw.get("body", {})

        if status in (401, 403):
            is_login = data.get("is_login", False)
            if not is_login:
                raise _AuthError(f"Cookie expired or invalid: HTTP {status}")
            raise _AuthError(f"Device fingerprint rejected by Shopee: HTTP {status} error={data.get('error')}")

        if data.get("errors"):
            error_msg = data["errors"][0].get("message", "unknown")
            if "no login" in error_msg.lower():
                raise _AuthError("Cookie invalid: no login")
            raise RuntimeError(f"GraphQL error: {error_msg}")

        nodes = data.get("data", {}).get("batchCustomLink", []) or []
        results: dict[str, str] = {}
        for url, node in zip(product_urls, nodes):
            fail_code = node.get("failCode")
            if fail_code:  # 0 = success, non-zero = error
                logger.warning("batchCustomLink failCode=%s for %s", fail_code, url[:60])
                continue
            short_link = node.get("shortLink")
            if short_link:
                results[url] = short_link
                logger.info("Generated affiliate link: %s → %s", url[:50], short_link)
        return results

    def _parse_cookies(self) -> list[dict[str, Any]]:
        src = self._cookie_source
        if isinstance(src, list):
            return self._normalize_cookie_list(src)
        if isinstance(src, str):
            stripped = src.strip()
            # JSON array/object
            if stripped.startswith("[") or stripped.startswith("{"):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, list):
                        return self._normalize_cookie_list(parsed)
                    if isinstance(parsed, dict):
                        return self._normalize_cookie_list([parsed])
                except json.JSONDecodeError:
                    pass
            # File path
            try:
                from pathlib import Path
                path = Path(stripped)
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        return self._normalize_cookie_list(data)
            except Exception:
                pass
            # "key=value; key2=value2" header string
            return self._parse_header_string(stripped)
        return []

    def _normalize_cookie_list(self, cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for cookie in cookies:
            name = str(cookie.get("name", ""))
            value = str(cookie.get("value", ""))
            if not name:
                continue
            domain = str(cookie.get("domain", ".shopee.vn"))
            entry: dict[str, Any] = {
                "name": name,
                "value": value,
                "domain": domain,
                "path": str(cookie.get("path", "/")),
            }
            same_site = cookie.get("sameSite") or cookie.get("same_site")
            if same_site in ("Strict", "Lax", "None"):
                entry["sameSite"] = same_site
            if cookie.get("httpOnly") is not None:
                entry["httpOnly"] = bool(cookie["httpOnly"])
            if cookie.get("secure") is not None:
                entry["secure"] = bool(cookie["secure"])
            normalized.append(entry)
            # AC_CERT_D is set on .shopee.vn but also needed by affiliate.shopee.vn
            if name == "AC_CERT_D" and domain in (".shopee.vn", "shopee.vn"):
                affiliate_entry = dict(entry)
                affiliate_entry["domain"] = "affiliate.shopee.vn"
                affiliate_entry.pop("httpOnly", None)
                normalized.append(affiliate_entry)
        return normalized

    def _parse_header_string(self, header: str) -> list[dict[str, Any]]:
        cookies: list[dict[str, Any]] = []
        for part in header.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            name, _, value = part.partition("=")
            name = name.strip()
            if name:
                cookies.append({"name": name, "value": value.strip(), "domain": ".shopee.vn", "path": "/"})
        return cookies


class _AuthError(Exception):
    pass


def load_cookie_client_from_config() -> CookieAffiliateClient | None:
    try:
        from common.config import load_settings
        settings = load_settings(refresh=True)
        cookie = getattr(settings.integrations, "shopee_affiliate_cookie", "")
        if cookie and str(cookie).strip():
            return CookieAffiliateClient(cookie)
    except Exception:
        pass
    return None
