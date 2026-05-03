"""
Tự động scrape proxy public miễn phí từ nhiều nguồn, test và lưu vào settings.
Chạy hàng ngày để luôn có pool proxy sạch.

Nguồn sử dụng:
- proxyscrape.com API (HTTP/HTTPS, không cần đăng ký)
- github.com/proxifly/free-proxy-list (HTTP/HTTPS)
- github.com/TheSpeedX/PROXY-List
- openproxylist.xyz
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx

from common.config import load_settings, save_runtime_config
from common.logging import get_logger

logger = get_logger(__name__)

# Các nguồn proxy public, trả về text "ip:port" mỗi dòng
_SOURCES: list[dict[str, Any]] = [
    {
        "name": "proxyscrape-https",
        "url": "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=yes&anonymity=all",
        "format": "ip:port",
    },
    {
        "name": "proxyscrape-http",
        "url": "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=no&anonymity=anonymous",
        "format": "ip:port",
    },
    {
        "name": "proxifly",
        "url": "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
        "format": "ip:port",
    },
    {
        "name": "speedx-http",
        "url": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "format": "ip:port",
    },
    {
        "name": "openproxy-http",
        "url": "https://openproxylist.xyz/http.txt",
        "format": "ip:port",
    },
    {
        "name": "monosans",
        "url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "format": "ip:port",
    },
]

_TEST_URL = "https://httpbin.org/ip"
_TEST_TIMEOUT = 8.0
_MAX_CONCURRENT_TESTS = 50
_MIN_HEALTHY = 10
_MAX_PROXIES_TO_KEEP = 80


async def scrape_all_sources(client: httpx.AsyncClient) -> list[str]:
    """Scrape proxy từ tất cả nguồn, trả về list ip:port unique."""
    raw: set[str] = set()
    for source in _SOURCES:
        try:
            r = await client.get(source["url"], timeout=15.0)
            if r.status_code == 200:
                text = r.text
                matches = re.findall(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{2,5})\b", text)
                raw.update(matches)
                logger.info("Scraped %d proxies from %s", len(matches), source["name"])
        except Exception as exc:
            logger.warning("Failed to scrape %s: %s", source["name"], exc)
    return list(raw)


async def test_proxy(proxy_url: str, semaphore: asyncio.Semaphore) -> tuple[str, bool, float]:
    """Test 1 proxy, trả về (url, is_alive, latency_ms)."""
    async with semaphore:
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(
                proxy=proxy_url,
                timeout=_TEST_TIMEOUT,
                follow_redirects=False,
            ) as client:
                r = await client.get(_TEST_URL)
                if r.status_code == 200:
                    latency = (time.monotonic() - start) * 1000
                    return proxy_url, True, latency
        except Exception:
            pass
        return proxy_url, False, 99999.0


async def scrape_and_test(max_test: int = 300) -> list[str]:
    """
    Scrape proxy từ tất cả nguồn, test, trả về list proxy URL sống.
    Format trả về: ["http://ip:port", ...]
    """
    logger.info("Starting proxy scrape...")
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        raw_list = await scrape_all_sources(client)

    logger.info("Scraped %d unique raw proxies, testing up to %d...", len(raw_list), max_test)

    # Shuffle và lấy sample để test nhanh hơn
    import random
    random.shuffle(raw_list)
    sample = raw_list[:max_test]

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_TESTS)
    tasks = [test_proxy(f"http://{addr}", semaphore) for addr in sample]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    alive: list[tuple[str, float]] = []
    for result in results:
        if isinstance(result, Exception):
            continue
        proxy_url, is_alive, latency = result
        if is_alive:
            alive.append((proxy_url, latency))

    # Sort theo latency tốt nhất trước
    alive.sort(key=lambda x: x[1])
    alive_urls = [url for url, _ in alive[:_MAX_PROXIES_TO_KEEP]]

    logger.info(
        "Proxy test done: %d/%d alive (best latency %.0fms)",
        len(alive_urls),
        len(sample),
        alive[0][1] if alive else 0,
    )
    return alive_urls


async def refresh_proxy_pool(force: bool = False) -> dict[str, Any]:
    """
    Refresh proxy pool: scrape → test → lưu vào runtime config.
    Gọi bởi scheduler hàng ngày.
    """
    settings = load_settings(refresh=True)
    current_proxies = settings.integrations.proxy_list or []

    # Kiểm tra pool hiện tại còn đủ sống không
    if not force and len(current_proxies) >= _MIN_HEALTHY:
        from modules.shopee.proxy_pool import ProxyPool
        pool = ProxyPool()
        health = await pool.health_check()
        alive_count = sum(1 for ok in health.values() if ok)
        if alive_count >= _MIN_HEALTHY:
            logger.info("Proxy pool still healthy (%d alive), skipping scrape", alive_count)
            return {"skipped": True, "alive": alive_count, "total": len(current_proxies)}

    alive_urls = await scrape_and_test(max_test=400)

    if not alive_urls:
        logger.warning("Proxy scrape returned 0 alive proxies, keeping existing pool")
        return {"error": "no_alive_proxies", "kept_existing": len(current_proxies)}

    # Lưu vào runtime config
    updated = save_runtime_config({
        "integrations": {
            **settings.integrations.model_dump(mode="json"),
            "proxy_list": alive_urls,
        }
    })

    logger.info("Proxy pool updated: %d proxies saved", len(alive_urls))
    return {
        "refreshed": True,
        "count": len(alive_urls),
        "sample": alive_urls[:3],
    }
