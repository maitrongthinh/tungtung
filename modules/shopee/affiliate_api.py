from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import httpx

from common.config import load_settings
from common.database import Database
from common.logging import get_logger
from common.models import ProductRecord
from modules.shopee.cookie_affiliate import CookieAffiliateClient, load_cookie_client_from_config

logger = get_logger(__name__)

GRAPHQL_ENDPOINT = "https://open-api.affiliate.shopee.vn/graphql"
GENERATE_SHORT_LINK_MUTATION = """
mutation GenerateShortLink($originUrl: String!, $subIds: [String!]) {
  generateShortLink(input: {originUrl: $originUrl, subIds: $subIds}) {
    shortLink
  }
}
"""

PRODUCT_OFFERS_QUERY = """
query ProductOfferV2($keyword: String!, $sortType: Int, $page: Int!, $limit: Int!) {
  productOfferV2(keyword: $keyword, sortType: $sortType, page: $page, limit: $limit) {
    nodes {
      productId
      productName
      commissionRate
      price
      priceMin
      priceMax
      imageUrl
      offerLink
      shopId
      shopName
      soldCount
      ratingStar
    }
    pageInfo {
      page
      limit
      hasNextPage
    }
  }
}
"""

CONVERSION_REPORT_QUERY = """
query ConversionReportV2($startTime: Int!, $endTime: Int!, $page: Int!, $limit: Int!, $subIds: [String!]) {
  conversionReportV2(startTime: $startTime, endTime: $endTime, page: $page, limit: $limit, subIds: $subIds) {
    nodes {
      orderId
      itemId
      itemName
      quantity
      price
      commission
      netCommission
      purchaseStatus
      purchaseTime
      clickTime
      subIds
    }
    pageInfo {
      page
      limit
      hasNextPage
    }
  }
}
"""


