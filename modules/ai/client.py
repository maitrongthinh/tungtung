from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import AsyncOpenAI

from common.config import load_settings
from common.logging import get_logger

logger = get_logger(__name__)

_MAX_RETRIES_PER_KEY = 2
_RATE_LIMIT_COOLDOWN = 60       # giây block key sau 429
_QUOTA_COOLDOWN = 3600          # giây block key sau quota exceeded (RPD hết)
_KEY_ERROR_COOLDOWN = 300       # giây block key sau lỗi khác


@dataclass
class _KeyState:
    key: str
    provider: str                   # "trollllm" | "gemini" | "openai"
    base_url: str
    model: str
    blocked_until: float = 0.0
    fail_count: int = 0
    total_calls: int = 0
    total_errors: int = 0

    def is_available(self) -> bool:
        return time.monotonic() >= self.blocked_until

    def block(self, seconds: float) -> None:
        self.blocked_until = time.monotonic() + seconds
        self.fail_count += 1
        logger.warning(
            "AI key blocked for %.0fs [provider=%s key=...%s]",
            seconds, self.provider, self.key[-8:],
        )

    def reset_fail(self) -> None:
        self.fail_count = 0


class _KeyPool:
    """Thread-safe (asyncio) pool của nhiều AI keys từ nhiều provider."""

    def __init__(self) -> None:
        self._keys: list[_KeyState] = []
        self._idx: int = 0

    def reload(self, settings_snapshot: Any) -> None:
        """Xây dựng/cập nhật pool từ settings — gọi mỗi khi cần."""
        integrations = settings_snapshot.integrations
        ai = settings_snapshot.ai
        new_keys: list[_KeyState] = []

        # Đọc danh sách providers từ integrations.ai_providers
        # Format: list of {"provider": "gemini", "key": "...", "model": "...", "base_url": "..."}
        providers_cfg: list[dict] = integrations.ai_providers or []

        for cfg in providers_cfg:
            provider = cfg.get("provider", "openai").lower()
            key = cfg.get("key", "").strip()
            if not key:
                continue
            base_url, model = _resolve_provider(provider, cfg, ai)
            new_keys.append(_KeyState(
                key=key,
                provider=provider,
                base_url=base_url,
                model=model,
            ))

        # Backward compat: ai_api_key đơn lẻ vẫn được dùng nếu providers rỗng
        if not new_keys:
            single_key = integrations.ai_api_key.strip()
            single_url = integrations.ai_base_url.strip()
            if single_key:
                provider = ai.provider.lower()
                new_keys.append(_KeyState(
                    key=single_key,
                    provider=provider,
                    base_url=single_url,
                    model=ai.model,
                ))

        if not new_keys:
            logger.warning("AI key pool is empty — no providers configured")
        else:
            logger.info("AI key pool loaded: %d key(s)", len(new_keys))

        # Giữ lại trạng thái block của key đã có (nếu key trùng)
        existing = {ks.key: ks for ks in self._keys}
        for ks in new_keys:
            if ks.key in existing:
                old = existing[ks.key]
                ks.blocked_until = old.blocked_until
                ks.fail_count = old.fail_count
                ks.total_calls = old.total_calls
                ks.total_errors = old.total_errors
        self._keys = new_keys

    def next_available(self) -> _KeyState | None:
        """Round-robin, bỏ qua key đang bị block."""
        if not self._keys:
            return None
        n = len(self._keys)
        for _ in range(n):
            ks = self._keys[self._idx % n]
            self._idx += 1
            if ks.is_available():
                return ks
        # Tất cả bị block — trả về cái hết block sớm nhất
        best = min(self._keys, key=lambda k: k.blocked_until)
        wait = best.blocked_until - time.monotonic()
        if wait > 0:
            logger.warning("All AI keys are blocked; will retry soonest in %.0fs", wait)
        return best

    def stats(self) -> list[dict]:
        now = time.monotonic()
        return [
            {
                "provider": ks.provider,
                "key_suffix": ks.key[-8:],
                "model": ks.model,
                "available": ks.is_available(),
                "blocked_remaining": max(0.0, ks.blocked_until - now),
                "total_calls": ks.total_calls,
                "total_errors": ks.total_errors,
            }
            for ks in self._keys
        ]


# Singleton pool dùng chung toàn app
_pool = _KeyPool()
_pool_last_reload: float = 0.0
_POOL_RELOAD_INTERVAL = 120   # reload pool mỗi 2 phút để nhận key mới


def _resolve_provider(provider: str, cfg: dict, ai_settings: Any) -> tuple[str, str]:
    """Trả về (base_url, model) cho provider."""
    if provider == "gemini":
        # Google AI Studio — dùng OpenAI-compatible endpoint
        base_url = cfg.get("base_url", "https://generativelanguage.googleapis.com/v1beta/openai")
        model = cfg.get("model", "gemini-2.0-flash")
        return base_url, model
    if provider == "trollllm":
        base_url = cfg.get("base_url", "https://chat.trollllm.xyz/v1")
        model = cfg.get("model", "claude-sonnet-4-6")
        return base_url, model
    if provider in ("openai", "anthropic"):
        base_url = cfg.get("base_url", "https://api.openai.com/v1")
        model = cfg.get("model", ai_settings.model)
        return base_url, model
    # Custom provider
    base_url = cfg.get("base_url", "https://api.openai.com/v1")
    model = cfg.get("model", ai_settings.model)
    return base_url, model


