from __future__ import annotations

import asyncio
import math
import random
import re
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from PIL import Image
try:
    from playwright.async_api import Browser, BrowserContext, Page, async_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False
    Browser = None  # type: ignore
    BrowserContext = None  # type: ignore
    Page = None  # type: ignore

from common.config import load_settings
from common.logging import get_logger
from common.models import ProductRecord
from modules.shopee.affiliate_api import ShopeeAffiliateAPI
from modules.shopee.proxy_pool import ProxyPool
from modules.shopee.rate_limiter import TokenBucketRateLimiter

logger = get_logger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
]
VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1920, "height": 1080},
    {"width": 1280, "height": 800},
]

# Fallback search terms mỗi category → thử nhiều từ khóa khi bị block
CATEGORY_FALLBACKS: dict[str, list[str]] = {
    "điện thoại": ["điện thoại thông minh", "smartphone", "iphone", "samsung galaxy", "điện thoại giá rẻ"],
    "tai nghe": ["tai nghe bluetooth", "earphone", "headphone", "tai nghe không dây", "airpods"],
    "laptop": ["máy tính xách tay", "laptop gaming", "notebook", "laptop văn phòng"],
    "gia dụng": ["đồ gia dụng", "máy xay sinh tố", "nồi cơm điện", "lò vi sóng"],
    "thời trang": ["quần áo", "áo thun", "váy đầm", "quần jean", "áo khoác"],
    "mỹ phẩm": ["kem dưỡng da", "son môi", "serum", "skincare", "make up"],
    "thực phẩm": ["đồ ăn vặt", "snack", "trà sữa", "cà phê", "bánh kẹo"],
    "sách": ["sách bán chạy", "sách self help", "tiểu thuyết", "sách kỹ năng"],
    "thể thao": ["giày thể thao", "dụng cụ gym", "áo thể thao", "bóng đá"],
    "đồ chơi": ["đồ chơi trẻ em", "lego", "đồ chơi giáo dục", "mô hình"],
}

# Shopee API endpoints thay thế
SHOPEE_API_ENDPOINTS = [
    "https://shopee.vn/api/v4/search/search_items",
    "https://shopee.vn/api/v2/search_items",
]

# Sắp xếp kết quả theo nhiều tiêu chí khác nhau để đa dạng
SORT_BY_OPTIONS = ["relevancy", "sales", "price_asc", "price_desc", "ctime"]


# Reusable browser instance for the crawler
_shared_browser = None
_playwright_context = None

async def _get_shared_browser():
    """Get or create a shared Playwright browser instance."""
    global _shared_browser, _playwright_context
    if _shared_browser is None or not _shared_browser.is_connected():
        from playwright.async_api import async_playwright
        _playwright_context = await async_playwright().__aenter__()
        _shared_browser = await _playwright_context.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-plugins",
            ],
        )
    return _shared_browser

async def close_shared_browser():
    """Cleanup shared browser on shutdown."""
    global _shared_browser, _playwright_context
    if _shared_browser:
        try:
            await _shared_browser.close()
        except Exception:
            pass
        _shared_browser = None
    if _playwright_context:
        try:
            await _playwright_context.__aexit__(None, None, None)
        except Exception:
            pass
        _playwright_context = None


