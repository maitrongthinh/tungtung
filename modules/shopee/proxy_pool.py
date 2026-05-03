from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from random import randint
from typing import Any

import httpx

from common.config import load_settings
from common.logging import get_logger

_SETTINGS_CACHE_TTL = 8.0

logger = get_logger(__name__)


@dataclass(slots=True)
class ProxyEndpoint:
    url: str
    healthy: bool = True
    request_count: int = 0
    failure_count: int = 0
    sticky_sessions: set[str] = field(default_factory=set)


class ProxyPool:
    def __init__(self, proxies: list[str] | None = None, rotate_every: int | None = None) -> None:
        self._configured_proxies = proxies
        self._configured_rotate_every = rotate_every
        self.proxies: list[ProxyEndpoint] = []
        self.rotate_every = rotate_every or load_settings().shopee.proxy_rotate_every
        self._cursor = 0
        self._sticky_map: dict[str, str] = {}
        self.error_log_path = Path(load_settings().log_dir / "error.log")
        self._last_sync: float = 0.0
        self._sync_from_settings()

    def _load_proxy_list(self) -> list[str]:
        if self._configured_proxies is not None:
            return [item.strip() for item in self._configured_proxies if item.strip()]
        return [item.strip() for item in load_settings(refresh=True).integrations.proxy_list if item.strip()]

    def _sync_from_settings(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_sync) < _SETTINGS_CACHE_TTL:
            return
        self._last_sync = now
        settings = load_settings(refresh=True)
        existing = {proxy.url: proxy for proxy in self.proxies}
        self.proxies = [existing.get(url, ProxyEndpoint(url=url)) for url in self._load_proxy_list()]
        self.rotate_every = self._configured_rotate_every or settings.shopee.proxy_rotate_every
        self.error_log_path = Path(settings.log_dir / "error.log")
        active_urls = {proxy.url for proxy in self.proxies}
        self._sticky_map = {
            session_key: proxy_url
            for session_key, proxy_url in self._sticky_map.items()
            if proxy_url in active_urls
        }

    async def health_check(self, test_url: str = "https://httpbin.org/ip") -> dict[str, bool]:
        self._sync_from_settings(force=True)
        if not self.proxies:
            return {}
        results: dict[str, bool] = {}
        for proxy in self.proxies:
            try:
                async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, proxy=proxy.url) as client:
                    response = await client.get(test_url)
                proxy.healthy = response.status_code == 200
                if proxy.healthy:
                    proxy.failure_count = 0
            except Exception as exc:
                proxy.healthy = False
                await self.report_failure(proxy.url, exc)
            results[proxy.url] = proxy.healthy
        return results

    def acquire(self, session_key: str | None = None) -> str | None:
        self._sync_from_settings()
        if not self.proxies:
            return None
        if session_key and session_key in self._sticky_map:
            proxy_url = self._sticky_map[session_key]
            proxy = self._find_proxy(proxy_url)
            if proxy and proxy.healthy:
                proxy.request_count += 1
                return proxy.url

        candidates = [proxy for proxy in self.proxies if proxy.healthy]
        if not candidates:
            return None
        proxy = candidates[self._cursor % len(candidates)]
        if proxy.request_count >= self.rotate_every:
            self._cursor = randint(0, len(candidates) - 1)
            proxy = candidates[self._cursor]
            proxy.request_count = 0
        proxy.request_count += 1
        self._cursor += 1
        if session_key:
            proxy.sticky_sessions.add(session_key)
            self._sticky_map[session_key] = proxy.url
        return proxy.url

    def release(self, session_key: str) -> None:
        self._sync_from_settings()
        proxy_url = self._sticky_map.pop(session_key, None)
        if not proxy_url:
            return
        proxy = self._find_proxy(proxy_url)
        if proxy:
            proxy.sticky_sessions.discard(session_key)

    async def report_failure(self, proxy_url: str, exc: Exception | None = None) -> None:
        self._sync_from_settings()
        proxy = self._find_proxy(proxy_url)
        if not proxy:
            return
        proxy.failure_count += 1
        proxy.healthy = False
        message = f"Proxy failure {proxy_url}: {exc or 'unknown error'}"
        self.error_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.error_log_path.open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")
        logger.warning(message)

    def mark_healthy(self, proxy_url: str) -> None:
        self._sync_from_settings()
        proxy = self._find_proxy(proxy_url)
        if proxy:
            proxy.healthy = True

    def summary(self) -> dict[str, Any]:
        self._sync_from_settings()
        return {
            "total": len(self.proxies),
            "alive": sum(1 for proxy in self.proxies if proxy.healthy),
            "failed": sum(proxy.failure_count for proxy in self.proxies),
        }

    def _find_proxy(self, proxy_url: str) -> ProxyEndpoint | None:
        for proxy in self.proxies:
            if proxy.url == proxy_url:
                return proxy
        return None