def _is_rate_limit(err: str) -> bool:
    return any(x in err for x in ("429", "rate_limit", "Rate limit", "rate limit", "RateLimitError"))


def _is_quota_exceeded(err: str) -> bool:
    return any(x in err for x in ("quota", "RESOURCE_EXHAUSTED", "exceeded your current quota", "billing"))


def _is_auth_error(err: str) -> bool:
    return any(x in err for x in ("401", "403", "invalid_api_key", "API key", "authentication"))


def _ensure_pool_fresh() -> None:
    global _pool_last_reload
    now = time.monotonic()
    if now - _pool_last_reload > _POOL_RELOAD_INTERVAL:
        settings = load_settings(refresh=True)
        _pool.reload(settings)
        _pool_last_reload = now


class JSONModelClient(Protocol):
    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 1200,
        temperature: float = 0.2,
    ) -> dict[str, Any]: ...


# ── Reuse AsyncOpenAI clients per (key, base_url) ──────────────
_client_cache: dict[str, AsyncOpenAI] = {}

def _get_or_create_client(api_key: str, base_url: str) -> AsyncOpenAI:
    cache_key = f"{api_key[-8:]}:{base_url}"
    client = _client_cache.get(cache_key)
    if client is None:
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url if base_url else None,
            timeout=90.0,
            max_retries=0,
        )
        _client_cache[cache_key] = client
    return client


class OpenAIJSONClient:
    """
    Multi-provider AI client với key rotation tự động.
    Khi gặp 429 / quota / lỗi → block key đó, chuyển sang key tiếp theo ngay.
    """

    def __init__(self, model: str | None = None) -> None:
        self.model = model
        self.last_usage: dict[str, int] | None = None

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 1200,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        _ensure_pool_fresh()
        settings = load_settings()

        # Số lần thử tối đa = số key × 2 (mỗi key thử 2 lần)
        max_attempts = max(len(_pool._keys) * _MAX_RETRIES_PER_KEY, 4)
        last_exc: Exception | None = None

        for attempt in range(max_attempts):
            ks = _pool.next_available()
            if ks is None:
                raise ValueError("Không có AI key nào khả dụng")

            # Nếu key vẫn đang bị block, chờ
            wait = ks.blocked_until - time.monotonic()
            if wait > 0:
                logger.info("Waiting %.0fs for soonest available AI key [%s]", wait, ks.provider)
                await asyncio.sleep(min(wait, 30))

            model = self.model or ks.model
            try:
                result = await self._call_once(ks, model, system_prompt, user_prompt, max_tokens, temperature)
                ks.reset_fail()
                ks.total_calls += 1
                return result
            except Exception as e:
                ks.total_errors += 1
                err_str = str(e)
                last_exc = e

                if _is_rate_limit(err_str):
                    # Block key ngắn, key khác tiếp quản ngay
                    ks.block(_RATE_LIMIT_COOLDOWN)
                    logger.info("Rate limit on %s, rotating to next key (attempt %d)", ks.provider, attempt + 1)
                    continue

                if _is_quota_exceeded(err_str):
                    # Block key dài (quota ngày hết)
                    ks.block(_QUOTA_COOLDOWN)
                    logger.warning("Quota exceeded on %s, blocked for 1h", ks.provider)
                    continue

                if _is_auth_error(err_str):
                    # Block key rất lâu (key sai/hết hạn)
                    ks.block(_QUOTA_COOLDOWN * 24)
                    logger.error("Auth error on key ...%s (%s), blocked indefinitely", ks.key[-8:], ks.provider)
                    continue

                # Lỗi tạm thời khác (network, server 5xx)
                backoff = min(5 * (attempt + 1), 30)
                logger.warning("AI error [%s] attempt %d: %s — retry in %ds", ks.provider, attempt + 1, err_str[:120], backoff)
                ks.block(backoff)
                await asyncio.sleep(backoff)
                continue

        raise ValueError(f"AI generation failed after {max_attempts} attempts across all keys: {last_exc}")

    async def _call_once(
        self,
        ks: _KeyState,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        client = _get_or_create_client(ks.key, ks.base_url)
        use_json_mode = (
            "gpt" in model.lower()
            and ks.base_url
            and "openai.com" in ks.base_url.lower()
        )
        create_kwargs: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if use_json_mode:
            create_kwargs["response_format"] = {"type": "json_object"}

        logger.debug("AI call: provider=%s model=%s key=...%s", ks.provider, model, ks.key[-8:])
        response = await client.chat.completions.create(**create_kwargs)
        if response.usage:
            self.last_usage = {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
            }
        content = response.choices[0].message.content or ""
        return self._extract_json(content)

    def _extract_json(self, text: str) -> dict[str, Any]:
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.split("\n")
            inner = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
            stripped = inner.strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(stripped[start : end + 1])
                except json.JSONDecodeError:
                    pass
        logger.warning("Model did not return valid JSON: %s", text[:200])
        raise ValueError("Invalid JSON response from AI provider")


def get_key_pool_stats() -> list[dict]:
    """Trả về trạng thái các key — dùng cho API /api/ai/keys."""
    _ensure_pool_fresh()
    return _pool.stats()