class ShopeeCrawler:
    def __init__(
        self,
        rate_limiter: TokenBucketRateLimiter,
        proxy_pool: ProxyPool,
        affiliate_api: ShopeeAffiliateAPI,
    ) -> None:
        self.settings = load_settings()
        self.rate_limiter = rate_limiter
        self.proxy_pool = proxy_pool
        self.affiliate_api = affiliate_api
        self.assets_root = Path(self.settings.farm_dir / "assets")

    async def crawl_categories(self, categories: list[str], limit_per_category: int = 20) -> list[ProductRecord]:
        deduped: dict[str, ProductRecord] = {}
        # Tăng limit mỗi lần cào để lấy được nhiều hơn
        effective_limit = max(limit_per_category, 30)

        if not _HAS_PLAYWRIGHT:
            logger.warning("Playwright not installed - using API-only crawl mode")
        browser = await _get_shared_browser() if _HAS_PLAYWRIGHT else None
        for category in categories:
            if not browser:
                break
            sort_options = random.sample(SORT_BY_OPTIONS, min(3, len(SORT_BY_OPTIONS)))
            for sort_by in sort_options:
                if len(deduped) >= self.settings.shopee.max_products_per_cycle:
                    break
                products = await self._crawl_category_with_browser(
                    browser, category, limit=effective_limit, sort_by=sort_by
                )
                new_count = 0
                for product in products:
                    if product.product_id not in deduped:
                        deduped[product.product_id] = product
                        new_count += 1
                if new_count > 0:
                    logger.info("Category %s sort=%s: +%d products (total=%d)", category, sort_by, new_count, len(deduped))
                if new_count >= effective_limit * 0.8:
                    break
            cat_count = sum(1 for p in deduped.values() if p.category == category)
            if cat_count < 5:
                await self._crawl_with_fallback_keywords(browser, category, effective_limit, deduped)
        return list(deduped.values())

    async def _crawl_with_fallback_keywords(
        self,
        browser: Browser,
        category: str,
        limit: int,
        deduped: dict[str, ProductRecord],
    ) -> None:
        """Thử các từ khóa fallback khi category chính bị block hoặc ít kết quả."""
        fallbacks = CATEGORY_FALLBACKS.get(category, [])
        # Tạo fallback động cho bất kỳ category nào không có trong dict
        if not fallbacks:
            fallbacks = [
                f"{category} giá rẻ",
                f"{category} chính hãng",
                f"mua {category}",
                f"{category} sale",
            ]
        for keyword in fallbacks[:3]:
            if len(deduped) >= self.settings.shopee.max_products_per_cycle:
                break
            products = await self._crawl_category_with_browser(browser, keyword, limit=limit // 2, sort_by="relevancy")
            new_count = 0
            for product in products:
                product.category = category  # giữ nguyên category gốc
                if product.product_id not in deduped:
                    deduped[product.product_id] = product
                    new_count += 1
            if new_count > 0:
                logger.info("Fallback keyword '%s' for '%s': +%d products", keyword, category, new_count)

    async def _crawl_category_with_browser(
        self,
        browser: Browser,
        category: str,
        limit: int = 20,
        sort_by: str = "relevancy",
    ) -> list[ProductRecord]:
        session_key = f"category:{category}:{sort_by}"
        proxy_url = self.proxy_pool.acquire(session_key)
        logger.info("Crawling Shopee category=%s sort=%s limit=%d", category, sort_by, limit)
        try:
            # Thử API với nhiều sort options và pages
            api_products = await self._search_via_api_multi_page(category, limit=limit, proxy_url=proxy_url, sort_by=sort_by)
            if api_products:
                logger.info("API search succeeded for %s (sort=%s): %d products", category, sort_by, len(api_products))
                return api_products

            # Fallback Playwright
            logger.info("API empty for %s sort=%s, trying Playwright", category, sort_by)
            search_url = f"https://shopee.vn/search?keyword={quote(category)}&by={sort_by}&order=desc"
            context_kwargs: dict[str, Any] = {
                "user_agent": random.choice(USER_AGENTS),
                "viewport": random.choice(VIEWPORTS),
                "locale": "vi-VN",
            }
            if proxy_url:
                context_kwargs["proxy"] = {"server": proxy_url}
            context = await browser.new_context(**context_kwargs)
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            try:
                cookie_str = self.settings.integrations.shopee_affiliate_cookie or ""
                if cookie_str:
                    import json as _json
                    parsed = _json.loads(cookie_str) if cookie_str.strip().startswith("[") else []
                    if parsed:
                        await context.add_cookies([
                            {"name": str(c["name"]), "value": str(c["value"]), "domain": c.get("domain", ".shopee.vn"), "path": c.get("path", "/")}
                            for c in parsed if c.get("name") and c.get("value") is not None
                        ])
            except Exception:
                pass
            try:
                page = await context.new_page()
                await self._paced_navigation(page, search_url)
                candidates = await self._extract_listing_cards(page, category, limit=limit * 2)
                products: list[ProductRecord] = []
                for candidate in candidates[:limit]:
                    try:
                        product = await self._enrich_product(context, candidate, category)
                        if not product.product_url:
                            continue
                        canonical = self._canonical_product_url(product.product_url)
                        product.affiliate_link = await self.affiliate_api.generate_affiliate_link(canonical)
                        if self.settings.features.download_assets:
                            image_path = await self.download_best_image(product)
                            product.image_path = str(image_path) if image_path else None
                        products.append(product)
                    except Exception as exc:
                        logger.warning("Failed to enrich product in %s: %s", category, exc)
                return products
            finally:
                await context.close()
        except Exception as exc:
            logger.warning("Crawler degraded for %s: %s", category, exc)
            await self.rate_limiter.throttle_to(self.settings.shopee.degraded_rate_limit_per_second)
            if proxy_url:
                await self.proxy_pool.report_failure(proxy_url, exc)
            return []
        finally:
            self.proxy_pool.release(session_key)

    async def crawl_category(self, category: str, limit: int = 20) -> list[ProductRecord]:
        if not _HAS_PLAYWRIGHT:
            logger.warning("Playwright not installed - using API-only crawl mode")
        browser = await _get_shared_browser() if _HAS_PLAYWRIGHT else None
        return await self._crawl_category_with_browser(browser, category, limit=limit)

    async def _paced_navigation(self, page: Page, url: str) -> None:
        await self.rate_limiter.acquire()
        await page.goto(url, wait_until="domcontentloaded", timeout=self.settings.shopee.request_timeout_seconds * 1000)
        await page.wait_for_timeout(random.randint(5000, 9000))
        body_text = await page.evaluate("() => document.body.innerText.slice(0, 200)")
        if "Lỗi tải" in body_text or "loading error" in body_text.lower():
            logger.info("Shopee loading error detected, retrying page navigation")
            await page.reload(wait_until="domcontentloaded", timeout=self.settings.shopee.request_timeout_seconds * 1000)
            await page.wait_for_timeout(random.randint(7000, 11000))
        # Random scroll để giả người dùng thật
        scroll_amount = random.randint(300, 900)
        await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
        await page.wait_for_timeout(random.randint(400, 900))
        await self._random_delay()

    async def _extract_listing_cards(self, page: Page, category: str, limit: int) -> list[dict[str, Any]]:
        await page.wait_for_timeout(1500)
        cards = await page.evaluate(
            """
            (limit) => {
              const anchors = Array.from(document.querySelectorAll('a[href*="-i."]')).slice(0, limit);
              return anchors.map((anchor) => {
                const text = anchor.innerText || '';
                const image = anchor.querySelector('img');
                return {
                  href: anchor.href,
                  text,
                  image: image?.src || image?.getAttribute('data-src') || ''
                };
              });
            }
            """,
            limit,
        )
        products: list[dict[str, Any]] = []
        for card in cards:
            href = card.get("href") or ""
            product_id = self._extract_product_id(href)
            if not product_id:
                continue
            text = card.get("text") or ""
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            name = lines[0] if lines else f"{category} {product_id}"
            price = self._parse_vnd(text)
            sold_count = self._extract_sold_count(text)
            products.append(
                {
                    "product_id": product_id,
                    "name": name,
                    "price": price,
                    "sold_count": sold_count,
                    "product_url": href,
                    "images": [card.get("image")] if card.get("image") else [],
                }
            )
        return products

    async def _enrich_product(self, context: BrowserContext, candidate: dict[str, Any], category: str) -> ProductRecord:
        page = await context.new_page()
        try:
            await self._paced_navigation(page, candidate["product_url"])
            details = await page.evaluate(
                """
                () => {
                  const text = document.body.innerText || '';
                  const images = Array.from(document.querySelectorAll('img'))
                    .map((img) => img.src || img.getAttribute('data-src') || '')
                    .filter(Boolean)
                    .slice(0, 12);
                  const ldJsonScripts = Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
                    .map((item) => item.textContent || '');
                  return { text, images, ldJsonScripts };
                }
                """
            )
            parsed = self._parse_detail_blob(details["text"], details["ldJsonScripts"])
            product = ProductRecord(
                product_id=candidate["product_id"],
                name=parsed.get("name") or candidate["name"],
                price=parsed.get("price") or candidate.get("price") or 0.0,
                original_price=parsed.get("original_price") or parsed.get("price") or candidate.get("price") or 0.0,
                discount_percent=parsed.get("discount_percent") or 0.0,
                sold_count=parsed.get("sold_count") or candidate.get("sold_count") or 0,
                rating=parsed.get("rating") or 0.0,
                review_count=parsed.get("review_count") or 0,
                shop_name=parsed.get("shop_name") or "",
                shop_rating=parsed.get("shop_rating") or 0.0,
                category=category,
                subcategory=parsed.get("subcategory") or "",
                images=details["images"] or candidate.get("images") or [],
                product_url=candidate["product_url"],
            )
            return product
        finally:
            await page.close()

    def _parse_detail_blob(self, text: str, ld_json_scripts: list[str]) -> dict[str, Any]:
        parsed: dict[str, Any] = {
            "price": self._parse_vnd(text),
            "original_price": self._parse_original_price(text),
            "discount_percent": self._extract_discount(text),
            "sold_count": self._extract_sold_count(text),
            "rating": self._extract_rating(text),
            "review_count": self._extract_reviews(text),
            "shop_name": self._extract_shop_name(text),
            "subcategory": "",
        }
        for script in ld_json_scripts:
            try:
                import json

                obj = json.loads(script)
            except Exception:
                continue
            if isinstance(obj, dict):
                if obj.get("name"):
                    parsed["name"] = obj["name"]
                offers = obj.get("offers")
                if isinstance(offers, dict) and offers.get("price"):
                    parsed["price"] = float(offers["price"])
                aggregate = obj.get("aggregateRating")
                if isinstance(aggregate, dict):
                    parsed["rating"] = float(aggregate.get("ratingValue") or parsed.get("rating") or 0.0)
                    parsed["review_count"] = int(float(aggregate.get("reviewCount") or aggregate.get("ratingCount") or 0))
        return parsed

    async def download_best_image(self, product: ProductRecord) -> Path | None:
        if not product.images:
            return None
        product_dir = self.assets_root / product.product_id
        product_dir.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            for image_url in product.images:
                try:
                    response = await client.get(image_url)
                    response.raise_for_status()
                    image = Image.open(BytesIO(response.content)).convert("RGB")
                    if not self._is_usable_image(image):
                        continue
                    image.thumbnail((1200, 1200))
                    output_path = product_dir / "cover.jpg"
                    image.save(output_path, format="JPEG", quality=85, optimize=True)
                    return output_path
                except Exception as exc:
                    logger.debug("Skipping image %s for %s: %s", image_url, product.product_id, exc)
        return None

    async def _search_via_api(self, category: str, limit: int = 20, proxy_url: str | None = None, sort_by: str = "relevancy") -> list[ProductRecord]:
        """Search Shopee via httpx API — page 0 only."""
        return await self._search_via_api_page(category, limit=limit, proxy_url=proxy_url, sort_by=sort_by, newest=0)

    async def _search_via_api_multi_page(
        self,
        category: str,
        limit: int = 30,
        proxy_url: str | None = None,
        sort_by: str = "relevancy",
    ) -> list[ProductRecord]:
        """
        Lấy nhiều trang API để có đủ sản phẩm.
        Shopee giới hạn 60 items/request, nên với limit > 60 cần page 2.
        """
        all_products: list[ProductRecord] = []
        seen_ids: set[str] = set()
        page_size = 60
        pages_needed = math.ceil(limit / page_size)

        for page_idx in range(pages_needed):
            newest = page_idx * page_size
            batch = await self._search_via_api_page(
                category,
                limit=page_size,
                proxy_url=proxy_url,
                sort_by=sort_by,
                newest=newest,
            )
            if not batch:
                break
            for p in batch:
                if p.product_id not in seen_ids:
                    seen_ids.add(p.product_id)
                    all_products.append(p)
            # Nếu trang này ít kết quả thì không cần trang tiếp
            if len(batch) < page_size * 0.5:
                break
            # Delay giữa các page để tránh rate limit
            if page_idx < pages_needed - 1:
                await asyncio.sleep(random.uniform(0.8, 1.5))

        return all_products[:limit]

    async def _search_via_api_page(
        self,
        category: str,
        limit: int = 60,
        proxy_url: str | None = None,
        sort_by: str = "relevancy",
        newest: int = 0,
    ) -> list[ProductRecord]:
        """Search một page từ Shopee API."""
        import json as _json
        cookie_str = self.settings.integrations.shopee_affiliate_cookie or ""
        if not cookie_str:
            return []
        try:
            cookies_raw = _json.loads(cookie_str)
        except Exception:
            return []
        cookie_dict = {
            str(c["name"]): str(c["value"])
            for c in cookies_raw
            if c.get("name") and c.get("value") is not None and ".shopee.vn" in c.get("domain", "")
        }
        ua = random.choice(USER_AGENTS)
        headers = {
            "User-Agent": ua,
            "Referer": f"https://shopee.vn/search?keyword={quote(category)}&by={sort_by}",
            "x-api-source": "pc",
            "x-csrftoken": cookie_dict.get("csrftoken", ""),
            "Accept": "application/json",
            "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8",
        }
        params: dict[str, Any] = {
            "by": sort_by,
            "keyword": category,
            "limit": min(limit, 60),
            "newest": newest,
            "order": "desc",
            "page_type": "search",
            "scenario": "PAGE_GLOBAL_SEARCH",
            "version": 2,
        }
        proxy_dict = {"http://": proxy_url, "https://": proxy_url} if proxy_url else None

        # Thử nhiều endpoints
        for endpoint in SHOPEE_API_ENDPOINTS:
            try:
                async with httpx.AsyncClient(
                    timeout=25.0,
                    follow_redirects=True,
                    headers=headers,
                    cookies=cookie_dict,
                    proxies=proxy_dict,
                ) as client:
                    resp = await client.get(endpoint, params=params)
                    if resp.status_code != 200:
                        logger.debug("Shopee API %s returned %s for %s", endpoint, resp.status_code, category)
                        continue
                    data = resp.json()
                    if data.get("error") == 90309999:
                        logger.debug("Shopee API captcha error for %s at %s", category, endpoint)
                        continue
                    items = data.get("items") or data.get("item_basic_list") or []
                    if items:
                        return await self._parse_api_items(items, category)
            except Exception as exc:
                logger.debug("Shopee httpx API failed at %s for %s: %s", endpoint, category, exc)
                continue

        return []

    async def _parse_api_items(self, items: list[Any], category: str) -> list[ProductRecord]:
        """Parse danh sách item từ Shopee API response."""
        products: list[ProductRecord] = []
        # Xử lý song song affiliate link để nhanh hơn
        tasks = []
        valid_items = []
        for item in items:
            info = item.get("item_basic") or item
            shop_id = str(info.get("shopid", ""))
            item_id = str(info.get("itemid", ""))
            if not shop_id or not item_id:
                continue
            valid_items.append((info, shop_id, item_id))

        # Tạo affiliate links song song
        affiliate_tasks = []
        canonicals = []
        for info, shop_id, item_id in valid_items:
            canonical = f"https://shopee.vn/product/{shop_id}/{item_id}"
            canonicals.append(canonical)
            affiliate_tasks.append(self.affiliate_api.generate_affiliate_link(canonical))

        try:
            affiliate_links = await asyncio.gather(*affiliate_tasks, return_exceptions=True)
        except Exception:
            affiliate_links = [""] * len(valid_items)

        # Download images song song (tối đa 5 cùng lúc để tránh overload)
        semaphore = asyncio.Semaphore(5)

        async def safe_download(product: ProductRecord) -> ProductRecord:
            async with semaphore:
                if self.settings.features.download_assets:
                    image_path = await self.download_best_image(product)
                    product.image_path = str(image_path) if image_path else None
            return product

        product_list_tmp: list[ProductRecord] = []
        for idx, (info, shop_id, item_id) in enumerate(valid_items):
            images_raw = info.get("images") or []
            images = [f"https://down-vn.img.susercontent.com/file/{img}" for img in images_raw[:5] if img]
            price_raw = info.get("price") or info.get("price_min") or 0
            price = price_raw / 100000 if price_raw > 100000 else float(price_raw)
            original_price_raw = info.get("price_before_discount") or price_raw
            original_price = original_price_raw / 100000 if original_price_raw > 100000 else float(original_price_raw)
            discount_raw = info.get("discount") or "0"
            if isinstance(discount_raw, str):
                discount_raw = discount_raw.replace("%", "")
            try:
                discount = float(discount_raw)
            except (ValueError, TypeError):
                discount = 0.0
            aff_link = affiliate_links[idx] if idx < len(affiliate_links) and isinstance(affiliate_links[idx], str) else ""
            product = ProductRecord(
                product_id=item_id,
                name=info.get("name", f"{category} {item_id}"),
                price=price,
                original_price=original_price,
                discount_percent=discount,
                sold_count=int(info.get("historical_sold") or info.get("sold") or 0),
                rating=float(info.get("item_rating", {}).get("rating_star") or info.get("rating_star") or 0.0),
                review_count=int(
                    info.get("item_rating", {}).get("rating_count", [0])[0]
                    if isinstance(info.get("item_rating", {}).get("rating_count"), list)
                    else info.get("comment_count") or 0
                ),
                shop_name=info.get("shop_name") or "",
                shop_rating=0.0,
                category=category,
                subcategory="",
                images=images,
                product_url=canonicals[idx] if idx < len(canonicals) else f"https://shopee.vn/product/{shop_id}/{item_id}",
                affiliate_link=aff_link,
            )
            product_list_tmp.append(product)

        # Download images song song
        products = list(await asyncio.gather(*[safe_download(p) for p in product_list_tmp]))
        return products

    def _is_usable_image(self, image: Image.Image) -> bool:
        width, height = image.size
        if width < self.settings.shopee.image_min_width:
            return False
        ratio = width / max(height, 1)
        target_ratios = [1.0, 4 / 3, 3 / 4, 16 / 9, 9 / 16]
        acceptable = any(abs(ratio - target) <= 0.25 for target in target_ratios)
        if not acceptable:
            return False
        histogram = image.convert("L").histogram()
        white_pixels = sum(histogram[240:256])
        total_pixels = sum(histogram) or 1
        if white_pixels / total_pixels > 0.85:
            return False
        return True

    async def _random_delay(self) -> None:
        delay = random.uniform(0.8, 3.2)
        await asyncio.sleep(delay)

    def _extract_product_id(self, url: str) -> str | None:
        match = re.search(r"-i\.(\d+)\.(\d+)", url)
        if match:
            return match.group(2)
        alt_match = re.search(r"itemid=(\d+)", url)
        if alt_match:
            return alt_match.group(1)
        return None

    def _canonical_product_url(self, url: str) -> str:
        """Convert any Shopee product URL to canonical /product/shopid/itemid format."""
        match = re.search(r"-i\.(\d+)\.(\d+)", url)
        if match:
            return f"https://shopee.vn/product/{match.group(1)}/{match.group(2)}"
        return url

    def _parse_vnd(self, text: str) -> float:
        matches = re.findall(r"(?:₫|đ)\s*([\d\.,]+)|([\d\.,]+)\s*(?:₫|đ)", text, flags=re.IGNORECASE)
        if not matches:
            return 0.0
        raw = next((item[0] or item[1] for item in matches if item[0] or item[1]), "0")
        cleaned = raw.replace(".", "").replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return 0.0

    def _parse_original_price(self, text: str) -> float:
        prices = re.findall(r"([\d\.,]+)\s*(?:₫|đ)", text, flags=re.IGNORECASE)
        if len(prices) < 2:
            return self._parse_vnd(text)
        cleaned = prices[1].replace(".", "").replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return self._parse_vnd(text)

    def _extract_discount(self, text: str) -> float:
        match = re.search(r"(\d{1,2})%", text)
        return float(match.group(1)) if match else 0.0

    def _extract_sold_count(self, text: str) -> int:
        match = re.search(r"(?:Đã bán|sold)\s*([\d\.,]+)k?", text, flags=re.IGNORECASE)
        if not match:
            return 0
        raw = match.group(1).replace(",", ".")
        value = float(raw)
        if "k" in match.group(0).lower():
            value *= 1000
        return int(value)

    def _extract_rating(self, text: str) -> float:
        match = re.search(r"(\d\.\d)\s*/\s*5|([1-5]\.\d)", text)
        if not match:
            return 0.0
        raw = match.group(1) or match.group(2)
        return float(raw)

    def _extract_reviews(self, text: str) -> int:
        match = re.search(r"([\d\.,]+)\s*(?:đánh giá|reviews?)", text, flags=re.IGNORECASE)
        if not match:
            return 0
        raw = match.group(1).replace(".", "").replace(",", "")
        return int(raw) if raw.isdigit() else 0

    def _extract_shop_name(self, text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if line.lower() in {"shop", "cửa hàng"} and index + 1 < len(lines):
                return lines[index + 1]
        return ""