class ShopeeAffiliateAPI:
    def __init__(self, database: Database, endpoint: str = GRAPHQL_ENDPOINT) -> None:
        self.database = database
        self.endpoint = endpoint
        self.settings = load_settings()
        self.timeout = self.settings.shopee.request_timeout_seconds
        self._cookie_client: CookieAffiliateClient | None = load_cookie_client_from_config()

    async def generate_affiliate_link(self, product_url: str) -> str:
        cached = self.database.get_cached_affiliate_link(product_url)
        if cached:
            return cached[0]

        settings = load_settings(refresh=True)
        token = settings.integrations.shopee_affiliate_token
        auth_mode = settings.shopee.affiliate_auth_mode.lower()
        has_api_token = bool(token) or auth_mode == "sha256"

        # Reload cookie client khi config thay đổi hoặc cookie mới được set
        new_cookie_source = getattr(settings.integrations, "shopee_affiliate_cookie", "")
        current_source = getattr(self._cookie_client, "_cookie_source", None) if self._cookie_client else None
        if new_cookie_source and new_cookie_source != current_source:
            self._cookie_client = CookieAffiliateClient(new_cookie_source)
        elif not new_cookie_source:
            self._cookie_client = None

        # Ưu tiên 1: Open API (GraphQL) nếu có token
        if has_api_token:
            sub_id_prefix = settings.integrations.shopee_sub_id_prefix or "shopee-agent"
            sub_ids = [f"{sub_id_prefix}-{datetime.now(UTC).strftime('%Y%m%d')}"]
            try:
                data = await self._graphql(
                    GENERATE_SHORT_LINK_MUTATION,
                    {"originUrl": product_url, "subIds": sub_ids},
                )
                short_link = data["generateShortLink"]["shortLink"]
                self.database.cache_affiliate_link(
                    product_url,
                    short_link,
                    ttl_hours=settings.shopee.affiliate_cache_ttl_hours,
                )
                return short_link
            except Exception as exc:
                logger.warning("Affiliate API failed for %s, trying cookie fallback: %s", product_url, exc)

        # Ưu tiên 2: Cookie-based (Playwright)
        if self._cookie_client:
            try:
                link = await self._cookie_client.generate_link(product_url)
                if link:
                    self.database.cache_affiliate_link(
                        product_url,
                        link,
                        ttl_hours=settings.shopee.affiliate_cache_ttl_hours,
                    )
                    return link
                logger.warning("Cookie affiliate returned no link for %s", product_url)
            except Exception as exc:
                logger.warning("Cookie affiliate error for %s: %s", product_url, exc)

        # Ưu tiên 3: Fallback link với UTM params
        if not has_api_token and self._cookie_client is None:
            logger.info("No affiliate method configured for %s, using fallback", product_url)
        fallback = self._build_fallback_link(product_url)
        self.database.cache_affiliate_link(
            product_url,
            fallback,
            ttl_hours=settings.shopee.affiliate_cache_ttl_hours,
        )
        return fallback

    async def get_commission_rate(self, product_id: str) -> float:
        offers = await self.get_trending_products(product_id, limit=5)
        for offer in offers:
            if offer.product_id == product_id and offer.commission_rate:
                return offer.commission_rate
        if offers:
            return offers[0].commission_rate
        return 0.0

    async def get_trending_products(self, category: str, limit: int = 20) -> list[ProductRecord]:
        data = await self._graphql(
            PRODUCT_OFFERS_QUERY,
            {"keyword": category, "sortType": 1, "page": 1, "limit": limit},
        )
        nodes = data.get("productOfferV2", {}).get("nodes", [])
        products: list[ProductRecord] = []
        for node in nodes:
            product_url = node.get("offerLink") or ""
            affiliate_link = product_url
            products.append(
                ProductRecord(
                    product_id=str(node.get("productId")),
                    name=node.get("productName", ""),
                    price=self._normalize_price(node.get("price") or node.get("priceMin") or 0),
                    original_price=self._normalize_price(node.get("priceMax") or node.get("price") or 0),
                    discount_percent=0.0,
                    sold_count=int(node.get("soldCount") or 0),
                    rating=float(node.get("ratingStar") or 0.0),
                    review_count=0,
                    shop_name=node.get("shopName", ""),
                    shop_rating=0.0,
                    category=category,
                    subcategory="",
                    images=[node.get("imageUrl")] if node.get("imageUrl") else [],
                    product_url=product_url,
                    affiliate_link=affiliate_link,
                    commission_rate=self._parse_percent(node.get("commissionRate")),
                )
            )
        return products

    async def get_product_performance(self, link_id: str) -> dict[str, Any]:
        now = datetime.now(UTC)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        data = await self._graphql(
            CONVERSION_REPORT_QUERY,
            {
                "startTime": int(start.timestamp()),
                "endTime": int(now.timestamp()),
                "page": 1,
                "limit": 50,
                "subIds": [link_id],
            },
        )
        nodes = data.get("conversionReportV2", {}).get("nodes", [])
        clicks = len(nodes)
        total_commission = sum(self._normalize_price(node.get("commission") or 0) for node in nodes)
        return {
            "link_id": link_id,
            "orders": clicks,
            "commission": total_commission,
            "items": nodes,
        }

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = {"query": query, "variables": variables}
        headers = self._build_headers(payload)
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            for attempt in range(3):
                try:
                    response = await client.post(self.endpoint, json=payload, headers=headers)
                    response.raise_for_status()
                    body = response.json()
                    if body.get("errors"):
                        raise RuntimeError(str(body["errors"]))
                    return body["data"]
                except Exception as exc:
                    last_error = exc
                    logger.warning("Shopee GraphQL attempt %s failed: %s", attempt + 1, exc)
                    if attempt < 2:
                        await self._sleep_backoff(attempt)
        raise RuntimeError(f"Shopee GraphQL failed after retries: {last_error}")

    def _build_headers(self, payload: dict[str, Any]) -> dict[str, str]:
        settings = load_settings(refresh=True)
        auth_mode = settings.shopee.affiliate_auth_mode.lower()
        token = settings.integrations.shopee_affiliate_token
        if auth_mode == "sha256":
            credential = settings.integrations.shopee_affiliate_credential
            secret = settings.integrations.shopee_affiliate_secret
            timestamp = str(int(datetime.now(UTC).timestamp()))
            signature = hmac.HMAC(
                secret.encode("utf-8"),
                msg=f"{timestamp}:{payload['query']}".encode("utf-8"),
                digestmod=hashlib.sha256,
            ).hexdigest()
            auth_value = f"SHA256 Credential={credential}, Signature={signature}, Timestamp={timestamp}"
        else:
            auth_value = f"Bearer {token}"
        return {
            "Authorization": auth_value,
            "Content-Type": "application/json",
        }

    async def _sleep_backoff(self, attempt: int) -> None:
        import asyncio

        await asyncio.sleep(2 ** attempt)

    def _build_fallback_link(self, product_url: str) -> str:
        publisher_id = load_settings(refresh=True).integrations.shopee_publisher_id
        parsed = urlparse(product_url)
        query = dict()
        if parsed.query:
            from urllib.parse import parse_qsl

            query.update(parse_qsl(parsed.query))
        if publisher_id:
            query["publisher_id"] = publisher_id
        query["utm_source"] = "fb_page"
        query["utm_medium"] = "affiliate"
        query["utm_campaign"] = "agent"
        return urlunparse(parsed._replace(query=urlencode(query)))

    def _normalize_price(self, value: Any) -> float:
        try:
            amount = float(value)
        except (TypeError, ValueError):
            return 0.0
        if amount >= 100000 and amount % 100000 == 0:
            return round(amount / 100000, 2)
        return round(amount, 2)

    def _parse_percent(self, value: Any) -> float:
        if value is None:
            return 0.0
        if isinstance(value, str):
            cleaned = value.replace("%", "").strip()
            try:
                return float(cleaned)
            except ValueError:
                return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
